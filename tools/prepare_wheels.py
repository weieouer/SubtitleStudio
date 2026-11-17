"""把运行依赖预先下载成 wheel，放进 wheels/ 供换机包首次启动离线安装。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

try:
    from pip_progress import requirement_count, run as run_pip
except Exception:  # Packaging must still work if the progress display is unavailable.
    requirement_count = lambda path: None
    run_pip = None

ROOT = Path(__file__).resolve().parents[1]
WHEELS = ROOT / 'wheels'
MARKER = WHEELS / '.prepared.json'
LOCK = ROOT / 'requirements-lock.txt'
REQUIREMENTS = ROOT / 'requirements.txt'
RUNTIME = ROOT / 'runtime/python/python.exe'
MIRRORS = (
    'https://mirrors.aliyun.com/pypi/simple',
    'https://pypi.tuna.tsinghua.edu.cn/simple',
    'https://mirrors.ustc.edu.cn/pypi/simple',
    'https://repo.huaweicloud.com/repository/pypi/simple',
    'https://pypi.org/simple',
)


def wheel_files():
    return sorted(WHEELS.glob('*.whl'))


def source():
    """锁文件包含全部传递依赖，离线安装才完整；没有锁文件时退回主清单。"""
    return LOCK if LOCK.is_file() else REQUIREMENTS


def bundled_python():
    if not RUNTIME.is_file():
        return None
    try:
        result = subprocess.run(
            [str(RUNTIME), '-I', '-c', 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def check_pip():
    """使用带 pip 的项目虚拟环境，开发环境与便携环境都可准备 wheel。"""
    result = subprocess.run([sys.executable, '-m', 'pip', '--version'],
                            capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode != 0:
        raise SystemExit('pip is not available in this Python. '
                         'Run py main.py setup, then use the project Python to prepare wheels.')


def check_target_python():
    """wheel 必须匹配随包 runtime 的 Python，而不是只匹配当前 .venv。"""
    target = bundled_python()
    current = f'{sys.version_info.major}.{sys.version_info.minor}'
    if target and target != current:
        raise SystemExit(f'Wheels must match bundled Python {target}, but this run uses {current}. '
                         f'Use a Python {target} virtual environment to prepare wheels; '
                         'keep the existing environment intact.')


def inputs_digest():
    """requirements.txt 或锁文件任一变化，旧 wheel 都不再可信。"""
    digest = hashlib.sha256()
    for path in (REQUIREMENTS, LOCK):
        if path.is_file():
            digest.update(path.name.encode('utf-8'))
            digest.update(b'\0')
            digest.update(path.read_bytes())
    return digest.hexdigest()


def current():
    files = wheel_files()
    if not files:
        return False
    try:
        marker = json.loads(MARKER.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return (
        marker.get('inputs_sha256') == inputs_digest()
        and marker.get('python') == f'{sys.version_info.major}.{sys.version_info.minor}'
        and marker.get('system') == sys.platform
        and marker.get('wheels') == len(files)
        and marker.get('bytes') == sum(path.stat().st_size for path in files)
    )


def build(source_path: Path):
    WHEELS.mkdir(parents=True, exist_ok=True)
    for old in wheel_files():
        old.unlink()
    MARKER.unlink(missing_ok=True)
    os.environ['PIP_CACHE_DIR'] = str(ROOT / 'data/cache/pip')
    env = {**os.environ, 'PYTHONUTF8': '1'}
    command = [
        sys.executable, '-m', 'pip', 'wheel', '--no-input', '--disable-pip-version-check',
        '--prefer-binary', '--progress-bar', 'off', '--timeout', '30', '--retries', '2',
        '--wheel-dir', str(WHEELS), '-r', str(source_path),
    ]
    total = requirement_count(source_path)
    for mirror in MIRRORS:
        print(f'Downloading wheels from {mirror}', flush=True)
        attempt = [*command, '--index-url', mirror]
        if run_pip is None:
            code = subprocess.run(attempt, cwd=ROOT, env=env).returncode
        else:
            try:
                code = run_pip(attempt, total=total, label='Downloading')
            except Exception as error:
                print(f'Progress display failed: {error}', flush=True)
                code = subprocess.run(attempt, cwd=ROOT, env=env).returncode
        if code == 0:
            return
    raise SystemExit('Wheel download failed. Check the network and run this tool again.')


def verify():
    """按主清单做一次离线解析，确认这些 wheel 足够装完运行依赖。"""
    command = [
        sys.executable, '-m', 'pip', 'install', '--dry-run', '--ignore-installed',
        '--no-input', '--disable-pip-version-check', '--no-index', '--only-binary=:all:',
        '--find-links', str(WHEELS), '-r', str(REQUIREMENTS),
    ]
    if subprocess.run(command, cwd=ROOT).returncode != 0:
        raise SystemExit('The wheel set is incomplete; pip cannot install requirements.txt offline.')
    files = wheel_files()
    marker = {
        'python': f'{sys.version_info.major}.{sys.version_info.minor}',
        'system': sys.platform,
        'requirements': source().name,
        'inputs_sha256': inputs_digest(),
        'wheels': len(files),
        'bytes': sum(path.stat().st_size for path in files),
        'created': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }
    MARKER.write_text(json.dumps(marker, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return marker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--force', action='store_true', help='忽略已有 wheel，重新下载')
    parser.add_argument('--check', action='store_true', help='只检查离线依赖是否已就绪')
    args = parser.parse_args()
    check_target_python()
    source_path = source()
    if not args.force and current():
        result = {'ready': True, 'wheels': len(wheel_files()), 'files': [path.name for path in wheel_files()]}
    elif args.check:
        raise SystemExit('Offline wheels are missing or outdated. Run tools/prepare_wheels.py first.')
    else:
        check_pip()
        build(source_path)
        result = {'ready': True, **verify()}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
