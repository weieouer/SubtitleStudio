"""对全片的选定字幕区域做轻量文字检测，输出待复核的出现时间段。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import urllib.request
import uuid

import av
from removal_mask import TextDetector, grouped_runs  # noqa: F401 - existing scan callers import these names

from .paths import CONFIG_DIR, MODELS_DIR, RESULTS_DIR
from .removal_jobs import video_info
from .removal_models import RemovalCancelled
from .storage import atomic_json


def merge_samples(samples, interval, duration):
    segments = []
    for stamp, positive in samples:
        if not positive:
            continue
        start = max(0, stamp - interval / 2)
        end = min(duration, stamp + interval)
        if segments and start <= segments[-1]['end'] + interval * .25:
            segments[-1]['end'] = round(end, 3)
        else:
            segments.append({'id': uuid.uuid4().hex[:12], 'start': round(start, 3), 'end': round(end, 3),
                             'enabled': True, 'source': 'detected'})
    return segments


class SubtitleScanner:
    def __init__(self):
        self.lock = threading.RLock()
        self.event = threading.Event()
        self.history = {}
        self.state = {'id': '', 'status': 'idle', 'progress': 0, 'message': '', 'segments': []}

    def get(self, job_id=''):
        with self.lock:
            if job_id and job_id != self.state['id']:
                return json.loads(json.dumps(self.history.get(job_id)))
            return json.loads(json.dumps(self.state))

    def busy(self):
        return self.get()['status'] in ('downloading', 'running')

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def cancel(self, job_id=''):
        with self.lock:
            if job_id and job_id != self.state['id']:
                return self.get(job_id)
            self.event.set()
            return self.get()

    def check(self):
        if self.event.is_set():
            raise RemovalCancelled()

    def start(self, body):
        info = video_info(body.get('path', ''))
        from .removal_jobs import validate_request
        request = validate_request({**body, 'segments': None, 'preview': False, 'output_dir': str(RESULTS_DIR),
                                    'start': 0, 'end': info['duration'], 'format': 'mkv'})
        with self.lock:
            if self.busy():
                raise ValueError('已有时间段扫描任务，请先等待或取消')
            if self.state['id']:
                self.history[self.state['id']] = self.get()
                while len(self.history) > 10:
                    del self.history[next(iter(self.history))]
            self.event = threading.Event()
            self.state = {'id': uuid.uuid4().hex, 'status': 'running', 'progress': 0,
                          'message': '准备文字检测模型…', 'segments': [], 'path': request['path'], 'error': ''}
            threading.Thread(target=self._run, args=(request,), daemon=True, name='subtitle-scan').start()
        return self.get()

    def _run(self, request):
        try:
            configuration = json.loads((CONFIG_DIR / 'removal-scan.json').read_text(encoding='utf-8'))
            model = MODELS_DIR / 'ppocrv5-det' / 'det.onnx'
            model.parent.mkdir(parents=True, exist_ok=True)
            if not model.exists() or hashlib.sha256(model.read_bytes()).hexdigest() != configuration['sha256']:
                self.update(status='downloading', message='首次扫描：下载 PP-OCRv5 文字检测模型…')
                temporary = model.with_suffix('.part')
                with urllib.request.urlopen(configuration['url'], timeout=30) as response, temporary.open('wb') as output:
                    while block := response.read(256 * 1024):
                        self.check()
                        output.write(block)
                if hashlib.sha256(temporary.read_bytes()).hexdigest() != configuration['sha256']:
                    raise RuntimeError('文字检测模型校验失败，请重新扫描以重试下载')
                os.replace(temporary, model)
            self.check()
            detector = TextDetector(model, configuration)
            interval = float(configuration['sample_interval'])
            duration = request['info']['duration']
            samples, next_time = [], 0
            self.update(status='running', message='扫描全片的框选区域…', progress=.03)
            with av.open(request['path']) as container:
                stream = container.streams.video[0]
                origin = float(container.start_time or 0) / av.time_base
                for index, frame in enumerate(container.decode(stream)):
                    self.check()
                    stamp = float(frame.time)-origin if frame.time is not None else index / float(stream.average_rate or 25)
                    if stamp + .0001 < next_time:
                        continue
                    positive = detector(frame, request['region'])
                    samples.append((stamp, positive))
                    next_time = stamp + interval
                    self.update(progress=min(.99, .03+.96*stamp/duration), message=f'扫描 {stamp:.1f} / {duration:.1f} 秒 · {len(samples)} 帧采样')
            segments = merge_samples(samples, interval, duration)
            with self.lock:
                self.update(status='done', progress=1, message=f'发现 {len(segments)} 个候选时间段，请复核', segments=segments,
                            interval=interval, sampled_frames=len(samples))
                final = self.get()
            atomic_json(RESULTS_DIR / 'removal' / ('scan-' + final['id'] + '.json'), final)
        except RemovalCancelled:
            self.update(status='cancelled', message='扫描已取消')
        except Exception as error:
            logging.getLogger('studio').exception('字幕时间段扫描失败')
            self.update(status='error', message='扫描失败，可重试', error=str(error))


SCANNER = SubtitleScanner()
