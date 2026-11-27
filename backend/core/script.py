"""读取 TXT/MD/DOCX 原文；参考解析和匹配统一由 reference_match 处理。"""
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
        parts = []
        for node in p.iter():
            if node.tag == f"{ns}t":
                parts.append(node.text or "")
            elif node.tag in (f"{ns}br", f"{ns}cr"):
                parts.append("\n")
            elif node.tag == f"{ns}tab":
                parts.append("\t")
        lines.append("".join(parts))
    return "\n".join(lines)


def read_script_text(path: str) -> tuple[str, str]:
    suffix = Path(path).suffix.lower()
    if suffix == ".docx":
        return _docx_text(path), "docx"
    if suffix == ".doc":
        raise RuntimeError("暂不支持 .doc，请另存为 .docx 或 .txt")
    from .srt_io import read_text
    return read_text(path)
