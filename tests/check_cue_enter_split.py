"""隔离浏览器验证：字幕文字中光标回车分段、Shift+回车换行、首尾回车只结束编辑。需 serve_fixture。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import quote

from browser_smoke import CDP, read_json

ROOT = Path(__file__).resolve().parents[1]
ENTER = {'key': 'Enter', 'code': 'Enter', 'windowsVirtualKeyCode': 13, 'nativeVirtualKeyCode': 13}
SHIFT = 8


def press(cdp, modifiers=0):
    base = {**ENTER, 'modifiers': modifiers}
    cdp.call('Input.dispatchKeyEvent', {**base, 'type': 'keyDown', 'text': '\r'})
    cdp.call('Input.dispatchKeyEvent', {**base, 'type': 'keyUp'})


def place_caret(cdp, index, offset):
    cdp.evaluate(
        "(()=>{const c=document.querySelector('#cueBody tr[data-i=\"" + str(index) + "\"] .cue-text');"
        "c.focus();const r=document.createRange();r.setStart(c.firstChild," + str(offset) + ");r.collapse(true);"
        "const s=getSelection();s.removeAllRanges();s.addRange(r);return true})()")


def load_cues(cdp, cues):
    cdp.evaluate("setMode('align');clearScript();replaceCues(" + json.dumps(cues, ensure_ascii=False) + ",{reset:true});renderAll();renderTable();selectCue(0,false)")
    try:
        cdp.wait("document.querySelectorAll('#cueBody tr[data-i]').length===" + str(len(cues)) + "&&!!document.querySelector('#cueBody tr[data-i=\"0\"] .cue-text')", timeout=20)
    except TimeoutError:
        probe = cdp.evaluate("JSON.stringify({cues:state.cues.length,rows:document.querySelectorAll('#cueBody tr[data-i]').length,"
                             "classes:[...document.querySelectorAll('#cueBody tr[data-i=\"0\"] [class]')].map(e=>e.className).join('|').slice(0,600)})")
        raise AssertionError('字幕文字单元格未渲染：' + probe)


def main():
    output = ROOT / 'data' / 'run'
    output.mkdir(parents=True, exist_ok=True)
    fixture_url = os.environ.get('SUBTITLE_FIXTURE_URL', 'http://127.0.0.1:1562')
    results = {}
    with tempfile.TemporaryDirectory(prefix='subtitle-enter-') as profile:
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

            # 1. 光标在文字中间按回车：从光标处切成两条，第二条获得焦点，两条拼回原文。
            load_cues(cdp, [{'text': '你好世界', 'start': .1, 'end': 1.1}])
            place_caret(cdp, 0, 2)
            press(cdp)
            cdp.wait("!state.operation && state.cues.length===2")
            texts = cdp.evaluate("state.cues.map(c=>c.text)")
            assert texts == ['你好', '世界'], texts
            assert cdp.evaluate("state.cues[0].manual_split===true && state.cues[0].needs_review===true && state.cues[0].segmentation==='manual_cursor'")
            assert cdp.evaluate("document.activeElement?.closest('tr')?.dataset.i==='1'")
            results['enter_in_middle_splits_at_caret'] = texts

            # 2. Shift+回车：在文字中插入换行，不分段。
            load_cues(cdp, [{'text': '甲乙丙丁', 'start': .1, 'end': 1.1}])
            place_caret(cdp, 0, 2)
            press(cdp, SHIFT)
            cdp.wait("state.cues[0].text.includes('\\n')")
            assert cdp.evaluate("state.cues.length===1")
            results['shift_enter_inserts_line_break_without_split'] = cdp.evaluate("JSON.stringify(state.cues[0].text)")

            # 3. 光标在末尾按回车：没有可拆的两侧，只结束编辑。
            load_cues(cdp, [{'text': '末尾回车', 'start': .1, 'end': 1.1}])
            place_caret(cdp, 0, 4)
            press(cdp)
            cdp.wait("document.activeElement?.closest('#cueBody')===null")
            assert cdp.evaluate("state.cues.length===1 && state.cues[0].text==='末尾回车'")
            results['enter_at_end_only_finishes_editing'] = True

            assert not cdp.exceptions, cdp.exceptions
            results['browser_exceptions'] = 0
            results['all_passed'] = True
            (output / 'cue_enter_split.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(results, ensure_ascii=False))
        finally:
            if cdp:
                cdp.socket.close()
            process.terminate(); process.wait(timeout=10)


if __name__ == '__main__':
    main()
