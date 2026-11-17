"""字幕智能分段：按字符数上限、标点和分词，不丢字符。"""
from __future__ import annotations

import re
import math
import unicodedata
from bisect import bisect_right

try:
    import jieba  # type: ignore
    jieba.setLogLevel(60)
except Exception:  # pragma: no cover - jieba 缺失时退化为按字切分
    jieba = None

PUNCT = "，。！？；：、,.!?;:"
SENTENCE_RE = re.compile(rf"[^{PUNCT}]*[{PUNCT}]*")
FALLBACK_DELIMS = "，。！？；：、,.!?;:）)】」』"
CLOSERS = set("）)】」』”\"'")
TERMINAL = ("。", "！", "？", "!", "?", "；", ";")
# 句首/句尾不足这个字数的碎片不单独成条；这是字幕，不是逐字打印。
EDGE_MIN_WIDTH = 3


def char_width(ch: str) -> float:
    if ch in "\r\n":
        return 0.0
    return 1.0


def text_width(text: str) -> float:
    # 与逐字符调用 char_width 等价；计数实现避免长字幕在 DP 和分段中反复建生成器。
    return len(text) - text.count("\n") - text.count("\r")


def significant_width(text: str) -> int:
    """只数字母/数字，标点空白不算；判断“一两个字”的碎片时不能把标点算进去。"""
    return sum(1 for ch in text if unicodedata.category(ch)[0] in ('L', 'N'))


def _ends_with_punct(text: str) -> bool:
    """去掉右引号/右括号后看最后一个字符是不是分词标点。"""
    text = (text or '').rstrip().rstrip(''.join(CLOSERS)).rstrip()
    return bool(text) and text[-1] in PUNCT


def _edge_is_fragment(edge: str, rest: str) -> bool:
    """句首/句尾只有一两个字、且其余部分明显更长，才视为要合回的碎片。"""
    short, long = significant_width(edge), significant_width(rest)
    return short < EDGE_MIN_WIDTH and long >= 2 * short


def _split_long_token(token: str, max_width: float) -> list[str]:
    """单个词超过宽度时按字符硬切，英文尽量保持完整。"""
    if text_width(token) <= max_width:
        return [token]
    out: list[str] = []
    buf: list[str] = []
    width = 0.0
    for ch in token:
        w = char_width(ch)
        if buf and width + w > max_width:
            out.append("".join(buf))
            buf = [ch]
            width = w
        else:
            buf.append(ch)
            width += w
    if buf:
        out.append("".join(buf))
    return out


def _rebalance_merge(left: str, right: str, max_width: float, min_width: int) -> list[str]:
    """合回碎片后若超宽，在附近重新找切点，避免又切出一个单字。"""
    text = left + right
    if text_width(text) <= max_width:
        return [text]
    best = None
    for cut in range(1, len(text)):
        head, tail = text[:cut], text[cut:]
        if (text_width(head) <= max_width and text_width(tail) <= max_width
                and significant_width(head) >= min_width and significant_width(tail) >= min_width):
            best = cut
    return [text[:best], text[best:]] if best is not None else [text]


def _merge_tiny_chunks(chunks: list[str], max_width: float = 20.0, min_width: int = 2) -> list[str]:
    """宽度分段也不能留下单个字的孤行；宁可让相邻行多一个字。"""
    out: list[str] = []
    for chunk in chunks:
        if out and (significant_width(chunk) < min_width or significant_width(out[-1]) < min_width):
            out[-1:] = _rebalance_merge(out[-1], chunk, max_width, min_width)
        else:
            out.append(chunk)
    return out


def split_text(text: str, max_width: float = 20.0, pause_bonus: dict | None = None) -> list[str]:
    """把一条字幕切成若干行，不丢字符。"""
    text = text or ""
    if not text or text_width(text) <= max_width:
        return [text] if text else []

    parts = [p for p in SENTENCE_RE.findall(text) if p]
    if not parts:
        parts = [text]

    tokens: list[str] = []
    for part in parts:
        if jieba is not None:
            tokens.extend(t for t in jieba.lcut(part) if t)
        else:
            tokens.extend(part)

    # DP 在词边界、标点和停顿中选最小代价路径；仅超长词允许内部切分。
    boundaries={0};offset=0
    for token in tokens:
        if text_width(token)>max_width:boundaries.update(range(offset+1,offset+len(token)))
        offset+=len(token);boundaries.add(offset)
    n=len(text);ideal=text_width(text)/max(1,math.ceil(text_width(text)/max_width))
    cost=[float('inf')]*(n+1);previous=[-1]*(n+1);cost[0]=0
    for end in range(1,n+1):
        if end not in boundaries:continue
        width=0
        for start in range(end-1,max(-1,end-int(max_width)*2-32),-1):
            width+=char_width(text[start])
            if width>max_width:break
            penalty=8+8*((ideal-width)/ideal)**2
            if width<=1:penalty+=4
            if end<n:
                if text[end] in PUNCT or text[end] in CLOSERS:penalty+=12
                if text[end-1] in '。！？.!?':penalty-=2
                elif text[end-1] in PUNCT:penalty-=.8
                penalty-=min(2,(pause_bonus or {}).get(end,0))
            value=cost[start]+penalty
            if value<cost[end]:cost[end]=value;previous[end]=start
    chunks=[];end=n
    while end>0:
        start=previous[end]
        if start<0:  # 非标准宽度/异常输入，仍然保证原文可还原。
            return _split_long_token(text,max_width)
        chunks.append(text[start:end]);end=start
    return _merge_tiny_chunks(chunks[::-1], max_width)


def _char_times(text: str, words: list[dict] | None) -> list[tuple[float, float] | None]:
    times: list[tuple[float, float] | None] = [None] * len(text)
    if not words:
        return times
    pos = 0
    for w in words:
        token = (w.get("word") or "").strip()
        if not token:
            continue
        idx = text.find(token, pos)
        if idx < 0:
            continue
        start = float(w.get("start", 0))
        end = float(w.get("end", start))
        # 同一词内按字符分配时间，避免长词跨片时每片都占整个词的时间。
        for k, timing in enumerate(_estimate_times(token, start, end)):
            times[idx + k] = timing
        pos = idx + len(token)
    return times


def _estimate_times(text: str, start: float, end: float) -> list[tuple[float, float]]:
    total = text_width(text) or 1.0
    out: list[tuple[float, float]] = []
    acc = 0.0
    span = max(0.0, end - start)
    for ch in text:
        w = char_width(ch)
        cs = start + span * (acc / total)
        acc += w
        ce = start + span * (acc / total)
        out.append((cs, ce))
    return out


def _word_positions(text: str, words: list[dict]):
    """逐字核对后映射词位置；不以旧词表覆盖人工修改过的文字。"""
    significant = lambda value: [(i, char) for i, char in enumerate(value)
                                 if unicodedata.category(char)[0] in ('L', 'N')]
    original = significant(text)
    tokens = [significant(str(word.get('word') or '')) for word in words]
    if (not original or not tokens or not all(tokens)
            or ''.join(char for _, char in original) != ''.join(char for token in tokens for _, char in token)):
        return None
    positions, offset = [], 0
    for token in tokens:
        positions.append(original[offset:offset+len(token)])
        offset += len(token)
    return positions


def _piece_words(cue: dict, positions, lo: int, hi: int, start: float, end: float):
    if positions is None:
        return [], True
    result, estimated = [], False
    for word, chars in zip(cue.get('words') or [], positions):
        chosen = [i for i, (position, _) in enumerate(chars) if lo <= position < hi]
        if not chosen:
            continue
        first, last = chosen[0], chosen[-1]+1
        ws, we = float(word['start']), float(word['end'])
        partial = first != 0 or last != len(chars)
        a = max(start, ws+(we-ws)*first/len(chars))
        b = min(end, ws+(we-ws)*last/len(chars))
        if b <= a:
            return [], True
        result.append({**word, 'word': cue['text'][chars[first][0]:chars[last-1][0]+1],
                       'start': round(a, 3), 'end': round(b, 3)})
        estimated = estimated or partial
    return result, estimated


def segment_cue(cue: dict, max_width: float = 20.0, *, split_at=None) -> tuple[list[dict], int]:
    text = cue.get("text") or ""
    if not text or (split_at is None and text_width(text) <= max_width):
        return [cue], 0

    char_times = _char_times(text, cue.get("words"))
    pauses={i:min(2,max(0,char_times[i][0]-char_times[i-1][1])*4)
            for i in range(1,len(text)) if char_times[i] and char_times[i-1]}
    chunks = [text[:split_at],text[split_at:]] if split_at is not None else split_text(text, max_width, pauses)
    if len(chunks) <= 1:
        return [cue], 0

    start = float(cue.get("start", 0))
    end = float(cue.get("end", start))
    estimated = False

    # 为每个 chunk 定位原文区间
    ranges: list[tuple[int, int]] = []
    pos = 0
    for chunk in chunks:
        idx = text.find(chunk, pos)
        if idx < 0:
            idx = pos
        ranges.append((idx, idx + len(chunk)))
        pos = idx + len(chunk)

    start_ms, end_ms = round(start * 1000), round(end * 1000)
    if end_ms - start_ms < len(chunks):
        return [dict(cue, needs_review=True, split_skipped=True)], 0
    boundaries = [start_ms]
    total = text_width(text) or 1.0
    for i, (a, _) in enumerate(ranges[1:], 1):
        left, right = char_times[a - 1], char_times[a]
        if left is not None and right is not None and left[1] <= right[0]:
            boundary = round((left[1] + right[0]) * 500)
        else:
            estimated = True
            boundary = round((start + (end - start) * text_width(text[:a]) / total) * 1000)
        boundaries.append(max(boundaries[-1] + 1, min(boundary, end_ms - (len(chunks) - i))))
    boundaries.append(end_ms)
    estimated = estimated or not cue.get("words")
    times = [(a / 1000, b / 1000) for a, b in zip(boundaries, boundaries[1:])]

    positions = _word_positions(text, cue.get('words') or [])
    out: list[dict] = []
    for pi, (chunk, (ts, te), (lo, hi)) in enumerate(zip(chunks, times, ranges)):
        fallback_start, fallback_end = ts, te
        # 按宽度切在词间时直接保留词间空档，不用中点把停顿分给两条。
        pause_before = lo>0 and char_times[lo] is not None and char_times[lo-1] is not None and char_times[lo-1][1]<char_times[lo][0]
        pause_after = hi<len(text) and char_times[hi-1] is not None and char_times[hi] is not None and char_times[hi-1][1]<char_times[hi][0]
        if pause_before:
            ts=max(ts,char_times[lo][0])
        if pause_after:
            te=min(te,char_times[hi-1][1])
        invalid_timing = round(te,3)<=round(ts,3)
        if invalid_timing:
            ts,te=fallback_start,fallback_end
            pause_before=pause_after=False
        words, word_estimated = _piece_words(cue, positions, lo, hi, ts, te)
        piece = dict(cue)
        piece.update({
            "start": round(ts, 3),
            "end": round(te, 3),
            "text": chunk,
            "words": words,
            "estimated": estimated or word_estimated or invalid_timing,
            "source": "split",
            "segmentation": "dynamic_programming_word_pause",
            "width_split": text_width(text)>max_width,
            "split_parent": f"{cue.get('display_id',cue.get('id',''))}:{start:.3f}:{end:.3f}",
            "script_text": "",
            "script_index": None,
            "match_ratio": None,
            "needs_review": bool(cue.get("needs_review")),
            "reference_match": None,
            "match_needs_review": False,
            "duration_trim": None,
        })
        base_id=cue.get('display_id',cue.get('id'))
        if base_id is not None:piece['display_id']=f'{base_id}.{pi+1}'
        if pause_before:piece['pause_before']=piece['start']
        if pause_after:piece['pause_after']=piece['end']
        out.append(piece)
    if cue.get('reference_match') or 'manual_reference' in cue:
        from .reference_match import inherit_reference, inherit_manual_reference
        inherit_manual_reference(cue,out)
        inherit_reference(cue,out)
    return out, len(out) - 1


def segment_midpoint(cue: dict, max_width: float=20):
    """只切当前一条，在中间附近选完整词边界；从不借用下一条字幕。"""
    text=cue.get('text') or '';offset=0;cuts=set()
    for token in jieba.lcut(text) if jieba is not None else list(text):
        if text_width(token)>max_width:cuts.update(range(offset+1,offset+len(token)))
        offset+=len(token)
        if offset<len(text):cuts.add(offset)
    # 前缀宽度只算一次，候选切点与代价函数都做 O(1) 查询，避免逐切点切片。
    prefix=[0.0]*(len(text)+1)
    for i,ch in enumerate(text):
        prefix[i+1]=prefix[i]+char_width(ch)
    total=prefix[-1]
    cuts=[cut for cut in cuts if significant_width(text[:cut])>=2 and significant_width(text[cut:])>=2
          and text[cut] not in PUNCT and text[cut] not in CLOSERS
          and max(prefix[cut],total-prefix[cut])<=max_width]
    if not cuts:
        pieces,count=segment_cue(cue,max_width)
        if count:
            for piece in pieces:piece.update(segmentation='manual_midpoint',manual_split=True,edited=True,review_complete=False)
        return pieces,count
    half=total/2;times=_char_times(text,cue.get('words'))
    def cost(cut):
        gap=max(0,times[cut][0]-times[cut-1][1]) if times[cut] and times[cut-1] else 0
        return abs(prefix[cut]-half)-min(.5,gap),cut
    pieces,count=segment_cue(cue,max_width,split_at=min(cuts,key=cost))
    if count:
        for piece in pieces:piece.update(segmentation='manual_midpoint',manual_split=True,edited=True,review_complete=False)
    return pieces,count


def segment_cues(cues: list[dict], max_width: float = 20.0) -> tuple[list[dict], int, int]:
    result: list[dict] = []
    splits = 0
    estimated = 0
    for cue in cues:
        pieces, n = segment_cue(cue, max_width)
        result.extend(pieces)
        splits += n
        estimated += sum(1 for p in pieces if p.get("estimated"))
    return result, splits, estimated


def segment_alignment_cues(cues: list[dict], max_width: float = 20.0) -> tuple[list[dict], int]:
    """对齐后按字数拆条；对齐失败的条目保持原样，避免复制错误时间。"""
    if max_width <= 0:
        return cues, 0
    result: list[dict] = []
    splits = 0
    for cue in cues:
        if (cue.get("alignment_error") or cue.get("word_timing_error")
                or (cue.get("alignment") or {}).get("stale")):
            result.append(cue)
            continue
        pieces, count = segment_cue(cue, max_width)
        result.extend(pieces)
        splits += count
    return result, splits


def _reference_break_context(cue: dict):
    """一条字幕只计算一次原文句界及文字映射，供全部词边界共用。"""
    match = cue.get('reference_match') or {}
    reference = match.get('reference') or {}
    raw = reference.get('text')
    text = cue.get('text') or ''
    from .reference_match import _reference_boundaries, normalized, sentence_ranges
    query, positions = normalized(text)
    manual = cue.get('manual_reference')
    manual_matches = (isinstance(manual,str) and bool(manual)
                      and not cue.get('manual_reference_needs_review')
                      and normalized(manual)[0] == query)
    if manual_matches:
        raw = manual
    elif (not raw or reference.get('reference_only')
            or match.get('status') in ('unmatched', 'context_mismatch')
            or (match.get('status') == 'ambiguous' and not match.get('candidate_confirmed'))):
        return None
    target, target_positions = normalized(raw)
    if not query or not target:
        return None
    # 采用参考后 match 会标记过期；只要文字仍与参考逐字一致，句界仍然可信。
    if not manual_matches and match.get('stale') and query != target:
        return None
    # 句读设置只控制显示；参考中逗号、冒号、省略号等也必须参与拆句。
    sentence_starts = {a for a,b in sentence_ranges(raw) if a}
    boundaries = range(1,len(query))
    mapped = _reference_boundaries(query,target,list(boundaries))
    breaks = {boundary for boundary,hit in zip(boundaries,mapped)
              if 0 < hit < len(target) and target_positions[hit][0] in sentence_starts}
    return [end for start,end in positions], breaks


def _reference_break_at(cue: dict, cut: int) -> bool | None:
    """参考明确句界才强制拆句；其余切点交给可靠声音停顿判断。"""
    context = _reference_break_context(cue)
    return True if context and bisect_right(context[0],cut) in context[1] else None


def _gap_has_audio_support(cue: dict, prev: dict, word: dict, min_gap: float) -> bool:
    """词间空档是否真的有静音作证。

    连续说话时模型词时间也可能给出几百毫秒的空档；如果边界精修已经生成了
    audio_silences 证据，却在该空档里找不到足够长的静音，就不按这个空档切，
    否则会出现“声波连贯但字幕只剩一两个字”的碎片。300 毫秒以上的空档
    已经明显超出词间抖动；背景音乐、呼吸或持续底噪可能让静音检测漏掉它，
    此时以词时间为准，不能把整段停顿吞掉。
    """
    gap = float(word['start']) - float(prev['end'])
    if gap >= max(.3, min_gap):
        return True
    if 'audio_silences' not in cue:
        return True
    left, right = float(prev['end']), float(word['start'])
    need = max(.15, min_gap * .5)
    for silence in cue.get('audio_silences') or []:
        overlap = min(right, float(silence['end'])) - max(left, float(silence['start']))
        if overlap + 1e-6 >= need:
            return True
    return False


def split_on_gaps(cues: list[dict], min_gap: float = 0.6, max_width: float=20) -> tuple[list[dict], int]:
    """在可靠词边界按停顿切分；声学静音可修正拖入静音内的词尾。"""
    out: list[dict] = []
    splits = 0
    for cue in cues:
        if cue.get('alignment_error') or (cue.get('alignment') or {}).get('stale'):
            out.append(cue)
            continue
        words = cue.get("words") or []
        if len(words) < 2:
            out.append(cue)
            continue
        original=str(cue.get('text') or '')
        positions=_word_positions(original,words)
        lexical_cuts=None
        if positions is not None and jieba is not None:
            lexical_cuts={0};offset=0
            for token in jieba.lcut(original):
                # 只有单个完整词本身超过全局字数上限，才允许在词内切开。
                if text_width(token)>max_width:
                    lexical_cuts.update(range(offset+1,offset+len(token)))
                offset+=len(token);lexical_cuts.add(offset)
        # 持续时长过滤与词对无关，先筛一次，避免每个词对重复遍历全部静音。
        silence_pool = [s for s in cue.get('audio_silences', [])
                        if min_gap > 0 and s['end']-s['start']+1e-6 >= min_gap]
        boundaries = {}
        forced = set()
        reference_context = _reference_break_context(cue) if positions is not None else None
        for wi, (prev, word) in enumerate(zip(words, words[1:]), 1):
            # 参考里明确是两句话：即使停顿没到阈值、或分词把该处并成一个词，
            # 也按句界切开；参考里是同一句时，才看停顿和分词边界。
            ref_break = bool(reference_context and bisect_right(reference_context[0],positions[wi][0][0]) in reference_context[1])
            # 模型可能给逐字时间；词间低能量或真实停顿也不能把“中毒”拆开。
            if not ref_break and lexical_cuts is not None and positions[wi][0][0] not in lexical_cuts:
                continue
            gap = float(word.get("start", 0)) - float(prev.get("end", 0))
            found = None
            if min_gap > 0 and gap + 1e-6 >= min_gap and _gap_has_audio_support(cue, prev, word, min_gap):
                found = (float(prev['end']), float(word['start']))
            else:
                # 不按时长比例猜文字；只采用位于相邻词之间、覆盖旧词尾的静音。
                silences = [s for s in silence_pool
                            if float(prev['start'])+.02 <= s['start']
                            and float(prev['end'])-.03 <= s['end'] <= float(word['start'])+.02]
                if silences:
                    silence = max(silences, key=lambda s: s['end']-s['start'])
                    found = (min(float(prev['end']), silence['start']+.01), float(word['start']))
            if ref_break is True:
                forced.add(wi)
                if found is None:
                    # 参考决定拆在哪里，词时间决定两边首尾；有停顿就留空，
                    # 不能为衔接下一句把前一条延长到后一个词的起点。
                    left,right=float(prev['end']),float(word['start'])
                    found = (left,right) if left<=right else (left,left)
            if found is None:
                continue
            boundaries[wi] = found
        if not boundaries:
            out.append(cue)
            continue

        # 只忽略标点/空白来定位；词表与当前校对原文不一致时绝不重建文本。
        if positions is None:
            out.append(dict(cue, needs_review=True, gap_split_skipped=True))
            continue
        # 连续说话时模型词时间也可能顿一下；首尾只剩一两个字不能单独成条，
        # 除非原句在该处本来就有标点（“你好，走吧”）。
        ordered = sorted(boundaries)
        if max_width >= EDGE_MIN_WIDTH:
            # 参考明确断句的切点必须保留，不能因为首尾只剩一两个字被并回去。
            while ordered and ordered[0] not in forced:
                cut = positions[ordered[0]][0][0]
                edge = original[:cut].strip()
                if _edge_is_fragment(edge, original[cut:]) and not _ends_with_punct(edge):
                    ordered.pop(0)
                else:
                    break
            while ordered and ordered[-1] not in forced:
                cut = positions[ordered[-1]][0][0]
                edge = original[cut:].strip()
                before = original[:cut].rstrip()
                if _edge_is_fragment(edge, before) and not _ends_with_punct(before):
                    ordered.pop()
                else:
                    break
            # 句中也不能留下单独一个字；把它并进相邻组，但不删除参考句界。
            while ordered:
                indexes = [0, *ordered, len(words)]
                removed = False
                for gi in range(len(indexes) - 1):
                    lo = 0 if gi == 0 else positions[indexes[gi]][0][0]
                    hi = len(original) if gi == len(indexes) - 2 else positions[indexes[gi + 1]][0][0]
                    if significant_width(original[lo:hi]) < 2:
                        candidate = 0 if gi == 0 else gi - 1 if gi < len(indexes) - 2 else -1
                        if ordered[candidate] in forced:
                            continue
                        ordered.pop(candidate)
                        removed = True
                        break
                if not removed:
                    break
            boundaries = {wi: boundaries[wi] for wi in ordered}
        if not boundaries:
            out.append(cue)
            continue
        indexes = [0, *sorted(boundaries), len(words)]
        groups = [words[a:b] for a, b in zip(indexes, indexes[1:])]
        cuts = [0, *(positions[i][0][0] for i in indexes[1:-1]), len(original)]

        pieces: list[dict] = []
        valid = True
        for gi, group in enumerate(groups):
            text = original[cuts[gi]:cuts[gi + 1]]
            if not text:
                valid = False
                break
            start = float(group[0].get("start", cue.get("start", 0)))
            end = float(group[-1].get("end", cue.get("end", start)))
            if gi:
                start = boundaries[indexes[gi]][1]
            if gi < len(groups)-1:
                end = boundaries[indexes[gi+1]][0]
            start = float(cue.get("start", start)) if gi == 0 else max(start, float(cue.get("start", start)))
            end = float(cue.get("end", end)) if gi == len(groups) - 1 else min(end, float(cue.get("end", end)))
            if (round(end, 3) <= round(start, 3) or (pieces and start < pieces[-1]["end"])
                    or any(float(w['start'])>end or float(w['end'])<start for w in group)):
                valid = False
                break
            piece = dict(cue)
            piece.update({
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "words": [{**w, 'start': max(start, float(w['start'])), 'end': min(end, float(w['end']))} for w in group],
                "source": "gap_split",
                "estimated": False,
                "script_text": "",
                "script_index": None,
                "match_ratio": None,
                "reference_match": None,
                "match_needs_review": False,
                "duration_trim": None,
            })
            if gi:
                piece['pause_before'] = piece['start']
            if gi < len(groups)-1:
                piece['pause_after'] = piece['end']
            base_id=cue.get('display_id',cue.get('id'))
            if base_id is not None:piece['display_id']=f'{base_id}.{gi+1}'
            pieces.append(piece)
        if not valid:
            out.append(cue)
        else:
            if cue.get('reference_match') or 'manual_reference' in cue:
                from .reference_match import inherit_reference, inherit_manual_reference
                inherit_manual_reference(cue,pieces)
                inherit_reference(cue,pieces)
            out.extend(pieces)
            splits += len(pieces) - 1
    return out, splits


def _cue_sources(cue: dict) -> list[dict]:
    """取一条字幕的全部原始识别记录；旧工程没有 asr_sources 时退回单条 asr_original。"""
    sources = cue.get("asr_sources")
    if isinstance(sources, list) and sources:
        return [dict(item) for item in sources if isinstance(item, dict)]
    original = cue.get("asr_original")
    if isinstance(original, dict) and original:
        return [dict(original)]
    return [{"text": cue.get("original_text", cue.get("text") or ""),
             "start": cue.get("original_start", cue.get("start", 0)),
             "end": cue.get("original_end", cue.get("end", 0)),
             "id": cue.get("id")}]


def repair_fragments(cues: list[dict], max_width: float, min_gap: float=.6):
    """同一原句的单字碎片：无有效停顿且总字数允许时合回，不合并独立台词。"""
    result=[];count=0
    for cue in cues:
        if result:
            left=result[-1];a=left.get('asr_original');b=cue.get('asr_original')
            same=bool(a and b and all(a.get(k)==b.get(k) for k in ('id','text','start','end')))
            gap=float(cue['start'])-float(left['end'])
            short=min(len(str(left['text']).strip()),len(str(cue['text']).strip()))<=1
            if (same and short and not str(left['text']).rstrip().endswith(TERMINAL)
                    and not left.get('manual_split') and not cue.get('manual_split')
                    and left.get('split_parent')==cue.get('split_parent')
                    and bool(left.get('exclude_export'))==bool(cue.get('exclude_export'))
                    and 0<=gap<=.25 and (not min_gap or gap+1e-6<min_gap)
                    and text_width(left['text']+cue['text'])<=max_width
                    and not left.get('alignment_error') and not cue.get('alignment_error')):
                merged,n=merge_cues([left,cue],max_width=max_width,max_gap=.25)
                if n:
                    combined=merged[0];combined['source']='fragment_repair'
                    combined['edited']=bool(left.get('edited') or cue.get('edited'))
                    combined.pop('segmentation',None)
                    if (left.get('alignment') and cue.get('alignment') and not left['alignment'].get('stale')
                            and not cue['alignment'].get('stale') and left['alignment'].get('engine')==cue['alignment'].get('engine')):
                        combined['alignment']={**left['alignment'],'stale':False,'fragment_repaired':True}
                    lm=left.get('reference_match') or {};rm=cue.get('reference_match') or {}
                    lr=lm.get('reference');rr=rm.get('reference')
                    if (lr and rr and not lm.get('stale') and not rm.get('stale')
                            and lr.get('source_id')==rr.get('source_id') and lr['end']==rr['start']):
                        from .reference_match import inherit_reference
                        ref={**lr,'text':lr['text']+rr['text'],'end':rr['end']}
                        if 'source_end' in rr:ref['source_end']=rr['source_end']
                        inherit_reference({**combined,'reference_match':{**lm,'reference':ref}},[combined])
                    result[-1]=combined;count+=1;continue
        result.append(cue)
    return result,count


def _merge_sources(left: dict, right: dict) -> list[dict]:
    """按记录身份去重合并原始 ASR，保证合并后仍能逐条追溯。"""
    merged: list[dict] = []
    seen: set[tuple] = set()
    for source in (*_cue_sources(left), *_cue_sources(right)):
        key = (source.get("id"), source.get("start"), source.get("end"), source.get("text"))
        if key not in seen:
            seen.add(key)
            merged.append(source)
    return merged


def merge_cues(cues: list[dict], max_width: float = 20.0, max_duration: float = 7.0,
               max_gap: float = 0.25) -> tuple[list[dict], int]:
    """合并相邻短句（同一角色、同样是否为噪音、间隙小、合并后不超宽）。"""
    result: list[dict] = []
    merged = 0
    for cue in cues:
        if result:
            prev = result[-1]
            same_role = (prev.get("role") or "") == (cue.get("role") or "")
            same_noise = bool(prev.get("is_noise")) == bool(cue.get("is_noise"))
            gap = float(cue.get("start", 0)) - float(prev.get("end", 0))
            new_text = (prev.get("text") or "") + (cue.get("text") or "")
            duration = float(cue.get("end", 0)) - float(prev.get("start", 0))
            if (same_role and same_noise and 0 <= gap <= max_gap and 0 < duration <= max_duration
                    and text_width(new_text) <= max_width):
                left_words, right_words = prev.get("words") or [], cue.get("words") or []
                sources = _merge_sources(prev, cue)
                starts = [float(s["start"]) for s in sources if isinstance(s.get("start"), (int, float))]
                ends = [float(s["end"]) for s in sources if isinstance(s.get("end"), (int, float))]
                original_text = "".join(str(s.get("text") or "") for s in sources)
                if 'manual_reference' in prev or 'manual_reference' in cue:
                    prev['manual_reference'] = prev.get('manual_reference',prev.get('script_text','')) + cue.get('manual_reference',cue.get('script_text',''))
                    prev['manual_reference_needs_review'] = bool(prev.get('manual_reference_needs_review') or cue.get('manual_reference_needs_review'))
                prev["words"] = left_words + right_words if left_words and right_words else []
                prev["text"] = new_text
                prev["end"] = cue.get("end", prev.get("end"))
                prev.pop('pause_after', None)
                if 'pause_after' in cue:
                    prev['pause_after'] = cue['pause_after']
                prev["edited"] = True
                for key in ('width_split','manual_split','split_parent','segmentation'):prev.pop(key,None)
                prev["estimated"] = bool(prev.get("estimated") or cue.get("estimated") or not prev["words"])
                prev["needs_review"] = bool(prev.get("needs_review") or cue.get("needs_review"))
                prev["is_noise"] = bool(prev.get("is_noise") or cue.get("is_noise"))
                prev["asr_sources"] = sources
                prev["original_text"] = original_text
                prev["original_start"] = min(starts) if starts else float(prev.get("start", 0))
                prev["original_end"] = max(ends) if ends else float(prev.get("end", 0))
                prev["asr_original"] = {"text": original_text, "start": prev["original_start"],
                                        "end": prev["original_end"],
                                        "id": sources[0].get("id") if sources else prev.get("id")}
                # 文本、时间或结构变了，派生结果不能再沿用。
                prev["reference_match"] = None
                prev["match_needs_review"] = False
                prev["alignment"] = {**prev["alignment"], "stale": True} if isinstance(prev.get("alignment"), dict) else None
                prev.pop("previous_asr", None)
                for key in ("script_text", "script_index", "match_ratio", "match_status", "review_decision"):
                    prev.pop(key, None)
                merged += 1
                continue
        result.append(dict(cue))
    return result, merged
