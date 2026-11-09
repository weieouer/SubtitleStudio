"""Run pip with one persistent console progress line; full output goes to pip.log."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / 'data' / 'logs' / 'pip.log'
INTERACTIVE = sys.stdout.isatty()

_ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
_COLLECT = re.compile(r'^Collecting\s+([A-Za-z0-9][A-Za-z0-9_.-]*)')
_PROCESS = re.compile(r'^Processing\s+(.+?\.whl)', re.IGNORECASE)
_DOWNLOAD = re.compile(r'^(?:Downloading|Using cached)\s+(\S+)', re.IGNORECASE)
_PROGRESS = re.compile(r'(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*([kKmMgG]?B)\b')
_SUFFIXES = ('.whl', '.tar.gz', '.tar.bz2', '.zip')
_UNITS = {'B': 1, 'KB': 1024, 'MB': 1024 ** 2, 'GB': 1024 ** 3}


def normalize(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name.strip().lower())


def requirement_count(path: Path):
    """Number of dependency lines to show as the progress total."""
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return None
    count = sum(1 for line in lines
                if line.strip() and not line.lstrip().startswith(('#', '-')))
    return count or None


def package_name(line: str):
    """Return the normalized package name pip is working on, if any."""
    line = _ANSI.sub('', line).replace('\r', '').lstrip()
    match = _COLLECT.match(line)
    if match:
        return normalize(match.group(1))
    match = _PROCESS.match(line)
    if match:
        name = Path(match.group(1)).name
    else:
        match = _DOWNLOAD.match(line)
        if not match:
            return None
        name = Path(match.group(1)).name
    for suffix in _SUFFIXES:
        if name.lower().endswith(suffix):
            name = name[:-len(suffix)]
            break
    else:
        return None
    # Wheel filenames replace '-' in the project name with '_'; take the first part.
    return normalize(name.split('-')[0])


def _human(size: float) -> str:
    value = float(size)
    for unit in ('B', 'kB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return f'{value:.0f} {unit}' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1024
    return f'{value:.1f} GB'


def _write(text: str):
    columns = max(30, shutil.get_terminal_size((100, 20)).columns - 1)
    text = text[:columns]
    if INTERACTIVE:
        sys.stdout.write('\r' + text.ljust(columns))
    else:
        sys.stdout.write(text + '\n')
    sys.stdout.flush()


def _bar(done: int, total, label: str, final: bool = False):
    if total:
        ratio = 1.0 if final else min(done / total, 1.0)
        cells = 22
        filled = int(ratio * cells)
        text = f'[{"#" * filled}{"-" * (cells - filled)}] {min(done, total):>3}/{total}  {label}'
    else:
        text = f'[{done:>3} packages] {label}'
    _write(text)


def _size_bar(done: float, total: float, label: str):
    ratio = 1.0 if total <= 0 else min(done / total, 1.0)
    cells = 22
    filled = int(ratio * cells)
    text = (f'[{"#" * filled}{"-" * (cells - filled)}] {ratio * 100:3.0f}%  '
            f'{_human(done)}/{_human(total)}  {label}')
    _write(text)


def run(command, total=None, label: str = 'Installing', *, env=None):
    """Run a pip command, updating one console line; return the pip exit code."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    process_env = {**(os.environ if env is None else env), 'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1',
           'PYTHONIOENCODING': 'utf-8'}
    try:
        process = subprocess.Popen(
            command, cwd=ROOT, env=process_env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding='utf-8',
            errors='replace', bufsize=1,
        )
    except OSError as error:
        print(f'Could not start pip: {error}', flush=True)
        return 1
    seen = set()
    current = ''
    tail = []
    with LOG.open('w', encoding='utf-8', errors='replace') as log:
        assert process.stdout is not None
        for raw in process.stdout:
            line = raw.rstrip('\r\n')
            log.write(line + '\n')
            log.flush()
            name = package_name(line)
            if name:
                current = name
                if name not in seen:
                    seen.add(name)
                    _bar(len(seen), total, f'{label} {name}')
                continue
            clean = _ANSI.sub('', line).replace('\r', '').strip()
            match = _PROGRESS.search(clean)
            if match:
                unit = _UNITS.get(match.group(3).upper(), 1)
                done = float(match.group(1)) * unit
                size = float(match.group(2)) * unit
                if size > 0:
                    shown = f'{label} {current}'.strip()
                    _size_bar(min(done, size), size, shown)
                continue
            if clean and not clean.startswith(('━', '─')):
                tail.append(clean)
                if len(tail) > 40:
                    tail.pop(0)
                if not seen:
                    # 若 pip 输出格式变化导致一行都没识别到，至少让控制台有原始输出。
                    print(clean, flush=True)
        code = process.wait()
    if seen:
        if code == 0:
            _bar(total or len(seen), total or len(seen), f'{label} 完成', final=True)
        else:
            _bar(len(seen), total, f'{label} 已停止')
    if INTERACTIVE:
        sys.stdout.write('\n')
        sys.stdout.flush()
    if code != 0:
        print(f'pip exited with code {code}; last output:', flush=True)
        for line in tail[-20:]:
            print('  ' + line, flush=True)
    return code
