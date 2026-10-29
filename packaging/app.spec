# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：onedir 模式（不要用 onefile）。

onefile 每次启动都要把 300MB+ 的依赖解压到临时目录，冷启动要十几秒；
onedir 只解压一次，双击秒开，也方便后续做安装包。

用法（在项目根目录）：
    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean ^
        --distpath dist --workpath build packaging\\app.spec
"""
import os

from PyInstaller.utils.hooks import collect_all

PROJECT = os.path.abspath(os.path.join(SPECPATH, os.pardir))  # noqa: F821

# 前端静态资源必须随包，否则界面 404
datas = [
    (os.path.join(PROJECT, "app", "static"), "app/static"),
]

binaries = []
hiddenimports = [
    # uvicorn 是动态导入的，不显式声明会漏
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "python_multipart",
]

# 这几个是带二进制 / 数据文件的包，必须整包收进来
_COLLECT = [
    "faster_whisper",   # 含 assets、自带 VAD 模型
    "ctranslate2",      # 推理后端 .pyd + DLL
    "onnxruntime",      # VAD 用的 onnxruntime
    "av",               # PyAV，解音频用
    "tokenizers",       # 分词，rust 扩展
    "huggingface_hub",  # 模型下载
    "jieba",            # 分词，带词典数据
    "pypinyin",         # 带数据表
    "srt",
]

for _pkg in _COLLECT:
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

# 明确排除，能把体积砍掉一大截
excludes = [
    "tkinter",
    "matplotlib",
    "scipy",
    "pandas",
    "IPython",
    "pytest",
    "torch",
    "torchaudio",
    "torchvision",
    "transformers",
    "tensorflow",
    "cv2",
    "PIL",
    "PyQt5",
    "PySide6",
]

a = Analysis(  # noqa: F821
    [os.path.join(PROJECT, "run.py")],
    pathex=[PROJECT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SubtitleStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # UPX 压 DLL 容易让 onnxruntime / ctranslate2 加载失败
    console=True,           # 保留控制台：出问题能看到报错，也方便看日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=os.path.join(SPECPATH, "version_info.txt"),  # noqa: F821
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SubtitleStudio",
)
