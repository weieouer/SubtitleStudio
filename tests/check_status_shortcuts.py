"""隔离浏览器验证双标签、参考箭头与快捷键设置，不使用用户工程。"""
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


def main():
    output = ROOT / 'docs/修复验证/双标签与快捷键_2026-11-17'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='subtitle-status-') as profile:
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
            target = read_json(f'http://127.0.0.1:{port}/json/new?' + quote(os.environ.get('SUBTITLE_FIXTURE_URL','http://127.0.0.1:1562'), safe=''), 'PUT')
            cdp = CDP(target['webSocketDebuggerUrl'])
            cdp.call('Runtime.enable'); cdp.call('Page.enable')
            cdp.call('Emulation.setDeviceMetricsOverride', {'width':1536,'height':1000,'deviceScaleFactor':1,'mobile':False})
            cdp.wait("document.querySelector('#healthDot')?.className==='ok' && !document.documentElement.classList.contains('booting')")
            cdp.evaluate("document.querySelector('#btnShortcutSettings').click()")
            assert cdp.evaluate("document.querySelector('#shortcutDialog').open && document.querySelector('#shortcutWavePan').value==='alt' && document.querySelector('#shortcutWaveZoom').value==='none'")
            cdp.evaluate("document.querySelector('#shortcutWavePan').value='none';document.querySelector('#btnSaveShortcuts').click()")
            assert cdp.evaluate("document.querySelector('#shortcutDialog').open && document.querySelector('#shortcutError').textContent.length>0")
            cdp.evaluate("document.querySelector('#btnResetShortcuts').click();document.querySelector('[data-shortcut=save]').dispatchEvent(new KeyboardEvent('keydown',{key:'s',code:'KeyS',ctrlKey:true,shiftKey:true,bubbles:true}));document.querySelector('#btnSaveShortcuts').click()")
            assert cdp.evaluate("!document.querySelector('#shortcutDialog').open && state.shortcuts.save==='Ctrl+Shift+S' && JSON.parse(localStorage.getItem('subtitleStudio.shortcuts.v1')).save==='Ctrl+Shift+S'")
            cdp.evaluate('window.__statusOldDocument=true')
            cdp.call('Page.reload');cdp.wait("!window.__statusOldDocument && document.querySelector('#healthDot')?.className==='ok' && typeof state!=='undefined' && state.shortcuts?.save==='Ctrl+Shift+S'")
            cdp.evaluate("state.settings.mode='align';syncSettingsUI();replaceCues([{text:'会震动整个世界',start:118.87,end:120.16,confidence:.81,script_text:'会感动整个世界',reference_match:{status:'typo',query_text:'会震动整个世界',reference:{text:'会感动整个世界',source_name:'EP359_SCRIPT.txt'}},verification_stage:'calibrate'},{text:'初始字幕',start:121,end:122}],{reset:true});renderAll()")
            assert cdp.evaluate("[...document.querySelectorAll('#cueBody tr')].every(r=>r.querySelectorAll('[data-stage=calibration]').length===1&&r.querySelectorAll('[data-stage=alignment]').length===1)")
            assert cdp.evaluate("(()=>{const r=document.querySelector('#cueBody tr'),left=r.querySelector('.cue-current').getBoundingClientRect(),arrow=r.querySelector('.cue-adopt').getBoundingClientRect(),right=r.querySelector('.suggest').getBoundingClientRect();return left.right<=arrow.left&&arrow.right<=right.left&&Math.abs(left.top-right.top)<1})()")
            cdp.evaluate("document.querySelector('#cueBody tr .adopt-reference').click()")
            assert cdp.evaluate("state.cues[0].text==='会感动整个世界' && document.querySelector('#cueBody tr [data-stage=calibration]').textContent==='校准通过' && document.querySelector('#cueBody tr [data-stage=alignment]').textContent==='尚未对齐'")
            cdp.evaluate("state.cues[1].alignment={engine:'inherited'};renderAll()")
            assert cdp.evaluate("document.querySelectorAll('[data-stage=alignment]')[1].textContent==='尚未对齐' && document.querySelectorAll('[data-stage=calibration]')[1].textContent==='尚未校准'")
            cdp.evaluate("const t=document.querySelector('input.time[data-field=start]');t.focus();t.value=fmtCueTime(118.9);t.dispatchEvent(new Event('change',{bubbles:true}));t.blur()")
            assert cdp.evaluate("document.querySelector('[data-stage=alignment]').textContent==='对齐通过'")
            assert cdp.evaluate("(()=>{const r=document.querySelector('#cueBody tr');return getComputedStyle(r.querySelector('.cue-reference')).color===getComputedStyle(r.querySelector('.adopt-reference')).color && getComputedStyle(r.querySelector('.cue-text')).color===getComputedStyle(r.querySelector('.badge.verified')).color})()")
            for width in (1536,1024):
                cdp.call('Emulation.setDeviceMetricsOverride', {'width':width,'height':1000,'deviceScaleFactor':1,'mobile':False})
                cdp.evaluate("renderAll()")
                assert cdp.evaluate("[...document.querySelectorAll('.cue-adopt button')].every(b=>b.getBoundingClientRect().width>=24)")
                shot=cdp.call('Page.captureScreenshot',{'format':'png'})
                (output/f'界面_{width}.png').write_bytes(base64.b64decode(shot['data']))
            cdp.evaluate("document.querySelector('#btnShortcutSettings').click()")
            shot=cdp.call('Page.captureScreenshot',{'format':'png'})
            (output/'快捷键设置.png').write_bytes(base64.b64decode(shot['data']))
            assert not cdp.exceptions, cdp.exceptions
            result={'all_passed':True,'checks':['two_independent_statuses','arrow_layout','arrow_adopts_reference','wheel_conflict_validation','shortcut_capture','shortcut_persistence','responsive_layout','inherited_alignment_stays_pending','manual_timing_passes','reference_blue_and_pass_green'],'browser_exceptions':0}
            (output/'页面验证.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(result))
        finally:
            if cdp:
                cdp.socket.close()
            process.terminate();process.wait(timeout=10)


if __name__ == '__main__':
    main()
