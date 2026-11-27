"""只在对应引擎隔离环境启动；SDK 标准输出不会混入 JSON 通信。"""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys


def atomic_write(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, path)


def _ascii_work_dir():
    """sentencepiece 是 C++ 实现，Windows 下打不开含中文的路径；找一个纯英文可写目录。"""
    for base in (os.environ.get('PROGRAMDATA'), os.environ.get('PUBLIC'),
                 os.environ.get('LOCALAPPDATA'), os.environ.get('TEMP')):
        if not base or not str(base).isascii():
            continue
        path = Path(base) / 'SubtitleStudio' / 'engine-cache'
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except OSError:
            continue
    return None


def _ascii_copy(path):
    """把句子模型复制到纯英文路径，返回可被 sentencepiece 加载的路径。"""
    source = Path(path)
    if str(source).isascii():
        return path
    try:
        base = _ascii_work_dir()
        if base is None:
            return path
        import hashlib, shutil
        digest = hashlib.sha1(str(source.resolve()).encode('utf-8')).hexdigest()[:10]
        target = base / f'{digest}-{source.name}'
        stat = source.stat()
        if not target.is_file() or target.stat().st_size != stat.st_size or target.stat().st_mtime_ns != stat.st_mtime_ns:
            shutil.copy2(source, target)
        return str(target)
    except OSError:
        return path


def _window_words(align, source, path, cues, target_index, low, high):
    """同一窗口裁音频、提交对应台词，再将目标句的词时间换回块内坐标。"""
    import wave
    from core.alignment_quality import cue_word_slices

    rate = source.getframerate()
    begin, stop = round(low*rate), round(high*rate)
    if begin>=stop:
        return []
    source.setpos(begin)
    with wave.open(path, 'wb') as output:
        output.setparams(source.getparams())
        output.writeframes(source.readframes(stop-begin))
    try:
        words = align(path, '\n'.join(c['text'] for c in cues))
    except Exception as error:
        print(f'Local alignment retry failed: {type(error).__name__}', flush=True)
        return []
    slices = cue_word_slices(cues,words)
    if slices is None:
        return []
    a,b = slices[target_index]
    offset = begin/rate
    return [{**w,'start':w['start']+offset,'end':w['end']+offset} for w in words[a:b]]


def _context_windows(cues, i, duration):
    left = i-1 if i and 0<=cues[i]['start']-cues[i-1]['end']<=1.5 else i
    right = i+1 if i+1<len(cues) and 0<=cues[i+1]['start']-cues[i]['end']<=1.5 else i
    # 两侧上下文优先，再试缩短到一侧。音频和文字总是使用同一组邻句。
    for a,b in dict.fromkeys(((left,right),(i,right),(left,i),(i,i))):
        low,high = max(0.,cues[a]['start']-.24),min(duration,cues[b]['end']+.24)
        if 0<high-low<=15:
            yield a,b,low,high


def _same_word_position(a,b):
    # Qwen 的词时间落在 80 ms 网格上；允许两次观测相差至多两格。
    return (len(a)==len(b) and bool(a) and a[-1]['end']>a[0]['start'] and b[-1]['end']>b[0]['start']
            and all(x['text']==y['text'] and abs(x['start']-y['start'])<=.161
                    and abs(x['end']-y['end'])<=.161 for x,y in zip(a,b)))


def _retry_with_context(align, source, path, cues, i, target, retry, low, high, usable):
    rate = source.getframerate()
    seen = {(0,len(cues)-1,0,source.getnframes()),(i,i,round(low*rate),round(high*rate))}
    evidence = [target,retry]
    valid = []
    for a,b,lo,hi in _context_windows(cues,i,source.getnframes()/rate):
        key = (a,b,round(lo*rate),round(hi*rate))
        if key in seen:
            continue
        seen.add(key)
        observed = _window_words(align,source,path,cues[a:b+1],i-a,lo,hi)
        if usable(observed):
            if any(_same_word_position(prior,observed) for prior in evidence):
                return observed
            valid.append(observed)
        else:
            for prior in valid:
                if _same_word_position(prior,observed):
                    return prior
        evidence.append(observed)
    return None


def align_chunk(model, chunk):
    """正常项仅对齐一次；异常项用原时间附近的上下文重试，复用同一模型。"""
    import tempfile
    import wave
    from core.alignment_quality import plain, cue_word_slices, cue_timing_issue

    def align(path, text):
        result = model.align(audio=path, text=text, language='Chinese')[0]
        return [{'text':w.text, 'start':float(w.start_time), 'end':float(w.end_time)} for w in result]

    words = align(chunk['path'], chunk['text'])
    cues = chunk.get('cues', [])
    slices = cue_word_slices(cues, words) if cues else None
    summary = {'attempted':0, 'accepted':0, 'unresolved':0}
    if slices is None:
        return words, summary
    # 只替换目标词片段，邻句只辅助定位，不改变其结果或文字。
    replacements = []
    with wave.open(chunk['path'], 'rb') as source:
        rate = source.getframerate()
        duration = source.getnframes()/rate
        for i, (first, last) in enumerate(slices):
            cue = cues[i]
            target = words[first:last]
            span = float(cue['end'])-float(cue['start'])
            issue = cue_timing_issue(cue['text'],target,span)
            # ASR 粗起点可能包含长段音效；候选完整落在本句范围内时，
            # 不能仅因起点晚了两秒就作废，再把短句重试到前一句声音上。
            inside = bool(target) and (target[0]['start']>=cue['start']-.25
                                       and target[-1]['end']<=cue['end']+.25)
            shifted = bool(target) and not inside and (abs(target[0]['start']-cue['start'])>2.
                                                      or target[-1]['end']>cue['end']+2.)
            if not issue and not shifted:
                continue
            summary['unresolved'] += 1
            # 仍失败的短句即使时间长度看似正常，也不能被父进程当作成功。
            for word in target:
                word['alignment_error'] = issue or '对齐位置偏移较大，局部重试仍未确认'
            # 粗时间本身跨越几十秒时，没有文字定位证据，不能凭能量猜一句在哪。
            if not .05 < span <= max(8., min(15., len(plain(cue['text']))*.8+2.)):
                continue
            # 全局异常时按当前一句的原时间重试，邻句会把短句再次拉向远处。
            low = max(0.,cue['start']-.6)
            high = min(duration,cue['end']+.6)
            # 重试只带本句文字，窗口里却保留邻句声音时，短句容易把首字贴到上一句尾音，
            # 或把尾字甩到下面重复出现的相似台词上。主对齐已把本句词放在邻句边界内时，
            # 把窗口收紧到边界，不让邻句语音成为干扰。
            if i and cues[i-1]['end'] <= cue['start']:
                edge = cues[i-1]['end']+.08
                if target and target[0]['start'] >= edge-.001:
                    low = max(low, edge)
            if i+1 < len(cues) and cues[i+1]['start'] >= cue['end']:
                edge = cues[i+1]['start']-.08
                if target and target[-1]['end'] <= edge+.001:
                    high = min(high, edge)
            if high<=low or high-low>30:
                continue
            summary['attempted'] += 1
            previous_end = words[first-1]['end'] if first else 0.
            if replacements and replacements[-1][1]==first:
                previous_end = replacements[-1][2][-1]['end']
            next_start = words[last]['start'] if last<len(words) else duration
            def usable(candidate):
                return (bool(candidate) and not cue_timing_issue(cue['text'],candidate,span)
                        and candidate[0]['start']>=max(previous_end-.001,cue['start']-1.)
                        and candidate[-1]['end']<=min(next_start+.001,duration+.001,cue['end']+1.))

            with tempfile.TemporaryDirectory(prefix='local-align-') as folder:
                path = str(Path(folder)/'audio.wav')
                retry = _window_words(align,source,path,[cue],0,low,high)
                candidate = retry if usable(retry) else None
                boundary_issue = issue and issue.startswith(('句首字词','句尾字词'))
                if candidate is None and (boundary_issue or any(w['start']==w['end'] for w in target+retry)):
                    candidate = _retry_with_context(align,source,path,cues,i,target,retry,low,high,usable)
                if candidate is None:
                    continue
            replacements.append((first, last, candidate))
            summary['accepted'] += 1
            summary['unresolved'] -= 1
    for first, last, candidate in reversed(replacements):
        words[first:last] = candidate
    return words, summary


def align_vocals(request, result_path, progress_path, device):
    """先分离疑难短片段并释放分离模型，再加载对齐器，避免同时占用两套显存。"""
    import gc
    import torch
    from core.vocal_separation import VocalSeparator, read_wave, write_wave
    chunks = request['chunks']
    entries = []
    separator = VocalSeparator(request['vocal_model'], device)
    threads = torch.get_num_threads()
    try:
        if device == 'cpu': torch.set_num_threads(min(8, os.cpu_count() or 1))
        for i, chunk in enumerate(chunks):
            atomic_write(progress_path, {'progress':.55*i/max(1,len(chunks)), 'message':f'分离疑难句人声 {i+1}/{len(chunks)}'})
            try:
                audio, rate = read_wave(chunk['path'])
                vocals = separator.separate(audio, rate)
                path = str(result_path.parent/f'vocals_{i}.wav')
                write_wave(path, vocals, rate)
                entries.append({'path':path})
            except Exception as error:
                print(f'Vocal separation failed: {error!r}', flush=True)
                entries.append({'error':type(error).__name__})
    finally:
        del separator
        gc.collect()
        if device != 'cpu': torch.cuda.empty_cache()
        torch.set_num_threads(threads)
    if any(not e.get('error') for e in entries):
        from qwen_asr import Qwen3ForcedAligner
        model = Qwen3ForcedAligner.from_pretrained(request['model'], dtype=torch.float32 if device=='cpu' else torch.float16, device_map=device)
        for i, (chunk, entry) in enumerate(zip(chunks, entries)):
            atomic_write(progress_path, {'progress':.55+.4*i/max(1,len(chunks)), 'message':f'核对人声逐字时间 {i+1}/{len(chunks)}'})
            if entry.get('error'): continue
            try:
                output = model.align(audio=entry['path'], text=chunk['text'], language='Chinese')[0]
                entry['words'] = [{'text':w.text, 'start':float(w.start_time), 'end':float(w.end_time)} for w in output]
            except Exception as error:
                print(f'Vocal alignment failed: {error!r}', flush=True)
                entry['error'] = type(error).__name__
    atomic_write(result_path, {'vocal_results':entries})


def run(request, result_path, progress_path):
    from core.windows_runtime import configure_libraries
    configure_libraries()
    import torch
    use_cuda = request['device'] != 'cpu' and torch.cuda.is_available()
    if request['device'] == 'cuda' and not use_cuda:
        raise RuntimeError('该引擎环境中的 CUDA 不可用，请选择 CPU 或自动')
    device = 'cuda:0' if use_cuda else 'cpu'
    engine, model_path = request['engine'], request['model']
    if engine == 'aligner-vocals':
        align_vocals(request, result_path, progress_path, device)
        return
    if engine == 'aligner':
        from qwen_asr import Qwen3ForcedAligner
        model = Qwen3ForcedAligner.from_pretrained(model_path, dtype=torch.float16 if use_cuda else torch.float32, device_map=device)
        aligned = []
        retries = {'attempted':0, 'accepted':0, 'unresolved':0}
        for i, chunk in enumerate(request['chunks']):
            atomic_write(progress_path, {'progress':i/len(request['chunks']),'message':f'音轨对齐 {i+1}/{len(request["chunks"])} 段'})
            words, summary = align_chunk(model, chunk)
            aligned.append(words)
            for key in retries: retries[key] += summary[key]
        atomic_write(result_path, {'aligned_words':aligned, 'local_retry':retries})
        return
    if engine == 'firered':
        from fireredasr2s.fireredasr2 import FireRedAsr2, FireRedAsr2Config
        from fireredasr2s.fireredasr2 import asr as firered_asr
        base_tokenizer = firered_asr.ChineseCharEnglishSpmTokenizer
        class _AsciiPathTokenizer(base_tokenizer):
            def __init__(self, dict_path, spm_model=None, *args, **kwargs):
                super().__init__(dict_path, _ascii_copy(spm_model) if spm_model else spm_model, *args, **kwargs)
        firered_asr.ChineseCharEnglishSpmTokenizer = _AsciiPathTokenizer
        try:
            model = FireRedAsr2.from_pretrained('aed', model_path,
                FireRedAsr2Config(use_gpu=use_cuda, use_half=use_cuda, return_timestamp=True, beam_size=3))
        finally:
            firered_asr.ChineseCharEnglishSpmTokenizer = base_tokenizer
    elif engine == 'qwen':
        from qwen_asr import Qwen3ASRModel
        model = Qwen3ASRModel.from_pretrained(model_path, dtype=torch.float16 if use_cuda else torch.float32,
            device_map=device, max_inference_batch_size=1, max_new_tokens=1024)
    else:
        raise ValueError('不支持的识别引擎')
    cues = []
    for i, chunk in enumerate(request['chunks']):
        atomic_write(progress_path, {'progress': i / len(request['chunks']), 'message': f'识别 {i + 1}/{len(request["chunks"])} 段'})
        words = []
        confidence = None
        if engine == 'firered':
            entry = model.transcribe([str(i)], [chunk['path']])[0]
            text, confidence = entry.get('text', ''), entry.get('confidence')
            words = [{'word': t, 'start': chunk['start'] + float(a), 'end': chunk['start'] + float(b)} for t, a, b in entry.get('timestamp', [])]
        else:
            entry = model.transcribe(audio=chunk['path'], language=None)[0]
            text = entry.text
        text = str(text).strip()
        if not text:
            continue
        start = max(chunk['start'], min((w['start'] for w in words), default=chunk['start']))
        end = min(chunk['end'], max((w['end'] for w in words), default=chunk['end']))
        if end <= start:
            continue
        cues.append({'id':len(cues)+1, 'start':round(start,3), 'end':round(end,3), 'text':text,
                     'words':words, 'source':'asr', 'confidence':confidence, 'is_noise':False,
                     'estimated':not bool(words), 'needs_review':not bool(words)})
    atomic_write(result_path, {'cues':cues})
    atomic_write(progress_path, {'progress':1, 'message':'识别完成'})


if __name__ == '__main__':
    run(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')), Path(sys.argv[2]), Path(sys.argv[3]))
