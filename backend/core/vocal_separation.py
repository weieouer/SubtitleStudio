"""可选的人声分离；只供疑难字幕复查，不改变原音轨或时间基准。"""
from pathlib import Path
import wave

import numpy as np


MODEL_NAME = 'hdemucs-vocals'
MODEL_FILE = 'hdemucs_high_trained.pt'
MODEL_SIZE = 334697255
MODEL_SHA256 = 'a004b2790d73ffeaa535db458a1a79b539dfdbafbccc31f275d07e632ebd7816'
MODEL_URL = 'https://download.pytorch.org/torchaudio/models/' + MODEL_FILE
MAX_CLIP_SECONDS = 12.


def read_wave(path):
    with wave.open(str(path), 'rb') as source:
        if source.getsampwidth() != 2 or source.getnchannels() != 1:
            raise ValueError('人声复查需要单声道 PCM16 音频')
        return np.frombuffer(source.readframes(source.getnframes()), dtype='<i2').astype(np.float32)/32768, source.getframerate()


def write_wave(path, audio, rate=16000):
    with wave.open(str(path), 'wb') as target:
        target.setnchannels(1); target.setsampwidth(2); target.setframerate(rate)
        target.writeframes((np.clip(audio, -1, 1)*32767).astype('<i2').tobytes())


class VocalSeparator:
    """延迟导入 Torch；权重只从本地加载，推理过程不联网。"""
    def __init__(self, directory, device='cpu'):
        import torch
        from third_party.hdemucs import hdemucs_high
        self.device = device
        self.model = hdemucs_high(sources=['drums', 'bass', 'other', 'vocals'])
        weights = torch.load(Path(directory)/MODEL_FILE, map_location='cpu', weights_only=True)
        self.model.load_state_dict(weights)
        self.model.eval().to(device)

    def separate(self, audio, rate=16000):
        import torch
        from scipy.signal import resample_poly
        from math import gcd
        if not 0 < len(audio) <= round(MAX_CLIP_SECONDS*rate):
            raise ValueError('人声复查音频超出局部窗口限制')
        with torch.inference_mode():
            divisor = gcd(rate, 44100)
            resampled = resample_poly(np.asarray(audio, dtype=np.float32), 44100//divisor, rate//divisor)
            mono = torch.from_numpy(resampled.copy()).to(self.device)
            stereo = mono.repeat(2, 1)
            # 只做幅度归一化，不做静音删除或速度变换，分离后保持逐样本时间基准。
            mean = stereo.mean(); scale = stereo.std().clamp_min(1e-6)
            signal = (stereo-mean)/scale
            length = signal.shape[-1]
            output = torch.zeros(length, device=self.device)
            weights = torch.zeros(length, device=self.device)
            # 短窗叠加降低峰值内存；模型和渐变都不引入时间偏移。
            segment, overlap = 44100*6, 44100
            for start in range(0, length, segment-overlap):
                stop = min(length, start+segment)
                stem = self.model(signal[:, start:stop].unsqueeze(0))[0, 3].mean(0)*scale
                fade = torch.ones(stop-start, device=self.device)
                n = min(overlap, len(fade))
                if start: fade[:n] *= torch.linspace(0, 1, n, device=self.device)
                if stop < length: fade[-n:] *= torch.linspace(1, 0, n, device=self.device)
                output[start:stop] += stem*fade; weights[start:stop] += fade
                if stop == length: break
            waveform = (output/weights.clamp_min(1e-6)).cpu().numpy()
            result = resample_poly(waveform, rate//divisor, 44100//divisor)
            # 重采样舍入最多产生一个样本差；不做滚动或互相关平移。
            result = np.pad(result, (0, max(0, len(audio)-len(result))))[:len(audio)]
        if not np.isfinite(result).all():
            raise ValueError('人声分离结果无效')
        return result
