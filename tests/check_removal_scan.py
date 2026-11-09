"""用合成的间歇中文字幕实测 PP-OCRv5 时间扫描；不测试 ProPainter 修复质量。"""
from __future__ import annotations

import ctypes
import hashlib
import json
import sys
import tempfile
import time
from ctypes import wintypes
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from core.removal_scan import TextDetector, merge_samples


def text_frame(background=35, foreground=0xffffff, outline=False):
    width, height = 640, 360

    class Header(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('width', wintypes.LONG), ('height', wintypes.LONG),
                    ('planes', wintypes.WORD), ('bits', wintypes.WORD), ('compression', wintypes.DWORD),
                    ('image_size', wintypes.DWORD), ('xppm', wintypes.LONG), ('yppm', wintypes.LONG),
                    ('colors', wintypes.DWORD), ('important', wintypes.DWORD)]

    gdi = ctypes.WinDLL('gdi32')
    for name in ('CreateCompatibleDC', 'CreateDIBSection', 'CreateFontW', 'SelectObject'):
        getattr(gdi, name).restype = ctypes.c_void_p
    gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wintypes.DWORD]
    gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi.TextOutW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int]
    gdi.SetBkMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
    gdi.SetTextColor.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    gdi.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]
    dc = gdi.CreateCompatibleDC(None)
    header = Header(ctypes.sizeof(Header), width, -height, 1, 32, 0, width*height*4, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    bitmap = gdi.CreateDIBSection(dc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    old_bitmap = gdi.SelectObject(dc, bitmap)
    font = gdi.CreateFontW(-40, 0, 0, 0, 600, 0, 0, 0, 1, 0, 0, 4, 0, 'Microsoft YaHei')
    old_font = gdi.SelectObject(dc, font)
    try:
        image = np.ctypeslib.as_array((ctypes.c_uint8 * (width*height*4)).from_address(bits.value)).reshape(height, width, 4)
        image[:] = 35
        if isinstance(background, np.ndarray):
            image[:, :, :3] = background[:, :, ::-1]
        else:
            image[:] = background
        gdi.SetBkMode(dc, 1)
        if outline:
            gdi.SetTextColor(dc, 0)
            for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-2, -2), (2, 2), (-2, 2), (2, -2)):
                gdi.TextOutW(dc, 220+dx, 270+dy, '测试字幕', 4)
        gdi.SetTextColor(dc, foreground)
        gdi.TextOutW(dc, 220, 270, '测试字幕', 4)
        return image[:, :, :3][:, :, ::-1].copy()
    finally:
        gdi.SelectObject(dc, old_font);gdi.SelectObject(dc, old_bitmap)
        gdi.DeleteObject(font);gdi.DeleteObject(bitmap);gdi.DeleteDC(dc)


def main():
    configuration = json.loads((ROOT / 'config/removal-scan.json').read_text(encoding='utf-8'))
    model = ROOT / 'data/models/ppocrv5-det/det.onnx'
    assert hashlib.sha256(model.read_bytes()).hexdigest() == configuration['sha256']
    detector = TextDetector(model, configuration)
    text = text_frame()
    blank = np.full_like(text, 35)
    region = {'x': 0, 'y': .55, 'width': 1, 'height': .4}
    with tempfile.TemporaryDirectory(prefix='removal-scan-real-') as temporary:
        video = Path(temporary) / 'timed-subtitles.mp4'
        with av.open(str(video), 'w') as output:
            stream = output.add_stream('libx264', rate=24)
            stream.width, stream.height, stream.pix_fmt = 640, 360, 'yuv420p'
            for i in range(72):
                array = text if (12 <= i < 28 or 48 <= i < 64) else blank
                frame = av.VideoFrame.from_ndarray(array, format='rgb24')
                frame.pts, frame.time_base = i, Fraction(1, 24)
                for packet in stream.encode(frame):
                    output.mux(packet)
            for packet in stream.encode():
                output.mux(packet)
        started = time.perf_counter()
        samples, next_time = [], 0
        with av.open(str(video)) as source:
            for frame in source.decode(video=0):
                stamp = float(frame.time)
                if stamp + .0001 < next_time:
                    continue
                samples.append((stamp, detector(frame, region)))
                next_time = stamp+.25
        segments = merge_samples(samples, .25, 3)
        assert len(segments) == 2, (samples, segments)
        assert abs(segments[0]['start']-.5)<.26 and abs(segments[1]['start']-2)<.26, segments
        assert samples[0][1] is False
        report = {'model': configuration['model'], 'sha256': configuration['sha256'], 'all_passed': True,
                  'scope': '真实 ONNX 模型、合成间歇中文字幕；非真实素材准确率评测，不测试视频修复',
                  'seconds': round(time.perf_counter()-started, 3), 'samples': samples, 'segments': segments}
        folder = ROOT / 'docs/修复验证/字幕消除_2026-11-10'
        folder.mkdir(parents=True, exist_ok=True)
        (folder/'文字扫描实测.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'all_passed': True, 'segments': len(segments), 'seconds': report['seconds']}))


if __name__ == '__main__':
    main()
