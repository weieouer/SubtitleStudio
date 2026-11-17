"""Windows 推理进程与发布构建共用的 C++ 运行库约定。"""
from __future__ import annotations

import os
from pathlib import Path
import sys

VC_DLLS = ('vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll',
           'msvcp140_1.dll', 'msvcp140_2.dll', 'msvcp140_atomic_wait.dll',
           'msvcp140_codecvt_ids.dll', 'concrt140.dll')
VC_DOWNLOAD = 'https://aka.ms/vs/17/release/vc_redist.x64.exe'
_DLL_HANDLES = []
_DLL_PATHS = set()


def configure_libraries():
    """在导入 torch 前注册随程序发布的 CRT；句柄必须存活到进程结束。"""
    if os.name != 'nt':
        return
    root = Path(__file__).resolve().parents[2]
    for directory in (Path(sys.executable).parent, Path(sys.base_prefix),
                      root / 'runtime/python'):
        directory = directory.resolve()
        if directory in _DLL_PATHS or not (directory / 'msvcp140.dll').is_file():
            continue
        _DLL_HANDLES.append(os.add_dll_directory(str(directory)))
        _DLL_PATHS.add(directory)


def engine_failure(log):
    """给已知环境错误提供可执行的修复步骤，其他错误保留诊断尾部。"""
    if ('Microsoft Visual C++ Redistributable is not installed' in log
            or any(name in log.lower() for name in ('msvcp140.dll', 'vcruntime140_1.dll'))):
        return ('缺少或无法加载 Microsoft Visual C++ x64 运行库，强制对齐引擎未能启动。'
                '请安装或修复微软运行库后重启程序，再重新对齐（无需重新校准或下载模型）：'
                + VC_DOWNLOAD)
    return log[-1600:]
