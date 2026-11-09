"""实测开发入口与便携版首次启动；只操作临时便携副本和本次创建的进程。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.package_release import selected_files


def stop_process(pid):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        import signal
        os.kill(pid, signal.SIGTERM)


def wait_ready(path, process=None):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding='utf-8'))
            except ValueError:
                pass
        if process and process.poll() is not None:
            raise RuntimeError(f'服务提前退出：{process.returncode}')
        time.sleep(.1)
    raise RuntimeError('服务未按时就绪：' + str(path))


def inspect_server(info, expected_root):
    url = info['url']
    if not re.fullmatch(r'http://127\.0\.0\.1:\d+/', url):
        raise RuntimeError('就绪地址不是本机地址')
    with urlopen(url, timeout=5) as response:
        html = response.read().decode('utf-8')
    token = re.search(r'name="subtitle-session" content="([^"]+)"', html).group(1)
    responses = {}
    for endpoint in ('api/health', 'api/models'):
        request = Request(url + endpoint, headers={'X-SubtitleStudio-Token': token})
        with urlopen(request, timeout=10) as response:
            responses[endpoint] = json.load(response)
    health = responses['api/health']
    assert health['ok'] is True
    assert Path(health['data_dir']).resolve() == (expected_root / 'data').resolve()
    return {'url': url, 'version': health['version'], 'health_ok': True,
            'models_api_ok': responses['api/models']['ok'] is True}


def package_list(python):
    return json.loads(subprocess.check_output(
        [str(python), '-m', 'pip', 'list', '--format=json', '--disable-pip-version-check'],
        encoding='utf-8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    base_python = Path(getattr(sys, '_base_executable', None) or sys.executable)
    before = package_list(base_python)
    dev_python = subprocess.check_output(
        [str(base_python), str(ROOT / 'main.py'), 'python'], encoding='utf-8').strip()
    subprocess.run([str(base_python), str(ROOT / 'main.py'), 'setup'], cwd=ROOT, check=True)
    probe_code = (
        'import sys,json,av,faster_whisper,numpy,jieba,rapidfuzz,pypinyin; '
        'print(json.dumps({"executable":sys.executable,"prefix":sys.prefix,'
        '"base_prefix":sys.base_prefix,"modules":{m.__name__:m.__file__ for m in '
        '(av,faster_whisper,numpy,jieba,rapidfuzz,pypinyin)}}))'
    )
    environment = json.loads(subprocess.check_output([dev_python, '-I', '-c', probe_code], encoding='utf-8'))
    prefix = Path(environment['prefix']).resolve()
    assert prefix != Path(environment['base_prefix']).resolve()
    assert all(Path(path).resolve().is_relative_to(prefix) for path in environment['modules'].values())
    results = {'development_environment': environment}

    with tempfile.TemporaryDirectory(prefix='subtitle-development-release-') as temporary:
        temporary_root = Path(temporary).resolve()
        assert temporary_root.is_relative_to(Path(tempfile.gettempdir()).resolve())
        ready = temporary_root / 'dev-ready.json'
        development_state = ROOT / 'data/run/instance.json'
        previous_state = development_state.read_bytes() if development_state.is_file() else None
        development_info = None
        with (temporary_root / 'dev-server.log').open('wb') as output:
            process = subprocess.Popen([str(base_python), str(ROOT / 'main.py'), '--port', '0', '--no-browser'],
                                       cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                                       env={**os.environ, 'SUBTITLESTUDIO_READY_FILE': str(ready)})
            try:
                development_info = wait_ready(ready, process)
                results['development_startup'] = inspect_server(development_info, ROOT)
            finally:
                stop_process(process.pid)
                process.wait(timeout=10)
                if development_info and development_state.is_file():
                    current = json.loads(development_state.read_text(encoding='utf-8'))
                    if current.get('pid') == development_info['pid']:
                        if previous_state is None:
                            development_state.unlink()
                        else:
                            development_state.write_bytes(previous_state)

        portable = temporary_root / 'SubtitleStudio'
        files = selected_files(ROOT, include_work=False)
        source_only = temporary_root / 'SourceOnly'
        for relative, source in files.items():
            if relative.startswith(('runtime/', 'wheels/')) or relative == 'run.bat':
                continue
            target = source_only / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        source_ready = temporary_root / 'source-ready.json'
        with (temporary_root / 'source-server.log').open('wb') as output:
            process = subprocess.Popen([str(base_python), str(source_only / 'main.py'), '--port', '0', '--no-browser'],
                                       cwd=source_only, stdout=output, stderr=subprocess.STDOUT,
                                       env={**os.environ, 'SUBTITLESTUDIO_READY_FILE': str(source_ready),
                                            'SUBTITLESTUDIO_DEV_VENV': str(prefix)})
            try:
                results['source_only_startup'] = inspect_server(wait_ready(source_ready, process), source_only)
                assert not any((source_only / name).exists() for name in ('runtime', 'wheels', 'run.bat', '.venv'))
                results['source_only_startup']['no_portable_resources_required'] = True
            finally:
                stop_process(process.pid)
                process.wait(timeout=10)
        for relative, source in files.items():
            target = portable / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        assert not (portable / '.venv').exists()
        assert not (portable / 'data/models').exists()
        print('Testing first start from a clean portable copy...', flush=True)
        state_file = portable / 'data/run/instance.json'
        try:
            started = subprocess.run(['cmd', '/d', '/c', '.\\run.bat', '--port', '0', '--no-browser'],
                                     cwd=portable, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     timeout=180, encoding='utf-8', errors='replace')
            if started.returncode:
                raise RuntimeError(started.stdout)
            info = wait_ready(state_file)
            result = inspect_server(info, portable)
            result['bootstrap_log'] = (portable / 'data/logs/bootstrap.log').read_text(encoding='utf-8')
            result['virtual_environment_created'] = (portable / '.venv/pyvenv.cfg').is_file()
            result['model_weights_absent'] = not any((portable / 'data/models').iterdir())
            assert result['virtual_environment_created'] and result['model_weights_absent']
            results['portable_first_start'] = result
        finally:
            if state_file.is_file():
                info = json.loads(state_file.read_text(encoding='utf-8'))
                stop_process(info['pid'])
        # 后台服务退出后再清理本次创建的临时目录。
        time.sleep(.3)

    after = package_list(base_python)
    assert before == after, '全局 Python 包列表发生变化'
    results['global_packages_unchanged'] = True
    results['global_package_count'] = len(after)
    results['all_passed'] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'all_passed': True, 'output': str(args.output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
