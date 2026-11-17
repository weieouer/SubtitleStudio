"""对齐词时间的异常信号；用于局部重试和拒绝不可靠的拆句依据。"""
from __future__ import annotations

import math
import unicodedata


def plain(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', str(text)) if c.isalnum()).lower()


def word_timing_issue(words):
    """只判定明显时间异常，不把模型强制返回的文字当作识别正确的证据。"""
    if not words:
        return '未得到可靠词时间'
    previous = None
    for word in words:
        start, end = float(word['start']), float(word['end'])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            return '模型词时间无效'
        length = len(plain(word.get('text', word.get('word', ''))))
        if end-start > max(2.5, length*.7+1.2):
            return '单个词时间异常拉长'
        if previous is not None:
            if start < previous['start']-.001 or end < previous['end']-.001:
                return '模型词时间顺序异常'
            if previous['end']-start>.08+.001:
                return '相邻词时间明显重叠'
            if start-previous['end'] > 3.:
                return '同句词时间跨越过长空档'
        previous = {'start':start, 'end':end}
    return None


def cue_word_slices(cues, words):
    """仅在完整文字及词边界一致时分配重试范围，不按时长猜文字。"""
    texts = [plain(cue['text']) for cue in cues]
    tokens = [plain(word['text']) for word in words]
    if ''.join(texts) != ''.join(tokens) or any(not token for token in tokens):
        return None
    boundaries = {0:0}
    total = 0
    for i, token in enumerate(tokens):
        total += len(token)
        boundaries[total] = i+1
    result = []
    total = 0
    for text in texts:
        end = total+len(text)
        if total not in boundaries or end not in boundaries:
            return None
        result.append((boundaries[total], boundaries[end]))
        total = end
    return result


def boundary_timing_issue(words):
    """首尾分别参考最多三个句内词的节奏；只发现离群，不按均分推算时间。"""
    from statistics import median
    if len(words)<3:
        return None
    for at_end in (False,True):
        edge=words[-1] if at_end else words[0]
        context=words[-4:-1] if at_end else words[1:4]
        rates=[(float(w['end'])-float(w['start']))/max(1,len(plain(w.get('text',w.get('word',''))))) for w in context]
        pace=median(rates)
        edge_rate=(float(edge['end'])-float(edge['start']))/max(1,len(plain(edge.get('text',edge.get('word','')))))
        gap=(float(edge['start'])-float(context[-1]['end']) if at_end else
             float(context[0]['start'])-float(edge['end']))
        if edge_rate>max(.8,pace*3.5) or gap>max(.6,pace*4):
            return ('句尾字词与前面多个字词脱节' if at_end else '句首字词与后续多个字词脱节')
    return None


def cue_timing_issue(text, words, input_span=None):
    """短句按整句检查，避免每个字都没超限、整句却被拖长。只判异常，不猜时间。"""
    issue=word_timing_issue(words)
    if issue:
        return issue
    from .duration import duration_bounds
    length,_,cap=duration_bounds(text)
    span=float(words[-1]['end'])-float(words[0]['start'])
    if 0<length<=6:
        if span>cap+.001:
            return '短句对齐时长与字数不符'
        if (input_span is not None and .1<input_span<=cap
                and span>max(input_span*2.5,input_span+.8)):
            return '短句对齐被异常拉长，需试听确认'
    return boundary_timing_issue(words)
