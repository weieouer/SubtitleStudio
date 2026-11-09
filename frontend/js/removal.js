/* 视频修复界面与字幕编辑状态独立；所有模型任务均由本机后端执行。 */
function removalFrameBox(width, height, videoWidth, videoHeight) {
  if(!(width>0&&height>0&&videoWidth>0&&videoHeight>0))return {left:0,top:0,width:0,height:0};
  const scale=Math.min(width/videoWidth,height/videoHeight);
  return {left:(width-videoWidth*scale)/2,top:(height-videoHeight*scale)/2,width:videoWidth*scale,height:videoHeight*scale};
}

(() => {
  const find=id=>document.getElementById(id);
  const player=find('removalVideo'),selection=find('removalSelection'),stage=find('removalStage');
  const r={info:null,region:null,model:null,installing:false,job:null,result:null,preview:null,view:'original',
    choosing:false,active:false,outputDir:'',epoch:0,selecting:false,drag:null,saveTimer:null,pollTimer:null,polling:false,
    previewKey:'',undo:[],deviceChosen:false,detached:null,segments:null,scanning:false,scanBinding:''};
  const busy=()=>!!(r.choosing||r.job||r.scanning);
  const stream=path=>'/api/media/stream?path='+encodeURIComponent(path)+'&token='+encodeURIComponent(SESSION_TOKEN);
  const clip=(x,lo,hi)=>Math.max(lo,Math.min(hi,x));
  const configuration=()=>({path:r.info?.path,region:r.region,start:Number(find('removalStart').value),end:Number(find('removalEnd').value),
    max_edge:Number(find('removalQuality').value),device:find('removalDevice').value,format:find('removalFormat').value,model:'propainter',segments:r.segments});
  const signature=()=>JSON.stringify(configuration());
  const valid=()=>r.info&&r.region&&r.region.width*r.info.width>=2&&r.region.height*r.info.height>=2
    &&Number(find('removalEnd').value)>Number(find('removalStart').value)&&r.segments?.some(s=>s.enabled);

  function remember() {
    r.undo.push(JSON.parse(JSON.stringify({region:r.region,segments:r.segments,scanBinding:r.scanBinding})));
    if(r.undo.length>40)r.undo.shift();
  }
  function undo() {
    if(busy()||!r.undo.length)return;
    const previous=r.undo.pop();r.region=previous.region;r.segments=previous.segments;r.scanBinding=previous.scanBinding;
    renderSegments();invalidate();
  }

  function renderSegments() {
    const segments=r.segments||[];
    find('removalSegmentCount').textContent=r.segments===null?'未扫描':`${segments.filter(s=>s.enabled).length} / ${segments.length} 段启用`;
    find('removalSegments').innerHTML=segments.length?segments.map((segment,index)=>`<div class="removal-segment-row" data-index="${index}"><input type="checkbox" data-field="enabled" ${segment.enabled?'checked':''} aria-label="启用第${index+1}段"><input type="number" data-field="start" min="0" step="0.05" value="${Number(segment.start).toFixed(3)}" aria-label="第${index+1}段开始秒数"><input type="number" data-field="end" min="0" step="0.05" value="${Number(segment.end).toFixed(3)}" aria-label="第${index+1}段结束秒数"><button type="button" data-delete="true" aria-label="删除第${index+1}段">×</button><small>${segment.source==='manual'?'手动添加':segment.stale?'自动 · 选区已变化':'自动检测 · 待复核'} · 点击本行定位</small></div>`).join('')
      :'<p class="removal-note">'+(r.segments===null?'先框选字幕位置，再扫描；也可手动添加。':'没有候选段，可手动添加遗漏的字幕。')+'</p>';
  }

  function paint() {
    if(!r.info)return;
    const bounds=removalFrameBox(stage.clientWidth,stage.clientHeight,r.info.width,r.info.height);
    Object.assign(selection.style,{left:bounds.left+'px',top:bounds.top+'px',width:bounds.width+'px',height:bounds.height+'px'});
    const roi=r.region||{x:0,y:0,width:0,height:0};
    const rect=find('removalRegion');
    for(const [key,value] of Object.entries(roi))rect.setAttribute(key,String(value*1000));
    const hw=10000/Math.max(1,bounds.width),hh=10000/Math.max(1,bounds.height);
    find('removalHandles').innerHTML=r.region?[[roi.x,roi.y,'nw'],[roi.x+roi.width,roi.y,'ne'],[roi.x,roi.y+roi.height,'sw'],[roi.x+roi.width,roi.y+roi.height,'se']]
      .map(([x,y,h])=>`<rect class="roi-handle" data-handle="${h}" x="${x*1000-hw/2}" y="${y*1000-hh/2}" width="${hw}" height="${hh}"/>`).join(''):'';
    selection.classList.toggle('selecting',r.selecting&&r.view==='original'&&!busy());
    selection.style.visibility=r.view==='original'?'visible':'hidden';
    if(!r.drag){
      find('removalX').value=Math.round(roi.x*r.info.width);find('removalY').value=Math.round(roi.y*r.info.height);
      find('removalW').value=Math.round(roi.width*r.info.width);find('removalH').value=Math.round(roi.height*r.info.height);
    }
    find('removalSelectionInfo').textContent=r.region?`${Math.round(roi.width*r.info.width)} × ${Math.round(roi.height*r.info.height)} 像素 · 拖动选框或角点调整`:'尚未框选';
    find('removalRegionSummary').textContent=r.region?`框选区域 · ${Math.round(roi.width*r.info.width)} × ${Math.round(roi.height*r.info.height)} 像素`:'框选区域 · 尚未框选';
  }

  function refresh() {
    const locked=busy(),ready=!!r.model?.available&&!r.installing&&!state.versionMismatch;
    for(const id of ['removalChoose','removalQuality','removalFormat','removalFolder','removalDevice','removalFullRange','removalX','removalY','removalW','removalH','removalStart','removalEnd'])find(id).disabled=locked;
    find('removalUseCurrent').hidden=!(state.media?.width&&state.media?.height);
    find('removalUseCurrent').disabled=locked;
    find('removalDraw').disabled=locked||!r.info||r.view!=='original';
    find('removalClear').disabled=locked||!r.region;
    find('removalDraw').textContent=r.selecting?'正在框选 · 拖动画面':r.region?'调整或重新框选':'框选字幕区域';
    find('removalPreview').disabled=locked||!ready||!valid();
    find('removalStartJob').disabled=locked||!ready||!valid();
    find('removalStartJob').textContent=r.job?'处理中…':'开始消除';
    find('removalCancelJob').hidden=!r.job&&!r.scanning;
    find('removalCancelJob').disabled=r.scanning&&!r.scanId;
    find('removalCancelJob').textContent=r.scanning?'取消扫描':'取消处理';
    find('removalScan').disabled=locked||!r.info||!r.region||!!state.versionMismatch;
    find('removalAddSegment').disabled=locked||!r.info;
    find('removalSegments').querySelectorAll('input,button').forEach(element=>{element.disabled=locked;});
    find('removalRangeSummary').textContent=r.info?`处理范围 · ${Number(find('removalStart').value).toFixed(1)}–${Number(find('removalEnd').value).toFixed(1)} 秒`:'处理范围 · 全片';
    find('removalInstall').disabled=locked||r.installing||!!state.versionMismatch;
    find('removalInstall').textContent=ready?'重新准备环境':'下载并准备模型';
    find('removalCancelInstall').hidden=!r.installing;
    find('removalModelBadge').textContent=ready?(r.model.cuda_available?'GPU 已就绪':'CPU 已就绪'):r.installing?'正在准备':'未准备';
    find('removalViewResult').disabled=!r.preview;
    find('removalViewOriginal').classList.toggle('active',r.view==='original');
    find('removalViewResult').classList.toggle('active',r.view==='result');
    find('removalFolderPath').textContent=r.outputDir||'开始前选择保存位置';
    find('removalFolderPath').title=r.outputDir;
    find('removalEmpty').hidden=!!r.info;
    find('removalReconnect').hidden=!r.detached;
    paint();
    updateButtons();
  }

  async function save(notify=false) {
    if(!r.info){if(notify)toast('请先选择视频','info');return false;}
    if(Number(find('removalEnd').value)<=Number(find('removalStart').value))return false;
    if(state.versionMismatch){cacheDraft();if(notify)toast('消除草稿已保留，请重启后台并刷新页面后保存','info');return false;}
    const body=configuration(),key=JSON.stringify(body);
    try {await api('/api/removal/project',body);if(signature()===key){try{localStorage.removeItem('subtitleStudio.removalDraft:'+body.path);}catch{}}if(r.active)setSaveState('消除设置已保存');return true;}
    catch(error){toast('消除设置保存失败：'+error.message,'error');return false;}
  }

  function cacheDraft() {
    if(!r.info)return;
    try{localStorage.setItem('subtitleStudio.removalDraft:'+r.info.path,JSON.stringify({...configuration(),saved_at:Date.now()}));}
    catch{setSaveState('消除设置未保存，请保持页面打开');}
  }

  function invalidate() {
    if(r.view==='result')showView('original');
    r.preview=null;r.previewKey='';
    if(r.scanBinding&&r.scanBinding!==JSON.stringify({path:r.info?.path,region:r.region})&&r.segments){
      r.segments.forEach(segment=>{if(segment.source==='detected'){segment.stale=true;segment.enabled=false;}});
      find('removalScanHint').textContent='选区已变化，旧自动片段已停用，请重新扫描；手动片段保留。';
      renderSegments();
    }
    if(r.lastOutput&&find('removalUseResult').hidden)find('removalResultTitle').textContent='历史预览 · 参数已变化';
    clearTimeout(r.saveTimer);
    cacheDraft();
    r.saveTimer=setTimeout(save,500);
    refresh();
  }

  async function load(path) {
    if(busy())return false;
    r.choosing=true;const epoch=++r.epoch;refresh();
    try {
      const response=await api('/api/removal/info?path='+encodeURIComponent(path));
      if(epoch!==r.epoch)return false;
      r.info=response.info;r.region=response.project?.region||null;r.preview=null;r.result=null;r.view='original';r.undo=[];
      r.segments=response.project?.segments??null;r.scanBinding=JSON.stringify({path:r.info.path,region:r.region});renderSegments();
      let project=response.project;
      try{const draft=JSON.parse(localStorage.getItem('subtitleStudio.removalDraft:'+r.info.path)||'null');if(draft&&draft.saved_at>(project?.updated_at||0)*1000){project=draft;r.region=draft.region;r.segments=draft.segments??null;r.scanBinding=JSON.stringify({path:r.info.path,region:r.region});renderSegments();}}catch{}
      find('removalStart').value=project?.start??0;find('removalEnd').value=project?.end??r.info.duration;
      if(project?.max_edge)find('removalQuality').value=project.max_edge;
      if(project?.format)find('removalFormat').value=project.format;
      find('removalSourceName').textContent=r.info.name;
      find('removalSourceMeta').textContent=`${r.info.width} × ${r.info.height} · ${r.info.duration.toFixed(1)} 秒`;
      find('removalResult').hidden=true;
      find('removalJobStatus').textContent=r.region?'已恢复选区，可预览效果':'在有字幕的画面拖动框选区域';
      find('removalJobDetail').textContent='';find('removalProgress').value=0;
      r.selecting=true;player.muted=false;player.src=stream(r.info.path);player.load();
      return true;
    }catch(error){toast(error.message,'error');return false;}
    finally{r.choosing=false;refresh();}
  }

  async function choose() {
    if(busy())return;
    r.choosing=true;refresh();let path='';
    try {const picked=await api('/api/dialog',{kind:'media',multiple:false,title:'选择需要消除字幕的视频'});path=picked.path||picked.paths?.[0]||'';}
    catch(error){toast(error.message,'error');}
    finally{r.choosing=false;refresh();}
    if(path)await load(path);
  }

  function showView(view) {
    if(!r.info||(view==='result'&&!r.preview))return;
    const oldTime=r.view==='result'?player.currentTime+(r.preview?.report?.origin_start||0):player.currentTime;
    r.view=view;
    player.pause();player.src=stream(view==='result'?r.preview.output_path:r.info.path);
    player.muted=view==='result';
    player.addEventListener('loadedmetadata',()=>{
      const next=view==='result'?oldTime-(r.preview?.report?.origin_start||0):oldTime;
      player.currentTime=clip(next,0,Math.max(0,player.duration-.01));paint();
    },{once:true});
    player.load();refresh();
  }

  async function folder() {
    if(busy())return false;
    r.choosing=true;refresh();
    try {const picked=await api('/api/dialog',{kind:'dir',purpose:'export',title:'选择消除结果的保存文件夹'});if(picked.path)r.outputDir=picked.path;return !!picked.path;}
    catch(error){toast(error.message,'error');return false;}
    finally{r.choosing=false;refresh();}
  }

  async function process(preview) {
    if(busy()||!valid())return;
    if(state.versionMismatch){toast('请先重启后台并刷新页面，再使用新功能','error');return;}
    if(r.detached){toast('有待查询的任务，请先继续查询原任务，避免重复处理','info');return;}
    if(!preview&&!r.outputDir&&!(await folder()))return;
    if(r.view==='result')showView('original');
    const body={...configuration(),preview,output_dir:r.outputDir,request_id:crypto.randomUUID().replaceAll('-','')};
    if(preview){
      body.start=clip(player.currentTime,body.start,Math.max(body.start,body.end-.04));
      if(r.segments){const enabled=r.segments.filter(s=>s.enabled);const hit=enabled.find(s=>s.start<=body.start&&body.start<s.end)||enabled.find(s=>s.start>=body.start)||enabled[0];if(hit)body.start=clip(hit.start,Number(find('removalStart').value),body.end-.04);}
      body.end=Math.min(body.end,body.start+3);
    }
    const epoch=r.epoch,key=signature();
    r.job={id:body.request_id,request:body};r.detached=null;r.selecting=false;
    find('removalJobStatus').textContent='正在提交任务…';find('removalProgress').value=0;refresh();
    try {
      const response=await api('/api/removal/jobs',body);r.job=response.job;
      await pollJob(epoch,key);
    }catch(error){
      // 已提交的网络中断保留任务 ID，重新查询不重复启动模型。
      r.detached={id:body.request_id,epoch,key};r.job=null;
      find('removalJobStatus').textContent=error.status?'未能开始：'+error.message:'连接中断，可继续查询原任务';
      if(error.status)r.detached=null;
      refresh();
    }
  }

  async function pollJob(epoch,key) {
    while(r.job&&epoch===r.epoch){
      const response=await api('/api/removal/jobs/'+r.job.id);
      const job=response.job;r.job=job;
      find('removalProgress').value=job.progress||0;
      find('removalJobStatus').textContent=job.message||job.status;
      find('removalJobDetail').textContent=job.error?String(job.error).slice(-240):`已用时 ${Math.round(job.elapsed||0)} 秒`;
      if(!['queued','running'].includes(job.status)){
        r.job=null;
        if(job.status==='done'){
          if(job.request.preview){if(key===signature()){r.preview=job;r.previewKey=key;showView('result');}}
          else r.result=job;
          find('removalResult').hidden=false;
          find('removalResultTitle').textContent=job.request.preview?'短片预览 · 仅画面对比':'消除完成 · 原片保留';
          find('removalResultPath').textContent=job.output_path;
          find('removalResultPath').title=job.output_path;
          find('removalUseResult').hidden=job.request.preview;
          r.lastOutput=job.output_path;
          find('removalResult').scrollIntoView({block:'nearest'});
        }
        refresh();return;
      }
      await new Promise(resolve=>setTimeout(resolve,600));
    }
  }

  async function modelPoll() {
    if(r.polling)return;
    r.polling=true;
    try {
      const response=await api('/api/removal/models');r.model=response.model;
      r.installing=['downloading','preparing'].includes(response.installation.status);
      if(!r.deviceChosen&&!r.installing){find('removalDevice').value=r.model.cuda_available?'cuda':r.model.nvidia_available?'cuda':'cpu';}
      const installation=response.installation;
      find('removalInstallStatus').textContent=installation.status==='error'?'准备失败：'+String(installation.error).slice(-160)
        :r.installing?installation.message:r.model.available?'模型与独立环境已就绪':'首次准备需要下载权重和推理依赖';
      find('removalInstallStatus').title=installation.error||'';
      refresh();
    }catch(error){find('removalInstallStatus').textContent='模型状态暂不可用：'+error.message;}
    finally{r.polling=false;clearTimeout(r.pollTimer);if(r.active||r.installing)r.pollTimer=setTimeout(modelPoll,2000);}
  }

  function point(event) {
    const bounds=selection.getBoundingClientRect();
    return {x:clip((event.clientX-bounds.left)/bounds.width,0,1),y:clip((event.clientY-bounds.top)/bounds.height,0,1)};
  }
  selection.addEventListener('pointerdown',event=>{
    if(!r.selecting||busy()||r.view!=='original')return;
    event.preventDefault();selection.setPointerCapture(event.pointerId);selection.focus();
    remember();
    r.drag={point:point(event),region:r.region?{...r.region}:null,kind:event.target.dataset.handle||(event.target.id==='removalRegion'?'move':'draw')};
  });
  selection.addEventListener('pointermove',event=>{
    if(!r.drag)return;
    const p=point(event),old=r.drag.region,origin=r.drag.point,kind=r.drag.kind;
    if(kind==='move'&&old)r.region={...old,x:clip(old.x+p.x-origin.x,0,1-old.width),y:clip(old.y+p.y-origin.y,0,1-old.height)};
    else{
      const anchor=old&&kind!=='draw'?{x:kind.includes('w')?old.x+old.width:old.x,y:kind.includes('n')?old.y+old.height:old.y}:origin;
      r.region={x:Math.min(p.x,anchor.x),y:Math.min(p.y,anchor.y),width:Math.abs(p.x-anchor.x),height:Math.abs(p.y-anchor.y)};
    }
    paint();
  });
  const finish=()=>{if(!r.drag)return;r.drag=null;invalidate();};
  selection.addEventListener('pointerup',finish);selection.addEventListener('pointercancel',finish);
  for(const id of ['removalX','removalY','removalW','removalH'])find(id).addEventListener('change',()=>{
    if(!r.info||busy())return;
    remember();
    const x=clip(Number(find('removalX').value)||0,0,r.info.width-2),y=clip(Number(find('removalY').value)||0,0,r.info.height-2);
    r.region={x:x/r.info.width,y:y/r.info.height,width:clip(Number(find('removalW').value)||2,2,r.info.width-x)/r.info.width,height:clip(Number(find('removalH').value)||2,2,r.info.height-y)/r.info.height};
    invalidate();
  });
  for(const id of ['removalStart','removalEnd','removalQuality','removalFormat','removalDevice'])find(id).addEventListener('change',()=>{
    if(id==='removalDevice')r.deviceChosen=true;
    if(r.info){find('removalStart').value=clip(Number(find('removalStart').value)||0,0,r.info.duration);find('removalEnd').value=clip(Number(find('removalEnd').value)||0,0,r.info.duration);}
    invalidate();
  });
  find('removalChoose').addEventListener('click',choose);
  find('removalUseCurrent').addEventListener('click',()=>state.media?.path&&load(state.media.path));
  find('removalDraw').addEventListener('click',()=>{if(busy())return;r.selecting=!r.selecting;player.pause();refresh();});
  find('removalClear').addEventListener('click',()=>{if(busy())return;remember();r.region=null;r.selecting=true;invalidate();});
  find('removalFullRange').addEventListener('click',()=>{if(!r.info||busy())return;find('removalStart').value=0;find('removalEnd').value=r.info.duration;invalidate();});
  find('removalFolder').addEventListener('click',folder);
  find('removalViewOriginal').addEventListener('click',()=>showView('original'));
  find('removalViewResult').addEventListener('click',()=>showView('result'));
  find('removalPreview').addEventListener('click',()=>process(true));
  find('removalStartJob').addEventListener('click',()=>process(false));
  find('removalInstall').addEventListener('click',async()=>{
    if(state.versionMismatch){toast('请先重启后台并刷新页面','error');return;}
    if(busy()||r.installing)return;r.installing=true;refresh();
    try{await api('/api/removal/models/install',{device:find('removalDevice').value});await modelPoll();}
    catch(error){r.installing=false;toast(error.message,'error');refresh();}
  });
  find('removalCancelInstall').addEventListener('click',async()=>{try{await api('/api/removal/models/cancel',{});}catch(error){toast(error.message,'error');}});
  find('removalCancelJob').addEventListener('click',async()=>{if(!r.job&&!r.scanning)return;try{await api(r.scanning?'/api/removal/scan/cancel':'/api/removal/jobs/'+r.job.id+'/cancel',r.scanning?{scan_id:r.scanId}:{});find('removalJobStatus').textContent='正在取消…';}catch(error){toast(error.message,'error');}});
  find('removalAddSegment').addEventListener('click',()=>{
    if(busy()||!r.info)return;
    remember();
    const start=clip(r.view==='result'?player.currentTime+(r.preview?.report?.origin_start||0):player.currentTime,0,Math.max(0,r.info.duration-.05));
    r.segments??=[];r.segments.push({id:crypto.randomUUID(),start,end:Math.min(r.info.duration,start+3),enabled:true,source:'manual'});
    r.segments.sort((a,b)=>a.start-b.start);renderSegments();invalidate();
  });
  find('removalSegments').addEventListener('change',event=>{
    const row=event.target.closest('[data-index]');if(!row||busy())return;
    const segment=r.segments[Number(row.dataset.index)],field=event.target.dataset.field;
    remember();
    if(field==='enabled'){segment.enabled=event.target.checked;if(segment.stale&&segment.enabled){segment.source='manual';segment.stale=false;}}
    else if(field==='start'||field==='end'){
      const value=clip(Number(event.target.value)||0,0,r.info.duration);
      if((field==='start'&&segment.end-value<.04)||(field==='end'&&value-segment.start<.04)){toast('结束时间需至少晚于开始0.04秒','error');renderSegments();return;}
      segment[field]=value;segment.source='manual';segment.stale=false;
    }
    renderSegments();invalidate();
  });
  find('removalSegments').addEventListener('click',event=>{
    const row=event.target.closest('[data-index]');if(!row||busy())return;
    const index=Number(row.dataset.index);
    if(event.target.closest('[data-delete]')){remember();r.segments.splice(index,1);renderSegments();invalidate();return;}
    if(event.target.matches('input'))return;
    const time=r.segments[index].start;if(r.view==='result')showView('original');
    if(player.readyState)player.currentTime=time;else player.addEventListener('loadedmetadata',()=>{player.currentTime=time;},{once:true});
  });
  find('removalScan').addEventListener('click',async()=>{
    if(state.versionMismatch){toast('请先重启后台并刷新页面','error');return;}
    if(busy()||!r.info||!r.region)return;
    r.scanning=true;r.scanId=null;r.selecting=false;const binding=JSON.stringify({path:r.info.path,region:r.region});refresh();
    try{
      let response=await api('/api/removal/scan',configuration());
      r.scanId=response.scan.id;
      refresh();
      while(['running','downloading'].includes(response.scan.status)){
        find('removalJobStatus').textContent=response.scan.message;find('removalProgress').value=response.scan.progress||0;
        await new Promise(resolve=>setTimeout(resolve,500));response=await api('/api/removal/scan?id='+encodeURIComponent(r.scanId));
      }
      const scan=response.scan;find('removalJobStatus').textContent=scan.message;
      if(scan.status==='done'){
        remember();
        r.segments=[...(r.segments||[]).filter(s=>s.source==='manual'),...scan.segments].sort((a,b)=>a.start-b.start);
        r.scanBinding=binding;renderSegments();invalidate();
        find('removalScanHint').textContent=`已扫描全片 · 采样间隔 ${scan.interval||.25} 秒；可修改、删除或手动补充候选段。`;
      }else if(scan.error)toast(scan.error,'error',8000);
    }catch(error){toast(error.message,'error');}
    finally{r.scanning=false;refresh();}
  });
  find('removalLocate').addEventListener('click',async()=>{if(!r.lastOutput)return;try{await api('/api/open_folder',{path:r.lastOutput});}catch(error){toast(error.message,'error');}});
  find('removalUseResult').addEventListener('click',async()=>{if(!r.result||busy())return;const path=r.result.output_path;setMode('extract');await loadMedia(path);});
  find('removalReconnect').addEventListener('click',async()=>{
    if(!r.detached||busy())return;const pending=r.detached;r.job={id:pending.id};r.detached=null;refresh();
    try{await pollJob(r.epoch,pending.key);}catch(error){r.job=null;if(error.status!==404)r.detached=pending;toast(error.message,'error');refresh();}
  });
  player.addEventListener('loadedmetadata',paint);
  new ResizeObserver(paint).observe(stage);

  function activate(active) {
    r.active=active;
    for(const id of ['removalSourceCard','removalModelCard','removalQualityCard','removalWorkspace','removalInspector'])find(id).hidden=!active;
    if(active){setSaveState('消除设置自动保存');refresh();modelPoll();}else{player.pause();setSaveState(state.dirty?'未保存':'已保存');}
  }
  function handleKey(event) {
    const editing=event.target.isContentEditable||['INPUT','SELECT','TEXTAREA'].includes(event.target.tagName);
    if(event.ctrlKey&&event.key.toLowerCase()==='s'){event.preventDefault();save(true).then(ok=>ok&&toast('消除设置已保存','success'));return;}
    if(editing||event.isComposing)return;
    if(event.code==='Space'&&!event.target.closest('button,a,video,dialog')){event.preventDefault();player.paused?player.play().catch(()=>{}):player.pause();}
    if(event.ctrlKey&&event.key.toLowerCase()==='z'&&!busy()&&r.undo.length){event.preventDefault();undo();}
    if(event.target===selection&&r.region&&!busy()&&['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)){
      event.preventDefault();const step=event.shiftKey?10:1;
      if(event.key==='ArrowLeft')r.region.x=clip(r.region.x-step/r.info.width,0,1-r.region.width);
      if(event.key==='ArrowRight')r.region.x=clip(r.region.x+step/r.info.width,0,1-r.region.width);
      if(event.key==='ArrowUp')r.region.y=clip(r.region.y-step/r.info.height,0,1-r.region.height);
      if(event.key==='ArrowDown')r.region.y=clip(r.region.y+step/r.info.height,0,1-r.region.height);
      invalidate();
    }
  }
  window.SubtitleRemoval={activate,busy,load,save,handleKey,frameBox:removalFrameBox};
  activate(state.settings.mode==='remove');
})();
