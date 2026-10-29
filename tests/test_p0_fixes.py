# -*- coding: utf-8 -*-
"""P0 修复的回归测试：长剧本分块对齐、最小时长收尾、任务持久化与删除安全。

不依赖真实音视频，也不需要 numpy 之外的额外依赖。
运行：.venv\\Scripts\\python.exe tests\\test_p0_fixes.py
"""
from __future__ import annotations

import json
import random
import shutil
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import jobs  # noqa: E402
from app.config import OUTPUT_DIR, UPLOAD_DIR  # noqa: E402
from app.jobs import Job  # noqa: E402
from app.main import _is_inside  # noqa: E402
from app.pipeline.align import (  # noqa: E402
    NW_MAX_CELLS,
    _difflib_pairs,
    _nw_pairs_chunked,
    _nw_pairs_dense,
    _nw_pairs_numpy,
    _nw_pairs_with_mode,
)
from app.pipeline.script_parser import DialogueLine, parse_file  # noqa: E402
from app.pipeline.segment import make_cues, strip_fillers, strip_leading_fillers  # noqa: E402
from app.pipeline.text import clean_cue_text, is_spoken  # noqa: E402

SAMPLE = ROOT / "samples" / "姐弟战天下_第10集.txt"
HAN = "我们一起去看看明天的太阳到底有多远啊你说是不是这个道理呢吧"
HOMO = {
    "帆": "凡", "远": "圆", "到": "倒", "底": "低", "你": "妳", "是": "事",
    "道": "到", "陈": "成", "清": "青", "明": "名", "白": "百", "看": "砍",
    "话": "画", "起": "启", "过": "郭", "梦": "孟", "雨": "宇", "站": "战",
}


def fake_asr(lines, per_char=0.2, line_gap=0.35, noise_every=17, seed=23):
    """按剧本伪造 ASR：带同音错字、漏字和周期性噪声段。"""
    rng = random.Random(seed)
    segments = []
    clock = 0.8
    for index, line in enumerate(lines):
        chars = [c for c in line.text if is_spoken(c)]
        words = []
        for ch in chars:
            roll = rng.random()
            if roll < 0.08:
                ch = HOMO.get(ch, ch)      # 同音错字
            elif roll < 0.12:
                continue                   # 漏字
            words.append(
                {"word": ch, "start": clock, "end": clock + per_char}
            )
            clock += per_char
        if not words:
            continue
        segments.append(
            {
                "start": words[0]["start"],
                "end": words[-1]["end"],
                "text": "".join(w["word"] for w in words),
                "words": words,
            }
        )
        if noise_every and index % noise_every == 5:
            segments.append({"start": clock, "end": clock + 0.7, "text": "嗯啊呃"})
            clock += 0.9
        clock += line_gap
    return segments


def long_lines(rounds=40):
    """把样例对白重复多遍，构造超过 NW_MAX_CELLS 的长剧本。"""
    base = parse_file(SAMPLE)
    out = []
    index = 0
    for _ in range(rounds):
        for line in base:
            index += 1
            out.append(
                DialogueLine(
                    idx=index,
                    speaker=line.speaker,
                    note=line.note,
                    is_os=line.is_os,
                    text=line.text,
                )
            )
    return out


def check_align_paths() -> None:
    """三条对齐路径：密集 / 分块 / 降级，行为必须一致且不丢同音匹配。"""
    rng = random.Random(7)
    for _ in range(30):
        n = rng.randint(1, 60)
        m = rng.randint(1, 60)
        s = [rng.choice(HAN + "aZ9") for _ in range(n)]
        a = [rng.choice(HAN + "aZ9") for _ in range(m)]
        assert _nw_pairs_dense(s, a) == _nw_pairs_numpy(s, a), "numpy 与纯 Python 结果不一致"

    # 分块与密集在「同音错字 + 漏字 + 噪声」下必须给出同一组配对
    for trial in range(6):
        base = [rng.choice(HAN) for _ in range(rng.randint(300, 900))]
        noisy = []
        for ch in base:
            roll = rng.random()
            if roll < 0.08:
                noisy.append(rng.choice(HAN))     # 乱字
            elif roll < 0.14:
                continue                          # 漏字
            else:
                noisy.append(ch)
        if rng.random() < 0.5:
            noisy.append(rng.choice(HAN))
        dense = set(_nw_pairs_numpy(base, noisy))
        chunked = set(_nw_pairs_chunked(base, noisy))
        assert dense == chunked, f"第 {trial} 次分块与密集不一致"

    # 长剧本：同音替换必须全部匹配上，且走的是分块而不是 difflib
    s = list("林帆和陈世美一起走了") * 260
    a = list("林凡和陈士美一起走了") * 260
    pairs, mode = _nw_pairs_with_mode(s, a)
    assert mode == "chunked", f"长剧本应走分块，实际 {mode}"
    assert len(pairs) == len(s), f"同音错字被漏掉：{len(pairs)}/{len(s)}"

    # 没有 numpy 时退回 difflib 会丢同音匹配，必须能被识别出来
    assert _difflib_pairs(s, a) != _nw_pairs_numpy(s, a), "difflib 与 NW 本应有差异"
    print("OK  对齐路径：密集/分块一致，同音匹配不丢，模式可识别")


def check_no_stretching() -> None:
    """窗口比实际需要长时，对齐不能被拉长去"吃掉"末尾。"""
    rng = random.Random(11)
    base = [rng.choice(HAN) for _ in range(600)]
    noisy = list(base)
    for k in range(0, len(noisy), 40):          # 每 40 字漏一个
        noisy.pop(k)
    # 末尾追加 400 个无关字，模拟窗口过长
    padded = noisy + [rng.choice(HAN) for _ in range(400)]

    pairs = _nw_pairs_chunked(base, padded)
    matched = {si for si, _ in pairs}
    assert len(matched) >= len(base) * 0.95, f"匹配率过低：{len(matched)}/{len(base)}"

    # 每个剧本字应对到大致正确的位置（容差 20 字），不允许整体后移
    drift = [aj - si for si, aj in pairs if si < len(base) - 20]
    assert drift, "没有配对"
    assert max(abs(d) for d in drift) < 20, f"时间映射漂移过大：{max(abs(d) for d in drift)}"
    print("OK  半全局对齐：窗口过长时不被拉长，映射不漂移")


def check_long_script_pipeline() -> None:
    """长剧本全流水线：匹配率、最小时长、字数上限、文字完整性。"""
    lines = long_lines(rounds=40)
    total = sum(sum(1 for c in l.text if is_spoken(c)) for l in lines)
    assert total * total > NW_MAX_CELLS, "测试数据应大到必须分块"
    segments = fake_asr(lines)

    started = time.time()
    aligned = jobs.align(lines, segments)
    elapsed = time.time() - started
    assert aligned.mode == "chunked", f"应走分块，实际 {aligned.mode}"
    assert elapsed < 30, f"对齐太慢：{elapsed:.1f}s"

    matched = aligned.total_matched / max(1, aligned.total_chars)
    assert matched > 0.90, f"匹配率过低：{matched:.1%}"
    low = sum(1 for l in aligned.lines if l.ratio < 0.60)
    assert low <= len(aligned.lines) * 0.02, f"匹配度低的句子太多：{low}/{len(aligned.lines)}"

    cues = jobs.make_cues(aligned.lines, max_chars=14, min_duration=0.55)
    assert cues, "没有生成字幕"
    ordered = sorted(cues, key=lambda c: c.start)

    short = [c for c in ordered if c.end - c.start < 0.55 - 1e-6]
    assert not short, f"有 {len(short)} 条字幕不足最小时长：{short[:2]}"
    over = [c for c in ordered if sum(1 for ch in c.text if is_spoken(ch)) > 14]
    assert not over, f"有 {len(over)} 条字幕超过单行字数"
    overlap = [
        (a.text, b.text) for a, b in zip(ordered, ordered[1:]) if a.end > b.start + 1e-6
    ]
    assert not overlap, f"有 {len(overlap)} 处字幕重叠"

    by_line = defaultdict(list)
    for cue in ordered:
        by_line[cue.line_idx].append(cue)
    broken = [
        line.idx
        for line in lines
        if "".join(c.text for c in by_line.get(line.idx, []))
        != strip_fillers(clean_cue_text(strip_leading_fillers(line.text)))
    ]
    assert not broken, f"{len(broken)} 句分段后文字与原文不一致，例如第 {broken[:3]} 句"

    print(
        f"OK  长剧本流水线：{len(lines)} 句 / {total} 字，"
        f"匹配率 {matched:.1%}，{len(cues)} 条字幕，对齐 {elapsed:.2f}s"
    )


def check_min_duration_regression() -> None:
    """原来 max_chars=8 时会漏一条不足最小时长的字幕，现在不允许。"""
    lines = parse_file(SAMPLE)
    segments = fake_asr(lines, per_char=0.22, line_gap=0.25, noise_every=0)
    aligned = jobs.align(lines, segments)
    for max_chars in (6, 8, 10, 14):
        cues = sorted(
            jobs.make_cues(aligned.lines, max_chars=max_chars, min_duration=0.55),
            key=lambda c: c.start,
        )
        short = [c for c in cues if c.end - c.start < 0.55 - 1e-6]
        assert not short, f"max_chars={max_chars} 有 {len(short)} 条不足最小时长"
        over = [c for c in cues if sum(1 for ch in c.text if is_spoken(ch)) > max_chars]
        assert not over, f"max_chars={max_chars} 有 {len(over)} 条超字数"
    print("OK  最小时长收尾：各 max_chars 下无闪帧字幕，也无超长字幕")


def check_persistence() -> None:
    """结果落盘、重启恢复、内存轮换后按需读回、中断任务被标记。"""
    scratch = Path(tempfile.mkdtemp(prefix="ss-results-"))
    original = jobs.RESULTS_DIR
    jobs.RESULTS_DIR = scratch
    try:
        job_id = uuid.uuid4().hex[:12]
        job = Job(
            id=job_id,
            media_path=Path("media.mp4"),
            script_path=Path("script.txt"),
            output_dir=Path("out"),
            stem="第10集",
            options={"model": "small"},
            status="done",
            result={"summary": {"cues": 2}, "cues": [{"index": 1}, {"index": 2}]},
        )
        jobs.register(job)
        record = scratch / f"{job_id}.json"
        assert record.is_file(), "结果没有落盘"
        saved = json.loads(record.read_text(encoding="utf-8"))
        assert saved["status"] == "done" and saved["result"]["summary"]["cues"] == 2

        # 模拟内存轮换：结果被清空后仍能读回
        jobs._JOBS[job_id].result = None
        snapshot = jobs._JOBS[job_id].snapshot()
        assert snapshot["result"] and len(snapshot["result"]["cues"]) == 2, "轮换后读不回"

        # 重启恢复
        jobs._JOBS.clear()
        restored = jobs.restore_jobs()
        assert restored >= 1, "重启后没有恢复任何任务"
        assert jobs.get_job(job_id).status == "done"

        # 在途任务被重启打断 → 标记为中断
        stuck_id = "_stuck"
        (scratch / f"{stuck_id}.json").write_text(
            json.dumps(
                {
                    "id": stuck_id,
                    "status": "running",
                    "stage": "识别语音",
                    "stem": "x",
                    "media_path": ".",
                    "script_path": ".",
                    "output_dir": ".",
                    "options": {},
                }
            ),
            encoding="utf-8",
        )
        jobs._JOBS.clear()
        jobs.restore_jobs()
        stuck = jobs.get_job(stuck_id)
        assert stuck is not None and stuck.status == "error", "在途任务没有被标记为中断"
        assert "重启" in stuck.error

        # 删除任务时记录一并消失
        jobs.remove_job(job_id)
        assert not record.exists(), "删除任务后记录还在"
        assert jobs.get_job(job_id) is None
    finally:
        jobs.RESULTS_DIR = original
        shutil.rmtree(scratch, ignore_errors=True)
    print("OK  任务持久化：落盘/恢复/轮换读回/中断标记/删除清理")


def check_delete_safety() -> None:
    """删除任务绝不能碰用户自己的文件，也不能碰数据目录之外的地方。"""
    assert _is_inside(UPLOAD_DIR, UPLOAD_DIR) is True
    assert _is_inside(Path.home(), UPLOAD_DIR) is False
    assert _is_inside(Path(UPLOAD_DIR.anchor), UPLOAD_DIR) is False

    from fastapi.testclient import TestClient  # noqa: E402

    from app.main import app  # noqa: E402

    scratch = Path(tempfile.mkdtemp(prefix="ss-del-"))
    try:
        user_dir = scratch / "我的成片"
        user_dir.mkdir()
        user_file = user_dir / "第10集.mp4"
        user_file.write_bytes(b"fake video")
        export = scratch / "导出"
        export.mkdir()
        srt = export / "第10集.srt"
        srt.write_text("1\n", encoding="utf-8")

        job_dir = UPLOAD_DIR / ("del" + uuid.uuid4().hex[:8])
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "script.txt").write_text("主角：你好", encoding="utf-8")

        job = Job(
            id=job_dir.name,
            media_path=user_file,
            script_path=job_dir / "script.txt",
            output_dir=export,
            stem="第10集",
            options={},
            status="done",
            srt_path=srt,
            external_media=True,
        )
        jobs.register(job)

        client = TestClient(app)
        response = client.delete(f"/api/jobs/{job.id}")
        assert response.status_code == 200, response.text

        assert user_file.is_file(), "用户的成片被删了！"
        assert user_dir.is_dir(), "用户的文件夹被删了！"
        assert not job_dir.exists(), "任务目录没有清理"
        assert not (jobs.RESULTS_DIR / f"{job.id}.json").exists(), "任务记录没有清理"
        assert export.is_dir(), "用户的导出目录被删了！"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print("OK  删除安全：外部成片、用户文件夹、导出目录均未被动过")


def main() -> None:
    check_align_paths()
    check_no_stretching()
    check_min_duration_regression()
    check_long_script_pipeline()
    check_persistence()
    check_delete_safety()
    print("\n全部通过")


if __name__ == "__main__":
    main()
