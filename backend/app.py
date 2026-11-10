r"""字幕工作室后端：标准库 HTTP 服务 + JSON API + Range 视频流。

启动：.venv\Scripts\python.exe backend\app.py [--port 8765] [--no-browser]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import logging.handlers
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from functools import lru_cache
from urllib.parse import parse_qs, unquote, urlparse

from core import media, script, segment, srt_io, reference_match, hallucination  # noqa: E402
from core.asr import list_models  # noqa: E402
from core.jobs import JobManager  # noqa: E402
from core.match_tasks import MatchTasks  # noqa: E402
from core import validation  # noqa: E402
from core.model_manager import (CATALOG, DOWNLOADS, engine_gpu_ready, gpu_status,  # noqa: E402
                                list_auxiliary, set_model_location)
from core.storage import WRITE_LOCK, atomic_json, locked_file  # noqa: E402
from core.removal_models import INSTALLER as REMOVAL_INSTALLER, model_status as removal_model_status
from core.removal_jobs import REMOVAL_JOBS, validate_request as validate_removal_request, video_info as removal_video_info, project_file as removal_project_file
from core.removal_jobs import validate_segments as validate_removal_segments
from core.removal_scan import SCANNER as REMOVAL_SCANNER
from core.paths import (  # noqa: E402
    CONFIG_DIR, DATA_DIR, FRONTEND_DIR, OUTPUTS_DIR, PROJECTS_DIR, UPLOADS_DIR,
)

# 日志：写文件 + 控制台（pythonw 无 stdout 时仅写文件）
LOGS_DIR = DATA_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOGS_DIR / "app.log"
RUN_STATE = DATA_DIR / "run" / "instance.json"
_handlers = [logging.handlers.RotatingFileHandler(str(LOG_FILE), maxBytes=5 * 1024 ** 2,
                                                  backupCount=3, encoding="utf-8")]
if sys.stdout is not None:
    _handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("studio")
log.info("日志初始化：file=%s", LOG_FILE)

WEB_DIR = FRONTEND_DIR
OUTPUT_DIR = OUTPUTS_DIR
PROJECT_DIR = PROJECTS_DIR

VERSION = "3.1.0"
STARTED_AT = time.time()
SESSION_TOKEN = secrets.token_urlsafe(32)
DIALOG_LOCK = threading.Lock()
TASK_START_LOCK = threading.Lock()
MAX_BODY = 64 * 1024 * 1024
MAX_UPLOAD = 20 * 1024 ** 3
MAX_GAP_SECONDS = 2.0
MIN_WIDTH = 6.0
MAX_WIDTH = 30.0
JOBS = JobManager()
MATCH_TASKS = MatchTasks()


class RevisionConflict(ValueError):
    pass


def _qget(query: dict, key: str, default: str = "") -> str:
    vals = query.get(key)
    return vals[0] if vals else default


def read_log_tail(path: Path, since: int = 0, max_lines: int = 500) -> dict:
    """按偏移最多读取256KiB尾部，避免每秒读取整个历史日志。"""
    max_lines=max(1,min(max_lines,2000))
    try:
        with path.open('rb') as source:
            size=os.fstat(source.fileno()).st_size
            since=max(0,since) if since<=size else 0
            start=max(since,size-256*1024)
            source.seek(start);data=source.read(size-start)
    except FileNotFoundError:
        return {'lines':[],'offset':0,'size':0}
    if start>since and b'\n' in data:data=data.split(b'\n',1)[1]
    return {'lines':data.decode('utf-8',errors='replace').splitlines()[-max_lines:],'offset':size,'size':size}


def _clean_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("文件路径必须是字符串")
    value = (value or "").strip().strip('"').strip("'")
    if not value:
        raise ValueError("缺少文件路径")
    return os.path.abspath(os.path.expandvars(os.path.expanduser(value)))


def _require_file(value: str) -> str:
    path = _clean_path(value)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"文件不存在：{path}")
    return path


DIALOG_DIRS_FILE = CONFIG_DIR / "dialog_dirs.json"


def _read_dialog_dirs() -> dict:
    try:
        data = json.loads(DIALOG_DIRS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _dialog_start_dir(kind: str) -> str:
    """上次成功选择过的目录；换电脑后路径不存在则退回最近一次的目录。"""
    data = _read_dialog_dirs()
    for key in (kind, "last"):
        value = data.get(key)
        if isinstance(value, str) and value and os.path.isdir(value):
            return os.path.abspath(value)
    return ""


def _remember_dialog_dir(kind: str, folder: str) -> None:
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        return
    try:
        with locked_file(DIALOG_DIRS_FILE):
            data = _read_dialog_dirs()
            if data.get(kind) == folder and data.get("last") == folder:
                return
            data[kind] = folder
            data["last"] = folder
            atomic_json(DIALOG_DIRS_FILE, data)
    except OSError:
        log.debug("无法记录文件选择位置：%s", DIALOG_DIRS_FILE, exc_info=True)


def _project_path(media_path: str) -> Path:
    key = hashlib.sha1(os.path.abspath(media_path).encode("utf-8")).hexdigest()[:16]
    return PROJECT_DIR / f"{key}.json"


@lru_cache(maxsize=64)
def _content_digest(path: str, size: int, modified: int) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _media_identity(path: str) -> str:
    stat = os.stat(path)
    return _content_digest(path, stat.st_size, stat.st_mtime_ns)


def _load_project(path: str) -> tuple[dict | None, int]:
    target = _project_path(path)
    if target.exists():
        project = validation.mapping(json.loads(target.read_text(encoding='utf-8')), '工程')
        return project, project.get('revision', 0)
    if not media.is_media(path):
        return None, 0
    identity = _media_identity(path)
    matches = []
    for candidate in PROJECT_DIR.glob('*.json'):
        try:
            project = json.loads(candidate.read_text(encoding='utf-8'))
            old_path = project.get('media_path')
            previous = project.get('media_identity')
            # 兼容此前按上传目录保存的工程；内容相同才恢复，不能只按文件名。
            if not previous and old_path and os.path.isfile(old_path) and os.path.getsize(old_path) == os.path.getsize(path):
                previous = _media_identity(old_path)
            if previous == identity:
                matches.append(project)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return (max(matches, key=lambda p: str(p.get('updated_at', ''))), 0) if matches else (None, 0)


def _config_file() -> Path:
    return CONFIG_DIR / "ui.json"


def _read_config() -> dict:
    target = _config_file()
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _safe_filename(value: str) -> str:
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        return "upload"
    suffix = Path(name).suffix[:12]
    stem = Path(name).stem[:96] or "upload"
    return stem + suffix


def _output_path(media_path: str, out_dir: str | None, overwrite: bool, extension: str = 'srt') -> Path:
    if media_path:
        media_path = os.path.abspath(media_path)
        stem = Path(media_path).stem
        folder = _clean_path(out_dir) if out_dir else str(Path(media_path).parent)
    else:
        stem = "字幕_" + time.strftime("%Y%m%d_%H%M%S")
        folder = _clean_path(out_dir) if out_dir else str(OUTPUT_DIR)
    os.makedirs(folder, exist_ok=True)
    target = Path(folder) / f"{stem}.{extension}"
    if not overwrite and target.exists():
        i = 2
        while (Path(folder) / f"{stem}_{i}.{extension}").exists():
            i += 1
        target = Path(folder) / f"{stem}_{i}.{extension}"
    return target


class Handler(BaseHTTPRequestHandler):
    server_version = f"SubtitleStudio/{VERSION}"
    protocol_version = "HTTP/1.1"

    def _check_request(self) -> bool:
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        hosts = self.headers.get_all("Host", [])
        origin = self.headers.get("Origin")
        if (len(hosts) != 1 or hosts[0].lower() not in allowed
                or (origin is not None and origin not in {f"http://{h}" for h in allowed})
                or self.headers.get("Sec-Fetch-Site") == "cross-site"):
            self.close_connection = True
            self.send_json({"ok": False, "error": "拒绝非本机页面请求"}, 403)
            return False
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            token = self.headers.get("X-SubtitleStudio-Token", "")
            if parsed.path == "/api/media/stream":
                token = token or _qget(parse_qs(parsed.query), "token")
            if not token.isascii() or not secrets.compare_digest(token, SESSION_TOKEN):
                self.close_connection = True
                self.send_json({"ok": False, "error": "页面会话已失效，请刷新页面"}, 403)
                return False
        return True

    def do_OPTIONS(self) -> None:
        self.close_connection = True
        self.send_json({"ok": False, "error": "不允许跨来源调用"}, 403)

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)

    # ---------- 基础响应 ----------
    def send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_bytes(self, body: bytes, content_type: str, status: int = 200,
                   extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_file(self, path: str, content_type: str | None = None) -> None:
        # 先打开文件；发头之后的读错误只能关闭连接，不能再发送第二份JSON响应。
        with open(path, "rb") as stream:
            self._send_open_file(stream, content_type or media.content_type(path))

    def _send_open_file(self, stream, content_type: str) -> None:
        file_size = os.fstat(stream.fileno()).st_size
        start, end = 0, file_size - 1
        status = 200
        range_header = self.headers.get("Range", "")
        if range_header:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            valid = bool(m and (m.group(1) or m.group(2)) and file_size)
            if valid:
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), file_size - 1) if m.group(2) else file_size - 1
                else:
                    suffix = int(m.group(2))
                    start, end = max(0, file_size - suffix), file_size - 1
                    valid = suffix > 0
                valid = valid and start < file_size and start <= end
            if not valid:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-cache")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()
        try:
            stream.seek(start)
            remaining = length
            while remaining > 0:
                chunk = stream.read(min(256 * 1024, remaining))
                if not chunk:
                    self.close_connection = True
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        except OSError:
            self.close_connection = True

    def read_json(self) -> dict:
        self.close_connection = True  # 非法/未读完的请求不复用连接。
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("不支持Transfer-Encoding")
        if len(self.headers.get_all("Content-Length", [])) > 1:
            raise ValueError("重复Content-Length")
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0:
            raise ValueError("Content-Length不能为负数")
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请求体必须为application/json")
        raw = self.rfile.read(length)
        try:
            if len(raw) != length:
                raise ValueError("请求体不完整")
            data = json.loads(raw.decode("utf-8"), parse_constant=lambda x: (_ for _ in ()).throw(ValueError("禁止非有限数字")))
        except Exception as exc:
            raise ValueError(f"JSON 解析失败：{exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def log_message(self, fmt: str, *args) -> None:
        # 轮询与静态资源不刷屏，仅记录错误
        path = getattr(self, "path", "")
        if path.startswith(("/api/jobs/", "/api/logs")):
            return
        if getattr(self, "command", "") == "GET" and path and not path.startswith("/api/"):
            return
        log.info("HTTP %s", fmt % args)

    # ---------- 路由 ----------
    def do_GET(self) -> None:
        if not self._check_request():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/" or path == "/index.html":
                self._static("index.html")
            elif path == "/favicon.ico":
                self._static("assets/studio-film.ico")
            elif not path.startswith("/api/"):
                self._static(unquote(path).lstrip("/"))
            elif path == "/api/health":
                self.send_json({"ok": True, "version": VERSION, "python": sys.version.split()[0],
                                "uptime": int(time.time() - STARTED_AT), "port": self.server.server_port,
                                "log_file": str(LOG_FILE),
                                "models": list_models(), "auxiliary": list_auxiliary(),
                                "gpu": gpu_status(), "data_dir": str(DATA_DIR)})
            elif path == "/api/models":
                self.send_json({"ok": True, "models": list_models(), "auxiliary": list_auxiliary(),
                                "gpu": gpu_status()})
            elif path == "/api/models/download":
                self.send_json({"ok": True, "download": DOWNLOADS.get()})
            elif path == '/api/removal/models':
                gpu_download = DOWNLOADS.get()
                self.send_json({'ok': True, 'model': removal_model_status(), 'installation': REMOVAL_INSTALLER.get(),
                                'gpu_installation': gpu_download if gpu_download.get('model') == 'gpu' else None})
            elif path == '/api/removal/scan':
                scan = REMOVAL_SCANNER.get(_qget(query, 'id', ''))
                self.send_json({'ok': scan is not None, 'scan': scan}, 200 if scan else 404)
            elif path == '/api/removal/info':
                info = removal_video_info(_require_file(_qget(query, 'path')))
                project = removal_project_file(info['path'])
                settings = json.loads(project.read_text(encoding='utf-8')) if project.is_file() else None
                self.send_json({'ok': True, 'info': info, 'project': settings})
            elif re.fullmatch(r'/api/removal/jobs/[a-f0-9]{32}', path):
                job = REMOVAL_JOBS.get(path.rsplit('/', 1)[1])
                self.send_json({'ok': job is not None, 'job': job}, 200 if job else 404)
            elif path == "/api/config":
                self.send_json({"ok": True, "config": _read_config(), "path": str(_config_file())})
            elif path == "/api/logs":
                since = int(_qget(query, "since", "0") or 0)
                max_lines = int(_qget(query, "lines", "500") or 500)
                self.send_json({"ok": True, **read_log_tail(LOG_FILE,since,max_lines)})
            elif path == "/api/media/info":
                p = _require_file(_qget(query, "path"))
                self.send_json({"ok": True, "info": media.probe(p)})
            elif path == "/api/media/stream":
                p = _require_file(_qget(query, "path"))
                if not media.is_media(p):
                    raise ValueError("不支持的媒体格式")
                self.send_file(p)
            elif path == "/api/waveform":
                p = _require_file(_qget(query, "path"))
                buckets = int(_qget(query, "buckets", "1200") or 1200)
                if "start" in query or "end" in query:
                    start = validation.number(float(_qget(query, "start")), "波形开始")
                    end = validation.number(float(_qget(query, "end")), "波形结束")
                    result = media.waveform_region(p, start, end, buckets)
                else:
                    result = media.waveform(p, buckets)
                self.send_json({"ok": True, **result})
            elif path == '/api/projects':
                projects=[]
                for file in PROJECT_DIR.glob('*.json'):
                    try:
                        saved=json.loads(file.read_text(encoding='utf-8'))
                        source=saved.get('source_path') or saved.get('media_path')
                        if not isinstance(source,str) or not source:
                            continue
                        projects.append({'name':Path(source).name,'source_path':source,
                            'updated_at':str(saved.get('updated_at','')),'count':len(saved.get('cues',[])),
                            'available':os.path.isfile(source)})
                    except (OSError,ValueError,TypeError,AttributeError):
                        continue
                projects.sort(key=lambda p:p['updated_at'],reverse=True)
                self.send_json({'ok':True,'projects':projects,'directory':str(PROJECT_DIR)})
            elif path == "/api/projects/load":
                p = _require_file(_qget(query, "path"))
                project, revision = _load_project(p)
                if project is not None:
                    validation.cues(project.get("cues", []))
                    validation.lines(project.get("script_lines", []))
                    validation.mapping(project.get("settings", {}), "工程settings")
                    # 载入旧工程时补做全文幻觉检测；已标记或用户恢复过导出的条目不重复处理。
                    flagged = hallucination.mark(project.get("cues", []))
                    self.send_json({"ok": True, "project": project, "revision": revision,
                                    "hallucination": {"count": len(flagged)}})
                else:
                    self.send_json({"ok": True, "project": None})
            elif path == '/api/script/match/status':
                identity = _qget(query, 'id')
                self.send_json({'ok': True, 'task': MATCH_TASKS.status(identity)})
            elif re.match(r"^/api/jobs/[a-f0-9]+$", path):
                job = JOBS.get(path.rsplit("/", 1)[-1])
                if job is None:
                    self.send_json({"ok": False, "error": "任务不存在"}, 404)
                else:
                    self.send_json({"ok": True, "job": job.to_dict(include_cues=_qget(query, "result") == "1")})
            else:
                self.send_json({"ok": False, "error": "接口不存在"}, 404)
        except FileNotFoundError as exc:
            self.send_json({"ok": False, "error": str(exc)}, 404)
        except RevisionConflict as exc:
            self.send_json({"ok": False, "error": str(exc)}, 409)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:  # pragma: no cover
            log.exception("请求处理失败：%s %s", self.command, self.path.split("?", 1)[0])
            self.send_json({"ok": False, "error": f"服务器错误：{exc}"}, 500)

    def do_POST(self) -> None:
        if not self._check_request():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/upload":
                self._upload(parse_qs(parsed.query))
                return
            body = self.read_json()
            if path == "/api/jobs":
                self._create_job(body)
            elif path == '/api/removal/models/install':
                with TASK_START_LOCK:
                    if REMOVAL_JOBS.busy() or any(j.status in ('queued', 'running') for j in list(JOBS.jobs.values())) or DOWNLOADS.get()['status'] in ('downloading', 'preparing'):
                        raise ValueError('请先完成或取消现有任务，再准备视频修复模型')
                    self.send_json({'ok': True, 'installation': REMOVAL_INSTALLER.start(body.get('device', 'cuda'))})
            elif path == '/api/removal/models/cancel':
                self.send_json({'ok': True, 'installation': REMOVAL_INSTALLER.cancel()})
            elif path == '/api/removal/jobs':
                with TASK_START_LOCK:
                    if REMOVAL_SCANNER.busy():
                        raise ValueError('请先完成或取消字幕时间段扫描')
                    if any(j.status in ('queued', 'running') for j in list(JOBS.jobs.values())) or DOWNLOADS.get()['status'] in ('downloading', 'preparing'):
                        raise ValueError('请先完成或取消现有识别/模型下载任务')
                    self.send_json({'ok': True, 'job': REMOVAL_JOBS.submit(body)})
            elif path == '/api/removal/scan':
                with TASK_START_LOCK:
                    if REMOVAL_JOBS.busy() or any(j.status in ('queued', 'running') for j in list(JOBS.jobs.values())):
                        raise ValueError('请先完成或取消视频/识别任务')
                    self.send_json({'ok': True, 'scan': REMOVAL_SCANNER.start(body)})
            elif path == '/api/removal/scan/cancel':
                self.send_json({'ok': True, 'scan': REMOVAL_SCANNER.cancel(body.get('scan_id', ''))})
            elif re.fullmatch(r'/api/removal/jobs/[a-f0-9]{32}/cancel', path):
                job = REMOVAL_JOBS.cancel(path.split('/')[4])
                self.send_json({'ok': job is not None, 'job': job}, 200 if job else 404)
            elif path == '/api/removal/project':
                request = validate_removal_request({**body, 'segments': None, 'region': body.get('region') or {'x': 0, 'y': 0, 'width': 1, 'height': 1}, 'preview': False, 'output_dir': str(OUTPUT_DIR)})
                settings = {key: request[key] for key in ('path', 'region', 'start', 'end', 'max_edge', 'format', 'device')}
                settings['segments'] = validate_removal_segments(body.get('segments'), request['info']['duration'], require_enabled=False)
                settings['updated_at'] = time.time()
                if body.get('region') is None:
                    settings['region'] = None
                atomic_json(removal_project_file(request['path']), settings)
                self.send_json({'ok': True})
            elif path == "/api/alignment/jobs":
                with TASK_START_LOCK:
                    self._create_alignment_job(body)
            elif path == "/api/models/download":
                with TASK_START_LOCK:
                    if REMOVAL_JOBS.busy() or REMOVAL_INSTALLER.busy():
                        raise ValueError('请先完成或取消视频修复任务')
                    if any(job.status in ("queued", "running") for job in list(JOBS.jobs.values())):
                        raise ValueError("请先完成或取消识别任务，再下载模型")
                    self.send_json({"ok": True, "download": DOWNLOADS.start(body.get("model", ""))})
            elif path == "/api/runtime/gpu":
                with TASK_START_LOCK:
                    if REMOVAL_JOBS.busy() or REMOVAL_INSTALLER.busy():
                        raise ValueError('请先完成或取消视频修复任务')
                    if any(job.status in ("queued", "running") for job in list(JOBS.jobs.values())):
                        raise ValueError("请先完成或取消识别任务，再下载 GPU 环境")
                    self.send_json({"ok": True, "download": DOWNLOADS.start_gpu()})
            elif path == "/api/models/cancel":
                self.send_json({"ok": True, "download": DOWNLOADS.cancel()})
            elif path == "/api/models/locate":
                location = set_model_location(str(body.get("model", "")), body.get("path") or "")
                self.send_json({"ok": True, "path": str(location) if location else "",
                                "models": list_models()})
            elif re.match(r"^/api/jobs/[a-f0-9]+/cancel$", path):
                job = JOBS.cancel(path.split("/")[3])
                if job is None:
                    self.send_json({"ok": False, "error": "任务不存在"}, 404)
                else:
                    self.send_json({"ok": True, "job": job.to_dict(include_cues=False)})
            elif path == "/api/srt/parse":
                self._parse_srt(body)
            elif path == "/api/srt/export":
                self._export_srt(body)
            elif path == "/api/script/parse":
                self._parse_script(body)
            elif path == "/api/script/match":
                self._match_script(body)
            elif path == '/api/script/match/cancel':
                self.send_json({'ok': True, 'cancelled': MATCH_TASKS.cancel(str(body.get('request_id', '')))})
            elif path == "/api/segment":
                self._segment(body)
            elif path == "/api/segment/merge":
                self._merge(body)
            elif path == "/api/projects/save":
                self._save_project(body)
            elif path == "/api/projects/backup":
                self._save_project(body, backup=True)
            elif path == "/api/config":
                self._save_config(body)
            elif path == "/api/dialog":
                self._dialog(body)
            elif path == "/api/open_folder":
                p = _clean_path(body.get("path", ""))
                media.open_path(p)
                self.send_json({"ok": True})
            else:
                self.send_json({"ok": False, "error": "接口不存在"}, 404)
        except FileNotFoundError as exc:
            self.send_json({"ok": False, "error": str(exc)}, 404)
        except RevisionConflict as exc:
            self.send_json({"ok": False, "error": str(exc)}, 409)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:  # pragma: no cover
            log.exception("请求处理失败：%s %s", self.command, self.path.split("?", 1)[0])
            self.send_json({"ok": False, "error": f"服务器错误：{exc}"}, 500)

    # ---------- 静态文件 ----------
    def _static(self, name: str) -> None:
        if name == "favicon.ico":
            self._static("assets/studio-film.ico")
            return
        target = (WEB_DIR / name).resolve()
        if WEB_DIR.resolve() not in target.parents or not target.is_file():
            self.send_json({"ok": False, "error": "页面不存在"}, 404)
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        body = target.read_bytes()
        if name == "index.html":
            body = body.replace(b"__SUBTITLE_SESSION_TOKEN__", SESSION_TOKEN.encode("ascii"))
        self.send_bytes(body, ctype)

    # ---------- API 实现 ----------

    def _create_alignment_job(self, body: dict) -> None:
        if REMOVAL_SCANNER.busy():
            raise ValueError('字幕时间段正在扫描，请先完成或取消')
        if REMOVAL_JOBS.busy() or REMOVAL_INSTALLER.busy():
            raise ValueError('视频修复正在运行，请完成或取消后再对齐')
        if DOWNLOADS.get()["status"] in ("downloading", "preparing"):
            raise ValueError("请先完成模型下载")
        path = _require_file(body.get("path", ""))
        if not media.is_media(path):
            raise ValueError("需要视频或音频素材")
        cues = validation.cues(body.get("cues", []),repair_word_times=True)
        if not cues:
            raise ValueError("请先导入或识别字幕")
        if not any(m["name"] == "qwen3-forcedaligner-0.6b" and m["available"] for m in list_auxiliary()):
            raise ValueError("请先在辅模菜单下载 Qwen3-ForcedAligner-0.6B")
        device = body.get("device", "auto")
        if device not in ("auto", "cpu", "cuda"):
            raise ValueError("设备无效")
        if device == "cuda" and not engine_gpu_ready("qwen"):
            raise ValueError("当前引擎环境没有 CUDA 版 PyTorch。请先在“设备”下方点“下载 GPU 环境”，"
                             "或把设备改为“自动/CPU”。")
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{12,32}", request_id):
            raise ValueError("任务标识无效")
        max_width = validation.number(body.get('max_width', 20), '每行字数', MIN_WIDTH, MAX_WIDTH)
        gap_seconds = validation.number(body.get('gap_seconds', .6), '停顿阈值', 0, MAX_GAP_SECONDS)
        width_split = body.get('width_split', True)
        if not isinstance(width_split, bool):
            raise ValueError('按字数拆条参数无效')
        job = JOBS.submit(path, "qwen3-forcedaligner-0.6b", device, False, gap_seconds, request_id,
                          task="align", input_cues=cues, max_width=max_width, width_split=width_split)
        self.send_json({"ok": True, "job": job.to_dict(include_cues=False)})

    def _create_job(self, body: dict) -> None:
        with TASK_START_LOCK:
            self._create_job_locked(body)

    def _create_job_locked(self, body: dict) -> None:
        if REMOVAL_SCANNER.busy():
            raise ValueError('字幕时间段正在扫描，请先完成或取消')
        if REMOVAL_JOBS.busy() or REMOVAL_INSTALLER.busy():
            raise ValueError('视频修复正在运行，请完成或取消后再识别')
        if DOWNLOADS.get()["status"] in ("downloading", "preparing"):
            raise ValueError("模型正在下载或安装，请完成后再识别")
        path = _require_file(body.get("path", ""))
        if not media.is_media(path):
            raise ValueError("不支持的媒体格式")
        model = str(body.get("model") or "medium")
        available = {m["name"] for m in list_models() if m["available"]}
        if model not in available:
            raise ValueError(f"模型 {model} 未下载，当前可用：{', '.join(sorted(available)) or '无'}")
        device = str(body.get("device") or "auto")
        if device not in ("auto", "cpu", "cuda"):
            raise ValueError("device 只能是 auto/cpu/cuda")
        engine = CATALOG.get(model, {}).get("engine")
        if device == "cuda" and engine in ("qwen", "firered") and not engine_gpu_ready(engine):
            raise ValueError("当前引擎环境没有 CUDA 版 PyTorch。请先在“设备”下方点“下载 GPU 环境”，"
                             "或把设备改为“自动/CPU”。")
        gap_seconds = validation.number(body.get("gap_seconds", 0.6), "停顿阈值", 0, MAX_GAP_SECONDS)
        if not (0 <= gap_seconds <= MAX_GAP_SECONDS):
            raise ValueError(f"停顿断句阈值需在 0-{MAX_GAP_SECONDS:g} 秒之间")
        request_id = body.get("request_id")
        if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{12,32}", request_id)):
            raise ValueError("request_id需为12–32位小写十六进制")
        max_width = validation.number(body.get('max_width', 20), '每行字数', MIN_WIDTH, MAX_WIDTH)
        region = body.get('region')
        if region is not None:
            validation.mapping(region,'本段时间')
            start=validation.number(region.get('start'),'本段开始')
            end=validation.number(region.get('end'),'本段结束')
            if not start < end or end-start > 600:raise ValueError('本段须为10分钟以内的有效区间')
            region={'start':start,'end':end}
        # 兼容旧前端：drop_interjections=true 等价于 keep_interjections=false。
        if 'keep_interjections' in body:
            keep_interjections = validation.boolean(body.get('keep_interjections'), '语气词')
        elif 'drop_interjections' in body:
            keep_interjections = not validation.boolean(body.get('drop_interjections'), '语气词过滤')
        else:
            keep_interjections = False
        job = JOBS.submit(path, model, device, validation.boolean(body.get("vad", True), "VAD"), gap_seconds, request_id=request_id, max_width=max_width,
                          task='asr_region' if region else 'asr', input_cues=[region] if region else None,
                          keep_interjections=keep_interjections)
        self.send_json({"ok": True, "job": job.to_dict(include_cues=False)})

    def _parse_srt(self, body: dict) -> None:
        if body.get("path"):
            path = _require_file(body["path"])
            cues, warnings, encoding = srt_io.parse_srt_file(path)
            name = os.path.basename(path)
        else:
            text = str(body.get("text") or "")
            cues, warnings = srt_io.parse_srt_text(text)
            encoding, name = "text", ""
        # 人工导入SRT仅提示；用户确认的片尾对白不能按ASR幻觉规则自动排除。
        flagged = hallucination.mark(cues,auto_exclude=False)
        self.send_json({"ok": True, "cues": cues, "warnings": warnings,
                        "encoding": encoding, "name": name,
                        "hallucination": {"count": len(flagged)}})

    def _export_srt(self, body: dict) -> None:
        export_format=body.get('format','srt')
        if export_format not in ('srt','mkv'):
            raise ValueError('不支持的导出格式')
        cues = validation.cues(body.get("cues", []))
        if not isinstance(cues, list) or not cues:
            raise ValueError("没有可导出的字幕")
        options = validation.mapping(body.get("options", {}), "options")
        for key in ("add_punctuation", "with_role", "overwrite", "keep_punctuation", "skip_empty", "single_line"):
            if key in options:
                validation.boolean(options[key], key)
        media_path = _clean_path(body.get("media_path", "")) if body.get("media_path") else ""
        duration = 0.0
        if media_path:
            if not os.path.isfile(media_path):
                raise FileNotFoundError(f"视频不存在：{media_path}")
            duration = float(media.probe(media_path).get("duration") or 0)
        cues = srt_io.prepare_export_cues(
            cues, keep_punctuation=options.get("keep_punctuation", True),
            skip_empty=options.get("skip_empty", True), single_line=options.get("single_line", False),
        )
        errors = srt_io.validate_cues(cues, duration)
        if errors:
            self.send_json({"ok": False, "error": "导出检查未通过", "errors": errors}, 400)
            return
        text = srt_io.format_srt(
            cues,
            add_punctuation=bool(options.get("add_punctuation", False)) and options.get("keep_punctuation", True),
            with_role=bool(options.get("with_role", False)),
        )
        if export_format == 'mkv':
            if not media_path:
                raise ValueError('MKV 导出需要先导入视频或音频')
            with WRITE_LOCK:
                target=_output_path(media_path,options.get('output_dir'),False,'mkv')
                target.open('xb').close()
            temp=None
            try:
                handle,name=tempfile.mkstemp(prefix='.subtitle-export-',suffix='.mkv',dir=target.parent)
                os.close(handle);temp=Path(name)
                media.remux_with_subtitles(media_path,text,str(temp))
                os.replace(temp,target)
            except Exception:
                target.unlink(missing_ok=True)
                raise
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)
            self.send_json({'ok':True,'path':str(target),'count':len(cues),'format':'mkv'})
            return
        with WRITE_LOCK:
            overwrite = bool(options.get("overwrite", False))
            target = _output_path(media_path, options.get("output_dir"), overwrite)
            with target.open("w" if overwrite else "x", encoding="utf-8", newline="") as fh:
                fh.write(text)
        self.send_json({"ok": True, "path": str(target), "count": len(cues)})

    def _parse_script(self, body: dict) -> None:
        remove_after = False
        if 'documents' in body:
            document=reference_match.parse_documents(body['documents'])
            text=document['text'];encoding,name='text',''
        elif body.get("path"):
            path = _require_file(body["path"])
            text, encoding = script.read_script_text(path)
            name = os.path.basename(path)
            remove_after = bool(body.get("remove_after"))
        else:
            text = str(body.get("text") or "")
            encoding, name = "text", ""
        if 'documents' not in body:
            document = reference_match.parse_document(text,body.get('sources'))
        lines = document["paragraphs"]
        if remove_after:
            try:
                uploaded = Path(path).resolve()
                uploads = UPLOADS_DIR.resolve()
                if uploads in uploaded.parents:
                    uploaded.unlink(missing_ok=True)
                    uploaded.parent.rmdir()
            except OSError:
                pass
        self.send_json({"ok": True, "lines": lines, "document": document,
                        "stats": {"total": len(lines), "characters": len(text)},
                        "encoding": encoding, "name": name})

    def _match_script(self, body: dict) -> None:
        cues = validation.cues(body.get("cues", []),repair_word_times=True)
        lines = validation.lines(body.get("lines", []))
        if not isinstance(cues, list) or not isinstance(lines, list):
            raise ValueError("cues/lines 必须是数组")
        document = validation.mapping(body.get("document", {}), "document")
        if not document:
            document = {"text": "\n".join(line["text"] for line in lines)}
        options = validation.mapping(body.get("options", {}), "匹配选项")
        force = body.get("force_indices", [])
        if not isinstance(force, list) or any(isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(cues) for i in force):
            raise ValueError("重新匹配序号无效")
        bounds=body.get('position_bounds')
        if bounds is not None:
            if not isinstance(bounds,list) or len(bounds)!=2:raise ValueError('参考位置区间无效')
            bounds=[validation.number(v,'参考位置',0,len(document.get('text',''))) for v in bounds]
            if bounds[0]>bounds[1]:raise ValueError('参考位置顺序无效')
        sequence=body.get('sequence_indices')
        if sequence is not None:
            if (not isinstance(sequence,list) or len(sequence)!=len(cues)
                    or any(type(v) is not int or v<0 for v in sequence)
                    or any(a>=b for a,b in zip(sequence,sequence[1:]))):raise ValueError('字幕上下文序号无效')
        identity = body.get('request_id')
        if identity is not None:
            if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{12,32}', identity):
                raise ValueError('匹配任务标识无效')
            result = MATCH_TASKS.run(identity, lambda checkpoint: reference_match.match_reference(
                cues, document, options, force, bounds, sequence, checkpoint=checkpoint))
        else:
            result = reference_match.match_reference(cues, document, options, force, bounds,sequence)
        self.send_json({"ok": True, **result})

    def _segment(self, body: dict) -> None:
        cues = validation.cues(body.get("cues", []),repair_word_times=True)
        if not isinstance(cues, list):
            raise ValueError("cues 必须是数组")
        max_width = validation.number(body.get("max_width", 20), "每行宽度", MIN_WIDTH, MAX_WIDTH)
        if not (MIN_WIDTH <= max_width <= MAX_WIDTH):
            raise ValueError(f"每行宽度需在 {MIN_WIDTH:g}-{MAX_WIDTH:g} 之间")
        if body.get('mode')=='midpoint':
            if len(cues)!=1:raise ValueError('中间分段一次只处理一条字幕')
            result,splits=segment.segment_midpoint(cues[0],max_width)
            self.send_json({'ok':True,'cues':result,'splits':splits,'gap_splits':0,
                            'estimated':sum(bool(c.get('estimated')) for c in result),'count':len(result)})
            return
        if body.get('mode')=='calibration':
            from core.calibration import normalize_cues
            gap = validation.number(body.get('gap_seconds', .6), '停顿阈值', 0, MAX_GAP_SECONDS)
            keep_punc = validation.boolean(body.get('keep_punc', True), '标点符号')
            keep_interjections = validation.boolean(body.get('keep_interjections', False), '语气词')
            self.send_json({'ok': True, **normalize_cues(cues, max_width, gap, keep_punc=keep_punc,
                                                        keep_interjections=keep_interjections)})
            return
        gap_seconds = validation.number(body.get("gap_seconds", 0), "停顿阈值", 0, MAX_GAP_SECONDS)
        if not (0 <= gap_seconds <= MAX_GAP_SECONDS):
            raise ValueError(f"停顿断句阈值需在 0-{MAX_GAP_SECONDS:g} 秒之间")
        gap_splits = 0
        pauses_only = validation.boolean(body.get('pauses_only', False), '仅按停顿分段')
        if gap_seconds > 0:
            # 仅按停顿重分时，“每行字数”不参与，避免宽度设置影响停顿切点。
            cues, gap_splits = segment.split_on_gaps(cues, gap_seconds, float('inf') if pauses_only else max_width)
        if pauses_only:
            result, splits, estimated = cues, 0, 0
        else:
            result, splits, estimated = segment.segment_cues(cues, max_width)
            result, _ = segment.repair_fragments(result,max_width,gap_seconds)
        self.send_json({"ok": True, "cues": result, "splits": splits, "gap_splits": gap_splits,
                        "estimated": estimated, "count": len(result)})

    def _merge(self, body: dict) -> None:
        cues = validation.cues(body.get("cues", []))
        if not isinstance(cues, list):
            raise ValueError("cues 必须是数组")
        result, merged = segment.merge_cues(
            cues,
            max_width=validation.number(body.get("max_width", 20), "每行宽度", MIN_WIDTH, MAX_WIDTH),
            max_duration=validation.number(body.get("max_duration", 7), "合并时长", 0.001, 60),
            max_gap=validation.number(body.get("max_gap", 0.25), "合并间隔", 0, 2),
        )
        self.send_json({"ok": True, "cues": result, "merged": merged, "count": len(result)})

    def _save_config(self, body: dict) -> None:
        settings = validation.mapping(body.get("settings", {}), "settings")
        layout = validation.mapping(body.get("layout", {}), "layout")
        if "gap" in settings:
            validation.number(settings["gap"], "停顿阈值", 0, MAX_GAP_SECONDS)
        if "width" in settings:
            validation.number(settings["width"], "每行宽度", MIN_WIDTH, MAX_WIDTH)
        for key in ("left", "right"):
            if key in layout:
                validation.number(layout[key], "栏宽", 100, 2000)
        theme = str(body.get("theme") or "system")
        if theme not in ("system", "light", "dark"):
            theme = "system"
        data = {
            "settings": settings,
            "layout": layout,
            "layout_version": 2 if body.get("layoutVersion") == 2 else 0,
            "theme": theme,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        target = _config_file()
        with locked_file(target):
            atomic_json(target, data)
        self.send_json({"ok": True, "path": str(target), "updated_at": data["updated_at"]})

    def _save_project(self, body: dict, backup: bool = False) -> None:
        media_path = _require_file(body.get("media_path") or body.get("source_path", ""))
        if not media.is_media(media_path) and Path(media_path).suffix.lower() != ".srt":
            raise ValueError("工程来源必须为媒体或SRT文件")
        cues = validation.cues(body.get("cues", []),repair_word_times=True)
        expected = body.get("expected_revision", 0)
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise ValueError("expected_revision必须是非负整数")
        script_document=validation.mapping(body.get('script_document') or {},'script_document')
        if script_document:
            script_document=reference_match.parse_document(script_document.get('text'),script_document.get('sources'))
        data = {
            "version": 2,
            "media_path": media_path if media.is_media(media_path) else "",
            "source_path": media_path,
            "cues": cues,
            "initial_cues": validation.cues(body['initial_cues']) if body.get('initial_cues') is not None else None,
            "media_identity": _media_identity(media_path) if media.is_media(media_path) else '',
            "script_lines": validation.lines(body.get("script_lines", [])),
            "script_name": str(body.get("script_name") or ""),
            "script_path": str(body.get("script_path") or ""),
            "script_names": validation.script_names(body.get("script_names", [])),
            "script_document": script_document,
            "subtitle_file": validation.mapping(body.get("subtitle_file") or {}, "subtitle_file"),
            "settings": validation.mapping(body.get("settings", {}), "settings"),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        target = _project_path(media_path)
        if backup:
            target = PROJECT_DIR / "backups" / (target.stem + "-" + secrets.token_hex(8) + ".json")
            data["revision"] = 0
            data["backup_of"] = str(_project_path(media_path))
            atomic_json(target, data)
            self.send_json({"ok": True, "path": str(target)})
            return
        with locked_file(target):
            current = validation.mapping(json.loads(target.read_text(encoding="utf-8")), "已有工程") if target.exists() else {}
            revision = current.get("revision", 0)
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                raise ValueError("已有工程版本号无效，已保留原文件，请先导出字幕备份")
            if expected != revision:
                raise RevisionConflict("工程已由其他页面更新；请先导出当前字幕备份，再重新加载工程")
            data["revision"] = revision + 1
            atomic_json(target, data)
        self.send_json({"ok": True, "path": str(target), "updated_at": data["updated_at"], "revision": data["revision"]})

    def _upload(self, query: dict) -> None:
        """浏览器拖入的文件：流向 data/uploads 工作目录，供识别与导出使用。"""
        self.close_connection = True
        self.connection.settimeout(60)
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
            raise ValueError("上传必须使用单个Content-Length")
        kind = _qget(query, "kind", "media")
        if kind not in ("media", "script", "srt"):
            raise ValueError("上传类型不受支持")
        name = _safe_filename(unquote(self.headers.get("X-Upload-Name", "")))
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("上传内容为空")
        if length > MAX_UPLOAD:
            raise ValueError("文件超过 20 GB")
        suffix = Path(name).suffix.lower()
        if kind == "media" and not media.is_media(name):
            raise ValueError("不支持的媒体格式")
        if kind == "script" and suffix not in (".txt", ".md", ".docx"):
            raise ValueError("剧本仅支持 txt / md / docx")
        if kind == "srt" and (suffix != ".srt" or length > 10 * 1024**2):
            raise ValueError("字幕仅支持 10 MB 以内的 SRT 文件")

        root = UPLOADS_DIR / ("_scripts" if kind == "script" else "_subtitles" if kind == "srt" else "drag")
        root.mkdir(parents=True, exist_ok=True)
        workdir = root / f"{time.strftime('%Y%m%d_%H%M%S')}_{os.urandom(3).hex()}"
        workdir.mkdir(parents=True, exist_ok=False)
        target = workdir / name
        temp = target.with_suffix(target.suffix + ".part")
        remaining = length
        try:
            with open(temp, "wb") as fh:
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ConnectionError("上传中断")
                    fh.write(chunk)
                    remaining -= len(chunk)
            os.replace(temp, target)
        except Exception:
            try:
                temp.unlink(missing_ok=True)
                workdir.rmdir()
            except OSError:
                pass
            raise
        self.send_json({
            "ok": True, "path": str(target), "name": name,
            "dir": str(target.parent), "size": length,
        })

    def _dialog(self, body: dict) -> None:
        # 独立进程运行系统选择器；同一服务只允许一个对话框。
        if not DIALOG_LOCK.acquire(blocking=False):
            raise ValueError("已有文件选择框打开，请先完成选择")
        try:
            kind = str(body.get("kind") or "media")
            if kind not in ("media", "script", "srt", "dir"):
                raise ValueError("未知对话框类型")
            memory_kind='export' if kind=='dir' and body.get('purpose')=='export' else kind
            request = dict(body)
            initial = _dialog_start_dir(memory_kind)
            if initial:
                request["initial_dir"] = initial
            try:
                result = subprocess.run(
                    [str(Path(sys.executable).with_name("python.exe")) if os.name == "nt" else sys.executable,
                     str(Path(__file__).parent / "core" / "file_dialog.py")],
                    input=json.dumps(request), capture_output=True, text=True, encoding="utf-8",
                    timeout=600, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except subprocess.TimeoutExpired:
                raise ValueError("文件选择框长时间无响应，请关闭选择框后重试") from None
            if result.returncode:
                log.error("系统文件选择框失败：%s", result.stderr[-4000:])
                raise ValueError("无法打开系统文件选择框，请在帮助 → 终端查看具体原因；也可拖入文件")
            payload = json.loads(result.stdout)
            if not isinstance(payload, dict):
                raise ValueError("文件选择框返回格式无效")
            paths = payload.get("paths")
            chosen = next((p for p in paths if isinstance(p, str) and p), "") if isinstance(paths, list) else ""
            if not chosen and isinstance(payload.get("path"), str):
                chosen = payload["path"]
            if chosen:
                folder = chosen if kind == "dir" else os.path.dirname(chosen)
                if folder:
                    _remember_dialog_dir(memory_kind, folder)
            self.send_json(payload)
        finally:
            DIALOG_LOCK.release()


def _dpi_aware() -> None:
    """让窗口尺寸按物理像素计算，避免 2K/125% 被误判成 2048 宽。"""
    try:
        import ctypes

        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass


def window_geometry() -> tuple[int, int, int, int] | None:
    """按主屏工作区返回居中窗口尺寸与位置：(宽, 高, x, y)。"""
    if os.name != "nt":
        return None
    _dpi_aware()
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if not ctypes.windll.user32.SystemParametersInfoW(
            0x0030, 0, ctypes.byref(rect), 0  # SPI_GETWORKAREA
        ):
            return None
        work_w = rect.right - rect.left
        work_h = rect.bottom - rect.top
        if work_w < 640 or work_h < 480:
            return None
        # 窗口比屏幕降一档：4K→2K，2K→1080p，1080p→720p。
        if work_w >= 3840:
            width, height = 2560, 1440
        elif work_w >= 2560:
            width, height = 1920, 1080
        elif work_w >= 1920:
            width, height = 1280, 720
        elif work_w >= 1280:
            width, height = 1280, 720
        else:
            width, height = round(work_w * 0.90), round(work_h * 0.90)
        width = max(640, min(width, work_w - 24))
        height = max(480, min(height, work_h - 24))
        x = rect.left + (work_w - width) // 2
        y = rect.top + (work_h - height) // 2
        return width, height, x, y
    except (AttributeError, OSError, ValueError):
        return None


def _dpi_scale() -> float:
    """系统缩放比例（125% → 1.25）。Chrome 命令行的窗口坐标/尺寸按 DIP 解释。"""
    try:
        import ctypes

        return max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)
    except (AttributeError, OSError):
        return 1.0


def _browser_profile() -> str:
    """独立浏览器配置目录。

    复用用户日常 Chrome 时，窗口参数会发给已有进程并被忽略，窗口先出现在
    默认位置再被移动。独立配置让 Chrome 每次按参数直接开窗，不经过二次挪动。
    """
    path = Path(tempfile.gettempdir()) / "SubtitleStudio" / "chrome-profile"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def open_browser(url: str) -> None:
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ]
    geometry = window_geometry()
    if geometry:
        # Chrome 把 --window-size/--window-position 当作 DIP，会再乘系统缩放；
        # 先除以缩放比例，首帧即落在物理像素的目标位置，无需开窗后再移动。
        scale = _dpi_scale()
        requested = tuple(round(v / scale) for v in geometry)
        window_args = [f"--window-size={requested[0]},{requested[1]}",
                       f"--window-position={requested[2]},{requested[3]}"]
    else:
        window_args = ["--window-size=1680,1000"]
    profile = _browser_profile()
    for exe in candidates:
        if os.path.isfile(exe):
            try:
                subprocess.Popen(
                    [exe, f"--app={url}", f"--user-data-dir={profile}",
                     "--no-first-run", "--no-default-browser-check",
                     "--disable-background-mode", *window_args],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                if os.name == "nt":
                    from core.window_icon import apply_browser_icon
                    threading.Thread(target=apply_browser_icon,
                                     args=(FRONTEND_DIR / "assets" / "studio-film-taskbar.ico",
                                           FRONTEND_DIR.parent / ("run.bat" if (FRONTEND_DIR.parent / "run.bat").is_file() else "main.py")),
                                     daemon=True, name="window-icon").start()
                return
            except OSError:
                continue
    webbrowser.open(url)


def write_run_state(url: str) -> None:
    """记录当前实例地址，启动器优先读它，避免逐端口探测拖慢启动。"""
    try:
        RUN_STATE.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(RUN_STATE, {"url": url, "pid": os.getpid(), "started": STARTED_AT})
    except OSError:
        pass


def clear_run_state() -> None:
    try:
        state = json.loads(RUN_STATE.read_text(encoding="utf-8"))
        if int(state.get("pid", 0)) == os.getpid():
            RUN_STATE.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        pass


class StudioHTTPServer(ThreadingHTTPServer):
    """Windows 上 SO_REUSEADDR 允许第二个进程绑定同一端口，重复启动会悄悄堆在一起；
    禁用地址复用后，端口被占用时会退回下一个端口。"""
    allow_reuse_address = os.name != "nt"


def main() -> None:
    parser = argparse.ArgumentParser(description="字幕工作室后端")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--open-url", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.open_url:
        open_browser(args.open_url)
        return

    server = None
    for candidate in range(args.port, args.port + 30):
        try:
            server = StudioHTTPServer(("127.0.0.1", candidate), Handler)
            break
        except OSError:
            continue
    if server is None:
        raise RuntimeError("找不到可用端口")
    port = server.server_port
    server.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    write_run_state(url)
    models = list_models()
    available = [f"{m['name']}({m['size_gb']}G)" for m in models if m["available"]]
    log.info("字幕工作室 v%s  %s", VERSION, url)
    log.info("模型: %s", ', '.join(available) if available else '未发现，请放入 data/models')
    log.info("Ctrl+C 停止")

    ready_file = os.environ.get('SUBTITLESTUDIO_READY_FILE')
    if ready_file:
        atomic_json(Path(ready_file), {'url':url,'pid':os.getpid(),'data_dir':str(DATA_DIR)})

    if not args.no_browser:
        import threading
        threading.Timer(0.6, lambda: open_browser(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("已停止")
    except Exception as exc:
        log.exception("服务异常退出：%s", exc)
    finally:
        clear_run_state()
        server.server_close()


if __name__ == "__main__":
    main()
