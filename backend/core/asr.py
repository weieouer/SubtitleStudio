"""faster-whisper 封装：本地模型解析、设备选择、进度与取消。"""
from __future__ import annotations

import math
import threading
from pathlib import Path

from .paths import MODELS_DIR
from .model_manager import CATALOG, runtime_ready, weights_present, model_directory, whisper_weights_present

REPO_PREFIX = "models--Systran--faster-whisper-"
_MODEL_LOCK = threading.RLock()
_MODEL_CACHE = {}


def clear_model_cache() -> None:
    """只保留当前 Whisper；切换引擎或对齐前释放，避免模型叠占显存。"""
    import gc
    with _MODEL_LOCK:
        _MODEL_CACHE.clear()
    gc.collect()


def _whisper_model(factory, path, device, compute):
    try:
        weights = (path / 'model.bin').stat()
        version = (weights.st_size, weights.st_mtime_ns)
    except OSError:
        version = None
    key = (str(path.resolve()), device, compute, version)
    with _MODEL_LOCK:
        if _MODEL_CACHE.get('key') == key:
            return _MODEL_CACHE['model'], True
        clear_model_cache()
        model = factory(str(path), device=device, compute_type=compute)
        _MODEL_CACHE.update(key=key, model=model)
        return model, False


class Cancelled(Exception):
    pass


def find_model(name: str) -> Path | None:
    """支持 HuggingFace 缓存目录与直接放置的模型目录。"""
    direct = model_directory(name)
    if (direct / '.studio-downloading').exists():
        return None
    if name in CATALOG and CATALOG[name]["engine"] != "whisper":
        return direct if weights_present(name) else None
    if whisper_weights_present(direct):
        return direct
    repo_id = CATALOG.get(name, {}).get('repo')
    repo = MODELS_DIR / ('models--' + repo_id.replace('/', '--') if repo_id else f"{REPO_PREFIX}{name}")
    snapshots = repo / "snapshots"
    if snapshots.is_dir():
        candidates = sorted(
            (d for d in snapshots.iterdir() if whisper_weights_present(d)),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    return None


def _dir_size(path: Path) -> int:
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return 0


def list_models() -> list[dict]:
    out = []
    names = list(CATALOG)
    for name in names:
        path = find_model(name)
        spec = CATALOG.get(name, {"label": name, "engine": "whisper", "description": "已有 Whisper 模型，兼容旧工程。", "size_hint": ""})
        out.append({
            "name": name,
            **spec,
            "available": path is not None,
            "downloaded": path is not None,
            "runtime_ready": runtime_ready(spec["engine"]),
            "path": str(path) if path else "",
            "size_gb": round(_dir_size(path) / (1024 ** 3), 2) if path else 0,
        })
    return out


def _cuda_runtime_ok() -> bool:
    """get_cuda_device_count 只查驱动；cuBLAS 缺失时提前回退 CPU。"""
    import ctypes
    import ctypes.util
    import os
    from .bundled_runtime import configure_cuda_libraries
    configure_cuda_libraries()
    if os.name == "nt":
        for name in ("cublas64_12.dll", "cublas64_11.dll"):
            try:
                ctypes.WinDLL(name)
                return True
            except OSError:
                continue
        return False
    try:
        return bool(ctypes.util.find_library("cublas"))  # type: ignore[attr-defined]
    except Exception:
        return False


def resolve_device(requested: str = "auto") -> tuple[str, str]:
    if requested == "cpu":
        return "cpu", "int8"
    if requested == "cuda":
        return "cuda", "float16"
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0 and _cuda_runtime_ok():
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def transcribe(path: str, model_name: str = "medium", device: str = "auto",
               vad: bool = True, on_progress=None, cancel_event=None, audio=None) -> tuple[list[dict], dict]:
    """转写音频/视频，返回 (cues, info)。on_progress(ratio, message)。"""
    if model_name in CATALOG and CATALOG[model_name]["engine"] != "whisper":
        clear_model_cache()
        from .external_asr import transcribe_external
        return transcribe_external(path, model_name, device, vad, on_progress, cancel_event, audio=audio)
    import gc

    from faster_whisper import WhisperModel

    from .media import DecodeCancelled, decode_audio

    def report(ratio: float, message: str) -> None:
        if on_progress:
            on_progress(max(0.0, min(1.0, ratio)), message)

    def check_cancel() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()

    def run(model) -> tuple[list[dict], object, float]:
        segments, info = model.transcribe(
            audio,
            language=None,
            vad_filter=vad,
            word_timestamps=True,
            no_speech_threshold=None,  # 疑似非人声只做标记，不按分数丢弃字幕。
            beam_size=5,
            patience=1.0,
            condition_on_previous_text=False,
            # 0 度卡在复读时依次升温重试，避免整段复读固定下来。
            temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
            # 词时间异常且前后都是静音的片段按幻觉丢弃，正常密集对白不受影响。
            hallucination_silence_threshold=2.0,
            initial_prompt="以下是普通话短剧对白，请输出简体中文并保留标点。",
        )
        duration = float(getattr(info, "duration", 0) or 0)
        cues: list[dict] = []
        for seg in segments:
            check_cancel()
            text = (seg.text or "").strip()
            if not text:
                continue
            words = []
            for w in (getattr(seg, "words", None) or []):
                words.append({
                    "start": round(float(w.start), 3),
                    "end": round(float(w.end), 3),
                    "word": w.word,
                    "probability": round(float(w.probability), 3) if getattr(w, "probability", None) is not None else None,
                })
            avg_logprob = float(getattr(seg, "avg_logprob", 0) or 0)
            no_speech = float(getattr(seg, "no_speech_prob", 0) or 0)
            confidence = max(0.0, min(1.0, math.exp(avg_logprob)))
            cues.append({
                "id": len(cues) + 1,
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "text": text,
                "words": words,
                "confidence": round(confidence, 3),
                "avg_logprob": round(avg_logprob, 3),
                "no_speech_prob": round(no_speech, 3),
                "is_noise": no_speech > 0.6,
                "needs_review": confidence < 0.45 or no_speech > 0.5,
                "edited": False,
                "estimated": False,
                "source": "asr",
            })
            if duration > 0:
                ratio = min(1.0, float(seg.end) / duration)
                report(0.08 + 0.91 * ratio, f"识别中 {int(ratio * 100)}%")
        return cues, info, duration

    model_path = find_model(model_name)
    if model_path is None:
        available = [m["name"] for m in list_models() if m["available"]]
        hint = f"当前可用：{', '.join(available)}" if available else "data/models 下还没有模型"
        raise RuntimeError(f"模型未下载：{model_name}（{hint}）")

    check_cancel()
    report(0.01, "读取音频（优先复用缓存）…")
    try:
        if audio is None:audio = decode_audio(str(path), 16000, cancel_event=cancel_event)
    except DecodeCancelled:
        raise Cancelled() from None
    check_cancel()

    dev, compute = resolve_device(device)
    attempts = [(dev, compute)]
    if dev == "cuda" and device == "auto":
        attempts.append(("cpu", "int8"))

    last_error: Exception | None = None
    for idx, (current_dev, current_compute) in enumerate(attempts):
        check_cancel()
        label = "GPU" if current_dev == "cuda" else "CPU"
        report(0.03 if idx == 0 else 0.05, f"加载模型 {model_name}（{label}）…")
        model = None
        try:
            model, reused = _whisper_model(WhisperModel, model_path, current_dev, current_compute)
            report(.06, ('复用已加载模型 ' if reused else '模型就绪 ') + model_name + f'（{label}）')
            cues, info, duration = run(model)
            from .audio_boundaries import refine_boundaries
            cues=refine_boundaries(cues,audio,cancel_event=cancel_event)
            report(1.0, f"完成，共 {len(cues)} 条")
            return cues, {
                "duration": round(duration, 3),
                "language": getattr(info, "language", "zh"),
                "device": current_dev,
                "compute_type": current_compute,
                "model": model_name,
                "boundary_refinement": "rms_spectral_flux_hysteresis",
            }
        except Cancelled:
            raise
        except Exception as exc:
            clear_model_cache()
            last_error = exc
            if idx + 1 < len(attempts):
                report(0.05, f"{label} 不可用，改用 CPU…")
                continue
            raise
        finally:
            del model
            gc.collect()
    raise RuntimeError(f"识别失败：{last_error}")
