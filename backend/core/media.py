"""媒体探测、波形峰值（PyAV，无需 ffmpeg 二进制）。"""
from __future__ import annotations

import hashlib
import json
import os
import heapq
import io
import logging
import tempfile
import threading
import time
from pathlib import Path

import av
import numpy as np

from .paths import RESULTS_DIR, DATA_DIR

CACHE_DIR = RESULTS_DIR
AUDIO_CACHE_DIR=DATA_DIR/'cache'/'audio'
AUDIO_CACHE_BYTES=1024**3
AUDIO_CACHE_ENTRIES=4
_AUDIO_LOCK=threading.RLock()
log=logging.getLogger('studio.media')

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".flv", ".ts", ".m4v", ".webm", ".wmv"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
MEDIA_EXT = VIDEO_EXT | AUDIO_EXT


class DecodeCancelled(Exception):
    pass


def remux_with_subtitles(source: str, subtitles: str, target: str) -> None:
    """保留所有原视频、音轨，附加当前 SRT 轨；交错写包，无需重编码。"""
    with av.open(source) as original, av.open(io.BytesIO(subtitles.encode('utf-8')), format='srt') as captions:
        with av.open(target, 'w', format='matroska') as output:
            output.metadata.update(original.metadata)
            streams=[s for s in original.streams if s.type in ('video','audio')]
            if not streams:
                raise ValueError('素材没有可导出的视频或音轨')
            mapping={s.index:output.add_stream_from_template(s) for s in streams}
            subtitle=output.add_stream_from_template(captions.streams[0])
            subtitle.metadata.update(language='zho',title='校对字幕')

            def packets(container, selected, destinations):
                for packet in container.demux(selected):
                    if packet.size == 0:
                        continue
                    packet.stream=destinations[packet.stream.index]
                    yield packet

            def timestamp(packet):
                return (packet.dts if packet.dts is not None else packet.pts or 0)*packet.time_base

            audio_video=packets(original,streams,mapping)
            text=packets(captions,list(captions.streams),{captions.streams[0].index:subtitle})
            for packet in heapq.merge(audio_video,text,key=timestamp):
                output.mux(packet)


def is_media(path: str) -> bool:
    return Path(path).suffix.lower() in MEDIA_EXT


def probe(path: str) -> dict:
    p = Path(path)
    info = {
        "path": str(p),
        "name": p.name,
        "size": p.stat().st_size if p.exists() else 0,
        "duration": 0.0,
        "width": 0,
        "height": 0,
        "fps": 0.0,
        "video_codec": "",
        "format": "",
        "audio_tracks": [],
        "error": None,
    }
    if not p.is_file():
        info["error"] = "文件不存在"
        return info
    try:
        with av.open(str(p)) as container:
            if container.duration:
                info["duration"] = round(float(container.duration) / av.time_base, 3)
            if container.format:
                info["format"] = container.format.name or ""
            for s in container.streams:
                ctx = s.codec_context
                if s.type == "video" and not info["width"]:
                    info["width"] = int(ctx.width or 0)
                    info["height"] = int(ctx.height or 0)
                    info["video_codec"] = ctx.name or ""
                    try:
                        info["fps"] = round(float(s.average_rate), 3) if s.average_rate else 0.0
                    except Exception:
                        pass
                elif s.type == "audio":
                    meta = s.metadata or {}
                    info["audio_tracks"].append({
                        "index": s.index,
                        "codec": ctx.name or "",
                        "channels": int(ctx.channels or 0),
                        "sample_rate": int(ctx.sample_rate or 0),
                        "language": meta.get("language", ""),
                        "title": meta.get("title", ""),
                    })
    except Exception as exc:
        info["error"] = f"无法读取媒体：{exc}"
    return info


def _cache_path(path: str, buckets: int) -> Path:
    p = Path(path)
    try:
        st = p.stat()
        key = f"{p.resolve()}|{st.st_mtime_ns}|{st.st_size}|{buckets}"
    except OSError:
        key = f"{path}|{buckets}"
    digest = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:20]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"wave_{digest}.json"


def waveform(path: str, buckets: int = 1200) -> dict:
    """分块聚合波形峰值，内存与媒体总时长无关；错误不缓存成静音。"""
    buckets = max(100, min(int(buckets or 1200), 4000))
    cache = _cache_path(path, buckets)
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    duration, sample_count = 0.0, 0
    peaks: list[float] = []
    current_peak = 0.0
    error = None
    try:
        with av.open(path) as container:
            if container.duration:
                duration = float(container.duration) / av.time_base
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                raise ValueError("媒体没有音轨")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=8000)
            step = max(1, int(np.ceil(duration * 8000 / buckets))) if duration else 80

            def collect(frames):
                nonlocal sample_count, step, current_peak, peaks
                for frame in frames:
                    data = frame.to_ndarray().reshape(-1)
                    offset = 0
                    while offset < data.size:
                        take = min(step - sample_count % step, data.size - offset)
                        block = data[offset:offset + take].astype(np.int32)
                        current_peak = max(current_peak, float(np.max(np.abs(block))) / 32768.0)
                        sample_count += take
                        offset += take
                        if sample_count % step == 0:
                            peaks.append(round(current_peak, 3))
                            current_peak = 0.0
                            if len(peaks) >= buckets * 2:
                                peaks = [max(peaks[i:i + 2]) for i in range(0, len(peaks), 2)]
                                step *= 2
            for frame in container.decode(stream):
                collect(resampler.resample(frame))
            collect(resampler.resample(None))
            if sample_count % step:
                peaks.append(round(current_peak, 3))
            if not duration:
                duration = sample_count / 8000.0
            while len(peaks) > buckets:
                peaks = [max(peaks[i:i + 2]) for i in range(0, len(peaks), 2)]
    except Exception as exc:
        peaks = []
        error = str(exc)
    result = {"duration": round(duration, 3), "peaks": peaks, "buckets": len(peaks), "error": error}
    if not error:
        try:
            cache.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return result


def waveform_region(path: str, start: float, end: float, buckets: int = 1200) -> dict:
    """定位并解码局部音频，避免长媒体放大后仍沿用粗粒度全片峰值。"""
    if not (0 <= start < end and end - start <= 120):
        raise ValueError("局部波形需为0–120秒内的有效区间")
    buckets = max(100, min(int(buckets), 4000))
    base = _cache_path(path, buckets)
    digest = hashlib.sha1(f"{base.stem}|{start:.6f}|{end:.6f}".encode()).hexdigest()[:20]
    cache = CACHE_DIR / f"wave_detail_{digest}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    peaks = np.zeros(buckets, dtype=np.float32)
    with av.open(path) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None or not stream.time_base:
            raise ValueError("音轨无法定位时间")
        duration = float(container.duration or 0) / av.time_base
        if duration:
            end = min(end, duration)
        if end <= start:
            raise ValueError("波形区间超出媒体时长")
        origin = float(stream.start_time or 0) * float(stream.time_base)
        container.seek(int((start + origin) / float(stream.time_base)), stream=stream, backward=True)
        resampler = av.AudioResampler(format="s16", layout="mono", rate=8000)
        past_end = False

        def collect(frames):
            nonlocal past_end
            for frame in frames:
                if frame.pts is None or not frame.time_base:
                    raise ValueError("该音轨缺少可定位时间戳")
                frame_start = float(frame.pts * frame.time_base) - origin
                samples = frame.to_ndarray().reshape(-1)
                positions = frame_start + np.arange(samples.size) / 8000.0
                mask = (positions >= start) & (positions < end)
                indexes = np.floor((positions[mask] - start) / (end - start) * buckets).astype(np.int32)
                values = np.abs(samples[mask].astype(np.int32)) / 32768.0
                np.maximum.at(peaks, np.clip(indexes, 0, buckets - 1), values)
                if frame_start + samples.size / 8000 >= end:
                    past_end = True
        for frame in container.decode(stream):
            collect(resampler.resample(frame))
            if past_end:
                break
        if not past_end:
            collect(resampler.resample(None))
    result = {"duration": round(duration, 3), "start": start, "end": end,
              "peaks": np.round(peaks, 3).tolist(), "buckets": buckets, "error": None}
    try:
        cache.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return result


def _decode_audio(path: str, sample_rate: int = 16000, cancel_event=None, start=None, end=None) -> np.ndarray:
    """解码第一条音轨为 16k 单声道 float32，供 faster-whisper 直接使用。

    流式写入预分配数组，避免 chunks 列表与 np.concatenate 的双份内存峰值。
    1 小时 16kHz 单声道约 230MB，原实现峰值接近两份；预分配后降至约 1.05 倍。
    """
    audio = np.empty(sample_rate, dtype=np.float32)
    offset = 0
    try:
        with av.open(path) as container:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                raise ValueError("该文件没有音轨")
            resampler = av.AudioResampler(format="flt", layout="mono", rate=sample_rate)
            origin = float(stream.start_time or 0) * float(stream.time_base or 0)
            container_duration = float(container.duration or 0) / av.time_base
            if start is not None:
                if end is None or not 0 <= start < end or not stream.time_base:
                    raise ValueError('重新识别的时间区间无效')
                container.seek(int((start+origin)/float(stream.time_base)),stream=stream,backward=True)
                span = min(end, container_duration or end) - start
            else:
                span = container_duration
            if span:
                capacity = int(span * sample_rate * 1.05) + sample_rate
                if capacity > audio.size:
                    audio = np.empty(capacity, dtype=np.float32)
            past_end = False
            def collect(frames):
                nonlocal offset, audio, past_end
                for out in frames:
                    arr=out.to_ndarray().reshape(-1)
                    if start is not None:
                        if out.pts is None or not out.time_base:raise ValueError('音轨缺少时间戳，无法只识别本段')
                        at=float(out.pts*out.time_base)-origin
                        lo=max(0,int(np.ceil((start-at)*sample_rate-1e-6)))
                        hi=min(arr.size,int(np.ceil((end-at)*sample_rate-1e-6)))
                        arr=arr[lo:max(lo,hi)]
                        past_end=at+out.samples/sample_rate>=end
                    arr=arr.astype(np.float32,copy=False)
                    n=arr.size
                    if not n:continue
                    if offset+n>audio.size:
                        new=np.empty(max(audio.size*2,offset+n)+sample_rate,dtype=np.float32)
                        new[:offset]=audio[:offset]
                        audio=new
                    audio[offset:offset+n]=arr
                    offset+=n
            for frame in container.decode(stream):
                if cancel_event is not None and cancel_event.is_set():
                    raise DecodeCancelled()
                collect(resampler.resample(frame))
                if past_end:break
            try:
                if not past_end:collect(resampler.resample(None))
            except Exception:
                pass
    except DecodeCancelled:
        raise
    except Exception as exc:
        raise RuntimeError(f"音频解码失败：{exc}") from exc
    if not offset:
        raise RuntimeError("没有解码到音频数据")
    if cancel_event is not None and cancel_event.is_set():
        raise DecodeCancelled()
    return audio[:offset]


def decode_audio(path: str, sample_rate: int = 16000, cancel_event=None, start=None, end=None) -> np.ndarray:
    """项目内限额PCM缓存：整段识别/对齐共享，局部请求优先读取同一缓存切片。"""
    if cancel_event is not None and cancel_event.is_set():raise DecodeCancelled()
    if start is not None and (end is None or not 0<=start<end):raise ValueError('重新识别的时间区间无效')
    source=Path(path).resolve();stat=source.stat()
    identity=f'{source}|{stat.st_mtime_ns}|{stat.st_size}|{sample_rate}|pcm-v1'
    digest=hashlib.sha256(identity.encode('utf-8')).hexdigest()
    target=AUDIO_CACHE_DIR/(digest+'.npy')
    with _AUDIO_LOCK:
        try:
            audio=np.load(target,mmap_mode='r',allow_pickle=False)
            if audio.dtype!=np.float32 or audio.ndim!=1:raise ValueError('缓存格式无效')
            if start is not None:audio=audio[round(start*sample_rate):round(end*sample_rate)]
            if not len(audio):raise RuntimeError('没有解码到音频数据')
            os.utime(target,None)
            log.info('PCM cache hit key=%s frames=%d rate=%d',digest[:12],len(audio),sample_rate)
            return audio
        except (OSError,ValueError,EOFError):pass
        started=time.perf_counter()
        audio=_decode_audio(str(source),sample_rate,cancel_event,start,end)
        log.info('PCM decode key=%s seconds=%.4f frames=%d rate=%d',digest[:12],time.perf_counter()-started,len(audio),sample_rate)
        # 局部首次识别只解码所需范围，不为了建缓存额外解码整集。
        if start is not None or audio.nbytes>AUDIO_CACHE_BYTES:return audio
        temp=None
        try:
            current=source.stat()
            if (current.st_mtime_ns,current.st_size)!=(stat.st_mtime_ns,stat.st_size):return audio
            AUDIO_CACHE_DIR.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=AUDIO_CACHE_DIR,suffix='.tmp',delete=False) as out:
                temp=Path(out.name);np.save(out,audio,allow_pickle=False)
            os.replace(temp,target);temp=None
            entries=sorted(AUDIO_CACHE_DIR.glob('*.npy'),key=lambda p:p.stat().st_mtime_ns,reverse=True)
            total=0
            for i,entry in enumerate(entries):
                total+=entry.stat().st_size
                if i>=AUDIO_CACHE_ENTRIES or total>AUDIO_CACHE_BYTES:
                    try:entry.unlink()
                    except OSError:pass  # 正被使用的Windows映射留到下次清理。
        except OSError:
            pass  # 磁盘不可写仍可继续识别，不把缓存失败当业务失败。
        finally:
            if temp is not None:
                try:temp.unlink(missing_ok=True)
                except OSError:pass
        return audio


def content_type(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {
        ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
        ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
        ".ts": "video/mp2t", ".flv": "video/x-flv", ".wmv": "video/x-ms-wmv",
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
        ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg",
        ".opus": "audio/ogg", ".wma": "audio/x-ms-wma",
    }.get(ext, "application/octet-stream")


def open_path(path: str) -> None:
    """在资源管理器中定位文件/文件夹。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    if os.name == "nt":
        if p.is_dir():
            os.startfile(str(p))  # type: ignore[attr-defined]
        else:
            import subprocess
            subprocess.Popen(["explorer", "/select,", str(p)])
    else:
        import subprocess
        subprocess.Popen(["open" if os.name == "posix" else "xdg-open", str(p.parent if p.is_file() else p)])
