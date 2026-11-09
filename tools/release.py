"""发布构建：默认在线 EXE 安装包，旧便携 ZIP 工具保留。"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .development import process_environment, validate_environment
from .package_release import ROOT, package


def build_installer(output: Path, environment: Path, *, iscc: Path | None = None, stage_only=False) -> dict:
    from installer.build_installer import app_version

    python = validate_environment(environment)
    output = output.resolve()
    executable = output / f'SubtitleStudio-Setup-{app_version()}-x64-online.exe'
    if not stage_only and executable.exists():
        raise FileExistsError(f'安装包已存在，请指定其他输出目录：{executable}')
    command = [str(python), str(ROOT / 'installer/build_installer.py'), '--output', str(output)]
    if iscc:
        command += ['--iscc', str(iscc)]
    if stage_only:
        command.append('--stage-only')
    subprocess.run(command, cwd=ROOT, env=process_environment(environment), check=True)
    if stage_only:
        return {'stage': str(ROOT / 'installer/build/stage-online'), 'edition': 'online',
                'first_start_downloads_dependencies': True}
    if not executable.is_file():
        raise RuntimeError(f'未生成预期安装包：{executable}')
    return {'installer': str(executable), 'bytes': executable.stat().st_size, 'edition': 'online',
            'first_start_downloads_dependencies': True}


def build_portable(output: Path, environment: Path, *, include_work=False) -> dict:
    # 先检查输出，避免准备完依赖才发现目标文件已存在。
    output = output.resolve()
    if output.is_relative_to(ROOT) or output.exists():
        raise ValueError('请指定项目目录外、尚不存在的 ZIP 路径')
    python = validate_environment(environment)
    subprocess.run([str(python), str(ROOT / 'tools/prepare_wheels.py')], cwd=ROOT,
                   env=process_environment(environment), check=True)
    return package(output, include_work=include_work)
