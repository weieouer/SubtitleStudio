"""独立进程文件选择器；Windows 使用系统 API，不依赖可选的 Tcl/Tk。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import sys

FILTERS = {
    "media": ("视频/音频", "*.mp4;*.mkv;*.mov;*.avi;*.flv;*.ts;*.m4v;*.webm;*.wmv;*.mp3;*.wav;*.m4a;*.aac;*.flac;*.ogg;*.opus;*.wma"),
    "script": ("剧本", "*.txt;*.md;*.docx"),
    "srt": ("字幕", "*.srt"),
}

BFFM_INITIALIZED = 1
BFFM_SETSELECTIONW = 0x0400 + 103
BrowseCallback = ctypes.WINFUNCTYPE(ctypes.c_int, w.HWND, w.UINT, w.LPARAM, w.LPARAM)
OpenFileHook = ctypes.WINFUNCTYPE(ctypes.c_size_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
WM_INITDIALOG = 0x0110
WM_NOTIFY = 0x004E
CDN_INITDONE = (-601) & 0xFFFFFFFF


class NotificationHeader(ctypes.Structure):
    _fields_ = [('hwndFrom',w.HWND), ('idFrom',ctypes.c_size_t), ('code',w.UINT)]


def foreground_dialog(user, hwnd):
    """只在初始化时提升真实选择框，隐藏owner的顶置属性不能代替它。"""
    user.SetWindowPos.argtypes = [w.HWND,w.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,w.UINT]
    user.SetWindowPos.restype = w.BOOL
    user.SetForegroundWindow.argtypes = [w.HWND]
    user.SetForegroundWindow.restype = w.BOOL
    # HWND_TOPMOST + 不移动/不缩放/显示；窗口销毁后顶置状态随之释放。
    user.SetWindowPos(hwnd,-1,0,0,0,0,0x0001 | 0x0002 | 0x0040)
    # Windows可以拒绝焦点请求；Z序已单独提升，不依赖强抢输入焦点。
    user.SetForegroundWindow(hwnd)


def start_directory(body: dict) -> str:
    initial = body.get("initial_dir")
    return initial if isinstance(initial, str) and os.path.isdir(initial) else ""


def selected_paths(raw: str) -> list[str]:
    parts = raw.split("\0\0", 1)[0].split("\0")
    if not parts[0]:
        return []
    if len(parts) == 1:
        return parts
    return [str(Path(parts[0]) / name) for name in parts[1:]]


class OpenFileName(ctypes.Structure):
    _fields_ = [
        ("lStructSize", w.DWORD), ("hwndOwner", w.HWND), ("hInstance", w.HINSTANCE),
        ("lpstrFilter", w.LPCWSTR), ("lpstrCustomFilter", w.LPWSTR),
        ("nMaxCustFilter", w.DWORD), ("nFilterIndex", w.DWORD),
        ("lpstrFile", ctypes.POINTER(w.WCHAR)), ("nMaxFile", w.DWORD),
        ("lpstrFileTitle", w.LPWSTR), ("nMaxFileTitle", w.DWORD),
        ("lpstrInitialDir", w.LPCWSTR), ("lpstrTitle", w.LPCWSTR), ("Flags", w.DWORD),
        ("nFileOffset", w.WORD), ("nFileExtension", w.WORD), ("lpstrDefExt", w.LPCWSTR),
        ("lCustData", ctypes.c_ssize_t), ("lpfnHook", ctypes.c_void_p),
        ("lpTemplateName", w.LPCWSTR), ("pvReserved", ctypes.c_void_p),
        ("dwReserved", w.DWORD), ("FlagsEx", w.DWORD),
    ]


class BrowseInfo(ctypes.Structure):
    _fields_ = [("hwndOwner", w.HWND), ("pidlRoot", ctypes.c_void_p),
                ("pszDisplayName", w.LPWSTR), ("lpszTitle", w.LPCWSTR),
                ("ulFlags", w.UINT), ("lpfn", ctypes.c_void_p),
                ("lParam", ctypes.c_ssize_t), ("iImage", ctypes.c_int)]


def windows_dialog(body: dict) -> list[str]:
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p]
    user.CreateWindowExW.restype = w.HWND
    user.DestroyWindow.argtypes = [w.HWND]
    user.GetParent.argtypes = [w.HWND]
    user.GetParent.restype = w.HWND
    # 顶置隐藏 owner 让选择框显示在应用前方；不创建控制台窗口。
    owner = user.CreateWindowExW(0x8 | 0x80, "STATIC", "Subtitle Studio",
                                 0, 0, 0, 0, 0, None, None, None, None)
    if not owner:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        kind = body.get("kind", "media")
        title = str(body.get("title") or "选择文件")
        initial = start_directory(body)
        if kind == "dir":
            return windows_directory(owner, title, initial, user=user)
        label, pattern = FILTERS[kind]
        buffer = ctypes.create_unicode_buffer(65536)
        options = OpenFileName()
        options.lStructSize = ctypes.sizeof(options)
        options.hwndOwner = owner
        options.lpstrFilter = label + "\0" + pattern + "\0所有文件\0*.*\0\0"
        options.nFilterIndex = 1
        options.lpstrFile = buffer
        options.nMaxFile = len(buffer)
        options.lpstrTitle = title
        if initial:
            options.lpstrInitialDir = initial
        options.Flags = 0x80000 | 0x1000 | 0x800 | 0x8 | 0x20  # Explorer / file/path exist / no chdir / hook
        @OpenFileHook
        def show_on_top(hwnd, message, wparam, lparam):
            initialized = message == WM_INITDIALOG
            if message == WM_NOTIFY and lparam:
                initialized = ctypes.cast(lparam,ctypes.POINTER(NotificationHeader)).contents.code == CDN_INITDONE
            if initialized:
                # Explorer式hook属于子对话框；提升它的父窗口才是完整文件选择框。
                foreground_dialog(user,user.GetParent(hwnd) or hwnd)
            return 0
        options.lpfnHook = ctypes.cast(show_on_top,ctypes.c_void_p).value
        if body.get("multiple", False):
            options.Flags |= 0x200
        common = ctypes.WinDLL("comdlg32")
        common.GetOpenFileNameW.argtypes = [ctypes.POINTER(OpenFileName)]
        common.GetOpenFileNameW.restype = w.BOOL
        common.CommDlgExtendedError.restype = w.DWORD
        if not common.GetOpenFileNameW(ctypes.byref(options)):
            error = common.CommDlgExtendedError()
            if error:
                raise RuntimeError(f"Windows 文件选择框错误 0x{error:04x}")
            return []
        return selected_paths(buffer[:])
    finally:
        user.DestroyWindow(owner)


def windows_directory(owner, title: str, initial: str = "", *, user=None) -> list[str]:
    shell = ctypes.WinDLL("shell32")
    ole = ctypes.OleDLL("ole32")
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, w.DWORD]
    ole.CoUninitialize.argtypes = []
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    shell.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BrowseInfo)]
    shell.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell.SHGetPathFromIDListEx.argtypes = [ctypes.c_void_p, w.LPWSTR, w.DWORD, w.DWORD]
    shell.SHGetPathFromIDListEx.restype = w.BOOL
    ole.CoInitializeEx(None, 2)
    try:
        display = ctypes.create_unicode_buffer(260)
        info = BrowseInfo(hwndOwner=owner, pszDisplayName=ctypes.cast(display, w.LPWSTR),
                          lpszTitle=title, ulFlags=0x51)
        if user is None:
            user = ctypes.WinDLL("user32", use_last_error=True)
        user.SendMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        user.SendMessageW.restype = ctypes.c_ssize_t
        initial_buffer = ctypes.create_unicode_buffer(initial) if initial else None

        @BrowseCallback
        def select_initial(hwnd, message, lparam, data):
            if message == BFFM_INITIALIZED:
                if initial_buffer is not None:
                    user.SendMessageW(hwnd, BFFM_SETSELECTIONW, 1,
                                      ctypes.cast(initial_buffer, ctypes.c_void_p).value)
                foreground_dialog(user,hwnd)
            return 0
        info.lpfn = ctypes.cast(select_initial, ctypes.c_void_p)
        pidl = shell.SHBrowseForFolderW(ctypes.byref(info))
        if not pidl:
            return []
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            if not shell.SHGetPathFromIDListEx(pidl, buffer, len(buffer), 0):
                raise RuntimeError("无法读取所选文件夹路径")
            return [buffer.value]
        finally:
            ole.CoTaskMemFree(pidl)
    finally:
        ole.CoUninitialize()


def open_dialog(body: dict) -> list[str]:
    if os.name == "nt":
        return windows_dialog(body)
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        kind = body.get("kind", "media")
        options = {"title": str(body.get("title") or "选择文件"), "parent": root}
        initial = start_directory(body)
        if initial:
            options["initialdir"] = initial
        if kind == "dir":
            selected = filedialog.askdirectory(**options)
            return [selected] if selected else []
        elif body.get("multiple", False):
            label, pattern = FILTERS[kind]
            return list(filedialog.askopenfilenames(**options, filetypes=[(label, pattern.replace(';', ' ')), ("所有文件", "*.*")]) or [])
        else:
            label, pattern = FILTERS[kind]
            selected = filedialog.askopenfilename(**options, filetypes=[(label, pattern.replace(';', ' ')), ("所有文件", "*.*")])
            return [selected] if selected else []
    finally:
        root.destroy()


def main():
    paths = open_dialog(json.loads(sys.stdin.read()))
    print(json.dumps({"ok": True, "paths": paths, "path": paths[0] if paths else ""}))


if __name__ == "__main__":
    main()
