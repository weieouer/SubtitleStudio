"""RMS/频谱变化与双阈值静音检测。只辅助时间微调，不能判断是否为对白。"""
from __future__ import annotations

import numpy as np

# Silero VAD 每帧 512 个 16 kHz 采样（32 ms）。只在边界微调时做二次校验。
VAD_FRAME_SECONDS = 512 / 16000
_VAD_DISABLED = False


def _vad_probabilities(audio, sample_rate=16000):
    """返回 Silero VAD 的逐帧语音概率；模型或 onnxruntime 不可用时返回 None。

    faster-whisper 已在主环境内置该 ONNX 模型，这里直接复用同一份权重；
    任何加载失败都不能影响原有的 RMS/频谱微调，因此调用方把 None 当作“没有额外证据”。
    """
    global _VAD_DISABLED
    if _VAD_DISABLED or sample_rate != 16000:
        return None
    try:
        from faster_whisper.vad import get_vad_model
        model = get_vad_model()
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if not len(samples):
            return None
        padded = np.pad(samples, (0, (-len(samples)) % 512))
        return np.asarray(model(padded), dtype=np.float32).reshape(-1)
    except Exception:
        # 模型资源随 faster-whisper 发行；缺失时静默退回原有 RMS 方案。
        _VAD_DISABLED = True
        return None


def _vad_runs(probabilities, threshold=.5, neg_threshold=.35,
              min_speech_frames=4, min_silence_frames=4):
    """把逐帧概率压成语音区间；至少 128 ms 才算是可靠语音，滤掉瞬时爆音。"""
    runs = []
    start = None
    quiet = 0
    for index, probability in enumerate(probabilities):
        if start is None:
            if probability >= threshold:
                start = index
                quiet = 0
        elif probability >= neg_threshold:
            quiet = 0
        else:
            quiet += 1
            if quiet >= min_silence_frames:
                end = index - quiet + 1
                if end - start >= min_speech_frames:
                    runs.append((start, end))
                start = None
                quiet = 0
    if start is not None and len(probabilities) - start >= min_speech_frames:
        runs.append((start, len(probabilities)))
    return runs


def _vad_region_ambiguous(probabilities, low, high, threshold=.30,
                          max_run=3, max_fraction=.15):
    """待裁掉的前/尾区域是否像“轻声说话”。

    音乐、噪声和音效只会有零星尖峰；持续 96 ms 以上的中高概率更可能是
    真实但音量较低的人声，此时宁可保留原时间，不冒险裁掉字。
    """
    segment = probabilities[max(0, int(low)):max(0, int(high))]
    if not len(segment):
        return False
    mask = segment >= threshold
    longest = current = 0
    for flag in mask:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    return longest >= max_run or float(mask.mean()) > max_fraction


def _vad_region(audio, sample_rate, start, end):
    """对整条字幕（前后各留 0.3 s）做一次 VAD。

    只截取边界附近的窗口会让模型丢掉句首上下文，容易把句尾音效继续当成
    人声；整条字幕的音轨足够短，一次推理也比分两次更省。
    """
    duration = len(audio) / sample_rate
    low = max(0., start - .30)
    high = min(duration, end + .30)
    if high - low < .25:
        return None, None
    probabilities = _vad_probabilities(audio[round(low * sample_rate):round(high * sample_rate)])
    return (low, probabilities) if probabilities is not None else (None, None)


def _run_overlap(onset, speech_end, word):
    """语音区间与某个词的时间重叠；用于把 VAD 区间和词位置对应起来。"""
    return max(0., min(speech_end, float(word['end'])) - max(onset, float(word['start'])))


def _vad_start(audio, sample_rate, start, end, words, probabilities=None, low=None, max_span=3.0):
    """若第一个词之前是 VAD 明确判为非语音的声音，返回真正的语音起点。

    只在有词时间、且裁掉后首词仍保留至少 50 ms 时给出建议；其余情况返回 None。
    有多个语音区间时选与首词重叠最多的那个，避免把词前的短促音效当成说话。
    """
    if sample_rate != 16000 or not words:
        return None
    if probabilities is None:
        low, probabilities = _vad_region(audio, sample_rate, start, end)
    if probabilities is None:
        return None
    start_index = max(0, min(len(probabilities), int(round((start - low) / VAD_FRAME_SECONDS))))
    best=None;best_overlap=0.
    for begin, finish in _vad_runs(probabilities):
        onset = low + begin * VAD_FRAME_SECONDS
        speech_end = low + finish * VAD_FRAME_SECONDS
        if speech_end <= start + .02:
            continue                      # 更早的语音，与本条起点无关
        if onset <= start + .02:
            return None                   # 起点处已经在说话，不能往后裁
        if _vad_region_ambiguous(probabilities, start_index, begin):
            continue                      # 该区间前可能是轻声人声，不能据此裁
        if onset - start < .12 or onset - start > max_span:
            continue
        if speech_end - onset < .12:
            continue
        overlap = _run_overlap(onset, speech_end, words[0])
        if overlap <= 0:
            continue                      # 该区间不对应首词，忽略
        if onset > float(words[0]['end']) - .05:
            continue                      # 该区间不对应首词，忽略
        if overlap > best_overlap:
            best=onset;best_overlap=overlap
    return best - .02 if best is not None else None


def _vad_end(audio, sample_rate, start, end, words, probabilities=None, low=None, max_span=3.0):
    """若词尾之后是 VAD 明确判为非语音的声音，返回真正的语音终点。"""
    if sample_rate != 16000 or not words:
        return None
    if probabilities is None:
        low, probabilities = _vad_region(audio, sample_rate, start, end)
    if probabilities is None:
        return None
    end_index = max(0, min(len(probabilities), int(round((end - low) / VAD_FRAME_SECONDS))))
    best=None;best_overlap=0.
    for begin, finish in reversed(_vad_runs(probabilities)):
        onset = low + begin * VAD_FRAME_SECONDS
        speech_end = low + finish * VAD_FRAME_SECONDS
        if speech_end < start + .02:
            continue
        if onset >= end - .02 or speech_end >= end - .02:
            return None                   # 结尾处还有语音，不能往前裁
        if _vad_region_ambiguous(probabilities, finish, end_index):
            continue                      # 该区间后可能是轻声人声，不能据此裁
        if end - speech_end < .12 or end - speech_end > max_span:
            continue
        if speech_end - onset < .12:
            continue
        overlap = _run_overlap(onset, speech_end, words[-1])
        if overlap <= 0:
            continue                      # 该区间不对应尾词，忽略
        if speech_end < float(words[-1]['start']) + .05:
            continue                      # 该区间不对应尾词，忽略
        if overlap > best_overlap:
            # VAD 的尾部常会多吞 0.1–0.4 秒音效；最后一个词的结束时间
            # 是更可靠的锚点，最多允许再留 150 ms 尾音。
            best=min(speech_end + .02, float(words[-1]['end']) + .15);best_overlap=overlap
    return best


def _clip_words(cue):
    """边界收紧后按新范围裁剪词时间；裁掉的词不再参与后续分段。"""
    words=cue.get('words') or []
    if not words:return
    start,end=float(cue['start']),float(cue['end'])
    clipped=[]
    for word in words:
        a=max(start,float(word['start']));b=min(end,float(word['end']))
        if b-a>=.02:clipped.append({**word,'start':round(a,3),'end':round(b,3)})
    cue['words']=clipped


def _speech_runs(audio,sample_rate,start,end,cancel_event=None):
    """窗口内的语音活动区间：优先 Silero VAD，缺失时退回 RMS/频谱。"""
    low=max(0.,float(start));high=min(len(audio)/sample_rate,float(end))
    if high<=low:return []
    segment=np.asarray(audio[round(low*sample_rate):round(high*sample_rate)],dtype=np.float32)
    if not len(segment):return []
    probabilities=_vad_probabilities(segment,sample_rate)
    if probabilities is not None:
        return [(low+a*VAD_FRAME_SECONDS,low+b*VAD_FRAME_SECONDS) for a,b in _vad_runs(probabilities)]
    rms,flux,hop,frame=acoustic_features(segment,sample_rate,cancel_event)
    intervals,_=speech_intervals(rms,flux,hop,frame,global_noise_floor(audio,sample_rate))
    return [(low+a,low+b) for a,b in intervals]


def cap_durations(cues,audio,sample_rate=16000,cancel_event=None,input_times=None):
    """按字数上限收紧被音效或相邻声音拉长的对齐结果，只调时间不改文字。

    对齐器在一个窗口里遇到音效时，可能把短字幕的起点或终点贴到音效上；
    这里用词时间和 VAD/RMS 语音活动找出真正说话的一段。语音证据不足时
    退回对齐前的输入时间，仍超上限则只标记待复核，绝不硬裁掉文字音频。
    input_times 是本次对齐前的逐条时间；缺省时退回 cue 的 original_start/end。
    返回实际处理的条目摘要，供对齐信息展示。
    """
    from .duration import duration_bounds,speech_coverage,speech_near,trim_to_speech
    if not cues:return []
    duration=len(audio)/sample_rate
    repairs=[]
    for index,cue in enumerate(cues):
        if cancel_event is not None and cancel_event.is_set():
            from .asr import Cancelled
            raise Cancelled()
        length,target,cap=duration_bounds(str(cue.get('text') or ''))
        start,end=float(cue.get('start',0.)),float(cue.get('end',0.))
        if not length or end-start<=0:continue
        if input_times is not None and index<len(input_times):
            input_start,input_end=float(input_times[index][0]),float(input_times[index][1])
        else:
            input_start=float(cue.get('original_start',start));input_end=float(cue.get('original_end',end))
        input_span=input_end-input_start
        over_cap=end-start>cap+1e-6
        shifted=abs(start-input_start)>1.2 and input_span>.1
        if not over_cap and not shifted:continue
        words=[w for w in (cue.get('words') or [])
               if float(w.get('end',0))>float(w.get('start',0))]
        # 对齐结果异常拉得很长时只在粗时间附近找语音，避免对整分钟音频跑 VAD。
        if max(end,input_end)-min(start,input_start)>30:
            low=max(0.,input_start-1.);high=min(duration,input_end+1.)
        else:
            low=max(0.,min(start,input_start)-.5);high=min(duration,max(end,input_end)+.5)
        runs=_speech_runs(audio,sample_rate,low,high,cancel_event)
        before=[round(start,3),round(end,3)]
        if not over_cap:
            # 时间长度合理，但整条贴在音效上：对齐段几乎没有语音，而粗时间附近有。
            covered=speech_coverage(runs,start,end)
            if covered>=.35 or speech_coverage(runs,input_start,input_end)<=covered+.25:continue
            cluster=speech_near(runs,(input_start,input_end))
            if cluster is None:continue
            length_=cluster[1]-cluster[0]
            anchor=min(input_end,cluster[1])-max(input_start,cluster[0])
            if not .1<=length_<=cap+.2 or anchor<.25*min(length_,input_span):continue
            new_start=round(max(0.,cluster[0]-.02),3);new_end=round(min(duration,cluster[1]+.02),3)
            cue['start'],cue['end']=new_start,new_end
            _clip_words(cue)
            cue['duration_trim']={'length':length,'target':round(target,3),'cap':round(cap,3),
                                  'before':before,'after':[new_start,new_end],'method':'speech_reanchor'}
            cue['needs_review']=True
            if cue.get('alignment'):cue['alignment']={**cue['alignment'],'duration_trimmed':True}
            repairs.append({'id':cue.get('display_id',cue.get('id')),'method':'speech_reanchor',
                            'before':before,'after':[new_start,new_end]})
            continue
        if not words and input_span>0:
            # 没有词时间时用对齐前的粗时间当锚点，避免音效拉长后完全没有证据。
            words=[{'start':input_start,'end':input_end}]
        chosen=trim_to_speech(runs,words,(input_start,input_end),target,cap)
        if chosen is not None:
            new_start=round(max(0.,chosen[0]),3);new_end=round(min(duration,chosen[1]),3)
            if 0.1<new_end-new_start<=cap+.2 and new_end-new_start<end-start-.05:
                cue['start'],cue['end']=new_start,new_end
                _clip_words(cue)
                cue['duration_trim']={'length':length,'target':round(target,3),'cap':round(cap,3),
                                      'before':before,'after':[new_start,new_end],'method':'speech_activity'}
                cue['needs_review']=True
                if cue.get('alignment'):cue['alignment']={**cue['alignment'],'duration_trimmed':True}
                repairs.append({'id':cue.get('display_id',cue.get('id')),'method':'speech_activity',
                                'before':before,'after':[new_start,new_end]})
                continue
        if 0.1<input_span<=cap*1.2 and input_span<end-start-.05:
            cue['start'],cue['end']=round(input_start,3),round(input_end,3)
            cue['words']=[]
            for key in ('pause_before','pause_after','audio_silences'):cue.pop(key,None)
            if cue.get('alignment'):cue['alignment']={**cue['alignment'],'stale':True}
            cue['alignment_error']='对齐时长与字幕长度不符，已保留原时间，请试听复核'
            cue['needs_review']=True
            cue['duration_trim']={'length':length,'target':round(target,3),'cap':round(cap,3),
                                  'before':before,'after':[round(input_start,3),round(input_end,3)],
                                  'method':'input_fallback'}
            repairs.append({'id':cue.get('display_id',cue.get('id')),'method':'input_fallback',
                            'before':before,'after':[round(input_start,3),round(input_end,3)]})
            continue
        cue['needs_review']=True
        cue['duration_trim']={'length':length,'target':round(target,3),'cap':round(cap,3),
                              'before':before,'after':before,'method':'unresolved'}
        # 原时间同样不可信时不能继续拿拉长的词时间拆条，或计为成功对齐。
        from .alignment import invalidate_alignment
        invalidate_alignment(cue,'字幕时长异常且原时间不可靠，未能定位，请试听复核')
    return repairs


def synchronize_word_times(cue):
    """边界变动后同步词时间；若会删掉整个字，停用整条词表并明确提示。"""
    words=cue.get('words') or []
    if not words:return cue
    clipped=[{**word,'start':max(float(cue['start']),float(word['start'])),
              'end':min(float(cue['end']),float(word['end']))} for word in words]
    if any(word['end']<=word['start'] for word in clipped):
        cue['words']=[];cue['needs_review']=True
        cue['word_timing_error']='词时间超出字幕范围或顺序异常，已停用旧词时间，请重新对齐'
        cue.setdefault('alignment_error',cue['word_timing_error'])
        if cue.get('alignment'):cue['alignment']={**cue['alignment'],'stale':True}
        for key in ('pause_before','pause_after','audio_silences'):cue.pop(key,None)
    else:
        cue['words']=clipped
    return cue


def acoustic_features(audio, sample_rate=16000, cancel_event=None):
    samples=np.asarray(audio,dtype=np.float32).reshape(-1)
    frame=max(16,round(sample_rate*.02));hop=max(8,round(sample_rate*.01))
    if len(samples)<frame:
        return np.array([]),np.array([]),hop/sample_rate,frame/sample_rate
    windows=np.lib.stride_tricks.sliding_window_view(samples,frame)[::hop]
    rms=np.empty(len(windows));flux=np.zeros(len(windows));previous=None
    taper=np.hanning(frame)
    for offset in range(0,len(windows),1024):
        if cancel_event is not None and cancel_event.is_set():
            from .asr import Cancelled
            raise Cancelled()
        block=windows[offset:offset+1024]
        rms[offset:offset+len(block)]=np.sqrt(np.mean(block*block,axis=1))
        spectrum=np.abs(np.fft.rfft(block*taper,axis=1))
        preceding=np.vstack([previous if previous is not None else spectrum[0],spectrum[:-1]])
        flux[offset:offset+len(block)]=np.maximum(0,spectrum-preceding).sum(axis=1)/(preceding.sum(axis=1)+1e-9)
        previous=spectrum[-1]
    return rms,flux,hop/sample_rate,frame/sample_rate


def global_noise_floor(audio, sample_rate=16000):
    """整段音频的稳健底噪基准；先按1秒聚合，孤立低谷不决定基准。"""
    samples=np.asarray(audio,dtype=np.float32).reshape(-1)
    frame=max(16,round(sample_rate*.02));levels=[]
    for offset in range(0,len(samples),sample_rate):
        block=samples[offset:offset+sample_rate]
        size=len(block)//frame*frame
        if size<frame:continue
        frames=block[:size].reshape(-1,frame)
        energy=np.sqrt(np.mean(frames*frames,axis=1))
        levels.append(float(np.percentile(energy,20)))
    return float(np.percentile(levels,20)) if levels else 0.


def speech_intervals(rms,flux,hop=.01,frame=.02,baseline=None):
    """噪声底随局部音频自适应；高阈值进入、低阈值退出，抑制抖动。"""
    if not len(rms):return [],None
    floor,voice=np.percentile(rms,[20,85])
    # 短暂停顿可能不足整句的20%；用持续60ms的安静窗口估计底噪，
    # 不用单个最低采样点，避免把正常辅音/瞬间能量低谷当作噪声底。
    quiet_needed=max(1,round(.06/hop))
    if len(rms)>=quiet_needed:
        energy=np.asarray(rms,dtype=np.float64)**2
        sums=np.r_[0.,np.cumsum(energy)]
        quiet_floor=np.sqrt(max(0.,float(np.percentile((sums[quiet_needed:]-sums[:-quiet_needed])/quiet_needed,2))))
        floor=min(floor,quiet_floor)
    local_floor=float(floor)
    if baseline is not None:
        floor=max(floor,float(baseline)*.5)
    if voice<1e-5 or voice<max(1e-8,floor)*1.8:
        return [],None  # 静音或持续音乐/噪声：不给出可靠语音边界。
    # 自适应死区仅作用于边界判断，原始样本不置零、不送降噪音频给模型。
    dead_zone=max(1e-5,float(floor)*1.5)
    high=max(dead_zone,floor+.28*(voice-floor));low=max(dead_zone,floor+.14*(voice-floor))
    flux_gate=max(.03,float(np.percentile(flux,85)))
    minimum=max(1,round(.04/hop))
    intervals=[];start=None;quiet=0;last=0
    for i,energy in enumerate(rms):
        onset=energy>=high or (energy>=low and flux[i]>=flux_gate)
        if start is None:
            if onset:start=i;last=i;quiet=0
        elif energy>=low:
            last=i;quiet=0
        else:
            quiet+=1
            if quiet>=quiet_needed:
                if last-start+1>=minimum:intervals.append((start*hop,last*hop+frame))
                start=None;quiet=0
    if start is not None and last-start+1>=minimum:intervals.append((start*hop,last*hop+frame))
    return intervals,{'on':float(high),'off':float(low),'noise_floor':float(floor),
                      'dead_zone':dead_zone,'voice':float(voice),'global_floor':baseline,
                      'local_floor':local_floor}


def refine_boundaries(cues,audio,sample_rate=16000,max_shift=.18,cancel_event=None):
    """普通边界最多移动180ms；明确的连续尾部静音可单独收紧。

    只对有字幕覆盖的时间范围计算声学特征，避免对整条音频做 FFT。
    裁剪网格对齐全片10ms步长，保留上一帧以正确计算频谱变化。
    """
    if not cues:return cues
    duration=len(audio)/sample_rate
    baseline=global_noise_floor(audio,sample_rate)
    t_min=max(0,min(float(c['start']) for c in cues)-0.35)
    t_max=min(duration,max(float(c['end']) for c in cues)+0.35)
    hop_samples=max(8,round(sample_rate*.01));frame_samples=max(16,round(sample_rate*.02))
    first_frame=max(0,int(t_min*sample_rate)//hop_samples-1)
    first_sample=first_frame*hop_samples
    last_sample=min(len(audio),int(np.ceil(t_max*sample_rate/hop_samples))*hop_samples+frame_samples)
    rms,flux,hop,frame=acoustic_features(audio[first_sample:last_sample],sample_rate,cancel_event)
    if not len(rms):return cues
    result=[]
    for i,original in enumerate(cues):
        cue=dict(original);start=float(cue['start']);end=float(cue['end'])
        cue.setdefault('asr_original',{'text':cue['text'],'start':start,'end':end,'id':cue.get('id',i+1)})
        cue.setdefault('original_start',start);cue.setdefault('original_end',end)
        left=max(t_min,start-.35);right=min(t_max,end+.35)
        a=max(0,int(left/hop)-first_frame);b=min(len(rms),int(right/hop)+1-first_frame)
        intervals,threshold=speech_intervals(rms[a:b],flux[a:b],hop,frame,baseline)
        origin=first_frame*hop+a*hop
        all_intervals=[(origin+x,origin+y) for x,y in intervals]
        intervals=[(x,y) for x,y in all_intervals if y>start-max_shift and x<end+max_shift]
        candidate_start,candidate_end=start,end
        quiet_trim=False
        vad_start=vad_end=None
        vad_start_applied=vad_end_applied=False
        words=cue.get('words') or []
        if intervals:
            beginnings=[x for x,_ in intervals if start<=x<=start+max_shift]
            endings=[y for _,y in intervals if abs(y-end)<=max_shift]
            if beginnings:candidate_start=min(beginnings,key=lambda t:abs(t-start))
            if endings:candidate_end=min(endings,key=lambda t:abs(t-end))
            if words:
                candidate_start=min(candidate_start,float(words[0]['start']))
                candidate_end=max(candidate_end,float(words[-1]['end']))
            # 旧词尾可能被模型拖进持续底噪；有足够语音/底噪反差和连续静音
            # 才允许超出微调范围收尾，并且绝不越过最后一个词的起点。
            covered=[(x,y) for x,y in intervals if x<end and y>start]
            if covered and threshold and threshold['voice']>=max(1e-5,threshold['noise_floor']*3):
                stop=max(y for _,y in covered)
                last_start=max(float(word['start']) for word in words) if words else start
                if end-stop>=.12 and stop>=max(start+.02,last_start+.02) and (words or end-stop<=max_shift):
                    candidate_end=min(candidate_end,stop+.01)
                    quiet_trim=end-candidate_end>max_shift
        # 音乐、噪声或音效常和后面的说话连成一段能量，RMS 分不开。
        # 只有词时间和 VAD 都给出可靠证据时才允许越过 max_shift 收紧；
        # 任何不确定、模型缺失或会裁掉整词的情况都保留原时间。
        if words:
            vad_low,vad_frames=_vad_region(audio,sample_rate,start,end)
            vad_start=_vad_start(audio,sample_rate,start,end,words,vad_frames,vad_low)
            vad_end=_vad_end(audio,sample_rate,start,end,words,vad_frames,vad_low)
            # 只做收紧、不做扩张：VAD 证据不足或把音效误判成人声时，
            # 结果不会比原来的 RMS/词时间更差。首尾词本身仍由 50ms 下限保护。
            vad_start_applied=(vad_start is not None and vad_start>candidate_start
                               and vad_start<=candidate_end-.05
                               and vad_start<=float(words[0]['end'])-.05)
            if vad_start_applied:candidate_start=vad_start
            vad_end_applied=(vad_end is not None and vad_end<candidate_end
                             and vad_end>=candidate_start+.05
                             and vad_end>=float(words[-1]['start'])+.05)
            if vad_end_applied:candidate_end=vad_end
        if intervals or vad_start_applied or vad_end_applied:
            lower=result[-1]['end'] if result else 0
            upper=float(cues[i+1]['start']) if i+1<len(cues) else duration
            candidate_start=max(lower,0,start,candidate_start)
            candidate_end=min(upper,duration,end+max_shift,candidate_end)
            if candidate_end>candidate_start+.01 and (abs(candidate_start-start)<=max_shift+.001 or vad_start_applied) and (abs(candidate_end-end)<=max_shift+.001 or quiet_trim or vad_end_applied):
                cue['start']=round(candidate_start,3);cue['end']=round(candidate_end,3)
                if words and cue['end']<end:
                    cue['words']=[{**word,'end':min(float(word['end']),cue['end']),
                                   **({'original_end':word.get('original_end',word['end'])} if float(word['end'])>cue['end'] else {})} for word in words]
                if quiet_trim:
                    cue['needs_review']=True
                if end-cue['end']>=.12:
                    cue['pause_after']=cue['end']
        # 显示用静音证据与“是否裁剪时间”分开。对齐已准确落在词尾时，
        # 同样要限制显示延时；使用未裁掉的前后余量验证连续安静。
        if threshold:
            def quiet_between(left,right):
                lo=max(0,int(np.ceil((left-origin)/hop)))
                hi=min(b-a,int(np.floor((right-origin-frame)/hop))+1)
                return hi-lo>=max(1,round(.08/hop)) and np.all(rms[a+lo:a+hi]<threshold['off'])
            stops=[y for x,y in all_intervals if x<end and abs(y-cue['end'])<=max_shift+.02]
            if stops:
                stop=max(stops)
                if quiet_between(max(stop,cue['end'])+.02,min(right,end+.35)):
                    cue['pause_after']=round(max(cue['end'],stop+.01),3)
            onsets=[x for x,y in all_intervals if y>start and abs(x-cue['start'])<=max_shift+.02]
            if onsets:
                onset=min(onsets)
                if quiet_between(max(left,start-.35),min(onset,cue['start'])-.02):
                    cue['pause_before']=round(min(cue['start'],max(0,onset-.01)),3)
        cue['audio_refinement']={'method':'rms_spectral_flux_hysteresis','before_start':start,'before_end':end,
            'status':'adjusted' if cue['start']!=start or cue['end']!=end else 'unchanged' if threshold else 'uncertain',
            'max_shift':max_shift,'thresholds':threshold,'onset_policy':'never_advance_from_energy',
            'long_silence_trim':quiet_trim}
        if vad_start is not None or vad_end is not None:
            cue['audio_refinement']['vad_trim']={
                'start':round(vad_start,3) if vad_start is not None else None,
                'end':round(vad_end,3) if vad_end is not None else None,
                'applied_start':bool(vad_start_applied),'applied_end':bool(vad_end_applied)}
        # 只记录静音证据，不凭静音凭空分配文字；分段仍依赖词时间/分词。
        cue['audio_silences']=[{'start':round(x[1],3),'end':round(y[0],3)} for x,y in zip(intervals,intervals[1:]) if y[0]-x[1]>=.06]
        result.append(cue)
    for left,right in zip(result,result[1:]):
        if left['end']<=right['start']:continue
        lower=max(left['start']+.001,right['start']);upper=min(left['end'],right['end']-.001)
        if lower<=upper:
            boundary=round(min(upper,max(lower,(left['end']+right['start'])/2)),3)
            if max(abs(boundary-left['end']),abs(boundary-right['start']))<=max_shift:
                left['end']=right['start']=boundary
                for cue in (left,right):cue['audio_refinement']['monotonic_adjustment']=True
                continue
        left['needs_review']=right['needs_review']=True
    for cue in result:synchronize_word_times(cue)
    return result
