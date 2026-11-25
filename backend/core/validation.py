"""HTTP输入类型与数值边界；不将不可信JSON传给业务函数。"""
from __future__ import annotations

import math


def number(value, label: str, low: float = 0, high: float = 10 ** 9) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}必须是数字")
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{label}需在{low:g}–{high:g}之间")
    return value


def mapping(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label}必须是对象")
    return value


def boolean(value, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label}必须是布尔值")
    return value


def cues(value, *, repair_word_times=False) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("cues必须是数组")
    if len(value) > 100000:
        raise ValueError("字幕条目过多")
    for i, cue in enumerate(value, 1):
        mapping(cue, f"第{i}条字幕")
        number(cue.get("start"), f"第{i}条开始时间")
        end = number(cue.get("end"), f"第{i}条结束时间")
        if end <= cue["start"]:
            raise ValueError(f"第{i}条结束时间必须晚于开始时间")
        if not isinstance(cue.get("text"), str):
            raise ValueError(f"第{i}条text必须是字符串")
        for field in ("role", "script_text", "source", "manual_reference"):
            if field in cue and not isinstance(cue[field], str):
                raise ValueError(f"第{i}条{field}必须是字符串")
        for field in ("confidence", "match_ratio", "no_speech_prob"):
            if cue.get(field) is not None:
                # 旧匹配器的长度罚分最多为 0.12，零相似度的组合子条可能存成负数。
                ratio = cue[field]
                if field == 'match_ratio' and type(ratio) in (int, float) and -.12 <= ratio < 0:
                    cue[field] = 0.
                    match = cue.get('reference_match')
                    if isinstance(match, dict) and match.get('score') == ratio:
                        match['score'] = 0.
                number(cue[field], f"第{i}条{field}", 0, 1)
        words = cue.get("words", [])
        if not isinstance(words, list):
            raise ValueError(f"第{i}条words必须是数组")
        damaged=False
        for wi, word in enumerate(words,1):
            mapping(word, "词时间")
            start = number(word.get("start"), "词开始")
            end = number(word.get("end"), "词结束")
            if not isinstance(word.get("word"), str):
                raise ValueError(f"第{i}条字幕第{wi}词文本无效")
            if end < start:
                if not repair_word_times:raise ValueError(f"第{i}条字幕第{wi}词结束时间早于开始时间")
                damaged=True
        if damaged:
            from .audio_boundaries import synchronize_word_times
            synchronize_word_times(cue)
    return value


def script_names(value) -> list[str]:
    if not isinstance(value, list) or len(value) > 10000 or any(not isinstance(name, str) for name in value):
        raise ValueError("剧本文件名必须是字符串数组（最多10000份）")
    return value


def lines(value) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("lines必须是数组")
    for line in value:
        mapping(line, "剧本行")
        for field in ("text", "role", "kind"):
            if not isinstance(line.get(field), str):
                raise ValueError(f"剧本{field}必须是字符串")
        index = line.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("剧本index必须是非负整数")
    return value
