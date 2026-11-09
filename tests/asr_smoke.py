"""本地模型冒烟：3秒合成静音，仅验证实际引擎可加载与完成，不评估准确率。"""
from pathlib import Path
import argparse
import json
import os
import sys
import tempfile
import time
import wave

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from core.asr import list_models, transcribe  # noqa: E402


def choose_model(requested: str | None) -> str:
    """不写死模型名：显式指定优先，否则取当前已下载的 Whisper 模型。"""
    available = [m["name"] for m in list_models() if m.get("available")]
    if requested:
        if requested not in available:
            raise SystemExit(f"模型 {requested} 未下载；当前可用：{', '.join(available) or '无'}")
        return requested
    if not available:
        raise SystemExit("data/models 中没有可用模型，请先在界面下载")
    return available[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=None, help="已下载的模型名，默认取第一个可用模型")
    args = parser.parse_args()
    model = choose_model(args.model)
    with tempfile.TemporaryDirectory(prefix="subtitle-local-model-") as temp:
        path = Path(temp) / "silence.wav"
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(b"\0\0" * 48000)
        started = time.monotonic()
        cues, info = transcribe(str(path), model_name=model, device="cpu", vad=True)
        result = {"passed": True, "model": model, "requested_device": "cpu", "offline_environment": True,
                  "fixture": "3-second synthetic silence", "elapsed_seconds": round(time.monotonic() - started, 2),
                  "cue_count": len(cues), "info": info, "scope": "engine smoke only; not speech accuracy or performance benchmark"}
        target = ROOT / "docs" / "修复验证" / "本地模型运行.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
