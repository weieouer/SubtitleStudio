"""以字幕为查询的全文参考匹配。所有位置均为原文 Unicode 字符的半开区间。"""
from __future__ import annotations

from bisect import bisect_right, bisect_left
from collections import Counter, defaultdict, OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from difflib import SequenceMatcher
import math
import re
import unicodedata
from functools import lru_cache
import hashlib
import logging
import threading
import time

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein
from pypinyin import lazy_pinyin, Style

from .interjections import mark_interjections

DEFAULT_OPTIONS = {
    "minimum": .64, "high": .94, "phonetic": .90, "ambiguity": .035,
    "pinyin_weight": .25, "short_minimum": .88, "max_group": 3, "merge_gap": 1.5,
    "switch_penalty": .12, "jump_penalty": .12,
}
BEAM_WIDTH=64
SOURCE_BEAM=8
BEAM_MAX=256
_INDEX_LOCK=threading.RLock()
log=logging.getLogger('studio.reference')
STATUS_LABELS = {"high": "高度匹配", "typo": "疑似错字", "homophone": "疑似谐音",
                 "missing": "疑似漏字", "extra": "疑似多字", "unmatched": "未匹配", "ambiguous": "多候选匹配",
                 "context_mismatch":"疑似多余台词"}
NARRATION = re.compile(r"^\s*(?:[【\[（(△▲#]|第.{1,10}[集场幕]|场景[：:]|镜头[：:]|画面[：:]|音效[：:]|BGM[：:])", re.I)
ACTION = re.compile(r"(?:镜头(?:切换|转向|拉近)|画面(?:切换|出现)|缓缓(?:走|转|抬)|转身(?:离开|走)|推开房门|望向窗外)")
MAX_SCRIPT = 500_000
MAX_CUES = 5000


def sentence_ranges(text):
    """按全部句读拆参考；数字和英文词内部符号不作句界，位置始终对应原文。"""
    start, has_content, pending = 0, False, False
    for i, char in enumerate(text):
        previous = text[i-1] if i else ''
        following = text[i+1] if i+1 < len(text) else ''
        internal = (char in '.,:/' and previous.isdigit() and following.isdigit()) or (
            char in "-'’" and previous.isascii() and previous.isalnum()
            and following.isascii() and following.isalnum())
        if char.isalnum():
            if pending:
                yield start, i
                start, pending = i, False
            has_content = True
        elif (unicodedata.category(char).startswith('P') or char in '\r\n⋯') and not internal and has_content:
            pending = True
    if has_content:
        yield start, len(text)


def options_from(value: dict | None) -> dict:
    result = {**DEFAULT_OPTIONS, **(value or {})}
    for key in DEFAULT_OPTIONS:
        v = result[key]
        if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v):
            raise ValueError(f"匹配参数 {key} 必须是有限数字")
        low, high = (1, 4) if key == "max_group" else (0, 10) if key == "merge_gap" else (0, 1)
        if not low <= v <= high or (key == "max_group" and int(v) != v):
            raise ValueError(f"匹配参数 {key} 超出范围")
    if result["high"] < result["minimum"]:
        raise ValueError("高度匹配阈值不能小于最低匹配阈值")
    if result["pinyin_weight"] > .4:
        raise ValueError("拼音权重最多为 0.4，不能仅凭拼音确定参考")
    result["max_group"] = int(result["max_group"])
    return {key: result[key] for key in DEFAULT_OPTIONS}


# 中文数字只在比对副本里换算成阿拉伯数字，剧本和字幕原文都不改。
CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNITS = {"十": 10, "百": 100, "千": 1000}
CN_NUMERALS = set(CN_DIGITS) | set(CN_UNITS) | {"万", "亿"}
# 这些是词，不是数量："千万不要""万一""万万没想到""亿万观众"。
CN_WORDS = {"千万", "万一", "万万", "亿万"}


def _cn_number(run: str) -> str | None:
    """连续中文数字转阿拉伯数字；只用于比对，不改变字幕原文。"""
    if run in CN_WORDS:
        return None
    has_digit = any(char in CN_DIGITS for char in run)
    # 单独的“百/千”多半是“百姓、百货”这类词；单独的“十”确定是数字。
    if not has_digit and len(run) == 1 and run != "十":
        return None
    if not any(char in CN_UNITS or char in "万亿" for char in run):
        if "两" in run and len(run) > 1:
            return None  # “一两”“两三”是约数，不是两位数。
        return "".join(str(CN_DIGITS[char]) for char in run)  # 一九九八 -> 1998
    total = section = number = 0
    last_unit = None
    for char in run:
        if char in CN_DIGITS:
            number = CN_DIGITS[char]
        elif char in CN_UNITS:
            last_unit = CN_UNITS[char]
            section += (number or 1) * last_unit
            number = 0
        elif char == "万":
            total += (section + number) * 10000
            section = number = 0
            last_unit = 10000
        else:  # 亿
            total = (total + section + number) * 100000000
            section = number = 0
            last_unit = 100000000
    # 口语省略：一百二=120、一千三=1300、一万二=12000；带“零”时按标准读法。
    if number and last_unit in (100, 1000, 10000, 100000000) and "零" not in run and "〇" not in run:
        number *= last_unit // 10
    value = total + section + number
    return str(value) if 0 < value < 10 ** 12 else None


def _collapse_numbers(chars: list[str], positions: list[tuple[int, int]]) -> tuple[str, list[tuple[int, int]]]:
    """把连续中文数字压成阿拉伯数字；每个数字字符映射回整段原字簇。"""
    out_chars, out_positions = [], []
    i = 0
    while i < len(chars):
        if chars[i] not in CN_NUMERALS:
            out_chars.append(chars[i]); out_positions.append(positions[i]); i += 1
            continue
        j = i
        while (j < len(chars) and chars[j] in CN_NUMERALS
               and (j == i or positions[j-1][1] == positions[j][0])):
            j += 1
        value = _cn_number("".join(chars[i:j]))
        # 小数点也折进同一段：三点五 -> 35，与 3.5 归一化后的 35 可比较。
        if (value is not None and j < len(chars) and chars[j] == "点"
                and positions[j-1][1] == positions[j][0]):
            k = j + 1
            while (k < len(chars) and chars[k] in CN_NUMERALS
                   and positions[k-1][1] == positions[k][0]):
                k += 1
            if k > j + 1:
                fraction = _cn_number("".join(chars[j + 1:k]))
                if fraction is not None:
                    value += fraction
                    j = k
        if value is None:
            out_chars.extend(chars[i:j]); out_positions.extend(positions[i:j])
        else:
            span = (positions[i][0], positions[j - 1][1])
            out_chars.extend(value); out_positions.extend([span] * len(value))
        i = j
    return "".join(out_chars), out_positions


def normalized(text: str) -> tuple[str, list[tuple[int, int]]]:
    """按 Unicode 字簇标准化；一对多转换仍映射到完整原字簇。"""
    chars, positions = [], []
    start = 0
    while start < len(text):
        end = start + 1
        while end < len(text) and unicodedata.combining(text[end]):
            end += 1
        for char in unicodedata.normalize("NFKC", text[start:end]).casefold():
            if char.isalnum():
                chars.append(char)
                positions.append((start, end))
        start = end
    return _collapse_numbers(chars, positions)


def parse_document(text: str, sources=None) -> dict:
    if not isinstance(text, str):
        raise ValueError("剧本全文必须是文本")
    if len(text) > MAX_SCRIPT:
        raise ValueError("剧本合计最多 50 万字符，请按集拆分")
    if sources is None:
        sources=[{'id':'source-0','name':'剧本','start':0,'end':len(text)}]
    if not isinstance(sources,list) or not 1<=len(sources)<=1000:
        raise ValueError('剧本来源必须为1–1000份的数组')
    validated=[];cursor=0;identities=set()
    for i,source in enumerate(sources):
        if not isinstance(source,dict):raise ValueError('剧本来源必须是对象')
        start,end=source.get('start'),source.get('end')
        identity=source.get('id',f'source-{i}');name=source.get('name','剧本')
        if (type(start) is not int or type(end) is not int or not cursor<=start<=end<=len(text)
                or text[cursor:start].strip() or not isinstance(identity,str) or not identity
                or identity in identities or not isinstance(name,str)):
            raise ValueError('剧本来源位置或标识无效')
        identities.add(identity);cursor=end
        validated.append({'id':identity,'name':name,'start':start,'end':end})
    if text[cursor:].strip():raise ValueError('剧本来源未覆盖完整原文')
    paragraphs, offset = [], 0
    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        end = offset + len(line.rstrip("\r\n"))
        if text[offset:end].strip():
            paragraphs.append({"index": len(paragraphs), "source_line": line_number,
                               "start": offset, "end": end, "text": text[offset:end],
                               "role": "", "kind": "reference", "cue_index": None})
        offset += len(line)
    return {"version": 2, "text": text, "paragraphs": paragraphs,"sources":validated,
            "offset_unit": "unicode_codepoint", "offset_end": "exclusive"}


def parse_documents(documents):
    """后端拼接原文并计算Unicode位置，文件顺序仅决定存储顺序。"""
    if not isinstance(documents,list) or not 1<=len(documents)<=1000:
        raise ValueError('剧本列表必须为1–1000份')
    texts=[];sources=[];offset=0
    for i,document in enumerate(documents):
        if not isinstance(document,dict) or not isinstance(document.get('text'),str):
            raise ValueError('剧本全文必须是文本')
        text=document['text']
        if i:offset+=1
        sources.append({'id':document.get('id',f'source-{i}'),'name':document.get('name','剧本'),
                        'start':offset,'end':offset+len(text)})
        offset+=len(text)
        if offset>MAX_SCRIPT:raise ValueError('剧本合计最多 50 万字符，请按集拆分')
        texts.append(text)
    return parse_document('\n'.join(texts),sources)


def pinyin_tokens(text: str) -> tuple[str, ...]:
    tokens = lazy_pinyin(text, style=Style.NORMAL, errors=lambda value: list(value), strict=False)
    # 非汉字也保持一个标准化字符对应一个 token。
    if len(tokens) != len(text):
        tokens = [lazy_pinyin(c, style=Style.NORMAL, errors=lambda s: list(s), strict=False)[0] for c in text]
    return tuple(tokens)


def text_diff(asr: str, reference: str) -> list[dict]:
    a, amap = normalized(asr)
    b, bmap = normalized(reference)
    result = []
    for tag, i, j, k, l in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        def span(mapping, x, y, raw):
            start = mapping[x][0] if x < len(mapping) else len(raw)
            end = mapping[y - 1][1] if y > x else start
            return start, end
        astart, aend = span(amap, i, j, asr)
        bstart, bend = span(bmap, k, l, reference)
        if tag != "equal":
            result.append({"operation": tag, "asr_start": astart, "asr_end": aend,
                           "script_start": bstart, "script_end": bend,
                           "asr": asr[astart:aend], "script": reference[bstart:bend]})
    return result


def effective_minimum(length: int, options: dict) -> float:
    if length <= 3:
        return max(options["minimum"], options["short_minimum"])
    return min(.98, options["minimum"] + (.08 if length <= 5 else .035 if length <= 8 else 0))


@lru_cache(maxsize=1)
def dialogue_prefixes(raw):
    """只屏蔽明确的角色前缀，保留原文坐标；冒号、引号后的文字仍是对白。"""
    pattern=re.compile(r'(?m)(?:^|(?<=[”」』"。！？!?；;]))[ \t]*(?P<role>[^\n\r:：,，。！？!?；;“”「」『』"…]{1,24})[：:][ \t]*')
    reserved={'时间','日期','地点','场景','镜头','画面','音效','BGM','备注','注意','提示','说明','例如','比如','原因','结果','答案','价格','网址','比例'}
    spans=[]
    for match in pattern.finditer(raw):
        role=match['role'].strip()
        following=raw[match.end():match.end()+1]
        if (role in reserved or role.upper() in {'HTTP','HTTPS','FTP','TIME','LOCATION','SCENE','NOTE','SFX','BGM'}
                or re.match(r'第.{0,10}[集场幕]',role)):continue
        if role.isdigit() or (role.isascii() and any(c.isdigit() for c in role)):continue
        quoted=following in ('"','“','「','『')
        named=((1<=len(role)<=4 and all('\u3400'<=c<='\u9fff' for c in role))
               or (len(role)<=16 and role.isascii() and role.replace(' ','').isalpha()))
        annotated=bool(re.fullmatch(r'[\u3400-\u9fffA-Za-z·]{1,12}[（(][^()（）]{1,12}[）)]',role))
        if quoted or named or annotated:
            spans.append((match.start('role'),match.end()))
    return tuple(spans)


@lru_cache(maxsize=1)
def _index_features(digest, raw):
    """只缓存一份全文的不可变检索特征；阈值、来源边界和查询缓存按请求隔离。"""
    prefixes=dialogue_prefixes(raw)
    searchable=raw
    if prefixes:
        pieces=[];cursor=0
        for start,end in prefixes:
            pieces.extend((raw[cursor:start],' '*(end-start)));cursor=end
        pieces.append(raw[cursor:]);searchable=''.join(pieces)
    # 等长屏蔽后再标准化，避免“张三：三个人”中的两个“三”先被合并换算。
    text,positions=normalized(searchable)
    phonetic=pinyin_tokens(text)
    grams,pygrams=defaultdict(list),defaultdict(list)
    for i in range(max(0,len(text)-1)):
        grams[text[i:i+2]].append(i);pygrams[phonetic[i:i+2]].append(i)
    return text,positions,phonetic,grams,pygrams,[p[0] for p in positions],[p[1] for p in positions]


def inherit_manual_reference(parent: dict, pieces: list[dict]) -> bool:
    """人工参考随拆条单调分配，子条拼接可恢复全文；不复制整段到每条。"""
    if 'manual_reference' not in parent or ''.join(p['text'] for p in pieces) != parent['text']:
        return False
    raw = parent['manual_reference']
    query, query_positions = normalized(parent['text'])
    target, positions = normalized(raw)
    boundaries = [0]
    offset = 0
    query_ends = [end for start,end in query_positions]
    for piece in pieces:
        offset += len(piece['text'])
        boundaries.append(bisect_right(query_ends, offset))
    mapped = _reference_boundaries(query, target, boundaries) if query and target else [0]*len(boundaries)
    cuts = [0, *(positions[k][0] if k < len(positions) else len(raw) for k in mapped[1:-1]), len(raw)]
    uncertain = (not query or not target or Levenshtein.normalized_similarity(query, target) < .64
                 or bool(parent.get('manual_reference_needs_review')))
    for piece, lo, hi in zip(pieces, cuts, cuts[1:]):
        piece['manual_reference'] = raw[lo:hi]
        piece['manual_reference_needs_review'] = uncertain or lo == hi
    return True


def inherit_reference(parent: dict, pieces: list[dict]) -> bool:
    """把已确认位置的父参考区间分配给子条目，不重复引用整段参考。"""
    match=parent.get('reference_match') or {};reference=match.get('reference')
    if (not reference or match.get('stale') or reference.get('reference_only')
            or (match.get('status')=='ambiguous' and not match.get('candidate_confirmed'))
            or ''.join(p['text'] for p in pieces)!=parent['text']):return False
    limits=reference.get('source_range')
    if limits and not limits[0]<=reference['start']<reference['end']<=limits[1]:return False
    query,query_positions=normalized(parent['text']);raw=reference['text'];text,positions=normalized(raw)
    if not query or not text:return False
    boundaries=[0]
    offset=0;query_ends=[end for start,end in query_positions]
    for piece in pieces:
        offset+=len(piece['text']);boundaries.append(bisect_right(query_ends,offset))
    mapped=_reference_boundaries(query,text,boundaries)
    if any(a>=b for a,b in zip(mapped,mapped[1:])):return False
    if any(positions[k-1]==positions[k] for k in mapped[1:-1] if 0<k<len(positions)):return False
    cuts=[0,*(positions[k][0] for k in mapped[1:-1]),len(raw)]
    for piece,lo,hi in zip(pieces,cuts,cuts[1:]):
        q=normalized(piece['text'])[0];r=normalized(raw[lo:hi])[0]
        character=fuzz.ratio(q,r)/100;phonetic=fuzz.ratio(pinyin_tokens(q),pinyin_tokens(r))/100
        score=.75*character+.25*phonetic-.12*abs(len(q)-len(r))/max(1,len(q),len(r))
        status=_status(q,r,character,phonetic,DEFAULT_OPTIONS)
        current={**reference,'text':raw[lo:hi]}
        # 参考缺少绝对位置（旧项目或手工构造）时仍可继承文字，只是不推算字符区间。
        if 'start' in reference:
            current.update(start=reference['start']+lo,end=reference['start']+hi)
        if 'source_start' in reference:
            current.update(source_start=reference['source_start']+lo,source_end=reference['source_start']+hi)
        stale=score<max(effective_minimum(len(q),DEFAULT_OPTIONS),match.get('minimum_used',0))
        piece.update(script_text=current['text'],script_index=parent.get('script_index'),match_ratio=round(score,4),
                     match_needs_review=stale or status!='high',reference_match={
                         **match,'status':status,'label':STATUS_LABELS[status],'reference':current,
                         'score':round(score,4),'character_score':round(character,4),'pinyin_score':round(phonetic,4),
                         'query_text':piece['text'],'differences':text_diff(piece['text'],current['text']),
                         'alternatives':[],'group':None,'stale':stale,'inherited':True})
    return True


class ReferenceIndex:
    def __init__(self, document: dict, options: dict, position_bounds=None):
        self.document, self.options = document, options
        self.raw = document["text"]
        started=time.perf_counter();digest=hashlib.sha256(self.raw.encode('utf-8')).hexdigest()
        with _INDEX_LOCK:
            before=_index_features.cache_info().hits
            self.text,self.positions,self.phonetic,self.grams,self.pygrams,starts,ends=_index_features(digest,self.raw)
            self.index_cache_hit=_index_features.cache_info().hits>before
        self.index_seconds=time.perf_counter()-started
        self.sources=document['sources']
        self.dialogue_prefixes=dialogue_prefixes(self.raw)
        self.dialogue_prefix_starts=[a for a,b in self.dialogue_prefixes]
        self.source_starts=[s['start'] for s in self.sources]
        self.source_ranges=[(bisect_left(starts,s['start']),bisect_right(ends,s['end'])) for s in self.sources]
        self.lower=bisect_left(starts,position_bounds[0]) if position_bounds else 0
        self.upper=bisect_right(ends,position_bounds[1]) if position_bounds else len(self.text)
        self.paragraphs = document["paragraphs"]
        self.starts = [p["start"] for p in self.paragraphs]
        self.sentences = []
        for source in self.sources:
            for a, b in sentence_ranges(self.raw[source['start']:source['end']]):
                lo = bisect_left(starts, source['start']+a)
                hi = bisect_right(ends, source['start']+b)
                if lo < hi:
                    self.sentences.append((lo, hi))
        self.sentence_starts = [a for a, b in self.sentences]
        self.sentence_ends = [b for a, b in self.sentences]
        self.sentence_start_set = set(self.sentence_starts)
        self.sentence_end_set = set(self.sentence_ends)
        self.cache = OrderedDict();self.candidate_queries=0;self.candidate_cache_hits=0
        self.repeated_queries=set();self.exact_cache=OrderedDict()

    def exact_after(self,query,sid,start,end=None):
        lo,hi=self.source_ranges[sid]
        start=max(lo,self.lower,start);end=min(hi,self.upper,end if end is not None else hi)
        key=(query,sid,start,end)
        if key in self.exact_cache:
            self.exact_cache.move_to_end(key);return self.exact_cache[key]
        hits=[];at=self.text.find(query,start,end)
        while at>=0 and len(hits)<2:
            stop=at+len(query)
            if self.source_index(at,stop)==sid:
                hits.append({**self.describe(at,stop),'norm_start':at,'norm_end':stop,
                             'character_score':1.,'pinyin_score':1.,'score':1.})
            at=self.text.find(query,at+1,end)
        self.exact_cache[key]=hits
        if len(self.exact_cache)>2048:self.exact_cache.popitem(last=False)
        return hits

    def source_index(self,start,end):
        raw_start,raw_end=self.positions[start][0],self.positions[end-1][1]
        boundary=bisect_right(self.dialogue_prefix_starts,raw_start)
        if boundary<len(self.dialogue_prefix_starts) and self.dialogue_prefix_starts[boundary]<raw_end:
            return None
        sid=max(0,bisect_right(self.source_starts,raw_start)-1)
        if raw_end>self.sources[sid]['end']:return None
        # 不允许候选截断同一个原字簇的一对多标准化副本。
        if start and self.positions[start]==self.positions[start-1]:return None
        if end<len(self.positions) and self.positions[end]==self.positions[end-1]:return None
        return sid

    def describe(self, start: int, end: int) -> dict:
        raw_start = self.positions[start][0]
        raw_end = self.positions[end - 1][1]
        sid=max(0,bisect_right(self.source_starts,raw_start)-1)
        source=self.sources[sid]
        while raw_start > source['start'] and self.raw[raw_start-1] in '“‘「『（("':
            raw_start -= 1
        # 带回紧随片段的句末标点；不跨越引号、角色标签或下一个句子。
        while raw_end < source['end'] and self.raw[raw_end] in '，。！？!?；;…%％‰”’」』）)"':
            raw_end += 1
        pstart = max(0, bisect_right(self.starts, raw_start) - 1)
        pend = max(pstart, bisect_right(self.starts, max(raw_start, raw_end - 1)) - 1)
        lines = self.paragraphs[pstart:pend + 1]
        risk = any(NARRATION.search(p["text"]) or ACTION.search(p["text"]) for p in lines)
        # 角色括号中的动作不污染其后的对白；仍不允许跨到下一位说话人。
        prefix=bisect_right(self.dialogue_prefix_starts,raw_start)-1
        if prefix>=0 and self.dialogue_prefixes[prefix][1]<=raw_start:
            after=self.dialogue_prefixes[prefix][1]
            if '\n' not in self.raw[after:raw_start] and '\r' not in self.raw[after:raw_start]:
                risk=bool(NARRATION.search(self.raw[raw_start:raw_end]))
        # 明确的格式标签跨在片段中时不能把人物名一起建议为字幕。
        if re.search(r"\n\s*[^\n：:]{1,16}[：:]", self.raw[raw_start:raw_end]):
            risk = True
        return {"text": self.raw[raw_start:raw_end], "start": raw_start, "end": raw_end,
                "paragraph_start": pstart + 1, "paragraph_end": pend + 1,
                "line_start": lines[0]["source_line"] if lines else 1,
                "line_end": lines[-1]["source_line"] if lines else 1,
                "reference_only": bool(risk),'source_index':sid,'source_id':source['id'],
                'source_name':source['name'],'source_range':[source['start'],source['end']],'source_start':raw_start-source['start'],
                'source_end':raw_end-source['start']}

    @staticmethod
    def _sample(positions, limit=48, start=0, end=None):
        # 先计算索引范围再采样，避免为高频二元组复制整段大列表。
        stop = len(positions) if end is None else end
        count = stop - start
        if count <= limit:
            return positions[start:stop]
        return [positions[start + round(i * (count - 1) / (limit - 1))] for i in range(limit)]

    def candidates(self, query: str) -> list[dict]:
        if query in self.cache:
            self.candidate_cache_hits+=1;self.cache.move_to_end(query)
            return self.cache[query]
        self.candidate_queries+=1
        n, size = len(query), len(self.text)
        if not n or not size:
            return []
        py = pinyin_tokens(query)
        seeds, exact = Counter(), []
        at = self.text.find(query,self.lower,self.upper)
        while at >= 0 and len(exact) < 96:
            exact.append((at, at + n))
            seeds[at] += 20
            at = self.text.find(query, at + 1,self.upper)
        if len(exact)>=32:self.repeated_queries.add(query)
        for i in sorted({round(j * max(0, n - 2) / 7) for j in range(8)}):
            if n < 2:
                break
            for positions, weight in ((self.grams.get(query[i:i + 2], []), 2),
                                      (self.pygrams.get(py[i:i + 2], []), 1)):
                for pos in self._sample(positions, start=bisect_left(positions,self.lower),
                                        end=bisect_left(positions,self.upper)):
                    seeds[max(0, pos - i)] += weight
        # 无锚点时仍搜索全文，不把找不到二元组当作未匹配。
        if not seeds or not exact:
            alignment = fuzz.partial_ratio_alignment(query, self.text[self.lower:self.upper], score_cutoff=40)
            if alignment:
                seeds[alignment.dest_start+self.lower] += 3
            palign = fuzz.partial_ratio_alignment(py, self.phonetic[self.lower:self.upper], score_cutoff=65)
            if palign:
                seeds[palign.dest_start+self.lower] += 2
        spans = set(exact)
        margin = min(12, max(2, math.ceil(n * .3)))
        for start, _ in seeds.most_common(24):
            lo, hi = max(self.lower, start - margin), min(self.upper, start + n + margin)
            alignment = fuzz.partial_ratio_alignment(query, self.text[lo:hi])
            anchor = lo + alignment.dest_start if alignment else start
            for left in range(max(self.lower, anchor - 2), min(self.upper, anchor + 3)):
                for length in range(max(1, n - margin), min(self.upper - left, n + margin) + 1):
                    spans.add((left, left + length))
        # 补充完整句候选，避免模糊滑窗把漏掉的首尾字切掉，或串入相邻半句。
        for a, b in list(spans):
            first = bisect_right(self.sentence_starts, a)-1
            last = bisect_left(self.sentence_ends, b)
            if 0 <= first <= last < len(self.sentences):
                lo, hi = self.sentences[first][0], self.sentences[last][1]
                if self.lower <= lo < hi <= self.upper and abs(hi-lo-n) <= margin:
                    spans.add((lo, hi))
        spans = {(a,b) for a,b in spans if (a,b) in exact or
                 bisect_right(self.sentence_ends,a) == bisect_left(self.sentence_starts,b)-1 or
                 (a in self.sentence_start_set and b in self.sentence_end_set)}
        # 先用字符评分筛选，再计算拼音，避免为整本剧本逐窗转拼音。
        ranked = sorted(((fuzz.ratio(query, self.text[a:b]) / 100, a, b) for a, b in spans
                         if self.source_index(a,b) is not None), reverse=True)
        # 不让某一文件同一位置的众多长度变体挤掉其他文件的候选。
        selected=[];represented=set()
        for item in ranked:
            sid=self.source_index(item[1],item[2])
            if sid not in represented:
                selected.append(item);represented.add(sid)
                if len(selected)>=80:break
        chosen_spans={(item[1],item[2]) for item in selected}
        for item in ranked:
            if len(selected)>=80:break
            span=(item[1],item[2])
            if span not in chosen_spans:
                chosen_spans.add(span);selected.append(item)
        # 精确重复位置全保留，供全局顺序和多候选分析使用。
        selected.extend((1., a, b) for a, b in exact if (1., a, b) not in selected)
        candidates, seen = [], set()
        for character, a, b in selected:
            if self.source_index(a,b) is None:continue
            if (a, b) in seen:
                continue
            seen.add((a, b))
            phonetic = fuzz.ratio(py, self.phonetic[a:b]) / 100
            score = ((1 - self.options["pinyin_weight"]) * character + self.options["pinyin_weight"] * phonetic
                     - .12 * abs(n - (b - a)) / max(n, b - a))
            if score < .38:
                continue
            desc = self.describe(a, b)
            candidates.append({**desc, "norm_start": a, "norm_end": b,
                               "character_score": round(character, 4), "pinyin_score": round(phonetic, 4),
                               "score": round(score, 4)})
        def rank(candidate):
            whole = (n>=5 and abs(candidate['norm_end']-candidate['norm_start']-n)<=2
                     and candidate['norm_start'] in self.sentence_start_set
                     and candidate['norm_end'] in self.sentence_end_set)
            return (-(candidate['score']+(.08 if whole else 0)), candidate['reference_only'],
                    candidate['norm_start'], candidate['norm_end'])
        candidates.sort(key=rank)
        # 同一位置的长短变体只保留最佳；不同位置的重复台词仍然独立。
        distinct = []
        for candidate in candidates:
            if any(self.same_location(candidate, other) for other in distinct):
                continue
            distinct.append(candidate)
        balanced=[];represented=set()
        for candidate in distinct:
            if candidate['source_index'] not in represented:
                balanced.append(candidate);represented.add(candidate['source_index'])
                if len(balanced)>=32:break
        chosen={(c['source_index'],c['norm_start'],c['norm_end']) for c in balanced}
        for candidate in distinct:
            if len(balanced)>=32:break
            key=(candidate['source_index'],candidate['norm_start'],candidate['norm_end'])
            if key not in chosen:
                chosen.add(key);balanced.append(candidate)
        distinct=sorted(balanced,key=lambda c:(-c['score'],c['reference_only'],c['norm_start'],c['norm_end']))
        self.cache[query] = distinct
        if len(self.cache)>1024:self.cache.popitem(last=False)
        return distinct

    @staticmethod
    def same_location(a, b):
        overlap = max(0, min(a["norm_end"], b["norm_end"]) - max(a["norm_start"], b["norm_start"]))
        return overlap >= .6 * min(a["norm_end"] - a["norm_start"], b["norm_end"] - b["norm_start"])

    def reliable(self, candidate: dict, length: int) -> bool:
        return (not candidate["reference_only"] and candidate["character_score"] >= .4
                and candidate["score"] >= effective_minimum(length, self.options))


@dataclass
class Node:
    score: float
    end: int
    previous: Node | None
    choice: tuple
    source: int = -1
    ends: tuple = ()


def _reference_boundaries(query: str, reference: str, boundaries: list[int]) -> list[int]:
    """把相邻字幕组合的查询边界单调映射到同一参考片段，避免重复填入全文。"""
    mapping = [None] * (len(query) + 1)
    for tag, a, b, c, d in Levenshtein.opcodes(query, reference):
        if tag == "insert":
            mapping[a] = c  # 插入归下一条；首尾边界在下面固定。
        else:
            for i in range(a, b + 1):
                mapping[i] = c + round((d - c) * (i - a) / max(1, b - a))
    mapping[0], mapping[-1] = 0, len(reference)
    result, previous = [], 0
    for boundary in boundaries:
        previous = max(previous, mapping[boundary] if mapping[boundary] is not None else previous)
        result.append(previous)
    return result


def _status(query, reference, character, phonetic, options):
    if query == reference:
        return "high"
    if phonetic >= options["phonetic"] and phonetic - character >= .06:
        return "homophone"
    ops = Levenshtein.opcodes(query, reference)
    inserted = sum(d - c for tag, a, b, c, d in ops if tag == "insert")
    deleted = sum(b - a for tag, a, b, c, d in ops if tag == "delete")
    if inserted and not deleted and len(reference) > len(query):
        return "missing"
    if deleted and not inserted and len(query) > len(reference):
        return "extra"
    return "typo"


def _context_constraints(index, queries, cues, excluded, sequence_indices=None):
    """用前后唯一长台词限定局部位置；不以全文偶然同句证明中间存在对白。"""
    anchors=[];occurrences=[]
    for ci,query in enumerate(queries):
        anchor=None
        found=[]
        if ci not in excluded and len(query)>=2:
            # 最稀有二元组定位精确命中，不额外对每条字幕做全文模糊搜索。
            offset=min(range(len(query)-1),key=lambda k:len(index.grams.get(query[k:k+2],())))
            postings=index.grams.get(query[offset:offset+2],())
            lo=bisect_left(postings,index.lower+offset);hi=bisect_left(postings,index.upper)
            if hi-lo<=256:
                for position in postings[lo:hi]:
                    start=position-offset;end=start+len(query)
                    if start<index.lower or end>index.upper or not index.text.startswith(query,start):continue
                    sid=index.source_index(start,end)
                    if sid is None or index.describe(start,end)['reference_only']:continue
                    found.append({'norm_start':start,'norm_end':end,'source_index':sid})
                    if len(found)>32:break
            if len(query)>=4 and len(found)==1:anchor=found[0]
        anchors.append(anchor)
        occurrences.append(found if len(found)<=32 else [])
    # 前一句常是“好的”等重复短句；再向前一句、向后两句寻找可靠锚点，
    # 仅当同一来源、连续字幕与局部位置共同唯一时提升为上下文锚点。
    for _ in range(2):
        for ci, possible in enumerate(occurrences):
            if anchors[ci] or not possible:continue
            lefts=[j for j in range(ci-1,max(-1,ci-3),-1) if anchors[j]]
            rights=[j for j in range(ci+1,min(len(cues),ci+3)) if anchors[j]]
            supported={}
            for left in lefts:
                for right in rights:
                    a,b=anchors[left],anchors[right]
                    if (a['source_index']!=b['source_index'] or a['norm_end']>b['norm_start']
                            or cues[right]['start']-cues[left]['end']>20
                            or (sequence_indices is not None and sequence_indices[right]-sequence_indices[left]!=right-left)
                            or b['norm_start']-a['norm_end']>max(48,min(200,sum(map(len,queries[left+1:right]))*3+12))):continue
                    bounded=[p for p in possible if p['source_index']==a['source_index']
                             and a['norm_end']<=p['norm_start']<p['norm_end']<=b['norm_start']]
                    if len(bounded)==1:
                        candidate=bounded[0]
                        supported[(candidate['norm_start'],candidate['norm_end'])]=candidate
            if len(supported)==1:anchors[ci]=next(iter(supported.values()))
    constraints={};keys={}
    for left,a in enumerate(anchors):
        if a is None:continue
        for right in range(left+2,min(len(cues),left+6)):
            b=anchors[right]
            if (b is None or a['source_index']!=b['source_index'] or a['norm_end']>b['norm_start']
                    or cues[right]['start']-cues[left]['end']>20
                    or (sequence_indices is not None and sequence_indices[right]-sequence_indices[left]!=right-left)):continue
            lower,upper=a['norm_end'],b['norm_start']
            if upper-lower>max(24,min(120,sum(len(q) for q in queries[left+1:right])*2+12)):continue
            for ci in range(left+1,right):
                if ci in excluded:continue
                own=anchors[ci]
                # 连续或反复出现的另一来源有自己的台词锚点，允许真正的场景切换。
                if own and own['source_index']!=a['source_index']:
                    supported=any(other and other['source_index']==own['source_index']
                        and ((j<ci and other['norm_end']<=own['norm_start']) or (j>ci and own['norm_end']<=other['norm_start']))
                        for j,other in enumerate(anchors[max(0,ci-3):min(len(anchors),ci+4)],max(0,ci-3)) if j!=ci)
                    if supported:continue
                key=(right-left,upper-lower)
                if ci in keys and keys[ci]<=key:continue
                keys[ci]=key
                raw_start=index.positions[lower-1][1];raw_end=index.positions[upper][0]
                source=index.sources[a['source_index']]
                constraints[ci]={'lower':lower,'upper':upper,'source_index':a['source_index'],
                    'source_id':source['id'],'source_name':source['name'],'start':raw_start,'end':raw_end,
                    'previous_cue':left,'next_cue':right,'previous_text':cues[left]['text'],'next_text':cues[right]['text'],
                    'previous_context':[cues[j]['text'] for j in range(max(0,left-1),left+1)],
                    'text':index.raw[raw_start:raw_end]}
    return constraints


def _context_candidate(index,query,context):
    lo,hi=context['lower'],context['upper']
    if not query or lo>=hi:return None
    alignment=fuzz.partial_ratio_alignment(query,index.text[lo:hi])
    if alignment is None:return None
    start,end=lo+alignment.dest_start,lo+alignment.dest_end
    if start>=end or index.source_index(start,end)!=context['source_index']:return None
    character=fuzz.ratio(query,index.text[start:end])/100
    phonetic=fuzz.ratio(pinyin_tokens(query),index.phonetic[start:end])/100
    score=(1-index.options['pinyin_weight'])*character+index.options['pinyin_weight']*phonetic-.12*abs(len(query)-(end-start))/max(len(query),end-start)
    candidate={**index.describe(start,end),'norm_start':start,'norm_end':end,
               'character_score':character,'pinyin_score':phonetic,'score':score}
    return candidate if index.reliable(candidate,len(query)) else None


def match_reference(cues: list[dict], document: dict, options: dict | None = None,
                    force_indices: list[int] | None = None, position_bounds=None, sequence_indices=None,
                    checkpoint=None) -> dict:
    started=time.perf_counter();opts = options_from(options)
    check = checkpoint or (lambda *args: None)
    check(0, '建立剧本索引')
    if len(cues) > MAX_CUES:
        raise ValueError("每次最多校验 5000 条字幕，请按集处理")
    if any(len(cue["text"]) > 1000 for cue in cues):
        raise ValueError("单条字幕最多 1000 字，请先分段")
    doc = parse_document(document.get("text", ""),document.get('sources'))
    index = ReferenceIndex(doc, opts, position_bounds)
    check(.05, '索引已就绪')
    result = deepcopy(cues)
    forced = set(force_indices or [])
    queries = [normalized(cue["text"])[0] for cue in result]
    excluded = {i for i, cue in enumerate(result) if cue.get("review_decision") in ("skipped", "non_script") and i not in forced}
    contexts=_context_constraints(index,queries,cues,excluded,sequence_indices)
    local_candidates={ci:_context_candidate(index,queries[ci],context) for ci,context in contexts.items()}
    blocked={ci for ci,candidate in local_candidates.items() if candidate is None}
    def within_context(candidate,start,end):
        return all(ci not in contexts or (candidate['source_index']==contexts[ci]['source_index']
            and contexts[ci]['lower']<=candidate['norm_start']<candidate['norm_end']<=contexts[ci]['upper']) for ci in range(start,end))
    source_ids={source['id']:source for source in doc['sources']}
    def hint(cue):
        match=cue.get('reference_match') or {};ref=match.get('reference') or {}
        return ref.get('source_id') if (not match.get('stale') and ref.get('source_id') in source_ids
            and source_ids[ref['source_id']]['start']<=ref.get('start',-1)<ref.get('end',-1)<=source_ids[ref['source_id']]['end']
            and doc['text'][ref.get('start',0):ref.get('end',0)]==ref.get('text')) else None
    hints=[hint(cue) for cue in cues]
    singles={}
    def groups_at(i):
        query = ""
        for k in range(1, opts["max_group"] + 1):
            end = i + k
            if end > len(cues) or end - 1 in excluded or end-1 in blocked:
                break
            if k > 1 and (cues[end - 1]["start"] - cues[end - 2]["end"] > opts["merge_gap"] or not queries[end - 1]):
                break
            if k>1 and hints[end-2] and hints[end-1] and hints[end-2]!=hints[end-1]:break
            query += queries[end - 1]
            if not query:
                break
            candidates=index.candidates(query)
            if k==1 and local_candidates.get(i):candidates=candidates+[local_candidates[i]]
            if k==1:singles[i]=candidates[:3]
            gain_base = sum(math.sqrt(max(1, len(q))) for q in queries[i:end])
            yield end,query,candidates,gain_base
    # 对字幕索引做动态规划，对剧本位置保留有限束；未匹配不会把后续位置卡死。
    layers = [dict() for _ in range(len(cues) + 1)]
    initial=tuple(start for start,end in index.source_ranges)
    layers[0][(-1,initial)] = Node(0, 0, None, (), -1, initial)
    for i in range(len(cues)):
        check(.05 + .85 * i / max(1, len(cues)), f'校验字幕 {i + 1}/{len(cues)}')
        ranked=sorted(layers[i].values(),key=lambda n:(-n.score,n.end))
        nodes=ranked[:BEAM_WIDTH];seen={id(n) for n in nodes};by_source=Counter()
        for node in ranked:
            if by_source[node.source]<SOURCE_BEAM and id(node) not in seen:
                nodes.append(node);seen.add(id(node))
            by_source[node.source]+=1
            if len(nodes)>=BEAM_MAX:break
        groups=list(groups_at(i)) if nodes else []
        def offer(target, node):
            key=(node.source,node.ends)
            old = layers[target].get(key)
            if old is None or node.score > old.score:
                layers[target][key] = node
        for node in nodes:
            offer(i + 1, Node(node.score, node.end, node, (i, i + 1, None),node.source,node.ends))
            for end,query,candidates,gain_base in groups:
                if node.source>=0 and query in index.repeated_queries:
                    candidates=candidates+index.exact_after(query,node.source,node.ends[node.source])
                for candidate in candidates:
                    sid=candidate['source_index']
                    if candidate["norm_start"] < node.ends[sid] or not index.reliable(candidate, len(query)) or not within_context(candidate,i,end):
                        continue
                    if sid!=node.source and node.source>=0 and len(query)<=3 and candidate['score']<opts['high']:continue
                    gain = gain_base * (candidate["score"] - .45)
                    # 接近整句时优先保留参考首尾字，避免把漏字误报成截短片段的高度匹配。
                    span_length = candidate['norm_end']-candidate['norm_start']
                    if (len(query)>=5 and abs(span_length-len(query))<=2
                            and candidate['norm_start'] in index.sentence_start_set
                            and candidate['norm_end'] in index.sentence_end_set):
                        gain += gain_base * .08
                    # 位置只作轻微连续性约束，不能盖过文本证据。
                    source_length=max(1,index.source_ranges[sid][1]-index.source_ranges[sid][0])
                    gap_penalty = (min(opts['jump_penalty'],math.sqrt((candidate['norm_start']-node.ends[sid])/source_length)*opts['jump_penalty'])
                                   if sid==node.source else opts['switch_penalty'] if node.source>=0 else 0.)
                    score = node.score + gain + .008 * (end - i - 1) - gap_penalty
                    cursors=list(node.ends);cursors[sid]=candidate['norm_end']
                    offer(end, Node(score, candidate["norm_end"], node, (i, end, candidate),sid,tuple(cursors)))
        layers[i].clear()
    chosen = max(layers[-1].values(), key=lambda n: (n.score, -n.end))
    path = []
    while chosen.previous is not None:
        path.append(chosen.choice)
        chosen = chosen.previous
    path.reverse()
    hits, positions = [], {}
    for start, end, candidate in path:
        check(.92, '整理匹配结果')
        if not candidate:
            continue
        query = "".join(queries[start:end])
        lengths = [0]
        for q in queries[start:end]:
            lengths.append(lengths[-1] + len(q))
        mapped = _reference_boundaries(query, index.text[candidate["norm_start"]:candidate["norm_end"]], lengths)
        for offset, ci in enumerate(range(start, end)):
            a, b = candidate["norm_start"] + mapped[offset], candidate["norm_start"] + mapped[offset + 1]
            if a < b:
                positions[ci] = (a, b, start, end, candidate)
    source_hits=defaultdict(list)
    for ci,pos in sorted(positions.items()):source_hits[pos[4]['source_index']].append((ci,pos[0],pos[1]))
    source_cues={sid:[hit[0] for hit in hits] for sid,hits in source_hits.items()}
    for ci, cue in enumerate(result):
        cue.setdefault("original_text", cue["text"])
        cue.setdefault("original_start", cue["start"])
        cue.setdefault("original_end", cue["end"])
        cue.setdefault("asr_original", {"text": cue["original_text"], "start": cue["original_start"], "end": cue["original_end"], "id": cue.get("id", ci + 1)})
        if ci in forced:
            cue["review_decision"] = "pending"
        cue["script_text"], cue["script_index"], cue["match_ratio"] = "", None, None
        cue["match_needs_review"] = True
        query = queries[ci]
        match = {"status": "unmatched", "label": STATUS_LABELS["unmatched"], "query_text": cue["text"],
                 "character_score": 0., "pinyin_score": 0., "score": 0., "differences": [],
                 "alternatives": [], "reference": None, "stale": False, "minimum_used": effective_minimum(len(query), opts)}
        if ci in positions:
            a, b, gstart, gend, group_candidate = positions[ci]
            reference = index.describe(a, b)
            refnorm = index.text[a:b]
            charscore = fuzz.ratio(query, refnorm) / 100
            pyscore = fuzz.ratio(pinyin_tokens(query), index.phonetic[a:b]) / 100
            score = ((1 - opts["pinyin_weight"]) * charscore + opts["pinyin_weight"] * pyscore
                     - .12 * abs(len(query) - len(refnorm)) / max(1, len(query), len(refnorm)))
            status = _status(query, refnorm, charscore, pyscore, opts)
            # 同一组内可利用上下文；不把短片段分数伪装成组合分数。
            match.update({"status": status, "reference": reference, "character_score": round(charscore, 4),
                          "pinyin_score": round(pyscore, 4), "score": round(score, 4),
                          "above_high_threshold": score >= opts["high"],
                          "group": {"cue_start": gstart, "cue_end": gend, "text": group_candidate["text"], "score": group_candidate["score"]},
                          "differences": text_diff(cue["text"], reference["text"])})
            def available(candidate):
                sid=candidate['source_index']
                hits=source_hits.get(sid,[]);indices=source_cues.get(sid,[])
                prev=bisect_left(indices,gstart)-1;next_=bisect_left(indices,gend)
                prev_end=hits[prev][2] if prev>=0 else index.source_ranges[sid][0]
                next_start=hits[next_][1] if next_<len(hits) else index.source_ranges[sid][1]
                return prev_end<=candidate['norm_start'] and candidate['norm_end']<=next_start and within_context(candidate,gstart,gend)
            group_query=''.join(queries[gstart:gend]);group_candidates=index.candidates(group_query)
            sid=reference['source_index']
            source_matches=source_hits[sid];indices=source_cues[sid];prev=bisect_left(indices,gstart)-1;next_=bisect_left(indices,gend)
            if group_query in index.repeated_queries:
                group_candidates=group_candidates+index.exact_after(group_query,sid,source_matches[prev][2] if prev>=0 else index.source_ranges[sid][0],source_matches[next_][1] if next_<len(source_matches) else None)
            alternatives = [c for c in group_candidates if available(c)
                            and index.reliable(c, len(group_query)) and c["score"] >= group_candidate["score"] - opts["ambiguity"]
                            and not index.same_location(c, group_candidate)]
            alternatives=list({(c['source_index'],c['norm_start'],c['norm_end']):c for c in alternatives}.values())
            if alternatives:
                match["status"] = "ambiguous"
                match["alternatives"] = [{**c, "group_only": gend - gstart > 1,
                                           "differences": text_diff(cue["text"], c["text"]) if gend - gstart == 1 else []} for c in alternatives[:3]]
            cue["script_text"] = reference["text"]
            cue["script_index"] = reference["paragraph_start"] - 1
            cue["match_ratio"] = round(score, 4)
            cue["match_needs_review"] = match["status"] != "high"
            hits.append({"cue_index": ci, "script_index": cue["script_index"], "ratio": round(score, 4),
                         'source_id':reference['source_id'],'source_name':reference['source_name']})
        elif ci in blocked:
            context=contexts[ci]
            match.update(status='context_mismatch',context={**context,'status':'outside'},
                warning=f"前后台词已定位到《{context['source_name']}》，当前片段中未找到这句。其他位置命中不能证明此处存在，请试听复核。",
                alternatives=[{**c,'reference_only':True} for c in index.candidates(query)[:3]])
        elif ci not in excluded:
            # 不可靠或叙述片段仍可查看，但不作为可采用的对白建议。
            possible = singles.get(ci,[])
            if possible:
                top = possible[0]
                match.update({"character_score": top["character_score"], "pinyin_score": top["pinyin_score"],
                              "score": top["score"], "alternatives": possible[:3],
                              "warning": "候选仅供参考：可能是叙述、位置冲突或证据不足，不代表 ASR 错误。"})
        match["label"] = STATUS_LABELS[match["status"]]
        cue["reference_match"] = match
    # 校验同时标出整条笑声、叹气、嗯啊等语气词，供前端红色提示和校准控制导出。
    mark_interjections(result)
    counts = Counter(c["reference_match"]["status"] for c in result)
    paragraphs = deepcopy(doc["paragraphs"])
    for hit in hits:
        if paragraphs[hit["script_index"]]["cue_index"] is None:
            paragraphs[hit["script_index"]]["cue_index"] = hit["cue_index"]
    # 以真实字幕反查剧本，不从未匹配的剧本段落生成空白字幕或缺句行。
    gaps = []
    elapsed=time.perf_counter()-started
    check(1, '校验完成')
    log.info('参考校验 chars=%d cues=%d index_cache=%s index_seconds=%.4f total_seconds=%.4f candidate_queries=%d',
             len(doc['text']),len(cues),index.index_cache_hit,index.index_seconds,elapsed,index.candidate_queries)
    return {"cues": result, "lines": paragraphs, "document": doc, "hits": hits, "gaps": gaps,
            "stats": {"total": len(cues), "matched": len(hits), "needs_review": len(cues) - counts["high"], "statuses": dict(counts)},
            "options": opts, "engine": {"character": "RapidFuzz", "pinyin": "pypinyin", "sequence": "source_monotonic_beam_dp", "beam_width": BEAM_WIDTH,
                'source_beam':SOURCE_BEAM,'beam_max':BEAM_MAX,'index_cache_hit':index.index_cache_hit,'index_seconds':round(index.index_seconds,6),
                'total_seconds':round(elapsed,6),'candidate_queries':index.candidate_queries,'candidate_cache_hits':index.candidate_cache_hits}}
