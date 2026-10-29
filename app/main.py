# -*- coding: utf-8 -*-
"""FastAPI 入口：本地网页界面 + 任务接口。"""
from __future__ import annotations

import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import uuid
from collections import OrderedDict
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import DATA_DIR, MODELS_DIR, OUTPUT_DIR, UPLOAD_DIR
from .jobs import Job, get_job, remove_job, restore_jobs, save_record, start
from .model_manager import MODEL_CATALOG, ModelBusyError, manager as model_manager
from .pipeline.asr import has_local_model
from .pipeline.script_parser import parse_bytes, parse_file, spoken_chars

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

MEDIA_EXT = {
    ".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".m4v",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus",
}
SCRIPT_EXT = {".txt", ".md"}

# 本机文件直读：只有用户在本会话里明确选过/填过的路径才发令牌，
# 浏览器拿到令牌才能流式读取，避免把界面变成任意文件读取器。
MEDIA_TOKENS: OrderedDict[str, str] = OrderedDict()
MEDIA_TOKEN_LIMIT = 32
# 上传体积上限：半小时成片通常 1-4 GB，给到 20 GB 足够，超出多半是选错文件。
UPLOAD_MAX_BYTES = 20 * 1024 * 1024 * 1024

app = FastAPI(title="SubtitleStudio", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def _restore_persisted_jobs() -> None:
    """把上次跑完的任务读回内存，刷新/重启后结果不丢。"""
    try:
        restore_jobs()
    except Exception:  # noqa: BLE001 - 启动不能因为历史记录读不了而失败
        traceback.print_exc()


def _human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"


def _run_powershell_dialog(
    script: str, timeout_message: str, error_message: str
) -> str:
    """跑一段 WinForms 选择窗口脚本，返回用户选中的路径。"""
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-STA",
                "-WindowStyle",
                "Hidden",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, timeout_message) from exc
    except OSError as exc:
        raise HTTPException(500, error_message) from exc
    return (result.stdout or "").strip()


def _pick_file_dialog(title: str, filter: str) -> str | None:
    """弹出原生文件选择窗口，返回绝对路径（Windows）。"""
    if sys.platform != "win32":
        raise HTTPException(501, "当前系统请直接输入文件路径")

    script = f"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = '{title}'
$dialog.Filter = '{filter}'
$dialog.InitialDirectory = [Environment]::GetFolderPath('UserProfile')
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {{
    [Console]::Out.Write($dialog.FileName)
}}
"""
    selected = _run_powershell_dialog(
        script, "选择文件超时", "无法打开文件选择窗口"
    )
    return selected or None


def _resolve_local_media(raw: str) -> Path:
    """校验用户填写的本机文件路径，并顺手发一个流式读取令牌。"""
    value = (raw or "").strip().strip('"').strip("'")
    if not value:
        raise HTTPException(400, "请先填写本机文件路径")

    path = Path(os.path.expandvars(os.path.expanduser(value)))
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if path.suffix.lower() not in MEDIA_EXT:
        raise HTTPException(400, f"不支持的文件格式：{path.suffix or '未知'}")
    if not path.is_file():
        raise HTTPException(400, f"找不到这个文件：{path}")

    try:
        with path.open("rb"):
            pass
    except OSError as exc:
        raise HTTPException(400, f"这个文件读不了：{exc}") from exc
    return path


def _media_token(path: Path) -> str:
    token = uuid.uuid4().hex[:20]
    MEDIA_TOKENS[token] = str(path)
    if len(MEDIA_TOKENS) > MEDIA_TOKEN_LIMIT:
        MEDIA_TOKENS.popitem(last=False)
    return token


def _resolve_export_dir(raw: str) -> Path | None:
    value = raw.strip().strip('"').strip("'")
    if not value:
        return None

    value = os.path.expandvars(os.path.expanduser(value))
    path = Path(value)
    if not path.is_absolute():
        path = BASE_DIR / path
    path = path.resolve()

    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(400, f"无法使用这个导出位置：{exc}") from exc
    if not path.is_dir():
        raise HTTPException(400, "导出位置不是文件夹")

    # Windows 上 os.access 对 ACL 判断并不可靠，直接写一个临时文件验证。
    try:
        with tempfile.NamedTemporaryFile(
            dir=path, prefix=".subtitle-studio-", suffix=".tmp"
        ):
            pass
    except OSError as exc:
        raise HTTPException(400, f"这个导出位置不可写：{exc}") from exc
    return path


def _unique_stem(directory: Path, stem: str) -> str:
    candidate = stem
    index = 2
    while (directory / f"{candidate}.srt").exists():
        candidate = f"{stem} ({index})"
        index += 1
    return candidate


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/paths")
def app_paths() -> dict:
    return {
        "data": str(DATA_DIR),
        "outputs": str(OUTPUT_DIR),
        "uploads": str(UPLOAD_DIR),
        "models": str(MODELS_DIR),
    }


@app.get("/api/models")
def model_status() -> dict:
    return model_manager.snapshot()


@app.post("/api/models/{model}/download")
def download_model(model: str) -> dict:
    if model not in MODEL_CATALOG:
        raise HTTPException(404, "没有这个模型")
    try:
        entry = model_manager.start_download(model)
    except ModelBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "model": entry}


@app.post("/api/script/inspect")
async def inspect_script(script: UploadFile = File(...)) -> dict:
    script_name = script.filename or "script.txt"
    if Path(script_name).suffix.lower() not in SCRIPT_EXT:
        raise HTTPException(400, "剧本请上传 .txt 或 .md 文件")

    try:
        data = await script.read(8 * 1024 * 1024 + 1)
    finally:
        await script.close()

    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(400, "剧本文件超过 8 MB，请检查文件")
    lines = parse_bytes(data)
    if not lines:
        raise HTTPException(400, "没有提取到对白行，请检查「角色：台词」格式。")

    return {
        "lines": len(lines),
        "os": sum(1 for line in lines if line.is_os),
        "chars": sum(spoken_chars(line.text) for line in lines),
        "speakers": len({line.speaker for line in lines}),
        "size": len(data),
    }


@app.post("/api/export-dir/pick")
def pick_export_dir() -> dict:
    if sys.platform != "win32":
        raise HTTPException(501, "当前系统请直接输入导出路径")

    script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择字幕导出位置'
$dialog.ShowNewFolderButton = $true
$dialog.SelectedPath = [Environment]::GetFolderPath('UserProfile')
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.Write($dialog.SelectedPath)
}
"""
    selected = _run_powershell_dialog(
        script, "选择文件夹超时", "无法打开文件夹选择窗口"
    )
    return {"path": selected or None}


@app.post("/api/media/pick")
def pick_media_file() -> dict:
    """选择本机成片文件，返回路径（不复制、不上传）。"""
    path = _pick_file_dialog(
        "选择成片视频或音频",
        "视频与音频|*.mp4;*.mov;*.mkv;*.avi;*.flv;*.webm;*.m4v;*.mp3;*.wav;*.m4a;*.aac;*.flac;*.ogg;*.opus|所有文件|*.*",
    )
    return {"path": path}


@app.post("/api/media/register")
def register_media(payload: dict) -> dict:
    """登记一个本机文件路径，返回流式读取令牌和文件信息。"""
    path = _resolve_local_media(str(payload.get("path") or ""))
    stat = path.stat()
    return {
        "token": _media_token(path),
        "name": path.name,
        "path": str(path),
        "size": stat.st_size,
        "size_label": _human_size(stat.st_size),
    }


@app.get("/api/media/stream")
def stream_media(token: str, request: Request) -> StreamingResponse:
    """按 Range 流式吐文件，供 <video> 预览和拖动进度条使用。"""
    raw = MEDIA_TOKENS.get(token)
    if raw is None:
        raise HTTPException(403, "这个文件没有登记，请重新选择")
    path = Path(raw)
    if not path.is_file():
        raise HTTPException(404, "文件不存在或已被移动")

    size = path.stat().st_size
    start, end = 0, size - 1
    status = 200
    if size:
        match = re.match(r"bytes=(\d*)-(\d*)", request.headers.get("range") or "")
        if match:
            first, last = match.group(1), match.group(2)
            if not first and last:  # bytes=-N 取末尾 N 字节
                start = max(0, size - int(last))
            else:
                start = int(first) if first else 0
                end = int(last) if last else size - 1
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
            status = 206

    length = end - start + 1
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": "no-store",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    def chunks():
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                block = handle.read(min(1024 * 1024, remaining))
                if not block:
                    break
                remaining -= len(block)
                yield block

    return StreamingResponse(
        chunks(), status_code=status, media_type=media_type, headers=headers
    )


def external_media_exists(path: Path) -> bool:
    """外部的本机文件必须是真实可读的视频/音频，不能是空文件。"""
    return path.is_file() and path.stat().st_size > 0


@app.post("/api/jobs")
async def create_job(
    request: Request,
    media: UploadFile | None = File(None),
    script: UploadFile | None = File(None),
    mode: str = Form("align"),
    media_path: str = Form(""),
    max_chars: int = Form(14),
    model: str = Form("small"),
    device: str = Form("cpu"),
    include_os: bool = Form(True),
    no_gap: bool = Form(True),
    max_fill: float = Form(0.6),
    min_duration: float = Form(0.55),
    lead_in: float = Form(0.0),
    tail_delay: float = Form(0.18),
    export_dir: str = Form(""),
) -> dict:
    mode = (mode or "align").strip().lower()
    if mode not in {"align", "extract"}:
        raise HTTPException(400, "处理方式不正确")

    if model not in MODEL_CATALOG:
        model = "small"
    if not has_local_model(model, MODELS_DIR):
        label = MODEL_CATALOG[model]["label"]
        raise HTTPException(
            409,
            f"{label} 模型还没有下载，请先在模型状态处点击下载，再开始处理。",
        )
    if device not in {"cpu", "cuda"}:
        device = "cpu"
    max_chars = max(6, min(30, int(max_chars)))
    max_fill = max(0.0, min(2.0, float(max_fill)))
    min_duration = max(0.2, min(2.0, float(min_duration)))
    lead_in = max(0.0, min(0.6, float(lead_in)))
    tail_delay = max(0.0, min(0.6, float(tail_delay)))
    custom_export_dir = _resolve_export_dir(export_dir)

    # 成片两种来路：填本机路径（零复制）或上传。
    external_media: Path | None = None
    if media_path.strip():
        external_media = _resolve_local_media(media_path)
        media_name = external_media.name
        media_suffix = external_media.suffix.lower()
    elif media is not None and media.filename:
        media_name = media.filename
        media_suffix = Path(media_name).suffix.lower()
        if media_suffix not in MEDIA_EXT:
            raise HTTPException(400, f"不支持的音视频格式：{media_suffix or '未知'}")
        # 上传前先看磁盘够不够，避免拷到一半失败留下半个文件。
        declared = int(request.headers.get("content-length") or 0)
        if declared > UPLOAD_MAX_BYTES:
            raise HTTPException(
                400,
                f"上传的文件太大（约 {_human_size(declared)}），上限 "
                f"{_human_size(UPLOAD_MAX_BYTES)}。大文件请改用「本机文件」零上传。",
            )
        if declared:
            free = shutil.disk_usage(UPLOAD_DIR).free
            if free < declared * 1.05:
                raise HTTPException(
                    400,
                    "磁盘剩余空间不足：上传约需 "
                    f"{_human_size(declared)}，当前可用 {_human_size(free)}。"
                    "请清理磁盘，或改用「本机文件」零上传。",
                )
    else:
        raise HTTPException(400, "请选择成片文件，或填写本机文件路径")

    script_name = script.filename if script is not None else ""
    script_suffix = Path(script_name).suffix.lower() if script_name else ""
    if mode == "align":
        if not script_name:
            raise HTTPException(400, "字幕对齐需要上传剧本，或改用字幕提取。")
        if script_suffix not in SCRIPT_EXT:
            raise HTTPException(400, "剧本请上传 .txt 或 .md 文件")

    job_id = uuid.uuid4().hex[:12]
    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    media_path_final: Path
    if external_media is not None:
        media_path_final = external_media
    else:
        media_path_final = job_dir / f"media{media_suffix}"
    # 字幕提取没有剧本；路径只作为任务目录的定位点，不会创建文件。
    script_path = (
        job_dir / f"script{script_suffix}" if mode == "align" else job_dir / "script.txt"
    )

    try:
        if external_media is None:
            assert media is not None
            with media_path_final.open("wb") as out:
                while True:
                    chunk = await media.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
        if mode == "align":
            assert script is not None
            with script_path.open("wb") as out:
                while True:
                    chunk = await script.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        if media is not None:
            await media.close()
        if script is not None:
            await script.close()

    if not external_media_exists(media_path_final):
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, "成片文件读取失败，请重新选择。")

    lines = []
    if mode == "align":
        lines = parse_file(script_path)
        if not lines:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(
                400, "剧本里没有提取到对白行，请检查格式是否为「角色：台词」。"
            )

    stem = re.sub(r'[\\/:*?"<>|\r\n]+', "_", Path(media_name).stem).strip()[:60]
    stem = stem or "subtitle"

    # 默认直接写进 data/outputs，导出位置固定；同名文件自动编号。
    output_dir = custom_export_dir or OUTPUT_DIR
    stem = _unique_stem(output_dir, stem)

    job = Job(
        id=job_id,
        media_path=media_path_final,
        script_path=script_path,
        output_dir=output_dir,
        stem=stem,
        external_media=external_media is not None,
        options={
            "mode": mode,
            "model": model,
            "device": device,
            "max_chars": max_chars,
            "include_os": include_os,
            "no_gap": no_gap,
            "max_fill": max_fill,
            "min_duration": min_duration,
            "lead_in": lead_in,
            "tail_delay": tail_delay,
        },
    )
    start(job)
    return {"id": job.id, "mode": mode, "lines": len(lines)}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return job.snapshot()


@app.get("/api/jobs/{job_id}/srt")
def job_srt(job_id: str) -> FileResponse:
    job = get_job(job_id)
    if job is None or job.srt_path is None or not job.srt_path.exists():
        raise HTTPException(404, "字幕还没有生成")
    return FileResponse(
        job.srt_path,
        media_type="application/x-subrip",
        filename=f"{job.stem}.srt",
    )


@app.post("/api/jobs/{job_id}/export")
def export_job(job_id: str, export_dir: str = Form("")) -> dict:
    """把已生成的 SRT 移到当前导出位置，完成卡片随之更新。

    处理完成时已经自动写到当时的导出位置；如果之后又改了导出位置，
    点「导出 SRT」以这里的最新设置为准，避免输入框和卡片路径对不上。
    """
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job.status != "done":
        raise HTTPException(409, "任务还没有完成")
    if job.srt_path is None or not job.srt_path.is_file():
        raise HTTPException(404, "字幕文件已经不在原位置，请重新处理这一集")

    target_dir = _resolve_export_dir(export_dir) or OUTPUT_DIR
    source = job.srt_path
    try:
        if source.parent.resolve() == target_dir.resolve():
            return {"output_path": str(source), "moved": False}
    except OSError:
        pass

    # 去掉默认目录里的「(2)」编号，换目录后优先用成片原名。
    base = re.sub(r"\s*\(\d+\)$", "", job.stem) or job.stem
    stem = _unique_stem(target_dir, base)
    target = target_dir / f"{stem}.srt"
    try:
        shutil.move(str(source), str(target))
    except OSError as exc:
        raise HTTPException(400, f"导出失败：{exc}") from exc

    job.output_dir = target_dir
    job.stem = stem
    job.srt_path = target
    save_record(job)
    return {"output_path": str(target), "moved": True}


@app.get("/api/jobs/{job_id}/media")
def job_media(job_id: str) -> FileResponse:
    job = get_job(job_id)
    if job is None or not job.media_path.exists():
        raise HTTPException(404, "素材不存在")
    return FileResponse(job.media_path)


@app.post("/api/jobs/{job_id}/reveal")
def reveal_job(job_id: str) -> dict:
    job = get_job(job_id)
    if job is None or job.srt_path is None or not job.srt_path.exists():
        raise HTTPException(404, "字幕还没有生成")

    target = str(job.srt_path)
    if sys.platform == "win32":
        # Explorer 不按常规方式拆参数：引号必须只包住路径，
        # 否则文件名带空格（如「wb (2).srt」）时 /select 会失效。
        subprocess.Popen(f'explorer /select,"{target}"')
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", target])
    else:
        subprocess.Popen(["xdg-open", str(job.srt_path.parent)])
    return {"ok": True}


def _is_inside(path: Path, root: Path) -> bool:
    """路径是否位于 root 之内。清理任务时只用它判断，绝不碰用户目录。"""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, keep_output: bool = False) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job.status in ("queued", "running"):
        # 后台线程还持有这个 Job；直接 pop 会让它在收尾时把记录写回来。
        raise HTTPException(409, "任务正在处理，完成或失败后才能删除。")

    job = remove_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")

    # 「本机文件」直读时 job 目录里只有剧本；成片在用户自己的文件夹里，
    # 一个字节都不能动，所以这里只清理 data/uploads 下的任务目录。
    job_dir = job.script_path.parent
    if _is_inside(job_dir, UPLOAD_DIR):
        if job.external_media:
            job.script_path.unlink(missing_ok=True)
            try:
                job_dir.rmdir()
            except OSError:
                pass
        else:
            shutil.rmtree(job_dir, ignore_errors=True)

    # 「下一集」会带 keep_output=true：只清任务记录和上传素材，保留已导出的 SRT。
    if not keep_output:
        default_output = (OUTPUT_DIR / job.id).resolve()
        try:
            if job.output_dir.resolve() == default_output:
                shutil.rmtree(job.output_dir, ignore_errors=True)
            elif job.srt_path is not None:
                job.srt_path.unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True}
