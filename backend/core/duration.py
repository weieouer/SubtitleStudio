"""字幕时长与文字长度的经验关系。

只用于发现和收紧“几个字却占了好几秒”的对齐异常，不生成字幕时间。
中文对白常见 3-6 字/秒；上限取偏慢语速，并为句内停顿留出余量。
"""
from __future__ import annotations

import re

_SPOKEN = re.compile(r'[0-9A-Za-z㐀-鿿]')
_TARGET_PER_CHAR = 0.30
_CAP_PER_CHAR = 0.55
_CAP_BASE = 0.90
_MIN_CAP = 1.20
_MAX_CAP = 12.0
_MAX_TARGET = 6.0


def spoken_length(text: str) -> int:
    """只数会发声的字、数字和字母，标点、空白不计。"""
    return len(_SPOKEN.findall(text or ''))


def duration_bounds(text: str) -> tuple[int, float, float]:
    """返回（发声字数、期望时长、时长上限），单位秒。

    期望时长用于在多个语音段中选择最接近的一段；上限用于判定字幕是否
    被音效、噪声或相邻声音拉长。2 字以内从 1.2 秒起算，长句允许更慢的
    语速和句内停顿，避免把正常慢速对白误判成异常。
    """
    length = spoken_length(text)
    if not length:
        return 0, 0.0, 0.0
    target = min(_MAX_TARGET, max(0.45, length * _TARGET_PER_CHAR))
    cap = min(_MAX_CAP, max(_MIN_CAP, length * _CAP_PER_CHAR + _CAP_BASE))
    return length, target, cap


def _overlap(start: float, end: float, other_start: float, other_end: float) -> float:
    return max(0.0, min(end, other_end) - max(start, other_start))


def _clusters(runs, max_gap: float = 0.30):
    """把间隔不超过 max_gap 的语音段并成一段，短停顿仍算连续说话。"""
    clusters = []
    for run in sorted(runs):
        if clusters and run[0] - clusters[-1][1] <= max_gap:
            clusters[-1] = (clusters[-1][0], max(clusters[-1][1], run[1]))
        else:
            clusters.append((run[0], run[1]))
    return clusters


def speech_coverage(runs, start: float, end: float) -> float:
    """区间被语音活动覆盖的比例；音效和噪声通常覆盖很低。"""
    if end <= start or not runs:
        return 0.0
    covered = sum(_overlap(run[0], run[1], start, end) for run in _clusters(runs))
    return min(1.0, covered / (end - start))


def speech_near(runs, span):
    """返回与粗时间重叠最多的语音段；用于发现字幕被音效带走的情况。"""
    if not runs or not span:
        return None
    best = None
    best_overlap = 0.0
    for run in _clusters(runs):
        overlap = _overlap(run[0], run[1], span[0], span[1])
        if overlap > best_overlap:
            best, best_overlap = run, overlap
    return best


def trim_to_speech(runs, words, input_span, target: float, cap: float):
    """从语音活动段中选最贴合词时间的一段，返回 (start, end)；无可靠证据返回 None。

    runs 为绝对时间区间；words 为带 start/end 的词时间；input_span 为对齐前
    的粗时间。只有每个词都落在选出的语音段里、且总长不超过字数上限时才返回，
    宁可不改也不硬裁掉文字对应的音频。
    """
    spans = [(float(w['start']), float(w['end'])) for w in words or []
             if float(w.get('end', 0)) > float(w.get('start', 0))]
    if not runs or not spans:
        return None

    def word_overlap(run):
        return sum(_overlap(run[0], run[1], start, end) for start, end in spans)

    best = None
    best_score = 0.0
    for run in _clusters(runs):
        overlap = word_overlap(run)
        if overlap <= 0:
            continue
        length = run[1] - run[0]
        score = overlap - max(0.0, length - target) * 0.15
        if input_span:
            score += 0.35 * _overlap(run[0], run[1], input_span[0], input_span[1])
        if score > best_score:
            best, best_score = run, score
    if best is None or best[1] - best[0] > cap + 0.15:
        return None
    if any(_overlap(start, end, best[0], best[1]) <= 0 for start, end in spans):
        return None
    if best[1] - best[0] < 0.15:
        return None
    return max(0.0, best[0] - 0.02), best[1] + 0.02
