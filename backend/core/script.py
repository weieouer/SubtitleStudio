"""剧本解析、与 ASR 结果模糊匹配。"""
from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

def _docx_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    lines: list[str] = []
    for p in root.iter(f"{ns}p"):
        line = "".join(t.text or "" for t in p.iter(f"{ns}t")).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def read_script_text(path: str) -> tuple[str, str]:
    suffix = Path(path).suffix.lower()
    if suffix == ".docx":
        return _docx_text(path), "docx"
    if suffix == ".doc":
        raise RuntimeError("暂不支持 .doc，请另存为 .docx 或 .txt")
    from .srt_io import read_text
    return read_text(path)


def parse_script_text(text: str) -> list[dict]:
    """Compatibility API: format-free paragraphs, preserving original positions."""
    from .reference_match import parse_document
    return parse_document(text)["paragraphs"]


def match_script(cues: list[dict], lines: list[dict]) -> dict:
    """Compatibility API: delegate to the single full-text matching engine."""
    from .reference_match import match_reference
    return match_reference(cues, {"text": "\n".join(line["text"] for line in lines)})
