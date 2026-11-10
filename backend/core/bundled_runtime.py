"""安装版预装运行库入口；源码开发仍使用项目及各引擎虚拟环境。"""
from __future__ import annotations

import ctypes
import json
import os

from .paths import ROOT_DIR

_DLL_HANDLES = []
_DLL_PATHS = set()


def manifest():
    try:
        value = json.loads((ROOT_DIR / 'runtime/manifest.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) and value.get('format') == 1 and value.get('ready') is True else {}


def stale_venv_home(location):
    try:
        rows = (location / 'pyvenv.cfg').read_text(encoding='utf-8').splitlines()
    except OSError:
        return False
    for row in rows:
        if row.lower().startswith('home') and '=' in row:
            from pathlib import Path
            home = Path(row.split('=', 1)[1].strip())
            return not home.is_dir()
    return False


def python_for(engine):
    info = manifest()
    if engine not in info.get('engines', []):
        return None
    gpu = gpu_python()
    if gpu:
        return gpu
    path = (ROOT_DIR / 'runtime' / str(info.get('ml', ''))).resolve()
    return path if path.is_relative_to((ROOT_DIR / 'runtime').resolve()) and path.is_file() else None


def gpu_python():
    location = ROOT_DIR / 'data/engines/gpu'
    try:
        value = json.loads((location / 'ready.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    python = location / 'python.exe'
    return python if value.get('torch') == 'cuda' and python.is_file() else None


def propainter_source():
    source = ROOT_DIR / 'runtime/vendor/propainter'
    return source if python_for('removal') and (source / 'inference_propainter.py').is_file() else None


def cuda_available():
    if manifest().get('torch_flavor') != 'cuda' and not gpu_python():
        return False
    try:
        driver = ctypes.WinDLL('nvcuda.dll') if os.name == 'nt' else ctypes.CDLL('libcuda.so.1')
        count, version = ctypes.c_int(), ctypes.c_int()
        return (driver.cuInit(0) == 0 and driver.cuDeviceGetCount(ctypes.byref(count)) == 0
                and count.value > 0 and driver.cuDriverGetVersion(ctypes.byref(version)) == 0 and version.value >= 12060)
    except (OSError, AttributeError):
        return False


def configure_cuda_libraries():
    if os.name != 'nt':
        return
    python = python_for('removal')
    if python is None:
        return
    directory = python.parent / 'Lib/site-packages/torch/lib'
    if directory.is_dir() and str(directory) not in _DLL_PATHS:
        _DLL_HANDLES.append(os.add_dll_directory(str(directory)))
        _DLL_PATHS.add(str(directory))
        os.environ['PATH'] = str(directory) + os.pathsep + os.environ.get('PATH', '')
