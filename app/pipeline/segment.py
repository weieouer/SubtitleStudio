# -*- coding: utf-8 -*-
"""长句智能分段。

  - 标点、符号、空格、换行都作为断句间隔；
  - 按中文词边界切分，一个字幕内不拆开完整的词；
  - 优先在语音停顿处断句，其次才按最高字数合并；
  - 同一句拆出的字幕默认首尾相接，自然停顿处保留空隙。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import jieba

from .align import AlignedLine
from .text import clean_cue_text, is_spoken

jieba.setLogLevel(logging.WARNING)

SOFT = set("的了是就也还又而但却把被让给在从向对为和与或着过地得")
# 字幕规范：语气词不进字幕。避免误删句子中间的字。
_FILLER_CHARS = "嗯呃额唔啊呀哇哎唉哦噢喔哟诶欸呐咧嘞咯"
FILLERS = set(_FILLER_CHARS)
_TRAILING_FILLERS = re.compile(rf"[{_FILLER_CHARS}]+$")

# 停顿超过该值时直接断句；标点后的轻微停顿用更低的阈值。
PAUSE_GAP = 0.30
PUNCT_PAUSE_GAP = 0.22
_STRONG_BREAK = set("。！？!?…；;.")
_PUNCTUATION = set("。！？!?…；;，,、：:—.·")


def strip_leading_fillers(raw: str) -> str:
    """去掉被停顿隔开的句首语气词，例如「嗯，我知道了」。

    只处理「嗯、哎」后面紧跟符号或空白的情况，避免把「哎呀妈呀」
    这类有情绪的表达整段吃掉。
    """
    index = 0
    while index < len(raw) and raw[index] in FILLERS:
        index += 1
    if index and index < len(raw) and not is_spoken(raw[index]):
        return raw[index:]
    return raw


def strip_fillers(text: str) -> str:
    """去掉句尾语气词；整条只有语气词时返回空串。

    像「你来了呀」只留下实义内容；「吗、呢、吧」这类承担语气的助词
    不在此列，避免把疑问句或祈使句改成陈述句。
    """
    return _TRAILING_FILLERS.sub("", text, count=1).strip()


@dataclass
class Cue:
    text: str
    start: float
    end: float
    line_idx: int
    speaker: str
    confidence: float
    pause_before: bool = False


def _spoken(text: str) -> int:
    return sum(1 for ch in text if is_spoken(ch))


def _split_long(text: str, s: int, e: int, max_chars: int) -> list[tuple[int, int]]:
    """超长单词的兜底切分；正常中文词不会走到这里。"""
    if _spoken(text[s:e]) <= max_chars:
        return [(s, e)]

    positions = [i for i in range(s, e) if is_spoken(text[i])]
    out: list[tuple[int, int]] = []
    start = s

    while True:
        remaining = [p for p in positions if p >= start]
        if len(remaining) <= max_chars:
            out.append((start, e))
            return out

        cut = remaining[max_chars - 1] + 1
        # 不把连续的英文 / 数字从中间切开
        while cut < e and is_spoken(text[cut - 1]) and is_spoken(text[cut]):
            if text[cut - 1].isascii() and text[cut].isascii():
                cut += 1
            else:
                break

        best = cut
        for k in range(cut - 1, max(start, cut - 5), -1):
            if text[k] in SOFT:
                best = k + 1
                break
        if best <= start:
            best = cut
        out.append((start, best))
        start = best


def _word_units(text: str) -> list[tuple[int, int]]:
    """按词切成最小单元；标点和空白只是单元之间的间隔。"""
    units: list[tuple[int, int]] = []
    for _word, s, e in jieba.tokenize(text):
        positions = [p for p in range(s, e) if is_spoken(text[p])]
        if positions:
            units.append((positions[0], positions[-1] + 1))
    return units


def _unit_bounds(
    unit: tuple[int, int], times: dict[int, tuple[float, float]] | None
) -> tuple[float, float] | None:
    if not times:
        return None
    positions = [p for p in range(unit[0], unit[1]) if p in times]
    if not positions:
        return None
    return times[positions[0]][0], times[positions[-1]][1]


def _visible_positions(text: str, s: int, e: int) -> list[int]:
    """返回最终字幕中真正保留的字位置，用于严格确定起始时间。

    句首被标点或空白隔开的语气词会先剔除；括号提示替换成的空格
    不算台词，因此从第一个真正发声的字开始找。
    """
    positions = [p for p in range(s, e) if is_spoken(text[p])]

    lead = 0
    while lead < len(positions) and text[positions[lead]] in FILLERS:
        lead += 1
    if lead:
        last = positions[lead - 1]
        if last + 1 >= e or not is_spoken(text[last + 1]):
            positions = positions[lead:]

    while positions and text[positions[-1]] in FILLERS:
        positions.pop()
    return positions


def split_spans(
    text: str,
    max_chars: int,
    times: dict[int, tuple[float, float]] | None = None,
) -> list[tuple[int, int, bool]]:
    """把一句台词切成字幕片段。

    返回 (start, end, pause_before)。pause_before 表示该段前面是
    自然停顿或句末标点，不应再用「无缝衔接」把它和上一段粘起来。
    """
    units = _word_units(text)
    if not units:
        return []

    # 极长且无法再分词的单元，退回按字切分。
    fine_units: list[tuple[int, int]] = []
    for s, e in units:
        if _spoken(text[s:e]) > max_chars:
            fine_units.extend(_split_long(text, s, e, max_chars))
        else:
            fine_units.append((s, e))

    groups: list[tuple[int, int, bool]] = []
    cur_s, cur_e = fine_units[0]
    cur_units = [fine_units[0]]
    cur_count = _spoken(text[cur_s:cur_e])
    pause_before = False
    prev = fine_units[0]
    prev_bounds = _unit_bounds(prev, times)

    for unit in fine_units[1:]:
        s, e = unit
        count = _spoken(text[s:e])
        separator = text[prev[1] : s]
        bounds = _unit_bounds(unit, times)
        gap = (
            bounds[0] - prev_bounds[1]
            if bounds is not None and prev_bounds is not None
            else None
        )

        strong_break = any(ch in _STRONG_BREAK for ch in separator)
        has_punct = any(ch in _PUNCTUATION for ch in separator)
        threshold = PUNCT_PAUSE_GAP if has_punct else PAUSE_GAP
        soft_pause = (
            gap is not None and gap >= threshold and cur_count >= 2
        )
        overflow = cur_count + count > max_chars

        # 到达上限时别把最后一个小词挤成单字条：从上一组末尾挪一个词过来。
        if (
            overflow
            and not strong_break
            and not soft_pause
            and len(cur_units) > 1
            and count <= 2
        ):
            moved_s, moved_e = cur_units[-1]
            moved_count = _spoken(text[moved_s:moved_e])
            if moved_count + count <= max_chars:
                cur_units.pop()
                cur_e = cur_units[-1][1]
                cur_count -= moved_count
                groups.append((cur_s, cur_e, pause_before))
                cur_s, cur_e = moved_s, e
                cur_units = [(moved_s, moved_e), unit]
                cur_count = moved_count + count
                pause_before = False
                prev = unit
                prev_bounds = bounds
                continue

        if overflow or strong_break or soft_pause:
            groups.append((cur_s, cur_e, pause_before))
            cur_s, cur_e, cur_count = s, e, count
            cur_units = [unit]
            pause_before = strong_break or soft_pause
        else:
            cur_e = e
            cur_count += count
            cur_units.append(unit)

        prev = unit
        prev_bounds = bounds

    groups.append((cur_s, cur_e, pause_before))
    return groups


def _merge_short(
    cues: list[Cue],
    min_duration: float,
    max_chars: int,
    pause_gap: float = PAUSE_GAP,
) -> list[Cue]:
    out: list[Cue] = []
    for cue in cues:
        if out and not cue.pause_before:
            prev = out[-1]
            gap = cue.start - prev.end
            if (
                cue.end - cue.start < min_duration
                and len(prev.text) + len(cue.text) <= max_chars
                and gap <= max(0.12, pause_gap * 0.5)
            ):
                prev.text += cue.text
                prev.end = max(prev.end, cue.end)
                continue
        out.append(cue)

    for cue in out:
        if cue.end - cue.start < min_duration:
            cue.end = cue.start + min_duration
    return out


def _register_names(aligned: list[AlignedLine]) -> None:
    """把角色名加入分词词典，避免把「林雨梦」拆成两个字幕。"""
    for item in aligned:
        name = item.line.speaker.strip()
        if len(name) < 2:
            continue
        for term in {name, name[1:], name[-2:]}:
            if len(term) >= 2:
                jieba.add_word(term, freq=20000)


def _enforce_min_duration(
    cues: list[Cue], min_duration: float, max_chars: int
) -> list[Cue]:
    """交付前的最后一道关卡：保证每条字幕都够长。

    _merge_short 补过最小时长，但后面的跨句裁剪又会把它切短，
    出现一闪而过的字幕。这里依次尝试：向后延长、请下一条让出一点
    时间、向前延长、并入上一条、并入下一条。全程维持「字幕互不
    重叠」和「不超过单行字数」；实在挤不进去就保留原样——超长字幕
    比略短的字幕更难交付。
    """
    if min_duration <= 0 or not cues:
        return cues

    out: list[Cue] = []
    for index, cue in enumerate(cues):
        if cue.end - cue.start >= min_duration:
            out.append(cue)
            continue

        need = min_duration - (cue.end - cue.start)
        nxt = cues[index + 1] if index + 1 < len(cues) else None
        prev = out[-1] if out else None

        # 1. 后面有空位，直接延长。
        if nxt is None or cue.start + min_duration <= nxt.start:
            cue.end = cue.start + min_duration
            out.append(cue)
            continue

        # 2. 请下一条把开头让出一点（最多 0.15 秒，且它自己仍够长）。
        if nxt is not None:
            slack = (nxt.end - nxt.start) - min_duration
            give = min(need, slack, 0.15)
            if give >= need - 1e-9:
                nxt.start += give
                cue.end = cue.start + min_duration
                out.append(cue)
                continue

        # 3. 向前的空隙借。
        floor = prev.end if prev is not None else 0.0
        if cue.end - min_duration >= floor:
            cue.start = cue.end - min_duration
            out.append(cue)
            continue

        # 4. 并入上一条或下一条，但合起来不能超过单行字数；
        #    跨行的字幕不合并，否则一句的文字会被拆到两条记录里。
        if (
            prev is not None
            and prev.line_idx == cue.line_idx
            and len(prev.text) + len(cue.text) <= max_chars
        ):
            prev.text += cue.text
            prev.end = max(prev.end, cue.end)
            continue
        if (
            nxt is not None
            and nxt.line_idx == cue.line_idx
            and len(cue.text) + len(nxt.text) <= max_chars
        ):
            nxt.text = cue.text + nxt.text
            nxt.start = min(nxt.start, cue.start)
            continue

        # 5. 前后都借不到又合不得：保留原样，交给审查清单。
        out.append(cue)

    return out


def make_cues(
    aligned: list[AlignedLine],
    max_chars: int = 14,
    include_os: bool = True,
    no_gap: bool = True,
    max_fill: float = 0.6,
    min_duration: float = 0.55,
    lead_in: float = 0.0,
    tail_delay: float = 0.18,
    whole_lines: bool = False,
) -> list[Cue]:
    """把对齐行生成字幕。

    whole_lines=True 用于字幕提取：每个识别片段完整输出为一条字幕，
    不再按单行字数拆分；对齐模式仍沿用智能分段。
    """
    _register_names(aligned)
    cues: list[Cue] = []

    for item in aligned:
        if item.start is None or item.end is None:
            continue  # 整句没在音频里找到，跳过并交给审查清单
        if item.line.is_os and not include_os:
            continue

        times = {
            t.text_pos: (t.start, t.end)
            for t in item.timed
            if t.start is not None and t.end is not None
        }
        line_cues: list[Cue] = []
        spans = (
            [(0, len(item.line.text), False)]
            if whole_lines
            else split_spans(item.line.text, max_chars, times)
        )

        for s, e, pause_before in spans:
            span_times = [times[p] for p in range(s, e) if p in times]
            if not span_times:
                continue

            visible = [p for p in _visible_positions(item.line.text, s, e) if p in times]
            if not visible:
                continue

            if whole_lines:
                # 整段输出时以真正显示的字为边界，跳过句首括号提示、
                # 被停顿隔开的语气词和句尾语气词。
                raw = item.line.text[visible[0] : visible[-1] + 1]
            else:
                raw = strip_leading_fillers(item.line.text[s:e])
            text = strip_fillers(clean_cue_text(raw))
            if not text:
                continue

            # 起始严格落在第一个真正显示的字上；结尾允许覆盖语气词。
            start = times[visible[0]][0]
            end = span_times[-1][1]
            if end <= start:
                end = start + 0.25

            line_cues.append(
                Cue(
                    text=text,
                    start=start,
                    end=end,
                    line_idx=item.line.idx,
                    speaker=item.line.speaker,
                    confidence=item.ratio,
                    pause_before=pause_before,
                )
            )

        if not line_cues:
            continue

        line_cues = _merge_short(line_cues, min_duration, max_chars)

        if no_gap:
            for a, b in zip(line_cues, line_cues[1:]):
                gap = b.start - a.end
                if not b.pause_before and 0 <= gap <= max_fill:
                    a.end = b.start

        if lead_in:
            for cue in line_cues:
                cue.start = max(0.0, cue.start - lead_in)
                cue.end = max(cue.start, cue.end - lead_in)
        line_cues[-1].end += tail_delay
        cues.extend(line_cues)

    cues.sort(key=lambda c: c.start)
    for a, b in zip(cues, cues[1:]):
        if a.end > b.start:
            a.end = b.start

    # 上面的裁剪会让字幕互不压盖，但可能把时长切得比最小时长还短；
    # 最后统一收尾一次，避免交付一闪而过的字幕。
    cues = _enforce_min_duration(cues, min_duration, max_chars)

    return [c for c in cues if c.end - c.start > 0.05]
