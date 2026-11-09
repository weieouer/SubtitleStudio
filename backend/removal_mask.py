"""共享的文字概率图检测；扫描与修复使用同一输入规范，不导入 Torch。"""
from __future__ import annotations

import math

import av
import numpy as np


def grouped_runs(values):
    edges = np.diff(np.r_[False, np.asarray(values, dtype=bool), False].astype(np.int8))
    return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))


def text_regions(probability, configuration):
    """筛选可信文字行，返回检测分辨率下的范围；不把整块选区当作文字。"""
    binary = probability > configuration.get('threshold', .3)
    rows = binary.sum(axis=1) >= max(3, binary.shape[1] * .012)
    regions = []
    for top, bottom in grouped_runs(rows):
        height = bottom - top
        if height < 2:
            continue
        columns = np.flatnonzero(binary[top:bottom].sum(axis=0) >= 1)
        if not len(columns):
            continue
        left, right = int(columns[0]), int(columns[-1]) + 1
        if right - left < max(6, height * 1.1):
            continue
        selected = binary[top:bottom, left:right]
        if float(probability[top:bottom, left:right][selected].mean()) >= configuration.get('box_threshold', .5):
            regions.append((left, int(top), right, int(bottom)))
    return binary, regions


class TextDetector:
    def __init__(self, path, configuration):
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(path), sess_options=options, providers=['CPUExecutionProvider'])
        self.input = self.session.get_inputs()[0].name
        self.configuration = configuration

    def probability(self, rgb):
        height, width = rgb.shape[:2]
        scale = min(2, 736 / max(height, width))
        target_h, target_w = max(32, round(height * scale / 32) * 32), max(32, round(width * scale / 32) * 32)
        image = av.VideoFrame.from_ndarray(np.ascontiguousarray(rgb), format='rgb24').reformat(
            width=target_w, height=target_h).to_ndarray(format='bgr24')
        image = (image.astype(np.float32) / 255 - np.array([.485, .456, .406], dtype=np.float32)) / np.array(
            [.229, .224, .225], dtype=np.float32)
        tensor = np.ascontiguousarray(image.transpose(2, 0, 1)[None])
        return self.session.run(None, {self.input: tensor})[0][0, 0]

    def __call__(self, frame, region):
        width, height = frame.width, frame.height
        x0, y0 = math.floor(region['x'] * width), math.floor(region['y'] * height)
        x1 = min(width, math.ceil((region['x'] + region['width']) * width))
        y1 = min(height, math.ceil((region['y'] + region['height']) * height))
        rgb = frame.to_ndarray(format='rgb24')[y0:y1, x0:x1]
        return bool(text_regions(self.probability(rgb), self.configuration)[1])

    def mask(self, rgb):
        """保留概率图形状并适度外扩覆盖描边；返回原选区尺寸的二值遮罩。"""
        import cv2
        probability = self.probability(rgb)
        binary, regions = text_regions(probability, self.configuration)
        mask = np.zeros_like(binary, dtype=np.uint8)
        height, width = rgb.shape[:2]
        for left, top, right, bottom in regions:
            # DB 输出通常是收缩的文字核；按行高补回字符边缘、描边及少量阴影。
            padding = max(3, math.ceil((bottom - top) * .7))
            line = np.zeros_like(mask)
            line[top:bottom, left:right] = binary[top:bottom, left:right]
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (padding * 2 + 1, padding * 2 + 1))
            mask = np.maximum(mask, cv2.dilate(line, kernel))
        return cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST).astype(bool)
