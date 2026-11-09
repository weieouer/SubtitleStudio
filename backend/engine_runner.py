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


def run(request, result_path, progress_path):
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
        for i, chunk in enumerate(request['chunks']):
            atomic_write(progress_path, {'progress':i/len(request['chunks']),'message':f'音轨对齐 {i+1}/{len(request["chunks"])} 段'})
            words = model.align(audio=chunk['path'], text=chunk['text'], language='Chinese')[0]
            aligned.append([{'text':w.text,'start':float(w.start_time),'end':float(w.end_time)} for w in words])
        atomic_write(result_path, {'aligned_words':aligned})
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
