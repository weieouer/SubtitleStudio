"""验证在线安装文件首次启动：通过本地 HTTP 索引实际下载依赖，不污染全局环境。"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from check_development_release import inspect_server, package_list, stop_process, wait_ready

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    stage = ROOT / 'installer/build/stage-online'
    assert (stage / 'run.bat').is_file(), '先运行 py main.py build --stage-only'
    assert not (stage / 'wheels').exists()
    wheels = {path.name: path for path in (ROOT / 'wheels').glob('*.whl')}
    downloads = []

    def normalize(name):
        return re.sub(r'[-_.]+', '-', name).lower()

    class WheelIndex(BaseHTTPRequestHandler):
        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            if path.startswith('/simple/'):
                name = normalize(path.removeprefix('/simple/').strip('/'))
                matches = [filename for filename in wheels if normalize(filename.split('-')[0]) == name]
                payload = ('<!doctype html>' + ''.join(f'<a href="/files/{filename}">{filename}</a>'
                                                      for filename in matches)).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
            elif path.startswith('/files/') and path.removeprefix('/files/') in wheels:
                filename = path.removeprefix('/files/')
                payload = wheels[filename].read_bytes()
                downloads.append(filename)
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
            else:
                self.send_error(404)
                return
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *arguments):
            pass

    base = Path(getattr(sys, '_base_executable', None) or sys.executable)
    before = package_list(base)
    server = ThreadingHTTPServer(('127.0.0.1', 0), WheelIndex)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='subtitle-online-first-start-') as temporary:
            temporary_root = Path(temporary).resolve()
            assert temporary_root.is_relative_to(Path(tempfile.gettempdir()).resolve())
            installed = temporary_root / 'SubtitleStudio'
            shutil.copytree(stage, installed)
            assert not (installed / '.venv').exists()
            index = f'http://127.0.0.1:{server.server_port}/simple/'
            code = ('import sys; sys.path.insert(0,"tools"); import bootstrap; '
                    f'bootstrap.MIRRORS=({index!r},); '
                    'sys.argv=["bootstrap.py","--port","0","--no-browser"]; '
                    'raise SystemExit(bootstrap.main())')
            state = installed / 'data/run/instance.json'
            try:
                result = subprocess.run([str(installed / 'runtime/python/python.exe'), '-c', code],
                                        cwd=installed, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        encoding='utf-8', errors='replace', timeout=180, check=False)
                if result.returncode:
                    raise RuntimeError(result.stdout)
                info = wait_ready(state)
                health = inspect_server(info, installed)
                assert downloads, '未实际通过 HTTP 下载任何依赖'
                assert (installed / '.venv/Scripts/python.exe').is_file()
            finally:
                if state.is_file():
                    stop_process(json.loads(state.read_text(encoding='utf-8'))['pid'])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    after = package_list(base)
    assert before == after
    record = {'all_passed': True, 'bundled_wheels': False, 'http_downloaded_packages': len(downloads),
              'downloaded_files': downloads, 'health': health, 'global_packages_unchanged': True,
              'scope': '本地 HTTP 索引真实下载/安装；未验证公网镜像速度或安装向导交互'}
    destination = ROOT / 'docs/修复验证/在线EXE首次启动验证.json'
    destination.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: record[key] for key in ('all_passed', 'bundled_wheels', 'http_downloaded_packages',
                                                'global_packages_unchanged')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
