"""消除模式交互与布局；真实媒体，模型任务通过接口替身隔离，不代表模型效果。"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from browser_smoke import CDP, read_json
from test_removal import video_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', type=Path, default=Path('docs/修复验证/字幕消除_2026-11-10'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    read_json(args.url + '/_fixture')
    results = []
    with tempfile.TemporaryDirectory(prefix='subtitle-removal-ui-') as temporary:
        directory = Path(temporary)
        video = directory / 'example.mp4'
        video_file(video, frames=96)
        profile = directory / 'profile'
        chrome = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Google/Chrome/Application/chrome.exe'
        process = subprocess.Popen([str(chrome), '--headless=new', '--disable-gpu', '--no-first-run',
            '--no-default-browser-check', '--remote-debugging-port=0', '--user-data-dir='+str(profile), 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        cdp = None
        try:
            endpoint = profile / 'DevToolsActivePort'
            for _ in range(100):
                if endpoint.exists():
                    break
                time.sleep(.1)
            port = int(endpoint.read_text().splitlines()[0])
            tab = read_json(f'http://127.0.0.1:{port}/json/new?'+quote(args.url, safe=''), 'PUT')
            cdp = CDP(tab['webSocketDebuggerUrl'])
            cdp.call('Runtime.enable'); cdp.call('Page.enable')
            cdp.call('Emulation.setDeviceMetricsOverride', {'width':1536,'height':1000,'deviceScaleFactor':1,'mobile':False})
            cdp.wait("document.querySelector('#healthDot')?.className==='ok'&&!!window.SubtitleRemoval")
            assert cdp.evaluate("[...document.querySelectorAll('.mode-btn')].map(b=>b.textContent.trim()).join('|')==='字幕消除|字幕提取|字幕对齐'")
            results.append({'name':'removal_mode_is_first_and_keeps_existing_modes','passed':True})
            cdp.evaluate("""window.__removalApi=api;window.__removalBodies=[];window.__removalCancelled=false;
              api=async(path,body,...rest)=>{
                if(path==='/api/removal/models')return {ok:true,model:{available:true,cuda_available:true,nvidia_available:true,device:'cuda'},installation:{status:'idle'}};
                if(path==='/api/dialog'&&body?.kind==='dir')return {path:"""+json.dumps(str(directory))+r"""};
                if(path==='/api/removal/jobs'){window.__removalBodies.push(body);window.__lastRemoval=body;return {job:{id:body.request_id,status:'queued'}};}
                if(/^\/api\/removal\/jobs\/[a-f0-9]+\/cancel$/.test(path)){window.__removalCancelled=true;return {ok:true};}
                if(/^\/api\/removal\/jobs\/[a-f0-9]+$/.test(path)){
                  const b=window.__lastRemoval;
                  if(window.__holdRemoval&&!window.__removalCancelled)return {job:{id:b.request_id,status:'running',progress:.3,message:'模拟处理中',elapsed:1}};
                  return {job:{id:b.request_id,status:window.__removalCancelled?'cancelled':'done',progress:1,message:'交互测试完成',request:b,output_path:b.path,report:{origin_start:b.start}}};
                }
                return window.__removalApi(path,body,...rest);
              };
              window.__originalCues=JSON.stringify(state.cues);
            """)
            cdp.click('[data-mode="remove"]')
            cdp.wait("document.body.dataset.mode==='remove'&&!document.querySelector('#removalWorkspace').hidden")
            assert cdp.evaluate("document.querySelector('#removalStartJob').disabled&&document.querySelector('#removalVideo').getBoundingClientRect().height>100")
            results.append({'name':'empty_removal_workspace_has_clear_disabled_actions','passed':True})
            assert cdp.evaluate('SubtitleRemoval.load('+json.dumps(str(video))+')')
            cdp.wait("document.querySelector('#removalVideo').videoWidth===96")
            box = cdp.evaluate("(()=>{const b=document.querySelector('#removalSelection').getBoundingClientRect();return {x:b.x,y:b.y,width:b.width,height:b.height}})()")
            start = {'x':box['x']+box['width']*.2,'y':box['y']+box['height']*.6}
            end = {'x':box['x']+box['width']*.8,'y':box['y']+box['height']*.8}
            cdp.call('Input.dispatchMouseEvent', {'type':'mousePressed','button':'left','clickCount':1,**start})
            cdp.call('Input.dispatchMouseEvent', {'type':'mouseMoved','button':'left','buttons':1,**end})
            cdp.call('Input.dispatchMouseEvent', {'type':'mouseReleased','button':'left','clickCount':1,**end})
            assert cdp.evaluate("Number(document.querySelector('#removalW').value)>50&&document.querySelector('#removalPreview').disabled")
            results.append({'name':'pointer_selection_maps_to_real_video_pixels','passed':True})
            cdp.click('#removalAddSegment')
            assert cdp.evaluate("document.querySelectorAll('.removal-segment-row').length===1&&!document.querySelector('#removalPreview').disabled")
            results.append({'name':'manual_time_segment_enables_selected_processing','passed':True})
            cdp.click('#removalPreview')
            cdp.wait("!SubtitleRemoval.busy()&&!document.querySelector('#removalResult').hidden")
            assert cdp.evaluate("window.__removalBodies.length===1&&window.__removalBodies[0].preview===true&&window.__removalBodies[0].end-window.__removalBodies[0].start<=3&&document.querySelector('#removalUseResult').hidden")
            results.append({'name':'short_preview_and_full_output_are_distinct','passed':True})
            cdp.click('#removalViewOriginal')
            cdp.evaluate("document.querySelector('#removalQuality').value='384';document.querySelector('#removalQuality').dispatchEvent(new Event('change',{bubbles:true}))")
            assert cdp.evaluate("document.querySelector('#removalViewResult').disabled")
            results.append({'name':'changing_parameters_invalidates_stale_preview','passed':True})
            cdp.click('#removalStartJob')
            cdp.wait("window.__removalBodies.length===2&&!SubtitleRemoval.busy()")
            assert cdp.evaluate("window.__removalBodies[1].preview===false&&!document.querySelector('#removalUseResult').hidden&&JSON.stringify(state.cues)===window.__originalCues")
            results.append({'name':'full_output_does_not_replace_subtitle_editing_state','passed':True})
            cdp.evaluate("window.__holdRemoval=true;window.__removalCancelled=false")
            cdp.click('#removalPreview')
            cdp.wait('SubtitleRemoval.busy()')
            cdp.click('#removalCancelJob')
            cdp.wait('!SubtitleRemoval.busy()')
            assert cdp.evaluate('window.__removalCancelled')
            results.append({'name':'cancel_uses_current_job_and_restores_controls','passed':True})
            cdp.click('#removalScan')
            cdp.wait("!SubtitleRemoval.busy()&&document.querySelector('#removalJobStatus').textContent.includes('候选时间段')")
            assert cdp.evaluate("document.querySelectorAll('.removal-segment-row').length===1&&document.querySelector('.removal-segment-row small').textContent.includes('手动')")
            results.append({'name':'real_full_video_text_scan_preserves_manual_segments','passed':True})
            cdp.click('#removalAddSegment')
            cdp.evaluate("(()=>{const e=document.querySelector('.removal-segment-row [data-field=end]');e.value='.75';e.dispatchEvent(new Event('change',{bubbles:true}))})()")
            assert cdp.evaluate("(async()=>{await SubtitleRemoval.save();const d=await api('/api/removal/info?path='+encodeURIComponent("+json.dumps(str(video))+"));return d.project.segments.length===2&&d.project.segments[0].end===.75})()")
            cdp.evaluate("(()=>{const n=document.querySelectorAll('.removal-segment-row').length;for(let i=0;i<n;i++){const e=document.querySelector('[data-index=\"'+i+'\"] [type=checkbox]');e.checked=false;e.dispatchEvent(new Event('change',{bubbles:true}));}})()")
            assert cdp.evaluate("document.querySelector('#removalStartJob').disabled")
            cdp.evaluate("(()=>{const e=document.querySelector('.removal-segment-row [type=checkbox]');e.checked=true;e.dispatchEvent(new Event('change',{bubbles:true}))})()")
            assert cdp.evaluate("!document.querySelector('#removalStartJob').disabled")
            results.append({'name':'time_rows_support_edit_save_and_explicit_export_scope','passed':True})
            for width,height in ((1536,1000),(1280,720),(1024,768)):
                cdp.call('Emulation.setDeviceMetricsOverride', {'width':width,'height':height,'deviceScaleFactor':1,'mobile':False})
                time.sleep(.1)
                cdp.evaluate("document.querySelector('#removalInspector').scrollTop=0")
                layout = cdp.evaluate("(()=>{const s=document.querySelector('#removalStage').getBoundingClientRect(),buttons=[...document.querySelectorAll('.mode-btn')];return {overflow:document.documentElement.scrollWidth>innerWidth,stage:s.width,clipped:buttons.some(b=>b.scrollWidth>b.clientWidth+1)}})()")
                assert not layout['overflow'] and not layout['clipped'] and layout['stage']>150, layout
                (args.output/f'消除界面_{width}x{height}.png').write_bytes(base64.b64decode(cdp.call('Page.captureScreenshot',{'format':'png'})['data']))
            results.append({'name':'three_viewports_keep_tabs_and_workspace_in_bounds','passed':True})
            cdp.click('[data-mode="extract"]')
            assert cdp.evaluate("document.body.dataset.mode==='extract'&&document.querySelector('#removalWorkspace').hidden&&JSON.stringify(state.cues)===window.__originalCues")
            results.append({'name':'switching_back_preserves_previous_subtitle_state','passed':True})
            assert not cdp.exceptions, cdp.exceptions
        except Exception:
            if cdp is not None:
                (args.output/'ui-failure.json').write_text(json.dumps({'checks':results,'state':cdp.evaluate("({mode:state.settings.mode,job:document.querySelector('#removalJobStatus').textContent,model:document.querySelector('#removalInstallStatus').textContent,errors:[]})"),'exceptions':cdp.exceptions},ensure_ascii=False,indent=2),encoding='utf-8')
                (args.output/'ui-failure.png').write_bytes(base64.b64decode(cdp.call('Page.captureScreenshot',{'format':'png'})['data']))
            raise
        finally:
            if cdp is not None:
                cdp.socket.close()
            process.terminate();process.wait(timeout=10)
    record = {'scope':'真实媒体与隔离浏览器；模型接口替身，不验证真实修复效果','checks':results,'all_passed':all(r['passed'] for r in results)}
    (args.output/'界面验证.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'checks':len(results),'all_passed':record['all_passed']}))


if __name__ == '__main__':
    main()
