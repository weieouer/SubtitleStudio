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
