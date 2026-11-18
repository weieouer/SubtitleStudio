"""隔离浏览器测量：主题切换与窗口缩放耗时（2000 条字幕），并保存浅色/深色截图。需 serve_fixture。"""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import quote

from browser_smoke import CDP, read_json

ROOT = Path(__file__).resolve().parents[1]
COUNT = 2000
# 每一帧记录间隔（ms），用于判断是否掉帧；STOP 后读取统计。
START_FRAMES = ("window.__gaps=[];window.__last=performance.now();window.__stop=false;"
                "(function f(t){if(window.__stop)return;window.__gaps.push(t-window.__last);window.__last=t;requestAnimationFrame(f)})(performance.now());"
                "true")
STOP_FRAMES = "window.__stop=true;JSON.stringify(window.__gaps)"


def frame_stats(cdp):
    gaps = json.loads(cdp.evaluate(STOP_FRAMES))
    gaps = gaps[1:] or [0]
    return {'frames': len(gaps), 'max_frame_ms': round(max(gaps), 1),
            'frames_over_50ms': sum(1 for g in gaps if g > 50), 'total_ms': round(sum(gaps), 1)}


def main():
    output = ROOT / 'data' / 'run' / os.environ.get('PERF_LABEL', 'perf')
    output.mkdir(parents=True, exist_ok=True)
    fixture_url = os.environ.get('SUBTITLE_FIXTURE_URL', 'http://127.0.0.1:1562')
    with tempfile.TemporaryDirectory(prefix='subtitle-perf-') as profile:
        chrome = Path(os.environ['ProgramFiles']) / 'Google/Chrome/Application/chrome.exe'
        process = subprocess.Popen([str(chrome), '--headless=new', '--disable-gpu', '--no-first-run',
                                    '--no-default-browser-check', '--remote-debugging-port=0',
                                    '--user-data-dir=' + profile, 'about:blank'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        cdp = None
        try:
            endpoint = Path(profile) / 'DevToolsActivePort'
            for _ in range(100):
                if endpoint.exists():
                    break
                time.sleep(.1)
            port = int(endpoint.read_text().splitlines()[0])
            target = read_json(f'http://127.0.0.1:{port}/json/new?' + quote(fixture_url, safe=''), 'PUT')
            cdp = CDP(target['webSocketDebuggerUrl'])
            cdp.call('Runtime.enable'); cdp.call('Page.enable')
            cdp.call('Emulation.setDeviceMetricsOverride', {'width': 1536, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
            cdp.wait("document.querySelector('#healthDot')?.className==='ok' && !document.documentElement.classList.contains('booting')")
            cues = [{'text': f'第{i}条字幕的测试文字，用来填充表格。', 'start': i * 1.5, 'end': i * 1.5 + 1.2} for i in range(COUNT)]
            cdp.evaluate("setMode('align');clearScript();replaceCues(" + json.dumps(cues, ensure_ascii=False) + ",{reset:true});renderAll();renderTable()")
            cdp.wait("document.querySelectorAll('#cueBody tr[data-i]').length>0", timeout=60)
            cdp.evaluate("switchTheme('dark')")
            time.sleep(1.5)  # 等 2000 行初次渲染结束，避免把建表耗时算进切换。
            cdp.evaluate(START_FRAMES)
            cdp.evaluate("switchTheme('light')")
            time.sleep(.8)
            theme = frame_stats(cdp)
            # 缩放：真实改变视口宽度 1536→1180，逐步变化，每步等待一帧。
            cdp.evaluate(START_FRAMES)
            for width in range(1536, 1179, -12):
                cdp.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
                cdp.evaluate("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
            time.sleep(.3)
            resize = frame_stats(cdp)
            cdp.call('Emulation.setDeviceMetricsOverride', {'width': 1536, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
            light = base64.b64decode(cdp.call('Page.captureScreenshot', {'format': 'png'})['data'])
            (output / 'light.png').write_bytes(light)
            cdp.evaluate("switchTheme('dark',false)")
            dark = base64.b64decode(cdp.call('Page.captureScreenshot', {'format': 'png'})['data'])
            (output / 'dark.png').write_bytes(dark)
            result = {'cues': COUNT, 'theme_switch': theme, 'window_resize': resize,
                      'browser_exceptions': len(cdp.exceptions)}
            (output / 'perf.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(result, ensure_ascii=False))
        finally:
            if cdp:
                cdp.socket.close()
            process.terminate(); process.wait(timeout=10)


if __name__ == '__main__':
    main()
