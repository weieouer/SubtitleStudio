"""独立进程文件选择器；Windows 使用系统 API，不依赖可选的 Tcl/Tk。"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes as w
import json
import os
import sys
from uuid import UUID

FILTERS = {
    "media": ("视频/音频", "*.mp4;*.mkv;*.mov;*.avi;*.flv;*.ts;*.m4v;*.webm;*.wmv;*.mp3;*.wav;*.m4a;*.aac;*.flac;*.ogg;*.opus;*.wma"),
    "script": ("剧本", "*.txt;*.md;*.docx"),
    "srt": ("字幕", "*.srt"),
}

CLSID_FILE_OPEN_DIALOG = "dc1c5a9c-e88a-4dde-a5a1-60f82a20aef7"
IID_FILE_OPEN_DIALOG = "d57c7288-d4ad-4768-be02-9d969532d960"
IID_SHELL_ITEM = "43826d1e-e718-42ee-bc55-a1e261c37bfe"
DIALOG_STATE_GUID = "c088597b-ccf8-40f5-a75f-a936ee403425"
FILE_DIALOG_CANCELLED = 0x800704C7
FOS_PICKFOLDERS = 0x20
FOS_FORCEFILESYSTEM = 0x40
FOS_ALLOWMULTISELECT = 0x200
SIGDN_FILESYSPATH = 0x80058000


class Guid(ctypes.Structure):
    _fields_ = [("data1", w.DWORD), ("data2", w.WORD), ("data3", w.WORD), ("data4", w.BYTE * 8)]


class FilterSpec(ctypes.Structure):
    _fields_ = [("name", w.LPCWSTR), ("pattern", w.LPCWSTR)]


class MonitorInfo(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("monitor", w.RECT), ("work", w.RECT), ("flags", w.DWORD)]


def start_directory(body: dict) -> str:
    initial = body.get("initial_dir")
    return initial if isinstance(initial, str) and os.path.isdir(initial) else ""


def native(dll, name, result, *arguments):
    function = getattr(dll, name)
    function.argtypes = arguments
    function.restype = result
    return function


def open_in_explorer(path):
    """通过 Shell 定位和激活现有文件夹窗口，中文路径直接使用 Unicode。"""
    ole = ctypes.WinDLL("ole32")
    shell = ctypes.WinDLL("shell32")
    initialized = native(ole,"CoInitializeEx",w.LONG,ctypes.c_void_p,w.DWORD)(None,2)
    if initialized not in (0,1,-2147417850):  # RPC_E_CHANGED_MODE 表示调用线程已初始化。
        check_hresult(initialized)
    pidl = ctypes.c_void_p()
    try:
        parse = native(shell,"SHParseDisplayName",w.LONG,w.LPCWSTR,ctypes.c_void_p,
                       ctypes.POINTER(ctypes.c_void_p),w.DWORD,ctypes.c_void_p)
        check_hresult(parse(str(path),None,ctypes.byref(pidl),0,None))
        items = None
        count = 0
        if path.is_dir():
            # 空相对 PIDL 表示当前文件夹。零项会改为打开父目录并选中该文件夹。
            empty = w.USHORT(0)
            items = (ctypes.c_void_p*1)(ctypes.addressof(empty))
            count = 1
        activate = native(shell,"SHOpenFolderAndSelectItems",w.LONG,ctypes.c_void_p,
                          w.UINT,ctypes.POINTER(ctypes.c_void_p),w.DWORD)
        check_hresult(activate(pidl,count,items,0))
        _activate_explorer_folder(path if path.is_dir() else path.parent)
    finally:
        if pidl:
            native(ole,"CoTaskMemFree",None,ctypes.c_void_p)(pidl)
        if initialized in (0,1):
            native(ole,"CoUninitialize",None)()


def _activate_explorer_folder(folder):
    """按完整路径查找 Shell 窗口，恢复最小化窗口并转移本次点击的焦点。"""
    import base64
    import subprocess
    target = base64.b64encode(str(folder).encode('utf-8')).decode('ascii')
    script = """
$target = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('TARGET'))
$shell = New-Object -ComObject Shell.Application
for ($attempt = 0; $attempt -lt 10; $attempt++) {
    foreach ($window in $shell.Windows()) {
        try {
            if ($window.Document.Folder.Self.Path -eq $target) {
                [Console]::Write($window.HWND)
                exit
            }
        } catch {}
    }
    Start-Sleep -Milliseconds 100
}
""".replace('TARGET',target)
    try:
        result = subprocess.run(
            ['powershell.exe','-NoProfile','-NonInteractive','-WindowStyle','Hidden','-EncodedCommand',
             base64.b64encode(script.encode('utf-16-le')).decode('ascii')],
            capture_output=True,timeout=8,creationflags=subprocess.CREATE_NO_WINDOW)
        handle = int(result.stdout.strip())
    except (OSError,ValueError,subprocess.TimeoutExpired):
        return
    user = ctypes.WinDLL("user32")
    kernel = ctypes.WinDLL("kernel32")
    foreground = native(user,"GetForegroundWindow",w.HWND)()
    foreground_thread = native(user,"GetWindowThreadProcessId",w.DWORD,w.HWND,ctypes.c_void_p)(foreground,None)
    current_thread = native(kernel,"GetCurrentThreadId",w.DWORD)()
    attach = native(user,"AttachThreadInput",w.BOOL,w.DWORD,w.DWORD,w.BOOL)
    attached = foreground_thread!=current_thread and attach(current_thread,foreground_thread,True)
    try:
        if native(user,"IsIconic",w.BOOL,w.HWND)(handle):
            native(user,"ShowWindow",w.BOOL,w.HWND,ctypes.c_int)(handle,9)
        native(user,"SetForegroundWindow",w.BOOL,w.HWND)(handle)
    finally:
        if attached:
            attach(current_thread,foreground_thread,False)


def guid(value):
    return ctypes.byref(Guid.from_buffer_copy(UUID(value).bytes_le))


def check_hresult(result):
    if result < 0:
        raise OSError(f"Windows 文件选择框错误 0x{result & 0xFFFFFFFF:08x}")


def com_method(pointer, slot, *arguments, result=w.LONG):
    # COM 的前三个槽位是 IUnknown，其余依照各接口在 Windows SDK 中的声明顺序。
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *arguments)(vtable[slot])


def release(pointer):
    com_method(pointer, 2, result=w.ULONG)(pointer)


@contextmanager
def com_output(call, *arguments):
    pointer = ctypes.c_void_p()
    check_hresult(call(*arguments, ctypes.byref(pointer)))
    try:
        yield pointer
    finally:
        release(pointer)


def dialog_bounds(window, work, dpi):
    work_width = work.right - work.left
    work_height = work.bottom - work.top
    width = window.right - window.left
    height = window.bottom - window.top
    # 尺寸以 96 DPI 为基准。小屏幕预留任务栏之外的边距。
    target_width = min(work_width, max(width, min(1000 * dpi // 96, work_width * 9 // 10)))
    target_height = min(work_height, max(height, min(660 * dpi // 96, work_height * 9 // 10)))
    if (target_width, target_height) != (width, height):
        left = work.left + (work_width - target_width) // 2
        top = work.top + (work_height - target_height) // 2
    else:
        left = max(work.left, min(window.left, work.right - target_width))
        top = max(work.top, min(window.top, work.bottom - target_height))
    return left, top, target_width, target_height


def foreground_dialog(user, hwnd):
    set_position = native(user, "SetWindowPos", w.BOOL, w.HWND, w.HWND, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, w.UINT)
    monitor = native(user, "MonitorFromWindow", w.HMONITOR, w.HWND, w.DWORD)(hwnd, 2)
    info = MonitorInfo(size=ctypes.sizeof(MonitorInfo))
    rect = w.RECT()
    get_monitor = native(user, "GetMonitorInfoW", w.BOOL, w.HMONITOR, ctypes.POINTER(MonitorInfo))
    get_rect = native(user, "GetWindowRect", w.BOOL, w.HWND, ctypes.POINTER(w.RECT))
    if monitor and get_monitor(monitor, ctypes.byref(info)) and get_rect(hwnd, ctypes.byref(rect)):
        dpi = native(user, "GetDpiForWindow", w.UINT, w.HWND)(hwnd) if hasattr(user, "GetDpiForWindow") else 96
        set_position(hwnd, -1, *dialog_bounds(rect, info.work, dpi or 96), 0x10)  # HWND_TOPMOST, SWP_NOACTIVATE
    else:
        set_position(hwnd, -1, 0, 0, 0, 0, 0x13)  # 仅调整 Z 序


def show_dialog(dialog, user, owner):
    """跟随当前线程的第一次对话框激活，在 Windows 接管输入前调整窗口。"""
    callback_type = ctypes.WINFUNCTYPE(w.LPARAM, ctypes.c_int, w.WPARAM, w.LPARAM)
    set_hook = native(user, "SetWindowsHookExW", w.HHOOK, ctypes.c_int, callback_type, w.HINSTANCE, w.DWORD)
    unhook = native(user, "UnhookWindowsHookEx", w.BOOL, w.HHOOK)
    next_hook = native(user, "CallNextHookEx", w.LPARAM, w.HHOOK, ctypes.c_int, w.WPARAM, w.LPARAM)
    get_owner = native(user, "GetWindow", w.HWND, w.HWND, w.UINT)
    thread_id = native(ctypes.WinDLL("kernel32"), "GetCurrentThreadId", w.DWORD)()
    adjusted = False

    @callback_type
    def on_activate(code, hwnd, data):
        nonlocal adjusted
        if code == 5 and not adjusted and get_owner(hwnd, 4) == owner:  # HCBT_ACTIVATE, GW_OWNER
            adjusted = True
            foreground_dialog(user, hwnd)
        return next_hook(None, code, hwnd, data)

    hook = set_hook(5, on_activate, None, thread_id)  # WH_CBT
    if not hook:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return com_method(dialog, 3, w.HWND)(dialog, owner)  # IModalWindow::Show
    finally:
        unhook(hook)


def configure_dialog(dialog, body, shell):
    check_hresult(com_method(dialog, 24, ctypes.c_void_p)(dialog, guid(DIALOG_STATE_GUID)))  # SetClientGuid
    kind = body.get("kind", "media")
    options = w.DWORD()
    check_hresult(com_method(dialog, 10, ctypes.POINTER(w.DWORD))(dialog, ctypes.byref(options)))  # GetOptions
    options.value |= FOS_FORCEFILESYSTEM
    if kind == "dir":
        options.value |= FOS_PICKFOLDERS
    else:
        if body.get("multiple"):
            options.value |= FOS_ALLOWMULTISELECT
        filters = (FilterSpec * 2)(FilterSpec(*FILTERS[kind]), FilterSpec("所有文件", "*.*"))
        check_hresult(com_method(dialog, 4, w.UINT, ctypes.POINTER(FilterSpec))(dialog, len(filters), filters))  # SetFileTypes
        check_hresult(com_method(dialog, 5, w.UINT)(dialog, 1))  # SetFileTypeIndex
    check_hresult(com_method(dialog, 9, w.DWORD)(dialog, options.value))  # SetOptions
    title = str(body.get("title") or ("选择文件夹" if kind == "dir" else "选择文件"))
    check_hresult(com_method(dialog, 17, w.LPCWSTR)(dialog, title))  # SetTitle

    initial = start_directory(body)
    if initial:
        shell_item = ctypes.c_void_p()
        create_item = native(shell, "SHCreateItemFromParsingName", w.LONG, w.LPCWSTR,
                             ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        if create_item(initial, None, guid(IID_SHELL_ITEM), ctypes.byref(shell_item)) >= 0:
            try:
                check_hresult(com_method(dialog, 12, ctypes.c_void_p)(dialog, shell_item))  # SetFolder
            finally:
                release(shell_item)


def dialog_paths(dialog, free):
    pointer_out = ctypes.POINTER(ctypes.c_void_p)
    get_results = com_method(dialog, 27, pointer_out)  # IFileOpenDialog::GetResults
    paths = []
    with com_output(get_results, dialog) as items:
        count = w.DWORD()
        check_hresult(com_method(items, 7, ctypes.POINTER(w.DWORD))(items, ctypes.byref(count)))
        get_item = com_method(items, 8, w.DWORD, pointer_out)  # IShellItemArray::GetItemAt
        for index in range(count.value):
            with com_output(get_item, items, index) as item:
                value = ctypes.c_void_p()
                get_name = com_method(item, 5, w.DWORD, pointer_out)  # IShellItem::GetDisplayName
                check_hresult(get_name(item, SIGDN_FILESYSPATH, ctypes.byref(value)))
                try:
                    paths.append(ctypes.wstring_at(value))
                finally:
                    free(value)
    return paths


def windows_dialog(body: dict) -> list[str]:
    user = ctypes.WinDLL("user32", use_last_error=True)
    ole = ctypes.WinDLL("ole32")
    shell = ctypes.WinDLL("shell32")
    initialize = native(ole, "CoInitializeEx", w.LONG, ctypes.c_void_p, w.DWORD)
    uninitialize = native(ole, "CoUninitialize", None)
    create = native(ole, "CoCreateInstance", w.LONG, ctypes.c_void_p, ctypes.c_void_p, w.DWORD,
                    ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    free = native(ole, "CoTaskMemFree", None, ctypes.c_void_p)
    create_window = native(user, "CreateWindowExW", w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                           w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p)
    destroy_window = native(user, "DestroyWindow", w.BOOL, w.HWND)
    if hasattr(user, "SetProcessDpiAwarenessContext"):
        native(user, "SetProcessDpiAwarenessContext", w.BOOL, ctypes.c_void_p)(ctypes.c_void_p(-4))
    foreground = native(user, "GetForegroundWindow", w.HWND)()
    anchor = w.RECT()
    native(user, "GetWindowRect", w.BOOL, w.HWND, ctypes.POINTER(w.RECT))(foreground, ctypes.byref(anchor))
    check_hresult(initialize(None, 2))  # COINIT_APARTMENTTHREADED
    try:
        owner = create_window(0x80, "STATIC", "Subtitle Studio", 0, anchor.left, anchor.top, 0, 0,
                              None, None, None, None)
        if not owner:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            with com_output(create, guid(CLSID_FILE_OPEN_DIALOG), None, 1, guid(IID_FILE_OPEN_DIALOG)) as dialog:
                configure_dialog(dialog, body, shell)
                result = show_dialog(dialog, user, owner)
                if result & 0xFFFFFFFF == FILE_DIALOG_CANCELLED:
                    return []
                check_hresult(result)
                return dialog_paths(dialog, free)
        finally:
            destroy_window(owner)
    finally:
        uninitialize()


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
