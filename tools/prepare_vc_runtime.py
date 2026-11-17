"""把一套完整的 x64 VC CRT 随便携 Python 发布，避免依赖开发机系统 DLL。"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import struct

from backend.core.windows_runtime import VC_DLLS


def is_x64_dll(path):
    try:
        with path.open('rb') as source:
            if source.read(2) != b'MZ':
                return False
            source.seek(0x3c)
            offset = struct.unpack('<I', source.read(4))[0]
            source.seek(offset)
            return source.read(6) == b'PE\0\0\x64\x86'
    except (OSError, struct.error):
        return False


def crt_source():
    explicit = os.environ.get('SUBTITLESTUDIO_VC_REDIST_DIR')
    candidates = [Path(explicit)] if explicit else []
    if not explicit:
        # 优先使用 Visual Studio 可再发行目录；其次使用构建机已安装的微软运行库。
        for variable in ('ProgramFiles', 'ProgramFiles(x86)'):
            base = os.environ.get(variable)
            if base:
                candidates.extend(sorted((Path(base) / 'Microsoft Visual Studio').glob(
                    '*/VC/Redist/MSVC/*/x64/Microsoft.VC*.CRT'), reverse=True))
                candidates.extend(sorted((Path(base) / 'Microsoft Visual Studio').glob(
                    '*/*/VC/Redist/MSVC/*/x64/Microsoft.VC*.CRT'), reverse=True))
        if os.environ.get('SystemRoot'):
            candidates.append(Path(os.environ['SystemRoot']) / 'System32')
    for directory in candidates:
        if all(is_x64_dll(directory / name) for name in VC_DLLS):
            return directory
    raise RuntimeError('缺少完整的 x64 Visual C++ 运行库，不能发布。请在构建机安装最新微软 '
                       'VC++ x64 运行库，或用 SUBTITLESTUDIO_VC_REDIST_DIR 指定 '
                       'Visual Studio 的 x64/Microsoft.VC*.CRT 目录。')


def copy_crt(target):
    source = crt_source()
    for name in VC_DLLS:
        shutil.copy2(source / name, target / name)
