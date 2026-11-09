"""视频修复模型和独立环境；不向项目或全局 Python 安装推理依赖。"""
from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import subprocess
import threading
import urllib.request
import venv
import zipfile
from contextlib import contextmanager
from pathlib import PurePosixPath

from .paths import CONFIG_DIR, DATA_DIR, MODELS_DIR
from .storage import atomic_json

ENGINE = DATA_DIR / 'engines' / 'removal'
MODEL = MODELS_DIR / 'propainter'
SOURCE = ENGINE / 'source'
DETECTOR = MODELS_DIR / 'ppocrv5-det' / 'det.onnx'


class RemovalCancelled(Exception):
    pass


@contextmanager
def installation_lock():
    # 长时间安装只锁本引擎；不能占用工程保存共用的 WRITE_LOCK。
    with (ENGINE / 'installation.lock').open('a+b') as handle:
        if not handle.tell():
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def specification():
    spec = json.loads((CONFIG_DIR / 'removal.json').read_text(encoding='utf-8'))
    spec['detector'] = json.loads((CONFIG_DIR / 'removal-scan.json').read_text(encoding='utf-8'))
    return spec


def interpreter():
    return ENGINE / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def nvidia_available():
    try:
        driver = ctypes.WinDLL('nvcuda.dll') if os.name == 'nt' else ctypes.CDLL('libcuda.so.1')
        count = ctypes.c_int()
        return driver.cuInit(0) == 0 and driver.cuDeviceGetCount(ctypes.byref(count)) == 0 and count.value > 0
    except (OSError, AttributeError):
        return False


def environment(device='cpu'):
    result = {k: v for k, v in os.environ.items()
              if not k.startswith('PIP_') and k not in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV')}
    result.update(PYTHONUTF8='1', PYTHONUNBUFFERED='1', PYTHONNOUSERSITE='1',
                  PIP_CONFIG_FILE=os.devnull, VIRTUAL_ENV=str(ENGINE),
                  PATH=str(interpreter().parent) + os.pathsep + result.get('PATH', ''))
    if device == 'cpu':
        result['CUDA_VISIBLE_DEVICES'] = ''
    return result


def model_status():
    spec = specification()
    try:
        marker = json.loads((ENGINE / 'removal-ready.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        marker = {}
    weights = all((MODEL / 'weights' / name).is_file() for name in spec['weights'])
    available = bool(weights and interpreter().is_file() and (SOURCE / 'inference_propainter.py').is_file()
                     and DETECTOR.is_file() and marker.get('detector_sha256') == spec['detector']['sha256']
                     and marker.get('revision') == spec['revision']
                     and marker.get('spec_sha256') == hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest())
    return {'name': 'propainter', 'label': 'ProPainter', 'description': '逐帧定位文字，结合相邻帧局部修复',
            'available': available, 'downloaded': weights, 'runtime_ready': available,
            'device': marker.get('device', ''), 'cuda_available': bool(marker.get('cuda_available', False) and nvidia_available()),
            'nvidia_available': nvidia_available(),
            'license': spec['license'], 'repository': spec['repository'], 'model_path': str(MODEL),
            'defaults': spec['defaults']}


class ModelInstaller:
    def __init__(self):
        self.lock = threading.RLock()
        self.event = threading.Event()
        self.state = {'status': 'idle', 'progress': 0, 'message': '模型按需下载', 'error': ''}

    def get(self):
        with self.lock:
            return dict(self.state)

    def busy(self):
        return self.get()['status'] in ('downloading', 'preparing')

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def check(self):
        if self.event.is_set():
            raise RemovalCancelled()

    def cancel(self):
        self.event.set()
        return self.get()

    def start(self, device):
        if device not in ('cpu', 'cuda'):
            raise ValueError('请选择 CPU 或 NVIDIA GPU 环境')
        with self.lock:
            if self.busy():
                raise ValueError('正在准备消除模型，请等待或取消')
            self.event = threading.Event()
            self.state = {'status': 'downloading', 'progress': 0, 'message': '准备 ProPainter', 'error': ''}
            threading.Thread(target=self._run, args=(device,), daemon=True, name='removal-model').start()
        return self.get()

    def download(self, url, destination, progress):
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + '.part')
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {'User-Agent': 'SubtitleStudio/3.0'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            resumed = response.status == 206
            if not resumed:
                offset = 0
            total = int(response.headers.get('Content-Length') or 0) + offset
            with partial.open('ab' if resumed else 'wb') as output:
                while True:
                    self.check()
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    output.write(block)
                    offset += len(block)
                    self.update(progress=progress, message=f'下载 {destination.name} · {offset / 1048576:.1f}'
                                + (f' / {total / 1048576:.1f} MB' if total else ' MB'))
            if total and offset != total:
                raise RuntimeError(f'{destination.name} 下载不完整，可重试续传')
        if partial.stat().st_size < 1024:
            raise RuntimeError('下载内容异常，请检查网络后重试')
        os.replace(partial, destination)

    def install(self, packages, index, device, log):
        command = [str(interpreter()), '-m', 'pip', 'install', '--only-binary=:all:', '--no-input',
                   '--disable-pip-version-check', '--timeout', '30', '--retries', '1',
                   '--index-url', index, *packages]
        with log.open('a', encoding='utf-8') as output:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                       env=environment(device), creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            try:
                while process.poll() is None:
                    if self.event.wait(.2):
                        raise RemovalCancelled()
                if process.returncode:
                    raise RuntimeError('环境安装失败：' + log.read_text(encoding='utf-8', errors='replace')[-1800:])
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

    def _run(self, device):
        try:
            ENGINE.mkdir(parents=True, exist_ok=True)
            with installation_lock():
                spec = specification()
                marker = ENGINE / 'removal-ready.json'
                marker.unlink(missing_ok=True)
                source_marker = SOURCE / 'revision.txt'
                if not source_marker.exists() or source_marker.read_text().strip() != spec['revision']:
                    archive = ENGINE / 'source.zip'
                    self.download(spec['source_url'], archive, .05)
                    with zipfile.ZipFile(archive) as bundle:
                        for item in bundle.infolist():
                            path = PurePosixPath(item.filename)
                            if path.is_absolute() or '..' in path.parts or (item.external_attr >> 16) & 0o170000 == 0o120000:
                                raise ValueError('模型源码压缩包含非法路径')
                            parts = path.parts[1:]
                            if not parts or item.is_dir():
                                continue
                            if parts[0] not in ('RAFT', 'model', 'core', 'utils', 'inference_propainter.py', 'LICENSE'):
                                continue
                            if path.suffix != '.py' and parts[-1] != 'LICENSE':
                                continue
                            target = SOURCE.joinpath(*parts)
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(bundle.read(item))
                    source_marker.write_text(spec['revision'], encoding='utf-8')
                for i, name in enumerate(spec['weights']):
                    target = MODEL / 'weights' / name
                    if not target.exists():
                        self.download(spec['weights_base'] + name, target, .15 + i * .15)
                if not DETECTOR.is_file() or hashlib.sha256(DETECTOR.read_bytes()).hexdigest() != spec['detector']['sha256']:
                    self.download(spec['detector']['url'], DETECTOR, .61)
                    if hashlib.sha256(DETECTOR.read_bytes()).hexdigest() != spec['detector']['sha256']:
                        DETECTOR.unlink(missing_ok=True)
                        raise RuntimeError('文字检测模型校验失败，请重新准备模型')
                self.check()
                self.update(status='preparing', progress=.65, message='创建独立视频修复环境…')
                if not interpreter().exists():
                    venv.EnvBuilder(with_pip=True).create(ENGINE)
                log = ENGINE / 'install.log'
                self.update(message='安装 ' + ('NVIDIA GPU' if device == 'cuda' else 'CPU') + ' 推理依赖，首次较慢…')
                torch_index = 'https://download.pytorch.org/whl/cu126' if device == 'cuda' else (
                    'https://pypi.org/simple' if os.name == 'nt' else 'https://download.pytorch.org/whl/cpu')
                self.install(spec['torch'], torch_index, device, log)
                self.install(spec['packages'], 'https://pypi.org/simple', device, log)
                self.check()
                probe = subprocess.run([str(interpreter()), '-c',
                    'import torch,torchvision,av,cv2,scipy,imageio,einops,timm,onnxruntime; print(int(torch.cuda.is_available()))'],
                    env=environment(device), capture_output=True, text=True, timeout=90, check=False)
                if probe.returncode or (device == 'cuda' and probe.stdout.strip() != '1'):
                    raise RuntimeError('推理环境自检失败：' + (probe.stderr[-1200:] or 'CUDA 不可用，可选择 CPU 后重试'))
                hashes = {name: hashlib.sha256((MODEL / 'weights' / name).read_bytes()).hexdigest() for name in spec['weights']}
                atomic_json(marker, {'revision': spec['revision'], 'device': device, 'cuda_available': device == 'cuda',
                                     'spec_sha256': hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
                                     'weights_sha256': hashes, 'detector_sha256': spec['detector']['sha256']})
                self.update(status='done', progress=1, message='ProPainter 已就绪')
        except RemovalCancelled:
            self.update(status='cancelled', message='准备已取消，已下载的模型保留')
        except Exception as error:
            logging.getLogger('studio').exception('字幕消除模型准备失败')
            self.update(status='error', message='准备失败，可重试', error=str(error))


INSTALLER = ModelInstaller()
