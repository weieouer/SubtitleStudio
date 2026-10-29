# -*- coding: utf-8 -*-
"""不依赖真实音视频的流水线自测：用假 ASR 数据验证对齐、分段、SRT。"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import threading
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.pipeline.align import AlignedLine, TimedChar, align  # noqa: E402
from app.pipeline.extract import extraction_lines  # noqa: E402
from app.pipeline.script_parser import (  # noqa: E402
    DialogueLine,
    decode_script_bytes,
    parse_bytes,
    parse_file,
    parse_text,
    spoken_chars,
)
from app.pipeline.segment import (  # noqa: E402
    make_cues,
    split_spans,
    strip_fillers,
    strip_leading_fillers,
)
from app.pipeline.srt_out import write_srt  # noqa: E402
from app.pipeline.text import clean_cue_text  # noqa: E402

SCRIPT = ROOT / "samples" / "姐弟战天下_第10集.txt"


def fake_segments(lines) -> list[dict]:
    """按剧本顺序伪造 ASR 结果：带噪声、带一处错字、带长停顿。"""
    segments: list[dict] = []
    t = 1.0

    for index, line in enumerate(lines):
        segments.append({"start": t, "end": t + 0.45, "text": "嗯嗯"})
        t += 0.65

        chars = [c for c in line.text if c.isalnum() or "一" <= c <= "鿿"]
        duration = max(0.5, 0.22 * len(chars))
        step = duration / len(chars)
        words = []
        for k, ch in enumerate(chars):
            if index == 2 and k == 1:
                ch = "错"  # 模拟一处识别错字
            words.append(
                {
                    "word": ch,
                    "start": t + k * step,
                    "end": t + (k + 1) * step,
                }
            )
        segments.append(
            {
                "start": t,
                "end": t + duration,
                "text": "".join(w["word"] for w in words),
                "words": words,
            }
        )
        t += duration

        if index in (5, 11):
            segments.append({"start": t, "end": t + 0.6, "text": "咳嗽"})
            t += 0.8

        t += 1.3

    return segments


def main() -> None:
    assert strip_fillers("你来了呀") == "你来了"
    assert strip_fillers("你吃饭了吗") == "你吃饭了吗"
    assert strip_fillers("嗯嗯") == ""
    assert strip_leading_fillers("嗯，我知道了") == "，我知道了"
    assert strip_leading_fillers("哎呀妈呀") == "哎呀妈呀"

    assert clean_cue_text("你 好，世 界！") == "你好世界"
    assert clean_cue_text("♪ 你 & 我 ♥\n再见") == "你我再见"
    assert clean_cue_text("ＡＢＣ　１２３") == "ABC123"

    # 按完整词切分：即使刚好到达字数上限，也不从词中间断开。
    word_text = "他们明天一起去公园"
    word_spans = split_spans(word_text, max_chars=4)
    word_parts = [word_text[s:e] for s, e, _ in word_spans]
    boundaries = {e for _, e, _ in word_spans[:-1]}
    assert "".join(word_parts) == word_text
    assert all(len(clean_cue_text(part)) <= 4 for part in word_parts)
    for word in ("他们", "明天", "一起", "公园"):
        pos = word_text.index(word)
        assert not any(pos < b < pos + len(word) for b in boundaries), (
            f"完整词「{word}」被切开：{word_parts}"
        )

    # 自然停顿处断句，并标记 pause_before，供无缝衔接逻辑保留间隙。
    pause_text = "那个，这个，怎么说呢"
    pause_times = {
        0: (1.0, 1.2),
        1: (1.2, 1.4),
        3: (1.75, 1.95),
        4: (1.95, 2.15),
        6: (2.60, 2.80),
        7: (2.80, 3.00),
        8: (3.00, 3.20),
        9: (3.20, 3.40),
    }
    pause_spans = split_spans(pause_text, 14, pause_times)
    assert [clean_cue_text(pause_text[s:e]) for s, e, _ in pause_spans] == [
        "那个",
        "这个",
        "怎么说呢",
    ]
    assert [flag for _, _, flag in pause_spans] == [False, True, True]

    tight_times = {
        0: (1.0, 1.2),
        1: (1.2, 1.4),
        3: (1.50, 1.70),
        4: (1.70, 1.90),
        6: (2.10, 2.30),
        7: (2.30, 2.50),
        8: (2.50, 2.70),
        9: (2.70, 2.90),
    }
    assert len(split_spans(pause_text, 14, tight_times)) == 1, (
        "停顿很短时不应把一句话拆得过碎"
    )

    # 起始严格落在第一个真正显示的字上；结尾允许轻微延音。
    fade_line = DialogueLine(
        idx=1, speaker="林晚", note="", is_os=False, text="哎，你怎么才来"
    )
    fade_times = [
        TimedChar("哎", 0, 0, 1.0, 1.2),
        TimedChar("你", 0, 2, 1.5, 1.7),
        TimedChar("怎", 0, 3, 1.7, 1.9),
        TimedChar("么", 0, 4, 1.9, 2.1),
        TimedChar("才", 0, 5, 2.2, 2.4),
        TimedChar("来", 0, 6, 2.4, 2.6),
    ]
    fade_aligned = AlignedLine(
        line=fade_line,
        start=1.0,
        end=2.6,
        ratio=1.0,
        matched_chars=6,
        total_chars=6,
        timed=fade_times,
    )
    fade_cues = make_cues([fade_aligned], max_chars=14, no_gap=True)
    assert len(fade_cues) == 1
    assert fade_cues[0].text == "你怎么才来"
    assert fade_cues[0].start == 1.5, "字幕起始没有落在真正开口的字上"
    assert abs(fade_cues[0].end - (2.6 + 0.18)) < 1e-6, (
        "结尾应保留轻微延音，但不能过长"
    )

    shifted = make_cues(
        [fade_aligned], max_chars=14, no_gap=True, lead_in=0.2, tail_delay=0.3
    )
    assert abs(shifted[0].start - 1.3) < 1e-6, "整体提前没有生效"
    assert abs(shifted[0].end - 2.7) < 1e-6, "出现和消失没有一起提前"

    # 无缝衔接不能吞掉自然停顿。
    pause_line = DialogueLine(
        idx=2, speaker="林晚", note="", is_os=False, text=pause_text
    )
    pause_aligned = AlignedLine(
        line=pause_line,
        start=1.0,
        end=3.4,
        ratio=1.0,
        matched_chars=10,
        total_chars=10,
        timed=[
            TimedChar(pause_text[pos], 1, pos, start, end)
            for pos, (start, end) in pause_times.items()
        ],
    )
    pause_cues = make_cues(
        [pause_aligned], max_chars=14, no_gap=True, max_fill=1.0
    )
    assert [cue.text for cue in pause_cues] == ["那个", "这个", "怎么说呢"]
    assert pause_cues[0].end < pause_cues[1].start
    assert pause_cues[1].end < pause_cues[2].start

    # 字幕提取：没有剧本，按词级时间生成字幕；括号音效和句尾语气词要剔除。
    extract_segments = [
        {
            "start": 1.0,
            "end": 3.0,
            "text": "（音乐）嗯，你怎么才来呀",
            "words": [
                {"word": "（音乐）", "start": 1.0, "end": 1.4},
                {"word": "嗯", "start": 1.5, "end": 1.65},
                {"word": "，", "start": 1.65, "end": 1.65},
                {"word": "你", "start": 1.8, "end": 1.95},
                {"word": "怎么", "start": 1.95, "end": 2.35},
                {"word": "才来", "start": 2.35, "end": 2.75},
                {"word": "呀", "start": 2.75, "end": 2.9},
            ],
        },
        {"start": 4.0, "end": 5.2, "text": "我们明天一起去公园"},
        {"start": 6.0, "end": 6.4, "text": "（掌声）"},
    ]
    extract_lines = extraction_lines(extract_segments)
    assert len(extract_lines) == 2, "括号音效没有从提取行中过滤掉"
    extract_cues = make_cues(extract_lines, whole_lines=True)
    assert [cue.text for cue in extract_cues] == [
        "你怎么才来",
        "我们明天一起去公园",
    ]
    assert all("音乐" not in cue.text and "掌声" not in cue.text for cue in extract_cues)
    assert extract_cues[0].start == 1.8, "提取字幕没有落在真正开口的第一个字上"
    assert abs(extract_cues[0].end - (2.9 + 0.18)) < 1e-6

    # 提取模式按识别片段整段输出，不按单行字数拆成多条。
    long_segments = [
        {
            "start": 8.0,
            "end": 11.0,
            "text": "我们今天晚上一起去公园散步然后吃饭",
        }
    ]
    long_lines = extraction_lines(long_segments)
    long_cues = make_cues(long_lines, whole_lines=True)
    assert len(long_cues) == 1
    assert long_cues[0].text == "我们今天晚上一起去公园散步然后吃饭"
    assert long_cues[0].start == 8.0
    assert abs(long_cues[0].end - 11.18) < 1e-6

    from app.main import _resolve_export_dir, _unique_stem

    with tempfile.TemporaryDirectory() as temp_dir:
        export_dir = _resolve_export_dir(str(Path(temp_dir) / "字幕导出"))
        assert export_dir is not None and export_dir.is_dir()
        (export_dir / "成片.srt").write_text("", encoding="utf-8")
        assert _unique_stem(export_dir, "成片") == "成片 (2)"
        (export_dir / "成片 (2).srt").write_text("", encoding="utf-8")
        assert _unique_stem(export_dir, "成片") == "成片 (3)"

    # 模型状态机：只在点击后下载；同模型重复点击复用，不并发下载第二个模型。
    from app import model_manager as mm
    from app.pipeline import asr as asr_module
    from app.pipeline.asr import has_local_model

    with tempfile.TemporaryDirectory() as temp_dir:
        models_dir = Path(temp_dir)
        manager = mm.ModelManager(models_dir)

        def skip_modelscope(_model):
            raise RuntimeError("测试固定走镜像路径")

        manager._download_modelscope = skip_modelscope
        assert [item["status"] for item in manager.snapshot()["models"]] == [
            "missing",
            "missing",
            "missing",
        ]

        release = threading.Event()
        calls = []

        def fake_download(**kwargs):
            calls.append(kwargs)
            release.wait(5)
            repo = kwargs["repo_id"]
            snapshot = (
                models_dir
                / ("models--" + repo.replace("/", "--"))
                / "snapshots"
                / "test-revision"
            )
            snapshot.mkdir(parents=True)
            (snapshot / "model.bin").write_bytes(b"fake model")
            (snapshot / "config.json").write_text("{}", encoding="utf-8")
            (snapshot / "tokenizer.json").write_text("{}", encoding="utf-8")
            return str(snapshot)

        original_download = mm.snapshot_download
        mm.snapshot_download = fake_download
        try:
            first = manager.start_download("small")
            assert first["status"] == "downloading"
            for _ in range(100):
                if calls:
                    break
                time.sleep(0.01)
            assert calls, "下载线程没有启动"
            assert calls[0]["allow_patterns"] == mm.ALLOW_PATTERNS
            assert calls[0]["max_workers"] == 4

            duplicate = manager.start_download("small")
            assert duplicate["status"] == "downloading"
            try:
                manager.start_download("medium")
            except mm.ModelBusyError as exc:
                assert "正在下载" in str(exc)
            else:
                raise AssertionError("第二个模型没有被并发保护拦截")
            assert len(calls) == 1, "重复点击启动了多个模型下载"
            assert not has_local_model("small", models_dir)

            release.set()
            entry = None
            for _ in range(300):
                entry = next(
                    item for item in manager.snapshot()["models"] if item["id"] == "small"
                )
                if entry["status"] == "ready":
                    break
                time.sleep(0.01)
            assert entry is not None and entry["status"] == "ready", entry
            assert has_local_model("small", models_dir)
            assert manager.snapshot()["active"] is None
        finally:
            release.set()
            mm.snapshot_download = original_download

    # 国内源成功时直接完成，不应再触发镜像下载。
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = mm.ModelManager(Path(temp_dir))
        hf_calls = []

        def fake_modelscope(model):
            repo = mm.MODEL_CATALOG[model]["repo"]
            snapshot = (
                Path(temp_dir)
                / ("models--" + repo.replace("/", "--"))
                / "snapshots"
                / "modelscope-master"
            )
            snapshot.mkdir(parents=True)
            (snapshot / "model.bin").write_bytes(b"fake model")
            (snapshot / "config.json").write_text("{}", encoding="utf-8")
            (snapshot / "tokenizer.json").write_text("{}", encoding="utf-8")

        manager._download_modelscope = fake_modelscope
        manager._download_hf = lambda model: hf_calls.append(model)
        manager._download("small")
        assert has_local_model("small", Path(temp_dir))
        assert hf_calls == [], "国内源成功后仍触发了镜像下载"

    # 断点续传：部分 CDN 会返回 200 + Content-Range，不能把已有字节覆盖掉。
    import http.server

    payload = b"0123456789" * 100

    class RangeHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            header = self.headers.get("Range", "")
            start = int(header.split("=")[1].split("-")[0]) if header else 0
            body = payload[start:]
            self.send_response(200)
            self.send_header(
                "Content-Range",
                f"bytes {start}-{len(payload) - 1}/{len(payload)}",
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    range_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    range_thread = threading.Thread(
        target=range_server.serve_forever, daemon=True
    )
    range_thread.start()
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = Path(temp_dir)
            (snapshot / "model.bin.part").write_bytes(payload[:321])
            manager = mm.ModelManager(snapshot)
            original_url = mm.MODELSCOPE_FILE_URL
            mm.MODELSCOPE_FILE_URL = (
                f"http://127.0.0.1:{range_server.server_address[1]}/{{path}}"
            )
            try:
                manager._download_modelscope_entries(
                    "small",
                    "Systran/faster-whisper-small",
                    snapshot,
                    [("model.bin", len(payload))],
                )
            finally:
                mm.MODELSCOPE_FILE_URL = original_url
            assert (snapshot / "model.bin").read_bytes() == payload
    finally:
        range_server.shutdown()
        range_thread.join(timeout=2)

    with tempfile.TemporaryDirectory() as temp_dir:
        manager = mm.ModelManager(Path(temp_dir))

        def skip_modelscope(_model):
            raise RuntimeError("测试固定走镜像路径")

        manager._download_modelscope = skip_modelscope

        def failing_download(**kwargs):
            raise TimeoutError("timed out")

        original_download = mm.snapshot_download
        mm.snapshot_download = failing_download
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                manager.start_download("medium")
                failed = None
                for _ in range(300):
                    failed = next(
                        item
                        for item in manager.snapshot()["models"]
                        if item["id"] == "medium"
                    )
                    if failed["status"] == "failed":
                        break
                    time.sleep(0.01)
            assert failed is not None and failed["status"] == "failed", failed
            assert "超时" in failed["error"]
        finally:
            mm.snapshot_download = original_download

    # 没有本地模型时直接拒绝，不会隐式联网下载。
    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            asr_module.get_model("large-v3", "cpu", Path(temp_dir))
        except asr_module.ModelNotDownloadedError as exc:
            assert "点击下载" in str(exc)
        else:
            raise AssertionError("未下载的模型被隐式加载了")

    inline = parse_text(
        "林雨梦：（失声）傻蛋！\n"
        "彭战：别过来！（握紧铁棍）\n"
        "光头彪：【大喊】都给我上！"
    )
    assert [clean_cue_text(line.text) for line in inline] == [
        "傻蛋",
        "别过来",
        "都给我上",
    ], "括号或方括号里的表演提示没有剔除"

    numbered = parse_text(
        "角色1:今天，我们来测一下字幕提取\r\n"
        "角色2:这段话说的稍微有点快\r\n"
        "角色3:你能听清吗\r\n"
        "角色4:那个我再讲一遍吧"
    )
    assert [line.speaker for line in numbered] == [
        "角色1",
        "角色2",
        "角色3",
        "角色4",
    ], "「角色编号 + 半角冒号」格式没有正确提取"
    assert [clean_cue_text(line.text) for line in numbered] == [
        "今天我们来测一下字幕提取",
        "这段话说的稍微有点快",
        "你能听清吗",
        "那个我再讲一遍吧",
    ]

    # 剧本预检：UTF-8 BOM 与 GB18030 都能读，统计口径与界面一致。
    sample_text = (
        "角色1:今天，我们来测一下字幕提取\r\n"
        "角色2:这段话说的稍微有点快\r\n"
        "角色3:你能听清吗\r\n"
        "角色4:那个我再讲一遍吧"
    )
    sample_utf8 = sample_text.encode("utf-8-sig")
    sample_gb = sample_text.encode("gb18030")
    assert decode_script_bytes(sample_utf8).startswith("角色1")
    assert not decode_script_bytes(sample_utf8).startswith("﻿")
    for data in (sample_utf8, sample_gb):
        inspected = parse_bytes(data)
        assert len(inspected) == 4
        assert len({line.speaker for line in inspected}) == 4
        assert sum(spoken_chars(line.text) for line in inspected) == 35
        assert sum(1 for line in inspected if line.is_os) == 0

    lines = parse_file(SCRIPT)
    assert len(lines) == 17, f"对白提取数量不对：{len(lines)}"

    segments = fake_segments(lines)
    result = align(lines, segments)

    unaligned = [al.line.idx for al in result.lines if al.start is None]
    assert not unaligned, f"有台词没对上：{unaligned}"
    assert result.noise, "没有识别出噪声片段"

    cues = make_cues(
        result.lines,
        max_chars=14,
        include_os=True,
        no_gap=True,
        max_fill=0.6,
    )
    assert cues, "没有生成字幕"

    by_line = defaultdict(list)
    for cue in cues:
        by_line[cue.line_idx].append(cue)

    os_lines = [line for line in lines if line.is_os]
    assert os_lines, "示例剧本里应当有内心独白"
    for line in os_lines:
        assert by_line[line.idx], f"内心独白第 {line.idx} 句没有生成字幕"

    for line in lines:
        joined = "".join(c.text for c in by_line[line.idx])
        expected = strip_fillers(
            clean_cue_text(strip_leading_fillers(line.text))
        )
        assert joined == expected, (
            f"第 {line.idx} 句分段后文字不一致：{joined!r} != {expected!r}"
        )

    assert all(cue.text == strip_fillers(cue.text) for cue in cues), (
        "字幕里还残留句首或句尾语气词"
    )
    assert all(cue.text == clean_cue_text(cue.text) for cue in cues), (
        "字幕里还残留标点、符号、空格或换行"
    )
    assert all(len(cue.text) <= 14 for cue in cues), "有字幕超过单行字数上限"

    for line_idx, line_cues in by_line.items():
        for a, b in zip(line_cues, line_cues[1:]):
            assert abs(a.end - b.start) < 0.02, (
                f"第 {line_idx} 句分段之间有缝：{a.end} / {b.start}"
            )

    with tempfile.TemporaryDirectory() as temp_dir:
        out = Path(temp_dir) / "test.srt"
        write_srt(cues, out)
        print(
            f"OK  对白 {len(lines)} 句 · 噪声 {len(result.noise)} 段 · "
            f"字幕 {len(cues)} 条 · 匹配率 {result.total_matched / result.total_chars:.1%}"
        )
        print(out.read_text(encoding="utf-8-sig")[:260])


if __name__ == "__main__":
    main()
