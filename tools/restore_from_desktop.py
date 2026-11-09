#!/usr/bin/env python3
"""从桌面旧安装恢复主运行环境、Qwen 引擎环境和模型权重。

用法：
    runtime\\python\\python.exe tools\\restore_from_desktop.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime" / "python" / "python.exe"
VENV = ROOT / ".venv"
VENV_PY = VENV / "Scripts" / "python.exe"
WHEELS = ROOT / "wheels"
REQUIREMENTS = ROOT / "requirements.txt"
MODELS_DIR = ROOT / "data" / "models"
ENGINES_DIR = ROOT / "data" / "engines"
LOG_PATH = ROOT / "data" / "logs" / "restore.log"

DESKTOP = Path.home() / "Desktop"
OLD_ROOT = DESKTOP / "1" / "字幕工作室" / "SubtitleStudio"
SPARE_MODELS = DESKTOP / "字幕模型"

MODEL_NAMES = ("Whisper large-v3", "ForcedAligner-0.6b", "fireredasr2-aed")
ENGINE_NAMES = ("qwen",)
MAIN_IMPORTS = "import av, faster_whisper, numpy, jieba, rapidfuzz, pypinyin"
QWEN_IMPORTS = "from qwen_asr import Qwen3ASRModel, Qwen3ForcedAligner"

_log = None


def log(message=""):
    print(message, flush=True)
    if _log is not None:
        _log.write(message + "\n")
        _log.flush()


def human(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def long_path(path):
    """Windows 上加上 \\\\?\\ 前缀，避免深层 site-packages 超过 260 字符。"""
    text = os.path.abspath(str(path))
    if os.name == "nt" and not text.startswith("\\\\?\\"):
        return "\\\\?\\" + text
    return text


def transfer_tree(source, target):
    """同一磁盘优先直接移动（秒级、不占额外空间），跨盘或目标已存在时复制。"""
    source_path = long_path(source)
    target_path = long_path(target)
    if not os.path.exists(target_path):
        try:
            os.replace(source_path, target_path)
            log("    已移动（同盘秒移，不占额外空间）")
            return
        except OSError as error:
            log(f"    直接移动失败，改为复制：{error}")
    copy_tree(source, target)


def copy_tree(source, target):
    source_walk = long_path(source)
    target_walk = long_path(target)
    copied = skipped = 0
    copied_bytes = 0
    started = time.time()
    for base, _dirs, files in os.walk(source_walk):
        relative = os.path.relpath(base, source_walk)
        target_dir = target_walk if relative == "." else os.path.join(target_walk, relative)
        os.makedirs(target_dir, exist_ok=True)
        for name in files:
            source_file = os.path.join(base, name)
            target_file = os.path.join(target_dir, name)
            try:
                size = os.path.getsize(source_file)
                if os.path.isfile(target_file) and os.path.getsize(target_file) == size:
                    skipped += 1
                    continue
                if size >= 100 * 1024 * 1024:
                    log(f"    复制 {name} ({human(size)}) ...")
                shutil.copy2(source_file, target_file)
                copied += 1
                copied_bytes += size
            except OSError as error:
                log(f"    [跳过] {name}: {error}")
    log(f"    完成：新复制 {copied} 个文件（{human(copied_bytes)}），"
        f"已存在跳过 {skipped} 个，用时 {time.time() - started:.0f} 秒")


def run_command(command):
    log("  > " + " ".join(str(part) for part in command))
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [str(part) for part in command],
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def import_ok(python, code, timeout=240):
    try:
        result = subprocess.run(
            [str(python), "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        return result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    except Exception as error:  # noqa: BLE001 - 探测失败时返回原因即可
        return False, str(error)


def patch_pyvenv(cfg):
    """把虚拟环境的 home/executable 指向本目录自带的 runtime Python。"""
    if not cfg.is_file():
        return False
    lines = cfg.read_text(encoding="utf-8", errors="replace").splitlines()
    seen = {"home": False, "executable": False}
    output = []
    for line in lines:
        key = line.split("=", 1)[0].strip().lower()
        if key == "home":
            output.append(f"home = {RUNTIME.parent}")
            seen["home"] = True
        elif key == "executable":
            output.append(f"executable = {RUNTIME}")
            seen["executable"] = True
        else:
            output.append(line)
    if not seen["home"]:
        output.insert(0, f"home = {RUNTIME.parent}")
    if not seen["executable"]:
        output.insert(1, f"executable = {RUNTIME}")
    cfg.write_text("\n".join(output) + "\n", encoding="utf-8")
    return True


def copy_old_venv():
    old_venv = OLD_ROOT / ".venv"
    if not (old_venv / "Scripts" / "python.exe").is_file():
        log(f"  旧 .venv 不存在：{old_venv}")
        return False
    if VENV.exists():
        broken = VENV.with_name(f".venv.broken_{time.strftime('%Y%m%d_%H%M%S')}")
        try:
            VENV.rename(broken)
            log(f"  原 .venv 已改名为 {broken.name}")
        except OSError as error:
            log(f"  无法改名原 .venv：{error}")
            return False
    log(f"  复制旧 .venv：{old_venv}")
    copy_tree(old_venv, VENV)
    patch_pyvenv(VENV / "pyvenv.cfg")
    ok, detail = import_ok(VENV_PY, MAIN_IMPORTS)
    if ok:
        log("  旧 .venv 已迁移并通过校验")
    else:
        log("  旧 .venv 校验仍失败：\n" + detail[-2000:])
    return ok


def restore_main_env():
    log("[1/3] 主运行环境 .venv")
    if not RUNTIME.is_file():
        log(f"  找不到便携 Python：{RUNTIME}")
        return False
    if not VENV_PY.is_file():
        run_command([RUNTIME, "-m", "venv", VENV])

    ok, _detail = import_ok(VENV_PY, "import pip")
    if not ok:
        log("  .venv 里没有 pip，正在用便携 Python 补装 ensurepip ...")
        run_command([VENV_PY, "-m", "ensurepip", "--upgrade"])
        ok, _detail = import_ok(VENV_PY, "import pip")

    if ok:
        log("  离线安装 requirements（使用包内 wheels，不需要联网）...")
        run_command([
            VENV_PY, "-m", "pip", "install", "--no-input",
            "--disable-pip-version-check", "--no-index", "--only-binary=:all:",
            "--find-links", WHEELS, "-r", REQUIREMENTS,
        ])
        ok, detail = import_ok(VENV_PY, MAIN_IMPORTS)

    if ok:
        log("  .venv 依赖校验通过")
        return True

    log("  修复 .venv 失败，改用桌面旧安装里的 .venv。")
    return copy_old_venv()


def restore_models():
    log("[2/3] 模型权重 data\\models")
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for name in MODEL_NAMES:
        target = MODELS_DIR / name
        candidates = (SPARE_MODELS / name, OLD_ROOT / "data" / "models" / name)
        source = next((path for path in candidates if path.is_dir()), None)
        if source is None:
            log(f"  [缺失] {name}：桌面没有找到源目录")
            continue
        log(f"  {name}  <-  {source}")
        transfer_tree(source, target)

    weights = [
        path for path in MODELS_DIR.rglob("*")
        if path.is_file() and path.name in ("model.bin", "model.safetensors", "model.pth.tar")
    ]
    for weight in weights:
        log(f"  已就位：{weight.relative_to(ROOT)} ({human(weight.stat().st_size)})")
    return bool(weights)


def restore_engines():
    log("[3/3] Qwen 引擎环境 data\\engines")
    ENGINES_DIR.mkdir(parents=True, exist_ok=True)
    ok = True
    for engine in ENGINE_NAMES:
        target = ENGINES_DIR / engine
        source = OLD_ROOT / "data" / "engines" / engine
        if not (source / "Scripts" / "python.exe").is_file():
            log(f"  [缺失] {engine}：{source} 不是完整的引擎环境")
            ok = False
            continue
        log(f"  {engine}  <-  {source}（约 1.7 GB，同盘会直接移动）")
        transfer_tree(source, target)
        patch_pyvenv(target / "pyvenv.cfg")
        passed, detail = import_ok(target / "Scripts" / "python.exe", QWEN_IMPORTS, timeout=300)
        if passed:
            log("  qwen 引擎环境校验通过")
        else:
            ok = False
            log("  qwen 引擎环境校验失败（启动后可在界面点“配置环境”重装）：\n" + detail[-2000:])
    return ok


def main():
    global _log
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _log = LOG_PATH.open("w", encoding="utf-8")
    started = time.time()

    log("字幕工作室 · 从桌面旧安装恢复环境和模型")
    log(f"目标目录：{ROOT}")
    log(f"旧安装目录：{OLD_ROOT}")
    log("")

    results = {}
    for name, action in (("main", restore_main_env), ("models", restore_models), ("engines", restore_engines)):
        try:
            results[name] = action()
        except Exception as error:  # noqa: BLE001 - 分项失败不影响其他步骤
            log(f"  [错误] {error}")
            results[name] = False
        log("")

    log("结果：")
    log(f"  主运行环境 .venv：{'OK' if results.get('main') else '失败'}")
    log(f"  模型权重：{'OK' if results.get('models') else '失败'}")
    log(f"  Qwen 引擎环境：{'OK' if results.get('engines') else '失败'}")
    log(f"  用时：{time.time() - started:.0f} 秒")
    if all(results.values()):
        log("全部完成。双击 run.bat 即可启动。")
        return 0
    log("有项目未恢复成功，请把本窗口和 data\\logs\\restore.log 发给助手。")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        if _log is not None:
            _log.close()
