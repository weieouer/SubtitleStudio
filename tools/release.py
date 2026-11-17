"""发布构建：默认完整 EXE 安装包（依赖内置、仅模型按需下载），旧便携 ZIP 工具保留。"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .development import process_environment, validate_environment
from .package_release import ROOT, package


def build_installer(output: Path, environment: Path, *, iscc: Path | None = None, stage_only=False) -> dict:
    from installer.build_installer import app_version

    python = validate_environment(environment)
    output = output.resolve()
    executable = output / f'SubtitleStudio-Setup-{app_version()}-x64-full.exe'
    if not stage_only and executable.exists():
        raise FileExistsError(f'安装包已存在，请指定其他输出目录：{executable}')
    from .prepare_runtime import prepared_runtime
    status = prepared_runtime(ROOT / 'installer/build/prepared-runtime')
    if not status['ready']:
        print('运行库需要重新准备：' + status['reason'], flush=True)
        subprocess.run([str(python), str(ROOT / 'tools/prepare_runtime.py')], cwd=ROOT,
                       env=process_environment(environment), check=True)
    command = [str(python), str(ROOT / 'installer/build_installer.py'), '--full', '--output', str(output)]
    if iscc:
        command += ['--iscc', str(iscc)]
    if stage_only:
        command.append('--stage-only')
    subprocess.run(command, cwd=ROOT, env=process_environment(environment), check=True)
    if stage_only:
        return {'stage': str(ROOT / 'installer/build/stage-full'), 'edition': 'full',
                'first_start_downloads_dependencies': False}
    if not executable.is_file():
        raise RuntimeError(f'未生成预期安装包：{executable}')
    return {'installer': str(executable), 'bytes': executable.stat().st_size, 'edition': 'full',
            'first_start_downloads_dependencies': False}


def build_portable(output: Path, environment: Path, *, include_work=False) -> dict:
    # 先检查输出，避免准备完依赖才发现目标文件已存在。
    output = output.resolve()
    if output.is_relative_to(ROOT) or output.exists():
        raise ValueError('请指定项目目录外、尚不存在的 ZIP 路径')
    python = validate_environment(environment)
    subprocess.run([str(python), str(ROOT / 'tools/prepare_wheels.py')], cwd=ROOT,
                   env=process_environment(environment), check=True)
    return package(output, include_work=include_work)
