"""隔离浏览器验收：本地草稿、跨页冲突、素材失联与校对布局。需 serve_fixture。"""
from pathlib import Path
import argparse
import base64
import json
import os
import subprocess
import tempfile
import time
from urllib.parse import quote

from browser_smoke import CDP,read_json


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--url',required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    fixture=read_json(args.url+'/_fixture');results={};moved=None;original=None
    with tempfile.TemporaryDirectory(prefix='subtitle-recovery-') as profile:
        chrome=Path(os.environ['ProgramFiles'])/'Google/Chrome/Application/chrome.exe'
        process=subprocess.Popen([str(chrome),'--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
            '--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        clients=[]
        try:
            endpoint=Path(profile)/'DevToolsActivePort'
            for _ in range(100):
                if endpoint.is_file():break
                time.sleep(.1)
            port=int(endpoint.read_text().splitlines()[0])
            def new_page():
                target=read_json(f'http://127.0.0.1:{port}/json/new?'+quote(args.url,safe=''),'PUT')
                client=CDP(target['webSocketDebuggerUrl']);clients.append(client)
                client.call('Runtime.enable');client.call('Page.enable');client.call('Network.enable')
                client.call('Emulation.setDeviceMetricsOverride',{'width':1536,'height':1000,'deviceScaleFactor':1,'mobile':False})
                client.wait("document.querySelector('#healthDot')?.className==='ok'&&!document.documentElement.classList.contains('booting')")
                client.evaluate('window.__fixture='+json.dumps(fixture));return client
            page=new_page()
            page.evaluate("""(async()=>{
              await api('/api/projects/save',{source_path:window.__fixture.srt,cues:[{text:'服务器原稿',start:0,end:1}],
                initial_cues:[{text:'原始记录',start:0,end:1}],expected_revision:0});
              await openSavedProject(window.__fixture.srt);await saveAll();
            })()""")
            page.call('Network.emulateNetworkConditions',{'offline':True,'latency':0,'downloadThroughput':-1,'uploadThroughput':-1})
            offline=page.evaluate("""(async()=>{
              state.cues[0].text='断网后的完整修改';scheduleSave();await cacheProjectDraft();
              const saved=await saveProject(true);clearTimeout(state.saveTimer);clearTimeout(state.draftTimer);
              const drafts=await SubtitleDrafts.list();return {saved,dirty:state.dirty,records:drafts.filter(d=>d.source===projectPath()).length,
                text:drafts.find(d=>d.source===projectPath())?.payload.cues[0].text};
            })()""")
            assert offline=={'saved':False,'dirty':True,'records':1,'text':'断网后的完整修改'},offline
            results['offline_draft']=offline
            page.evaluate('state.dirty=false;state.pendingSaves=0;window.__reloading=true')
            page.call('Network.emulateNetworkConditions',{'offline':False,'latency':0,'downloadThroughput':-1,'uploadThroughput':-1})
            page.call('Page.reload');page.wait("!window.__reloading&&window.SubtitleDrafts&&document.querySelector('#healthDot')?.className==='ok'&&!document.documentElement.classList.contains('booting')")
            page.evaluate('window.__fixture='+json.dumps(fixture))
            recovered=page.evaluate("""(async()=>{
              const records=await SubtitleDrafts.list();const record=records.find(r=>r.source===window.__fixture.srt);
              await recoverProjectDraft(record.key);const text=state.cues[0].text;
              if(!await saveAll())throw new Error('recovered save failed');
              return {text,remaining:(await SubtitleDrafts.list()).filter(r=>r.source===window.__fixture.srt).length};
            })()""")
            assert recovered=={'text':'断网后的完整修改','remaining':0},recovered
            results['reload_recovery_and_cleanup']=recovered
            other=new_page()
            page.evaluate("SubtitleDrafts.put('cross-tab.srt',{cues:[{text:'A页'}]},1,0)")
            other.evaluate("SubtitleDrafts.put('cross-tab.srt',{cues:[{text:'B页'}]},1,0)")
            assert page.evaluate("(async()=>{const records=(await SubtitleDrafts.list()).filter(r=>r.source==='cross-tab.srt');return records.length===2&&new Set(records.map(r=>r.tab)).size===2})()")
            page.evaluate("SubtitleDrafts.clearThrough('cross-tab.srt',1,1)")
            assert other.evaluate("(async()=>{const records=(await SubtitleDrafts.list()).filter(r=>r.source==='cross-tab.srt');return records.length===1&&records[0].payload.cues[0].text==='B页'})()")
            results['independent_tab_drafts_and_cleanup']=True
            assert other.evaluate("""(async()=>{
              const old=(await SubtitleDrafts.list()).find(r=>r.source==='cross-tab.srt');
              await SubtitleDrafts.put('cross-tab.srt',{cues:[{text:'B页新版本'}]},2,0);
              await SubtitleDrafts.clearRecovered(old.key,old.version);
              return (await SubtitleDrafts.list()).find(r=>r.source==='cross-tab.srt')?.payload.cues[0].text==='B页新版本';
            })()""")
            results['newer_draft_not_removed_by_old_acknowledgement']=True
            conflict=page.evaluate("""(async()=>{
              const source=projectPath(),before=await api('/api/projects/load?path='+encodeURIComponent(source));
              const key=await SubtitleDrafts.put(source,{...before.project,cues:[{text:'本地分支',start:0,end:1}]},10,before.revision);
              await api('/api/projects/save',{...before.project,source_path:source,script_path:'server-reference.txt',cues:[{text:'另一页面的新稿',start:0,end:1}],expected_revision:before.revision});
              await recoverProjectDraft(key);const blocked=projectConflicts.has(source)&&!await saveProject(true);
              const latest=await api('/api/projects/load?path='+encodeURIComponent(source));
              return {blocked,local:state.cues[0].text,remote:latest.project.cues[0].text};
            })()""")
            assert conflict=={'blocked':True,'local':'本地分支','remote':'另一页面的新稿'},conflict
            results['recovery_does_not_overwrite_remote']=conflict
            restored_path=page.evaluate("""(async()=>{
              const previousConfirm=confirmDialog;confirmDialog=async()=>true;
              try{await resolveProjectConflict();return state.scriptPath==='server-reference.txt'&&state.cues[0].text==='另一页面的新稿'&&!projectConflicts.has(projectPath());}
              finally{confirmDialog=previousConfirm;}
            })()""")
            assert restored_path
            results['conflict_reload_restores_reference_location']=restored_path
            page.evaluate('state.dirty=false;state.pendingSaves=0;clearTimeout(state.saveTimer);clearTimeout(state.draftTimer);window.__reloading=true')
            page.call('Page.reload');page.wait("!window.__reloading&&window.SubtitleDrafts&&document.querySelector('#healthDot')?.className==='ok'&&!document.documentElement.classList.contains('booting')")
            page.evaluate('window.__fixture='+json.dumps(fixture))
            original=Path(fixture['srt']).resolve()
            assert original.is_relative_to(Path(tempfile.gettempdir()).resolve()) and any(p.startswith('subtitle-browser-') for p in original.parts)
            moved=original.with_name('moved-fixture.srt');original.rename(moved)
            missing=page.evaluate("""(async()=>{
              await openSavedProject(window.__fixture.srt,false);
              const opened=state.cues[0].text;state.cues[0].text='素材失联后编辑';scheduleSave();
              if(!await saveAll())throw new Error('missing source save failed');
              const exported=await api('/api/srt/export',{cues:state.cues.map(c=>({start:c.start,end:c.end,text:c.text})),options:{}});
              const initial=JSON.stringify(state.initialCues);
              if(!await loadMedia(window.__fixture.audio,{relink:true}))throw new Error('relink failed');
              return {opened,edited:state.cues[0].text,exported:exported.count,initialPreserved:JSON.stringify(state.initialCues)===initial,
                media:state.media.path===window.__fixture.audio};
            })()""")
            assert missing=={'opened':'另一页面的新稿','edited':'素材失联后编辑','exported':1,'initialPreserved':True,'media':True},missing
            results['missing_source_edit_export_relink']=missing
            layouts=[]
            for width,height in ((1536,1000),(1280,720),(1024,600),(390,844)):
                page.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':height,'deviceScaleFactor':1,'mobile':False})
                layout=page.evaluate("""(()=>{setTimelineHeight(230);const box=s=>document.querySelector(s).getBoundingClientRect();
                  const v=box('#videoWrap'),p=box('#subtitleParameters'),t=box('#timelinePanel'),c=box('.center');
                  return {width:innerWidth,height:innerHeight,belowVideo:p.top>=v.bottom-1,
                    beforeWave:p.bottom<=t.top+1,waveInside:t.bottom<=c.bottom+1,videoHeight:v.height,
                    horizontal:document.documentElement.scrollWidth>innerWidth+1};})()""")
                assert layout['belowVideo'] and layout['beforeWave'] and layout['waveInside'] and not layout['horizontal'],layout
                layouts.append(layout)
                if width in (1536,1280):
                    (args.output/f'布局_{width}x{height}.png').write_bytes(base64.b64decode(page.call('Page.captureScreenshot',{'format':'png'})['data']))
            results['layouts']=layouts
            page.call('Emulation.setDeviceMetricsOverride',{'width':1280,'height':720,'deviceScaleFactor':1,'mobile':False})
            tags=page.evaluate("""(()=>{
              applyTheme('dark',false);state.search='';state.script=[];setMode('align',false);
              replaceCues([{text:'通过',start:.1,end:.3,verification_stage:'verify',reference_match:{status:'high',query_text:'通过'}},
                {text:'错字',start:.4,end:.6,verification_stage:'verify',reference_match:{status:'typo',query_text:'错字',reference:{text:'正字'}}},
                {text:'多识别',start:.7,end:.9,verification_stage:'verify',reference_match:{status:'unmatched',query_text:'多识别',reference:null}},
                {text:'人工确认',start:1.1,end:1.3,edited:true,modification_complete:{text:'人工确认'}},
                {text:'未校验',start:1.5,end:1.7}],{reset:true});
              const counts={};const select=document.querySelector('#filterSelect');
              for(const filter of ['minor','obvious','verified','calibrated','aligned','edited','unchecked']){
                select.value=filter;select.dispatchEvent(new Event('change',{bubbles:true}));
                counts[filter]=document.querySelectorAll('#cueBody tr[data-i]').length;
              }
              selectAllCues(true);select.value='all';select.dispatchEvent(new Event('change',{bubbles:true}));
              const checkbox=document.querySelector('#selectAllCues').getBoundingClientRect();
              const first=document.querySelector('#cueBody .cue-pick').getBoundingClientRect();
              const bar=document.querySelector('.cue-list-tools').getBoundingClientRect();
              return {counts,selected:selectedCueIndices().length,leftAligned:Math.abs(checkbox.left-first.left)<15,
                aboveRows:bar.bottom<=first.top,caption:document.querySelector('.filter-picker .model-btn span').textContent};
            })()""")
            assert tags=={'counts':{'minor':1,'obvious':1,'verified':1,'calibrated':0,'aligned':0,'edited':1,'unchecked':1},'selected':5,
                          'leftAligned':True,'aboveRows':True,'caption':'标签筛选'},tags
            results['tag_filter_and_list_select_all']=tags
            (args.output/'标签筛选与全选.png').write_bytes(base64.b64decode(page.call('Page.captureScreenshot',{'format':'png'})['data']))
            page.click('.filter-picker .model-btn')
            (args.output/'标签筛选菜单.png').write_bytes(base64.b64decode(page.call('Page.captureScreenshot',{'format':'png'})['data']))
            assert not page.exceptions,page.exceptions
            page.evaluate('state.dirty=false;state.pendingSaves=0;clearTimeout(state.saveTimer);clearTimeout(state.draftTimer)')
            other.evaluate('state.dirty=false;state.pendingSaves=0');page.call('Browser.close')
        finally:
            for client in clients:client.socket.close()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)
            if moved and moved.is_file():moved.rename(original)
    results['all_passed']=True
    (args.output/'草稿与失联恢复.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'all_passed':True,'scenarios':list(results)},ensure_ascii=True))


if __name__=='__main__':main()
