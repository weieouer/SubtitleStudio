# -*- coding: utf-8 -*-
"""启动本地服务并自动打开浏览器。

以后打包成桌面软件时，这个文件可以作为入口。
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

HOST = "127.0.0.1"
START_PORT = 8765


def find_port() -> int:
    for port in range(START_PORT, START_PORT + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((HOST, port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到可用端口，请关闭占用 8765 的程序后重试。")


def find_app_browser() -> str | None:
    """优先用 Edge/Chrome 的 --app 模式，打开无地址栏的独立窗口。"""
    if sys.platform != "win32":
        return None
    browsers = (
        ("Microsoft/Edge/Application/msedge.exe", "msedge"),
        ("Google/Chrome/Application/chrome.exe", "chrome"),
    )
    roots = (
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("PROGRAMFILES"),
        os.environ.get("LOCALAPPDATA"),
    )
    for root in roots:
        if not root:
            continue
        for relative, _ in browsers:
            exe = Path(root) / relative
            if exe.exists():
                return str(exe)
    for _, name in browsers:
        found = shutil.which(name)
        if found:
            return found
    return None


def open_when_ready(port: int) -> None:
    url = f"http://{HOST}:{port}"
    for _ in range(80):
        try:
            with socket.create_connection((HOST, port), timeout=0.3):
                exe = find_app_browser()
                if exe:
                    try:
                        subprocess.Popen(
                            [
                                exe,
                                f"--app={url}",
                                "--window-size=1440,920",
                                "--no-first-run",
                                "--no-default-browser-check",
                            ]
                        )
                        return
                    except OSError:
                        pass
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.25)


def main() -> None:
    port = find_port()
    url = f"http://{HOST}:{port}"
    print("=" * 56)
    print("  SubtitleStudio · 字幕工作室")
    print(f"  已启动：{url}")
    print("  关闭这个窗口即可停止服务")
    print("=" * 56)
    if os.environ.get("SUBTITLESTUDIO_NO_BROWSER") != "1":
        threading.Thread(target=open_when_ready, args=(port,), daemon=True).start()
    uvicorn.run(app, host=HOST, port=port, log_level="info")


if __name__ == "__main__":
    main()
