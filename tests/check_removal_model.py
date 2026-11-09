"""真实 ProPainter CPU 短片验证；使用独立验证环境，不更改用户选择的引擎环境。"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from check_removal_scan import text_frame

ROOT = Path(__file__).resolve().parents[1]


def main():
    python = ROOT / 'data/engines/removal-validation/Scripts/python.exe'
    assert (python.parent.parent/'removal-ready.json').is_file(), '先准备独立 CPU 验证环境'
    evidence = ROOT / 'docs/修复验证/字幕消除_2026-11-10'
    evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='removal-real-model-') as temporary:
        folder = Path(temporary)
        source = folder / 'source.mp4'
        image = text_frame()
        with av.open(str(source), 'w') as output:
            stream = output.add_stream('libx264', rate=4)
            stream.width, stream.height, stream.pix_fmt = 640, 360, 'yuv420p'
            for i in range(4):
                frame = av.VideoFrame.from_ndarray(image, format='rgb24')
                frame.pts, frame.time_base = i, Fraction(1, 4)
                for packet in stream.encode(frame):output.mux(packet)
            for packet in stream.encode():output.mux(packet)
        request = {'path': str(source), 'region': {'x': .33, 'y': .74, 'width': .34, 'height': .18},
                   'detector_path': str(ROOT / 'data/models/ppocrv5-det/det.onnx'),
                   'detector_config': json.loads((ROOT / 'config/removal-scan.json').read_text(encoding='utf-8')),
                   'start': 0, 'end': 1, 'preview': True, 'max_edge': 384, 'device': 'cpu',
                   'source': str(ROOT / 'data/engines/removal/source'), 'model_dir': str(ROOT / 'data/models/propainter'),
                   'work_dir': str(folder), 'repaired': str(folder/'output.mp4'), 'result_file': str(folder/'result.json'),
                   'progress_file': str(folder/'progress.json')}
        job = folder / 'request.json'
        job.write_text(json.dumps(request), encoding='utf-8')
        environment = {k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME')}
        environment.update(CUDA_VISIBLE_DEVICES='',PYTHONUTF8='1',PYTHONUNBUFFERED='1')
        started = time.perf_counter()
        with (evidence/'修复模型实测日志.txt').open('w',encoding='utf-8') as log:
            result = subprocess.run([str(python),str(ROOT/'backend/removal_runner.py'),str(job)],
                cwd=ROOT,env=environment,stdout=log,stderr=subprocess.STDOUT,timeout=240,check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if result.returncode:
            raise RuntimeError((evidence/'修复模型实测日志.txt').read_text(encoding='utf-8')[-3000:])
        with av.open(str(source)) as original,av.open(request['repaired']) as repaired:
            first=[f.to_ndarray(format='rgb24') for f in original.decode(video=0)]
            second=[f.to_ndarray(format='rgb24') for f in repaired.decode(video=0)]
        assert len(first)==len(second)==4
        before=float(np.mean(first[0][270:322,220:390]>160))
        after=float(np.mean(second[0][270:322,220:390]>160))
        outside=float(np.mean(np.abs(first[0][:180].astype(float)-second[0][:180].astype(float))))
        assert after<before*.8,(before,after)
        assert outside<8,outside
        record={'all_passed':True,'device':'cpu','frames':4,'source_size':[640,360],
                'processing_max_edge':384,'seconds':round(time.perf_counter()-started,3),
                'bright_text_fraction_before':before,'bright_text_fraction_after':after,'outside_mean_difference':outside,
                'scope':'真实 ProPainter 权重与推理，1秒合成静态背景中文字幕；不代表真人长片、复杂背景、GPU速度或普遍修复质量验收'}
        (evidence/'修复模型实测.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(record,ensure_ascii=False))


if __name__=='__main__':
    main()
