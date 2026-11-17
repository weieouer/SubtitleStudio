"""非 Whisper 引擎进程：主环境解码、VAD 和分块，独立环境加载模型。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import wave

import numpy as np

from .model_manager import CATALOG, runtime_python, model_directory, ensure_runtime
from .paths import RESULTS_DIR


def speech_chunks(samples, spans, sample_rate=16000, max_seconds=25, check=None):
    """优先在接近块尾的低能量区切分；无可靠静音时守住最大时长，连续覆盖原音频。"""
    frame = max(1, round(sample_rate*.02))
    limit = round(max_seconds*sample_rate)
    if limit < frame*10:
        raise ValueError('音频分块时长过短')
    chunks = []
    for span in spans:
        start, stop = int(span['start']), int(span['end'])
        while start < stop:
            if check: check()
            cut = min(stop, start+limit)
            if cut < stop:
                lo = max(start+frame*10, cut-round(sample_rate*2))
                count = (cut-lo)//frame
                if count >= 10:
                    block = np.asarray(samples[lo:lo+count*frame],dtype=np.float32).reshape(-1,frame)
                    energy = np.mean(block*block,axis=1)
                    quiet = np.convolve(energy,np.ones(10)/10,mode='valid')
                    at = int(np.argmin(quiet))
                    # 持续音乐或噪声没有明显低谷时，保留原切点，不凭最小值猜静音。
                    if quiet[at] < max(1e-10,float(np.percentile(energy,80))*.04):
                        cut = lo+(at+5)*frame
            chunks.append((start,cut))
            start = cut
    return chunks


def transcribe_external(path, name, device, vad, on_progress=None, cancel_event=None, audio=None):
    from .asr import Cancelled
    from .media import decode_audio, probe
    spec = CATALOG[name]
    ensure_runtime(spec['engine'], on_progress, cancel_event)
    def report(value, message):
        if on_progress:
            on_progress(value, message)
    def check():
        if cancel_event and cancel_event.is_set():
            raise Cancelled()
    report(.01, "解码音频…")
    samples = decode_audio(path, 16000, cancel_event) if audio is None else audio
    duration = len(samples) / 16000
    spans = [{"start": 0, "end": len(samples)}]
    if vad:
        from faster_whisper.vad import get_speech_timestamps, VadOptions
        spans = get_speech_timestamps(samples, VadOptions())
    chunks = speech_chunks(samples, spans, check=check)
    if not chunks:
        return [], {"duration": duration, "engine": spec["engine"]}
    with tempfile.TemporaryDirectory(prefix="engine-", dir=RESULTS_DIR) as temp:
        folder = Path(temp)
        files = []
        for i, (start, end) in enumerate(chunks):
            check()
            audio = folder / f"chunk_{i}.wav"
            with wave.open(str(audio), "wb") as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
                output.writeframes((np.clip(samples[start:end], -1, 1) * 32767).astype("<i2").tobytes())
            files.append({"path": str(audio), "start": start / 16000, "end": end / 16000})
        request = folder / "request.json"
        request.write_text(json.dumps({"engine": spec["engine"], "model": str(model_directory(name)),
                                       "device": device, "chunks": files}, ensure_ascii=False), encoding="utf-8")
        result_path, progress_path = folder / "result.json", folder / "progress.json"
        log_path = folder / "engine.log"
        report(.08, "加载 " + spec["label"] + "…")
        runner = Path(__file__).resolve().parents[1] / "engine_runner.py"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen([str(runtime_python(spec["engine"])), str(runner), str(request), str(result_path), str(progress_path)],
                stdout=log, stderr=subprocess.STDOUT,
                env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONUTF8": "1"},
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                while process.poll() is None:
                    check()
                    try:
                        progress = json.loads(progress_path.read_text(encoding="utf-8"))
                        report(.08 + .9 * progress["progress"], progress["message"])
                    except (OSError, ValueError, KeyError):
                        pass
                    if cancel_event:
                        cancel_event.wait(.3)
                    else:
                        import time
                        time.sleep(.3)
                if process.returncode or not result_path.exists():
                    from .windows_runtime import engine_failure
                    tail = engine_failure(log_path.read_text(encoding="utf-8", errors="replace"))
                    raise RuntimeError(spec["label"] + " 运行失败：" + tail)
                data = json.loads(result_path.read_text(encoding="utf-8"))
            finally:
                if process.poll() is None:
                    process.terminate(); process.wait(timeout=10)
        check()
        info = probe(path)
        info.update(duration=duration, engine=spec["engine"], timing="estimated_chunks" if spec["engine"] == "qwen" else "model_timestamps")
        from .audio_boundaries import refine_boundaries
        cues=refine_boundaries(data['cues'],samples,cancel_event=cancel_event)
        info['boundary_refinement']='rms_spectral_flux_hysteresis'
        return cues, info
