# -*- coding: utf-8 -*-
"""输出剪映可直接导入的 SRT。"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import srt

from .segment import Cue


def write_srt(cues: list[Cue], path: str | Path) -> Path:
    subs = []
    for index, cue in enumerate(cues, start=1):
        start = timedelta(milliseconds=round(cue.start * 1000))
        end = timedelta(milliseconds=round(cue.end * 1000))
        if end <= start:
            end = start + timedelta(milliseconds=250)
        subs.append(srt.Subtitle(index=index, start=start, end=end, content=cue.text))

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(srt.compose(subs), encoding="utf-8-sig")
    return output
