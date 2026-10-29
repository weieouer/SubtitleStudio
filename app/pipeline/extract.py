# -*- coding: utf-8 -*-
"""字幕提取：不依赖剧本，直接把识别人声变成字幕行。

ASR 的每个片段先还原成「带字级时间的整行文本」，再交给统一的分段器。
方括号里的音效/注释整段替换成等长空格，保留位置映射但不进入字幕。
"""
from __future__ import annotations

import re
from typing import Any

from .align import AlignedLine, TimedChar
from .script_parser import DialogueLine
from .segment import clean_cue_text, strip_fillers, strip_leading_fillers
from .text import is_spoken

# 与剧本解析保持一致：（）【】[] 等括号里的内容只是提示，不是台词。
_INLINE_NOTE_RE = re.compile(r"[（(【\[〔{][^）)】\]〕}]*[）)】\]〕}]")


def _field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _seconds(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _line_from_segment(segment: Any, idx: int) -> AlignedLine | None:
    seg_start = _seconds(_field(segment, "start")) or 0.0
    seg_end = _seconds(_field(segment, "end")) or seg_start
    raw_parts: list[str] = []
    times: dict[int, tuple[float, float]] = {}
    position = 0

    words = _field(segment, "words") or []
    for word in words:
        text = str(_field(word, "word", "") or "")
        if not text:
            continue

        start = _seconds(_field(word, "start"))
        end = _seconds(_field(word, "end"))
        spoken = [k for k, ch in enumerate(text) if is_spoken(ch)]
        if not spoken:
            raw_parts.append(text)
            position += len(text)
            continue

        if start is None or end is None or end <= start:
            start, end = seg_start, seg_end
        step = (end - start) / len(spoken)
        for order, offset in enumerate(spoken):
            times[position + offset] = (
                start + step * order,
                start + step * (order + 1),
            )
        raw_parts.append(text)
        position += len(text)

    raw = "".join(raw_parts)
    if not raw or not times:
        # 没有词级时间戳时退回片段级时间，按字平均分配。
        raw = str(_field(segment, "text", "") or "")
        spoken = [k for k, ch in enumerate(raw) if is_spoken(ch)]
        if not spoken:
            return None
        if seg_end <= seg_start:
            seg_end = seg_start + 0.25 * len(spoken)
        step = (seg_end - seg_start) / len(spoken)
        times = {
            offset: (
                seg_start + step * order,
                seg_start + step * (order + 1),
            )
            for order, offset in enumerate(spoken)
        }

    # 括号内容换成等长空格：位置不变，时间映射还在，但不会变成字幕。
    raw = _INLINE_NOTE_RE.sub(lambda match: " " * len(match.group(0)), raw)

    timed = [
        TimedChar(
            ch=raw[offset],
            line_idx=idx,
            text_pos=offset,
            start=start,
            end=end,
            matched=True,
        )
        for offset, (start, end) in sorted(times.items())
        if offset < len(raw) and is_spoken(raw[offset])
    ]
    if not timed:
        return None

    visible = strip_fillers(clean_cue_text(strip_leading_fillers(raw)))
    if not visible:
        return None

    line = DialogueLine(idx=idx, speaker="", note="", is_os=False, text=raw)
    return AlignedLine(
        line=line,
        start=timed[0].start,
        end=timed[-1].end,
        ratio=1.0,
        matched_chars=len(timed),
        total_chars=len(timed),
        timed=timed,
    )


def extraction_lines(segments: list[Any]) -> list[AlignedLine]:
    """把 ASR 片段转成可复用剧本分段器的对齐行。"""
    lines: list[AlignedLine] = []
    for idx, segment in enumerate(segments, start=1):
        item = _line_from_segment(segment, idx)
        if item is not None:
            lines.append(item)
    return lines
