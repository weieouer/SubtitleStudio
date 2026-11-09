"""源码开发环境管理；与便携包首次启动流程相互独立。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from .pip_progress import requirement_count, run as run_pip

ROOT = Path(__file__).resolve().parents[1]


def environment_path(value: str | None = None) -> Path:
    configured = value or os.environ.get('SUBTITLESTUDIO_DEV_VENV')
    return (Path(configured).expanduser() if configured else ROOT / '.venv').resolve()


def python_path(environment: Path) -> Path:
    return environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def process_environment(environment: Path | None = None) -> dict:
    result = dict(os.environ)
    for name in ('PYTHONHOME', 'PYTHONPATH', 'PIP_TARGET', 'PIP_PREFIX', 'PIP_USER'):
        result.pop(name, None)
    result.update(PYTHONNOUSERSITE='1', PYTHONUTF8='1', PIP_REQUIRE_VIRTUALENV='true',
                  PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_CACHE_DIR=str(ROOT / 'data/cache/pip'))
    if environment is not None:
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
        code = ('import sys; from pathlib import Path; '
                f'assert sys.prefix != sys.base_prefix and Path(sys.prefix).resolve() == Path({str(environment)!r}); '
                'assert sys.version_info[:2] == (3, 13)')
        subprocess.run([str(python), '-I', '-c', code], check=True, capture_output=True,
                       env=process_environment(environment), timeout=30)
        return python
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('开发虚拟环境不存在或不可用，请运行 py -3.13 main.py setup；原目录已保留') from error


def install_command(python: Path, wheels: Path | None = None, index: str | None = None) -> list[str]:
    command = [str(python), '-I', '-m', 'pip', '--isolated', '--cache-dir', str(ROOT / 'data/cache/pip'),
               'install', '--disable-pip-version-check', '--no-input', '-r', str(ROOT / 'requirements.txt')]
    if (ROOT / 'requirements-lock.txt').is_file():
        command += ['-c', str(ROOT / 'requirements-lock.txt')]
    if wheels is not None:
        command += ['--no-index', '--only-binary=:all:', '--find-links', str(wheels)]
    elif index:
        command += ['--index-url', index, '--timeout', '20', '--retries', '1']
    return command


def setup_environment(environment: Path) -> Path:
    if sys.version_info[:2] != (3, 13):
        raise ValueError('开发环境使用 Python 3.13，请运行 py -3.13 main.py setup')
    validate_target(environment)
    if not environment.exists():
        environment.parent.mkdir(parents=True, exist_ok=True)
        base = getattr(sys, '_base_executable', None) or sys.executable
        print(f'创建项目虚拟环境：{environment}', flush=True)
        subprocess.run([str(base), '-I', '-m', 'venv', str(environment)], check=True)
    python = validate_environment(environment)
    wheels = ROOT / 'wheels'
    attempts = [install_command(python, wheels=wheels)] if any(wheels.glob('*.whl')) else []
    configuration = json.loads((ROOT / 'config/engines.json').read_text(encoding='utf-8'))
    attempts += [install_command(python, index=index) for index in configuration['indexes']]
    total = requirement_count(ROOT / 'requirements-lock.txt')
    # pip 被固定到验证过的虚拟环境解释器；--isolated 忽略用户的全局安装重定向设置。
    for command in attempts:
        if run_pip(command, total=total, label='安装项目依赖', env=process_environment(environment)) == 0:
            break
    else:
        raise RuntimeError('项目依赖安装失败，请查看 data/logs/pip.log')
    subprocess.run([str(python), '-I', '-m', 'pip', 'check'], check=True,
                   env=process_environment(environment))
    subprocess.run([str(python), '-I', '-c', 'import av, faster_whisper, numpy, jieba, rapidfuzz, pypinyin'],
                   check=True, env=process_environment(environment))
    print(f'开发环境已就绪：{python}', flush=True)
    return python
