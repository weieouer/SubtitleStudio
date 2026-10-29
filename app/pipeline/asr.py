# -*- coding: utf-8 -*-
"""faster-whisper 封装：只关心字级时间，不关心识别文字对不对。"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable

# 国内下载模型走镜像；必须在导入 huggingface_hub 之前设置
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

from faster_whisper import WhisperModel  # noqa: E402

_MODELS: dict[tuple[str, str], WhisperModel] = {}
_MODEL_LOCK = threading.RLock()
_TRANSCRIBE_LOCK = threading.Lock()
_CUDA_DISABLED = False
_CUDA_ERROR_HINTS = (
    "cublas", "cudnn", "cuda", "nvcuda", "no kernel image",
    "cuda driver", "gpu", "out of memory",
)

HF_REPOS = {
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
}

MODEL_DOWNLOAD_SIZES = {
    "small": "约 500 MB",
    "medium": "约 1.5 GB",
    "large-v3": "约 3 GB",
}


class ModelNotDownloadedError(RuntimeError):
    """模型尚未在本机，必须由用户在界面上主动下载。"""


def _local_model_dir(model_size: str, download_root: Path) -> Path | None:
    """优先用已经下载好的模型，网络不通时也能直接开工。"""
    repo = HF_REPOS.get(model_size)
    if repo is None:
        return None

    folder = "models--" + repo.replace("/", "--")
    snapshots = download_root / folder / "snapshots"
    if not snapshots.is_dir():
        return None

    for snapshot in sorted(snapshots.iterdir(), reverse=True):
        files = [
            snapshot / name
            for name in ("model.bin", "config.json", "tokenizer.json")
        ]
        if all(path.is_file() and path.stat().st_size > 0 for path in files):
            return snapshot
    return None


def has_local_model(model_size: str, download_root: Path) -> bool:
    """本机是否已有可直接加载的完整模型，用于给界面准确的联网提示。"""
    return _local_model_dir(model_size, download_root) is not None


def _load_model(
    source: str, device: str, download_root: Path
) -> WhisperModel:
    return WhisperModel(
        source,
        device=device,
        compute_type="float16" if device == "cuda" else "int8",
        download_root=str(download_root),
        local_files_only=True,
    )


def cuda_disabled() -> bool:
    return _CUDA_DISABLED


def _disable_cuda() -> None:
    global _CUDA_DISABLED
    _CUDA_DISABLED = True
    with _MODEL_LOCK:
        for key in [key for key in _MODELS if key[1] == "cuda"]:
            _MODELS.pop(key, None)


def _is_cuda_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(hint in message for hint in _CUDA_ERROR_HINTS)


def get_model(model_size: str, device: str, download_root: Path) -> WhisperModel:
    global _CUDA_DISABLED

    with _MODEL_LOCK:
        if device == "cuda" and _CUDA_DISABLED:
            device = "cpu"
        key = (model_size, device)
        if key in _MODELS:
            return _MODELS[key]

        local = _local_model_dir(model_size, download_root)
        if local is None:
            raise ModelNotDownloadedError(
                f"{model_size} 模型还没有下载。请先在界面里点击下载，再开始处理。"
            )
        source = str(local)

        if device == "cuda":
            try:
                model = _load_model(source, "cuda", download_root)
            except Exception:
                # 没装 CUDA / cuBLAS 运行库时自动退回 CPU，不让任务直接失败。
                _CUDA_DISABLED = True
                device = "cpu"
                key = (model_size, "cpu")
                if key in _MODELS:
                    return _MODELS[key]
                model = _load_model(source, "cpu", download_root)
        else:
            model = _load_model(source, "cpu", download_root)

        _MODELS[key] = model
        return model


def _run_transcribe(
    model: WhisperModel,
    media_path: Path,
    progress: Callable[[float], None] | None,
) -> tuple[list, object]:
    with _TRANSCRIBE_LOCK:
        segments_iter, info = model.transcribe(
            str(media_path),
            language="zh",
            word_timestamps=True,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
            beam_size=5,
            condition_on_previous_text=False,
            temperature=0.0,
        )

        duration = float(getattr(info, "duration", 0) or 0)
        segments = []
        for seg in segments_iter:
            segments.append(seg)
            if progress and duration > 0:
                progress(min(max(float(seg.end) / duration, 0.0), 1.0))

    if progress:
        progress(1.0)
    return segments, info


def transcribe(
    media_path: Path,
    model_size: str,
    device: str,
    download_root: Path,
    progress: Callable[[float], None] | None = None,
    on_fallback: Callable[[], None] | None = None,
) -> tuple[list, object]:
    """返回 (segments, info)。segments 里每个词都带时间戳。

    CUDA 运行库缺失时，模型可能在真正识别时才报 cublas/cudnn 错误；
    这里捕获后清掉 GPU 模型，并用 CPU 重跑一次。
    """
    model = get_model(model_size, device, download_root)
    if device == "cuda" and cuda_disabled() and on_fallback:
        on_fallback()

    try:
        return _run_transcribe(model, media_path, progress)
    except Exception as exc:
        if device != "cuda" or not _is_cuda_error(exc):
            raise
        _disable_cuda()
        if on_fallback:
            on_fallback()
        if progress:
            progress(0.0)
        cpu_model = get_model(model_size, "cpu", download_root)
        return _run_transcribe(cpu_model, media_path, progress)
