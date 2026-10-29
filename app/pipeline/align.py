# -*- coding: utf-8 -*-
"""把剧本对白对齐到 ASR 的字级时间轴。

  1. 剧本只保留发音字符，形成字符流 S；
  2. ASR 的词展开成逐字时间序列 A（每个字带时间）；
  3. 用全局序列比对（Needleman-Wunsch）把 S 和 A 对上。
     同音字算半匹配，识别成"林帆"不影响"林凡"的对轴；
  4. 匹配上的剧本字继承 ASR 字的时间，没匹配上的在相邻字之间插值；
  5. 没匹配到任何剧本字的 ASR 片段 = 咳嗽、嗯嗯、环境声，丢弃。

文字永远来自剧本，ASR 只贡献时间。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Sequence

from pypinyin import Style, lazy_pinyin

from .script_parser import DialogueLine
from .text import is_spoken

_CJK_RE = re.compile(r"[一-鿿]")

_PY_CACHE: dict[str, str] = {}
_PY_ID: dict[str, int] = {}

# numpy 只是加速器：没有它就退回纯 Python 密集对齐，功能不变。
try:  # pragma: no cover - 取决于安装环境
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None

# 单个 DP 矩阵的单元格上限。一集半小时的剧本约 6000-12000 字，
# 乘上 ASR 字数（通常更多）动辄上亿格，纯 Python 密集对齐既慢又吃内存，
# 因此超过该值就按剧本分块对齐，块内仍用完整的同音加权比对。
NW_MAX_CELLS = 4_000_000
CHUNK_CHARS = 1200     # 每个对齐块采信的剧本字数
CONTEXT_CHARS = 240    # 块尾额外带的上下文字数，只用于减少边界误配
ASR_SLACK = 600        # ASR 窗口相对块长的额外余量（噪声、重复、口误）


def _pinyin(ch: str) -> str:
    cached = _PY_CACHE.get(ch)
    if cached is not None:
        return cached
    if _CJK_RE.fullmatch(ch):
        parts = lazy_pinyin(ch, style=Style.NORMAL, errors="ignore")
        value = parts[0] if parts else ch
    else:
        value = ch.lower()
    _PY_CACHE[ch] = value
    return value


def _score(a: str, b: str) -> int:
    """字相同 3 分，同音 2 分，不同 -1 分。"""
    if a == b:
        return 3
    if a.lower() == b.lower():
        return 3
    if _CJK_RE.fullmatch(a) and _CJK_RE.fullmatch(b) and _pinyin(a) == _pinyin(b):
        return 2
    return -1


@dataclass
class ScriptChar:
    ch: str
    line_idx: int
    text_pos: int


@dataclass
class AsrChar:
    ch: str
    start: float
    end: float
    seg_idx: int


@dataclass
class TimedChar:
    ch: str
    line_idx: int
    text_pos: int
    start: float | None = None
    end: float | None = None
    matched: bool = False


@dataclass
class AlignedLine:
    line: DialogueLine
    start: float | None
    end: float | None
    ratio: float
    matched_chars: int
    total_chars: int
    timed: list[TimedChar]


@dataclass
class NoiseSegment:
    seg_idx: int
    start: float
    end: float
    text: str


@dataclass
class AlignmentResult:
    lines: list[AlignedLine]
    asr_chars: list[AsrChar]
    noise: list[NoiseSegment]
    total_matched: int
    total_chars: int
    # dense=整段密集对齐；chunked=分块对齐（长剧本，质量不减）；
    # difflib=没有 numpy 时的降级路径，同音匹配会丢失。
    mode: str = "dense"


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def script_chars_from_lines(lines: Sequence[DialogueLine]) -> list[ScriptChar]:
    out: list[ScriptChar] = []
    for line_idx, line in enumerate(lines):
        for text_pos, ch in enumerate(line.text):
            if is_spoken(ch):
                out.append(ScriptChar(ch=ch, line_idx=line_idx, text_pos=text_pos))
    return out


def asr_chars_from_segments(segments: Sequence[Any]) -> list[AsrChar]:
    """把 faster-whisper 的 segments/words 展开成逐字时间序列。"""
    out: list[AsrChar] = []

    for seg_idx, seg in enumerate(segments):
        seg_start = float(_get(seg, "start", 0.0) or 0.0)
        seg_end = float(_get(seg, "end", seg_start) or seg_start)

        words = _get(seg, "words", None)
        entries: list[tuple[str, float, float]] = []
        if words:
            for word in words:
                text = _get(word, "word", "") or ""
                ws = float(_get(word, "start", seg_start) or seg_start)
                we = float(_get(word, "end", seg_end) or seg_end)
                entries.append((text, ws, we))
        else:
            entries.append((_get(seg, "text", "") or "", seg_start, seg_end))

        for text, ws, we in entries:
            chars = [c for c in text if is_spoken(c)]
            if not chars:
                continue
            ws = max(0.0, ws)
            we = max(we, ws + 0.02)
            step = (we - ws) / len(chars)
            for k, ch in enumerate(chars):
                out.append(
                    AsrChar(
                        ch=ch,
                        start=ws + step * k,
                        end=ws + step * (k + 1),
                        seg_idx=seg_idx,
                    )
                )
    return out


def _difflib_pairs(s: Sequence[str], a: Sequence[str]) -> list[tuple[int, int]]:
    matcher = SequenceMatcher(None, s, a, autojunk=False)
    pairs: list[tuple[int, int]] = []
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            pairs.append((block.a + k, block.b + k))
    return pairs


def _nw_pairs_dense(s: Sequence[str], a: Sequence[str]) -> list[tuple[int, int]]:
    """纯 Python 密集比对，短剧本走这条，行为与历史版本一致。"""
    n, m = len(s), len(a)
    if n == 0 or m == 0:
        return []

    prev = [-j for j in range(m + 1)]
    rows = [prev]
    for i in range(1, n + 1):
        si = s[i - 1]
        row = [-i] + [0] * m
        for j in range(1, m + 1):
            diag = prev[j - 1] + _score(si, a[j - 1])
            up = prev[j] - 1          # 剧本字没在音频里出现
            left = row[j - 1] - 1     # 音频里多出来的字（噪声）
            row[j] = max(diag, up, left)
        rows.append(row)
        prev = row

    pairs: list[tuple[int, int]] = []
    # 半全局对齐：剧本必须全部对上，但音频末尾多出来的字（噪声、静音）
    # 不罚分。若强制吃到窗口末尾，窗口比实际需要长时，最优解会把字幕
    # 一路拉长去"吃掉"末尾，整段的时间映射都会漂移。
    row_n = rows[n]
    j = max(range(m + 1), key=lambda k: row_n[k])
    i = n
    while i > 0 and j > 0:
        sc = _score(s[i - 1], a[j - 1])
        if sc > 0 and rows[i][j] == rows[i - 1][j - 1] + sc:
            pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif rows[i][j] == rows[i][j - 1] - 1:
            j -= 1  # 优先丢弃音频噪声
        elif rows[i][j] == rows[i - 1][j] - 1:
            i -= 1
        else:
            i -= 1
            j -= 1
    pairs.reverse()
    return pairs


def _char_codes(seq: Sequence[str]):
    """统一大小写后的码点；与 _score 的 a==b / a.lower()==b.lower() 等价。"""
    codes = []
    for ch in seq:
        low = ch.lower()
        codes.append(ord(low) if len(low) == 1 else ord(ch))
    return _np.array(codes, dtype=_np.int32)


def _pinyin_ids(seq: Sequence[str]):
    out = []
    for ch in seq:
        py = _pinyin(ch)
        pid = _PY_ID.get(py)
        if pid is None:
            pid = len(_PY_ID)
            _PY_ID[py] = pid
        out.append(pid)
    return _np.array(out, dtype=_np.int32)


def _score_matrix(s: Sequence[str], a: Sequence[str]):
    """整块打分矩阵：字相同 3，同音 2，不同 -1。与 _score 逐格等价。"""
    n, m = len(s), len(a)
    matrix = _np.full((n, m), -1, dtype=_np.int8)
    if n == 0 or m == 0:
        return matrix

    equal = _char_codes(s)[:, None] == _char_codes(a)[None, :]
    matrix[equal] = 3

    same_py = _pinyin_ids(s)[:, None] == _pinyin_ids(a)[None, :]
    if same_py.any():
        s_cjk = _np.array([bool(_CJK_RE.fullmatch(c)) for c in s], dtype=bool)
        a_cjk = _np.array([bool(_CJK_RE.fullmatch(c)) for c in a], dtype=bool)
        matrix[same_py & ~equal & s_cjk[:, None] & a_cjk[None, :]] = 2
    return matrix


def _nw_pairs_numpy(s: Sequence[str], a: Sequence[str]) -> list[tuple[int, int]]:
    """numpy 版密集比对。

    递推式 row[j] = max(diag, up, row[j-1]-1) 里只有左向是行内扫描，
    可以用一次 maximum.accumulate 把整行向量化，因此每行只需常数次
    数组运算，比纯 Python 快一个量级；矩阵仍完整保留用于回溯。
    """
    n, m = len(s), len(a)
    if n == 0 or m == 0:
        return []

    scores = _score_matrix(s, a)
    offsets = _np.arange(m + 1, dtype=_np.int32)
    dp = _np.empty((n + 1, m + 1), dtype=_np.int32)
    dp[0] = -offsets
    prev = dp[0]

    for i in range(1, n + 1):
        row = _np.empty(m + 1, dtype=_np.int32)
        row[0] = -i
        if m:
            diag = prev[:m] + scores[i - 1]
            up = prev[1:] - 1
            base = _np.maximum(diag, up)
            cand = _np.empty(m + 1, dtype=_np.int32)
            cand[0] = -i
            cand[1:] = base
            # row[j] = max_{k<=j}(base[k] - (j-k))
            row = _np.maximum.accumulate(cand + offsets) - offsets
            row[0] = -i
        dp[i] = row
        prev = row

    pairs: list[tuple[int, int]] = []
    # 半全局对齐：剧本必须全部对上，音频末尾多出来的字不罚分。
    # 参见 _nw_pairs_dense 里的说明——分块窗口通常比实际需要长，
    # 不这样做会把字幕拉长去"吃掉"末尾，时间映射整体漂移。
    last_row = dp[n]
    j = int(last_row.argmax())
    i = n
    while i > 0 and j > 0:
        current = int(dp[i, j])
        sc = int(scores[i - 1, j - 1])
        if sc > 0 and current == int(dp[i - 1, j - 1]) + sc:
            pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif current == int(dp[i, j - 1]) - 1:
            j -= 1  # 优先丢弃音频噪声
        elif current == int(dp[i - 1, j]) - 1:
            i -= 1
        else:
            i -= 1
            j -= 1
    pairs.reverse()
    return pairs


def _nw_pairs_chunked(s: Sequence[str], a: Sequence[str]) -> list[tuple[int, int]]:
    """按剧本分块顺序对齐，块与块之间用 ASR 游标衔接。

    长剧本一次做完整 DP 需要 O(n*m) 内存（半小时一集上百亿格），
    而剧本和音频本来就是按时间顺序一一对应的，所以切成 1200 字的块、
    每块只看一段 ASR 窗口就够。块尾多带 240 字上下文，避免边界上的字
    因为看不到后文而被误判为不匹配。
    """
    n, m = len(s), len(a)
    if n == 0 or m == 0:
        return []

    pairs: list[tuple[int, int]] = []
    cursor = 0          # 下一个还没被消费的 ASR 字
    ratio = 1.0         # ASR 字数 / 剧本字数 的经验倍率，用于估窗口长度
    start = 0

    while start < n and cursor < m:
        owned = min(CHUNK_CHARS, n - start)
        end = min(n, start + owned + CONTEXT_CHARS)
        block = s[start:end]

        need = int(len(block) * ratio) + ASR_SLACK
        window_end = min(m, cursor + max(need, len(block)))
        local = _nw_pairs_numpy(block, a[cursor:window_end])

        # 只采信属于本块的字；上下文部分的匹配留给下一块重新判。
        used = [(si, aj) for si, aj in local if si < owned]
        for si, aj in used:
            pairs.append((start + si, cursor + aj))

        if used:
            last_si, last_aj = used[-1]
            cursor += last_aj + 1
            span = last_si + 1
            if span > 0:
                measured = (last_aj + 1) / span
                ratio = min(3.0, max(0.5, ratio * 0.7 + measured * 0.3))
        else:
            # 整块一个字都没配上：按经验倍率往前挪，避免卡死。
            cursor += max(1, int(owned * ratio))
        start += owned

    return pairs


def _nw_pairs_with_mode(
    s: Sequence[str], a: Sequence[str]
) -> tuple[list[tuple[int, int]], str]:
    n, m = len(s), len(a)
    if n == 0 or m == 0:
        return [], "dense"
    if n * m <= NW_MAX_CELLS:
        return _nw_pairs_dense(s, a), "dense"
    if _np is not None:
        return _nw_pairs_chunked(s, a), "chunked"
    # 没有 numpy 时只能整体退回 difflib：同音匹配会丢，界面要提示。
    return _difflib_pairs(s, a), "difflib"


def _fill_times(timed: list[TimedChar]) -> None:
    """在每句内部，为没匹配上的字插值出时间。"""
    by_line: dict[int, list[int]] = {}
    for idx, item in enumerate(timed):
        by_line.setdefault(item.line_idx, []).append(idx)

    for idxs in by_line.values():
        matched = [k for k in idxs if timed[k].matched]
        if not matched:
            continue

        durations = [
            timed[k].end - timed[k].start
            for k in matched
            if timed[k].end is not None
            and timed[k].start is not None
            and timed[k].end > timed[k].start
        ]
        avg = sum(durations) / len(durations) if durations else 0.2
        avg = min(max(avg, 0.06), 0.6)

        first_pos = idxs.index(matched[0])
        t0 = timed[matched[0]].start or 0.0
        for off, k in enumerate(reversed(idxs[:first_pos]), start=1):
            s = max(0.0, t0 - avg * off)
            timed[k].start = s
            timed[k].end = min(t0, s + avg)

        for p0, p1 in zip(matched, matched[1:]):
            i0 = idxs.index(p0)
            i1 = idxs.index(p1)
            if i1 - i0 <= 1:
                continue
            t_start = timed[p0].end if timed[p0].end is not None else timed[p0].start
            t_end = timed[p1].start if timed[p1].start is not None else timed[p1].end
            t_start = t_start or 0.0
            t_end = max(t_end or t_start, t_start)
            step = (t_end - t_start) / (i1 - i0)
            for off, k in enumerate(idxs[i0 + 1 : i1], start=1):
                timed[k].start = t_start + step * (off - 1)
                timed[k].end = t_start + step * off

        last_pos = idxs.index(matched[-1])
        t_end = timed[matched[-1]].end or timed[matched[-1]].start or 0.0
        for off, k in enumerate(idxs[last_pos + 1 :], start=1):
            s = t_end + avg * (off - 1)
            timed[k].start = s
            timed[k].end = s + avg


def align(lines: Sequence[DialogueLine], segments: Sequence[Any]) -> AlignmentResult:
    script_chars = script_chars_from_lines(lines)
    asr_chars = asr_chars_from_segments(segments)
    pairs, mode = _nw_pairs_with_mode(
        [c.ch for c in script_chars], [c.ch for c in asr_chars]
    )

    timed = [
        TimedChar(ch=c.ch, line_idx=c.line_idx, text_pos=c.text_pos)
        for c in script_chars
    ]
    matched_segments: set[int] = set()
    for si, ai in pairs:
        ac = asr_chars[ai]
        timed[si].start = ac.start
        timed[si].end = ac.end
        timed[si].matched = True
        matched_segments.add(ac.seg_idx)

    _fill_times(timed)

    # 一次遍历按行分组；长剧本下逐行全表扫描会是 O(行数 × 总字数)。
    by_line: dict[int, list[TimedChar]] = {}
    for item in timed:
        by_line.setdefault(item.line_idx, []).append(item)

    aligned_lines: list[AlignedLine] = []
    for line_idx, line in enumerate(lines):
        chars = by_line.get(line_idx, [])
        total = len(chars)
        matched_count = sum(1 for t in chars if t.matched)
        starts = [t.start for t in chars if t.start is not None]
        ends = [t.end for t in chars if t.end is not None]
        start = min(starts) if starts else None
        end = max(ends) if ends else None
        if start is not None and end is not None and end <= start:
            end = start + 0.3
        ratio = matched_count / total if total else 0.0
        aligned_lines.append(
            AlignedLine(
                line=line,
                start=start,
                end=end,
                ratio=ratio,
                matched_chars=matched_count,
                total_chars=total,
                timed=chars,
            )
        )

    noise: list[NoiseSegment] = []
    for seg_idx, seg in enumerate(segments):
        if seg_idx in matched_segments:
            continue
        text = str(_get(seg, "text", "") or "").strip()
        start = float(_get(seg, "start", 0.0) or 0.0)
        end = float(_get(seg, "end", start) or start)
        if end - start < 0.15:
            continue
        noise.append(NoiseSegment(seg_idx=seg_idx, start=start, end=end, text=text))

    return AlignmentResult(
        lines=aligned_lines,
        asr_chars=asr_chars,
        noise=noise,
        total_matched=sum(1 for t in timed if t.matched),
        total_chars=len(timed),
        mode=mode,
    )
