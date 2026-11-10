"""在视频修复独立环境中运行；有界帧窗口，不向主环境导入 Torch。"""
from __future__ import annotations

import gc
import json
import math
import os
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from removal_mask import TextDetector


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


# RAFT 的相关金字塔最小时只剩 1 像素，采样时除以 0 得到 NaN；
# 全部为 NaN 的偏移再进入 deform_conv2d 会触发访问冲突（整个任务直接崩溃）。
# 因此处理尺寸的每条边都不能低于 128，必要时先在原片内扩边，画面本身不足时再放大。
REPAIR_MIN_EDGE = 128


def _expand_span(lo, hi, needed, limit):
    """在 [0, limit] 内把区间扩到 needed，尽量保持原中心；画面不够时取满整条边。"""
    if hi - lo >= needed:
        return lo, hi
    if limit <= needed:
        return 0, limit
    extra = needed - (hi - lo)
    lo = max(0, lo - extra // 2)
    hi = min(limit, lo + needed)
    return max(0, hi - needed), hi


def crop_geometry(width, height, region, max_edge):
    x0, y0 = math.floor(region['x'] * width), math.floor(region['y'] * height)
    x1 = min(width, math.ceil((region['x'] + region['width']) * width))
    y1 = min(height, math.ceil((region['y'] + region['height']) * height))
    margin = max(32, (y1 - y0) * 2)
    crop = [max(0, x0 - margin), max(0, y0 - margin), min(width, x1 + margin), min(height, y1 + margin)]
    scale = min(1, max_edge / max(crop[2] - crop[0], crop[3] - crop[1]))
    needed = math.ceil(REPAIR_MIN_EDGE / scale)
    crop[0], crop[2] = _expand_span(crop[0], crop[2], needed, width)
    crop[1], crop[3] = _expand_span(crop[1], crop[3], needed, height)
    cw, ch = crop[2] - crop[0], crop[3] - crop[1]
    scale = min(1, max_edge / max(cw, ch))
    size = (max(REPAIR_MIN_EDGE, int(cw * scale) // 8 * 8),
            max(REPAIR_MIN_EDGE, int(ch * scale) // 8 * 8))
    return (x0, y0, x1, y1), tuple(crop), size


def active_at(request, stamp):
    if not request['start'] <= stamp < request['end']:
        return False
    segments = request.get('segments')
    return segments is None or any(s.get('enabled', True) and s['start'] <= stamp < s['end'] for s in segments)


def scene_ranges(arrays):
    """明显切镜头处分开修复，避免把上一镜头的背景传播到下一镜头。"""
    start, previous = 0, None
    for index, array in enumerate(arrays):
        thumb = av.VideoFrame.from_ndarray(array, format='rgb24').reformat(width=64, height=36).to_ndarray(format='rgb24')
        histogram = np.concatenate([np.histogram(thumb[:, :, channel], bins=16, range=(0, 256))[0] for channel in range(3)])
        histogram = histogram / histogram.sum()
        if previous is not None:
            difference = np.abs(thumb.astype(np.int16) - previous[0]).mean()
            distance = np.abs(histogram - previous[1]).sum() / 2
            if difference > 35 and distance > .35:
                yield start, index
                start = index
        previous = thumb.astype(np.int16), histogram
    if arrays:
        yield start, len(arrays)


def composite(original, repaired, mask):
    """仅合成遮罩内像素；遮罩外在编码前逐像素保持原帧。"""
    alpha = mask.astype(np.float32)
    if not np.any(alpha):
        return original.copy()
    # 羽化只发生在遮罩内部，不能把修复结果扩散到受保护的背景。
    padded = np.pad(alpha, 1, mode='edge')
    blurred = sum(padded[y:y+alpha.shape[0], x:x+alpha.shape[1]] * weight
                  for y, x, weight in ((0, 0, 1), (0, 1, 2), (0, 2, 1), (1, 0, 2),
                                       (1, 1, 4), (1, 2, 2), (2, 0, 1), (2, 1, 2), (2, 2, 1))) / 16
    alpha *= blurred
    result = original.copy()
    selected = mask.astype(bool)
    blend = alpha[selected, None]
    result[selected] = np.rint(original[selected] * (1 - blend) + repaired[selected] * blend).astype(np.uint8)
    return result


class ProPainter:
    def __init__(self, request):
        import cv2
        import imageio
        import torch
        from PIL import Image
        self.Image, self.torch = Image, torch
        self.request = request
        self.detector = TextDetector(request['detector_path'], request['detector_config'])
        if request['device'] == 'cuda' and not torch.cuda.is_available():
            raise RuntimeError('NVIDIA GPU 环境不可用，请选择 CPU 或重新准备环境')
        # 上游 core/ 是命名空间目录；移除工作脚本目录，避免被本项目 core 包遮蔽。
        backend = Path(__file__).resolve().parent
        sys.path[:] = [request['source'], *[entry for entry in sys.path
                       if entry and Path(entry).resolve() not in (backend, Path(request['source']).resolve())]]
        for name in tuple(sys.modules):
            module = sys.modules[name]
            filename = getattr(module, '__file__', None)
            if (name == 'core' or name.startswith('core.')) and filename and Path(filename).resolve().is_relative_to(backend):
                del sys.modules[name]
        os.chdir(request['model_dir'])
        # 上游只使用这些视频写入作演示；实际输出由本适配器保留 PTS 逐帧编码。
        imageio.mimwrite = lambda *args, **kwargs: None

        def unicode_imread(path, flags=cv2.IMREAD_COLOR):
            return cv2.imdecode(np.fromfile(path, dtype=np.uint8), flags)

        def unicode_imwrite(path, image, params=None):
            success, encoded = cv2.imencode(Path(path).suffix, image, params or [])
            if success:
                encoded.tofile(path)
            return success

        cv2.imread, cv2.imwrite = unicode_imread, unicode_imwrite
        # 每个任务只加载一次三组权重；不同窗口共享网络，不共享帧状态。
        self.models = {}
        script_path = Path(request['source']) / 'inference_propainter.py'
        script = script_path.read_text(encoding='utf-8')
        for original, replacement in (
            ('fix_raft = RAFT_bi(ckpt_path, device)', "fix_raft = _cached_model('raft', lambda: RAFT_bi(ckpt_path, device))"),
            ('fix_flow_complete = RecurrentFlowCompleteNet(ckpt_path)', "fix_flow_complete = _cached_model('flow', lambda: RecurrentFlowCompleteNet(ckpt_path))"),
            ('model = InpaintGenerator(model_path=ckpt_path).to(device)', "model = _cached_model('paint', lambda: InpaintGenerator(model_path=ckpt_path).to(device))"),
        ):
            if script.count(original) != 1:
                raise RuntimeError('ProPainter 源码与固定适配版本不一致，请重新准备模型')
            script = script.replace(original, replacement)
        self.code = compile(script, str(script_path), 'exec')

    def cached_model(self, name, factory):
        if name not in self.models:
            model = factory()
            if name in ('raft', 'flow'):
                # 光流出现 NaN 时 deform_conv2d 会越界写入并让整个进程崩溃；
                # 在此显式失败，让任务面板给出可读错误而不是“处理失败”后无日志。
                original = model.forward

                def checked(*args, **kwargs):
                    result = original(*args, **kwargs)
                    values = [value for value in (result if isinstance(result, tuple) else (result,))
                              if isinstance(value, self.torch.Tensor)]
                    if not all(self.torch.isfinite(value).all() for value in values):
                        raise RuntimeError('光流计算出现无效数值，请缩小框选区域后重试；必要时改用 GPU')
                    return result

                model.forward = checked
            self.models[name] = model
        return self.models[name]

    def process(self, arrays, active, geometry):
        roi, _crop, _size = geometry
        x0, y0, x1, y1 = roi
        masks = []
        for array, enabled in zip(arrays, active):
            mask = self.detector.mask(array[y0:y1, x0:x1]) if enabled else np.zeros((y1-y0, x1-x0), dtype=bool)
            masks.append(mask)
        output = []
        for start, end in scene_ranges(arrays):
            if any(np.any(mask) for mask in masks[start:end]):
                output.extend(self.process_scene(arrays[start:end], masks[start:end], geometry))
            else:
                output.extend(array.copy() for array in arrays[start:end])
        return output

    def process_scene(self, arrays, text_masks, geometry):
        roi, crop, size = geometry
        x0, y0, x1, y1 = roi
        cx0, cy0, cx1, cy1 = crop
        with tempfile.TemporaryDirectory(prefix='window-', dir=self.request['work_dir']) as temporary:
            folder = Path(temporary)
            frames, masks, results = folder / 'frames', folder / 'masks', folder / 'results'
            frames.mkdir(); masks.mkdir()
            for index, (array, text_mask) in enumerate(zip(arrays, text_masks)):
                self.Image.fromarray(array[cy0:cy1, cx0:cx1]).resize(size).save(frames / f'{index:04}.png')
                mask = np.zeros((cy1-cy0, cx1-cx0), dtype=np.uint8)
                mask[y0-cy0:y1-cy0, x0-cx0:x1-cx0] = text_mask.astype(np.uint8) * 255
                # 缩小遮罩用 BOX 保留细小笔画，避免最近邻采样把细线漏掉。
                scaled = np.asarray(self.Image.fromarray(mask).resize(size, self.Image.Resampling.BOX))
                self.Image.fromarray((scaled > 0).astype(np.uint8) * 255).save(masks / f'{index:04}.png')
            # 光流至少需要两帧；仅余一帧时复制上下文，输出仍只写一次。
            if len(arrays) == 1:
                (frames / '0001.png').write_bytes((frames / '0000.png').read_bytes())
                (masks / '0001.png').write_bytes((masks / '0000.png').read_bytes())
            arguments = [str(Path(self.request['source']) / 'inference_propainter.py'),
                         '--video', str(frames), '--mask', str(masks), '--output', str(results),
                         '--save_frames', '--mask_dilation', '0', '--subvideo_length', '20', '--neighbor_length', '6', '--ref_stride', '5']
            if self.request['device'] == 'cuda':
                arguments.append('--fp16')
            previous = sys.argv
            try:
                sys.argv = arguments
                namespace = {'__name__': '__main__', '__file__': arguments[0], '_cached_model': self.cached_model}
                exec(self.code, namespace)  # noqa: S102 - fixed upstream revision, never user-provided code
                del namespace
            finally:
                sys.argv = previous
            output = []
            for index, (array, text_mask) in enumerate(zip(arrays, text_masks)):
                result = array.copy()
                if np.any(text_mask):
                    with self.Image.open(results / 'frames' / 'frames' / f'{index:04}.png') as image:
                        repaired = np.asarray(image.convert('RGB').resize((cx1-cx0, cy1-cy0)))
                    result[y0:y1, x0:x1] = composite(array[y0:y1, x0:x1], repaired[y0-cy0:y1-cy0, x0-cx0:x1-cx0], text_mask)
                output.append(result)
            gc.collect()
            if self.torch.cuda.is_available():
                self.torch.cuda.empty_cache()
            return output


def run(request, processor_factory=ProPainter):
    from collections import deque
    request = dict(request)
    for key in ('work_dir', 'repaired', 'progress_file', 'result_file', 'source', 'model_dir', 'path'):
        request[key] = str(Path(request[key]).resolve())
    progress = lambda **values: write_json(request['progress_file'], values)
    processor = None
    written, repaired_count, unchanged_count = 0, 0, 0
    first_time = None
    window, overlap, skip = 20, 4, 0
    with av.open(request['path']) as original, av.open(request['repaired'], 'w', format='mp4') as output:
        stream = original.streams.video[0]
        rate = stream.average_rate or stream.base_rate or Fraction(25)
        time_base = stream.time_base or Fraction(1, 90000)
        source_origin = float(original.start_time or 0) / av.time_base
        encoder = output.add_stream('libx264', rate=rate)
        encoder.width, encoder.height = stream.width, stream.height
        encoder.pix_fmt = 'yuv420p' if stream.width % 2 == 0 and stream.height % 2 == 0 else 'yuv444p'
        encoder.time_base = time_base
        encoder.codec_context.time_base = time_base
        if stream.sample_aspect_ratio:
            encoder.sample_aspect_ratio = stream.sample_aspect_ratio
        encoder.options = {'crf': '18', 'preset': 'medium'}
        if request['preview']:
            original.seek(int((request['start']+source_origin) / time_base), stream=stream, backward=True)

        def decoded():
            for index, frame in enumerate(original.decode(stream)):
                stamp = float(frame.pts * time_base)-source_origin if frame.pts is not None else index / float(rate)
                if request['preview'] and stamp < request['start']:
                    continue
                if request['preview'] and stamp >= request['end']:
                    break
                yield frame, stamp

        frames = iter(decoded())
        pending = deque()
        finished = False
        geometry = crop_geometry(stream.width, stream.height, request['region'], request['max_edge'])
        while pending or not finished:
            while len(pending) < window and not finished:
                try:
                    pending.append(next(frames))
                except StopIteration:
                    finished = True
            if len(pending) <= skip:
                break
            items = list(pending)
            active = [active_at(request, stamp) for _, stamp in items]
            arrays = [frame.to_ndarray(format='rgb24') for frame, _ in items]
            if any(active):
                if processor is None:
                    progress(progress=.02, stage='loading', message='加载 ProPainter 网络与权重…')
                    processor = processor_factory(request)
                progress(progress=.05 + .85 * max(0, min(1, (items[0][1]-request['start']) / max(.001, request['end']-request['start']))),
                         stage='repairing', message=f'修复字幕区域 · 已输出 {written} 帧')
                results = processor.process(arrays, active, geometry)
            else:
                results = arrays
            stop = len(items) if finished else len(items) - overlap
            for index in range(skip, stop):
                _frame, stamp = items[index]
                if first_time is None:
                    first_time = stamp
                result = av.VideoFrame.from_ndarray(results[index], format='rgb24')
                # 整片保留原 PTS；预览从实际第一帧归零。
                offset = first_time if request['preview'] else 0
                result.pts = round((stamp-offset) / time_base)
                result.time_base = time_base
                for packet in encoder.encode(result):
                    output.mux(packet)
                written += 1
                changed = bool(np.any(results[index] != arrays[index]))
                repaired_count += int(changed)
                unchanged_count += int(not changed)
            if finished:
                break
            pending = deque(items[-overlap*2:])
            skip = overlap
        for packet in encoder.encode():
            output.mux(packet)
    if not written:
        raise RuntimeError('处理范围内没有可解码的视频帧')
    write_json(request['result_file'], {'frames': written, 'repaired_frames': repaired_count,
               'preview': request['preview'], 'origin_start': first_time, 'max_edge': request['max_edge'],
               'window_frames': window, 'overlap_frames': overlap, 'source_origin': source_origin,
               'unchanged_frames_before_encoding': unchanged_count, 'mask_mode': 'per-frame-text'})
    progress(progress=.93, stage='encoding', message=f'视频编码完成 · {written} 帧')


if __name__ == '__main__':
    request_file = Path(sys.argv[1]).resolve()
    with request_file.open(encoding='utf-8') as source:
        run(json.load(source))
