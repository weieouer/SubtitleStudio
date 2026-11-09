"""Whisper 幻觉字幕识别。

中文 Whisper 在音乐、环境音和长静音上会复读训练数据里的片尾口播
（"请不吝点赞、订阅、转发、打赏支持明镜与点点栏目"之类）。这些句子有几个共同点：
命中训练数据里的固定说法、时长远长于字数能支撑的长度、模型自己的 no_speech 偏高。

只做两件事：给条目加标记，并默认把它排除在预览和导出之外。
字幕文字、原始时间和 ASR 记录都保留，用户随时可以恢复导出或删除。
"""
from __future__ import annotations

import unicodedata

# 训练数据里反复出现的口播/频道信息。短剧对白不会在几秒里连续出现两个以上。
PHRASES = (
    "请不吝", "点赞订阅", "点赞关注", "订阅转发", "转发打赏", "打赏支持",
    "明镜与点点", "明镜", "点点栏目", "小铃铛", "字幕由", "字幕组",
    "感谢观看", "谢谢观看", "一键三连", "订阅频道",
)
# 几乎只出现在这类片尾里的说法，命中即可确认。
STRONG = ("明镜与点点", "点点栏目", "小铃铛", "字幕由", "一键三连")


def _plain(text: str) -> str:
    """去标点空白后的比较用文本；保留汉字、字母和数字。"""
    return "".join(char for char in unicodedata.normalize("NFKC", text or "")
                   if char.isalnum())


SUSPECT_NO_SPEECH = 0.5


def analyze(cue: dict) -> dict | None:
    """返回幻觉判定，None 表示正常字幕。只看文本、时长和模型分数。

    confirmed=True 的条目默认不导出；confirmed=False 的只是提醒，导出保持原样。
    """
    text = str(cue.get("text") or "")
    plain = _plain(text)
    if not plain:
        return None
    duration = max(0.0, float(cue.get("end", 0) or 0) - float(cue.get("start", 0) or 0))
    rate = len(plain) / duration if duration > 0 else 99.0
    hits = [phrase for phrase in PHRASES if phrase in plain]
    known = bool(hits)
    no_speech = float(cue.get("no_speech_prob") or 0)
    reasons = []
    if known:
        reasons.append("known-text")
    if duration >= 6 and rate <= 0.8:
        reasons.append("sparse")
    if no_speech >= SUSPECT_NO_SPEECH:
        reasons.append("no-speech")
    if not reasons:
        return None
    strong = any(phrase in plain for phrase in STRONG)
    # 确证：口播用语 + 第二信号、极强用语、或 20 秒以上每字超过 4 秒的极端稀疏段。
    confirmed = ((known and len(reasons) >= 2) or (strong and duration >= 3)
                 or (duration >= 20 and rate <= 0.25))
    suspect = confirmed or len(reasons) >= 2 or strong
    if not suspect:
        return None
    return {"reasons": reasons, "phrases": hits, "confirmed": confirmed,
            "chars_per_second": round(rate, 2), "duration": round(duration, 2)}


REPEAT_RUN = 3


def _mark_repeats(cues: list[dict]) -> None:
    """连续三条以上文字完全相同的字幕按循环幻觉提醒，不自动排除。

    复读循环是 Whisper 在静音或音乐上的典型幻觉，但短句连说也可能真实存在，
    所以只加“需复核”标记，导出仍由用户决定。
    """
    run: list[dict] = []

    def flush() -> None:
        if len(run) < REPEAT_RUN or len(_plain(run[0].get("text"))) < 2:
            return
        for cue in run:
            if cue.get("hallucination") or cue.get("review_decision") == "included":
                continue
            cue["hallucination"] = {"reasons": ["repeated-text"], "phrases": [],
                                    "confirmed": False, "automatic_exclusion": False,
                                    "repeats": len(run)}
            cue["needs_review"] = True

    for cue in cues:
        plain = _plain(cue.get("text"))
        same = bool(run) and _plain(run[-1].get("text")) == plain
        if plain and same:
            run.append(cue)
        else:
            flush()
            run = [cue] if plain else []
    flush()


def mark(cues: list[dict], *, auto_exclude: bool = True) -> list[dict]:
    """就地标记幻觉条目。返回自动排除（不导出）的条目。

    已标记过的条目不再重复处理，尊重用户手工恢复导出的决定。
    """
    _mark_repeats(cues)
    marked = []
    for cue in cues:
        if cue.get("hallucination") or cue.get("review_decision") == "included":
            continue
        info = analyze(cue)
        if not info:
            continue
        if not auto_exclude:
            info={**info,'confirmed':False,'automatic_exclusion':False}
        cue["hallucination"] = info
        cue["needs_review"] = True
        if auto_exclude and info["confirmed"]:
            cue["is_noise"] = True
            cue["exclude_export"] = True
            marked.append(cue)
    return marked
