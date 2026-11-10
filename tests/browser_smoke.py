"""使用本机Chrome无头模式验证临时测试站点；不使用日常浏览器配置。"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time
from urllib.parse import urlparse, quote
from urllib.request import Request, urlopen


def read_json(url, method="GET"):
    with urlopen(Request(url, method=method), timeout=5) as response:
        return json.load(response)


class CDP:
    def __init__(self, url):
        parsed = urlparse(url)
        self.socket = socket.create_connection((parsed.hostname, parsed.port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        request = f"GET {parsed.path} HTTP/1.1\r\nHost: {parsed.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        self.socket.sendall(request.encode())
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            head += self.socket.recv(1)
        if not head.startswith(b"HTTP/1.1 101 "):
            raise RuntimeError(head.decode())
        self.counter = 0
        self.exceptions = []

    def read_exact(self, length):
        data = b""
        while len(data) < length:
            part = self.socket.recv(length - len(data))
            if not part:
                raise EOFError("CDP连接中断")
            data += part
        return data

    def receive(self):
        first, second = self.read_exact(2)
        length = second & 127
        if length == 126:
            length = struct.unpack("!H", self.read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.read_exact(8))[0]
        mask = self.read_exact(4) if second & 128 else None
        data = self.read_exact(length)
        if mask:
            data = bytes(v ^ mask[i % 4] for i, v in enumerate(data))
        if first & 15 == 8:
            raise EOFError("CDP关闭")
        return json.loads(data)

    def call(self, method, params=None):
        self.counter += 1
        data = json.dumps({"id": self.counter, "method": method, "params": params or {}}).encode()
        mask = os.urandom(4)
        size = len(data)
        header = bytes([0x81, 0x80 | size]) if size < 126 else bytes([0x81, 0xFE]) + struct.pack("!H", size) if size < 65536 else bytes([0x81, 0xFF]) + struct.pack("!Q", size)
        self.socket.sendall(header + mask + bytes(v ^ mask[i % 4] for i, v in enumerate(data)))
        while True:
            result = self.receive()
            if result.get("method") == "Runtime.exceptionThrown":
                self.exceptions.append(result["params"])
            if result.get("id") == self.counter:
                if "error" in result:
                    raise RuntimeError(result["error"])
                return result.get("result", {})

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                "awaitPromise": True, "userGesture": True})
        if result.get("exceptionDetails"):
            raise RuntimeError(result["exceptionDetails"])
        return result.get("result", {}).get("value")

    def wait(self, expression, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.evaluate(expression)
            if result:
                return result
            time.sleep(.1)
        raise TimeoutError(expression)

    def click(self, selector):
        self.evaluate(f"(()=>{{const e=document.querySelector({json.dumps(selector)});if(!e.closest('.model-menu'))e.scrollIntoView({{block:'center',inline:'nearest',behavior:'instant'}})}})()")
        rect = self.evaluate(f"(()=>{{const r=document.querySelector({json.dumps(selector)}).getBoundingClientRect();return {{x:r.x+r.width/2,y:r.y+r.height/2}}}})()")
        self.call("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, **rect})
        self.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, **rect})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, default=Path("docs/修复验证"))
    parser.add_argument('--benchmark',action='store_true')
    args = parser.parse_args()
    fixture = read_json(args.url + "/_fixture")
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="subtitle-headless-") as profile:
        chrome = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe"
        process = subprocess.Popen([str(chrome), "--headless=new", "--disable-gpu", "--no-first-run",
                                    "--no-default-browser-check", "--remote-debugging-port=0",
                                    "--user-data-dir=" + profile, "about:blank"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        cdp = None
        try:
            endpoint = Path(profile) / "DevToolsActivePort"
            for _ in range(100):
                if endpoint.exists():
                    break
                time.sleep(.1)
            port = int(endpoint.read_text().splitlines()[0])
            target = read_json(f"http://127.0.0.1:{port}/json/new?" + quote(args.url, safe=""), "PUT")
            cdp = CDP(target["webSocketDebuggerUrl"])
            cdp.call("Runtime.enable")
            cdp.call("Page.enable")
            cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1536, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
            cdp.wait("document.querySelector('#healthDot')?.className==='ok' && !document.documentElement.classList.contains('booting')")
            results.append({"name": "page_boot", "passed": True})
            cdp.evaluate("window.confirm=()=>false")
            cdp.evaluate("window.__windowIsMaximized=windowIsMaximized;windowIsMaximized=()=>false;localStorage.setItem('subtitleStudio.layout.v2',JSON.stringify({left:280,right:520}));resetPanelWidths()")
            assert cdp.evaluate("state.layout.left===state.layout.right && Math.abs(document.querySelector('.left').getBoundingClientRect().width-document.querySelector('.right').getBoundingClientRect().width)<1")
            cdp.evaluate("windowIsMaximized=()=>true;window.dispatchEvent(new Event('resize'))")
            assert cdp.evaluate("state.layout.left===520 && state.layout.right===520 && document.querySelector('.center').getBoundingClientRect().width>=460 && document.documentElement.scrollWidth===innerWidth")
            cdp.evaluate("windowIsMaximized=()=>false;window.dispatchEvent(new Event('resize'));windowIsMaximized=window.__windowIsMaximized;localStorage.removeItem('subtitleStudio.layout.v2');resetPanelWidths()")
            results.append({"name": "symmetric_startup_ignores_old_widths_maximized_sides_expand_and_restore", "passed": True})
            assert cdp.evaluate("state.settings.volume===80 && document.querySelector('#soundLevel').textContent==='80%'")
            cdp.evaluate("document.querySelector('#soundVolume').value='60';document.querySelector('#soundVolume').dispatchEvent(new Event('input',{bubbles:true}))")
            assert cdp.evaluate("state.settings.volume===60 && JSON.parse(localStorage.getItem(SETTINGS_KEY)).volume===60")
            cdp.evaluate("document.querySelector('#soundVolume').value='80';document.querySelector('#soundVolume').dispatchEvent(new Event('input',{bubbles:true}))")
            results.append({"name": "default_volume_80_and_volume_persistence", "passed": True})
            with urlopen(args.url + "/favicon.ico", timeout=5) as icon_response:
                assert icon_response.status == 200 and icon_response.read(4) == b"\0\0\1\0"
            assert cdp.evaluate("document.querySelector('.brand-icon').complete && document.querySelector('.brand-icon').naturalWidth>0")
            results.append({"name": "film_favicon_and_header_logo", "passed": True})
            geometry = cdp.evaluate("(()=>{const e=document.querySelector('.filter-picker .model-btn'),s=getComputedStyle(e);return {height:e.getBoundingClientRect().height,line:parseFloat(s.lineHeight)||parseFloat(s.fontSize)*1.45,inner:e.clientHeight-parseFloat(s.paddingTop)-parseFloat(s.paddingBottom),text:e.querySelector('span').textContent}})()")
            assert geometry["inner"] >= geometry["line"], geometry
            results.append({"name": "filter_text_not_clipped", "passed": True, "geometry": geometry})
            assert cdp.evaluate("(()=>{const boxes=[...document.querySelectorAll('.export-options input')];return !document.querySelector('#excludeNoise') && boxes.length===5 && boxes.every(e=>{const r=e.getBoundingClientRect();return r.width>0 && r.height>0})})()")
            assert cdp.evaluate("!document.querySelector('.help-dot') && document.querySelectorAll('#vadCheck').length===1 && !!document.querySelector('.left .export-options #vadCheck') && document.querySelector('#vadCheck').parentElement.textContent.trim()==='语音活动检测'")
            cdp.click('#vadCheck')
            assert cdp.evaluate("!state.settings.vad && JSON.parse(localStorage.getItem(SETTINGS_KEY)).vad===false")
            cdp.click('#vadCheck')
            cdp.call('Input.dispatchMouseEvent', {'type':'mouseMoved','x':1,'y':1})
            hover = cdp.evaluate("(()=>{const e=document.querySelector('#vadCheck').parentElement,r=e.getBoundingClientRect();return {x:r.right-10,y:r.y+r.height/2}})()")
            cdp.call('Input.dispatchMouseEvent', {'type':'mouseMoved',**hover})
            cdp.wait("document.querySelector('#tip').classList.contains('show') && document.querySelector('#tip').textContent.includes('检测人声区间')")
            cdp.call('Input.dispatchMouseEvent', {'type':'mouseMoved','x':1,'y':1})
            cdp.wait("!document.querySelector('#tip').classList.contains('show')")
            results.append({'name':'voice_only_in_center_persists_toggle_and_text_hover_tooltip_without_question_icons','passed':True})
            assert cdp.evaluate("!document.querySelector('#keepInterjections').checked && document.querySelector('#keepInterjections').parentElement.textContent.trim()==='语气词'")
            cdp.click('#keepInterjections')
            assert cdp.evaluate("state.settings.keepInterjections && JSON.parse(localStorage.getItem(SETTINGS_KEY)).keepInterjections===true")
            cdp.click('#keepInterjections')
            cdp.call('Input.dispatchMouseEvent', {'type':'mouseMoved','x':1,'y':1})
            hover = cdp.evaluate("(()=>{const e=document.querySelector('#keepInterjections').parentElement,r=e.getBoundingClientRect();return {x:r.right-10,y:r.y+r.height/2}})()")
            cdp.call('Input.dispatchMouseEvent', {'type':'mouseMoved',**hover})
            cdp.wait("document.querySelector('#tip').classList.contains('show') && document.querySelector('#tip').textContent.includes('仅控制提取阶段是否保留语气词')")
            cdp.call('Input.dispatchMouseEvent', {'type':'mouseMoved','x':1,'y':1})
            cdp.wait("!document.querySelector('#tip').classList.contains('show')")
            results.append({'name':'interjection_toggle_default_off_removes_and_explains_scope','passed':True})
            assert cdp.evaluate("(()=>{const selectors=['#gapInput','#widthInput','#searchInput','.dz-title','#keepPunc'];return new Set(selectors.map(s=>getComputedStyle(document.querySelector(s)).fontFamily)).size===1})()")
            cdp.evaluate("setMode('align')")
            assert cdp.evaluate("document.querySelector('#btnExtract').disabled && document.querySelector('#subtitleFileName').textContent.includes('请选择') && getComputedStyle(document.querySelector('.recognition-only')).display!=='none'")
            cdp.evaluate("setMode('extract')")
            cdp.click('#keepPunc')
            assert cdp.evaluate("!state.settings.keepPunc && !document.querySelector('#addPunc') && !document.querySelector('#withRole') && !document.querySelector('#tip').classList.contains('show')")
            cdp.click('#keepPunc')
            results.append({"name": "unified_font_square_checkbox_and_direct_alignment_requires_srt", "passed": True})
            cdp.click('#modelBtn')
            assert cdp.evaluate("document.querySelectorAll('#modelMenu .model-choice').length===3 && document.querySelector('#modelMenu [data-name=\"fireredasr2-aed\"]').disabled && !document.querySelector('#modelMenu [data-download=\"fireredasr2-aed\"]').disabled")
            assert cdp.evaluate("(()=>{const e=document.querySelector('#modelMenu [data-download=\"fireredasr2-aed\"]'),r=e.getBoundingClientRect();return document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)?.closest('[data-download]')===e})()"), cdp.evaluate("({menu:document.querySelector('#modelMenu').getBoundingClientRect().toJSON(),button:document.querySelector('#modelMenu [data-download=\"fireredasr2-aed\"]').getBoundingClientRect().toJSON()})")
            assert cdp.evaluate("document.querySelector('#modelMenu [data-name=\"fireredasr2-aed\"] .model-desc').textContent.includes('中文优先')")
            # 弹层宽度跟随“模型”按钮；三行选项框等宽，说明不换行导致高度参差。
            assert cdp.evaluate("(()=>{const b=document.querySelector('#modelBtn').getBoundingClientRect().width,m=parseFloat(getComputedStyle(document.querySelector('#modelMenu')).width),rows=[...document.querySelectorAll('#modelMenu .model-choice')],opts=[...document.querySelectorAll('#modelMenu .model-option')];return Math.abs(m-Math.min(Math.max(b,300),innerWidth-16))<1 && new Set(rows.map(r=>Math.round(r.getBoundingClientRect().height))).size===1 && new Set(opts.map(o=>Math.round(o.getBoundingClientRect().width))).size===1})()")
            cdp.click('#modelMenu [data-download="fireredasr2-aed"]')
            try:
                cdp.wait("downloadBusy()")
            except TimeoutError:
                raise AssertionError(cdp.evaluate("({locked:editLocked(),download:state.modelDownload,menuHidden:document.querySelector('#modelMenu').hidden,toast:toastEl.textContent,rect:document.querySelector('#modelMenu').getBoundingClientRect().toJSON()})"))
            assert cdp.evaluate("document.querySelector('.progress').getBoundingClientRect().height>=13 && document.querySelector('#btnExtract').disabled && !document.querySelector('#btnCancel').hidden && document.querySelector('#jobMsg').textContent.includes('ModelScope') && document.querySelector('#jobMsg').textContent.includes('MB/s')")
            cdp.wait("state.modelDownload.status==='done' && state.models.some(m=>m.name==='fireredasr2-aed' && m.available)")
            cdp.click('#auxBtn')
            assert cdp.evaluate("document.querySelectorAll('#auxMenu .model-choice').length===2 && document.querySelector('#auxMenu [data-aux=\"qwen3-forcedaligner-0.6b\"]').disabled && !document.querySelector('#auxMenu [data-aux=\"silero-vad\"]').disabled")
            cdp.click('#auxMenu [data-download="qwen3-forcedaligner-0.6b"]')
            cdp.wait("downloadBusy()")
            cdp.click('#btnCancel')
            cdp.wait("state.modelDownload.status==='cancelled'")
            cdp.click('#auxBtn')
            cdp.click('#auxMenu [data-download="qwen3-forcedaligner-0.6b"]')
            cdp.wait("state.modelDownload.status==='done' && state.auxModels.some(m=>m.name==='qwen3-forcedaligner-0.6b' && m.available)")
            assert cdp.evaluate("document.querySelector('#modelLabel').dataset.tip.includes('Whisper') && document.querySelector('#auxLabel').dataset.tip.includes('Qwen3') && !document.querySelector('#btnImportSrt') && !document.querySelector('#queueCard')")
            cdp.evaluate("document.querySelector('.left').scrollTop=0;hideTip();toastEl.classList.remove('show')")
            results.append({"name": "three_models_two_auxiliary_compact_download_cancel_resume_mock_transport", "passed": True})
            cdp.evaluate("applyTheme('dark',false)")
            assert cdp.evaluate("(()=>{const colors=['.center','.left','.card'].map(s=>getComputedStyle(document.querySelector(s)).backgroundColor.match(/\\d+/g).slice(0,3).map(Number));return colors.every(c=>c[0]===c[1] && c[1]===c[2]) && colors[0][0]<colors[1][0] && colors[1][0]<colors[2][0]})()")
            results.append({'name':'neutral_dark_shell_and_deeper_center_without_warm_tint','passed':True})
            time.sleep(.3)
            image = cdp.call("Page.captureScreenshot", {"format": "png"})
            (args.output / "界面_深色.png").write_bytes(base64.b64decode(image["data"]))
            cdp.evaluate("applyTheme('light',false)")
            time.sleep(.3)
            image = cdp.call("Page.captureScreenshot", {"format": "png"})
            (args.output / "界面_浅色.png").write_bytes(base64.b64decode(image["data"]))
            cdp.evaluate("window.confirm=()=>false;window.__soundEvents=[];window.__originalSound=playSound;playSound=(kind)=>{window.__soundEvents.push(kind);window.__originalSound(kind)}")
            cdp.click("#soundToggle")
            cdp.click("#soundToggle")
            slider = cdp.evaluate("(()=>{const e=document.querySelector('#gapRange'),r=e.getBoundingClientRect();return {x:r.x,y:r.y+r.height/2,width:r.width}})()")
            cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": slider["x"] + 7, "y": slider["y"], "button": "left", "clickCount": 1})
            for i in range(1, 8):
                cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": slider["x"] + 7 + (slider["width"] - 14) * i / 10, "y": slider["y"], "button": "left", "buttons": 1})
                time.sleep(.04)
            cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": slider["x"] + slider["width"] * .7, "y": slider["y"], "button": "left", "clickCount": 1})
            assert cdp.evaluate("document.querySelector('#gapRange').value===document.querySelector('#gapInput').value || Number(document.querySelector('#gapRange').value)===Number(document.querySelector('#gapInput').value)")
            assert cdp.evaluate("window.__soundEvents.includes('tick') && audioContext?.state==='running'")
            results.append({"name": "slider_sync_and_tick_audio", "passed": True})
            cdp.evaluate("window.__testApi=api;api=async(path,...args)=>path==='/api/dialog'&&args[0]?.kind==='srt'?{path:" + json.dumps(fixture["srt"]) + "}:window.__testApi(path,...args)")
            cdp.evaluate("setMode('align')")
            cdp.click("#btnSourceSrt")
            cdp.wait("state.srtPath && !state.operation && !state.dirty")
            assert cdp.evaluate("state.subtitleFile.origin==='imported' && document.querySelector('#subtitleFileName').textContent.endsWith('.srt')")
            cdp.evaluate("(async()=>{setMode('align');state.script=(await api('/api/script/parse',{text:'甲：原始字幕。'})).lines;state.scriptName='剧本';return matchScript()})()")
            assert cdp.evaluate("!state.jobId && state.cues[0].script_text==='原始字幕。'"), cdp.evaluate("({cue:state.cues[0],mode:state.settings.mode,operation:state.operation,toast:toastEl.textContent})")
            cdp.evaluate("setMode('extract')")
            results.append({"name": "manual_srt_alignment_without_recognition", "passed": True})
            cdp.evaluate("document.querySelector('.cue-text').focus();document.querySelector('.cue-text').innerText='校对后的字幕。';document.querySelector('.cue-text').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('.cue-text').blur()")
            cdp.evaluate("saveAll()")
            assert cdp.evaluate("!state.dirty && projectPath().endsWith('.srt')")
            remembered_parameters=cdp.evaluate("({width:state.settings.width,gap:state.settings.gap,device:state.settings.device})")
            old_origin = cdp.evaluate("performance.timeOrigin")
            cdp.call("Page.reload")
            cdp.wait("performance.timeOrigin!==" + str(old_origin) + " && typeof playSound==='function' && document.querySelector('#healthDot')?.className==='ok' && !document.documentElement.classList.contains('booting')")
            assert cdp.evaluate("JSON.stringify({width:state.settings.width,gap:state.settings.gap,device:state.settings.device})===JSON.stringify("+json.dumps(remembered_parameters)+")")
            assert cdp.evaluate("document.querySelector('#settingsTitle').textContent==='参数' && document.querySelector('#btnSegment').getBoundingClientRect().top>=document.querySelector('.cue-search-row').getBoundingClientRect().bottom")
            cdp.evaluate("window.confirm=()=>false;window.__soundEvents=[];window.__originalSound=playSound;playSound=(kind)=>{window.__soundEvents.push(kind);window.__originalSound(kind)}")
            cdp.evaluate("window.__realConfirmDialog=confirmDialog;window.__referenceMatchPrompts=[];confirmDialog=(message)=>message.startsWith('音视频、字幕和参考文本已齐全')?(window.__referenceMatchPrompts.push(message),Promise.resolve(window.__referenceMatchAnswer===true)):window.__realConfirmDialog(message)")
            cdp.evaluate("window.__testApi=api;api=async(path,...args)=>path==='/api/dialog'&&args[0]?.kind==='srt'?{path:" + json.dumps(fixture["srt"]) + "}:window.__testApi(path,...args)")
            cdp.evaluate("importSrt()")
            assert cdp.evaluate("state.cues[0].text==='校对后的字幕。' && !state.dirty")
            results.append({"name": "standalone_srt_save_restore", "passed": True})
            cdp.click('#btnOpenProject')
            cdp.wait("document.querySelector('#projectList [data-project]')!==null")
            assert cdp.evaluate("document.querySelector('#projectList').textContent.includes('独立字幕.srt')")
            geometry = cdp.evaluate("(()=>{const d=document.querySelector('#projectDialog'),b=[...d.querySelectorAll('.dialog-actions button')];if(b.length!==2)return {count:b.length};const r=d.getBoundingClientRect();return {labels:b.map(x=>x.textContent).join('|'),left:b[0].getBoundingClientRect().left-r.left,right:r.right-b[1].getBoundingClientRect().right,width:r.width}})()")
            assert geometry['labels']=='打开工程文件夹|关闭' and geometry['left']<geometry['width']/2 and geometry['right']<geometry['width']/2, geometry
            cdp.evaluate("window.__folderApi=api;window.__folderOpened='';api=async(path,body,...rest)=>path==='/api/open_folder'?(window.__folderOpened=body.path,{ok:true}):window.__folderApi(path,body,...rest)")
            cdp.click('#btnOpenProjectFolder')
            assert cdp.evaluate("!!state.projectDirectory&&window.__folderOpened===state.projectDirectory&&window.__folderOpened!==(state.media?.path||state.srtPath)")
            cdp.evaluate("api=window.__folderApi")
            cdp.evaluate("document.querySelector('#projectList [data-project=\"'+state.recentProjects.findIndex(p=>p.source_path===" + json.dumps(fixture['srt']) + ")+ '\"]').click()")
            cdp.wait("!state.operation && state.srtPath && !document.querySelector('#projectDialog').open")
            assert cdp.evaluate("state.cues[0].text==='校对后的字幕。'")
            results.append({'name':'top_project_library_restores_saved_subtitles_and_parameters','passed':True})
            cdp.evaluate("loadMedia(" + json.dumps(fixture["audio"]) + ")")
            cdp.wait("document.querySelector('#confirmDialog').open || (!!state.media && !state.loading)")
            if cdp.evaluate("document.querySelector('#confirmDialog').open"):
                cdp.click('#btnConfirmCancel')
            cdp.wait("!!state.media && !state.loading && !document.querySelector('#confirmDialog').open")
            assert cdp.evaluate("!document.querySelector('#btnAsr') && document.querySelector('#btnCancel').hidden")
            cdp.click("#btnExtract")
            cdp.wait("!!state.jobId")
            assert cdp.evaluate("document.querySelector('#btnSourceSrt').disabled && document.querySelector('#btnSegment').disabled && !document.querySelector('.cue-text[contenteditable=true]')")
            cdp.wait("!state.jobId && state.cues.length>0 && !state.dirty")
            assert cdp.evaluate("state.subtitleFile.origin==='generated' && state.subtitleFile.path.endsWith('.srt') && document.querySelector('#subtitleFileName').textContent==='演示音频.srt'")
            assert cdp.evaluate("(async()=>{const d=await api('/api/srt/parse',{path:state.subtitleFile.path});return d.cues[0].text===state.cues[0].text})()")
            cdp.evaluate("window.__extractedSource=state.subtitleFile.path;window.__extractedCues=JSON.stringify(state.cues);setMode('align')")
            assert cdp.evaluate("state.subtitleFile.path===window.__extractedSource && JSON.stringify(state.cues)===window.__extractedCues && document.querySelector('#subtitleFileNote').textContent.includes('已载入对齐字幕')")
            cdp.evaluate("setMode('extract')")
            assert cdp.evaluate("window.__soundEvents.includes('success')")
            results.append({"name": "asr_ui_lock_and_completion_audio_mock_engine", "passed": True})
            results.append({"name": "generated_srt_file_and_automatic_alignment_handoff", "passed": True})
            cdp.evaluate("window.__originalCueText=state.cues[0].text;state.cues[0].text='第十二集，3个人。';video.currentTime=.3;updateOverlay()")
            assert cdp.evaluate("!state.settings.arabicNumbers && overlay.textContent==='第十二集，三个人。'")
            cdp.click("#arabicNumbers")
            assert cdp.evaluate("state.settings.arabicNumbers && overlay.textContent==='第12集，3个人。'")
            cdp.evaluate("window.__exportApi=api;api=async(path,body,...rest)=>{if(path==='/api/srt/export')window.__exportPayload=body;return window.__exportApi(path,body,...rest)};exportSrt(false)")
            assert cdp.evaluate("window.__exportPayload.cues[0].text==='第12集，3个人。' && state.cues[0].text==='第十二集，3个人。' && window.__exportPayload.options.output_dir.endsWith('选择的导出目录')")
            cdp.evaluate("api=window.__exportApi;state.cues[0].text=window.__originalCueText")
            cdp.click("#arabicNumbers")
            assert cdp.evaluate("!state.settings.arabicNumbers")
            results.append({"name": "number_switch_preview_export_and_original_preserved", "passed": True})
            assert cdp.evaluate("document.querySelector('#arabicNumbers').parentElement.textContent.trim()==='阿拉伯数字' && document.querySelector('#wrapSubtitles').checked")
            cdp.evaluate("window.__multilineOriginal=state.cues[0].text;state.cues[0].text='上半句\\n下半句';state.cues[0].is_noise=true;renderTable()")
            assert cdp.evaluate("document.querySelector('#cueCount').textContent.includes('共 1 条 · 已对齐 0 条 · 疑似 1 条')")
            wrapped = cdp.evaluate("(async()=>{const d=await exportSrt(false);return (await api('/api/srt/parse',{path:d.path})).cues[0]})()")
            assert wrapped['text'] == '上半句\n下半句', wrapped
            cdp.click('#wrapSubtitles')
            flat = cdp.evaluate("(async()=>{const d=await exportSrt(false);return (await api('/api/srt/parse',{path:d.path})).cues[0]})()")
            assert flat['text'] == '上半句 下半句' and flat['start'] == wrapped['start'] and flat['end'] == wrapped['end'], flat
            assert cdp.evaluate("state.cues[0].text==='上半句\\n下半句'")
            cdp.click('#wrapSubtitles')
            cdp.evaluate("state.cues[0].text=window.__multilineOriginal;state.cues[0].is_noise=false;renderTable();updateOverlay()")
            results.append({"name": "subtitle_wrap_checked_preserves_lines_unchecked_flattens_actual_srt_without_source_time_changes", "passed": True})
            cdp.evaluate("window.__cuesBeforePunc=state.cues;state.cues=[{start:1,end:2,text:'诶？好，走吧。'}];renderTable()")
            assert cdp.evaluate("subtitleText(state.cues[0].text)==='诶？好，走吧。'")
            cdp.click('#keepPunc')
            assert cdp.evaluate("subtitleText(state.cues[0].text)==='诶好走吧' && state.cues[0].text==='诶？好，走吧。'"), cdp.evaluate("subtitleText(state.cues[0].text)")
            assert cdp.evaluate("document.querySelector('.cue-text').textContent==='诶好走吧'")
            assert cdp.evaluate("wrapSubtitleText(subtitleText(state.cues[0].text),state.settings.width)==='诶好走吧'")
            cdp.click('#keepPunc')
            assert cdp.evaluate("document.querySelector('.cue-text').textContent==='诶？好，走吧。'")
            cdp.evaluate("state.cues=window.__cuesBeforePunc;renderTable()")
            results.append({"name": "punctuation_switch_applies_to_preview_and_overlay_without_touching_source", "passed": True})
            # 演示音频只有 2 秒；临时把媒体时长放宽，才能验证 6–9 秒的长字幕按时长上限截断而不是被媒体结尾裁掉。
            cdp.evaluate("window.__mediaBeforeTiming=state.media;window.__cuesBeforeTiming=state.cues;"
                         "state.media={...(state.media||{}),duration:20};"
                         "state.cues=[{start:1,end:1.2,text:'短'},{start:1.5,end:1.6,text:'近'},{start:6,end:9,text:'长'}]")
            for selector, value in (("#subtitleMin", "0.8"), ("#subtitleMax", "2")):
                cdp.evaluate(f"document.querySelector('{selector}').value='{value}';document.querySelector('{selector}').dispatchEvent(new Event('change',{{bubbles:true}}))")
            cdp.click('#btnPreviewTiming')
            timing = cdp.evaluate("presentationCues().map(c=>[c.start,c.end])")
            assert [[round(v, 3) for v in pair] for pair in timing] == [[1, 1.5], [1.5, 2.3], [6, 8]], timing
            assert cdp.evaluate("state.cues[2].end===9")
            cdp.evaluate("window.__exportApi=api;api=async(path,body,...rest)=>{if(path==='/api/srt/export')window.__exportPayload=body;return window.__exportApi(path,body,...rest)};")
            cdp.evaluate("(async()=>{await exportSrt(false)})()")
            exported = cdp.evaluate("window.__exportPayload.cues.map(c=>[c.start,c.end])")
            assert [[round(v, 3) for v in pair] for pair in exported] == [[1, 1.2], [1.5, 1.6], [6, 9]], exported
            cdp.evaluate("api=window.__exportApi;state.media=window.__mediaBeforeTiming;state.cues=window.__cuesBeforeTiming;state.presentationCache=null;"
                         "for(const id of ['#subtitleMin','#subtitleMax']){const e=document.querySelector(id);e.value='0';e.dispatchEvent(new Event('change',{bubbles:true}))}"
                         "renderTable();updateOverlay()")
            results.append({"name": "min_max_display_duration_changes_preview_export_keeps_current_cue_times", "passed": True})
            cdp.evaluate("(async()=>{state.script=(await api('/api/script/parse',{text:'角色：这是测试字幕。'})).lines;state.scriptName='测试剧本';setMode('align');return matchScript()})()")
            assert cdp.evaluate("state.script[0].cue_index===0 && document.querySelector('#cueBody input[data-field=start]').value==='00:00.10'")
            cdp.evaluate("selectCue(0,true)")
            assert cdp.evaluate("state.selected===0 && Math.abs(video.currentTime-.12)<.1 && Number.parseFloat(document.querySelector('#wave').getAttribute('aria-valuemax'))===2")
            cdp.evaluate("(()=>{const e=document.querySelector('#cueBody input[data-field=start]');e.value='00:00.20';e.dispatchEvent(new Event('change',{bubbles:true}))})()")
            assert cdp.evaluate("state.cues[0].start===.2 && state.history.length>0")
            cdp.evaluate("document.querySelector('#verificationReportDialog').close();undo()")
            assert cdp.evaluate("state.cues[0].start===.1")
            cdp.evaluate("selectCue(0,false);focusWave(state.cues[0])")
            edge = cdp.evaluate("(()=>{const r=document.querySelector('#wave').getBoundingClientRect(),v=waveWindow();return {x:r.x+(state.cues[0].start-v.start)/v.span*r.width,to:r.x+(.25-v.start)/v.span*r.width,y:r.y+45}})()")
            cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": edge["x"], "y": edge["y"], "button": "left", "clickCount": 1})
            cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": edge["to"], "y": edge["y"], "button": "left", "buttons": 1})
            cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": edge["to"], "y": edge["y"], "button": "left", "clickCount": 1})
            assert cdp.evaluate("Math.abs(state.cues[0].start-.25)<.02 && !state.operation")
            cdp.evaluate("document.querySelector('#verificationReportDialog').close();undo()")
            assert cdp.evaluate("state.cues[0].start===.1")
            cdp.evaluate("selectCue(0,false);focusWave(state.cues[0])")
            results.append({"name": "alignment_panel_preview_timeline_and_time_edit_undo", "passed": True})
            cdp.click("#waveZoomIn")
            cdp.wait("state.detailWave?.start!=null && state.detailWave?.peaks?.length>=1200")
            assert cdp.evaluate("state.detailWave.start<=waveWindow().start+.001 && state.detailWave.end>=waveWindow().start+waveWindow().span-.001")
            assert cdp.evaluate("state.detailWave.end-state.detailWave.start<=118.001")
            cdp.click("#waveReset")
            results.append({"name": "zoom_fetches_detailed_waveform_region", "passed": True})
            mode = cdp.evaluate("(()=>{const e=document.querySelector('#modeNote'),s=getComputedStyle(e);return {text:e.textContent,height:e.clientHeight,line:parseFloat(s.lineHeight)}})()")
            assert mode["height"] <= mode["line"] + 1, mode
            results.append({"name": "short_mode_note_one_line", "passed": True})
            cdp.evaluate("saveAll()")
            assert cdp.evaluate("(async()=>{const d=await api('/api/projects/load?path='+encodeURIComponent(projectPath()));return d.project.subtitle_file.path===state.subtitleFile.path && d.project.settings.keepPunc===true})()")
            results.append({"name": "subtitle_file_and_export_settings_persist_in_project", "passed": True})
            cdp.evaluate("hideTip();toastEl.classList.remove('show')")
            cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 750, "y": 24})
            cdp.evaluate("applyTheme('dark',false)")
            time.sleep(.3)
            image = cdp.call("Page.captureScreenshot", {"format": "png"})
            (args.output / "界面_字幕对齐.png").write_bytes(base64.b64decode(image["data"]))
            cdp.click("#btnExport")
            cdp.click("#btnConfirmExport")
            cdp.wait("document.querySelector('#toast').textContent.includes('已导出')")
            assert cdp.evaluate("toastEl.textContent.includes('选择的导出目录')")
            results.append({"name": "browser_export", "passed": True})
            cdp.evaluate("window.__exportApi=api;api=async(path,...args)=>{const result=await window.__exportApi(path,...args);if(path==='/api/srt/export')window.__mkvResult=result;return result}")
            cdp.click('#btnExport')
            cdp.evaluate("document.querySelector('#exportFormat').value='mkv'")
            cdp.click('#btnConfirmExport')
            cdp.wait("!state.operation && window.__mkvResult?.path.endsWith('.mkv')")
            assert cdp.evaluate("window.__mkvResult.path.includes('选择的导出目录')")
            assert cdp.evaluate("(async()=>{const result=await api('/api/media/info?path='+encodeURIComponent(window.__mkvResult.path));return result.info.audio_tracks.length>0 && result.info.format.includes('matroska')})()")
            cdp.evaluate("api=window.__exportApi;document.querySelector('#exportFormat').value='srt'")
            results.append({'name':'export_dialog_writes_real_mkv_audio_and_subtitle_container','passed':True})
            cdp.evaluate("(()=>{setMode('align');const dt=new DataTransfer();dt.items.add(new File(['1\\r\\n00:00:00,200 --> 00:00:01,000\\r\\n我们明天去公圆散步。\\r\\n'.replaceAll('\\\\r','\\r').replaceAll('\\\\n','\\n')],'拖入字幕.srt',{type:'application/x-subrip'}));document.querySelector('#subtitleSource').dispatchEvent(new DragEvent('drop',{dataTransfer:dt,bubbles:true,cancelable:true}))})()")
            cdp.wait("!state.operation && state.subtitleFile?.path.endsWith('拖入字幕.srt')")
            assert cdp.evaluate("state.cues[0].text==='我们明天去公圆散步。' && state.cues[0].start===.2 && state.subtitleFile.origin==='imported'")
            cdp.evaluate("(async()=>{window.__referenceMatchAnswer=true;state.cues[0].is_noise=true;await applyScriptResult(await api('/api/script/parse',{text:'清晨。\\n我们明天去公园散步。'}),'参考.txt');window.__beforeCombined=JSON.stringify({text:state.cues[0].text,start:state.cues[0].start,end:state.cues[0].end});return saveAll()})()")
            cdp.wait("window.__referenceMatchPrompts.some(message=>message.includes('是否开始对比校验'))")
            cdp.wait("!state.jobId && !state.operation && state.cues[0].reference_match?.status==='homophone'")
            assert cdp.evaluate("state.cues[0].text===JSON.parse(window.__beforeCombined).text && state.cues[0].start===JSON.parse(window.__beforeCombined).start && state.cues[0].end===JSON.parse(window.__beforeCombined).end && state.subtitleFile.path.endsWith('拖入字幕.srt')")
            cdp.evaluate("window.__referenceMatchAnswer=false")
            assert cdp.evaluate("state.cues[0].is_noise && document.querySelector('#cueCount').textContent.includes('共 1 条 · 已对齐 0 条 · 疑似 1 条')")
            assert cdp.evaluate("(async()=>{const d=await api('/api/srt/parse',{path:state.subtitleFile.path});return d.cues[0].start===.2})()")
            cdp.evaluate("window.__beforeRealign=JSON.stringify({text:state.cues[0].text,start:state.cues[0].start,end:state.cues[0].end})")
            cdp.click('#btnRealignAll')
            cdp.wait("!state.jobId && !state.operation && !!state.cues[0].alignment")
            assert cdp.evaluate("state.cues[0].text===JSON.parse(window.__beforeRealign).text && state.cues[0].start===.22 && state.cues[0].original_start===.2")
            cdp.evaluate("document.querySelector('#verificationReportDialog').close();selectCue(0,false)")
            cdp.click('#btnCalibrate')
            cdp.wait("!state.operation&&state.cues[0].text==='我们明天去公园散步。'")
            assert cdp.evaluate("state.cues[0].text==='我们明天去公园散步。' && state.cues[0].original_text==='我们明天去公圆散步。' && state.cues[0].start===.22 && state.cues[0].end===1.02")
            cdp.evaluate("document.querySelector('#verificationReportDialog').close();undo()")
            assert cdp.evaluate("state.cues[0].text==='我们明天去公圆散步。'")
            cdp.evaluate('selectCue(0,false)')
            assert cdp.evaluate("!document.querySelector('#btnExcludeSelected')")
            cdp.click('#btnDelete')
            assert cdp.evaluate("state.cues.length===0")
            cdp.click('#btnUndo')
            assert cdp.evaluate("state.cues.length===1 && state.cues[0].text==='我们明天去公圆散步。'")
            cdp.evaluate("matchScript(false,[0])")
            assert cdp.evaluate("state.cues[0].reference_match.status==='homophone' && !state.cues[0].exclude_export")
            cdp.evaluate("saveAll()")
            assert cdp.evaluate("(async()=>{const d=await api('/api/projects/load?path='+encodeURIComponent(projectPath()));return d.project.script_document.text.includes('我们明天去公园散步') && d.project.cues[0].original_text==='我们明天去公圆散步。' && d.project.cues[0].reference_match.status==='homophone'})()")
            results.append({"name": "srt_drop_combined_alignment_reference_review_undo_and_original_persistence", "passed": True})
            cdp.evaluate("window.__workspaceBefore={cues:snapshot(),settings:{...state.settings},script:state.script,doc:state.scriptDocument};state.cues=[normalizeCue({start:.1,end:.3,text:'前。'},0),normalizeCue({start:.4,end:.9,text:'旧的中间字幕'},1),normalizeCue({start:1.1,end:1.5,text:'后。'},2)];window.__neighbors=[JSON.stringify(state.cues[0]),JSON.stringify(state.cues[2])];renderAll();window.__regionApi=api;api=async(path,body,...rest)=>{if(path==='/api/jobs'&&body?.region)window.__regionSent=body.region;return window.__regionApi(path,body,...rest)}")
            # 重新识别属于字幕提取；对齐模式不显示该按钮，切到提取模式后再操作。
            cdp.evaluate("setMode('extract')")
            cdp.evaluate('selectCue(1,false)')
            cdp.click('#btnRerunSelected')
            cdp.wait("!state.jobId && state.cues[1]?.text==='这是测试字幕。'")
            assert cdp.evaluate("window.__regionSent.start===.4 && window.__regionSent.end===.9 && JSON.stringify(state.cues[0])===window.__neighbors[0] && JSON.stringify(state.cues[2])===window.__neighbors[1] && state.cues[1].start>=.4 && state.cues[1].end<=.9 && !state.cues[1].reference_match && JSON.stringify(state.script)===JSON.stringify(window.__workspaceBefore.script)")
            results.append({'name':'regional_recognition_replaces_only_target_without_touching_script','passed':True})
            cdp.evaluate("window.__noReference={snapshot:snapshot(),script:state.script,doc:state.scriptDocument,settings:{...state.settings},confirm:window.confirmDialog};state.cues=[normalizeCue({start:.2,end:1,text:'會不會是我們，乾脆這樣。'},0)];state.script=[];state.scriptDocument=null;state.settings.subtitleLead=0;state.settings.subtitleTail=0;state.settings.mode='align';syncSettingsUI();renderAll();window.__noReferencePrompts=0;window.confirmDialog=async()=>{window.__noReferencePrompts++;return false}")
            assert cdp.evaluate("state.cues[0].text==='会不会是我们，干脆这样。' && state.cues[0].original_text==='會不會是我們，乾脆這樣。' && document.querySelector('#btnExtract').disabled && !document.querySelector('#btnRealignAll').disabled")
            cdp.click('#btnRealignAll')
            cdp.wait("!state.jobId && !state.operation && !!state.cues[0].alignment")
            assert cdp.evaluate("window.__noReferencePrompts===0 && state.script.length===0 && state.cues[0].text==='会不会是我们，干脆这样。' && state.cues[0].start===.22 && !state.cues[0].reference_match")
            assert cdp.evaluate("(async()=>{const d=await exportSrt(false);const result=await api('/api/srt/parse',{path:d.path});return result.cues[0].text.includes('会不会是我们') && !result.cues[0].text.includes('會')})()")
            cdp.evaluate("window.__waveVolume=video.volume;document.querySelector('#waveGain').value=2;document.querySelector('#waveGain').dispatchEvent(new Event('input',{bubbles:true}))")
            assert cdp.evaluate("state.settings.waveGain===2 && video.volume===window.__waveVolume && document.querySelector('#waveGainValue').textContent==='2×' && !document.querySelector('#saveState') && !!document.querySelector('#topSaveState')")
            cdp.evaluate("restoreSnapshot(window.__noReference.snapshot);state.script=window.__noReference.script;state.scriptDocument=window.__noReference.doc;state.settings=window.__noReference.settings;window.confirmDialog=window.__noReference.confirm;syncSettingsUI();renderAll()")
            results.append({'name':'simplified_subtitles_optional_script_alignment_wave_gain_and_single_save_indicator','passed':True})
            cdp.evaluate("api=window.__regionApi;state.settings.width=10;state.settings.wrapSubtitles=true;state.cues=Array.from({length:65},(_,i)=>normalizeCue({start:i*.025,end:(i+1)*.025,text:'用于检查预览换行和自动跟随字幕片段'},i));state.script=[];state.scriptDocument=null;state.playingIndex=-1;state.listScrollPauseUntil=0;setMode('extract',false);renderAll();video.currentTime=1.1;updateOverlay()")
            assert cdp.evaluate("!!document.querySelector('.right #cueBody') && !document.querySelector('.center #cueBody') && !document.querySelector('#scriptList') && document.querySelector('.right .table-wrap').scrollTop>0 && overlay.textContent.split('\\n').every(s=>[...s].length<=10) && document.querySelector('#cueBody .suggest').textContent.includes('尚无参考')")
            cdp.evaluate("video.muted=true;video.play()")
            cdp.wait("!video.paused && state.playbackFrame!==null && video.currentTime>1.12")
            cdp.evaluate("video.pause()")
            assert cdp.evaluate("state.playbackFrame===null")
            results.append({'name':'single_sidebar_tracks_actual_playback_and_preview_character_wrap','passed':True})
            cdp.evaluate("document.querySelector('#verificationReportDialog').close();state.waveSpan=2;state.waveStart=0;drawWave();window.__trackHeight=document.querySelector('#timelinePanel').getBoundingClientRect().height")
            bounds=cdp.evaluate("(()=>{const r=document.querySelector('#wave').getBoundingClientRect();return {x:r.x+r.width*.5,y:r.y+r.height*.75}})()")
            cdp.call('Input.dispatchMouseEvent',{'type':'mouseWheel','deltaX':0,'deltaY':-120,**bounds})
            cdp.wait("state.waveSpan<2")
            grip=cdp.evaluate("(()=>{const r=document.querySelector('#timelineResize').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()")
            cdp.call('Input.dispatchMouseEvent',{'type':'mousePressed','button':'left','clickCount':1,**grip})
            cdp.call('Input.dispatchMouseEvent',{'type':'mouseMoved','buttons':1,'x':grip['x'],'y':grip['y']-60})
            cdp.call('Input.dispatchMouseEvent',{'type':'mouseReleased','button':'left','clickCount':1,'x':grip['x'],'y':grip['y']-60})
            assert cdp.evaluate("document.querySelector('#timelinePanel').getBoundingClientRect().height>window.__trackHeight+40")
            cdp.evaluate("window.__panTime=video.currentTime;state.waveSpan=.5;state.waveStart=0;drawWave();document.querySelector('#wavePan').scrollLeft=document.querySelector('#wavePan').clientWidth")
            cdp.wait("Math.abs(waveWindow().start-.5)<.01")
            assert cdp.evaluate("Math.abs(video.currentTime-window.__panTime)<.02 && document.querySelector('#wavePan').scrollWidth>document.querySelector('#wavePan').clientWidth*2")
            results.append({'name':'plain_wheel_zoom_drag_resize_and_proportional_viewport_scrollbar','passed':True})
            cdp.evaluate("state.cues=[normalizeCue({start:.1,end:.2,text:'前'},0),normalizeCue({start:.3,end:.8,text:'一二三四五六七八九十甲乙丙丁'},1),normalizeCue({start:.9,end:1.4,text:'后'},2)];state.initialCues=JSON.parse(JSON.stringify(state.cues));state.filter='all';state.search='';state.settings.width=6;syncSettingsUI();state.selectedCues=new Set();renderAll();selectCue(0,false)")
            cdp.evaluate("document.querySelector('#cueBody tr[data-i=\"1\"] .cue-text').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,cancelable:true,ctrlKey:true}));document.querySelector('#cueBody tr[data-i=\"1\"] .cue-text').dispatchEvent(new MouseEvent('click',{bubbles:true,ctrlKey:true}))")
            assert cdp.evaluate("selectedCueIndices().join(',')==='0,1' && Math.abs(video.currentTime-.32)<.02 && !document.querySelector('#btnMerge').disabled")
            cdp.click('#btnMerge')
            assert cdp.evaluate("state.cues.length===2 && state.cues[0].text.startsWith('前一二') && state.cues[1].text==='后'")
            cdp.click('#btnUndo')
            cdp.click('#cueBody tr[data-i="0"] .cue-pick')
            cdp.click('#cueBody tr[data-i="1"] .cue-pick')
            cdp.evaluate("state.cues[0].end=.21;completeModification(state.cues[0]);state.cues[1].text='壹'+state.cues[1].text.slice(1);completeModification(state.cues[1]);renderAll()")
            assert cdp.evaluate("state.cues.slice(0,2).every(hasCompletedModification)&&!hasCompletedModification(state.cues[2])&&!document.querySelector('#btnComplete')")
            cdp.evaluate("state.filter='low';renderTable()")
            assert cdp.evaluate("document.querySelectorAll('#cueBody tr').length===1 && document.querySelector('#cueBody tr').dataset.i==='2'")
            cdp.evaluate("state.filter='all';renderTable();selectCue(1,false)")
            cdp.click('#btnSegment')
            cdp.wait("!state.operation && state.cues.length>3")
            assert cdp.evaluate("state.cues[1].display_id==='2.1' && state.cues[2].display_id==='2.2' && state.cues[0].text==='前' && state.cues.at(-1).text==='后'")
            cdp.click('#btnReset')
            cdp.wait("!state.operation && state.cues.length===3")
            assert cdp.evaluate("state.cues.every(c=>!c.review_complete) && state.cues[1].start===.3 && state.cues[1].text==='一二三四五六七八九十甲乙丙丁'")
            cdp.click('#btnAddCue')
            assert cdp.evaluate("state.cues.at(-1).text==='' && state.cues.length===4")
            cdp.click('#btnUndo')
            assert cdp.evaluate("state.cues.length===3 && !document.querySelector('#cueBody .review-actions') && document.querySelector('#btnExport').closest('.cue-search-row')!==null")
            results.append({'name':'ctrl_latest_seek_merge_split_modified_done_reset_blank_add_and_undo','passed':True})
            cdp.evaluate("window.__confirmOriginal=window.confirmDialog;window.__confirmCount=0;window.confirmDialog=async()=>{window.__confirmCount++;return true};state.settings.width=9;syncSettingsUI();state.cues[0].text='恢复测试';saveAll()")
            cdp.evaluate("loadMedia(" + json.dumps(fixture['audio']) + ")")
            assert cdp.evaluate("window.__confirmCount===1 && state.cues[0].text==='恢复测试' && state.settings.width===9 && state.initialCues.length===3")
            cdp.evaluate("window.confirmDialog=async()=>{window.__confirmCount++;return false}")
            cdp.evaluate("loadMedia(" + json.dumps(fixture['audio']) + ")")
            assert cdp.evaluate("window.__confirmCount===2 && state.cues.length===0 && state.subtitleFile===null && state.initialCues===null")
            cdp.evaluate("window.confirmDialog=window.__confirmOriginal")
            results.append({'name':'reimport_prompts_to_restore_previous_settings_and_generated_subtitles_or_start_fresh','passed':True})
            cdp.evaluate("restoreSnapshot(window.__workspaceBefore.cues);state.settings=window.__workspaceBefore.settings;state.script=window.__workspaceBefore.script;state.scriptDocument=window.__workspaceBefore.doc;syncSettingsUI();setTimelineHeight(window.__trackHeight);state.waveSpan=0;state.waveStart=0;renderAll()")
            cdp.evaluate("hideTip();toastEl.classList.remove('show');document.querySelector('.left').scrollTop=0;document.querySelector('.right').scrollTop=0")
            cdp.evaluate("applyTheme('dark',false)")
            time.sleep(.3)
            image = cdp.call('Page.captureScreenshot', {'format':'png'})
            (args.output/'界面_反向纠错.png').write_bytes(base64.b64decode(image['data']))
            cdp.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]})
            assert cdp.evaluate("reduceMotion() && getComputedStyle(document.querySelector('.export-panel')).animationName==='none'")
            cdp.call("Emulation.setEmulatedMedia", {"features": []})
            cdp.evaluate("hideTip();toastEl.classList.remove('show')")
            time.sleep(.35)
            results.append({"name": "reduced_motion_disables_decorative_animation", "passed": True})
            cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 720, "deviceScaleFactor": 1, "mobile": False})
            assert cdp.evaluate("(()=>{const center=document.querySelector('.center');center.scrollTop=center.scrollHeight;const bottom=document.querySelector('.statusbar').getBoundingClientRect().bottom;return bottom<=721 && document.querySelector('#wave').getBoundingClientRect().height>40})()")
            cdp.evaluate("document.querySelector('.center').scrollTop=0")
            assert cdp.evaluate("document.querySelector('.cues').getBoundingClientRect().width<=document.querySelector('.right').clientWidth")
            image = cdp.call("Page.captureScreenshot", {"format": "png"})
            (args.output / "界面_1280x720.png").write_bytes(base64.b64decode(image["data"]))
            results.append({"name": "compact_desktop_all_controls_reachable", "passed": True})
            cdp.call("Emulation.setDeviceMetricsOverride", {"width": 760, "height": 900, "deviceScaleFactor": 1, "mobile": False})
            assert cdp.evaluate("getComputedStyle(document.querySelector('.left')).display!=='none' && getComputedStyle(document.querySelector('.right')).display!=='none'")
            image = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
            (args.output / "界面_窄屏.png").write_bytes(base64.b64decode(image["data"]))
            results.append({"name": "small_screen_entry_visible", "passed": True})
            cdp.evaluate("setMode('align');clearScript();state.cues=[normalizeCue({text:'我们明天去公园',start:.1,end:.7},0),normalizeCue({text:'今晚一起回家',start:.8,end:1.4},1)];renderAll();(()=>{const dt=new DataTransfer();dt.items.add(new File(['我们，明天去公园。'],'第一份.txt'));dt.items.add(new File(['今晚，一起回家！'],'第二份.txt'));document.querySelector('#scriptZone').dispatchEvent(new DragEvent('drop',{dataTransfer:dt,bubbles:true,cancelable:true}))})()")
            cdp.wait("!state.scriptUploading && state.scriptNames.length===2")
            assert cdp.evaluate("state.scriptDocument.text==='我们，明天去公园。\\n今晚，一起回家！' && document.querySelector('#scriptStats').textContent==='第二份.txt ×2' && document.querySelector('#scriptStats').title==='1. 第一份.txt\\n2. 第二份.txt'")
            assert cdp.evaluate("matchScript(true)")
            cdp.evaluate("state.settings.keepPunc=false;syncSettingsUI();renderTable()")
            assert cdp.evaluate("[...document.querySelectorAll('.sugg-text')].every(e=>!/[，。！]/.test(e.textContent)) && state.scriptDocument.text.includes('，') && state.cues[0].script_text.includes('，')")
            cdp.evaluate("state.settings.keepPunc=true;syncSettingsUI();renderTable()")
            assert cdp.evaluate("document.querySelector('.sugg-text').textContent.includes('我们，明天去公园。')")
            assert cdp.evaluate("(async()=>{await saveAll();const p=(await api('/api/projects/load?path='+encodeURIComponent(projectPath()))).project;return p.script_names.join('|')==='第一份.txt|第二份.txt' && p.script_document.text===state.scriptDocument.text && p.script_name==='第二份.txt'})()")
            results.append({'name':'multi_file_drag_continuous_match_latest_name_count_punctuation_toggle_and_persistence','passed':True})
            cdp.evaluate("setMode('align');replaceCues(Array.from({length:40},(_,i)=>({start:i*.03,end:i*.03+.02,text:'用于滚动验证的字幕'})),{reset:true});document.querySelector('.right .table-wrap').scrollTop=350")
            assert cdp.evaluate("document.querySelectorAll('.cue-column-head').length===0 && document.querySelectorAll('#cueBody .field-caption').length===0")
            cdp.evaluate("state.operation='测试锁定';updateButtons()")
            assert cdp.evaluate("['vadCheck','arabicNumbers','keepPunc','wrapSubtitles','subtitleLead','subtitleTail','subtitleMin','subtitleMax','gapInput'].every(id=>document.getElementById(id).disabled)")
            cdp.evaluate("state.operation='';updateButtons()")
            assert cdp.evaluate("document.querySelector('#vadCheck').disabled && !document.querySelector('#keepPunc').disabled")
            cdp.evaluate("setMode('extract');replaceCues([{start:.1,end:1.1,text:'你好，走吧！',words:[{word:'你好',start:.1,end:.3},{word:'走吧',start:.8,end:1.1}]}],{reset:true});document.querySelector('#gapInput').value='.4';document.querySelector('#gapInput').dispatchEvent(new Event('change',{bubbles:true}))")
            cdp.wait("!state.operation && state.cues.length===2")
            assert cdp.evaluate("!document.querySelector('#vadCheck').disabled && state.cues[0].end===.3 && state.cues[1].start===.8 && state.history.length===1")
            cdp.evaluate("Object.assign(state.settings,{subtitleLead:1,subtitleTail:1,subtitleMin:3});syncSettingsUI();renderAll()")
            assert cdp.evaluate("presentationCues()[0].end===.3 && presentationCues()[1].start===.8")
            cdp.evaluate("Object.assign(state.settings,{width:6,wrapSubtitles:false});state.cues[0].text='一二三四五六七八九十';syncSettingsUI();renderAll();video.currentTime=.15;updateOverlay()")
            assert cdp.evaluate("!overlay.textContent.includes('\\n')")
            cdp.evaluate("state.settings.wrapSubtitles=true;syncSettingsUI();updateOverlay();document.querySelector('.right .table-wrap').scrollTop=0;hideTip();toastEl.classList.remove('show')")
            assert cdp.evaluate("overlay.textContent.includes('\\n')")
            cdp.call('Emulation.setDeviceMetricsOverride',{'width':1536,'height':1000,'deviceScaleFactor':1,'mobile':False})
            (args.output/'界面_固定列头与停顿.png').write_bytes(base64.b64decode(cdp.call('Page.captureScreenshot',{'format':'png'})['data']))
            results.append({'name':'sticky_single_column_header_global_disabled_controls_pause_change_and_protected_silence','passed':True})
            cdp.evaluate("setMode('align');clearScript();replaceCues([{start:.1,end:.9,text:'我们明天去公园散步'},{start:1.1,end:1.8,text:'今天晚上一起回家吃饭'}],{reset:true});(()=>{const dt=new DataTransfer();dt.items.add(new File(['今天晚上一起回家吃饭。😀'],'B.txt'));dt.items.add(new File(['我们明天去公园散步。'],'A.txt'));document.querySelector('#scriptZone').dispatchEvent(new DragEvent('drop',{dataTransfer:dt,bubbles:true,cancelable:true}))})()")
            cdp.wait("!state.scriptUploading && state.scriptNames.length===2")
            assert cdp.evaluate("matchScript(true)")
            assert cdp.evaluate("state.scriptDocument.sources.map(s=>s.name).join('|')==='B.txt|A.txt' && state.cues.map(c=>c.reference_match.reference.source_name).join('|')==='A.txt|B.txt' && state.cues[0].reference_match.reference.start>state.cues[1].reference_match.reference.start")
            assert cdp.evaluate("(async()=>{await saveAll();const p=(await api('/api/projects/load?path='+encodeURIComponent(projectPath()))).project;return p.script_document.sources.length===2 && p.script_document.sources[1].name==='A.txt'})()")
            cdp.evaluate("state.cues[0].words=[{word:'我们明天',start:.1,end:.3},{word:'去公园散步',start:.6,end:.9}];state.settings.gap=.2")
            cdp.evaluate("applyPauseSetting()")
            assert cdp.evaluate("state.cues.length===3 && state.cues[0].reference_match.inherited && state.cues[1].reference_match.inherited && state.cues[0].reference_match.reference.end===state.cues[1].reference_match.reference.start")
            cdp.evaluate("state.selectedCues=new Set([0,1]);doMerge()")
            assert cdp.evaluate("state.cues.length===2 && state.cues[0].reference_match.reference.source_name==='A.txt'")
            assert cdp.evaluate("document.querySelector('#cueBody .match-alternatives summary').textContent.includes('A.txt')")
            cdp.evaluate("document.querySelector('.right .table-wrap').scrollTop=0;hideTip();toastEl.classList.remove('show')")
            (args.output/'界面_乱序剧本来源.png').write_bytes(base64.b64decode(cdp.call('Page.captureScreenshot',{'format':'png'})['data']))
            cdp.evaluate("setMode('extract')")
            assert cdp.evaluate("getComputedStyle(document.querySelector('#cueBody .suggest')).display==='none' && getComputedStyle(document.querySelector('#cueBody .cue-review')).display==='none'")
            results.append({'name':'unordered_B_A_import_A_B_video_sources_persist_split_inherits_merge_rematches_extract_hides_reference','passed':True})
            assert cdp.evaluate("(async()=>{setMode('align');clearScript();window.__pickerFiles=[];for(const [name,text] of [['多选甲.txt','第一份剧本内容。'],['多选乙.txt','第二份剧本内容。']])window.__pickerFiles.push((await uploadFile(new File([text],name),'script')).path);const original=api;api=async(path,...args)=>path==='/api/dialog'?{paths:window.__pickerFiles}:original(path,...args);try{await pickScript()}finally{api=original}return state.scriptNames.join('|')==='多选甲.txt|多选乙.txt'&&state.script.length===2&&state.scriptDocument.sources.length===2&&state.scriptDocument.text==='第一份剧本内容。\\n第二份剧本内容。'&&!state.scriptPickerOpen})()")
            results.append({'name':'multi_select_picker_real_file_parsing_keeps_both_sources_and_nonzero_content','passed':True})
            assert cdp.evaluate("(async()=>{await saveAll();const before=JSON.stringify({script:state.script,document:state.scriptDocument,names:state.scriptNames,cues:state.cues,history:state.history,redo:state.redoStack});const original=api;api=async(path,body,...args)=>path==='/api/dialog'?{paths:window.__pickerFiles}:path==='/api/script/parse'&&body.documents?{ok:true,lines:[],document:{version:1,text:''},stats:{total:0}}:original(path,body,...args);try{await pickScript()}finally{api=original}return before===JSON.stringify({script:state.script,document:state.scriptDocument,names:state.scriptNames,cues:state.cues,history:state.history,redo:state.redoStack})&&!state.scriptPickerOpen&&toastEl.textContent.includes('重启字幕工作室')&&!toastEl.textContent.includes('已导入：0')})()")
            results.append({'name':'old_backend_empty_success_preserves_existing_scripts_and_prompts_restart','passed':True})
            diag = cdp.evaluate("(async()=>{const parsed=await api('/api/script/parse',{text:'录音在我这里。\\n正确台词。'});clearScript();await applyScriptResult(parsed,'提醒回归.txt');replaceCues([{start:.1,end:.8,text:'录音在我这里',words:[{word:'里',start:.9,end:.8}],alignment:{engine:'fixture'}},{start:.9,end:1.3,text:'正确台词',is_noise:true,needs_review:true},{start:1.4,end:1.8,text:'完全无关的内容'}],{reset:true});if(!await matchScript(true))return 'matchScript false';const rows=[...document.querySelectorAll('#cueBody tr')];const verifyOk=state.cues.length===3&&state.cues[0].text==='录音在我这里'&&state.cues[0].words.length===0&&!!state.cues[0].word_timing_error&&rows[0].querySelector('.badge.noise')?.textContent==='明显问题'&&rows[1].querySelector('.badge.verified')?.textContent==='参考一致'&&!rows[1].querySelector('.badge.noise')&&rows[2].querySelector('.badge.noise')?.textContent==='明显问题';state.cues[0].verification_stage='align';renderAll();const aligned=[...document.querySelectorAll('#cueBody tr')];const ok=verifyOk&&aligned[0].querySelector('.badge.noise')?.textContent==='明显问题';if(!ok){const hex=s=>[...String(s||'')].map(c=>c.codePointAt(0).toString(16)).join(',');return JSON.stringify({verifyOk,badges:rows.map(r=>hex(r.querySelector('.badges')?.textContent)),aligned:hex(aligned[0].querySelector('.badges')?.textContent),stage:state.cues[0].verification_stage,words:state.cues[0].words?.length,wte:!!state.cues[0].word_timing_error,status:state.cues.map(c=>c.reference_match?.status),stages:state.cues.map(c=>c.verification_stage),labels:state.cues.map(c=>hex(computeCueVerification(c)?.label))});}window.__dirtyTrail=[];const originalSchedule=scheduleSave;scheduleSave=function(){window.__dirtyTrail.push(new Error().stack);return originalSchedule.apply(this,arguments)};const saved=await saveAll();scheduleSave=originalSchedule;return saved?'OK':JSON.stringify({dirty:state.dirty,conflict:projectConflicts.has(projectPath()),save:document.querySelector('#topSaveState')?.textContent,toast:toastEl.textContent,revision:state.editRevision,trail:window.__dirtyTrail,versions:[...projectVersions.entries()]})})()")
            assert diag == 'OK', diag
            diag = cdp.evaluate("""(async()=>{
              setMode('align');clearScript();state.settings.width=20;syncSettingsUI();
              await applyScriptResult(await api('/api/script/parse',{text:'我们明天去公园散步。'}),'改字回归.txt');
              replaceCues([{start:.1,end:.9,text:'我们明天去公圆散步'}],{reset:true});
              if(!await matchScript(true))return 'matchScript false';
              let row=document.querySelector('#cueBody tr[data-i="0"]');
              if(row.querySelector('.badge.low')?.textContent!=='疑似问题')return 'setup badge '+row.querySelector('.badges')?.textContent;
              let cell=row.querySelector('.cue-text');
              cell.focus();cell.innerText='我们明天去公园散步';cell.dispatchEvent(new Event('input',{bubbles:true}));cell.blur();
              row=document.querySelector('#cueBody tr[data-i="0"]');
              if(row.querySelector('.badge.modified-done')?.textContent!=='文字已确认')return 'edit1 badge '+row.querySelector('.badges')?.textContent;
              if(row.querySelector('.badge.low')||row.querySelector('.badge.noise'))return 'old problems still visible';
              if(state.cues[0].modification_complete?.text!==state.cues[0].text)return 'edit1 snapshot';
              const editedText=state.cues[0].text;
              document.querySelector('#btnRealignAll').click();
              for(let i=0;i<200&&(state.jobId||state.operation);i++)await new Promise(r=>setTimeout(r,100));
              if(state.jobId||state.operation)return 'align did not finish';
              document.querySelector('#verificationReportDialog').close();
              row=document.querySelector('#cueBody tr[data-i="0"]');
              if(state.cues[0].text!==editedText)return 'align changed text ['+editedText+'] -> ['+state.cues[0].text+'] count '+state.cues.length+' | '+state.cues.map(c=>c.text).join(' | ');
              if(Math.abs(state.cues[0].start-.12)>1e-6||Math.abs(state.cues[0].end-.92)>1e-6)return 'align time '+state.cues[0].start+'-'+state.cues[0].end;
              if(state.cues[0].verification_stage!=='align')return 'stage '+state.cues[0].verification_stage;
              if(row.querySelector('.badge.modified-done')?.textContent!=='文字已确认')return 'align badge '+row.querySelector('.badges')?.textContent;
              if(row.querySelector('.badge.noise'))return 'align made it obvious';
              cell=row.querySelector('.cue-text');
              cell.focus();cell.innerText='我们明天去公园里散步';cell.dispatchEvent(new Event('input',{bubbles:true}));cell.blur();
              row=document.querySelector('#cueBody tr[data-i="0"]');
              if(row.querySelector('.badge.modified-done')?.textContent!=='文字已确认')return 'edit2 badge '+row.querySelector('.badges')?.textContent;
              if(state.cues[0].modification_complete?.text!==state.cues[0].text)return 'edit2 snapshot';
              if(!await matchScript(true))return 'recheck false';
              row=document.querySelector('#cueBody tr[data-i="0"]');
              if(state.cues[0].verification_stage!=='verify')return 'recheck stage';
              if(state.cues[0].modification_complete!==false)return 'recheck kept confirmation';
              const label=computeCueVerification(state.cues[0])?.label;
              if(label!=='疑似问题'&&label!=='明显问题')return 'recheck label '+label;
              if(row.querySelector('.badge.modified-done'))return 'recheck still modified-done';
              return await saveAll()?'OK':'saveAll false';
            })()""")
            assert diag == 'OK', diag
            cdp.evaluate("document.querySelector('.right .table-wrap').scrollTop=0;hideTip();toastEl.classList.remove('show')")
            (args.output/'界面_校验提醒.png').write_bytes(base64.b64decode(cdp.call('Page.captureScreenshot',{'format':'png'})['data']))
            results.append({'name':'damaged_words_match_and_save_succeeds_pass_and_problem_badges_differ','passed':True})
            assert cdp.evaluate("(async()=>{let writes=0;const original=api,before=toastEl.textContent;api=async(path,...args)=>{if(path==='/api/dialog')return {path:'',paths:[]};if(path==='/api/srt/export')writes++;return original(path,...args)};try{const result=await exportSrt(false);return result===null&&writes===0&&toastEl.textContent===before&&!state.operation}finally{api=original}})()")
            results.append({'name':'export_directory_selection_cancel_does_not_write_or_show_success','passed':True})
            cdp.evaluate("state.filter='all';state.search='';replaceCues([{start:.1,end:.4,text:'第一句'},{start:.6,end:.9,text:'第二句',exclude_export:true},{start:1.1,end:1.4,text:'第三句'}],{reset:true});state.search='第一';renderAll()")
            cdp.click('#selectAllCues')
            assert cdp.evaluate("selectedCueIndices().join(',')==='0,1,2'&&state.cues[1].exclude_export&&document.querySelector('#selectAllCues').checked&&document.querySelectorAll('#cueBody tr').length===1&&!document.querySelector('#btnDelete').disabled")
            cdp.evaluate("state.search='';document.querySelector('#searchInput').value='';renderAll();hideTip();toastEl.classList.remove('show')")
            (args.output/'界面_全选批量编辑.png').write_bytes(base64.b64decode(cdp.call('Page.captureScreenshot',{'format':'png'})['data']))
            assert cdp.evaluate("!document.querySelector('#btnComplete')&&!document.querySelector('#btnAdoptSelected')&&!document.querySelector('#btnRematchAll')&&state.cues[1].exclude_export")
            cdp.evaluate("state.selectedCues=new Set([0,2]);renderTable();updateButtons()")
            cdp.click('#btnDelete')
            assert cdp.evaluate("state.cues.length===1&&state.cues[0].text==='第二句'")
            cdp.click('#btnUndo')
            assert cdp.evaluate("state.cues.length===3")
            cdp.evaluate("state.search='';document.querySelector('#searchInput').value='';renderAll();state.selectedCues=new Set([0,1,2]);updateButtons()")
            cdp.click('#btnDelete')
            assert cdp.evaluate("state.cues.length===0")
            cdp.click('#btnUndo')
            results.append({'name':'global_select_all_includes_excluded_without_changing_export_flags_and_batch_edit_undo','passed':True})
            assert cdp.evaluate("(async()=>{state.settings.width=20;syncSettingsUI();const parsed=await api('/api/script/parse',{text:'我们明天去公园散步。\\n今天晚上一起回家吃饭。'});clearScript();await applyScriptResult(parsed,'批量参考.txt');replaceCues([{start:.1,end:.4,text:'我们明天去公圆散步',script_text:'我们明天去公园散步。',reference_match:{status:'homophone',query_text:'我们明天去公圆散步',reference:{text:'我们明天去公园散步。'}}},{start:.6,end:.9,text:'今天晚上一起回家吃犯',script_text:'今天晚上一起回家吃饭。',reference_match:{status:'homophone',query_text:'今天晚上一起回家吃犯',reference:{text:'今天晚上一起回家吃饭。'}}}],{reset:true});selectAllCues(true);return !document.querySelector('#btnCalibrate').disabled})()")
            cdp.click('#btnCalibrate')
            cdp.wait("!state.operation&&document.querySelector('#verificationReportDialog').open")
            assert cdp.evaluate("state.cues[0].text==='我们明天去公园散步。'&&state.cues[1].text==='今天晚上一起回家吃饭。'&&state.history.length===1&&state.cues[0].asr_original.text==='我们明天去公圆散步'&&document.querySelectorAll('.badge.modified-done').length===2")
            cdp.evaluate("document.querySelector('#verificationReportDialog').close()")
            cdp.click('#btnUndo')
            results.append({'name':'one_click_calibration_preserves_originals_shows_verified_labels_and_undoes_once','passed':True})
            cdp.evaluate("setMode('extract');state.script=[];state.scriptDocument=null;replaceCues([{start:.1,end:.5,text:'选中甲'},{start:.6,end:.8,text:'保留中间'},{start:1.,end:1.5,text:'选中乙'}],{reset:true});state.selectedCues=new Set([0,2]);renderAll();window.__batchMiddle=JSON.stringify(state.cues[1])")
            cdp.click('#btnRerunSelected')
            assert cdp.evaluate("state.batchRecognition&&document.querySelector('#selectAllCues').disabled&&[...document.querySelectorAll('.cue-pick')].every(input=>input.disabled)")
            cdp.wait("!state.batchRecognition&&!state.jobId&&jobMsg.textContent.includes('2/2')")
            assert cdp.evaluate("state.history.length===1&&JSON.stringify(state.cues.find(c=>c.text==='保留中间'))===window.__batchMiddle")
            cdp.click('#btnUndo')
            assert cdp.evaluate("state.cues.map(c=>c.text).join('|')==='选中甲|保留中间|选中乙'")
            cdp.evaluate("replaceCues(Array.from({length:11},(_,i)=>({start:i*.1,end:i*.1+.08,text:'短句'+i})),{reset:true});selectAllCues(true)")
            assert cdp.evaluate("!document.querySelector('#btnRerunSelected').disabled&&selectedCueIndices().length===11")
            results.append({'name':'batch_region_recognition_preserves_unselected_handles_two_jobs_and_no_ten_limit','passed':True})
            assert cdp.evaluate("(async()=>{replaceCues([{start:.123,end:.456,text:'甲'},{start:.789,end:1.234,text:'乙'},{start:1.5,end:1.8,text:'丙',exclude_export:true}],{reset:true});state.selectedCues=new Set([0]);state.search='甲';Object.assign(state.settings,{subtitleLead:1,subtitleTail:1,subtitleMin:5,subtitleMax:.1});syncSettingsUI();renderAll();const result=await exportSrt(false);if(!result)return false;const parsed=await api('/api/srt/parse',{path:result.path});if(!(parsed.cues.length===2&&parsed.cues[0].text==='甲'&&parsed.cues[0].start===.123&&parsed.cues[0].end===.456&&parsed.cues[1].text==='乙'&&parsed.cues[1].start===.789&&parsed.cues[1].end===1.234))return false;selectAllCues(true);const second=await exportSrt(false);const again=await api('/api/srt/parse',{path:second.path});return selectedCueIndices().length===3&&state.cues[2].exclude_export&&again.cues.length===2&&again.cues.every((c,i)=>c.text===parsed.cues[i].text&&c.start===parsed.cues[i].start&&c.end===parsed.cues[i].end)})()")
            results.append({'name':'real_srt_exports_all_nonexcluded_with_exact_current_millisecond_times','passed':True})
            assert cdp.evaluate("(async()=>{setMode('align');state.search='';state.settings.width=20;syncSettingsUI();clearScript();await applyScriptResult(await api('/api/script/parse',{text:'我们明天一起去公园散步。\\n请你现在把门口快递拿回来。'}),'当前场景.txt');await applyScriptResult(await api('/api/script/parse',{text:'感谢各位观众朋友一直以来的支持和陪伴。'}),'其他场景.txt');replaceCues([{start:.1,end:.5,text:'我们明天一起去公园散步'},{start:.6,end:.9,text:'感谢各位观众朋友一直以来的支持和陪伴'},{start:1.1,end:1.6,text:'请你现在把门口快递拿回来'}],{reset:true});if(!await matchScript(true))return false;const row=document.querySelector('#cueBody tr[data-i=\"1\"]');return state.cues[1].reference_match.status==='context_mismatch'&&state.cues[1].reference_match.alternatives.length>0&&!state.cues[1].exclude_export&&row.querySelector('.badge.noise').textContent==='明显问题'&&row.querySelector('.sugg-text').textContent==='当前片段无此台词'&&getComputedStyle(row.querySelector('.cue-text')).color===getComputedStyle(row.querySelector('.badge.noise')).color})()")
            assert cdp.evaluate("(async()=>{const before=JSON.stringify([state.cues[0],state.cues[2]]);selectCue(1,false);await rematchDerivedCues([1]);return state.cues[1].reference_match.status==='context_mismatch'&&!canAdoptCue(state.cues[1])&&JSON.stringify([state.cues[0],state.cues[2]])===before})()")
            cdp.evaluate("document.querySelector('.right .table-wrap').scrollTop=0;hideTip();toastEl.classList.remove('show')")
            (args.output/'界面_上下文多余台词.png').write_bytes(base64.b64decode(cdp.call('Page.captureScreenshot',{'format':'png'})['data']))
            results.append({'name':'global_exact_hit_outside_local_context_is_red_and_selected_check_keeps_neighbors','passed':True})
            cdp.evaluate("clearScript();state.settings.width=12;syncSettingsUI();replaceCues([{start:.1,end:1.1,text:'谁能把黎红拉进自己的战队奖金五十万'},{start:1.3,end:1.8,text:'原来的下一条保持完整'}],{reset:true});window.__splitOriginal=state.cues[0].text;window.__splitNext=JSON.stringify(state.cues[1]);selectCue(0,false)")
            cdp.click('#btnSegment')
            cdp.wait("!state.operation&&state.cues.length===3")
            assert cdp.evaluate("state.cues[0].text+state.cues[1].text===window.__splitOriginal&&Math.abs(state.cues[0].text.length-state.cues[1].text.length)<=4&&state.cues[0].manual_split&&state.cues[1].manual_split&&JSON.stringify(state.cues[2])===window.__splitNext&&state.cues[0].start===.1&&state.cues[1].end===1.1")
            results.append({'name':'midpoint_splits_real_text_balanced_independent_without_appending_to_next','passed':True})
            assert cdp.evaluate("(async()=>{Object.assign(state.settings,{keepPunc:false,subtitleLead:0,subtitleTail:0,subtitleMin:0,subtitleMax:0,width:20});replaceCues([{start:.1,end:.8,text:'你好，世界？！。',script_text:'你好，世界？！。'}],{reset:true});syncSettingsUI();renderAll();video.currentTime=.3;updateOverlay();if(document.querySelector('.cue-text').textContent!=='你好世界'||document.querySelector('.sugg-text').textContent!=='你好世界'||overlay.textContent!=='你好世界'||state.cues[0].text!=='你好，世界？！。')return false;let file=await exportSrt(false),parsed=await api('/api/srt/parse',{path:file.path});if(parsed.cues[0].text!=='你好世界')return false;state.settings.keepPunc=true;syncSettingsUI();renderAll();file=await exportSrt(false);parsed=await api('/api/srt/parse',{path:file.path});if(parsed.cues[0].text!=='你好，世界？！。'||document.querySelector('.cue-text').textContent!=='你好，世界？！。')return false;state.settings.keepPunc=false;syncSettingsUI();renderAll();const cell=document.querySelector('.cue-text');cell.focus();cell.innerText='您好世界';cell.dispatchEvent(new Event('input',{bubbles:true}));cell.blur();if(state.cues[0].text!=='您好，世界？！。'||state.cues[0].asr_original.text!=='你好，世界？！。'||cell.textContent!=='您好世界')return false;state.settings.keepPunc=true;syncSettingsUI();renderAll();return document.querySelector('.cue-text').textContent==='您好，世界？！。'})()")
            results.append({'name':'punctuation_toggle_controls_editor_reference_preview_real_srt_and_preserves_hidden_marks_on_edit','passed':True})
            assert cdp.evaluate("[...document.querySelectorAll('.cue-edit-tools button')].map(b=>b.textContent).join('|')==='合并|分段|添加|删除|撤销|重置'&&[...document.querySelectorAll('.cue-process-tools button')].filter(b=>!b.hidden&&getComputedStyle(b).display!=='none').map(b=>b.textContent).join('|')==='校准字幕|对齐字幕'&&document.querySelector('.cue-process-tools').getBoundingClientRect().top<document.querySelector('.cue-edit-tools').getBoundingClientRect().top&&!document.querySelector('#btnComplete')&&!document.querySelector('#btnAdoptSelected')&&!document.querySelector('#btnRematchAll')&&document.querySelector('#selectAllCues').closest('.right-head')!==null")
            assert cdp.evaluate("[...document.querySelectorAll('.cue-edit-tools button')].every(b=>b.getBoundingClientRect().height>=30&&parseFloat(getComputedStyle(b).fontSize)>=12)")
            cdp.evaluate("setMode('extract');clearScript();replaceCues([{start:.1,end:.5,text:'重识目标'},{start:1.,end:1.5,text:'保留字幕'}],{reset:true});selectCue(0,false)")
            assert cdp.evaluate("!document.querySelector('#btnExtract').hidden&&!document.querySelector('#btnRerunSelected').hidden&&!document.querySelector('#btnRerunSelected').disabled&&document.querySelector('#btnRealignAll').hidden&&document.querySelector('#btnCalibrate').hidden")
            cdp.click('#btnRerunSelected')
            cdp.wait("!state.batchRecognition&&!state.jobId&&jobMsg.textContent.includes('1/1')")
            assert cdp.evaluate("state.cues.some(c=>c.text==='保留字幕')")
            cdp.evaluate("setMode('align');window.__confirmBeforeRealign=confirmDialog;window.__alignPrompts=0;confirmDialog=async()=>{window.__alignPrompts++;return true};window.__beforeRealignText=state.cues.map(c=>c.text).join('|');window.__beforeRealignMatches=JSON.stringify(state.cues.map(c=>c.reference_match??null))")
            cdp.click('#btnRealignAll')
            cdp.wait("!state.jobId&&!state.operation&&state.cues.every(c=>c.alignment)")
            assert cdp.evaluate("window.__alignPrompts===0&&state.cues.map(c=>c.text).join('|')===window.__beforeRealignText&&JSON.stringify(state.cues.map(c=>c.reference_match??null))===window.__beforeRealignMatches")
            assert cdp.evaluate("document.querySelector('#verificationReportDialog').open&&document.querySelector('#reportAfterPassed').textContent!==''")
            cdp.evaluate("document.querySelector('#verificationReportDialog').close();confirmDialog=window.__confirmBeforeRealign")
            results.append({'name':'two_row_toolbar_has_larger_buttons_recognition_in_both_modes_and_global_realign','passed':True})
            assert cdp.evaluate("(async()=>{replaceCues([{start:.1,end:.5,text:'一般疑点',reference_match:{status:'unmatched',query_text:'一般疑点',label:'无参考内容'}},{start:.7,end:1.2,text:'明显问题',reference_match:{status:'context_mismatch',query_text:'明显问题',warning:'上下文明确不符'}}],{reset:true});renderAll();const rows=[...document.querySelectorAll('#cueBody tr')];if(rows[0].querySelector('.badge.low')?.textContent!=='疑似问题'||rows[1].querySelector('.badge.noise')?.textContent!=='明显问题')return false;const cell=rows[1].querySelector('.cue-text');cell.focus();cell.innerText='已修正文本';cell.dispatchEvent(new Event('input',{bubbles:true}));cell.blur();const badges=rows[1].querySelector('.badges');return badges.querySelector('.modified-done')?.textContent==='文字已确认'&&!badges.textContent.includes('疑似问题')&&!badges.textContent.includes('明显问题')&&!badges.textContent.includes('待重新校验')&&!badges.textContent.includes('已完成')})()")
            cdp.evaluate("hideTip();toastEl.classList.remove('show');document.querySelector('.right .table-wrap').scrollTop=0")
            (args.output/'界面_两排工具与提醒.png').write_bytes(base64.b64decode(cdp.call('Page.captureScreenshot',{'format':'png'})['data']))
            results.append({'name':'yellow_suspect_red_obvious_and_green_modified_done_replace_old_labels','passed':True})
            assert cdp.evaluate("""(async()=>{
              setMode('align');clearScript();Object.assign(state.settings,{width:20,gap:.4,keepPunc:true,wrapSubtitles:false});
              await applyScriptResult(await api('/api/script/parse',{text:'中。毒的人。你去哪里？我回学校！'}),'语气参考.txt');
              replaceCues([{start:.1,end:.2,text:'中'},{start:.2,end:.5,text:'毒的人'},
                {start:.6,end:1.8,text:'你去哪里我回学校'},{start:1.85,end:2,text:'相邻保留'}],{reset:true});
              if(!await matchScript(true))return false;
              const before=snapshot(),neighbor=JSON.stringify(state.cues[3]);state.selectedCues=new Set([0,1,2]);
              await calibrateAll();
              if(!document.querySelector('#verificationReportDialog').open)return false;
              if(document.querySelector('#reportAfterPassed').textContent==='')return false;
              document.querySelector('#verificationReportDialog').close();
              if(state.cues.map(c=>c.text).join('|')!=='中。|毒的人。|你去哪里？我回学校！|相邻保留')return false;
              if(JSON.stringify(state.cues.at(-1))!==neighbor)return false;
              // 校准只复制参考文字，不再给出合并/拆条建议；合并仍由用户手动执行。
              if(state.cues[0].merge_suggestion||document.querySelector('#cueBody .badge.merge-suggest'))return false;
              state.selectedCues=new Set([0,1]);updateButtons();
              if(document.querySelector('#btnMerge').disabled)return false;
              await doMerge();
              if(state.cues.map(c=>c.text).join('|')!=='中。毒的人。|你去哪里？我回学校！|相邻保留')return false;
              if(state.cues[0].start!==.1||state.cues[0].end!==.5)return false;
              if(state.cues[0].asr_sources.map(c=>c.text).join('')!=='中毒的人'||state.cues[1].end>state.cues[2].start)return false;
              let file=await exportSrt(false),parsed=await api('/api/srt/parse',{path:file.path});
              if(parsed.cues.map(c=>c.text).join('|')!=='中。毒的人。|你去哪里？我回学校！|相邻保留')return false;
              state.settings.keepPunc=false;syncSettingsUI();renderAll();file=await exportSrt(false);parsed=await api('/api/srt/parse',{path:file.path});
              if(/[，。？！]/.test(parsed.cues.map(c=>c.text).join('')))return false;
              undo();undo();return snapshot()===before;
            })()"""), cdp.evaluate("JSON.stringify({cues:state.cues,toast:toastEl.textContent,history:state.history.length})")
            results.append({'name':'calibration_marks_fragment_red_then_multi_select_merge_joins_text_and_time','passed':True})
            cdp.evaluate("redo();state.settings.keepPunc=true;syncSettingsUI();renderAll();hideTip();toastEl.classList.remove('show');document.querySelector('.right .table-wrap').scrollTop=0")
            (args.output/'界面_单字合回与语气分句.png').write_bytes(base64.b64decode(cdp.call('Page.captureScreenshot',{'format':'png'})['data']))
            assert cdp.evaluate("""(async()=>{
              document.querySelectorAll('dialog[open]').forEach(d=>d.close());
              Object.assign(state.settings,{subtitleLead:0,subtitleTail:0,subtitleMin:0,subtitleMax:.8});
              replaceCues([{text:'确认台词',start:.1,end:1.5,words:[{word:'确认台词',start:.15,end:.6}]}],{reset:true});
              syncSettingsUI();renderAll();
              if(presentationCues()[0].end!==1.5)return 'raw preview';
              document.querySelector('#btnPreviewTiming').click();
              if(!state.timingPreview||Math.abs(presentationCues()[0].end-.9)>1e-6)return 'preview action';
              let result=await exportSrt(false),parsed=await api('/api/srt/parse',{path:result.path});
              if(parsed.cues[0].end!==1.5||document.querySelector('#exportResult').hidden)return 'unapplied export';
              if(!document.querySelector('#timingPreviewNote').textContent.includes('尚未应用'))return 'missing preview note';
              document.querySelector('#btnApplyTiming').click();
              if(state.timingPreview||state.cues[0].end!==.9||state.cues[0].asr_original.end!==1.5)return 'apply';
              result=await exportSrt(false);parsed=await api('/api/srt/parse',{path:result.path});
              if(parsed.cues[0].end!==.9||presentationCues()[0].end!==.9)return 'applied export';
              if(document.querySelector('#exportResultPath').textContent!==result.path)return 'export path';
              undo();if(state.cues[0].end!==1.5)return 'undo';
              return true;
            })()""") is True
            results.append({'name':'explicit_timing_preview_apply_export_and_undo_share_actual_times','passed':True})
            assert cdp.evaluate("""(()=>{
              Object.assign(state.settings,{subtitleLead:0,subtitleTail:0,subtitleMin:0,subtitleMax:0});
              replaceCues([{text:'确认台词',start:.1,end:1,edited:true,alignment:{engine:'fixture'},alignment_error:'无法定位音轨',verification_stage:'align'}],{reset:true});
              state.filter='low';state.search='';renderAll();
              const row=document.querySelector('#cueBody tr');
              if(!row?.querySelector('.badge.noise')||!row.querySelector('.badge.modified-done'))return false;
              if(cueStatistics(state.cues).aligned!==0||cueReportVerdict(state.cues[0])!=='obvious')return false;
              state.filter='all';renderAll();return true;
            })()""")
            results.append({'name':'confirmed_text_keeps_timing_errors_visible_in_badges_filter_and_statistics','passed':True})
            (args.output/'界面_时间状态与路径.png').write_bytes(base64.b64decode(cdp.call('Page.captureScreenshot',{'format':'png'})['data']))
            cdp.click('#btnHelp')
            help_sizes = []
            for width, height in ((1536, 1000), (1280, 720), (1024, 600), (800, 600), (390, 844)):
                cdp.call('Emulation.setDeviceMetricsOverride', {'width':width,'height':height,'deviceScaleFactor':1,'mobile':False})
                layout = cdp.evaluate("(()=>{const d=document.querySelector('#helpDialog'),r=d.getBoundingClientRect();return {scroll:d.scrollHeight>d.clientHeight+1,horizontal:d.scrollWidth>d.clientWidth+1,left:r.left,top:r.top,right:r.right,bottom:r.bottom}})()")
                assert not layout['scroll'] and not layout['horizontal'] and layout['left']>=0 and layout['top']>=0 and layout['right']<=width+1 and layout['bottom']<=height+1, (width,height,layout)
                help_sizes.append({'width':width,'height':height,**layout})
            cdp.call('Emulation.setDeviceMetricsOverride', {'width':1280,'height':720,'deviceScaleFactor':1,'mobile':False})
            (args.output/'界面_帮助速查.png').write_bytes(base64.b64decode(cdp.call('Page.captureScreenshot',{'format':'png'})['data']))
            (args.output/'帮助布局验证.json').write_text(json.dumps(help_sizes,ensure_ascii=False,indent=2),encoding='utf-8')
            cdp.evaluate("document.querySelector('#helpDialog').close()")
            cdp.call('Emulation.clearDeviceMetricsOverride')
            results.append({'name':'concise_help_fits_five_viewports_without_scrolling','passed':True})
            if args.benchmark:
                cdp.socket.settimeout(60)
                performance=cdp.evaluate("(()=>{state.script=[];state.scriptDocument={text:'剧本内容'.repeat(100000)};state.selected=-1;state.selectedCues=new Set();state.cues=Array.from({length:5000},(_,i)=>normalizeCue({start:i*.04,end:i*.04+.03,text:'这是用于五千条字幕性能检查的普通对白内容'},i));const a=performance.now();snapshot();const b=performance.now();renderTable();void cueBody.offsetHeight;const c=performance.now();const payload=JSON.stringify({cues:state.cues,document:state.scriptDocument});const d=performance.now();document.getElementById('subtitleTail').value='.2';const e=performance.now();commitSettings({target:document.getElementById('subtitleTail')});const f=performance.now();return {cues:5000,script_characters:400000,snapshot_ms:b-a,render_ms:c-b,payload_json_ms:d-c,payload_bytes:new TextEncoder().encode(payload).length,timing_setting_ms:f-e}})()")
                (args.output/'前端性能.json').write_text(json.dumps(performance,ensure_ascii=False,indent=2),encoding='utf-8')
            assert not cdp.exceptions, cdp.exceptions
            results.append({"name": "no_uncaught_browser_errors", "passed": True})
            cdp.evaluate("state.dirty=false;state.pendingSaves=0;state.jobId=null")
            cdp.call("Browser.close")
        except Exception as exc:
            diagnostic = {'error': str(exc), 'passed_checks': results}
            if cdp:
                try:
                    diagnostic['state'] = cdp.evaluate("({cues:state.cues.slice(0,5),history:state.history.length,operation:state.operation,jobId:state.jobId,toast:toastEl.textContent,modal:document.querySelector('dialog[open]')?.id,verified:document.querySelectorAll('.badge.verified').length})")
                    (args.output/'failure.png').write_bytes(base64.b64decode(cdp.call('Page.captureScreenshot',{'format':'png'})['data']))
                except Exception:
                    pass
            (args.output/'failure.json').write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2),encoding='utf-8')
            raise
        finally:
            if cdp:
                cdp.socket.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
        (args.output / "浏览器验证.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"browser_checks": len(results), "all_passed": all(r["passed"] for r in results)}))


if __name__ == "__main__":
    main()
