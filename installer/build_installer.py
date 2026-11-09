"""Build the Windows installer for Subtitle Studio.

Stages the same file set as ``tools/package_release.py`` (offline wheels and the
portable Python runtime included), then compiles ``installer/SubtitleStudio.iss``
with Inno Setup.

Usage:
    runtime\\python\\python.exe installer\\build_installer.py
    runtime\\python\\python.exe installer\\build_installer.py --iscc "C:\\...\\ISCC.exe"
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'installer'
ISS = INSTALLER / 'SubtitleStudio.iss'
APP = ROOT / 'backend' / 'app.py'


def app_version() -> str:
    text = APP.read_text(encoding='utf-8')
    match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)', text, re.MULTILINE)
    if not match:
        raise SystemExit('无法从 backend/app.py 读取 VERSION')
    return match.group(1)


def find_iscc(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return path
        raise SystemExit(f'找不到 ISCC：{path}')
    env = os.environ.get('INNO_SETUP_ISCC')
    candidates = [Path(env)] if env else []
    candidates += [
        Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'Inno Setup 6' / 'ISCC.exe',
        Path(r'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'),
        Path(r'C:\Program Files\Inno Setup 6\ISCC.exe'),
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise SystemExit(
        '未找到 Inno Setup 的 ISCC.exe。\n'
        '请安装 Inno Setup 6：https://jrsoftware.org/isdl.php\n'
        '或用 --iscc 指定 ISCC.exe 的完整路径。'
    )


def stage_release(stage: Path, offline: bool) -> int:
    sys.path.insert(0, str(ROOT))
    from tools import package_release

    build_root = (INSTALLER / 'build').resolve()
    if stage.is_symlink() or not stage.resolve().is_relative_to(build_root) or stage.resolve() == build_root:
        raise ValueError('暂存目录必须位于 installer/build 内，不能指向源码或其他目录')
    stage = stage.resolve()
    files = package_release.selected_files(ROOT, include_work=False)
    # Only ship what the app needs to run: no dev docs, no test suite.
    runtime_files = {'run.bat', 'requirements.txt', 'requirements-lock.txt',
                     'config/engines.json', 'config/ui.json', 'config/removal.json', 'config/removal-scan.json',
                     'tools/bootstrap.py', 'tools/pip_progress.py'}
    files = {name: path for name, path in files.items()
             if name.startswith(('backend/', 'frontend/', 'runtime/')) or name in runtime_files
             or offline and name.startswith('wheels/')}
    files['首次使用说明.txt'] = INSTALLER / '首次使用说明.txt'
    if not offline:
        # Dependencies can be downloaded from the mirrors in tools/bootstrap.py;
        # keeping the ~106 MB wheels out makes the installer much smaller.
        files = {name: path for name, path in files.items()
                 if not name.startswith('wheels/')}
    if stage.exists():
        try:
            shutil.rmtree(stage)
        except PermissionError:
            # Explorer or antivirus can hold a handle to the (empty) directory.
            if any(stage.iterdir()):
                raise
    stage.mkdir(parents=True, exist_ok=True)
    total = 0
    for relative, source in files.items():
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        total += source.stat().st_size
    mode = '离线版（含 wheels）' if offline else '在线版（首次启动下载依赖）'
    print(f'已暂存 {len(files)} 个文件，共 {total / 1024 / 1024:.1f} MB -> {stage}  [{mode}]')
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description='构建 Subtitle Studio 安装程序')
    parser.add_argument('--iscc', help='ISCC.exe 的完整路径')
    parser.add_argument('--output', default=str(ROOT / 'dist'), help='安装包输出目录')
    parser.add_argument('--stage-only', action='store_true', help='只生成暂存目录，不编译')
    parser.add_argument('--offline', action='store_true',
                        help='打包 wheels 做离线版（体积大，首次启动不需要联网）')
    args = parser.parse_args()

    version = app_version()
    stage = INSTALLER / 'build' / ('stage-offline' if args.offline else 'stage-online')
    stage_release(stage, offline=args.offline)
    if args.stage_only:
        return 0

    iscc = find_iscc(args.iscc)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    edition = 'offline' if args.offline else 'online'
    print(f'版本 {version}，{edition} 版，使用 {iscc}')

    command = [
        str(iscc),
        f'/DAppVersion={version}',
        f'/DStageDir={stage}',
        f'/DSourceRoot={ROOT}',
        f'/DOutputDir={output}',
        f'/DEdition={edition}',
        str(ISS),
    ]
    print('正在编译安装包（压缩需要一点时间）…')
    result = subprocess.run(command, cwd=str(INSTALLER), check=False)
    if result.returncode != 0:
        print(f'编译失败，ISCC 返回 {result.returncode}')
        return result.returncode

    exe = output / f'SubtitleStudio-Setup-{version}-x64-{edition}.exe'
    if not exe.is_file():
        print('编译命令成功，但未找到输出文件：' + str(exe))
        return 1
    print(f'完成：{exe}  ({exe.stat().st_size / 1024 / 1024:.1f} MB)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
