"""字幕消除：独立任务、输入约束、取消、持久化及原音轨封装。"""
from __future__ import annotations

import copy
import hashlib
import heapq
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from fractions import Fraction
from pathlib import Path

import av

from . import media, validation
from .paths import OUTPUTS_DIR, PROJECTS_DIR, RESULTS_DIR
from .removal_models import (
    DETECTOR,
    INSTALLER,
    MODEL,
    SOURCE,
    RemovalCancelled,
    environment,
    interpreter,
    model_status,
    specification,
)
from .storage import atomic_json


def video_info(path):
    if not isinstance(path, str) or not Path(path).is_file():
        raise FileNotFoundError('请选择存在的视频文件')
    info = media.probe(path)
    if info.get('error') or not info['width'] or not info['height'] or info['duration'] <= 0:
        raise ValueError(info.get('error') or '素材中没有可处理的视频轨道')
    with av.open(path) as container:
        stream = container.streams.video[0]
        context = stream.codec_context
        if (context.format and any(component.bits > 8 for component in context.format.components)) or context.color_trc in (16, 18):
            raise ValueError('暂不支持 HDR 或高于 8 位的视频，避免修复时损失色彩；请使用 SDR 8 位副本')
        rotation = int(float(stream.metadata.get('rotate', '0')))
        for side in getattr(stream, 'side_data', []):
            if 'DISPLAYMATRIX' in str(side.type):
                rotation = int(getattr(side, 'rotation', rotation))
        if rotation % 360:
            raise ValueError('视频带旋转标记，请先转正后处理，避免框选位置错位')
    return info


def validate_request(body):
    validation.mapping(body, '字幕消除请求')
    if not isinstance(body.get('path'), str) or not body['path'].strip():
        raise ValueError('请选择视频文件')
    path = str(Path(body.get('path', '')).resolve())
    info = video_info(path)
    roi = validation.mapping(body.get('region'), '框选区域')
    roi = {key: validation.number(roi.get(key), key, 0, 1) for key in ('x', 'y', 'width', 'height')}
    if roi['x'] + roi['width'] > 1 + 1e-8 or roi['y'] + roi['height'] > 1 + 1e-8:
        raise ValueError('框选区域超出视频画面')
    if roi['width'] * info['width'] < 2 or roi['height'] * info['height'] < 2:
        raise ValueError('请先框选至少 2×2 像素的字幕区域')
    start = validation.number(body.get('start', 0), '开始时间', 0, info['duration'])
    end = validation.number(body.get('end', info['duration']), '结束时间', 0, info['duration'])
    if end - start < .04:
        raise ValueError('处理范围至少需要 0.04 秒')
    segments = validate_segments(body.get('segments'), info['duration'])
    preview = validation.boolean(body.get('preview', False), 'preview')
    if preview and end - start > 5.001:
        raise ValueError('短片预览最多处理 5 秒')
    edge = body.get('max_edge', 512)
    if isinstance(edge, bool) or edge not in (384, 512, 640):
        raise ValueError('处理精度仅支持 384、512 或 640')
    if body.get('model', 'propainter') != 'propainter':
        raise ValueError('未知字幕消除模型')
    output_format = 'mp4' if preview else body.get('format', 'mkv')
    if output_format not in ('mkv', 'mp4'):
        raise ValueError('输出格式仅支持 MKV 或 MP4')
    if not preview and output_format == 'mp4' and any(t['codec'] not in ('aac', 'mp3', 'alac') for t in info['audio_tracks']):
        raise ValueError('此视频的音轨不兼容 MP4，请选择 MKV 保留原音轨')
    output = OUTPUTS_DIR / 'removal' / 'previews' if preview else Path(body.get('output_dir', '')).resolve()
    if preview:
        output.mkdir(parents=True, exist_ok=True)
    elif not body.get('output_dir') or not output.is_dir():
        raise ValueError('请先选择输出文件夹')
    request_id = body.get('request_id') or uuid.uuid4().hex
    if not isinstance(request_id, str) or not re.fullmatch('[a-f0-9]{32}', request_id):
        raise ValueError('任务标识无效')
    device = body.get('device', 'auto')
    if device not in ('auto', 'cpu', 'cuda'):
        raise ValueError('未知推理设备')
    return {'id': request_id, 'path': path, 'region': roi, 'start': start, 'end': end,
            'preview': preview, 'max_edge': edge, 'format': output_format,
            'output_dir': str(output), 'model': 'propainter', 'device': device, 'info': info, 'segments': segments}


def validate_segments(value, duration, require_enabled=True):
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 10000:
        raise ValueError('字幕时间段必须是列表，最多 10000 段')
    result = []
    for item in value:
        validation.mapping(item, '字幕时间段')
        start = validation.number(item.get('start'), '片段开始', 0, duration)
        end = validation.number(item.get('end'), '片段结束', 0, duration)
        enabled = validation.boolean(item.get('enabled', True), '片段启用')
        if end - start < .04:
            raise ValueError('字幕片段至少需要 0.04 秒')
        result.append({'id': str(item.get('id') or uuid.uuid4().hex[:12])[:64], 'start': start, 'end': end,
                       'enabled': enabled, 'source': 'manual' if item.get('source') == 'manual' else 'detected'})
    if require_enabled and not any(item['enabled'] for item in result):
        raise ValueError('没有启用的字幕时间段，请添加或勾选片段')
    return sorted(result, key=lambda item: (item['start'], item['end']))


def project_file(path):
    source = Path(path).resolve()
    stat = source.stat()
    key = hashlib.sha256(f'{source}|{stat.st_size}|{stat.st_mtime_ns}'.encode()).hexdigest()[:24]
    return PROJECTS_DIR / 'removal' / (key + '.json')


def mux_audio(source, repaired, target, offset=0, clip_end=None, cancel_event=None):
    """时间戳归零后交错封装；全片输出复制全部原音轨，不解码音频。"""
    with av.open(str(source)) as original, av.open(str(repaired)) as video, av.open(str(target), 'w', format='matroska' if target.suffix == '.mkv' else 'mp4') as output:
        output.metadata.update(original.metadata)
        new_video = output.add_stream_from_template(video.streams.video[0])
        audio_map = {stream.index: output.add_stream_from_template(stream) for stream in original.streams.audio}
        for stream in original.streams.audio:
            audio_map[stream.index].metadata.update(stream.metadata)

        def video_packets():
            for packet in video.demux(video.streams.video[0]):
                if packet.size:
                    packet.stream = new_video
                    yield packet

        def audio_packets():
            if not audio_map:
                return
            for packet in original.demux(list(original.streams.audio)):
                if not packet.size:
                    continue
                stamp = float((packet.pts if packet.pts is not None else packet.dts) * packet.time_base)
                if clip_end is not None and (stamp < offset or stamp >= clip_end):
                    continue
                shift = round(Fraction(str(offset)) / packet.time_base)
                packet.pts = None if packet.pts is None else packet.pts - shift
                packet.dts = None if packet.dts is None else packet.dts - shift
                packet.stream = audio_map[packet.stream.index]
                yield packet

        for packet in heapq.merge(video_packets(), audio_packets(), key=lambda p: float((p.dts if p.dts is not None else p.pts or 0) * p.time_base)):
            if cancel_event is not None and cancel_event.is_set():
                raise RemovalCancelled()
            output.mux(packet)


class RemovalJobs:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = {}
        self.events = {}

    def busy(self):
        with self.lock:
            return any(j['status'] in ('queued', 'running') for j in self.jobs.values())

    def get(self, job_id):
        if not re.fullmatch('[a-f0-9]{32}', str(job_id)):
            return None
        with self.lock:
            if job_id in self.jobs:
                result = copy.deepcopy(self.jobs[job_id])
                result['elapsed'] = round(result.get('finished', time.time()) - result['started'], 1)
                return result
        try:
            result = json.loads((RESULTS_DIR / 'removal' / job_id / 'status.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        if result['status'] in ('queued', 'running'):
            result.update(status='interrupted', message='服务已重启，任务未确认完成，可重新处理')
        return result

    def cancel(self, job_id):
        with self.lock:
            event = self.events.get(job_id)
            if event is not None:
                event.set()
        return self.get(job_id)

    def update(self, job_id, **values):
        with self.lock:
            if values.get('status') in ('done', 'error', 'cancelled'):
                values['finished'] = time.time()
            self.jobs[job_id].update(values)
            snapshot = copy.deepcopy(self.jobs[job_id])
        atomic_json(RESULTS_DIR / 'removal' / job_id / 'status.json', snapshot)

    def submit(self, body):
        request = validate_request(body)
        job_id = request['id']
        with self.lock:
            if job_id in self.jobs:
                if self.jobs[job_id]['request'] != request:
                    raise ValueError('同一任务标识不能提交不同参数')
                return self.get(job_id)
            if self.busy() or INSTALLER.busy():
                raise ValueError('请先完成或取消当前视频修复任务')
            model = model_status()
            if not model['available']:
                raise ValueError('请先下载并准备 ProPainter 模型')
            if request['device'] == 'cuda' and not model['cuda_available']:
                raise ValueError('请先准备 NVIDIA GPU 环境，或选择 CPU')
            self.events[job_id] = threading.Event()
            self.jobs[job_id] = {'id': job_id, 'request': request, 'status': 'queued', 'progress': 0,
                                 'stage': 'queued', 'message': '等待处理', 'error': '', 'output_path': '', 'started': time.time()}
            threading.Thread(target=self._run, args=(request, model), daemon=True, name='removal-job').start()
        return self.get(job_id)

    def _run(self, request, model):
        job_id = request['id']
        folder = RESULTS_DIR / 'removal' / job_id
        folder.mkdir(parents=True, exist_ok=True)
        event = self.events[job_id]
        target = None
        temp = None
        try:
            stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', Path(request['path']).stem)[:100]
            for i in range(1, 10000):
                candidate = Path(request['output_dir']) / (stem + ('_消除预览' if request['preview'] else '_无字幕') + (f'_{i}' if i > 1 else '') + '.' + request['format'])
                try:
                    with candidate.open('xb'):
                        pass
                    target = candidate
                    break
                except FileExistsError:
                    continue
            if target is None:
                raise RuntimeError('无法分配输出文件名')
            repaired = folder / 'repaired.mp4'
            progress = folder / 'progress.json'
            result = folder / 'result.json'
            device = 'cpu' if request['device'] == 'cpu' else ('cuda' if model.get('cuda_available') else 'cpu')
            worker_request = {**request, 'source': str(SOURCE), 'model_dir': str(MODEL),
                              'detector_path': str(DETECTOR), 'detector_config': specification()['detector'],
                              'device': device, 'repaired': str(repaired), 'progress_file': str(progress),
                              'result_file': str(result), 'work_dir': str(folder)}
            source = folder / 'request.json'
            atomic_json(source, worker_request)
            self.update(job_id, status='running', stage='loading', message='加载视频修复模型…')
            runner = Path(__file__).resolve().parents[1] / 'removal_runner.py'
            from .asr import clear_model_cache
            clear_model_cache()
            with (folder / 'engine.log').open('w', encoding='utf-8') as log:
                process = subprocess.Popen([str(interpreter()), str(runner), str(source)],
                    stdout=log, stderr=subprocess.STDOUT, env=environment(device),
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                try:
                    while process.poll() is None:
                        if event.wait(.3):
                            raise RemovalCancelled()
                        try:
                            values = json.loads(progress.read_text(encoding='utf-8'))
                            self.update(job_id, **{k: values[k] for k in ('progress', 'stage', 'message') if k in values})
                        except (OSError, ValueError):
                            pass
                    if process.returncode or not result.exists():
                        raise RuntimeError((folder / 'engine.log').read_text(encoding='utf-8', errors='replace')[-2000:] or '视频修复进程异常退出，请查看任务日志')
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill(); process.wait(timeout=5)
            self.update(job_id, stage='muxing', progress=.95, message='封装视频与原音轨…')
            report = json.loads(result.read_text(encoding='utf-8'))
            temp = target.with_name('.' + target.stem + '-' + job_id + target.suffix)
            if request['preview']:
                os.replace(repaired, temp)
            else:
                mux_audio(request['path'], repaired, temp, offset=report.get('source_origin', 0), cancel_event=event)
            if event.is_set():
                raise RemovalCancelled()
            os.replace(temp, target)
            message = '短片预览已生成' if request['preview'] else '字幕消除完成'
            if not report.get('repaired_frames'):
                message = '未检测到可修复文字，请检查选区与时间段'
            self.update(job_id, status='done', progress=1, stage='done', message=message,
                        output_path=str(target), report=report)
            target = None
        except RemovalCancelled:
            self.update(job_id, status='cancelled', stage='cancelled', message='处理已取消')
        except Exception as error:
            logging.getLogger('studio').exception('字幕消除任务失败 %s', job_id)
            self.update(job_id, status='error', stage='error', error=str(error), message='处理失败，原片保留')
        finally:
            if target is not None:
                target.unlink(missing_ok=True)
            if temp is not None:
                temp.unlink(missing_ok=True)


REMOVAL_JOBS = RemovalJobs()
