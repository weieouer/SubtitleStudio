# -*- coding: utf-8 -*-
"""任务队列：本地单进程，后台线程跑流水线，前端轮询进度。

结果同时落盘到 data/results/<id>.json：刷新页面、重启服务之后
「完成」的任务还能找回，内存里只保留最近若干个完整报告。
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import MODELS_DIR, RESULTS_DIR
from .pipeline.align import AlignmentResult, align
from .pipeline.asr import has_local_model, transcribe
from .pipeline.extract import extraction_lines
from .pipeline.script_parser import DialogueLine, parse_file
from .pipeline.segment import Cue, make_cues
from .pipeline.srt_out import write_srt

# 内存里最多保留多少条完整报告，超出的已落盘、按需再读。
MAX_JOBS_IN_MEMORY = 24


@dataclass
class Job:
    id: str
    media_path: Path
    script_path: Path
    output_dir: Path
    stem: str
    options: dict[str, Any]
    status: str = "queued"
    stage: str = "排队中"
    message: str = ""
    progress: float = 0.0
    error: str = ""
    result: dict | None = None
    srt_path: Path | None = None
    created_at: float = field(default_factory=time.time)
    # 直读本机文件时为 True：删除任务时不能动用户自己的文件夹。
    external_media: bool = False
    # 删除后后台线程可能还在收尾，不能再把记录写回来。
    deleted: bool = False

    @property
    def record_path(self) -> Path:
        return RESULTS_DIR / f"{self.id}.json"

    def snapshot(self) -> dict:
        result = self.result
        if result is None and self.status == "done":
            # 内存里被轮换掉了，从磁盘读回来（读完缓存回内存）。
            result = _read_record(self.id).get("result")
            if result is not None:
                self.result = result
        return {
            "id": self.id,
            "status": self.status,
            "stage": self.stage,
            "message": self.message,
            "progress": round(self.progress, 4),
            "error": self.error,
            "stem": self.stem,
            "mode": self.options.get("mode", "align"),
            "external_media": self.external_media,
            "result": result if self.status == "done" else None,
            "output_path": (
                str(self.srt_path)
                if self.status == "done" and self.srt_path is not None
                else None
            ),
        }


_JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.RLock()


def _read_record(job_id: str) -> dict:
    try:
        data = json.loads(
            (RESULTS_DIR / f"{job_id}.json").read_text(encoding="utf-8")
        )
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_record(job: Job) -> None:
    """原子写入，避免写到一半断电留下读不了的 JSON。

    删除任务时 delete 会先置 deleted，后台线程收尾时就不再写回记录。
    """
    with _JOBS_LOCK:
        if job.deleted:
            return
        payload = {
            "id": job.id,
            "status": job.status,
            "stage": job.stage,
            "message": job.message,
            "error": job.error,
            "stem": job.stem,
            "media_path": str(job.media_path),
            "script_path": str(job.script_path),
            "output_dir": str(job.output_dir),
            "srt_path": str(job.srt_path) if job.srt_path else None,
            "external_media": job.external_media,
            "options": job.options,
            "created_at": job.created_at,
            "finished_at": time.time(),
            "result": job.result,
        }
        try:
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            temp = job.record_path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, job.record_path)
        except OSError:
            # 落盘失败不影响本次处理，只是重启后找不回来。
            pass


def _trim_memory() -> None:
    """报告已落盘，内存里只留最近的一批；旧任务用到时再从磁盘读回。"""
    with _JOBS_LOCK:
        excess = len(_JOBS) - MAX_JOBS_IN_MEMORY
        if excess <= 0:
            return
        finished = sorted(
            (job for job in _JOBS.values() if job.status in ("done", "error")),
            key=lambda job: job.created_at,
        )
        for job in finished[:excess]:
            _JOBS.pop(job.id, None)


def _job_from_record(data: dict) -> Job | None:
    """把落盘的记录还原成 Job；被重启打断的排队/处理中任务标记为中断。"""
    job_id = str(data.get("id") or "")
    if not job_id:
        return None

    status = str(data.get("status") or "done")
    error = str(data.get("error") or "")
    stage = str(data.get("stage") or "")
    if status in ("queued", "running"):
        # 服务重启后线程不会回来，不能让它一直显示「排队中」或「处理中」。
        status, stage = "error", "已中断"
        error = error or "服务已重启，这个任务被中断了。请重新开始处理。"

    srt_raw = data.get("srt_path")
    try:
        created_at = float(data.get("created_at") or time.time())
    except (TypeError, ValueError):
        created_at = time.time()

    return Job(
        id=job_id,
        media_path=Path(str(data.get("media_path") or ".")),
        script_path=Path(str(data.get("script_path") or ".")),
        output_dir=Path(str(data.get("output_dir") or ".")),
        stem=str(data.get("stem") or "subtitle"),
        options=data.get("options") or {},
        status=status,
        stage=stage,
        error=error,
        result=data.get("result"),
        srt_path=Path(srt_raw) if srt_raw else None,
        created_at=created_at,
        external_media=bool(data.get("external_media")),
    )


def restore_jobs() -> int:
    """启动时只读最近的一批记录，旧任务等打开时再按需读盘。"""
    if not RESULTS_DIR.is_dir():
        return 0

    paths = sorted(
        RESULTS_DIR.glob("*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:MAX_JOBS_IN_MEMORY]

    restored = 0
    # 从旧到新放回内存，_trim_memory 淘汰时顺序才稳定。
    for path in reversed(paths):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue

        job = _job_from_record(data)
        if job is None:
            continue
        with _JOBS_LOCK:
            _JOBS[job.id] = job
        restored += 1
        if str(data.get("status")) in ("queued", "running"):
            _write_record(job)

    return restored


def register(job: Job) -> None:
    with _JOBS_LOCK:
        _JOBS[job.id] = job
    _write_record(job)
    _trim_memory()


def save_record(job: Job) -> None:
    """导出位置变化后，把新的输出路径重新落盘。"""
    _write_record(job)


def get_job(job_id: str) -> Job | None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is not None:
        return job

    # 被内存淘汰的旧任务：记录还在磁盘上，按需读回来。
    job = _job_from_record(_read_record(job_id))
    if job is None:
        return None
    with _JOBS_LOCK:
        return _JOBS.setdefault(job_id, job)


def remove_job(job_id: str) -> Job | None:
    job = get_job(job_id)
    if job is None:
        return None
    with _JOBS_LOCK:
        job.deleted = True
        _JOBS.pop(job_id, None)
    job.record_path.unlink(missing_ok=True)
    return job


def _set(job: Job, **kwargs: Any) -> None:
    with _JOBS_LOCK:
        for key, value in kwargs.items():
            setattr(job, key, value)


def start(job: Job) -> None:
    register(job)
    threading.Thread(target=_run, args=(job.id,), daemon=True).start()


def _run(job_id: str) -> None:
    job = get_job(job_id)
    if job is None or job.deleted:
        return

    try:
        opts = job.options
        mode = opts.get("mode", "align")

        _set(
            job,
            status="running",
            stage="解析剧本" if mode == "align" else "准备识别",
            progress=0.02,
            message=(
                "正在提取剧本里的对白行…"
                if mode == "align"
                else "正在准备识别人声…"
            ),
        )
        # 排队状态也必须落盘：进程如果在这里被关掉，重启后才知道它被中断了。
        _write_record(job)

        lines: list[DialogueLine] = []
        if mode == "align":
            lines = parse_file(job.script_path)
            if not lines:
                raise ValueError(
                    "剧本里没有提取到对白行，请检查格式是否为「角色：台词」。"
                )

        if not has_local_model(opts["model"], MODELS_DIR):
            raise ValueError(
                f"{opts['model']} 模型还没有下载，请先在模型状态处点击下载。"
            )
        _set(
            job,
            stage="识别语音",
            progress=0.06,
            message="正在从本机加载语音模型，不会联网…",
        )

        def on_progress(value: float) -> None:
            _set(
                job,
                progress=0.08 + 0.70 * value,
                message=f"正在识别语音… {int(value * 100)}%",
            )

        used_cpu_fallback = False

        def on_fallback() -> None:
            nonlocal used_cpu_fallback
            used_cpu_fallback = True
            _set(job, message="GPU 运行库不可用，已自动改用 CPU 继续识别…")

        segments, info = transcribe(
            job.media_path,
            model_size=opts["model"],
            device=opts["device"],
            download_root=MODELS_DIR,
            progress=on_progress,
            on_fallback=on_fallback,
        )

        if mode == "extract":
            _set(
                job,
                stage="整理字幕",
                progress=0.82,
                message="正在整理人声段落，每段保持完整输出…",
            )
            extracted = extraction_lines(segments)
            if not extracted:
                raise ValueError(
                    "没有识别到可用的人声，请确认成片里有对白，或换一个更准确的模型。"
                )
            # 提取严格使用识别段落的时间，不做整体提前和句尾延音。
            cues = make_cues(
                extracted,
                include_os=True,
                min_duration=opts["min_duration"],
                lead_in=0.0,
                tail_delay=0.0,
                whole_lines=True,
            )
            if not cues:
                raise ValueError("没有生成任何字幕，请检查成片里是否有人声。")
            report = _build_extract_report(extracted, cues, info)
        else:
            _set(
                job,
                stage="对齐剧本",
                progress=0.82,
                message="正在把剧本逐字贴到语音时间轴上…",
            )
            aligned = align(lines, segments)

            _set(
                job,
                stage="智能分段",
                progress=0.90,
                message="正在按词边界和自然停顿分段…",
            )
            cues = make_cues(
                aligned.lines,
                max_chars=opts["max_chars"],
                include_os=opts["include_os"],
                no_gap=opts["no_gap"],
                max_fill=opts["max_fill"],
                min_duration=opts["min_duration"],
                lead_in=float(opts.get("lead_in", 0.0)),
                tail_delay=float(opts.get("tail_delay", 0.18)),
            )
            if not cues:
                raise ValueError("没有生成任何字幕，可能剧本与音频完全不匹配。")
            report = _build_report(
                lines, aligned, cues, include_os=opts["include_os"]
            )

        if job.deleted:
            return

        _set(
            job,
            stage="生成字幕",
            progress=0.96,
            message="正在写入 SRT 文件…",
        )
        srt_path = write_srt(cues, job.output_dir / f"{job.stem}.srt")

        _set(
            job,
            status="done",
            stage="完成",
            progress=1.0,
            message=(
                f"完成：{len(cues)} 条字幕"
                + ("（字幕提取）" if mode == "extract" else "")
                + ("（GPU 不可用，已改用 CPU）" if used_cpu_fallback else "")
            ),
            result=report,
            srt_path=srt_path,
        )
        _write_record(job)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _set(
            job,
            status="error",
            stage="处理失败",
            error=str(exc),
            message="处理失败，请查看错误信息。",
        )
        _write_record(job)


def _cue_items(cues: list[Cue]) -> list[dict]:
    return [
        {
            "index": index,
            "start": round(cue.start, 2),
            "end": round(cue.end, 2),
            "text": cue.text,
            "speaker": cue.speaker,
            "line_idx": cue.line_idx,
        }
        for index, cue in enumerate(cues, start=1)
    ]


def _build_extract_report(
    extracted: list,
    cues: list[Cue],
    info: object,
) -> dict:
    """字幕提取没有剧本可审，只保留人声段和字幕清单。"""
    cue_counts = Counter(c.line_idx for c in cues)
    items = [
        {
            "idx": item.line.idx,
            "speaker": "",
            "text": "".join(t.ch for t in item.timed),
            "is_os": False,
            "start": round(item.start, 2) if item.start is not None else None,
            "end": round(item.end, 2) if item.end is not None else None,
            "ratio": 1.0,
            "matched": item.matched_chars,
            "total": item.total_chars,
            "status": "ok",
            "cue_count": cue_counts.get(item.line.idx, 0),
        }
        for item in extracted
    ]

    duration = float(getattr(info, "duration", 0) or 0)
    if duration <= 0:
        duration = max(
            (float(item.end) for item in extracted if item.end is not None),
            default=0.0,
        )

    summary = {
        "mode": "extract",
        "lines": len(items),
        "aligned": len(items),
        "low": 0,
        "unaligned": 0,
        "noise": 0,
        "noise_seconds": 0,
        "cues": len(cues),
        "matched_ratio": 1.0,
        "align_mode": "extract",
        "duration": round(duration, 1),
    }

    return {"summary": summary, "lines": items, "noise": [], "cues": _cue_items(cues)}


def _build_report(
    lines: list[DialogueLine],
    aligned: AlignmentResult,
    cues: list[Cue],
    include_os: bool,
) -> dict:
    cue_counts = Counter(c.line_idx for c in cues)
    items: list[dict] = []

    for item in aligned.lines:
        if item.start is None:
            status = "unaligned"
        elif item.line.is_os and not include_os:
            status = "os"
        elif item.ratio < 0.60:
            status = "low"
        else:
            status = "ok"

        items.append(
            {
                "idx": item.line.idx,
                "speaker": item.line.speaker,
                "text": item.line.text,
                "is_os": item.line.is_os,
                "start": round(item.start, 2) if item.start is not None else None,
                "end": round(item.end, 2) if item.end is not None else None,
                "ratio": round(item.ratio, 3),
                "matched": item.matched_chars,
                "total": item.total_chars,
                "status": status,
                "cue_count": cue_counts.get(item.line.idx, 0),
            }
        )

    noise = [
        {
            "start": round(n.start, 2),
            "end": round(n.end, 2),
            "duration": round(n.end - n.start, 2),
            "text": n.text,
        }
        for n in sorted(aligned.noise, key=lambda n: n.start)
    ]

    summary = {
        "mode": "align",
        "lines": len(lines),
        "aligned": sum(1 for x in items if x["start"] is not None),
        "low": sum(1 for x in items if x["status"] == "low"),
        "unaligned": sum(1 for x in items if x["status"] == "unaligned"),
        "noise": len(noise),
        "noise_seconds": round(sum(n["duration"] for n in noise), 1),
        "cues": len(cues),
        "matched_ratio": round(
            aligned.total_matched / aligned.total_chars, 3
        )
        if aligned.total_chars
        else 0.0,
        # difflib = 没有 numpy，同音匹配降级；dense/chunked 都算没降级。
        "align_mode": aligned.mode,
    }

    return {
        "summary": summary,
        "lines": items,
        "noise": noise,
        "cues": _cue_items(cues),
    }
