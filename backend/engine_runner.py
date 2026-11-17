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


def align_chunk(model, chunk):
    """正常项仅对齐一次；异常项用原时间附近的上下文重试，复用同一模型。"""
    import tempfile
    import wave
    from core.alignment_quality import plain, cue_word_slices, word_timing_issue

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
            issue = word_timing_issue(target)
            shifted = bool(target) and (abs(target[0]['start']-cue['start'])>2.
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
            left = right = i
            # 全局异常时按当前一句的原时间重试，邻句会把短句再次拉向远处。
            low = max(0., cues[left]['start']-.6)
            high = min(duration, max(c['end'] for c in cues[left:right+1])+.6)
            if high<=low or high-low>30:
                continue
            summary['attempted'] += 1
            with tempfile.TemporaryDirectory(prefix='local-align-') as folder:
                path = str(Path(folder)/'audio.wav')
                begin, stop = round(low*rate), round(high*rate)
                source.setpos(begin)
                with wave.open(path, 'wb') as output:
                    output.setparams(source.getparams())
                    output.writeframes(source.readframes(stop-begin))
                try:
                    retry = align(path, '\n'.join(c['text'] for c in cues[left:right+1]))
                except Exception as error:
                    # 局部重试失败不会丢掉已成功的主结果；取消仍由父进程终止。
                    print(f'Local alignment retry failed: {type(error).__name__}', flush=True)
                    continue
            retry_slices = cue_word_slices(cues[left:right+1], retry)
            if retry_slices is None:
                continue
            a, b = retry_slices[i-left]
            candidate = [{**w, 'start':w['start']+low, 'end':w['end']+low} for w in retry[a:b]]
            if word_timing_issue(candidate):
                continue
            if (candidate[0]['start']<cue['start']-1. or candidate[-1]['end']>cue['end']+1.
                    or candidate[-1]['end']>duration+.001):
                continue
            if (first and candidate[0]['start']<words[first-1]['end']-.001
                    or last<len(words) and candidate[-1]['end']>words[last]['start']+.001):
                continue
            replacements.append((first, last, candidate))
            summary['accepted'] += 1
            summary['unresolved'] -= 1
    for first, last, candidate in reversed(replacements):
        words[first:last] = candidate
    return words, summary


def run(request, result_path, progress_path):
    from core.windows_runtime import configure_libraries
    configure_libraries()
    import torch
    use_cuda = request['device'] != 'cpu' and torch.cuda.is_available()
    if request['device'] == 'cuda' and not use_cuda:
        raise RuntimeError('该引擎环境中的 CUDA 不可用，请选择 CPU 或自动')
    device = 'cuda:0' if use_cuda else 'cpu'
    engine, model_path = request['engine'], request['model']
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
