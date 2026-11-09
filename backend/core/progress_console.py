"""下载 / 配置环境时弹出的可见控制台窗口。

界面里点“下载模型”或“下载 GPU 环境”后，后台用 CREATE_NEW_CONSOLE 启动本文件。
它读取 data/run/activity.json，实时显示进度、速度和 pip 安装输出；
任务完成、失败或暂停后自动退出，控制台窗口随之关闭。

本文件既被 backend 导入（使用 spawn），也可以作为脚本单独运行（进度窗口本身）。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

ROOT_DIR = Path(__file__).resolve().parents[2]

ACTIVE_STATES = ("idle", "waiting", "downloading", "preparing")
FINAL_STATES = ("done", "error", "cancelled")
LINGER = {"done": 2.0, "cancelled": 2.0, "error": 6.0}
KIND_LABELS = {"download": "模型下载", "gpu": "GPU 环境配置", "env": "运行环境配置"}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SPAWN_LOCK = threading.Lock()
_PROCESS = None


# ---------------------------------------------------------------- 后端调用

def spawn(root=None, parent_pid=None):
    """打开一个进度控制台；已有窗口仍存活时不重复打开。"""
    if os.name != "nt":
        return None
    global _PROCESS
    with _SPAWN_LOCK:
        if _PROCESS is not None and _PROCESS.poll() is None:
            return _PROCESS
        python = Path(sys.executable).with_name("python.exe")
        if not python.is_file():
            return None
        script = Path(__file__).resolve()
        folder = Path(root or ROOT_DIR)
        arguments = [str(python), str(script), "--root", str(folder)]
        if parent_pid:
            arguments += ["--pid", str(parent_pid)]
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
                       "PYTHONIOENCODING": "utf-8"}
        try:
            _PROCESS = subprocess.Popen(
                arguments, cwd=str(folder), env=environment,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        except OSError:
            _PROCESS = None
        return _PROCESS


# ---------------------------------------------------------------- 控制台

def _setup_console():
    """接管新控制台，设为 UTF-8 并启用 ANSI 重绘；返回是否支持 ANSI。"""
    if os.name != "nt":
        return sys.stdout.isatty()
    try:
        kernel32 = ctypes.windll.kernel32
        if not kernel32.GetConsoleWindow() and not kernel32.AllocConsole():
            return False
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleTitleW("字幕工作室 · 下载")
        try:
            stream = open("CONOUT$", "w", encoding="utf-8", errors="replace",
                          buffering=1)
            sys.stdout = stream
            sys.stderr = stream
        except OSError:
            pass
        import msvcrt
        handle = msvcrt.get_osfhandle(sys.stdout.fileno())
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        pass
    return False


def _set_title(text):
    if os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(text[:200])
    except Exception:
        pass


def _alive(pid):
    """父进程还在运行吗？进程退出后进度窗口也跟着关闭。"""
    if not pid:
        return True
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True


# ---------------------------------------------------------------- 读取与渲染

def _read_activity(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _human(size):
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _duration(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _bar_line(line):
    core = line.strip()
    if not core:
        return True
    marks = sum(1 for ch in core if ch in "━─-—#=<>|\\/")
    return marks >= max(6, len(core) * 0.5)


def _recent_install_log(root, limit=7):
    """最新的引擎安装日志；只在最近几分钟有写入时显示。"""
    try:
        logs = [p for p in (root / "data" / "engines").glob("*/install.log")
                if p.is_file()]
        if not logs:
            return []
        newest = max(logs, key=lambda p: p.stat().st_mtime)
        if time.time() - newest.stat().st_mtime > 180:
            return []
        with newest.open("rb") as source:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            source.seek(max(0, size - 48 * 1024))
            data = source.read()
    except OSError:
        return []
    rows = []
    for raw in data.decode("utf-8", errors="replace").replace("\r", "\n").splitlines():
        line = _ANSI.sub("", raw).strip()
        if not line or _bar_line(line):
            continue
        if rows and rows[-1] == line:
            continue
        rows.append(line)
    return rows[-limit:]


def _indeterminate(width, tick):
    block = max(4, width // 5)
    span = width + block
    start = tick % span
    cells = ["-"] * width
    for index in range(block):
        position = start - block + index
        if 0 <= position < width:
            cells[position] = "#"
    return "[" + "".join(cells) + "]"


def render(task, root, columns, tick):
    width = max(48, min(columns - 1, 110))
    raw_kind = str(task.get("kind") or "")
    lines = [f"字幕工作室 · {KIND_LABELS.get(raw_kind, '下载任务')}", "─" * width]

    state = str(task.get("status") or "waiting")
    title = str(task.get("title") or "").strip()
    message = str(task.get("message") or "").strip()
    if title:
        lines.append(title)
    if message and message != title:
        lines.append(message)
    source = str(task.get("source") or "").strip()
    if source and source not in message and raw_kind == "download" and state == "downloading":
        lines.append("下载源：" + source)
    lines.append("")

    total = float(task.get("total") or 0)
    done = float(task.get("downloaded") or 0)
    speed = float(task.get("speed_bps") or 0)
    started = float(task.get("started_at") or time.time())
    elapsed = time.time() - started
    cells = max(12, min(38, width - 30))

    if state == "done":
        lines.append("[" + "#" * cells + "] 100%")
        lines.append("完成")
    elif state == "cancelled":
        lines.append("[ 已暂停 ] 已完成的片段会保留，可在界面继续下载")
    elif state == "error":
        lines.append("[ 失败 ]")
        error = str(task.get("error") or "").strip()
        if error and error not in message:
            lines.append(error)
    elif state == "downloading" and total > 0:
        ratio = max(0.0, min(1.0, done / total))
        filled = int(round(ratio * cells))
        lines.append(f"[{'#' * filled}{'-' * (cells - filled)}] {ratio * 100:3.0f}%")
        detail = f"{_human(done)} / {_human(total)}"
        if speed > 0:
            detail += f"    {_human(speed)}/s"
        lines.append(detail)
        eta = (total - done) / speed if speed > 0 and done < total else 0
        lines.append(f"已用 {_duration(elapsed)}"
                     + (f" · 剩余约 {_duration(eta)}" if eta else ""))
    else:
        lines.append(_indeterminate(cells, tick))
        label = "安装依赖中…" if state == "preparing" else "处理中…"
        lines.append(label)
        lines.append(f"已用 {_duration(elapsed)}")

    if state == "preparing":
        rows = _recent_install_log(root)
        if rows:
            lines.append("")
            lines.append("安装输出：")
            lines.extend("  " + row for row in rows)

    return [line[:width] for line in lines]


def paint(lines, ansi):
    if ansi:
        sys.stdout.write("\x1b[H\x1b[2J" + "\n".join(lines) + "\n")
    else:
        os.system("cls")
        sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------- 主循环

def main(argv=None):
    parser = argparse.ArgumentParser(description="下载进度窗口")
    parser.add_argument("--root", default=str(ROOT_DIR))
    parser.add_argument("--pid", type=int, default=0)
    args = parser.parse_args(argv)
    root = Path(args.root)
    activity = root / "data" / "run" / "activity.json"
    ansi = _setup_console()

    failures = 0
    linger_until = 0.0
    linger_task = ""
    result = 0
    while True:
        task = _read_activity(activity)
        if task is None:
            failures += 1
            if failures == 1:
                paint(["字幕工作室 · 下载", "", "正在等待任务…"], ansi)
            if failures >= 25 or (args.pid and not _alive(args.pid)):
                return 1
            time.sleep(0.4)
            continue
        failures = 0

        columns = shutil.get_terminal_size((96, 28)).columns
        paint(render(task, root, columns, int(time.time() * 3)), ansi)

        state = str(task.get("status") or "")
        task_id = str(task.get("task") or "")
        _set_title("字幕工作室 · " + (str(task.get("title") or "下载")[:80]))

        if state in FINAL_STATES:
            if not linger_until:
                linger_until = time.monotonic() + LINGER.get(state, 2.0)
                linger_task = task_id
                result = 0 if state != "error" else 1
            if time.monotonic() >= linger_until:
                return result
            # 等待关闭期间又有新任务，就继续显示，不重复弹窗。
            time.sleep(0.35)
            newer = _read_activity(activity)
            if (newer and str(newer.get("task") or "") not in ("", linger_task)
                    and str(newer.get("status") or "") in ACTIVE_STATES):
                linger_until = 0.0
            continue

        linger_until = 0.0
        if args.pid and not _alive(args.pid):
            return 1
        time.sleep(0.4)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        try:
            folder = ROOT_DIR / "data" / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / "progress-window.log").open("a", encoding="utf-8") as log:
                log.write(time.strftime("%Y-%m-%d %H:%M:%S\n"))
                log.write(traceback.format_exc() + "\n")
        except OSError:
            pass
        raise
