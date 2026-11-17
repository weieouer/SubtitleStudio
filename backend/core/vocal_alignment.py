"""比较原声和人声的逐字边界。证据不足时保留原结果及既有复核状态。"""
from copy import deepcopy
from pathlib import Path
import math

import numpy as np

from .alignment_quality import plain, cue_timing_issue, cue_word_slices
from .vocal_separation import MAX_CLIP_SECONDS, read_wave, write_wave


def raw_cue_words(chunks, aligned_words, count):
    result = [[] for _ in range(count)]
    for chunk, words in zip(chunks, aligned_words):
        slices = cue_word_slices(chunk['cues'], words)
        if slices is None: continue
        for index, (a, b) in zip(chunk['indices'], slices):
            try:
                values = [{**w, 'start':float(w['start'])+chunk['offset'], 'end':float(w['end'])+chunk['offset']} for w in words[a:b]]
            except (ValueError, TypeError, KeyError):
                continue
            if all(math.isfinite(w[k]) for w in values for k in ('start','end')) and all(w['end']>w['start']>=0 for w in values):
                result[index] = values
    return result


def retry_windows(cues, inputs, original_words, duration):
    """只复查已被现有规则判为疑难的字幕，不把正常字幕全部再跑一遍。"""
    windows = []
    for i, cue in enumerate(cues):
        if not (cue.get('alignment_error') or cue.get('alignment_warning')): continue
        words = original_words[i]
        # 至少三个独立词记录，才可能由另外两个词共同确认首尾。短口令保留人工复核。
        if len(words) < 3: continue
        times = [float(w[key]) for w in words for key in ('start', 'end')]
        if not all(math.isfinite(t) and t >= 0 for t in times): continue
        start, end = inputs[i]
        low = max(0., min(start, min(times))-.8)
        high = min(duration, max(end, max(times))+.8)
        if not .1 < high-low <= MAX_CLIP_SECONDS: continue
        # 先对齐到采样点，导出 WAV 与回填时间使用同一偏移。
        low = round(low*16000)/16000; high = round(high*16000)/16000
        windows.append({'index':i, 'offset':low, 'end':high, 'text':cue['text']})
    return windows


def _word_ranges(words):
    position = 0; result = {}
    for word in words:
        text = plain(word.get('text', word.get('word', '')))
        result[position, position+len(text)] = word
        position += len(text)
    return result


def _rms(audio, start, end, offset):
    lo = max(0, round((start-offset)*16000)); hi = min(len(audio), round((end-offset)*16000))
    values = audio[lo:hi]
    return float(np.sqrt(np.mean(values*values))) if hi > lo else 0.


def confirm_vocal_candidate(text, original, candidate, mixture, vocals, offset, input_span):
    """必须同时有多个稳定词锚点和音效被抑制的证据，才允许收紧首尾。"""
    if not original or not candidate or len(mixture) != len(vocals): return False
    if not np.isfinite(mixture).all() or not np.isfinite(vocals).all(): return False
    if plain(text) != ''.join(plain(w['text']) for w in candidate): return False
    if plain(text) != ''.join(plain(w['text']) for w in original): return False
    if cue_timing_issue(text, candidate, input_span): return False
    start_change = float(candidate[0]['start'])-float(original[0]['start'])
    end_change = float(original[-1]['end'])-float(candidate[-1]['end'])
    # 分离可能吞轻声：不借此扩张、整句平移或跨过过长的未知区域。
    if min(start_change, end_change) < -.10 or max(start_change, end_change) > 1.5: return False
    if max(start_change, end_change) < .10: return False
    if (candidate[0]['start'] < offset-.001 or
            candidate[-1]['end'] > offset+len(vocals)/16000+.001): return False
    old = _word_ranges(original); new = _word_ranges(candidate)
    anchors = []
    for i,(bounds, word) in enumerate(new.items()):
        # 被收紧的首/尾词本身不能充当自己的定位证据。
        if (i==0 and start_change>=.10) or (i==len(new)-1 and end_change>=.10): continue
        other = old.get(bounds)
        if other and max(abs(float(word[k])-float(other[k])) for k in ('start', 'end')) <= .12:
            anchors.append(word)
    if len(anchors) < 2: return False
    voice_energy = [_rms(vocals, w['start'], w['end'], offset) for w in anchors]
    original_energy = [_rms(mixture, w['start'], w['end'], offset) for w in anchors]
    typical = float(np.median(voice_energy))
    if typical < 1e-4: return False
    retention = float(np.median([a/max(b, 1e-6) for a, b in zip(voice_energy, original_energy)]))
    # 候选首字和尾字都必须在人声轨上存在，不能只因删掉声音就认定对齐正确。
    for word in (candidate[0], candidate[-1]):
        if _rms(vocals, word['start'], word['end'], offset) < typical*.12: return False
    removed = []
    if start_change >= .10: removed.append((original[0]['start'], candidate[0]['start']-.02))
    if end_change >= .10: removed.append((candidate[-1]['end']+.02, original[-1]['end']))
    for start, end in removed:
        energy = _rms(mixture, start, end, offset)
        if energy < 1e-4: return False
        # 相对于句内人声，首尾被裁区域必须显著更易被分离器抑制。
        if _rms(vocals, start, end, offset)/energy >= min(.35, retention*.45): return False
    return True


def assist_vocal_alignment(cues, inputs, original_words, audio, folder, worker, report, check):
    """可选复查失败不影响原声主结果；取消操作由调用方继续向上传递。"""
    from .alignment import apply_word_times
    windows = retry_windows(cues, inputs, original_words, len(audio)/16000)
    summary = {'attempted':len(windows), 'accepted':0, 'unresolved':len(windows)}
    if not windows: return summary
    chunks = []
    for n, window in enumerate(windows):
        check()
        path = Path(folder)/f'vocal_{n}.wav'
        low, high = window['offset'], window['end']
        write_wave(path, audio[round(low*16000):round(high*16000)])
        chunks.append({**window, 'path':str(path)})
    report(.7, f'人声辅助复查 {len(chunks)} 句…')
    data = worker(chunks)
    if len(data.get('vocal_results', [])) != len(chunks):
        raise ValueError('人声复查结果不完整')
    for chunk, entry in zip(chunks, data['vocal_results']):
        check()
        if entry.get('error'): continue
        index, offset = chunk['index'], chunk['offset']
        words = [{**w, 'start':w['start']+offset, 'end':w['end']+offset} for w in entry['words']]
        mixture, rate = read_wave(chunk['path'])
        vocals, vocal_rate = read_wave(entry['path'])
        if rate != 16000 or vocal_rate != 16000: continue
        if not confirm_vocal_candidate(chunk['text'], original_words[index], words, mixture, vocals, offset,
                                       inputs[index][1]-inputs[index][0]): continue
        # 相邻正常字幕是边界约束，辅助模型不能挤占相邻字幕。
        if index and words[0]['start'] < cues[index-1]['end']-.001: continue
        if index+1 < len(cues) and words[-1]['end'] > cues[index+1]['start']+.001: continue
        trial = deepcopy(cues[index])
        apply_word_times([trial], [0], words)
        if trial.get('alignment_error'): continue
        trial['alignment']['vocal_assisted'] = True
        trial.pop('duration_trim', None)
        trial.pop('audio_refinement', None)
        cues[index] = trial
        summary['accepted'] += 1
        summary['unresolved'] -= 1
    return summary
