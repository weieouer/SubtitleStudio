"""First-start dependency install with a visible overall progress line."""
from __future__ import annotations

from pathlib import Path
import sys

try:
    from pip_progress import requirement_count, run
except Exception:  # Keep first start working even if the progress helper is broken.
    requirement_count = lambda path: None
    run = None

ROOT = Path(__file__).resolve().parents[1]
WHEELS = ROOT / 'wheels'
REQUIREMENTS = ROOT / 'requirements.txt'
LOCK = ROOT / 'requirements-lock.txt'
MIRRORS = (
    'https://mirrors.aliyun.com/pypi/simple',
    'https://pypi.tuna.tsinghua.edu.cn/simple',
    'https://mirrors.ustc.edu.cn/pypi/simple',
    'https://repo.huaweicloud.com/repository/pypi/simple',
    'https://pypi.org/simple',
)


def install_command(offline: bool, mirror: str | None = None):
    command = [sys.executable, '-m', 'pip', 'install', '--no-input',
               '--disable-pip-version-check', '--progress-bar', 'off']
    if offline:
        command += ['--no-index', '--only-binary=:all:',
                    '--find-links', str(WHEELS)]
    else:
        command += ['--timeout', '20', '--retries', '1', '--index-url', mirror]
    return command + ['-r', str(REQUIREMENTS)]


def execute(command, total, label: str):
    if run is None:
        import subprocess
        return subprocess.run(command, cwd=ROOT).returncode
    return run(command, total=total, label=label)


def main():
    wheels = sorted(WHEELS.glob('*.whl'))
    if wheels:
        print(f'Installing {len(wheels)} bundled packages. No internet needed.', flush=True)
        if execute(install_command(True), len(wheels), 'Installing') == 0:
            return 0
        print('Bundled install failed; trying online mirrors.', flush=True)
    else:
        print('No bundled wheels found. First start downloads about 110 MB.', flush=True)

    total = requirement_count(LOCK)
    for mirror in MIRRORS:
        print(f'Trying {mirror}', flush=True)
        if execute(install_command(False, mirror), total, 'Downloading') == 0:
            return 0
    return 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as error:  # 2 tells run.bat to use its built-in online path.
        print(f'Bootstrap failed: {error}', flush=True)
        raise SystemExit(2)
