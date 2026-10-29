# -*- coding: utf-8 -*-
"""语音模型的本地状态与显式下载。

模型只在用户点击「下载」后联网；运行任务时绝不隐式下载。
"""
from __future__ import annotations

import fnmatch
import json
import os
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

# 必须在导入 huggingface_hub 之前设置镜像。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

from huggingface_hub import snapshot_download  # noqa: E402
from huggingface_hub.utils.tqdm import tqdm as HFTqdm  # noqa: E402

from .config import MODELS_DIR  # noqa: E402
from .pipeline.asr import (  # noqa: E402
    HF_REPOS,
    MODEL_DOWNLOAD_SIZES,
    has_local_model,
)

MODEL_CATALOG: dict[str, dict[str, str]] = {
    "small": {
        "label": "Small",
        "repo": HF_REPOS["small"],
        "size_label": MODEL_DOWNLOAD_SIZES["small"],
    },
    "medium": {
        "label": "Medium",
        "repo": HF_REPOS["medium"],
        "size_label": MODEL_DOWNLOAD_SIZES["medium"],
    },
    "large-v3": {
        "label": "Large v3",
        "repo": HF_REPOS["large-v3"],
        "size_label": MODEL_DOWNLOAD_SIZES["large-v3"],
    },
}

# faster-whisper 仓库里真正需要的文件。
ALLOW_PATTERNS = [
    "config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
    "preprocessor_config.json",
]

# 国内网络优先走 ModelScope；它的仓库与 Systran 官方文件一致，
# 且支持 Range 续传，不需要用户额外开代理。失败时再回退 hf-mirror。
MODELSCOPE_REVISION = "master"
MODELSCOPE_FILES_API = (
    "https://modelscope.cn/api/v1/models/{repo}/repo/files"
    "?Revision={revision}&Recursive=true"
)
MODELSCOPE_FILE_URL = (
    "https://modelscope.cn/models/{repo}/resolve/{revision}/{path}"
)
DOWNLOAD_USER_AGENT = "SubtitleStudio/0.1"
DOWNLOAD_CHUNK = 1024 * 1024
DOWNLOAD_TIMEOUT = 60
MODELSCOPE_API_TIMEOUT = 20


def _wanted_file(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in ALLOW_PATTERNS)


def _safe_relative_path(name: str) -> Path:
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(
        part in ("", ".", "..") for part in relative.parts
    ):
        raise RuntimeError("下载源返回了异常文件路径")
    return Path(*relative.parts)


def _local_failure(exc: BaseException) -> bool:
    """磁盘 / 权限问题换源也没用，直接报错。"""
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "no space",
            "disk full",
            "not enough space",
            "permission",
            "access is denied",
        )
    )


class ModelBusyError(RuntimeError):
    """同一时间只允许一个模型下载，避免带宽和磁盘争抢。"""


@dataclass
class DownloadState:
    model: str
    status: str = "downloading"
    progress: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    error: str = ""
    detail: str = ""
    updated_at: float = 0.0


def _friendly_error(exc: BaseException) -> str:
    text = str(exc).strip()
    lowered = text.lower()
    name = type(exc).__name__.lower()

    if "timeout" in lowered or "timed out" in lowered or "timeout" in name:
        return "连接模型下载源超时，请检查网络或代理后重试。"
    if (
        "connection" in lowered
        or "connect" in lowered
        or "network" in lowered
        or "network" in name
    ):
        return "无法连接模型下载源，请检查网络或代理后重试。"
    if "no space" in lowered or "disk full" in lowered or "not enough space" in lowered:
        return "磁盘空间不足，请清理 data/models 后重试。"
    if "permission" in lowered or "access is denied" in lowered:
        return "模型目录不可写，请检查 data/models 的权限。"
    if "404" in lowered or "not found" in lowered or "repositorynotfound" in name:
        return "下载源上暂时找不到这个模型，请稍后重试。"
    return "下载失败，请检查网络后重试。"


class ModelManager:
    def __init__(self, models_dir: Path = MODELS_DIR):
        self.models_dir = Path(models_dir)
        self._lock = threading.RLock()
        self._states: dict[str, DownloadState] = {}

    def _is_local(self, model: str) -> bool:
        return has_local_model(model, self.models_dir)

    def _active_model_locked(self) -> str | None:
        for key, state in self._states.items():
            if state.status == "downloading":
                return key
        return None

    def _update(self, model: str, **changes: Any) -> None:
        with self._lock:
            state = self._states.get(model)
            if state is None:
                return
            for key, value in changes.items():
                setattr(state, key, value)
            state.updated_at = time.time()

    def _set_progress(
        self,
        model: str,
        progress: float,
        downloaded_bytes: int,
        total_bytes: int,
    ) -> None:
        self._update(
            model,
            progress=max(0.0, min(float(progress), 0.995)),
            downloaded_bytes=max(0, int(downloaded_bytes)),
            total_bytes=max(0, int(total_bytes)),
        )

    def _entry(self, model: str) -> dict:
        spec = MODEL_CATALOG[model]
        local = self._is_local(model)
        state = self._states.get(model)

        if local:
            status = "ready"
            progress = 1.0
            error = ""
            downloaded_bytes = state.downloaded_bytes if state else 0
            total_bytes = state.total_bytes if state else 0
        elif state is not None:
            status = state.status
            progress = state.progress
            error = state.error
            downloaded_bytes = state.downloaded_bytes
            total_bytes = state.total_bytes
        else:
            status = "missing"
            progress = 0.0
            error = ""
            downloaded_bytes = 0
            total_bytes = 0

        return {
            "id": model,
            "label": spec["label"],
            "repo": spec["repo"],
            "size_label": spec["size_label"],
            "status": status,
            "progress": round(progress, 4),
            "downloaded_bytes": downloaded_bytes,
            "total_bytes": total_bytes,
            "error": error,
        }

    def snapshot(self) -> dict:
        with self._lock:
            active = self._active_model_locked()
            models = [self._entry(model) for model in MODEL_CATALOG]
        return {"models": models, "active": active}

    def start_download(self, model: str) -> dict:
        if model not in MODEL_CATALOG:
            raise KeyError(model)

        with self._lock:
            if self._is_local(model):
                return self._entry(model)

            active = self._active_model_locked()
            if active is not None and active != model:
                raise ModelBusyError(
                    f"{MODEL_CATALOG[active]['label']} 正在下载，请等待它完成后再下载其他模型。"
                )

            state = self._states.get(model)
            if state is not None and state.status == "downloading":
                return self._entry(model)

            state = DownloadState(
                model=model,
                status="downloading",
                updated_at=time.time(),
            )
            self._states[model] = state
            thread = threading.Thread(
                target=self._download,
                args=(model,),
                name=f"model-download-{model}",
                daemon=True,
            )
            thread.start()
            return self._entry(model)

    def _download(self, model: str) -> None:
        self._update(
            model,
            status="downloading",
            progress=0.0,
            error="",
            detail="",
            downloaded_bytes=0,
            total_bytes=0,
        )

        try:
            try:
                self._download_modelscope(model)
            except Exception as primary_exc:  # noqa: BLE001
                if _local_failure(primary_exc):
                    raise
                try:
                    print(
                        f"[model] 国内源失败，切换镜像：{primary_exc}",
                        flush=True,
                    )
                except Exception:
                    pass
                self._update(
                    model,
                    detail=f"国内源失败，正在切换镜像：{primary_exc}"[:1000],
                )
                self._set_progress(model, 0.0, 0, 0)
                try:
                    self._download_hf(model)
                except Exception as fallback_exc:  # noqa: BLE001
                    raise RuntimeError(
                        f"国内源：{primary_exc}\n镜像：{fallback_exc}"
                    ) from fallback_exc

            if not self._is_local(model):
                raise RuntimeError("下载完成但模型文件不完整")

            state = self._states.get(model)
            self._update(
                model,
                status="ready",
                progress=1.0,
                error="",
                detail="",
                total_bytes=state.total_bytes if state else 0,
            )
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._update(
                model,
                status="failed",
                error=_friendly_error(exc),
                detail=str(exc)[:1000],
            )

    def _download_hf(self, model: str) -> None:
        spec = MODEL_CATALOG[model]
        snapshot_download(
            repo_id=spec["repo"],
            cache_dir=str(self.models_dir),
            allow_patterns=ALLOW_PATTERNS,
            max_workers=4,
            tqdm_class=self._progress_bar_class(model),
        )

    def _download_modelscope(self, model: str) -> None:
        spec = MODEL_CATALOG[model]
        entries = self._modelscope_entries(spec["repo"])
        if not entries:
            raise RuntimeError("国内源没有返回模型文件")
        names = {name for name, _ in entries}
        required = {"model.bin", "config.json", "tokenizer.json"}
        if not required <= names:
            raise RuntimeError("国内源缺少模型文件（model.bin / config.json / tokenizer.json）")

        snapshot = (
            self.models_dir
            / ("models--" + spec["repo"].replace("/", "--"))
            / "snapshots"
            / f"modelscope-{MODELSCOPE_REVISION}"
        )
        snapshot.mkdir(parents=True, exist_ok=True)
        self._download_modelscope_entries(
            model, spec["repo"], snapshot, entries
        )

    def _modelscope_entries(self, repo: str) -> list[tuple[str, int]]:
        url = MODELSCOPE_FILES_API.format(
            repo=repo, revision=MODELSCOPE_REVISION
        )
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": DOWNLOAD_USER_AGENT,
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(
            request, timeout=MODELSCOPE_API_TIMEOUT
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))

        code = payload.get("Code", payload.get("code"))
        if str(code) not in {"200", "0"}:
            message = (
                payload.get("Message")
                or payload.get("message")
                or "未知错误"
            )
            raise RuntimeError(f"国内源文件列表读取失败：{message}")

        data = payload.get("Data") or payload.get("data") or {}
        files = data.get("Files") or data.get("files") or []
        entries: list[tuple[str, int]] = []
        for item in files:
            if str(item.get("Type") or "blob").lower() != "blob":
                continue
            name = str(item.get("Path") or "").strip()
            if not name or not _wanted_file(name):
                continue
            entries.append((name, max(0, int(item.get("Size") or 0))))
        return entries

    def _download_modelscope_entries(
        self,
        model: str,
        repo: str,
        snapshot: Path,
        entries: list[tuple[str, int]],
    ) -> None:
        total = sum(size for _, size in entries)
        completed = 0

        for name, size in entries:
            target = snapshot / _safe_relative_path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            part = target.with_name(target.name + ".part")

            if target.is_file():
                actual = target.stat().st_size
                if size <= 0 or actual == size:
                    completed += actual if size <= 0 else size
                    self._report_bytes(model, completed, total)
                    continue
                target.unlink()

            resumed = part.stat().st_size if part.is_file() else 0
            if resumed and size and resumed > size:
                part.unlink()
                resumed = 0
            if resumed and size and resumed == size:
                os.replace(part, target)
                completed += size
                self._report_bytes(model, completed, total)
                continue

            url = MODELSCOPE_FILE_URL.format(
                repo=repo,
                revision=MODELSCOPE_REVISION,
                path=quote(name, safe="/"),
            )
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": DOWNLOAD_USER_AGENT,
                    "Accept": "*/*",
                },
            )
            if resumed:
                request.add_header("Range", f"bytes={resumed}-")

            try:
                response = urllib.request.urlopen(
                    request, timeout=DOWNLOAD_TIMEOUT
                )
            except urllib.error.HTTPError as exc:
                if exc.code == 416 and size and resumed >= size:
                    os.replace(part, target)
                    completed += size
                    self._report_bytes(model, completed, total)
                    continue
                raise

            with response:
                status = int(getattr(response, "status", 200) or 200)
                content_range = response.headers.get("Content-Range", "")
                ranged = status == 206 or (
                    resumed > 0
                    and content_range.startswith(f"bytes {resumed}-")
                )
                start = resumed if ranged else 0
                mode = "ab" if start else "wb"
                with part.open(mode) as output:
                    written = start
                    while True:
                        chunk = response.read(DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        output.write(chunk)
                        written += len(chunk)
                        self._report_bytes(
                            model, completed + written, total
                        )

            actual = part.stat().st_size
            if size and actual != size:
                raise RuntimeError(
                    f"模型文件 {name} 下载不完整"
                    f"（{actual}/{size} 字节）"
                )
            os.replace(part, target)
            completed += actual if size <= 0 else size
            self._report_bytes(model, completed, total)

        model_path = snapshot / "model.bin"
        if not model_path.is_file() or model_path.stat().st_size <= 0:
            raise RuntimeError("模型文件 model.bin 为空")

    def _report_bytes(self, model: str, done: int, total: int) -> None:
        if total > 0:
            self._set_progress(model, done / total, done, total)

    def _progress_bar_class(self, model: str):
        manager = self

        class DownloadProgressBar(HFTqdm):
            """只接管 snapshot 的字节总和进度，并把控制台进度条静音。"""

            def __init__(self, *args, **kwargs):
                self._track = kwargs.get("name") == "huggingface_hub.snapshot_download"
                initial = kwargs.get("initial", 0) or 0
                self._seen = float(initial)
                super().__init__(*args, **kwargs)

            def update(self, n=1):
                increment = 1 if n is None else n
                if self._track:
                    self._seen += float(increment)
                    total = float(self.total or 0)
                    if total > 0:
                        manager._set_progress(
                            model,
                            self._seen / total,
                            int(self._seen),
                            int(total),
                        )
                try:
                    return super().update(n)
                except Exception:
                    return None

            def display(self, *args, **kwargs):
                return None

        return DownloadProgressBar


manager = ModelManager()
