# -*- coding: utf-8 -*-
"""台词字符判定：只保留真正念出来的字。

标点、符号、空格、换行都不属于台词，只作为断句和分段的间隔。
"""
from __future__ import annotations

import unicodedata


def is_spoken(ch: str) -> bool:
    """是否为台词字符：各类文字与数字，不含标点、符号与空白。"""
    category = unicodedata.category(ch)
    return category.startswith("L") or category in {"Nd", "Nl"}


def clean_cue_text(text: str) -> str:
    """清洗最终字幕：统一全半角，只留下台词字符。"""
    normalized = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in normalized if is_spoken(ch))
