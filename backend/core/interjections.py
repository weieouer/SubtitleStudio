"""识别和去除整条及句首、句末的语气词。

校验用红色“语气词”标出整条笑声、叹气、填充音；关闭“语气词”开关时，
提取和校准会直接删除这些整条字幕，并去掉对话句首、句末的“啊、呀、
哎、唉、哈哈”等语气词。开启时保留整条语气词，校准只去掉可靠参考里
没有的语气词。

只处理句首、句末和标点之间的独立语气词；句中正常字词、引号里的“啊”
以及“呢、吗、吧”等语法助词不动。
"""
from __future__ import annotations

import re
import unicodedata

# 整条全是这些字时判为语气词。笑声、叹气、咂嘴/嗤声、迟疑填充音分开维护，便于调整。
LAUGH = set("哈呵嘿嘻噗呲嗤嘎嗬")
SIGH = set("唉哎嗐嘶呼")
DISDAIN = set("哼切啧嘁呸嘘咄")
FILLER = set("嗯唔呃额诶欸咦")
STRONG = LAUGH | SIGH | DISDAIN | FILLER
# “啊呀哇哦”也可能是正常对白；单独成条时按语气词删除，句中保留。
WEAK = set("啊呀哇哦噢喔哟")

# 行首/行末清理专用。排除“呼、嘶、切、额”等常出现在正常词尾的字
# （招呼、声嘶、一切、余额），避免把正常台词截断。
BOUNDARY = (STRONG | WEAK) - set("呼嘶切额")

# 模型偶尔直接输出舞台说明；整条命中才丢，句中出现不动。
STAGE_WORDS = {
    "笑", "笑声", "大笑", "哈哈大笑", "苦笑", "冷笑", "偷笑",
    "叹气", "叹息", "唉声叹气", "哭声", "喘气", "呼吸声",
}

_PAUSE = '，,、；;。！？!?…'
_RUN = '[' + re.escape(''.join(sorted(BOUNDARY))) + ']+'
_LEAD_TRAIL = '，,、；;'
_OPENERS = '“"\'‘（(【['
_PAUSE_CLASS = '[' + re.escape(_PAUSE) + ']'

# 1) 两个标点之间的独立语气词：连同前一个标点去掉，保留后一个标点。
_ISOLATED = re.compile(rf'{_PAUSE_CLASS}{_RUN}(?={_PAUSE_CLASS})')
# 2) 行首语气词：后面必须是逗号、空白或结尾，避免截断“哈哈镜”这类正常词。
_LEADING = re.compile(rf'^[ \t]*{_RUN}(?:[{re.escape(_LEAD_TRAIL)} \t]+|$)')
# 3) 行末语气词，连同前面的逗号/分号一起去掉。
_TRAILING = re.compile(rf'[{re.escape(_LEAD_TRAIL)} \t]*{_RUN}[ \t]*$')
# 4) 句末语气词：紧挨句号/问号/叹号，但不动引号里的“啊！”。
_BEFORE_TERMINAL = re.compile(rf'(?<![{re.escape(_OPENERS)}]){_RUN}(?=[。！？!?…])')
# 5) 句号/问号/叹号后紧跟的语气词：后面必须是逗号、空白或结尾。
_AFTER_TERMINAL = re.compile(rf'(?<=[。！？!?…]){_RUN}(?:[{re.escape(_LEAD_TRAIL)} \t]+|$)')
_LEADING_PUNCT = re.compile(rf'^{_PAUSE_CLASS}+')
_TRAILING_COMMA = re.compile(rf'[{re.escape(_LEAD_TRAIL)}]+$')
# 去掉“我们走吧，啊！”里的“啊”后，会留下悬空逗号，直接去掉。
_COMMA_BEFORE_TERMINAL = re.compile(rf'[{re.escape(_LEAD_TRAIL)}](?=[。！？!?…])')
# 去掉“我们走吧。啊！”里的“啊”后，句号与叹号会相邻，只留更强的那个。
_TERMINAL_PAIR = re.compile(r'。(?=[！？!?])|(?<=[！？!?])。')
_SPACES = re.compile(r'[ \t]{2,}')


def _plain(text: str) -> str:
    """去掉标点、符号、括号和空白，只留下用于判断的字。"""
    return "".join(ch for ch in str(text or "")
                   if not (ch.isspace() or unicodedata.category(ch)[0] in "PSZC"))


def is_interjection(text: str) -> bool:
    """整条字幕是否只是语气词/笑声/叹气。"""
    plain = _plain(text)
    if not plain:
        return False
    if plain in STAGE_WORDS:
        return True
    chars = set(plain)
    # 单独的“啊/哦/呀/哼/呵/嗯？”也算语气词：关闭开关时整条删除。
    if chars <= (STRONG | WEAK):
        return True
    return False


def strip_boundary_interjections(text: str) -> str:
    """去掉句首、句末和标点之间的语气词，保留句中正常字词。

    只删除独立出现或紧贴句末标点的语气词；引号里的对白和句中
    “你呀，就是……”这类语气词不删。整条都是语气词时返回空串。
    """
    original = str(text or '')
    value = original
    for _ in range(4):
        previous = value
        value = _ISOLATED.sub('', value)
        value = _LEADING.sub('', value)
        value = _TRAILING.sub('', value)
        value = _AFTER_TERMINAL.sub('', value)
        value = _BEFORE_TERMINAL.sub('', value)
        if value == previous:
            break
    if value == original:
        return original
    value = _LEADING_PUNCT.sub('', value)
    value = _TRAILING_COMMA.sub('', value)
    value = _COMMA_BEFORE_TERMINAL.sub('', value)
    value = _TERMINAL_PAIR.sub('', value)
    value = _SPACES.sub(' ', value).strip()
    return value if _plain(value) else ''


def strip_interjection_cues(cues: list[dict]) -> tuple[list[dict], int, int]:
    """关闭语气词时清理提取结果。

    返回（保留的字幕, 删除的整条语气词数, 去掉句首句末语气词的条数）。
    保留的字幕会原地写入 text/edited/modification_complete。
    """
    kept: list[dict] = []
    removed = trimmed = 0
    for cue in cues:
        if cue.get('review_decision') == 'included':
            kept.append(cue)
            continue
        original = str(cue.get('text') or '')
        if is_interjection(original):
            removed += 1
            continue
        text = strip_boundary_interjections(original)
        if text != original:
            if not _plain(text):
                removed += 1
                continue
            cue['text'] = text
            cue['edited'] = True
            cue['modification_complete'] = False
            trimmed += 1
        kept.append(cue)
    return kept, removed, trimmed


def mark_interjections(cues: list[dict]) -> list[dict]:
    """给每条字幕写入 interjection 标记并返回命中的条目，不删除字幕。

    校验用这个标记显示红色“语气词”；提取在开关关闭时已经删除，
    开关开启时保留并在这里标记。
    """
    flagged: list[dict] = []
    for cue in cues:
        hit = is_interjection(cue.get("text", ""))
        cue["interjection"] = hit
        if hit:
            flagged.append(cue)
    return flagged


def drop_interjections(cues: list[dict]) -> tuple[list[dict], list[dict]]:
    """返回（保留的字幕, 被丢弃的字幕摘要）；保留给旧接口和测试使用。"""
    kept: list[dict] = []
    dropped: list[dict] = []
    for cue in cues:
        if is_interjection(cue.get("text", "")):
            dropped.append({"id": cue.get("id"), "text": cue.get("text", "")})
        else:
            kept.append(cue)
    return kept, dropped
