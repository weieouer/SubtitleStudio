"""真实检测和修复：宽选区、描边文字、无字帧及遮罩外逐像素保护。"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from check_removal_scan import text_frame
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from removal_mask import TextDetector
from removal_runner import ProPainter, crop_geometry


def main():
    evidence = ROOT / 'docs/修复验证/字幕消除_2026-11-10'
    evidence.mkdir(parents=True, exist_ok=True)
    configuration = json.loads((ROOT / 'config/removal-scan.json').read_text(encoding='utf-8'))
    detector_path = ROOT / 'data/models/ppocrv5-det/det.onnx'
    detector = TextDetector(detector_path, configuration)
    y, x = np.indices((360, 640))
    blank = np.stack([35+x//12, 50+y//12, 45+x//20], axis=-1).astype(np.uint8)
    white = text_frame(background=blank)
    outlined = text_frame(background=blank, foreground=0x00ffff, outline=True)
    roi = {'x': .1, 'y': .64, 'width': .8, 'height': .3}
    geometry = crop_geometry(640, 360, roi, 384)
    x0, y0, x1, y1 = geometry[0]
    arrays = [blank, white, white, outlined, outlined, blank]
    masks = [detector.mask(array[y0:y1, x0:x1]) for array in arrays]
    records = []
    for name, array, mask in zip(('blank', 'white', 'white', 'outlined', 'outlined', 'blank'), arrays, masks):
        truth = np.max(np.abs(array.astype(np.int16)-blank.astype(np.int16)), axis=2)[y0:y1, x0:x1] > 5
        recall = float(mask[truth].mean()) if np.any(truth) else None
        fraction = float(mask.mean())
        if recall is not None:
            assert recall == 1, (name, recall)
            assert fraction < .6, (name, fraction)
        else:
            assert not mask.any(), '无字帧不得生成修复遮罩'
        records.append({'style': name, 'text_and_outline_coverage': recall, 'roi_mask_fraction': fraction})
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='removal-quality-') as temporary:
        request = {'device': 'cpu', 'source': str(ROOT/'data/engines/removal/source'),
                   'model_dir': str(ROOT/'data/models/propainter'), 'work_dir': temporary,
                   'detector_path': str(detector_path), 'detector_config': configuration}
        processor = ProPainter(request)
        repaired = processor.process(arrays, [True]*len(arrays), geometry)
        for index, (before, after, mask) in enumerate(zip(arrays, repaired, masks)):
            full_mask = np.zeros(before.shape[:2], dtype=bool)
            full_mask[y0:y1, x0:x1] = mask
            np.testing.assert_array_equal(before[~full_mask], after[~full_mask])
            if not full_mask.any():
                np.testing.assert_array_equal(before, after)
            else:
                truth = np.max(np.abs(before.astype(np.int16)-blank.astype(np.int16)), axis=2) > 5
                prior = float(np.abs(before[truth].astype(float)-blank[truth]).mean())
                remaining = float(np.abs(after[truth].astype(float)-blank[truth]).mean())
                assert remaining < prior * .4, (index, prior, remaining)
                records[index]['residual_difference_ratio'] = remaining / prior
        strip = np.concatenate([arrays[3], repaired[3], blank], axis=1)
        Image.fromarray(strip).save(evidence/'描边字幕修复对比.png')
    record = {'all_passed': True, 'device': 'cpu', 'frames': len(arrays), 'seconds': round(time.perf_counter()-started, 3),
              'outside_mask_identical_before_encoding': True, 'frames_detail': records,
              'scope': '真实 PP-OCRv5 + ProPainter，合成渐变背景白字/黄字黑描边/无字帧；非真人长片、GPU或通用质量验收'}
    (evidence/'精细遮罩实测.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(record, ensure_ascii=False))


if __name__ == '__main__':
    main()
