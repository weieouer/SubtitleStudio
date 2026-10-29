# -*- coding: utf-8 -*-
"""从漫剧剧本中提取对白行。

剧本格式约定（以《姐弟战天下》为例）：
  【剧名/集数】、【音效】：...、△ 动作描写、第X集-场N｜...   —— 都不是对白
  角色：台词
  角色（表演提示）：台词     —— 括号内提示不进字幕
  彭战（OS）：...            —— 内心独白，默认保留为字幕（通常是配音）
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

from .text import is_spoken

DIALOGUE_RE = re.compile(r"^([^\s：:△【｜（）]{1,12})(?:（([^）]*)）)?\s*[：:]\s*(.+)$")
TITLE_RE = re.compile(r"^第\s*\d+\s*集")
SKIP_PREFIXES = ("【", "△", "#", "—")
# 台词里的括号提示只是表演说明，不属于台词。
_INLINE_NOTE_RE = re.compile(r"[（(【\[〔{][^）)】\]〕}]*[）)】\]〕}]")


@dataclass
class DialogueLine:
    idx: int
    speaker: str
    note: str
    is_os: bool
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


def spoken_chars(text: str) -> int:
    """只统计真正发音的字符，用于估算时长和分段。"""
    return sum(1 for ch in text if is_spoken(ch))


def decode_script_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_script_text(path: str | Path) -> str:
    return decode_script_bytes(Path(path).read_bytes())


def parse_text(raw: str) -> list[DialogueLine]:
    lines: list[DialogueLine] = []

    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(SKIP_PREFIXES):
            continue

        m = DIALOGUE_RE.match(line)
        if not m:
            continue

        speaker, note, text = m.group(1), (m.group(2) or ""), m.group(3).strip()
        text = unicodedata.normalize("NFKC", _INLINE_NOTE_RE.sub("", text)).strip()
        if not text or TITLE_RE.match(speaker) or text.startswith("《"):
            continue

        lines.append(
            DialogueLine(
                idx=len(lines) + 1,
                speaker=speaker,
                note=note,
                is_os="OS" in note.upper(),
                text=text,
            )
        )

    return lines


def parse_bytes(data: bytes) -> list[DialogueLine]:
    return parse_text(decode_script_bytes(data))


def parse_file(path: str | Path) -> list[DialogueLine]:
    return parse_text(read_script_text(path))
