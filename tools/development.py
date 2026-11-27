"""源码开发环境管理；与便携包首次启动流程相互独立。"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def environment_path(value: str | None = None, *, root: Path = ROOT) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    uv_environment = os.environ.get('UV_PROJECT_ENVIRONMENT')
    if uv_environment:
        return (root / Path(uv_environment)).resolve()
    legacy_environment = os.environ.get('SUBTITLESTUDIO_DEV_VENV')
    return Path(legacy_environment).expanduser().resolve() if legacy_environment else (root / '.venv').resolve()


def python_path(environment: Path) -> Path:
    return environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def process_environment(environment: Path | None = None) -> dict:
    result = dict(os.environ)
    for name in ('PYTHONHOME', 'PYTHONPATH', 'PIP_TARGET', 'PIP_PREFIX', 'PIP_USER'):
        result.pop(name, None)
    result.update(PYTHONNOUSERSITE='1', PYTHONUTF8='1', PIP_REQUIRE_VIRTUALENV='true',
                  PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_CACHE_DIR=str(ROOT / 'data/cache/pip'))
    if environment is not None:
        result['UV_PROJECT_ENVIRONMENT'] = str(environment)
        result['SUBTITLESTUDIO_DEV_VENV'] = str(environment)
    return result


def validate_target(environment: Path) -> None:
    if environment in (Path(sys.base_prefix).resolve(), ROOT):
        raise ValueError('虚拟环境不能指向全局 Python 或项目根目录')
    if environment.exists() and not (environment / 'pyvenv.cfg').is_file():
        raise ValueError('目标目录已存在且不是虚拟环境，请选择新的独立目录')


def validate_environment(environment: Path) -> Path:
    validate_target(environment)
    try:
        settings = dict(line.lower().split('=', 1) for line in
                        (environment / 'pyvenv.cfg').read_text(encoding='utf-8').splitlines() if '=' in line)
        settings = {key.strip(): value.strip() for key, value in settings.items()}
        if settings.get('include-system-site-packages') != 'false':
            raise ValueError('开发环境必须关闭全局包继承；原目录已保留')
        python = python_path(environment)
        if sys.prefix != sys.base_prefix and Path(sys.prefix).resolve() == environment:
            if sys.version_info[:2] != (3, 13):
                raise ValueError('开发环境需要 Python 3.13；请另选虚拟环境目录')
            return python
        code = ('import sys; from pathlib import Path; '
                f'assert sys.prefix != sys.base_prefix and Path(sys.prefix).resolve() == Path({str(environment)!r}); '
                'assert sys.version_info[:2] == (3, 13)')
        subprocess.run([str(python), '-I', '-c', code], check=True, capture_output=True,
                       env=process_environment(environment), timeout=30)
        return python
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('开发虚拟环境不存在或不可用，请运行 uv sync --locked；已有目录会保留') from error


def uv_executable() -> str:
    executable = shutil.which('uv')
    if executable is None:
        raise RuntimeError('找不到 uv，请安装 uv 并确认它在 PATH 中')
    return executable


def export_requirements(destination: Path, *, root: Path = ROOT) -> None:
    # jieba 的离线 wheel 由源码构建，不能使用 PyPI 源码包的哈希做 wheel 校验。
    subprocess.run(
        [uv_executable(), 'export', '--project', str(root), '--locked', '--no-hashes',
         '--no-annotate', '--no-header', '--output-file', str(destination), '--quiet'],
        cwd=root, check=True,
    )


def setup_environment(environment: Path) -> Path:
    validate_target(environment)
    if environment.exists():
        validate_environment(environment)
    child_environment = process_environment(environment)
    subprocess.run([uv_executable(), 'sync', '--project', str(ROOT), '--locked', '--no-active'],
                   cwd=ROOT, env=child_environment, check=True)
    python = validate_environment(environment)
    subprocess.run([str(python), '-I', '-c', 'import av, faster_whisper, numpy, jieba, rapidfuzz, pypinyin'],
                   check=True, env=child_environment)
    print(f'开发环境已就绪：{python}', flush=True)
    return python
