"""固定模型目录、可续传下载和隔离运行环境；只有用户点击下载才联网。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import threading
import time
import uuid
from urllib.parse import quote
from urllib.request import Request, urlopen
import venv

from .paths import CONFIG_DIR, DATA_DIR, MODELS_DIR, ROOT_DIR
from .progress_console import spawn as spawn_progress_console
from .storage import atomic_json, locked_file
from .model_sources import resolve_sources

CATALOG = {
    "large-v3": {"label": "Whisper large-v3", "engine": "whisper", "repo": "Systran/faster-whisper-large-v3", "size_hint": "约 3 GB",
                 "description": "多语言优先"},
    "fireredasr2-aed": {"label": "FireRedASR2-AED", "engine": "firered", "repo": "FireRedTeam/FireRedASR2-AED", "size_hint": "约 4.5 GB",
                        "description": "中文优先"},
    "qwen3-asr-1.7b": {"label": "Qwen3-ASR-1.7B", "engine": "qwen", "repo": "Qwen/Qwen3-ASR-1.7B", "size_hint": "约 3.5 GB",
                       "description": "方言更强"},
}
AUXILIARY = {
    "qwen3-forcedaligner-0.6b": {"label": "Qwen3-ForcedAligner-0.6B", "engine": "qwen", "repo": "Qwen/Qwen3-ForcedAligner-0.6B", "size_hint": "约 1.2 GB", "description": "字幕对齐音轨"},
    "silero-vad": {"label": "Silero VAD", "engine": "builtin", "repo": "", "size_hint": "已内置", "description": "人声检测，内置"},
}
ALL_MODELS = {**CATALOG, **AUXILIARY}
DEFAULT_ENGINES = {
    "firered": {"packages": ["https://github.com/FireRedTeam/FireRedASR2S/archive/refs/heads/main.zip",
                             "kaldi_native_fbank"],
                "probe": "from fireredasr2s.fireredasr2 import FireRedAsr2, FireRedAsr2Config",
                "torch": True},
    "qwen": {"packages": ["qwen-asr==0.0.6", "gradio<6"],
             "probe": "from qwen_asr import Qwen3ASRModel, Qwen3ForcedAligner",
             "torch": True},
}
DEFAULT_TORCH = {"mode": "manual", "cuda_index": "https://download.pytorch.org/whl/cu126",
                 "packages": ["torch", "torchaudio"]}
DEFAULT_INDEXES = ('https://mirrors.aliyun.com/pypi/simple',
                   'https://pypi.tuna.tsinghua.edu.cn/simple',
                   'https://mirrors.ustc.edu.cn/pypi/simple',
                   'https://repo.huaweicloud.com/repository/pypi/simple',
                   'https://mirrors.cloud.tencent.com/pypi/simple',
                   'https://pypi.org/simple')
ENGINE_CONFIG_FILE = CONFIG_DIR / "engines.json"
FIRERED_CONFIG_FILE = CONFIG_DIR / 'firered.json'
FIRERED_FALLBACK_REVISION = '4e7d9aaf4482a47cec1724807026b9b151926eb5'
PIP_CACHE_DIR = DATA_DIR / "cache" / "pip"
_CONFIG_CACHE: dict = {"key": object(), "data": None}


def _valid_packages(value):
    return isinstance(value, list) and bool(value) and all(isinstance(item, str) and item.strip() for item in value)


def _valid_indexes(value):
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and item.startswith(("https://", "http://")) for item in value)


def engine_config():
    """读取 config/engines.json；文件缺失或字段非法时逐项回退到内置默认值。

    依赖装在 data/engines/<引擎> 的独立虚拟环境里，权重在 data/models，
    pip 下载缓存统一放 data/cache/pip，整个 data 目录可以直接备份或迁移。
    """
    try:
        mtime = ENGINE_CONFIG_FILE.stat().st_mtime_ns
    except OSError:
        mtime = None
    try:
        firered_text = FIRERED_CONFIG_FILE.read_text(encoding='utf-8')
    except (OSError,UnicodeError):
        firered_text = ''
    # 固定版本文件很小，按内容失效，避免 Windows 快速写入时复用同一时间戳。
    key = (str(ENGINE_CONFIG_FILE), mtime, str(FIRERED_CONFIG_FILE), firered_text)
    cached = _CONFIG_CACHE
    if cached["data"] is not None and cached["key"] == key:
        return cached["data"]
    data = {"pip_cache": "data/cache/pip", "indexes": list(DEFAULT_INDEXES), "torch": dict(DEFAULT_TORCH),
            "engines": {name: dict(spec, packages=list(spec["packages"])) for name, spec in DEFAULT_ENGINES.items()}}
    if mtime is not None:
        try:
            raw = json.loads(ENGINE_CONFIG_FILE.read_text(encoding="utf-8"))
            if not isinstance(raw,dict):raise ValueError('引擎配置必须是对象')
            if _valid_indexes(raw.get("indexes")):
                data["indexes"] = list(raw["indexes"])
            if isinstance(raw.get("pip_cache"), str) and raw["pip_cache"].strip():
                data["pip_cache"] = raw["pip_cache"].strip()
            torch = raw.get("torch")
            if isinstance(torch, dict):
                if torch.get("mode") in ("auto", "cpu", "cuda", "manual"):
                    data["torch"]["mode"] = torch["mode"]
                if isinstance(torch.get("cuda_index"), str) and torch["cuda_index"].startswith(("https://", "http://")):
                    data["torch"]["cuda_index"] = torch["cuda_index"].strip()
                if _valid_packages(torch.get("packages")):
                    data["torch"]["packages"] = torch["packages"]
            configured_engines = raw.get('engines')
            for name, spec in (configured_engines if isinstance(configured_engines,dict) else {}).items():
                if name not in DEFAULT_ENGINES or not isinstance(spec, dict):
                    continue
                merged = dict(data["engines"][name])
                if _valid_packages(spec.get("packages")):
                    merged["packages"] = spec["packages"]
                if isinstance(spec.get("probe"), str) and spec["probe"].strip():
                    merged["probe"] = spec["probe"]
                if _valid_indexes(spec.get("indexes")):
                    merged["indexes"] = spec["indexes"]
                if isinstance(spec.get("torch"), bool):
                    merged["torch"] = spec["torch"]
                data["engines"][name] = merged
        except (OSError, ValueError, TypeError):
            pass
    # 与完整安装包使用同一提交，防止在线准备时悄悄安装 main 分支的新代码。
    revision = FIRERED_FALLBACK_REVISION
    try:
        pinned = json.loads(firered_text).get('revision')
        if isinstance(pinned,str) and re.fullmatch(r'[0-9a-f]{40}',pinned):revision=pinned
    except (OSError,ValueError,AttributeError):
        pass
    data['engines']['firered']['packages'] = [
        f'https://github.com/FireRedTeam/FireRedASR2S/archive/{revision}.zip'
        if package == DEFAULT_ENGINES['firered']['packages'][0] else package
        for package in data['engines']['firered']['packages']]
    _CONFIG_CACHE.update(key=key, data=data)
    return data


def engine_spec(engine):
    return engine_config()["engines"][engine]


def pip_cache_directory():
    path = Path(engine_config()["pip_cache"])
    if not path.is_absolute():
        path = ROOT_DIR / path
    path.mkdir(parents=True, exist_ok=True)
    return path


RUNTIME_PACKAGES = {name: spec["packages"] for name, spec in engine_config()["engines"].items()}
RUNTIME_INDEXES = tuple(engine_config()["indexes"])
ENGINE_DIR = DATA_DIR / "engines"
MODEL_FOLDER_ALIASES = {
    'large-v3': ('Whisper large-v3', 'whisper-large-v3', 'faster-whisper-large-v3'),
    'qwen3-forcedaligner-0.6b': ('ForcedAligner-0.6b', 'forcedaligner-0.6b', 'Qwen3-ForcedAligner-0.6B'),
}
ACTIVITY_FILE = DATA_DIR / "run" / "activity.json"
ACTIVITY_FINAL = ("done", "error", "cancelled")
ENGINE_TITLES = {"qwen": "Qwen 运行环境", "firered": "FireRed 运行环境"}
_ACTIVITY_LOCK = threading.Lock()
_activity_written = 0.0


WEIGHT_FILES = ('model.bin', 'model.safetensors', 'model.safetensors.index.json', 'model.pth.tar', 'model.pt')
LOCATION_FILE = '.locations.json'


def _has_weights(directory):
    return directory.is_dir() and any((directory / weight).is_file() for weight in WEIGHT_FILES)


def _normalized(value):
    return re.sub(r'[^0-9a-z一-鿿]+', '', value.casefold())


def _folder_tokens(name):
    values = (name, *MODEL_FOLDER_ALIASES.get(name, ()))
    return [token for token in (_normalized(value) for value in values) if len(token) >= 4]


def _manual_location(name):
    """用户在界面上手动指定的模型目录，优先于默认命名。"""
    try:
        data = json.loads((MODELS_DIR / LOCATION_FILE).read_text(encoding='utf-8'))
        value = data.get(name)
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    directory = Path(value)
    return directory if directory.is_dir() else None


def _discovered_folder(name):
    """目录名和模型名不一致时按名称包含关系认领；匹配最长的模型优先。"""
    tokens = {key: _folder_tokens(key) for key in ALL_MODELS}
    if not tokens.get(name):
        return None
    try:
        folders = [d for d in MODELS_DIR.iterdir() if d.is_dir() and not d.name.startswith('.')]
    except OSError:
        return None
    found = None
    for directory in folders:
        if not _has_weights(directory):
            continue
        label = _normalized(directory.name)
        owner, length = None, 0
        for key, values in tokens.items():
            match = max((len(token) for token in values if token in label), default=0)
            if match > length:
                owner, length = key, match
        if owner != name or not length:
            continue
        try:
            mtime = directory.stat().st_mtime_ns
        except OSError:
            mtime = 0
        if found is None or mtime > found[0]:
            found = (mtime, directory)
    return found[1] if found else None


def model_directory(name):
    manual = _manual_location(name)
    if manual is not None:
        return manual
    for folder in (name, *MODEL_FOLDER_ALIASES.get(name, ())):
        directory = MODELS_DIR / folder
        if _has_weights(directory):
            return directory
    discovered = _discovered_folder(name)
    if discovered is not None:
        return discovered
    return MODELS_DIR / name


def set_model_location(name, directory):
    """记录用户手动选择的模型目录；directory 为空表示恢复默认。"""
    spec = ALL_MODELS.get(name)
    if not spec or spec['engine'] == 'builtin':
        raise ValueError('该模型不需要指定目录')
    state = {}
    try:
        raw = json.loads((MODELS_DIR / LOCATION_FILE).read_text(encoding='utf-8'))
        if isinstance(raw, dict):
            state = {key: value for key, value in raw.items() if isinstance(value, str)}
    except (OSError, ValueError):
        pass
    if not directory:
        state.pop(name, None)
    else:
        path = Path(str(directory)).expanduser()
        if not path.is_dir():
            raise ValueError('所选目录不存在')
        path = path.resolve()
        if not _has_weights(path):
            raise ValueError('所选目录里没有模型权重文件（model.bin / model.safetensors 等）')
        state[name] = str(path)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    atomic_json(MODELS_DIR / LOCATION_FILE, state)
    return model_directory(name) if name in state else None


def runtime_python(engine):
    from .bundled_runtime import python_for
    bundled = python_for(engine)
    if bundled:
        return bundled
    return ENGINE_DIR / engine / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


_GPU_DETECTION: dict = {}


def nvidia_gpu_present():
    """通过 nvidia-smi 判断是否有可用的 NVIDIA 显卡；结果只查一次。"""
    if "value" in _GPU_DETECTION:
        return _GPU_DETECTION["value"]
    present = False
    executable = shutil.which("nvidia-smi")
    if executable:
        try:
            result = subprocess.run([executable, "--query-gpu=name", "--format=csv,noheader"],
                                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=8,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            present = result.returncode == 0 and bool(result.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            present = False
    _GPU_DETECTION["value"] = present
    return present


def desired_torch():
    """manual：不自动安装 CUDA，等界面点“下载 GPU 环境”；cpu：强制 CPU。

    auto 在检测到 NVIDIA 显卡时随模型下载安装 CUDA 版；cuda 强制安装。
    安装失败会恢复 CPU 环境并在 ready.json 记 gpu_error，不会让引擎不可用。
    """
    mode = engine_config()["torch"]["mode"]
    if mode in ("cpu", "manual"):
        return "cpu"
    if mode == "cuda" or nvidia_gpu_present():
        return "cuda"
    return "cpu"


def ready_marker(engine):
    """读取引擎环境的 ready.json；不存在或损坏时返回空字典。"""
    from .bundled_runtime import cuda_available, manifest, python_for
    if python_for(engine):
        from .bundled_runtime import gpu_python
        return {'engine': engine, 'torch': 'cuda' if gpu_python() else manifest().get('torch_flavor', 'cpu'),
                'cuda_available': cuda_available(), 'bundled': True}
    try:
        data = json.loads((ENGINE_DIR / engine / "ready.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def engine_prepared(engine):
    """引擎的独立环境是否已经创建并完成依赖自检。"""
    from .bundled_runtime import python_for
    return bool(python_for(engine)) or runtime_python(engine).is_file() and (ENGINE_DIR / engine / "ready.json").is_file()


def engine_gpu_ready(engine):
    """环境里装的是否为 CUDA 版 PyTorch；当前驱动是否可用由运行时再判断。"""
    return ready_marker(engine).get("torch") == "cuda"


def runtime_ready(engine):
    from .bundled_runtime import manifest, python_for
    if engine in manifest().get('engines', []):
        return bool(python_for(engine))
    if engine in ("whisper", "builtin"):
        return True
    location = ENGINE_DIR / engine
    from .bundled_runtime import stale_venv_home
    if stale_venv_home(location):
        return False
    if not (runtime_python(engine).is_file() and (location / "ready.json").is_file()):
        return False
    marker = ready_marker(engine)
    if marker.get('engine') != engine:
        return False
    # 要求 CUDA 版 PyTorch 时，旧的 CPU 环境视为未就绪，下次下载会重装 torch。
    # 但已经尝试过 GPU 并回退 CPU 的环境必须算就绪，否则每次启动都会重下 2.9 GB。
    if engine_config()["engines"].get(engine, {}).get("torch") and desired_torch() == "cuda":
        return marker.get("torch") == "cuda" or bool(marker.get("gpu_error"))
    return True


def gpu_status():
    """给界面用的 GPU 环境状态；不触发任何下载或安装。"""
    torch = engine_config()["torch"]
    engines = []
    for name, spec in engine_config()["engines"].items():
        if not spec.get("torch"):
            continue
        prepared = engine_prepared(name)
        marker = ready_marker(name) if prepared else {}
        engines.append({
            "engine": name,
            "prepared": prepared,
            "torch": marker.get("torch", "cpu") if prepared else "",
            "cuda_available": marker.get("cuda_available") if prepared else None,
            "error": str(marker.get("gpu_error") or "") if prepared else "",
        })
    installed = [item for item in engines if item["prepared"]]
    # 有失败/回退记录时优先显示 failed：CUDA torch 可能装上了但驱动不可用，
    # 不能让界面误报“GPU 已就绪”。
    if any(item["error"] for item in installed):
        state = "failed"
    elif installed and all(item["torch"] == "cuda" for item in installed):
        state = "cuda"
    elif any(item["torch"] == "cuda" for item in installed):
        state = "partial"
    elif installed:
        state = "cpu"
    else:
        state = "missing"
    return {
        "mode": torch["mode"],
        "nvidia": nvidia_gpu_present(),
        "state": state,
        "downloadable": bool(installed),
        "size_hint": "约 2.9 GB",
        "error": next((item["error"] for item in installed if item["error"]), ""),
        "engines": engines,
    }


def _torch_engines():
    """当前已建好环境、可以补装 CUDA 的引擎；没有环境时不替用户凭空创建。"""
    return [name for name, spec in engine_config()["engines"].items()
            if spec.get("torch") and engine_prepared(name)]


WHISPER_FILES = ("model.bin", "config.json", "tokenizer.json")


def whisper_weights_present(directory):
    """CTranslate2 模型缺 config/tokenizer 时无法加载，不能只看 model.bin。"""
    return all((directory / name).is_file() for name in WHISPER_FILES)


def weights_present(name):
    directory = model_directory(name)
    spec = ALL_MODELS.get(name)
    if not spec:
        return False
    if spec["engine"] == "builtin":
        from importlib.util import find_spec
        package = find_spec("faster_whisper")
        return bool(package and package.origin and (Path(package.origin).parent / "assets" / "silero_vad_v6.onnx").is_file())
    if spec["engine"] == "whisper":
        return whisper_weights_present(directory)
    manifest = directory / ".studio-model.json"
    if not manifest.exists() and spec['engine'] == 'qwen':
        return all((directory / filename).is_file() for filename in ('config.json', 'model.safetensors', 'tokenizer_config.json', 'vocab.json', 'merges.txt', 'preprocessor_config.json'))
    try:
        files = json.loads(manifest.read_text(encoding="utf-8"))["files"]
        return bool(files) and all((directory / f["name"]).is_file() and (directory / f["name"]).stat().st_size == f["size"] for f in files)
    except (OSError, ValueError, KeyError, TypeError):
        return False


NAGISA_MARKER = '_subtitle_studio_ascii_model'


def patch_nagisa_model_path(engine):
    """nagisa 用 DyNet 的窄字符接口打开模型；项目路径含中文时 Windows 上必然失败。

    纯英文路径不动。中文路径下把 nagisa_v001.model 复制到纯英文目录再改写路径，
    只改引擎 venv 里的 tagger.py，重装依赖后会重新应用。
    """
    if os.name != 'nt':
        return
    package = ENGINE_DIR / engine / 'Lib' / 'site-packages' / 'nagisa'
    tagger = package / 'tagger.py'
    model = package / 'data' / 'nagisa_v001.model'
    if not tagger.is_file() or not model.is_file() or str(model).isascii():
        return
    try:
        source = tagger.read_text(encoding='utf-8')
    except OSError:
        return
    if NAGISA_MARKER in source:
        return
    replacement = """base = os.path.dirname(os.path.abspath(__file__))
# _subtitle_studio_ascii_model
_model_path = base + '/data/nagisa_v001.model'
if os.name == 'nt' and not _model_path.isascii():
    import shutil, tempfile
    _roots = [os.environ.get('PUBLIC'), os.environ.get('ProgramData'), tempfile.gettempdir()]
    _folder = next((os.path.join(root, 'SubtitleStudio', 'nagisa')
                    for root in _roots if root and root.isascii()), '')
    if _folder:
        os.makedirs(_folder, exist_ok=True)
        _copy = os.path.join(_folder, 'nagisa_v001.model')
        if not os.path.isfile(_copy) or os.path.getsize(_copy) != os.path.getsize(_model_path):
            shutil.copyfile(_model_path, _copy)
        _model_path = _copy"""
    source = source.replace("base = os.path.dirname(os.path.abspath(__file__))", replacement, 1)
    source = source.replace("params = base + '/data/nagisa_v001.model'", "params = _model_path", 1)
    if NAGISA_MARKER not in source or 'params = _model_path' not in source:
        return
    try:
        tagger.write_text(source, encoding='utf-8')
    except OSError:
        pass


class DownloadCancelled(Exception):
    pass


class ModelDownloads:
    def __init__(self, on_progress=None, cancel_event=None):
        self.lock = threading.RLock()
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self.on_progress = on_progress
        self.status = {"status": "idle", "progress": 0, "message": "等待任务", "model": ""}
        self.task_id = ""
        self.kind = ""
        self.title = ""
        self.started_at = 0.0

    def get(self):
        with self.lock:
            return dict(self.status)

    def update(self, **changes):
        with self.lock:
            if changes.get('status') in ('done', 'error', 'cancelled'):
                changes['finished'] = time.time()
                changes['elapsed'] = max(0, changes['finished'] - self.started_at) if self.started_at else 0
            self.status.update(changes)
        if self.on_progress and changes.get('message'):
            self.on_progress(.02, changes['message'])
        self._publish()

    def _begin_activity(self, kind, title, open_window=True):
        """给这次操作一个任务号，写入进度文件；需要时打开可见的进度窗口。"""
        with self.lock:
            self.task_id = uuid.uuid4().hex
            self.kind = kind
            self.title = title
            self.started_at = time.time()
            self._publish(force=True)
        if open_window:
            try:
                spawn_progress_console(root=ROOT_DIR, parent_pid=os.getpid())
            except Exception:
                pass

    def _publish(self, force=False):
        """把状态写到 data/run/activity.json，供进度窗口轮询；高频更新做节流。"""
        global _activity_written
        with self.lock:
            if not self.task_id:
                return
            state = self.status["status"]
            snapshot = {
                "task": self.task_id,
                "kind": self.kind,
                "title": self.title,
                "status": state,
                "progress": float(self.status.get("progress") or 0),
                "message": str(self.status.get("message") or ""),
                "downloaded": int(self.status.get("downloaded") or 0),
                "total": int(self.status.get("total") or 0),
                "speed_bps": float(self.status.get("speed_bps") or 0),
                "source": str(self.status.get("source") or ""),
                "error": str(self.status.get("error") or ""),
                "started_at": self.started_at,
                "updated_at": time.time(),
            }
        now = time.monotonic()
        with _ACTIVITY_LOCK:
            if not force and state not in ACTIVITY_FINAL and now - _activity_written < 0.5:
                return
            _activity_written = now
        try:
            atomic_json(ACTIVITY_FILE, snapshot)
        except OSError:
            pass

    def start(self, name):
        if not isinstance(name, str) or name not in ALL_MODELS or ALL_MODELS[name]["engine"] == "builtin":
            raise ValueError("未知模型，不能下载任意外部地址")
        with self.lock:
            if self.status["status"] in ("downloading", "preparing"):
                if self.status["model"] == name:
                    return self.get()
                raise ValueError("已有模型下载任务，请先完成或取消")
            self.cancel_event = threading.Event()
            local_weights = weights_present(name)
            self.status = {"status": "preparing" if local_weights else "downloading", "model": name, "progress": 0,
                           "message": "权重已存在，正在初始化运行环境…" if local_weights else "正在检测国内模型源…",
                           "downloaded": 0, "total": 0, "error": "", "source": "", "speed_bps": 0}
            self._begin_activity("download", ALL_MODELS[name]["label"])
            threading.Thread(target=self._run, args=(name,), daemon=True, name="model-download").start()
            return self.get()

    def cancel(self):
        self.cancel_event.set()
        return self.get()

    def check(self):
        if self.cancel_event.is_set():
            raise DownloadCancelled()

    def _sha256(self, path):
        hasher = hashlib.sha256()
        with path.open('rb') as stream:
            while block := stream.read(1024 * 1024):
                self.check()
                hasher.update(block)
        return hasher.hexdigest()

    @staticmethod
    def _request(url, headers=None, timeout=12):
        return urlopen(Request(url, headers={"User-Agent": "SubtitleStudio/2.1", **(headers or {})}), timeout=timeout)

    def _files(self, name):
        return resolve_sources(name, ALL_MODELS[name]['repo'], self._request, self.check, self.update)

    def _download_file(self, name, revision, item, completed, total):
        directory = model_directory(name)
        target = directory.joinpath(*PurePosixPath(item["name"]).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        sources = item.get('urls') or [{'url':f"https://hf-mirror.com/{ALL_MODELS[name]['repo']}/resolve/{revision}/{quote(item['name'], safe='/')}", 'label':'HF 国内镜像'}]
        # 两个后端可能同时下载同一文件；锁内重新检查完整文件和断点，避免互相截断。
        with locked_file(target.with_name(target.name + '.download.lock'), timeout=1800, cancelled=self.check):
            self._download_locked(name, item, target, completed, total, sources)

    def _download_locked(self, name, item, target, completed, total, sources):
        expected = item["size"]
        if target.exists() and target.stat().st_size == expected:
            if not item.get('sha256') or self._sha256(target) == item['sha256']:
                return
        partial = target.with_name(target.name + ".part")
        offset = partial.stat().st_size if partial.exists() else 0
        if expected and offset == expected:
            digest = item.get("sha256")
            if not digest or self._sha256(partial) == digest:
                os.replace(partial, target)
                return
            partial.unlink(missing_ok=True)
        elif expected and offset > expected:
            partial.unlink(missing_ok=True)
        for attempt in range(3):
            self.check()
            try:
                source = sources[attempt % len(sources)]
                self.update(source=source['label'], speed_bps=0,
                            message=('切换备用源：' if attempt else '下载源：') + source['label'])
                offset = partial.stat().st_size if partial.exists() else 0
                headers = {"Range": f"bytes={offset}-"} if offset else {}
                started, initial_offset = time.monotonic(), offset
                with self._request(source['url'], headers) as response:
                    if offset and response.status == 206:
                        content_range = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+|\*)',response.headers.get('Content-Range',''))
                        if not content_range or int(content_range[1]) != offset or (expected and content_range[3] != str(expected)):
                            raise RuntimeError('续传区间不一致，保留片段并尝试备用源')
                    if response.status != 206:
                        offset = initial_offset = 0
                    with partial.open("ab" if offset else "wb") as stream:
                        while True:
                            self.check()
                            data = response.read(1024 * 1024)
                            if not data:
                                break
                            stream.write(data)
                            offset += len(data)
                            self.update(downloaded=completed + offset, total=total,
                                        progress=min(.88, (completed + offset) / max(1, total) * .88),
                                        speed_bps=(offset-initial_offset)/max(.001,time.monotonic()-started),
                                        message=source['label'] + " · " + ALL_MODELS[name]["label"] + " · " + item["name"])
                if expected and offset != expected:
                    raise RuntimeError("文件长度不完整，保留下载片段以便重试")
                digest = item.get("sha256")
                if digest:
                    if self._sha256(partial) != digest:
                        partial.unlink(missing_ok=True)
                        raise RuntimeError("模型文件校验失败，已移除损坏片段")
                os.replace(partial, target)
                return
            except DownloadCancelled:
                raise
            except Exception:
                if attempt == 2:
                    raise
                self.cancel_event.wait(1 + attempt)

    def _install(self, command, output, environment):
        """运行一次 pip 安装并等待结束；取消时终止子进程。"""
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                   env=environment,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            while process.poll() is None:
                self.check()
                self.cancel_event.wait(.5)
            return process.returncode
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)

    def _install_torch(self, engine, torch, environment, output, cuda):
        """安装 CUDA 或 CPU 版 torch/torchaudio；强制重装，避免已有 CPU 版被判为已满足。"""
        packages = torch["packages"]
        if not packages:
            return False
        indexes = [torch["cuda_index"]] if cuda else list(engine_config()["indexes"])
        for index in indexes:
            self.check()
            label = 'CUDA 版' if cuda else 'CPU 版'
            output.write('\n'+label+' PyTorch：'+index+'\n'); output.flush()
            command = [str(runtime_python(engine)), '-m', 'pip', 'install', '--disable-pip-version-check',
                       '--progress-bar', 'on', '--index-url', index, '--timeout', '60',
                       '--retries', '2', '--no-input', '--upgrade', '--force-reinstall', *packages]
            if self._install(command, output, environment) == 0:
                return True
        return False

    def _torch_state(self, engine, environment):
        """返回 True（CUDA 可用）/ False（可导入但不可用）/ None（torch 导入失败）。"""
        code = "import torch; print('cuda' if torch.cuda.is_available() else 'cpu')"
        try:
            result = subprocess.run([str(runtime_python(engine)), '-c', code],
                                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90, env=environment,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode:
            return None
        text = str(getattr(result, 'stdout', '') or '').strip().lower()
        if text == 'cuda':
            return True
        if text == 'cpu':
            return False
        return None

    def _probe_engine(self, engine, environment, output=None):
        """在目标环境里 import 引擎入口；失败时把输出留在安装日志里。"""
        try:
            probe = subprocess.run([str(runtime_python(engine)), '-c', engine_spec(engine)["probe"]],
                                   capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90, env=environment,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except (OSError, subprocess.SubprocessError) as error:
            if output is not None:
                output.write('\n引擎自检失败：'+str(error)+'\n'); output.flush()
            return False
        if output is not None:
            text = str(getattr(probe, 'stdout', '') or '') + str(getattr(probe, 'stderr', '') or '')
            if text:
                output.write(text); output.flush()
        return probe.returncode == 0

    def _mark_torch(self, engine, flavor, cuda_available=None, gpu_error=''):
        """只更新 ready.json 的 GPU 字段，保留原 packages/indexes/prepared。"""
        path = ENGINE_DIR / engine / "ready.json"
        marker = ready_marker(engine)
        marker.update({"torch": flavor, "cuda_available": cuda_available})
        if gpu_error:
            marker["gpu_error"] = gpu_error
        else:
            marker.pop("gpu_error", None)
        atomic_json(path, marker)

    def _prepare(self, engine):
        from .bundled_runtime import manifest
        if manifest() and engine not in ('builtin', 'whisper'):
            if not runtime_ready(engine):
                raise RuntimeError('内置运行库不完整，请重新安装完整程序包；安装版不会在线安装依赖')
            return
        if runtime_ready(engine):
            return
        with self.lock:
            fresh = not self.task_id
        if fresh:
            # 直接调用时不弹窗（例如下载流程已经弹过窗）；ensure_runtime 会先弹窗。
            self._begin_activity("env", ENGINE_TITLES.get(engine, engine + " 运行环境"),
                                 open_window=False)
        self.update(status="preparing", progress=.9, message="模型已下载，正在准备独立识别环境…")
        location = ENGINE_DIR / engine
        from .bundled_runtime import stale_venv_home
        relocated = stale_venv_home(location)
        venv.EnvBuilder(with_pip=True).create(location)
        self.check()
        spec = engine_spec(engine)
        packages = spec["packages"]
        indexes = spec.get("indexes") or engine_config()["indexes"]
        environment = {**os.environ, 'PYTHONUTF8': '1',
                       'PYTHONUNBUFFERED': '1',
                       'PIP_CACHE_DIR': str(pip_cache_directory()),
                       'PIP_DISABLE_PIP_VERSION_CHECK': '1'}
        if relocated and self._probe_engine(engine, environment):
            available = self._torch_state(engine, environment)
            atomic_json(location / 'ready.json', {'engine': engine, 'packages': packages,
                        'torch': 'cuda' if available else 'cpu', 'cuda_available': available, 'relocated': True,
                        'prepared': time.time()})
            return
        log = location / "install.log"
        torch = engine_config()["torch"]
        torch_flavor, cuda_available, gpu_error = "cpu", None, ""
        with log.open('a',encoding='utf-8') as output:
            if spec.get("torch") and desired_torch() == "cuda":
                output.write('\nCUDA 版 PyTorch：'+torch["cuda_index"]+'\n');output.flush()
                self.update(message='安装 CUDA 版 PyTorch（约 2.9 GB，首次较慢）…')
                installed = self._install_torch(engine, torch, environment, output, cuda=True)
                available = self._torch_state(engine, environment) if installed else None
                if available is not None:
                    torch_flavor, cuda_available = "cuda", available
                    if not available:
                        gpu_error = 'CUDA 版 PyTorch 已安装，但当前显卡/驱动不可用；识别时会自动使用 CPU'
                        self.update(message='CUDA 版 PyTorch 已安装，但当前显卡不可用，稍后自动回退 CPU…')
                else:
                    # 下载或导入失败时恢复 CPU 环境，不能让引擎因此不可用。
                    gpu_error = 'CUDA 版 PyTorch 安装失败，已改用 CPU 环境'
                    self.update(message='GPU 环境安装失败，正在改用 CPU 环境…')
                    output.write('\nGPU 安装失败，回退 CPU\n'); output.flush()
                    if not self._install_torch(engine, torch, environment, output, cuda=False):
                        raise RuntimeError('GPU 版 PyTorch 安装失败，CPU 版 PyTorch 也未能安装。'
                                           '可把 config/engines.json 的 torch.mode 改为 manual 或 cpu 后重试。'
                                           '日志：'+str(log))
                    torch_flavor, cuda_available = "cpu", None
            succeeded=False
            for index in indexes:
                self.check()
                output.write('\n安装源：'+index+'\n');output.flush()
                self.update(message='安装 '+engine+' 运行环境：'+index)
                command=[str(runtime_python(engine)),'-m','pip','install','--disable-pip-version-check',
                         '--progress-bar','on','--index-url',index,'--timeout','15','--retries','1',
                         '--no-input',*packages]
                if self._install(command, output, environment) == 0:
                    succeeded=True;break
            if not succeeded:
                raise RuntimeError('模型已保存，多个安装源均未能完成环境安装。日志：'+str(log))
            patch_nagisa_model_path(engine)
            probe_ok = self._probe_engine(engine, environment, output)
            if not probe_ok and torch_flavor == "cuda":
                # CUDA 版能导入但引擎加载失败时，退回 CPU 再自检一次。
                output.write('\nCUDA 环境无法加载引擎，回退 CPU 后重试\n'); output.flush()
                if self._install_torch(engine, torch, environment, output, cuda=False):
                    torch_flavor, cuda_available = "cpu", None
                    gpu_error = 'CUDA 环境无法加载引擎，已回退 CPU'
                    probe_ok = self._probe_engine(engine, environment, output)
            if not probe_ok:
                hint = ''
                if engine == 'qwen' and not str(MODELS_DIR).isascii():
                    hint = '项目路径含中文，nagisa 可能无法读取模型；可把项目移到纯英文路径重试。'
                raise RuntimeError('依赖已安装，但引擎无法加载，运行环境未标记就绪。'+hint+'日志：'+str(log))
        marker = {"engine": engine, "packages": packages, "indexes": list(indexes),
                  "torch": torch_flavor, "cuda_available": cuda_available, "prepared": time.time()}
        if gpu_error:
            marker["gpu_error"] = gpu_error
        atomic_json(location / "ready.json", marker)

    def start_gpu(self):
        from .bundled_runtime import manifest
        if manifest():
            with self.lock:
                if self.status['status'] in ('downloading', 'preparing'):
                    raise ValueError('请先完成或取消当前下载')
                self.cancel_event = threading.Event()
                self.started_at = time.time()
                self.status = {'status': 'preparing', 'model': 'gpu', 'progress': 0,
                               'message': '准备独立 GPU 环境，CPU 环境保留…', 'error': ''}
                threading.Thread(target=self._run_bundled_gpu, daemon=True, name='gpu-runtime').start()
                return self.get()
        """按需给已建好的引擎环境补装 CUDA 版 PyTorch；不在下载模型时自动触发。"""
        with self.lock:
            if self.status["status"] in ("downloading", "preparing"):
                if self.status.get("model") == "gpu":
                    return self.get()
                raise ValueError("已有模型下载任务，请先完成或取消")
            engines = _torch_engines()
            if not engines:
                raise ValueError("请先下载并配置任一 Qwen 或 FireRed 模型，再下载 GPU 环境")
            self.cancel_event = threading.Event()
            self.status = {"status": "preparing", "model": "gpu", "progress": 0,
                           "message": "准备安装 CUDA 版 PyTorch（约 2.9 GB，首次较慢）…",
                           "downloaded": 0, "total": 0, "error": "", "source": "", "speed_bps": 0}
            self._begin_activity("gpu", "GPU 环境")
            threading.Thread(target=self._run_gpu, args=(engines,), daemon=True,
                             name="gpu-download").start()
            return self.get()

    def _run_bundled_gpu(self):
        from .bundled_runtime import gpu_python, manifest
        location = ENGINE_DIR / 'gpu'
        try:
            if gpu_python():
                self.update(status='done', progress=1, message='GPU 运行库已配置，无需重复下载；驱动由电脑提供')
                return
            source = ROOT_DIR / 'runtime' / Path(manifest()['ml']).parent
            if location.exists():
                resolved = location.resolve()
                if location.is_symlink() or not resolved.is_relative_to(ENGINE_DIR.resolve()) or resolved == ENGINE_DIR.resolve():
                    raise ValueError('GPU 临时环境目录不合法，原目录保留')
                old_log = location / 'install.log'
                if old_log.is_file():
                    log_archive = ROOT_DIR / 'data/logs/gpu-install-previous.log'
                    log_archive.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(old_log, log_archive)
                shutil.rmtree(resolved)
            location.mkdir(parents=True, exist_ok=True)
            self.check()
            shutil.copytree(source, location, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            self.check()
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith('PIP_') and key not in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV')}
            environment.update(PYTHONUTF8='1', PYTHONUNBUFFERED='1', PYTHONNOUSERSITE='1',
                               PIP_CONFIG_FILE=os.devnull, PIP_CACHE_DIR=str(pip_cache_directory()))
            python = location / 'python.exe'
            command = [str(python), '-m', 'pip', 'install', '--no-input', '--no-deps',
                       '--disable-pip-version-check', '--progress-bar', 'on', '--timeout', '120', '--retries', '3',
                       '--index-url', 'https://download.pytorch.org/whl/cu126',
                       'torch==2.9.1+cu126', 'torchvision==0.24.1+cu126', 'torchaudio==2.9.1+cu126']
            self.update(status='preparing', progress=.1, message='下载并配置 GPU 运行库，约 2.6 GB；CPU 环境保留…')
            with (location / 'install.log').open('a', encoding='utf-8') as log:
                if self._install(command, log, environment):
                    raise RuntimeError('GPU 配置未完成，可重试；CPU 内置环境仍可使用。日志：' + str(location / 'install.log'))
            self.check()
            probe = subprocess.run([str(python), '-c',
                'import torch,torchvision,torchaudio; from qwen_asr import Qwen3ForcedAligner; print(int(torch.cuda.is_available()))'],
                env=environment, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if probe.returncode:
                raise RuntimeError('GPU 环境自检失败，CPU 环境保留：' + probe.stderr[-1200:])
            available = probe.stdout.strip() == '1'
            atomic_json(location / 'ready.json', {'torch': 'cuda', 'cuda_available': available,
                                                 'packages': ['torch==2.9.1+cu126'], 'prepared': time.time()})
            self.update(status='done', progress=1, message='GPU 环境已就绪' if available else
                        'GPU 运行库已安装，显卡驱动尚不可用；可继续使用 CPU')
        except DownloadCancelled:
            self.update(status='cancelled', message='GPU 配置已取消，CPU 环境保留')
        except Exception as error:
            self.update(status='error', error=str(error), message=str(error))

    def _run_gpu(self, engines):
        try:
            torch = engine_config()["torch"]
            environment = {**os.environ, 'PYTHONUTF8': '1',
                           'PYTHONUNBUFFERED': '1',
                           'PIP_CACHE_DIR': str(pip_cache_directory()),
                           'PIP_DISABLE_PIP_VERSION_CHECK': '1'}
            failed = []
            for engine in engines:
                self.check()
                log = ENGINE_DIR / engine / "install.log"
                with log.open('a', encoding='utf-8') as output:
                    output.write('\nGPU 环境：'+torch["cuda_index"]+'\n'); output.flush()
                    self.update(message='安装 '+engine+' 的 CUDA 版 PyTorch（约 2.9 GB，首次较慢）…')
                    installed = self._install_torch(engine, torch, environment, output, cuda=True)
                    available = self._torch_state(engine, environment) if installed else None
                    probe_ok = available is not None and self._probe_engine(engine, environment, output)
                    if probe_ok:
                        error = '' if available else 'CUDA 版 PyTorch 已安装，但当前显卡/驱动不可用；识别时会自动回退 CPU'
                        self._mark_torch(engine, 'cuda', bool(available), error)
                    else:
                        failed.append(engine)
                        self.update(message=engine+' 的 GPU 环境不可用，正在恢复 CPU 环境…')
                        output.write('\nGPU 环境不可用，恢复 CPU 版 PyTorch\n'); output.flush()
                        if not self._install_torch(engine, torch, environment, output, cuda=False):
                            raise RuntimeError('GPU 环境安装失败，CPU 版 PyTorch 也未能恢复。'
                                               '请删除 data/engines/'+engine+' 后重新下载模型。日志：'+str(log))
                        self._mark_torch(engine, 'cpu', None, 'GPU 环境安装失败，已恢复 CPU')
                self.check()
            if failed:
                self.update(status='error', progress=1,
                            message='GPU 环境未装好，已回退 CPU：'+'、'.join(failed)+'。可重试或继续用 CPU')
            else:
                self.update(status='done', progress=1,
                            message='GPU 环境已就绪，可在“设备”选择自动或 GPU (CUDA)')
        except DownloadCancelled:
            self.update(status="cancelled", message="已暂停 GPU 环境安装，已完成的引擎保留", speed_bps=0)
        except Exception as error:
            self.update(status="error", error=str(error), message="GPU 环境安装失败："+str(error), speed_bps=0)

    def _run(self, name):
        try:
            self.check()
            if weights_present(name):
                self.update(status='preparing', message='权重已存在，仅初始化运行环境…')
                self._prepare(ALL_MODELS[name]['engine'])
                self.check()
                self.update(status='done', progress=1, message=ALL_MODELS[name]['label'] + ' 已就绪', speed_bps=0)
                return
            revision, files = self._files(name)
            directory = model_directory(name)
            directory.mkdir(parents=True, exist_ok=True)
            marker = directory / '.studio-downloading'
            marker.touch()
            total = sum(item["size"] for item in files)
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            def _on_disk(item):
                target = directory / item["name"]
                if target.exists():
                    return target.stat().st_size
                partial = target.with_name(target.name + ".part")
                return partial.stat().st_size if partial.exists() else 0

            remaining = sum(max(0, item["size"] - _on_disk(item)) for item in files)
            if total and shutil.disk_usage(MODELS_DIR).free < remaining + 512 * 1024**2:
                raise RuntimeError("模型目录磁盘空间不足，请释放空间后重试")
            completed = 0
            for item in files:
                self.check()
                self._download_file(name, revision, item, completed, total)
                completed += item["size"]
            for item in files:
                item["size"] = (directory / item["name"]).stat().st_size
            atomic_json(directory / ".studio-model.json", {"repo": ALL_MODELS[name]["repo"], "revision": revision,
                        "source":self.get().get('source',''), "files": files})
            marker.unlink(missing_ok=True)
            self.check()
            self._prepare(ALL_MODELS[name]["engine"])
            self.check()
            self.update(status="done", progress=1, message=ALL_MODELS[name]["label"] + " 已就绪", downloaded=total, total=total, speed_bps=0)
        except DownloadCancelled:
            self.update(status="cancelled", message="下载已暂停，文件片段保留，点击下载可继续", speed_bps=0)
        except Exception as error:
            self.update(status="error", error=str(error), message="模型下载失败：" + str(error), speed_bps=0)


DOWNLOADS = ModelDownloads()


def ensure_runtime(engine, on_progress=None, cancel_event=None):
    if runtime_ready(engine):
        return
    downloads = ModelDownloads(on_progress, cancel_event)
    downloads._begin_activity("env", ENGINE_TITLES.get(engine, engine + " 运行环境"))
    try:
        downloads._prepare(engine)
    except DownloadCancelled:
        downloads.update(status="cancelled", message="运行环境准备已暂停")
        from .asr import Cancelled
        raise Cancelled() from None
    except Exception as error:
        downloads.update(status="error", message="运行环境准备失败：" + str(error),
                         error=str(error))
        raise
    downloads.update(status="done", progress=1,
                     message=ENGINE_TITLES.get(engine, engine + " 运行环境") + " 已就绪")


def list_auxiliary():
    result = []
    for name, spec in AUXILIARY.items():
        downloaded = weights_present(name)
        result.append({"name": name, **spec, "downloaded": downloaded,
                       "available": downloaded, "runtime_ready": runtime_ready(spec["engine"])})
    return result
