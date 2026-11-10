"""SRT 解析与格式化（编码自动识别，UTF-8 输出）。"""
from __future__ import annotations

import codecs
import math
import re
import unicodedata
from pathlib import Path

TIME_RE = re.compile(
    r"(\d{1,3}):([0-5]?\d):([0-5]?\d)[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,3}):([0-5]?\d):([0-5]?\d)[,.](\d{1,3})"
)
INDEX_RE = re.compile(r"^\s*(\d+)\s*$")
PUNCT_END = set("。！？!?…；;，,、：:")
QUESTION_TAIL = set("吗呢么")
EXCLAM_TAIL = set("啊呀吧啦")


def normalize_subtitle_punctuation(text: str) -> str:
    """开启标点时保留原文全部标点；关闭时使用独立去标点函数。"""
    return str(text or '').strip()


def strip_subtitle_punctuation(text: str) -> str:
    """关闭“标点符号”时使用的去标点文本；保护数值、单位和英文词内部符号。"""
    text = str(text or '').strip()
    chars = []
    for i, char in enumerate(text):
        previous = text[i - 1] if i else ''
        following = text[i + 1] if i + 1 < len(text) else ''
        internal = ((char in '.,:/' and previous.isdigit() and following.isdigit())
                    or (char in "-'’" and previous.isascii() and previous.isalnum()
                        and following.isascii() and following.isalnum()))
        unit = char in '%％‰' and previous.isdigit()
        if (not unicodedata.category(char).startswith('P') or internal or unit) and char != '⋯':
            chars.append(char)
    return re.sub(r'[ \t]{2,}', ' ', ''.join(chars)).strip()


def read_text(path: str) -> tuple[str, str]:
    """读取文本，返回 (文本, 编码)。依次尝试 UTF-8 / GB18030 / Big5。"""
    raw = Path(path).read_bytes()
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        return raw.decode("utf-16"), "utf-16"
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000.0


def parse_srt_text(text: str) -> tuple[list[dict], list[str]]:
    """解析 SRT 文本。返回 (cues, warnings)。"""
    warnings: list[str] = []
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return [], ["文件为空"]

    blocks = re.split(r"\n\s*\n", text)
    cues: list[dict] = []
    seen_ids: set[int] = set()
    auto_id = 1

    for bi, block in enumerate(blocks, 1):
        lines = [ln.rstrip() for ln in block.split("\n")]
        lines = [ln for ln in lines if ln.strip() != ""]
        if not lines:
            continue

        time_idx = -1
        match = None
        for i, ln in enumerate(lines):
            m = TIME_RE.search(ln)
            if m:
                time_idx = i
                match = m
                break
        if match is None:
            warnings.append(f"第 {bi} 个块没有时间轴，已跳过")
            continue

        start = _to_seconds(*match.groups()[:4])
        end = _to_seconds(*match.groups()[4:8])
        if end <= start:
            warnings.append(f"第 {bi} 个块结束时间不大于开始时间")

        cid = None
        if time_idx > 0 and INDEX_RE.match(lines[time_idx - 1]):
            cid = int(INDEX_RE.match(lines[time_idx - 1]).group(1))
        if cid is None:
            cid = auto_id
        if cid in seen_ids:
            warnings.append(f"第 {bi} 个块序号重复：{cid}")
        seen_ids.add(cid)
        auto_id = max(auto_id, cid + 1)

        content = "\n".join(lines[time_idx + 1:]).strip()
        if not content:
            warnings.append(f"第 {bi} 个块没有字幕文本")

        cues.append({
            "id": cid,
            "start": round(start, 3),
            "end": round(end, 3),
            "text": content,
            "source": "srt",
        })

    if not cues:
        warnings.append("没有解析到有效字幕")
        return [], warnings

    original = list(cues)
    cues.sort(key=lambda c: (c["start"], c["end"]))
    if [c["id"] for c in cues] != [c["id"] for c in original]:
        warnings.append("字幕顺序与序号不一致，已按时间排序")

    return cues, warnings


def parse_srt_file(path: str) -> tuple[list[dict], list[str], str]:
    text, encoding = read_text(path)
    cues, warnings = parse_srt_text(text)
    return cues, warnings, encoding


def format_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def add_end_punctuation(text: str) -> str:
    text = normalize_subtitle_punctuation(text)
    if not text:
        return text
    tail = text[-1]
    if tail in PUNCT_END or tail in "”\"'』」）)":
        return text
    if tail in QUESTION_TAIL:
        return text + "？"
    if tail in EXCLAM_TAIL:
        return text + "！"
    return text


def prepare_export_cues(cues: list[dict], *, keep_punctuation: bool = True,
                        skip_empty: bool = True, single_line: bool = False) -> list[dict]:
    """在副本上处理导出文本，不改工程；保留数字和英文词内部的连接符。"""
    result = []
    for cue in cues:
        if cue.get('exclude_export') or cue.get('review_decision') == 'non_script':
            continue
        text = strip_subtitle_punctuation(cue["text"]) if not keep_punctuation else normalize_subtitle_punctuation(cue["text"])
        if single_line:
            text = re.sub(r"\s*\r?\n\s*", " ", text).strip()
        if skip_empty and not text:
            continue
        result.append({**cue, "text": text})
    return result


def format_srt(cues: list[dict], *, add_punctuation: bool = False, with_role: bool = False) -> str:
    """格式化为 SRT 文本；时间轴升序、序号重排。"""
    ordered = sorted(cues, key=lambda c: (float(c.get("start", 0)), float(c.get("end", 0))))
    out: list[str] = []
    for i, cue in enumerate(ordered, 1):
        start = float(cue.get("start", 0))
        end = max(float(cue.get("end", start)), start + 0.001)
        text = (cue.get("text") or "").strip()
        if with_role and cue.get("role") and cue["role"] != "待确认":
            text = f"{cue['role']}：{text}"
        if add_punctuation:
            text = add_end_punctuation(text)
        out.append(str(i))
        out.append(f"{format_timestamp(start)} --> {format_timestamp(end)}")
        out.append(text)
        out.append("")
    return "\r\n".join(out)


def validate_cues(cues: list[dict], duration: float = 0) -> list[str]:
    """导出前检查，返回错误列表（空表示通过）。"""
    errors: list[str] = []
    if not cues:
        return ["没有可导出的字幕"]
    for i, cue in enumerate(cues, 1):
        if not isinstance(cue, dict):
            errors.append(f"第 {i} 条必须是字幕对象")
            continue
        try:
            start = float(cue.get("start"))
            end = float(cue.get("end"))
        except (TypeError, ValueError):
            errors.append(f"第 {i} 条时间无效")
            continue
        if not (math.isfinite(start) and math.isfinite(end)):
            errors.append(f"第 {i} 条时间必须是有限数字")
            continue
        if start < 0:
            errors.append(f"第 {i} 条开始时间为负数")
        if end <= start:
            errors.append(f"第 {i} 条结束时间必须晚于开始时间")
        if duration and (start > duration + 0.001 or end > duration + 0.001):
            errors.append(f"第 {i} 条超出视频时长")
        if not isinstance(cue.get("text"), str) or not cue["text"].strip():
            errors.append(f"第 {i} 条没有文本")
    valid = [(float(c["start"]), float(c["end"]), i) for i, c in enumerate(cues, 1)
             if isinstance(c, dict) and isinstance(c.get("start"), (int, float))
             and isinstance(c.get("end"), (int, float))
             and math.isfinite(c["start"]) and math.isfinite(c["end"])]
    latest_end = -1.0
    for start, end, i in sorted(valid):
        if start < latest_end - 0.001:
            errors.append(f"第 {i} 条与前面字幕时间重叠，请调整后导出")
        latest_end = max(latest_end, end)
    if len(errors) > 20:
        errors = errors[:20] + [f"……还有 {len(errors) - 20} 个问题"]
    return errors
