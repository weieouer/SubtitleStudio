// 素材齐全只询问是否对比；用户确认前不改变文字、时间或参考结果。
(() => {
  let prompted='',pending=false;
  function maybePrompt() {
    if(pending||state.settings.mode!=='align'||editLocked()||!state.media||!state.cues.length||!state.script.length)return;
    const key=JSON.stringify([state.mediaEpoch,state.media.path,state.subtitleFile?.path,
      state.scriptDocument?.text||state.script.map(line=>line.text).join('\n')]);
    if(key===prompted)return;
    pending=true;prompted=key;
    setTimeout(async()=>{
      try{
        if(editLocked()){prompted='';return;}
        const path=projectPath(),epoch=state.mediaEpoch;
        const confirmed=await confirmDialog('音视频、字幕和参考文本已齐全。\n是否开始对比校验？\n只标出错字、漏句、多句和上下文问题，不自动改写字幕。');
        if(confirmed&&path===projectPath()&&epoch===state.mediaEpoch&&!editLocked())await verifyOnly();
      }finally{pending=false;}
    },0);
  }
  window.SubtitleComparison={maybePrompt};
})();
