"""One-click startup: check the bundled environment, repair what is missing,
then launch the app. The console shows one line per check and a persistent
progress bar while packages are being restored.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback

try:
    if __package__:
        from .pip_progress import requirement_count, run as pip_run
    else:
        from pip_progress import requirement_count, run as pip_run
except Exception:  # Keep startup working even if the progress helper is broken.
    requirement_count = lambda path: None
    pip_run = None

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'runtime' / 'python' / 'python.exe'
VENV = ROOT / '.venv'
VENV_PY = VENV / 'Scripts' / 'python.exe'
WHEELS = ROOT / 'wheels'
REQUIREMENTS = ROOT / 'requirements.txt'
LOCK = ROOT / 'requirements-lock.txt'
LAUNCH = ROOT / 'backend' / 'launch.py'
LOG_DIR = ROOT / 'data' / 'logs'
BOOT_LOG = LOG_DIR / 'bootstrap.log'

MODULES = ('av', 'faster_whisper', 'numpy', 'jieba', 'rapidfuzz', 'pypinyin')
MAIN_IMPORTS = 'import av, faster_whisper, numpy, jieba, rapidfuzz, pypinyin'
REQUIRED_FILES = ('backend/app.py', 'backend/launch.py', 'frontend/index.html',
                  'requirements.txt')
DATA_DIRS = ('data', 'data/models', 'data/engines', 'data/logs',
             'data/cache', 'data/run', 'data/uploads')
WEIGHT_FILES = ('model.bin', 'model.safetensors', 'model.pth.tar', 'model.pt')
MIRRORS = (
    'https://mirrors.aliyun.com/pypi/simple',
    'https://pypi.tuna.tsinghua.edu.cn/simple',
    'https://mirrors.ustc.edu.cn/pypi/simple',
    'https://repo.huaweicloud.com/repository/pypi/simple',
    'https://pypi.org/simple',
)
TOTAL_STEPS = 6

_log_file = None


def log(message=''):
    print(message, flush=True)
    if _log_file is not None:
        _log_file.write(message + '\n')
        _log_file.flush()


def begin(number, name):
    print(f'[{number}/{TOTAL_STEPS}] {name} ... ', end='', flush=True)


def end(status):
    print(status, flush=True)


def probe(python, code, timeout=120):
    """Run a short check with the given interpreter; return (ok, output)."""
    try:
        result = subprocess.run(
            [str(python), '-I', '-c', code], cwd=ROOT,
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=timeout, env={**os.environ, 'PYTHONUTF8': '1'},
        )
        return result.returncode == 0, (result.stdout or '') + (result.stderr or '')
    except Exception as error:  # noqa: BLE001 - report any probe failure
        return False, str(error)


def run_spinner(command, label):
    """Run something slow (venv creation, ensurepip) with a live spinner."""
    started = time.time()
    try:
        process = subprocess.Popen(
            [str(part) for part in command], cwd=ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        log(f'      无法启动：{error}')
        return 1
    frames = '|/-\\'
    index = 0
    columns = max(30, shutil.get_terminal_size((100, 20)).columns - 1)
    while process.poll() is None:
        if sys.stdout.isatty():
            elapsed = int(time.time() - started)
            text = f'      {frames[index % 4]} {label} {elapsed}s'
            sys.stdout.write('\r' + text[:columns])
            sys.stdout.flush()
        index += 1
        time.sleep(0.15)
    if sys.stdout.isatty():
        sys.stdout.write('\r' + ' ' * columns + '\r')
        sys.stdout.flush()
    return process.wait()


def venv_ready():
    if not VENV_PY.is_file():
        return False
    ok, _detail = probe(
        VENV_PY,
        'import sys; from pathlib import Path; '
        'raise SystemExit(0 if sys.version_info >= (3, 12) '
        'and sys.prefix != sys.base_prefix '
        f'and Path(sys.prefix).resolve() == Path({str(VENV)!r}).resolve() else 1)',
    )
    return ok


def rebuild_venv():
    if VENV.exists():
        broken = VENV.with_name('.venv.broken_' + time.strftime('%Y%m%d_%H%M%S'))
        try:
            VENV.rename(broken)
            log(f'      旧环境已改名为 {broken.name}')
        except OSError as error:
            log(f'      旧环境无法改名（{error}），直接删除')
            shutil.rmtree(VENV, ignore_errors=True)
    log(f'      正在创建虚拟环境 {VENV}（首次需要一点时间）…')
    VENV.parent.mkdir(parents=True, exist_ok=True)
    code = run_spinner([RUNTIME, '-m', 'venv', str(VENV)], '正在创建虚拟环境')
    return code == 0 and venv_ready()


def pip_ready():
    ok, _detail = probe(VENV_PY, 'import pip')
    return ok


def repair_pip():
    log('      正在补全 pip…')
    run_spinner([VENV_PY, '-m', 'ensurepip', '--upgrade'], '正在补全 pip')
    return pip_ready()


def missing_modules():
    code = (
        'import importlib.util, json\n'
        f'mods = {list(MODULES)!r}\n'
        'print(json.dumps([m for m in mods if importlib.util.find_spec(m) is None]))\n'
    )
    ok, output = probe(VENV_PY, code)
    if not ok:
        return list(MODULES)
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith('['):
            try:
                return json.loads(line)
            except ValueError:
                pass
    return list(MODULES)


def install_command(offline, mirror=None):
    command = [str(VENV_PY), '-m', 'pip', 'install', '--no-input',
               '--disable-pip-version-check', '--progress-bar', 'on']
    if offline:
        command += ['--no-index', '--only-binary=:all:',
                    '--find-links', str(WHEELS)]
    else:
        command += ['--timeout', '20', '--retries', '1', '--index-url', mirror]
    return command + ['-r', str(REQUIREMENTS)]


def execute(command, total, label):
    if pip_run is None:
        return subprocess.call([str(part) for part in command], cwd=ROOT)
    return pip_run(command, total=total, label=label)


def install_dependencies():
    wheels = sorted(WHEELS.glob('*.whl'))
    if wheels:
        log(f'      使用包内 wheels 离线补全（{len(wheels)} 个包）…')
        if execute(install_command(True), len(wheels), '离线补全') == 0:
            return True
        log('      离线补全未完成，改为联网下载…')
    else:
        log('      包内没有 wheels，正在联网下载…')
    total = requirement_count(LOCK) or requirement_count(REQUIREMENTS)
    for mirror in MIRRORS:
        log(f'      尝试 {mirror}')
        if execute(install_command(False, mirror), total, '下载') == 0:
            return True
    return False


def imports_ready():
    ok, _detail = probe(VENV_PY, MAIN_IMPORTS, timeout=300)
    return ok


def model_names():
    base = ROOT / 'data' / 'models'
    found = []
    try:
        for folder in sorted(base.iterdir()):
            if folder.is_dir() and any((folder / name).is_file() for name in WEIGHT_FILES):
                found.append(folder.name)
    except OSError:
        pass
    return found


def step_runtime():
    begin(1, 'Python 运行环境')
    ok, _detail = probe(
        RUNTIME,
        'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)',
    )
    if ok:
        end('通过')
        return True
    end('缺失')
    log('      程序内置 Python 不完整，请重新安装或完整解压程序包。')
    return False


def step_venv():
    begin(2, '虚拟环境 .venv')
    if venv_ready():
        end('通过')
        return True
    end('缺失，正在创建')
    if not rebuild_venv():
        log('      创建虚拟环境失败。')
        return False
    log('      虚拟环境已就绪')
    return True


def step_pip():
    begin(3, 'pip')
    if pip_ready():
        end('通过')
        return True
    end('缺失，正在补全')
    if not repair_pip():
        log('      pip 补全失败。')
        return False
    log('      pip 已就绪')
    return True


def step_dependencies():
    begin(4, '运行依赖')
    missing = missing_modules()
    if not missing:
        end('通过')
        return True
    end('缺失 ' + '、'.join(missing) + '，正在补全')
    if not install_dependencies():
        log('      依赖补全失败，请检查网络，或查看 data\\logs\\pip.log。')
        return False
    if not imports_ready():
        log('      依赖已安装但导入失败，请查看 data\\logs\\pip.log。')
        return False
    log('      依赖已补全')
    return True


def step_program_files():
    begin(5, '程序文件')
    missing = [name for name in REQUIRED_FILES if not (ROOT / name).is_file()]
    if missing:
        end('缺失')
        log('      缺少：' + '、'.join(missing))
        log('      请重新安装或完整解压程序包。')
        return False
    end('通过')
    return True


def step_data():
    begin(6, '数据目录')
    for name in DATA_DIRS:
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    end('通过')
    models = model_names()
    if models:
        log('      模型：已就位 ' + '、'.join(models))
    else:
        log('      模型：未放置（可在界面里下载）')


def main(arguments=None, *, setup_only=False):
    global _log_file
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log_file = BOOT_LOG.open('w', encoding='utf-8', errors='replace')
    try:
        log('字幕工作室 · 启动检查')
        log('-' * 60)
        for step in (step_runtime, step_venv, step_pip,
                     step_dependencies, step_program_files, step_data):
            if step() is False:
                log('-' * 60)
                log('检查未通过，请按上面的提示处理后重试。')
                return 1
        log('-' * 60)
        if setup_only:
            log(f'环境已就绪：{VENV_PY}')
            return 0
        log('全部检查通过，正在打开界面…')
        return subprocess.call(
            [str(VENV_PY), str(LAUNCH), *(sys.argv[1:] if arguments is None else arguments)], cwd=ROOT,
        )
    except Exception:  # noqa: BLE001 - keep the window open with details
        log('启动检查出错：')
        log(traceback.format_exc())
        return 1
    finally:
        if _log_file is not None:
            _log_file.close()
            _log_file = None


if __name__ == '__main__':
    raise SystemExit(main())
