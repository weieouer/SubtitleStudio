"""后台识别任务：单 worker 队列、进度、取消、结果落盘。"""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from pathlib import Path

from .asr import Cancelled, transcribe
from .hallucination import mark as mark_hallucination
from .interjections import mark_interjections, strip_interjection_cues
from .paths import RESULTS_DIR
from .segment import (split_on_gaps, split_script_cues, segment_cues, segment_alignment_cues,
                      repair_fragments)
from .storage import atomic_json
from .srt_io import format_srt

log = logging.getLogger("studio.jobs")
TASK_LABELS = {"asr": "字幕提取", "align": "对齐字幕", "asr_region": "重识别本段"}


class Job:
    def __init__(self, path: str, model: str, device: str, vad: bool, gap_seconds: float = 0.6,
                 keep_interjections: bool = False):
        self.id = uuid.uuid4().hex[:12]
        self.path = path
        self.model = model
        self.device = device
        self.vad = vad
        self.gap_seconds = gap_seconds
        self.keep_interjections = keep_interjections
        self.max_width = 20
        self.width_split = True
        self.status = "queued"       # queued/running/done/error/cancelled
        self.progress = 0.0
        self.message = "排队中"
        self.error = ""
        self.subtitle_path = ""
        self.task = "asr"
        self.input_cues = []
        self.script_document = None
        self.cues: list[dict] = []
        self.info: dict = {}
        self.created = time.time()
        self.started = 0.0
        self.finished = 0.0
        self.cancel_event = threading.Event()

    def to_dict(self, include_cues: bool = True) -> dict:
        data = {
            "id": self.id,
            "path": self.path,
            "model": self.model,
            "device": self.device,
            "gap_seconds": self.gap_seconds,
            "keep_interjections": self.keep_interjections,
            "max_width": self.max_width,
            "width_split": self.width_split,
            "status": self.status,
            "progress": round(self.progress, 3),
            "message": self.message,
            "error": self.error,
            "subtitle_path": self.subtitle_path,
            "task": self.task,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "elapsed_seconds": round(max(0,(self.finished or time.time())-self.started),3) if self.started else 0,
        }
        if include_cues:
            data["cues"] = self.cues
            data["info"] = self.info
        return data


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.queue: queue.Queue[Job] = queue.Queue()
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._worker, daemon=True, name="asr-worker").start()

    def submit(self, path: str, model: str, device: str, vad: bool, gap_seconds: float = 0.6,
               request_id: str | None = None, task: str = "asr", input_cues: list | None = None, max_width: float = 20,
               keep_interjections: bool = False, width_split: bool = True, script_document=None) -> Job:
        job = Job(path, model, device, vad, gap_seconds, keep_interjections)
        job.task = task
        job.input_cues = input_cues or []
        job.script_document = script_document
        job.max_width = max_width
        job.width_split = width_split
        with self.lock:
            if request_id and request_id in self.jobs:
                previous = self.jobs[request_id]
                if (previous.path, previous.model, previous.device, previous.vad, previous.gap_seconds,
                        previous.task, previous.input_cues, previous.max_width, previous.keep_interjections,
                        previous.width_split, previous.script_document) != (path, model, device, vad, gap_seconds,
                                                  task, input_cues or [], max_width, keep_interjections, width_split, script_document):
                    raise ValueError("同一任务标识不能提交不同素材或参数")
                return previous
            if request_id:
                job.id = request_id
            self.jobs[job.id] = job
        self.queue.put(job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job is None:
            return None
        job.cancel_event.set()
        if job.status == "queued":
            job.status = "cancelled"
            job.message = "已取消"
            job.finished = time.time()
        return job

    def _worker(self) -> None:
        while True:
            job = self.queue.get()
            job.status = "running"
            job.started = time.time()
            log.info("任务开始：%s %s 模型=%s 设备=%s 文件=%s", job.id,
                     TASK_LABELS.get(job.task, job.task), job.model, job.device, job.path)

            logged_percent = [-10]

            def on_progress(ratio: float, message: str) -> None:
                job.progress = ratio
                job.message = message
                # 每 10% 记一行，长任务在运行终端里能看出还在推进。
                percent = int(max(0.0, min(1.0, ratio)) * 100)
                if percent >= logged_percent[0] + 10:
                    logged_percent[0] = percent
                    log.info("任务进度：%s %s %d%% %s", job.id,
                             TASK_LABELS.get(job.task, job.task), percent, message)

            try:
                if job.cancel_event.is_set():
                    raise Cancelled()
                if job.task == "align":
                    from .asr import clear_model_cache
                    clear_model_cache()
                    from .alignment import align_cues
                    cues, info = align_cues(job.path, job.input_cues, job.device, on_progress, job.cancel_event)
                elif job.task == 'asr_region':
                    from .media import decode_audio, DecodeCancelled
                    region=job.input_cues[0]
                    on_progress(.01,'读取本段音频…')
                    try:
                        audio=decode_audio(job.path,16000,job.cancel_event,start=region['start'],end=region['end'])
                    except DecodeCancelled:
                        raise Cancelled() from None
                    cues,info=transcribe(job.path,job.model,job.device,job.vad,on_progress=on_progress,cancel_event=job.cancel_event,audio=audio)
                    for cue in cues:
                        cue['start']=round(max(region['start'],region['start']+cue['start']),3)
                        cue['end']=round(min(region['end'],region['start']+cue['end']),3)
                        for key in ('original_start','original_end'):
                            if key in cue:cue[key]=round(region['start']+cue[key],3)
                        for key in ('pause_before','pause_after'):
                            if key in cue:cue[key]=round(region['start']+cue[key],3)
                        if cue.get('asr_original'):
                            cue['asr_original']=dict(cue['asr_original'])
                            for key in ('start','end'):cue['asr_original'][key]=round(region['start']+cue['asr_original'][key],3)
                        if cue.get('audio_refinement'):
                            for key in ('before_start','before_end'):cue['audio_refinement'][key]=round(region['start']+cue['audio_refinement'][key],3)
                        for interval in cue.get('audio_silences',[]):
                            for key in ('start','end'):interval[key]=round(region['start']+interval[key],3)
                        for word in cue.get('words',[]):
                            word['start']=round(max(region['start'],region['start']+word['start']),3)
                            word['end']=round(min(region['end'],region['start']+word['end']),3)
                            if 'original_end' in word:word['original_end']=round(region['start']+word['original_end'],3)
                    cues=[c for c in cues if c['end']>c['start']]
                    info['region']=region
                else:
                    cues, info = transcribe(
                        job.path, job.model, job.device, job.vad,
                        on_progress=on_progress, cancel_event=job.cancel_event,
                    )
                if job.task in ('asr', 'asr_region'):
                    # 关闭“语气词”时，下方在提取结果里直接删除整条语气词；
                    # 开启时保留并写 interjection 标记，校准按可靠参考再确认。
                    # 幻觉特征只标记供人工复核，不自动排除或删除。
                    flagged = mark_hallucination(cues)
                    if flagged:
                        info['hallucination'] = {'count': len(flagged),
                                                 'cues': [c.get('id') for c in flagged]}
                        log.info('疑似幻觉字幕 %d 条：%s', len(flagged),
                                 '、'.join(str(c.get('id', '')) for c in flagged[:10]))
                if job.cancel_event.is_set():
                    job.status = "cancelled"
                    job.message = "已取消"
                else:
                    for cue in cues:
                        cue.setdefault("asr_original", {"text": cue["text"], "start": cue["start"],
                                                       "end": cue["end"], "id": cue.get("id")})
                    # 对齐负责时间：先按声音停顿拆独立字幕条（0 关闭），
                    # 再按 width_split 把过长条目按“每行字数”分段衔接后面；不合并碎片、不改字词。
                    # 停顿切点会参考剧本：同一句内按停顿阈值决定，参考明确断句处强制拆开。
                    if job.task == 'align':
                        cues, info['script_splits'] = split_script_cues(cues, job.script_document)
                        cues, gap_splits = split_on_gaps(cues, job.gap_seconds, job.max_width)
                        # 0 表示关闭停顿拆条；不写入 0 值，避免界面上出现“已按停顿处理”。
                        if job.gap_seconds > 0:
                            info['gap_splits'] = gap_splits
                        if job.width_split:
                            cues, width_splits = segment_alignment_cues(cues, job.max_width)
                            info['width_splits'] = width_splits
                    else:
                        if job.gap_seconds > 0 and job.task in ('asr','asr_region'):
                            cues, gap_splits = split_on_gaps(cues, job.gap_seconds,job.max_width)
                            info["gap_splits"] = gap_splits
                        cues, width_splits, _ = segment_cues(cues, job.max_width)
                        info['width_splits'] = width_splits
                        cues,info['fragment_merges']=repair_fragments(cues,job.max_width,job.gap_seconds)
                    # 关闭“语气词”时提取直接删除整条语气词，并去掉句首/句末语气词；
                    # 对齐只改时间，不改字词，交给校准统一处理。
                    if job.task in ('asr', 'asr_region') and not job.keep_interjections:
                        cues, interjections_removed, interjections_trimmed = strip_interjection_cues(cues)
                        if interjections_removed:
                            info['interjections_removed'] = interjections_removed
                        if interjections_trimmed:
                            info['interjections_trimmed'] = interjections_trimmed
                    for cue in cues:
                        cue.setdefault("original_text", cue["text"])
                        cue.setdefault("original_start", cue["start"])
                        cue.setdefault("original_end", cue["end"])
                    # 分段后重新标记，保证拆条结果也能显示红色“语气词”。
                    flagged = mark_interjections(cues)
                    if flagged:
                        info['interjections'] = {'count': len(flagged),
                                                 'cues': [item.get('id') for item in flagged],
                                                 'texts': [item.get('text', '') for item in flagged[:10]]}
                        log.info('检测到语气词字幕 %d 条：%s', len(flagged),
                                 '、'.join(str(item.get('text', ''))[:12] for item in flagged[:10]))
                    job.cues = cues
                    job.info = info
                    if job.cancel_event.is_set():
                        raise Cancelled()
                    job.progress = 1.0
                    job.message = "完成"
                    job.finished = time.time()
                    self._save(job)
                    job.status = "done"
                    log.info("任务完成：%s 用时 %.1f 秒，%d 条字幕", job.id,
                             job.finished - job.started, len(job.cues))
            except Cancelled:
                job.status = "cancelled"
                job.message = "已取消"
                log.info("任务取消：%s", job.id)
            except Exception as exc:
                job.status = "error"
                job.error = str(exc)
                job.message = str(exc)
                log.exception("任务失败：%s %s", job.id, str(exc))
            finally:
                job.finished = time.time()
                self.queue.task_done()

    def _save(self, job: Job) -> None:
        # A task-specific SRT keeps the extraction available for alignment,
        # independent of final export formatting and noise filters.
        subtitle = RESULTS_DIR / job.id / (Path(job.path).stem + ".srt")
        subtitle.parent.mkdir(parents=True, exist_ok=True)
        subtitle.write_text(format_srt(job.cues), encoding="utf-8", newline="")
        job.subtitle_path = str(subtitle)
        path = RESULTS_DIR / f"{job.id}.json"
        data = job.to_dict()
        data["status"] = "done"
        atomic_json(path, data)
