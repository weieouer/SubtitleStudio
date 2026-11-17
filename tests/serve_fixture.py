"""隔离页面测试服务：合成媒体/任务，配置与工程均在临时目录。"""
from pathlib import Path
from array import array
import math
import json
import sys
import tempfile
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import app
from core import jobs, media, alignment
from core.model_manager import CATALOG, AUXILIARY
from core.srt_io import format_srt


def fake_transcribe(path, model, device, vad, on_progress=None, cancel_event=None, audio=None):
    for i in range(8):
        if cancel_event and cancel_event.is_set():
            raise jobs.Cancelled()
        if on_progress:
            on_progress(i / 8, "测试识别中")
        time.sleep(.15)
    return [{"start": 0.1, "end": 1.2, "text": "这是测试字幕。", "source": "asr", "words": []}], {"duration": 2}


def fake_align(path, cues, device, on_progress=None, cancel_event=None):
    result = json.loads(json.dumps(cues))
    for i in range(4):
        if cancel_event and cancel_event.is_set(): raise jobs.Cancelled()
        if on_progress: on_progress(i/4, '测试音轨对齐')
        time.sleep(.1)
    for index,cue in enumerate(result):
        # 模拟真实对齐的词时间和状态清理，避免只有一个空 alignment 记录的假成功。
        alignment.apply_word_times(result,[index],[{'text':cue['text'],'start':cue['start']+.02,'end':cue['end']+.02}])
        cue['alignment']['engine']='fixture'
    return result, {'duration':2}


class FixtureDownloads:
    def __init__(self):
        self.ready = {'large-v3', 'silero-vad'}
        self.status = {'status':'idle','progress':0,'message':'等待任务','model':''}
        self.started = 0

    def get(self):
        if self.status['status'] in ('downloading','preparing'):
            progress = min(1, (time.monotonic()-self.started)/1.3)
            if self.status['status'] == 'downloading':
                self.status.update(progress=progress,downloaded=round(progress*1000),total=1000)
            else:
                self.status.update(progress=progress)
            if progress >= 1:
                if self.status.get('model') == 'gpu':
                    self.status.update(status='done',message='测试 GPU 环境已就绪',speed_bps=0)
                else:
                    self.ready.add(self.status['model']); self.status.update(status='done',message='测试模型已就绪',speed_bps=0)
        return dict(self.status)

    def start(self, name):
        self.started=time.monotonic()
        self.status={'status':'downloading','progress':0,'model':name,'message':'魔搭 ModelScope · 测试下载中','downloaded':0,'total':1000,'source':'魔搭 ModelScope','speed_bps':1048576}
        return self.get()

    def start_gpu(self):
        self.started=time.monotonic()
        self.status={'status':'preparing','progress':0,'model':'gpu','message':'测试 GPU 环境安装中','downloaded':0,'total':0,'source':'','speed_bps':0}
        return self.get()

    def cancel(self):
        self.status.update(status='cancelled',message='测试下载已暂停')
        return self.get()


def main():
    with tempfile.TemporaryDirectory(prefix="subtitle-browser-") as temp:
        folder = Path(temp)
        for name in ("PROJECT_DIR", "CONFIG_DIR", "OUTPUT_DIR", "UPLOADS_DIR"):
            setattr(app, name, folder / name)
            getattr(app, name).mkdir()
        jobs.RESULTS_DIR = folder
        app.DIALOG_DIRS_FILE = app.CONFIG_DIR / 'dialog_dirs.json'
        media.CACHE_DIR = folder
        jobs.transcribe = fake_transcribe
        alignment.align_cues = fake_align
        downloads = FixtureDownloads()
        app.DOWNLOADS = downloads
        app.list_models = lambda: [{"name":name,**spec,"available":name in downloads.ready,"downloaded":name in downloads.ready,"size_gb":1.5} for name,spec in CATALOG.items()]
        app.list_auxiliary = lambda: [{"name":name,**spec,"available":name in downloads.ready,"downloaded":name in downloads.ready} for name,spec in AUXILIARY.items()]
        app.gpu_status = lambda: {"mode":"manual","nvidia":False,"state":"cpu","downloadable":True,
                                  "size_hint":"约 2.9 GB","error":"",
                                  "engines":[{"engine":"qwen","prepared":True,"torch":"cpu",
                                              "cuda_available":None,"error":""}]}
        audio = folder / "演示音频.wav"
        with wave.open(str(audio), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(array("h", [int(20000 * math.sin(2 * math.pi * 330 * i / 16000)
                * abs(math.sin(math.pi * 3 * i / 16000))) if 1600 < i < 19200 else 0 for i in range(32000)]).tobytes())
        subtitle = folder / "独立字幕.srt"
        subtitle.write_text(format_srt([{"start": .1, "end": 1.2, "text": "原始字幕。"}]), encoding="utf-8", newline="")
        class FixtureHandler(app.Handler):
            def _dialog(self,body):
                if body.get('kind')=='dir' and body.get('purpose')=='export':
                    target=app.CONFIG_DIR.parent / '选择的导出目录'
                    target.mkdir(exist_ok=True)
                    app._remember_dialog_dir('export',str(target))
                    self.send_json({'ok':True,'path':str(target),'paths':[str(target)]})
                else:
                    super()._dialog(body)

            def do_GET(self):
                if self.path == "/_fixture" and self._check_request():
                    downloads.ready={'large-v3','silero-vad'}
                    downloads.status={'status':'idle','progress':0,'message':'等待任务','model':''}
                    run_folder = folder / str(time.time_ns())
                    run_folder.mkdir()
                    # 相同合成音频会触发按内容恢复工程；各轮必须隔离工程库。
                    app.PROJECT_DIR = run_folder / 'projects'
                    app.PROJECT_DIR.mkdir()
                    app.CONFIG_DIR = run_folder / 'config'
                    app.CONFIG_DIR.mkdir()
                    app.DIALOG_DIRS_FILE = app.CONFIG_DIR / 'dialog_dirs.json'
                    fresh_subtitle = run_folder / subtitle.name
                    fresh_subtitle.write_bytes(subtitle.read_bytes())
                    fresh_audio = run_folder / audio.name
                    fresh_audio.write_bytes(audio.read_bytes())
                    self.send_json({"audio": str(fresh_audio), "srt": str(fresh_subtitle)})
                    return
                super().do_GET()
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        server.daemon_threads = True
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}",
                          "audio": str(audio), "srt": str(subtitle), "temporary": temp}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
