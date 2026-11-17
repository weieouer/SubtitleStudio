"""人工确认的当前字幕 -> 音轨强制对齐；不从剧本生成任何字幕文字。"""
from __future__ import annotations
from copy import deepcopy
from bisect import bisect_left
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import wave
import numpy as np
from rapidfuzz.distance import Levenshtein

from .model_manager import runtime_python, weights_present, model_directory, ensure_runtime
from .paths import RESULTS_DIR
from .reference_match import normalized, _reference_boundaries
from .alignment_quality import cue_timing_issue


def invalidate_alignment(cue, reason):
    """回退后的模型词时间不可继续切分；原始记录和人工文字保留。"""
    cue['words']=[]
    for key in ('pause_before','pause_after','audio_silences'):
        cue.pop(key,None)
    cue['alignment']={**(cue.get('alignment') or {}),'stale':True}
    if cue.get('audio_refinement'):
        cue['audio_refinement']={**cue['audio_refinement'],'stale':True}
    cue['alignment_error']=reason
    cue['needs_review']=True


def apply_word_times(cues, indices, words, offset=0):
    """按文字映射词级时间戳，词内切分标记为估计，保存原始输入时间。"""
    query, bounds = '', [0]
    for ci in indices:
        query += normalized(cues[ci]['text'])[0]
        bounds.append(len(query))
    reference, times, word_ranges = '', [], []
    # 一条词记录坏掉时只标记受影响的字幕，不能终止其他字幕的对齐。
    words=[dict(word) for word in words]
    for word in words:
        text = normalized(word['text'])[0]
        try:
            start, end = float(word['start']) + offset, float(word['end']) + offset
            valid=math.isfinite(start) and math.isfinite(end) and start>=0 and end>start
        except (TypeError,ValueError,KeyError):
            valid=False
        if not valid:
            word['alignment_error']='对齐模型返回了无效词级时间'
            start=end=0.
        word_ranges.append((len(reference),len(reference)+len(text)))
        reference += text
        times.extend((start + (end-start)*j/max(1,len(text)), start + (end-start)*(j+1)/max(1,len(text)), j > 0, j < len(text)-1) for j in range(len(text)))
    if not query or not reference or Levenshtein.normalized_similarity(query, reference) < .9:
        for ci in indices:
            invalidate_alignment(cues[ci],'对齐模型返回文字与输入不一致，已保留当前字幕和时间')
        return cues
    # 等长替换可可靠映射时间，不能因为一个同音字就静默清空全部词。
    char_map=[None]*len(query)
    for tag,lo,hi,first,last in Levenshtein.opcodes(query,reference):
        if tag in ('equal','replace') and hi-lo==last-first:
            char_map[lo:hi]=range(first,last)
    owners=[-1]*len(reference)
    for wi,(lo,hi) in enumerate(word_ranges):owners[lo:hi]=[wi]*(hi-lo)
    mapped = _reference_boundaries(query, reference, bounds)
    for local, ci in enumerate(indices):
        a,b = mapped[local],mapped[local+1]
        cue = cues[ci]
        if b <= a or b > len(times):
            # 单条拿不到可靠时间时保留原时间并标记，不能让整批对齐失败。
            invalidate_alignment(cue,'未得到可靠对齐时间')
            continue
        start, end = round(times[a][0],3), round(times[b-1][1],3)
        if end <= start:
            # 同一词内切出的零长度区间：保留原时间继续对齐其他条目。
            invalidate_alignment(cue,'对齐时长无效')
            continue
        cue_normalized,positions=normalized(cue['text'])
        selected=char_map[bounds[local]:bounds[local+1]]
        if any(value is None for value in selected):
            invalidate_alignment(cue,'对齐返回文字有增删，无法可靠分配词时间，已保留输入时间')
            continue
        uncertain = next((words[owners[value]].get('alignment_error') for value in selected
                          if words[owners[value]].get('alignment_error')), None)
        if uncertain:
            invalidate_alignment(cue, uncertain+'，已保留输入时间，请试听复核')
            continue
        mapped_words=[]
        k=0
        while k<len(positions):
            lo=k;k+=1
            # 同一原字簇及同一模型词归在一起，避免Unicode一对多被重复写出。
            while k<len(positions) and (positions[k]==positions[k-1] or owners[selected[k]]==owners[selected[k-1]]):k+=1
            mapped_words.append({'word':cue['text'][positions[lo][0]:positions[k-1][1]],
                                 'start':round(times[selected[lo]][0],3),
                                 'end':round(times[selected[k-1]][1],3)})
        if not mapped_words or any(w['end']<=w['start'] for w in mapped_words):
            invalidate_alignment(cue,'对齐词时间不可用，已保留输入时间')
            continue
        input_span=float(cue['end'])-float(cue['start'])
        issue = cue_timing_issue(cue['text'],mapped_words,input_span)
        if issue:
            invalidate_alignment(cue, issue+'，已保留输入时间，请试听复核')
            continue
        stretched=any(w['end']-w['start']>max(3.,len(w['word'])*.6+1.) for w in mapped_words)
        if (stretched and end-start>max(input_span*3,input_span+3.)
                and abs(start-float(cue['start']))>2.):
            invalidate_alignment(cue,'对齐结果异常拉长，已保留输入时间，请试听复核')
            continue
        cue.pop('alignment_error',None)
        cue.pop('alignment_warning',None)
        cue.pop('word_timing_error',None)
        cue.setdefault('original_start',cue['start']); cue.setdefault('original_end',cue['end'])
        cue.setdefault('original_text',cue['text'])
        cue.setdefault('asr_original', {'text':cue['original_text'],'start':cue['original_start'],'end':cue['original_end'],'id':cue.get('id',ci+1)})
        cue['start'], cue['end'] = start, end
        cue.pop('pause_before', None); cue.pop('pause_after', None)
        cue['estimated'] = bool(times[a][2] or times[b-1][3] or cue_normalized!=reference[a:b])
        cue['alignment'] = {'engine':'Qwen3-ForcedAligner-0.6B','original_start':cue['original_start'],
                            'original_end':cue['original_end'],'word_internal_boundary':cue['estimated']}
        cue['words']=mapped_words
    return cues


def _resolve_overlaps(cues, input_times):
    """强制相邻字幕不重叠，最多首尾相接。

    对齐器在长音频里偶尔把相邻几条的顺序或边界算反。小范围重叠在重叠区中点切开；
    顺序颠倒或整段错位时，把冲突位置附近几条回退到对齐前的输入时间并标记，
    避免为了凑时间把字幕压成零长度。输入时间来自用户确认的字幕，顺序是可靠的。
    """
    def restore(index, reason):
        start, end = input_times[index]
        cues[index]['start'], cues[index]['end'] = start, end
        invalidate_alignment(cues[index],reason)

    def clamp_pass():
        for index in range(1, len(cues)):
            previous, current = cues[index - 1], cues[index]
            if current['start'] >= previous['end'] - .001:
                continue
            if (previous.get('alignment_error') and current.get('alignment_error')
                    and previous['start']+.02 < current['start']):
                previous['end'] = current['start']
                continue
            # 在重叠区中点切开，让两条首尾相接；前提是两边都还留有可显示时长。
            boundary = round((previous['end'] + current['start']) / 2, 3)
            if boundary > previous['start'] + .02 and boundary < current['end'] - .02:
                previous['end'] = current['start'] = boundary
            else:
                current['start'] = previous['end']
            invalidate_alignment(previous,'相邻对齐边界冲突，已调整时间，请复核')
            invalidate_alignment(current,'相邻对齐边界冲突，已调整时间，请复核')

    # 失败条目的旧范围可能长达几分钟；不能让它把相邻成功句一起回退、拉长。
    for index in range(1, len(cues)):
        previous, current = cues[index-1], cues[index]
        if previous['end'] <= current['start']:
            continue
        if previous.get('alignment_error') and not current.get('alignment_error'):
            if previous['start'] + .02 < current['start']:
                previous['end'] = current['start']
        elif current.get('alignment_error') and not previous.get('alignment_error'):
            if previous['end'] + .02 < current['end']:
                current['start'] = previous['end']

    # 先处理顺序颠倒和跨度过大的错位；回退窗口包含前两条，因为坏边界常出在更前一条。
    for _ in range(4):
        conflicts = []
        for index in range(1, len(cues)):
            previous, current = cues[index - 1], cues[index]
            if current['start'] >= previous['end'] - .001:
                continue
            inverted = (current['start'] < previous['start'] - .001
                        or current['end'] < previous['end'] - .001)
            if inverted or previous['end'] - current['start'] > 1:
                conflicts.append(index)
        if not conflicts:
            break
        for index in conflicts:
            for target in range(max(0, index - 2), index + 1):
                restore(target, '对齐结果与相邻字幕冲突，已保留原时间')
        clamp_pass()

    clamp_pass()
    # 最后一道硬保证：即使前面出现无法平分的极端情况，也绝不允许后一条压到前一条。
    for index in range(1, len(cues)):
        previous, current = cues[index - 1], cues[index]
        if current['start'] < previous['end']:
            current['start'] = previous['end']
            invalidate_alignment(current,'相邻对齐边界冲突，已调整时间，请复核')
        if current['end'] <= current['start'] + .001:
            current['end'] = current['start']
            invalidate_alignment(current,'对齐时长无效')
    return cues


def _alignment_windows(cues, audio, sample_rate=16000, max_span=230., context=2., cancel_event=None, preferred_span=30.):
    """按完整字幕组批，优先在持续低能量区切窗；无静音时保留重叠上下文。"""
    from .audio_boundaries import global_noise_floor
    from .asr import Cancelled
    def check():
        if cancel_event is not None and cancel_event.is_set():raise Cancelled()
    check()
    duration=len(audio)/sample_rate
    if not cues:return []
    frame=max(1,round(sample_rate*.02));count=len(audio)//frame
    rms=np.empty(count,dtype=np.float32)
    # 只计算低开销RMS，分块控制临时内存，不为切窗做整轨FFT。
    for offset in range(0,count,1024):
        check()
        block=np.asarray(audio[offset*frame:min(count,offset+1024)*frame]).reshape(-1,frame)
        rms[offset:offset+len(block)]=np.sqrt(np.mean(block*block,axis=1))
    check()
    floor=global_noise_floor(audio,sample_rate)
    voice=float(np.percentile(rms,85)) if count else 0.
    # 限制阈值相对语音能量，持续音乐/恒定噪声不能整段被当成静音。
    threshold=min(max(1e-5,floor*1.5),max(1e-5,voice*.15))
    quiet=rms<=threshold
    edges=np.diff(np.r_[False,quiet,False].astype(np.int8))
    silences=[(a*frame/sample_rate,b*frame/sample_rate)
              for a,b in zip(np.flatnonzero(edges==1),np.flatnonzero(edges==-1))
              if (b-a)*frame/sample_rate>=.08-1e-6]
    ends=[b for _,b in silences]
    def quiet_cut(lo,hi,target):
        """切点两侧各留40ms低能量，且不进入本窗口负责的字幕范围。"""
        if hi<lo:return None
        candidates=[]
        for j in range(bisect_left(ends,lo+.04),len(silences)):
            a,b=silences[j]
            if a>hi-.04:break
            left,right=max(lo,a+.04),min(hi,b-.04)
            if left<=right:
                cut=min(right,max(left,target));candidates.append(cut)
        return min(candidates,key=lambda value:abs(value-target)) if candidates else None
    groups=[];first=0
    while first<len(cues):
        check()
        # 短窗口减少短句匹配到远处相似音频；已有长字幕单独保留完整内容。
        limit=min(max_span,max(preferred_span,float(cues[first]['end'])-float(cues[first]['start'])))
        last=first;covered=float(cues[first]['end']);prefix=[]
        while last<len(cues):
            cue=cues[last]
            if float(cue['end'])-float(cue['start'])>max_span:
                raise ValueError('单条字幕过长，请先分段再对齐')
            end=max(covered,float(cue['end']))
            if last>first and end-float(cues[first]['start'])>limit:break
            covered=end;prefix.append(covered);last+=1
        # 临近容量上限时向前最多30秒找完整字幕间的静音，不切成大量短块。
        if last<len(cues):
            for boundary in range(last,first,-1):
                if float(cues[boundary]['start'])<float(cues[first]['start'])+max(0,limit-5):break
                left=prefix[boundary-first-1];right=float(cues[boundary]['start'])
                if quiet_cut(left,right,(left+right)/2) is not None:
                    last=boundary;break
        groups.append(list(range(first,last)));first=last
    windows=[]
    for indices in groups:
        check()
        first_start=float(cues[indices[0]]['start'])
        last_end=max(float(cues[i]['end']) for i in indices)
        start=max(0.,first_start-context);end=min(duration,last_end+context)
        leading=quiet_cut(start,min(duration,first_start),first_start-.2)
        trailing=quiet_cut(max(0.,last_end),end,last_end+.2)
        if leading is not None:start=leading
        if trailing is not None:end=trailing
        if start>=end:raise ValueError('字幕时间超出音轨，请先粗调至对应片段')
        windows.append({'indices':indices,'start':start,'end':end,
                        'start_boundary':'quiet' if leading is not None else 'context',
                        'end_boundary':'quiet' if trailing is not None else 'context'})
    return windows


def align_cues(path, cues, device, on_progress=None, cancel_event=None):
    from .asr import Cancelled
    from .media import decode_audio
    name = 'qwen3-forcedaligner-0.6b'
    if not weights_present(name):
        raise ValueError('请先在辅模菜单下载强制对齐模型')
    ensure_runtime('qwen', on_progress, cancel_event)
    def check():
        if cancel_event and cancel_event.is_set(): raise Cancelled()
    def report(p,message):
        if on_progress: on_progress(p,message)
    if any(not normalized(c['text'])[0] for c in cues):
        raise ValueError('强制对齐前请先处理空字幕或仅含标点的字幕')
    if any(cues[i]['start'] < cues[i-1]['start'] for i in range(1,len(cues))):
        raise ValueError('字幕顺序与时间不一致，请先按时间整理')
    report(.01,'读取视频音轨（优先复用缓存）…')
    audio = decode_audio(path,16000,cancel_event); duration = len(audio)/16000
    # 对齐失败时要用“本次输入”的时间回退；不能用 original_start，它可能来自更早的识别。
    input_times = [(float(cue['start']), float(cue['end'])) for cue in cues]
    result = deepcopy(cues)
    from .vocal_separation import MODEL_NAME as vocal_model
    from .vocal_alignment import raw_cue_words, retry_windows, assist_vocal_alignment
    vocal_ready = weights_present(vocal_model)
    vocal_summary = {'available':vocal_ready, 'attempted':0, 'accepted':0, 'unresolved':0}
    report(.02,'按完整字幕及静音规划对齐窗口…')
    windows=_alignment_windows(cues,audio,cancel_event=cancel_event)
    with tempfile.TemporaryDirectory(prefix='align-',dir=RESULTS_DIR) as temp:
        folder=Path(temp); chunks=[]
        for n,window in enumerate(windows):
            check()
            indices=window['indices'];start=window['start'];end=window['end']
            filename=folder/f'align_{n}.wav'
            with wave.open(str(filename),'wb') as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
                output.writeframes((np.clip(audio[round(start*16000):round(end*16000)],-1,1)*32767).astype('<i2').tobytes())
            chunks.append({'path':str(filename),'text':'\n'.join(cues[i]['text'] for i in indices),'offset':start,'indices':indices,
                           'cues':[{'text':cues[i]['text'],'start':max(0.,float(cues[i]['start'])-start),
                                    'end':min(end-start,float(cues[i]['end'])-start)} for i in indices]})
        def run_engine(payload, prefix, low, span):
            request,result_path,progress=[folder/f'{prefix}-{suffix}.json' for suffix in ('request','result','progress')]
            request.write_text(json.dumps({'model':str(model_directory(name)), 'device':device, **payload},ensure_ascii=False),encoding='utf-8')
            log=folder/f'{prefix}-engine.log'
            with log.open('w',encoding='utf-8') as out:
                process=subprocess.Popen([str(runtime_python('qwen')),str(Path(__file__).resolve().parents[1]/'engine_runner.py'),str(request),str(result_path),str(progress)],
                    stdout=out,stderr=subprocess.STDOUT,env={**os.environ,'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','PYTHONUTF8':'1'},
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                try:
                    while process.poll() is None:
                        check()
                        try:
                            p=json.loads(progress.read_text(encoding='utf-8')); report(low+span*p['progress'],p['message'])
                        except (OSError,ValueError,KeyError): pass
                        if cancel_event: cancel_event.wait(.3)
                        else:
                            import time
                            time.sleep(.3)
                    check()
                    if process.returncode or not result_path.exists():
                        from .windows_runtime import engine_failure
                        raise RuntimeError('强制对齐失败：'+engine_failure(log.read_text(encoding='utf-8',errors='replace')))
                    return json.loads(result_path.read_text(encoding='utf-8'))
                finally:
                    if process.poll() is None: process.terminate(); process.wait(timeout=10)
        data=run_engine({'engine':'aligner','chunks':chunks},'original',.05,.6 if vocal_ready else .9)
        for chunk,words in zip(chunks,data['aligned_words']):
            apply_word_times(result,chunk['indices'],words,chunk['offset'])
        check()
        if len(data['aligned_words']) != len(chunks): raise ValueError('对齐结果不完整')
        from .audio_boundaries import refine_boundaries, cap_durations
        result=refine_boundaries(result,audio,cancel_event=cancel_event)
        # 短字幕不该占很长的时间；音效或相邻声音会把边界拉长，用语音活动收紧。
        duration_repairs=cap_durations(result,audio,input_times=input_times,cancel_event=cancel_event)
        if vocal_ready:
            raw_words=raw_cue_words(chunks,data['aligned_words'],len(cues))
            retries=retry_windows(result,input_times,raw_words,duration)
            vocal_summary.update(attempted=len(retries),unresolved=len(retries))
            if retries:
                def run_vocals(batch):
                    return run_engine({'engine':'aligner-vocals','chunks':batch,
                                       'vocal_model':str(model_directory(vocal_model))},'vocal',.7,.25)
                before_vocals=deepcopy(result)
                try:
                    vocal_summary.update(assist_vocal_alignment(result,input_times,raw_words,audio,folder,run_vocals,report,check))
                    # 再用原音轨检查一次，不能仅凭分离结果消掉原有的不确定性。
                    if vocal_summary['accepted']:
                        refined=refine_boundaries(deepcopy(result),audio,cancel_event=cancel_event)
                        for i,cue in enumerate(result):
                            if (cue.get('alignment') or {}).get('vocal_assisted'):
                                if refined[i].get('alignment_error'):
                                    result[i]=before_vocals[i]
                                    vocal_summary['accepted']-=1;vocal_summary['unresolved']+=1
                                else:result[i]=refined[i]
                except Cancelled:
                    raise
                except Exception as error:
                    import logging
                    logging.getLogger(__name__).warning('人声辅助复查未完成，保留原声对齐结果：%s',error)
                    result=before_vocals
                    vocal_summary.update(accepted=0,unresolved=len(retries))
                    vocal_summary['error']='人声复查未完成，保留原声对齐结果'
                check()
        # 失败项不能凭能量重定位后看起来像成功；也不能继续使用旧词时间拆句。
        for i,cue in enumerate(result):
            if cue.get('alignment_error'):
                cue['start'],cue['end']=input_times[i]
                invalidate_alignment(cue,cue['alignment_error'])
        # 对齐结果必须满足“不重叠、最多首尾相接”；校验前先归一化，避免一条坏时间拖垮整批。
        result=_resolve_overlaps(result,input_times)
        from .srt_io import validate_cues
        # 提示里用界面序号（display_id），避免拆条后显示的内部 id 与用户看到的序号不一致。
        skipped = [{'id': c.get('display_id') or c.get('id'), 'reason': c['alignment_error']} for c in result if c.get('alignment_error')]
        # 跳过的条目保留原时间，不参与重叠校驗，避免一条失败拖垮整批。
        errors = validate_cues([c for c in result if not c.get('alignment_error')], duration)
        if errors:
            raise ValueError('对齐时间校验未通过：'+'；'.join(errors[:3]))
        return result, {'duration':duration,'engine':'Qwen3-ForcedAligner-0.6B','boundary_refinement':'rms_spectral_flux_hysteresis',
                         'aligned_count':len(result)-len(skipped),'failed_count':len(skipped),
                         'duration_repairs':duration_repairs,
                         'local_retry':data.get('local_retry',{}),
                         'vocal_assistance':vocal_summary,
                         'windowing':{'strategy':'cue_silence_context','count':len(windows),
                                      'windows':windows},
                         'skipped': skipped}
