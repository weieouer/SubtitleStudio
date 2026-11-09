"""为本工具的Windows app窗口设置胶片图标及独立任务栏身份。"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

_icon_handles = []  # 窗口使用期间保留HICON，随后端进程退出释放。


def relaunch_command(launcher: Path) -> str:
    if launcher.suffix.lower() == '.py':
        python = Path(sys.executable)
        windowless = python.with_name('pythonw.exe')
        if os.name == 'nt' and windowless.is_file():
            python = windowless
        return subprocess.list2cmdline([str(python), str(launcher), '--background'])
    return subprocess.list2cmdline([os.environ.get('COMSPEC', 'cmd.exe'), '/c', str(launcher)])


def window_icon_sizes(hwnd: int) -> tuple[int, int]:
    """窗口所在屏幕的标题栏/任务栏尺寸，避免固定 16/48 像素被二次缩放。"""
    import ctypes as c
    from ctypes import wintypes as w
    user = c.windll.user32
    try:
        user.GetDpiForWindow.argtypes = [w.HWND]
        user.GetDpiForWindow.restype = w.UINT
        dpi = user.GetDpiForWindow(hwnd) or 96
        user.GetSystemMetricsForDpi.argtypes = [c.c_int, w.UINT]
        user.GetSystemMetricsForDpi.restype = c.c_int
        return (user.GetSystemMetricsForDpi(49, dpi) or round(16 * dpi / 96),
                user.GetSystemMetricsForDpi(11, dpi) or round(32 * dpi / 96))
    except AttributeError:
        return (user.GetSystemMetrics(49) or 16, user.GetSystemMetrics(11) or 32)


def set_taskbar_identity(hwnd: int, icon_path: Path, launcher: Path) -> bool:
    if os.name != "nt":
        return False
    import ctypes as c
    from ctypes import wintypes as w

    class GUID(c.Structure):
        _fields_ = [("data", c.c_ubyte * 16)]

    def guid(value):
        return GUID((c.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le))

    class PROPERTYKEY(c.Structure):
        _fields_ = [("fmtid", GUID), ("pid", w.DWORD)]

    class DATA(c.Union):
        _fields_ = [("pointer", c.c_void_p), ("padding", c.c_size_t * 2)]

    class PROPVARIANT(c.Structure):
        _fields_ = [("vt", c.c_ushort), ("reserved", c.c_ushort * 3), ("value", DATA)]

    ole, shell = c.windll.ole32, c.windll.shell32
    initialized = ole.CoInitialize(None) >= 0
    store = c.c_void_p()
    try:
        shell.SHGetPropertyStoreForWindow.argtypes = [w.HWND, c.POINTER(GUID), c.POINTER(c.c_void_p)]
        shell.SHGetPropertyStoreForWindow.restype = c.c_long
        iid = guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")
        if shell.SHGetPropertyStoreForWindow(hwnd, c.byref(iid), c.byref(store)) < 0:
            return False
        table = c.cast(store, c.POINTER(c.POINTER(c.c_void_p))).contents
        setter = c.WINFUNCTYPE(c.c_long, c.c_void_p, c.POINTER(PROPERTYKEY), c.POINTER(PROPVARIANT))(table[6])
        release = c.WINFUNCTYPE(c.c_ulong, c.c_void_p)(table[2])
        try:
            values = [(2, relaunch_command(launcher)),
                      (3, str(icon_path.resolve()) + ",0"), (4, "字幕工作室"), (5, "SubtitleStudio.Local.Workbench")]
            for property_id, text in values:
                key = PROPERTYKEY(guid("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3"), property_id)
                value = PROPVARIANT()
                value.vt = 31  # VT_LPWSTR，SetValue复制字符串。
                buffer = c.create_unicode_buffer(text)
                value.value.pointer = c.cast(buffer, c.c_void_p).value
                if setter(store, c.byref(key), c.byref(value)) < 0:
                    return False
            return True
        finally:
            release(store)
    finally:
        if initialized:
            ole.CoUninitialize()


def set_window_icon(hwnd: int, icon_path: Path, launcher: Path) -> dict:
    if os.name != "nt":
        return {"window_icon": False, "taskbar": False}
    import ctypes as c
    from ctypes import wintypes as w
    user = c.windll.user32
    user.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT, c.c_int, c.c_int, w.UINT]
    user.LoadImageW.restype = w.HANDLE
    user.SendMessageTimeoutW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT, c.POINTER(c.c_size_t)]
    user.SendMessageTimeoutW.restype = c.c_ssize_t
    applied = True
    small, large = window_icon_sizes(hwnd)
    for kind, size in ((0, small), (1, large)):
        handle = user.LoadImageW(None, str(icon_path.resolve()), 1, size, size, 0x10)
        if not handle:
            applied = False
            continue
        _icon_handles.append(handle)
        result = c.c_size_t()
        if not user.SendMessageTimeoutW(hwnd, 0x80, kind, handle, 2, 250, c.byref(result)):
            applied = False
    return {"window_icon": applied, "taskbar": set_taskbar_identity(hwnd, icon_path, launcher)}


def apply_browser_icon(icon_path: Path, launcher: Path) -> None:
    """只处理标题恰为字幕工作室的Chrome app窗口，不修改日常浏览器窗口。"""
    if os.name != "nt":
        return
    import ctypes as c
    from ctypes import wintypes as w
    user = c.windll.user32
    callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
    user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
    user.IsWindowVisible.argtypes = [w.HWND]
    seen = set()

    @callback_type
    def visit(hwnd, _):
        title, class_name = c.create_unicode_buffer(256), c.create_unicode_buffer(128)
        user.GetWindowTextW(hwnd, title, 256)
        user.GetClassNameW(hwnd, class_name, 128)
        if (hwnd not in seen and user.IsWindowVisible(hwnd) and title.value == "字幕工作室"
                and class_name.value.startswith("Chrome_WidgetWin")):
            try:
                result = set_window_icon(hwnd, icon_path, launcher)
                if result["window_icon"] and result["taskbar"]:
                    seen.add(hwnd)
            except (OSError, ValueError, AttributeError):
                pass
        return True

    for _ in range(30):
        user.EnumWindows(visit, 0)
        if seen:
            return
        time.sleep(.5)
