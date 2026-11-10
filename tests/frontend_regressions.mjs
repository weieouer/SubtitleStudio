import vm from 'node:vm';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';

export async function runFrontendTests(source) {
  const results = [];
  const extract = name => {
    const start = source.search(new RegExp(`^(?:async )?function ${name}\\(`, 'm'));
    const next = source.slice(start + 1).search(/^(?:async )?function \w+\(/m);
    assert(start >= 0 && next >= 0, name);
    return source.slice(start, start + 1 + next);
  };
  const noop = () => {};
  const make = (overrides = {}, functions = []) => {
    const elements = new Map();
    const context = vm.createContext({
      state: { media: {path: 'A.wav'}, srtPath: '', mediaEpoch: 1, cues: [{start: 0, end: 1, text: '原字幕'}],
        settings: {mode: 'extract', sound: true}, script: [], scriptName: '', selected: -1, selectedCues:new Set(), initialCues:null,
        history: [], redoStack: [], asrEpoch: 0, jobId: null, submitting: false, uploading: false,
        operation: '', loading: false, pendingResult: null, pendingSaves: 0,
        editRevision: 1, dirty: true },
      $: id => { if (!elements.has(id)) elements.set(id, {hidden: true, textContent: '', style: {}, disabled: false, setAttribute:noop}); return elements.get(id); },
      document: {activeElement: {blur: noop}}, window:{}, crypto: {randomUUID}, waveCanvas: null, cueBody: {querySelector:()=>null},
      escapeHtml: String, toast: noop, renderAll: noop, renderTable: noop, renderScript: noop, updateButtons: noop, updateOverlay: noop,
      confirmDialog: async () => true, selectCue: noop,
      updateJob: noop, drawWave: noop, renderSubtitleSource: noop, downloadBusy: () => false, setSaveState: noop, playSound: noop,
      modelAvailable: () => true, collectSettings: () => ({mode: 'extract', model: 'medium', device: 'cpu', gap: .6, vad: true}),
      scheduleSave: noop, saveProject: async () => true, saveConfig: async () => true,
      matchScript: async () => true, exportSrt: async () => ({path: 'A.srt'}),
      rematchDerivedCues: async()=>true,
      verificationCache:null,
      APP_VERSION: "3.1.0", projectConflicts: new Set(), projectVersions: new Map(), projectSaveChain: Promise.resolve(), configSaveChain: Promise.resolve(),
      HISTORY_LIMIT: 60, SAVE_DELAY: 1200, SESSION_TOKEN: 'test-token',
      jobMsg: {textContent: ''}, sleep: async () => {},
      setTimeout: callback => setTimeout(callback, 1), clearTimeout,
      ...overrides,
    });
    context.runReferenceMatch = body => context.api('/api/script/match',body);
    vm.runInContext(['projectPath', 'editLocked', 'snapshot', 'restoreSnapshot', 'sameDocument', 'simplifiedText', 'normalizeCue', 'pushHistory', 'replaceCues', 'clamp', 'cueExcluded', 'verificationStageName', 'verificationStage', 'releaseVerificationExclusions', 'hasCompletedModification', 'completeModification', 'markNoReference', 'cueTimingIssues', 'cueVerificationIssues', 'computeCueVerification', 'cueVerification', 'cueReportVerdict', 'verificationStatistics', 'cueReportKey', 'countChangedCues', 'stopWaitingForJob', 'showVerificationReport', 'cueNeedsReview', 'canAdoptCue', 'adoptCue', 'adjustedPresentationCues', 'presentationCues', 'selectedCueIndices', 'contiguousSelection', 'formatElapsed', ...functions].map(extract).join('\n'), context);
    return context;
  };
  // 与 backend/core/calibration.py 的 reference_copy 合同一致：只复制已确认的参考文字，
  // 不增删条目、不改时间；参考不确定的条目原样留在待复核列表。
  const copyReferenceCues = (body, strip = value => String(value ?? '')) => {
    const changed_indices = [], completed_indices = [], pending_indices = [];
    const cues = body.cues.map((cue, index) => {
      const match = cue.reference_match || {};
      const reference = match.reference || {};
      const confirmed = ['high','typo','homophone','missing','extra'].includes(match.status)
        || (match.status === 'ambiguous' && !!match.candidate_confirmed);
      const usable = !!cue.text && !!reference.text && !reference.reference_only
        && !match.stale && (match.query_text ?? cue.text) === cue.text;
      if (!confirmed || !usable) { pending_indices.push(index); return cue; }
      const target = body.keep_punc === false ? strip(reference.text) : reference.text;
      const next = {...cue, text:target, script_text:reference.text, words:[],
        reference_match:{...match, status:'high', label:'参考一致', query_text:target,
          stale:false, differences:[]}, match_needs_review:false, verification_stage:'calibrate'};
      if (target !== cue.text) {
        next.edited = true;
        if (cue.alignment) next.alignment = {...cue.alignment, stale:true};
        changed_indices.push(index);
      }
      completed_indices.push(index);
      return next;
    });
    return {mode:'reference_copy', cues, changed_indices, completed_indices, pending_indices,
      count:cues.length, splits:0, merged:0, merge_suggestions:0};
  };
  const test = async (name, run) => {
    try { await run(); results.push({name, passed: true}); }
    catch(error) { results.push({name,passed:false,error:String(error.stack||error)}); }
  };

  await test('subtitle_padding_caps_at_one_second_next_cue_and_media_end_without_mutating_raw', async () => {
    const ctx=make({},['presentationCues']);
    ctx.state.settings.subtitleLead=.4;ctx.state.settings.subtitleTail=1;
    ctx.state.media.duration=4;
    ctx.state.cues=[{start:1,end:2,text:'first'},{start:2.2,end:3.5,text:'second'},{start:3.6,end:3.9,text:'excluded',exclude_export:true}];
    const raw=JSON.stringify(ctx.state.cues);ctx.state.timingPreview=true;const result=ctx.presentationCues();
    assert.equal(result.length,2);
    assert.equal(result[0].start,.6);
    assert.equal(result[0].end,result[1].start);
    assert.equal(result[1].start,2);
    assert.equal(result[1].end,4);
    assert.equal(JSON.stringify(ctx.state.cues),raw);
  });

  await test('preview_wrap_and_time_display_keep_character_limit_and_two_decimals', async () => {
    const ctx=make({pad:(n,len=2)=>String(n).padStart(len,'0')},['wrapSubtitleText','fmtTime']);
    const wrapped=ctx.wrapSubtitleText('一二三四五六七八九十甲乙abc',10);
    assert(wrapped.split('\n').every(line=>[...line].length<=10));
    assert.equal(wrapped.replaceAll('\n',''),'一二三四五六七八九十甲乙abc');
    assert.equal(ctx.fmtTime(59.999),'00:01:00.00');
    assert.equal(ctx.fmtTime(2.128),'00:00:02.13');
  });

  await test('cue_statistics_count_all_alignment_and_deduplicate_suspect_flags', async () => {
    const ctx = make({}, ['cueNeedsReview', 'cueStatistics']);
    const stats = ctx.cueStatistics([
      {text:'alpha',start:0,end:1,is_noise:true,needs_review:true,confidence:.2,alignment:{engine:'test'}},
      {text:'beta',start:1,end:2,confidence:.9},
      {text:'gamma',start:2,end:3,match_needs_review:true,alignment:{engine:'test',stale:true}},
    ]);
    assert.equal(stats.total, 3);
    assert.equal(stats.aligned, 1);
    assert.equal(stats.suspect, 2);
  });

  await test('legacy_single_line_settings_migrate_without_reversing_export_behavior', async () => {
    const ctx = make({PARAM_LIMITS: {gap:{min:0,max:2},width:{min:6,max:30}}}, ['normalizeSettings']);
    ctx.state.settings.singleLine = true;
    ctx.state.settings.excludeNoise = true;
    ctx.state.settings.wrapSubtitles = true;
    ctx.normalizeSettings();
    assert.equal(ctx.state.settings.wrapSubtitles, false);
    assert.equal('singleLine' in ctx.state.settings, false);
    assert.equal('excludeNoise' in ctx.state.settings, false);
    ctx.state.settings.singleLine = false;
    ctx.normalizeSettings();
    assert.equal(ctx.state.settings.wrapSubtitles, true);
    ctx.state.settings.wrapSubtitles = false;
    ctx.normalizeSettings();
    assert.equal(ctx.state.settings.wrapSubtitles, false);
  });

  await test('legacy_drop_interjections_migrates_to_keep_flag', async () => {
    for (const [legacy, expected] of [[true, false], [false, true]]) {
      const ctx = make({PARAM_LIMITS: {gap:{min:0,max:2},width:{min:6,max:30}}}, ['normalizeSettings']);
      ctx.state.settings.dropInterjections = legacy;
      ctx.normalizeSettings();
      assert.equal(ctx.state.settings.keepInterjections, expected);
      assert.equal('dropInterjections' in ctx.state.settings, false);
    }
    const ctx = make({PARAM_LIMITS: {gap:{min:0,max:2},width:{min:6,max:30}}}, ['normalizeSettings']);
    ctx.normalizeSettings();
    assert.equal(ctx.state.settings.keepInterjections, false);
  });

  await test('zero_padding_defaults_migrate_once_then_remember_manual_values', async () => {
    const ctx=make({PARAM_LIMITS:{gap:{min:0,max:2},width:{min:6,max:30}}},['normalizeSettings']);
    ctx.state.settings.subtitleTail=1;ctx.normalizeSettings();
    assert.equal(ctx.state.settings.subtitleLead,0);assert.equal(ctx.state.settings.subtitleTail,0);
    ctx.state.settings.subtitleLead=.15;ctx.state.settings.subtitleTail=.25;ctx.normalizeSettings();
    assert.equal(ctx.state.settings.subtitleLead,.15);assert.equal(ctx.state.settings.subtitleTail,.25);
  });

  await test('alignment_only_updates_times_without_script_verification',async()=>{
    let started=0,matched=0,sounds=0,reverified=[];
    const ctx=make({confirmDialog:async()=>{throw new Error('对齐不应询问参考')},startAsr:async(task)=>{assert.equal(task,'align');started++;return {status:'done'}},matchScript:async()=>{matched++;return true},rematchDerivedCues:async(indices,options)=>{reverified.push({indices:[...indices],preserveModification:options?.preserveModification});return true},playSound:()=>sounds++},['alignAndVerify']);
    await ctx.alignAndVerify();assert.equal(started,1);assert.equal(matched,0);assert.equal(sounds,1);assert.equal(reverified.length,0);
    assert(ctx.state.cues.every(c=>c.verification_stage==='align'));
    ctx.state.script=[{text:'参考'}];await ctx.alignAndVerify();
    assert.equal(started,2);assert.equal(matched,0);assert.equal(sounds,2);assert.equal(reverified.length,0);
  });

  await test('waveform_normalizes_quiet_speech_and_gain_changes_display_only',async()=>{
    const ctx=make({},['waveformHeights']),peaks=[0,.002,.01,.02,.003,0],raw=JSON.stringify(peaks);
    const normal=ctx.waveformHeights(peaks,0,6,6),boost=ctx.waveformHeights(peaks,0,6,6,2);
    assert.equal(normal[0],0);assert(normal[2]>.5);assert(boost[1]>normal[1]);assert([...boost].every(n=>n>=0&&n<=1));assert.equal(JSON.stringify(peaks),raw);
  });

  await test('asr_duplicate_submission_locked_before_request_returns', async () => {
    let release, calls = 0;
    const ctx = make({api: async path => {
      if (path === '/api/jobs') {calls++; return new Promise(resolve => {release = resolve;});}
      return {job: {status: 'done', cues: [{start: 0, end: 1, text: '结果'}]}};
    }}, ['startAsr']);
    const first = ctx.startAsr();
    assert(ctx.state.jobId && ctx.state.submitting);
    assert.equal(await ctx.startAsr(), null);
    assert.equal(calls, 1);
    release({job: {status: 'queued'}});
    assert.equal((await first).status, 'done');
  });

  await test('asr_retains_manual_edits_and_offers_result', async () => {
    let saved = 0;
    const ctx = make({saveProject: async () => {saved++; return true;}}, ['startAsr']);
    ctx.api = async path => {
      if (path === '/api/jobs') return {job: {status: 'queued'}};
      ctx.state.cues[0].text = '人工校对';
      return {job: {status: 'done', cues: [{start: 0, end: 1, text: '识别结果'}]}};
    };
    assert.equal((await ctx.startAsr()).status, 'error');
    assert.equal(ctx.state.cues[0].text, '人工校对');
    assert.equal(ctx.state.pendingResult.job.cues[0].text, '识别结果');
    assert.equal(saved, 0);
  });

  await test('poll_and_result_download_retry_same_task', async () => {
    let polls = 0, downloads = 0, identities = [];
    const ctx = make({}, ['startAsr']);
    ctx.api = async path => {
      if (path === '/api/jobs') return {job: {status: 'queued'}};
      identities.push(ctx.state.jobId);
      if (path.includes('result=1')) { if (++downloads === 1) throw new Error('temporary result failure'); }
      else if (++polls === 1) throw new Error('temporary status failure');
      return {job: {status: 'done', cues: [{start: 0, end: 1, text: '结果'}]}};
    };
    assert.equal((await ctx.startAsr()).status, 'done');
    assert.equal(new Set(identities).size, 1);
    assert(identities.every(Boolean));
    assert.equal(downloads, 2);
  });

  await test('standalone_srt_save_uses_source_and_revision', async () => {
    let payload;
    const ctx = make({api: async (path, body) => {payload = body; return {revision: 1, updated_at: '2026-11-04 12:00:00'};}}, ['saveProject']);
    ctx.state.media = null; ctx.state.srtPath = 'standalone.srt';
    assert.equal(await ctx.saveProject(), true);
    assert.equal(payload.source_path, 'standalone.srt');
    assert.equal(payload.media_path, ''); assert.equal(payload.expected_revision, 0);
    assert.equal(ctx.state.dirty, false);
  });

  await test('unbound_cues_never_report_saved', async () => {
    const ctx = make({}, ['saveProject']); ctx.state.media = null;
    assert.equal(await ctx.saveProject(), false);
  });

  await test('saves_are_serial_and_latest_snapshot_wins', async () => {
    let release;
    const payloads = [];
    const ctx = make({api: async (path, body) => {
      payloads.push(body);
      if (payloads.length === 1) return new Promise(resolve => {release = resolve;});
      return {revision: 2, updated_at: '2026-11-04 12:00:00'};
    }}, ['saveProject']);
    const first = ctx.saveProject();
    await Promise.resolve();
    ctx.state.cues[0].text = '最新修改'; ctx.state.editRevision++;
    const second = ctx.saveProject();
    assert.equal(payloads.length, 1);
    release({revision: 1, updated_at: '2026-11-04 12:00:00'});
    assert.equal(await first, true); assert.equal(await second, true);
    assert.equal(payloads[1].expected_revision, 1);
    assert.equal(payloads[1].cues[0].text, '最新修改');
    assert.equal(payloads[0].cues[0].text, '原字幕');
    assert.equal(ctx.state.dirty, false);
  });

  await test('save_conflict_retains_dirty_edits', async () => {
    const ctx = make({api: async () => {throw Object.assign(new Error('conflict'), {status: 409});}}, ['saveProject']);
    assert.equal(await ctx.saveProject(), false);
    assert.equal(ctx.state.dirty, true);
    assert.equal(ctx.state.cues[0].text, '原字幕');
  });

  await test('failed_save_blocks_media_switch', async () => {
    let requests = 0;
    const ctx = make({saveProject: async () => false, api: async () => {requests++;}}, ['loadMedia']);
    assert.equal(await ctx.loadMedia('B.wav'), false);
    assert.equal(ctx.state.media.path, 'A.wav'); assert.equal(requests, 0);
  });

  await test('stale_waveform_is_ignored', async () => {
    let release;
    const ctx = make({api: () => new Promise(resolve => {release = resolve;})}, ['loadWaveform']);
    const pending = ctx.loadWaveform(); ctx.state.media = {path: 'B.wav'}; ctx.state.mediaEpoch++;
    ctx.state.waveform = {tag: 'B'}; release({tag: 'A'}); await pending;
    assert.equal(ctx.state.waveform.tag, 'B');
  });

  await test('unsorted_cue_preview_is_correct', async () => {
    const ctx = make({}, ['findCueAt']); ctx.state.cues = [{start: 10, end: 11}, {start: 20, end: 21}, {start: 0, end: 1}];
    assert.equal(ctx.findCueAt(.5).start, 0);
  });

  await test('manual_midpoint_segment_splits_current_text_without_duplicating_or_touching_next', async () => {
    const ctx = make({api:async(path,body)=>{
      assert.equal(path,'/api/segment');assert.equal(body.mode,'midpoint');assert.equal(body.cues.length,1);
      return {cues:[{...body.cues[0],text:'中间',end:.5,display_id:'1.1'},
        {...body.cues[0],text:'字幕',start:.5,display_id:'1.2'}],splits:1};
    }}, ['doSegment']);
    ctx.state.cues=[{start:0,end:1,text:'中间字幕'},{start:2,end:3,text:'下一条原文'}].map(ctx.normalizeCue);
    const next=JSON.stringify(ctx.state.cues[1]);
    ctx.state.selectedCues=new Set([0]);
    await ctx.doSegment();
    assert.equal(ctx.state.cues.length,3);
    assert.equal(ctx.state.cues[0].text+ctx.state.cues[1].text,'中间字幕');
    assert.equal(JSON.stringify(ctx.state.cues[2]),next);
    assert.equal(ctx.state.cues[0].end,ctx.state.cues[1].start);assert.equal(ctx.state.operation,'');
  });

  await test('replacement_and_undo_keep_one_document_history', async () => {
    const ctx = make({}, ['undo']);
    ctx.replaceCues([{start: 0, end: 1, text: '新字幕'}]);
    ctx.undo(); assert.equal(ctx.state.cues[0].text, '原字幕');
    ctx.replaceCues([{start: 0, end: 1, text: '另一个工程'}], {reset: true});
    assert.equal(ctx.state.history.length, 0); assert.equal(ctx.state.redoStack.length, 0);
  });

  await test('selected_split_labels_children_and_preserves_neighbors_then_merge_retains_sources', async () => {
    const ctx=make({selectCue:()=>{},api:async(path,body)=>{
      assert.equal(body.cues.length,1);assert.equal(body.cues[0].text,'中间字幕');
      return {cues:[{...body.cues[0],text:'中间',start:1,end:2,display_id:'2.1'},{...body.cues[0],text:'字幕',start:2,end:3,display_id:'2.2'}],splits:1};
    }},['doSegment','doMerge']);
    ctx.state.cues=[{start:0,end:1,text:'前'},{start:1,end:3,text:'中间字幕'},{start:3,end:4,text:'后'}].map(ctx.normalizeCue);
    const first=JSON.stringify(ctx.state.cues[0]),last=JSON.stringify(ctx.state.cues[2]);
    ctx.state.selectedCues=new Set([1]);await ctx.doSegment();
    assert.equal(ctx.state.cues[1].display_id,'2.1');assert.equal(ctx.state.cues[2].display_id,'2.2');
    assert.equal(JSON.stringify(ctx.state.cues[0]),first);assert.equal(JSON.stringify(ctx.state.cues[3]),last);
    ctx.state.cues[1].text='中间';ctx.state.cues[2].text='字幕';
    ctx.state.selectedCues=new Set([1,2]);await ctx.doMerge();
    assert.equal(ctx.state.cues.length,3);assert.equal(ctx.state.cues[1].text,'中间字幕');
    assert.equal(ctx.state.cues[1].asr_sources.length,1);
    assert.equal(ctx.state.cues[1].original_text,'中间字幕');
    assert.equal(JSON.stringify(ctx.state.cues[0]),first);assert.equal(JSON.stringify(ctx.state.cues[2]),last);
  });

  await test('multi_select_merge_joins_text_in_order_and_spans_first_start_to_last_end',async()=>{
    let rematched=0;
    const ctx=make({selectCue:()=>{},rematchDerivedCues:async()=>{rematched++;return true}},['doMerge']);
    ctx.state.cues=[{start:0,end:1,text:'一'},{start:1,end:2,text:'二'},{start:2.2,end:3.5,text:'三'},{start:4,end:5,text:'四'}].map(ctx.normalizeCue);
    // 自动变灰不随合并继承：碎片合并后应恢复导出；只有手动“不导出”才保留。
    for(const cue of ctx.state.cues.slice(0,3)){cue.exclude_export=true;cue.verification_excluded=true;}
    ctx.state.selectedCues=new Set([0,1,2]);await ctx.doMerge();
    assert.equal(ctx.state.cues.length,2);
    assert.equal(ctx.state.cues[0].text,'一二三');
    assert.equal(ctx.state.cues[0].start,0);assert.equal(ctx.state.cues[0].end,3.5);
    assert.equal(ctx.state.cues[0].asr_sources.map(c=>c.text).join(''),'一二三');
    assert(!ctx.state.cues[0].exclude_export);assert(!('verification_excluded' in ctx.state.cues[0]));
    assert.equal(ctx.state.cues[1].text,'四');
    assert.equal(ctx.state.history.length,1);
    ctx.state.script=[{text:'参考'}];ctx.state.selectedCues=new Set([0,1]);await ctx.doMerge();
    assert.equal(rematched,1);assert.equal(ctx.state.cues.length,1);
  });

  await test('merge_rejects_non_contiguous_multi_selection',async()=>{
    const messages=[];const ctx=make({toast:message=>messages.push(message),selectCue:()=>{}},['doMerge']);
    ctx.state.cues=[{start:0,end:1,text:'一'},{start:1,end:2,text:'二'},{start:2,end:3,text:'三'}].map(ctx.normalizeCue);
    const before=JSON.stringify(ctx.state.cues);ctx.state.selectedCues=new Set([0,2]);await ctx.doMerge();
    assert.equal(JSON.stringify(ctx.state.cues),before);
    assert(messages.some(message=>message.includes('连续相邻')));
  });

  await test('merge_suggestion_is_red_badge_and_counted_for_review',async()=>{
    const ctx=make({},['escapeHtml','badgesHtml','cueReviewHtml','filteredRows']);
    const cue=ctx.normalizeCue({start:0,end:.3,text:'中',merge_suggestion:{direction:'next',reason:'疑似拆出的短碎片'}},0);
    ctx.state.cues=[cue,ctx.normalizeCue({start:.3,end:1,text:'毒的人'},1)];
    ctx.state.filter='low';ctx.state.search='';
    assert(ctx.cueNeedsReview(cue));assert(ctx.filteredRows().some(row=>row.i===0));
    const badges=ctx.badgesHtml(cue);assert(badges.includes('建议合并'));assert(badges.includes('merge-suggest'));
    assert(ctx.cueReviewHtml(cue,0).includes('建议合并'));
  });

  await test('modified_completion_is_derived_and_reset_restores_original_text_timing_and_exclusion', async () => {
    const ctx=make({},['resetCues','undo','filteredRows','cueNeedsReview']);
    ctx.state.cues=[{start:0,end:1,text:'原文',is_noise:true},{start:1,end:2,text:'另一条'}].map(ctx.normalizeCue);
    ctx.state.initialCues=JSON.parse(JSON.stringify(ctx.state.cues));
    const before=ctx.snapshot();ctx.state.cues.forEach(ctx.completeModification);ctx.pushHistory(before);
    assert(ctx.state.cues.every(ctx.hasCompletedModification));assert.equal(ctx.state.cues[0].is_noise,true);
    ctx.state.filter='low';ctx.state.search='';assert.equal(ctx.filteredRows().length,0);
    ctx.undo();assert(ctx.state.cues.every(c=>!ctx.hasCompletedModification(c)));assert.equal(ctx.filteredRows().length,2);
    ctx.state.cues[0].text='修改';ctx.state.cues[0].start=.3;ctx.state.cues[0].exclude_export=true;
    await ctx.resetCues();assert.equal(ctx.state.cues[0].text,'原文');assert.equal(ctx.state.cues[0].start,0);assert(!ctx.state.cues[0].exclude_export);
    ctx.undo();assert.equal(ctx.state.cues[0].text,'修改');assert.equal(ctx.state.cues[0].start,.3);assert(ctx.state.cues[0].exclude_export);
  });

  await test('api_timeout_and_session_header', async () => {
    const ctx = make({AbortController, setTimeout, clearTimeout, fetch: async (path, options) => {
      assert.equal(options.headers['X-SubtitleStudio-Token'], 'test-token');
      return new Promise((resolve, reject) => options.signal.addEventListener('abort', () => reject(Object.assign(new Error(), {name: 'AbortError'}))));
    }}, ['api']);
    await assert.rejects(() => ctx.api('/api/config', null, null, {timeout: 1}), /超时/);
  });

  await test('time_input_rejects_empty_or_invalid_clock_values', async () => {
    const ctx = make({}, ['parseTime']);
    for (const value of ['', ' ', '00:60:01', '00:01:60', '1:99', 'Infinity']) assert.equal(ctx.parseTime(value), null);
    assert.equal(ctx.parseTime('00:01:02,250'), 62.25);
    assert.equal(ctx.parseTime('62.25'), 62.25);
  });

  await test('default_audio_gain_is_louder_and_volume_controls_it', async () => {
    const peaks = [];
    class AudioMock {
      constructor() {this.currentTime = 1; this.state = 'running'; this.destination = {};}
      createOscillator() {return {frequency: {setValueAtTime() {}}, connect() {}, start() {}, stop() {}};}
      createGain() {return {gain: {setValueAtTime() {}, exponentialRampToValueAtTime(value) {if (value > .001) peaks.push(value);}}, connect() {}};}
    }
    const ctx = make({window: {AudioContext: AudioMock}, audioContext: null, lastTick: 0}, ['playSound']);
    ctx.playSound('key'); const defaultPeak=peaks.at(-1);assert(defaultPeak>0);
    ctx.state.settings.volume = 20; ctx.playSound('key'); assert(Math.abs(peaks.at(-1) - defaultPeak/4) < .00001);
    const count = peaks.length; ctx.state.settings.volume = 0; ctx.playSound('success'); assert.equal(peaks.length, count);
  });

  await test('short_cue_drag_stays_positive_and_unlocks_on_lost_capture', async () => {
    const handlers = {};
    const canvas = {getBoundingClientRect: () => ({left: 0, width: 100}), addEventListener: (name, fn) => {handlers[name] = fn;},
      setPointerCapture() {}, hasPointerCapture: () => false, releasePointerCapture() {}};
    const ctx = make({video: {currentTime: 0}, renderAlignmentInspector() {}}, ['waveWindow', 'bindWaveCanvas']);
    ctx.state.media.duration = .005; ctx.state.settings.mode = 'align'; ctx.state.selected = 0;
    ctx.state.cues = [{start: 0, end: .005, text: '短'}];
    ctx.bindWaveCanvas(canvas);
    handlers.pointerdown({clientX: 100, pointerId: 1});
    handlers.pointermove({clientX: -100, pointerId: 1});
    assert(ctx.state.cues[0].end > 0 && ctx.state.cues[0].end <= .005);
    handlers.lostpointercapture({pointerId: 1}); assert.equal(ctx.state.operation, '');
  });

  await test('caption_body_moves_without_changing_duration_and_edges_stop_at_neighbors', async () => {
    const handlers={};
    const canvas={getBoundingClientRect:()=>({left:0,top:0,width:1000}),addEventListener:(name,fn)=>{handlers[name]=fn},setPointerCapture(){},hasPointerCapture:()=>false,releasePointerCapture(){}};
    const ctx=make({waveCanvas:canvas,video:{currentTime:0,paused:true},renderAlignmentInspector(){}},['waveWindow','bindWaveCanvas']);
    ctx.state.media.duration=5;ctx.state.settings.subtitleLead=0;ctx.state.settings.subtitleTail=0;
    ctx.state.cues=[{start:0,end:.5,text:'前'},{start:1,end:2,text:'中'},{start:3,end:4,text:'后'}];ctx.state.selected=1;
    ctx.selectCue=i=>{ctx.state.selected=i};
    ctx.bindWaveCanvas(canvas);
    handlers.pointerdown({clientX:300,clientY:32,pointerId:1});
    handlers.pointermove({clientX:900,clientY:32,pointerId:1});
    assert.equal(ctx.state.cues[1].start,2);assert.equal(ctx.state.cues[1].end,3);
    handlers.pointermove({clientX:0,clientY:32,pointerId:1});
    assert.equal(ctx.state.cues[1].start,.5);assert.equal(ctx.state.cues[1].end,1.5);
    handlers.pointerup({pointerId:1});
    handlers.pointerdown({clientX:300,clientY:32,pointerId:2});
    handlers.pointermove({clientX:900,clientY:32,pointerId:2});
    assert.equal(ctx.state.cues[1].start,.5);assert.equal(ctx.state.cues[1].end,3);
    handlers.pointerup({pointerId:2});assert.equal(ctx.state.operation,'');
  });

  await test('stale_zoom_waveform_does_not_replace_current_media', async () => {
    let release;
    const ctx = make({api: () => new Promise(resolve => {release = resolve;})}, ['waveWindow', 'detailCovering', 'rememberWaveDetail', 'requestWaveDetail']);
    ctx.state.detailWaveEpoch = 0; ctx.state.media.duration = 20; ctx.state.waveSpan = 4; ctx.state.waveStart = 5;
    ctx.requestWaveDetail(); await new Promise(resolve => setTimeout(resolve, 5));
    ctx.state.mediaEpoch++; ctx.state.media = {path: 'B.wav', duration: 20}; ctx.state.detailWave = {tag: 'B'};
    release({start: 5, end: 9, peaks: [1]}); await new Promise(resolve => setTimeout(resolve, 5));
    assert.equal(ctx.state.detailWave.tag, 'B');
  });

  await test('reference_and_candidates_follow_punctuation_setting_without_changing_original', async () => {
    const chinese = {window: {}, OpenCC: {Converter: () => value => value}};
    vm.runInNewContext(readFileSync(new URL('../frontend/js/chinese.js', import.meta.url), 'utf8'), chinese);
    const ctx = make({window: chinese.window}, ['subtitleText','suggestHtml','cueReviewHtml','escapeHtml']);
    const cue = {script_text:'你好，世界！12:30，3.5%',reference_match:{score:1,alternatives:[{text:'你好，世界！',paragraph_start:1}]}};
    const original=JSON.stringify(cue);
    ctx.state.settings.keepPunc=false;
    assert(ctx.suggestHtml(cue,0).includes('你好世界12:30 3.5%') || ctx.suggestHtml(cue,0).includes('你好世界12:303.5%'));
    assert(!ctx.cueReviewHtml(cue,0).includes('你好，世界！'));
    ctx.state.settings.keepPunc=true;
    // 开启标点时保留参考原文的标点，不再二次规整。
    assert(ctx.suggestHtml(cue,0).includes('你好，世界！12:30，3.5%'));
    assert(ctx.cueReviewHtml(cue,0).includes('你好，世界！'));
    assert.equal(JSON.stringify(cue),original);
  });

  const parsedScript = text => ({lines:text.split('\n').filter(Boolean).map((text,index)=>({text,index,role:'',kind:'reference'})),document:{text}});
  const parsedBody = body => {
    if (!body.documents) return parsedScript(body.text || body.path);
    let offset=0;
    const sources=body.documents.map((doc,i)=>{if(i)offset++;const start=offset;offset+=[...doc.text].length;return {id:doc.id || 'source-'+i,name:doc.name,start,end:offset};});
    const result=parsedScript(body.documents.map(doc=>doc.text).join('\n'));
    result.document.sources=sources;result.document.paragraphs=result.lines;
    return result;
  };
  await test('multiple_scripts_append_in_import_order_clear_old_matches_and_save_names', async () => {
    let saved;
    const ctx=make({api:async(path,body)=>path==='/api/script/parse'?parsedBody(body):(saved=body,{revision:1,updated_at:''})},['applyScriptResult','saveProject','restoredScriptNames']);
    ctx.state.cues[0].reference_match={reference:{text:'旧建议'}};ctx.state.history=['old'];ctx.state.redoStack=['old'];
    await ctx.applyScriptResult(parsedScript('第一幕。'),'第一份.txt');
    await ctx.applyScriptResult(parsedScript('第二幕。'),'第二份.txt');
    assert.equal(ctx.state.scriptDocument.text,'第一幕。\n第二幕。');
    assert.equal(ctx.state.scriptName,'第二份.txt');
    assert.deepEqual([...ctx.state.scriptNames],['第一份.txt','第二份.txt']);
    assert.equal(ctx.state.cues[0].reference_match,null);assert.equal(ctx.state.history.length,0);assert.equal(ctx.state.redoStack.length,0);
    await ctx.saveProject();
    assert.deepEqual([...saved.script_names],['第一份.txt','第二份.txt']);
    assert.deepEqual([...ctx.restoredScriptNames(saved)],[...saved.script_names]);
    assert.deepEqual([...ctx.restoredScriptNames({script_lines:[{}],script_name:'旧剧本.txt'})],['旧剧本.txt']);
    assert.equal(ctx.restoredScriptNames(null).length,0);
  });

  await test('failed_or_empty_append_preserves_existing_script_and_suggestions',async()=>{
    const ctx=make({api:async()=>{throw new Error('超过全文上限')}},['applyScriptResult']);
    ctx.state.script=[{text:'已导入'}];ctx.state.scriptDocument={text:'已导入'};ctx.state.scriptNames=['原.txt'];
    ctx.state.cues[0].reference_match={reference:{text:'保留'}};
    const before=JSON.stringify(ctx.state);
    await assert.rejects(ctx.applyScriptResult(parsedScript('新内容'),'新.txt'),/超过/);
    assert.equal(JSON.stringify(ctx.state),before);
    await assert.rejects(ctx.applyScriptResult(parsedScript(''),'空.txt'),/没有/);
    assert.equal(JSON.stringify(ctx.state),before);
  });

  await test('script_drop_batch_uploads_all_files_sequentially_and_unlocks',async()=>{
    const order=[];
    const ctx=make({isScriptName:()=>true,uploadFile:async file=>{assert(ctx.editLocked());order.push(file.name);return {path:file.name}},api:async(path,body)=>parsedBody(body)},['startScriptUpload','abortScriptUpload','applyScriptResult']);
    ctx.state.settings.mode='align';ctx.state.scriptEpoch=0;
    await ctx.startScriptUpload([{name:'一.txt'},{name:'二.txt'}]);
    assert.deepEqual(order,['一.txt','二.txt']);assert.equal(ctx.state.scriptDocument.text,'一.txt\n二.txt');
    assert.deepEqual([...ctx.state.scriptNames],order);assert.equal(ctx.editLocked(),false);
  });

  await test('old_backend_empty_success_does_not_clear_scripts_matches_or_history',async()=>{
    let saves=0;
    const ctx=make({api:async()=>({ok:true,lines:[],document:{version:1,text:''},stats:{total:0}}),scheduleSave:()=>saves++},['applyScriptResult']);
    ctx.state.script=[{text:'保留剧本'}];ctx.state.scriptDocument=parsedBody({documents:[{name:'原.txt',text:'保留剧本'}]}).document;
    ctx.state.scriptNames=['原.txt'];ctx.state.scriptName='原.txt';ctx.state.referenceGaps=[{text:'原漏句'}];
    ctx.state.cues[0].reference_match={reference:{text:'保留建议'}};ctx.state.history=['保留撤销'];ctx.state.redoStack=['保留重做'];
    const before=JSON.stringify(ctx.state);
    await assert.rejects(ctx.applyScriptResult(parsedScript('新剧本'),'新.txt'),/重启.*仅刷新页面无效/);
    assert.equal(JSON.stringify(ctx.state),before);assert.equal(saves,0);
  });

  await test('multi_select_old_backend_stops_with_restart_message_without_importing_zero',async()=>{
    const messages=[];let parses=0,saves=0;
    const ctx=make({toast:(text)=>messages.push(text),scheduleSave:()=>saves++,api:async(path,body)=>{
      if(path==='/api/dialog')return {paths:['一.txt','二.txt']};
      parses++;return body.path?parsedScript('第一份内容'):{ok:true,lines:[],document:{version:1,text:''},stats:{total:0}};
    }},['pickScript','applyScriptResult']);
    ctx.state.settings.mode='align';
    await ctx.pickScript();
    assert.equal(parses,2);assert.equal(ctx.state.script.length,0);assert.equal(saves,0);
    assert.equal(ctx.state.scriptPickerOpen,false);assert.equal(ctx.editLocked(),false);
    assert.equal(messages.length,1);assert.match(messages[0],/后台.*重启/);assert(!messages[0].includes('已导入：0'));
  });

  await test('inconsistent_combined_document_is_rejected_before_commit',async()=>{
    const ctx=make({api:async()=>parsedBody({documents:[{name:'错误.txt',text:'错误的全文'}]})},['applyScriptResult']);
    const before=JSON.stringify(ctx.state);
    await assert.rejects(ctx.applyScriptResult(parsedScript('正确全文'),'正确.txt'),/接口版本不一致/);
    assert.equal(JSON.stringify(ctx.state),before);
  });

  await test('script_picker_imports_every_selected_path_and_cancel_keeps_existing',async()=>{
    let cancel=false;
    const ctx=make({api:async(path,body)=>path==='/api/dialog'?(assert.equal(body.multiple,true),{paths:cancel?[]:['一.txt','二.txt']}):parsedBody(body)},['pickScript','applyScriptResult']);
    ctx.state.settings.mode='align';
    await ctx.pickScript();
    assert.equal(ctx.state.scriptDocument.text,'一.txt\n二.txt');assert.equal(ctx.state.scriptPickerOpen,false);
    cancel=true;await ctx.pickScript();assert.equal(ctx.state.scriptNames.length,2);
  });

  await test('script_slot_shows_latest_name_count_and_ordered_tooltip',async()=>{
    const ctx=make({updateModeUI:()=>{},updateModeNote:()=>{}},['renderScript']);
    const zone=ctx.$('#scriptZone');zone.classList={toggle:()=>{}};
    ctx.state.script=[{text:'一'},{text:'二'}];ctx.state.scriptName='二.txt';ctx.state.scriptNames=['一.txt','二.txt'];
    ctx.renderScript();assert.equal(ctx.$('#scriptStats').textContent,'二.txt ×2');assert.equal(ctx.$('#scriptStats').title,'1. 一.txt\n2. 二.txt');
  });

  await test('pause_split_applies_to_current_cues_and_retains_undo',async()=>{
    const ctx=make({api:async(path,body)=>{
      assert.equal(body.pauses_only,true);assert.equal(body.gap_seconds,.1);
      return {gap_splits:1,cues:[{start:0,end:.2,text:'你好',pause_after:.2},{start:.3,end:.6,text:'走吧',pause_before:.3}]};
    }},['applyPauseSetting','undo']);
    ctx.state.settings.gap=.1;ctx.state.settings.width=10;
    await ctx.applyPauseSetting();assert.equal(ctx.state.cues.length,2);assert.equal(ctx.state.operation,'');
    ctx.undo();assert.equal(ctx.state.cues.length,1);assert.equal(ctx.state.cues[0].text,'原字幕');
  });

  await test('presentation_padding_and_minimum_cannot_refill_detected_pauses',async()=>{
    const ctx=make();ctx.state.media.duration=4;ctx.state.timingPreview=true;
    Object.assign(ctx.state.settings,{subtitleLead:1,subtitleTail:1,subtitleMin:3});
    ctx.state.cues=[{start:0,end:.5,text:'你好',pause_after:.5},{start:1.5,end:2,text:'走吧',pause_before:1.5,pause_after:2}];
    const shown=ctx.presentationCues();
    assert.equal(shown[0].end,.5);assert.equal(shown[1].start,1.5);assert.equal(shown[1].end,2);
    assert.equal(ctx.state.cues[1].start,1.5);
  });

  await test('preview_and_export_share_line_break_setting',async()=>{
    const ctx=make({},['subtitleText','wrapSubtitleText','displayedSubtitleText']);
    ctx.state.settings.width=3;ctx.state.settings.wrapSubtitles=true;
    assert.equal(ctx.displayedSubtitleText('ABCD\nEF'),'ABC\nD\nEF');
    ctx.state.settings.wrapSubtitles=false;assert.equal(ctx.displayedSubtitleText('ABCD\nEF'),'ABCD EF');
  });

  await test('source_metadata_and_unicode_boundaries_reach_match_api',async()=>{
    let received;
    const ctx=make({api:async(path,body)=>{
      if(path==='/api/script/parse')return parsedBody(body);
      received=body.document;return {document:body.document,lines:ctx.state.script,cues:ctx.state.cues};
    }},['applyScriptResult','matchScript']);
    ctx.state.settings.mode='align';
    await ctx.applyScriptResult(parsedScript('B😀。'),'B.txt');
    await ctx.applyScriptResult(parsedScript('A对白。'),'A.txt');
    assert.equal(ctx.state.scriptDocument.sources[1].start,4);
    await ctx.matchScript(true);
    assert.equal(received.sources.length,2);assert.equal(received.sources[0].name,'B.txt');
    assert.equal(received.sources[1].name,'A.txt');
  });

  await test('timing_and_device_settings_do_not_rebuild_all_caption_rows',async()=>{
    let renders=0,waves=0;
    const ctx=make({renderTable:()=>renders++,drawWave:()=>waves++,scheduleConfigSave:()=>{}},['commitSettings']);
    ctx.commitSettings({target:{id:'subtitleTail'}});ctx.commitSettings({target:{id:'deviceSelect'}});
    assert.equal(renders,0);assert.equal(waves,2);
    ctx.commitSettings({target:{id:'keepPunc'}});assert.equal(renders,1);
  });

  await test('verified_good_cues_replace_old_asr_suspicions_with_pass_label',async()=>{
    const ctx=make({escapeHtml:String},['cueNeedsReview','badgesHtml','cueStatistics']);
    const cue={text:'正确台词',start:1,end:2,is_noise:true,needs_review:true,confidence:.5,verification_stage:'verify',
      reference_match:{status:'high',query_text:'正确台词'}};
    const before=JSON.stringify(cue),html=ctx.badgesHtml(cue);
    assert(html.includes('参考一致'));assert(!html.includes('疑似噪音'));assert(!html.includes('未校对'));
    assert.equal(ctx.cueNeedsReview(cue),false);assert.equal(ctx.cueStatistics([cue]).suspect,0);
    assert.equal(JSON.stringify(cue),before);
  });

  await test('verification_report_counts_before_after_and_changed_cues',async()=>{
    const ctx=make({},['verificationStatistics','cueReportKey', 'countChangedCues', 'stopWaitingForJob','showVerificationReport']);
    const good={text:'正确台词',start:0,end:1,verification_stage:'verify',reference_match:{status:'high',query_text:'正确台词'}};
    const before=[good,{text:'错字',start:1,end:2,reference_match:{status:'typo',query_text:'错字',reference:{text:'正字'}}}];
    const after=[good,{text:'正字',start:1,end:2,edited:true,modification_complete:{text:'正字',start:1,end:2},
      reference_match:{status:'high',query_text:'正字',reference:{text:'正字'}}}];
    const report=ctx.showVerificationReport('校准',before,after,{requireReference:true});
    assert.deepEqual(JSON.parse(JSON.stringify(report.before)),{total:2,passed:1,suspect:1,obvious:0,unchecked:0});
    assert.deepEqual(JSON.parse(JSON.stringify(report.after)),{total:2,passed:2,suspect:0,obvious:0,unchecked:0});
    assert.equal(report.changed,1);
    assert.equal(ctx.$('#reportTitle').textContent,'校准报告');
    assert.equal(ctx.$('#reportAfterChanged').textContent,'1');
    assert.equal(ctx.$('#reportAfterPassed').textContent,'2');
  });

  await test('over_width_text_is_suspect_and_empty_text_is_obvious',async()=>{
    const ctx=make({escapeHtml:String},['badgesHtml']);
    ctx.state.settings.width=6;
    const long=ctx.badgesHtml({text:'一二三四五六七',start:0,end:2,verification_stage:'calibrate',reference_match:{status:'high',query_text:'一二三四五六七'}});
    assert(long.includes('badge low'));assert(long.includes('疑似问题'));
    const empty=ctx.badgesHtml({text:'',start:0,end:1,reference_match:{status:'high',query_text:''}});
    assert(empty.includes('badge noise'));assert(empty.includes('明显问题'));
  });

  await test('ordinary_problems_are_yellow_and_obvious_timing_problems_are_red',async()=>{
    const ctx=make({escapeHtml:String},['cueNeedsReview','badgesHtml']);
    const base={text:'先留着',start:61.35,end:62.11,reference_match:{status:'high',query_text:'先留着'}};
    for(const cue of [{...base,reference_match:{status:'typo',label:'疑似错字'}},
      {...base,reference_match:{status:'unmatched',label:'未匹配'}},
      {...base,reference_match:{status:'ambiguous',label:'多候选匹配'}}]){
      const html=ctx.badgesHtml(cue);assert(html.includes('badge low'));assert(html.includes('疑似问题'));
      assert(!html.includes('参考一致'));assert.equal(ctx.cueNeedsReview(cue),true);
    }
    for(const cue of [{...base,start:50.18,end:62.02,verification_stage:'align'},{...base,alignment_error:'对齐结果异常拉长',verification_stage:'align'}]){
      const html=ctx.badgesHtml(cue);assert(html.includes('badge noise'));assert(html.includes('明显问题'));
    }
  });

  await test('labels_do_not_close_export_and_legacy_auto_exclusions_are_released',async()=>{
    const ctx=make({escapeHtml:String},['releaseVerificationExclusions']);
    const auto=ctx.normalizeCue({text:'自动排除',start:0,end:1,exclude_export:true,verification_excluded:true},0);
    const manual=ctx.normalizeCue({text:'手动排除',start:1,end:2,exclude_export:true,review_decision:'non_script'},1);
    const hallucinated=ctx.normalizeCue({text:'幻觉',start:2,end:3,exclude_export:true,verification_excluded:true,
      hallucination:{confirmed:true}},2);
    const pass=ctx.normalizeCue({text:'通过',start:3,end:4},3);
    const restored=ctx.releaseVerificationExclusions([auto,manual,hallucinated,pass]);
    assert.equal(restored,1);
    assert.equal(auto.exclude_export,false);assert(!('verification_excluded' in auto));
    assert.equal(manual.exclude_export,true);
    assert.equal(hallucinated.exclude_export,true);
    assert(!('verification_excluded' in hallucinated));
  });

  await test('interjection_cue_is_red_and_explains_export_behavior',async()=>{
    const ctx=make({escapeHtml:String},['computeCueVerification','cueNeedsReview','badgesHtml']);
    const cue={text:'哈哈哈',start:0,end:.5,interjection:true};
    let check=ctx.computeCueVerification(cue);
    assert.equal(check.status,'suspect');assert.equal(check.obvious,true);assert.equal(check.label,'语气词');
    assert.match(check.reason,/由你判断后删除或保留/);
    assert(ctx.cueNeedsReview(cue));
    const html=ctx.badgesHtml(cue);assert(html.includes('badge noise'));assert(html.includes('语气词'));
    ctx.state.settings.keepInterjections=true;
    check=ctx.computeCueVerification(cue);
    assert.equal(check.label,'语气词');assert.match(check.reason,/正常导出/);
    assert.match(ctx.computeCueVerification({...cue,review_decision:'included'}).reason,/手动开启导出/);
    assert.match(ctx.computeCueVerification({...cue,review_decision:'non_script'}).reason,/已手动标为不导出/);
    assert.notEqual(ctx.computeCueVerification({text:'你呀，快走',start:0,end:1,interjection:false})?.label,'语气词');
  });

  await test('modified_done_is_green_and_suppresses_old_problem_or_recheck_labels',async()=>{
    const ctx=make({escapeHtml:String},['cueNeedsReview','badgesHtml']);
    const cue={text:'修改台词',start:0,end:1,reference_match:{status:'high',query_text:'原台词'}};
    ctx.completeModification(cue);cue.reference_match.stale=true;
    const html=ctx.badgesHtml(cue);assert(html.includes('文字已确认'));assert(html.includes('modified-done'));
    assert(!html.includes('疑似问题'));assert(!html.includes('待重新校验'));assert(!html.includes('已完成'));assert(!ctx.cueNeedsReview(cue));
    cue.reference_match={status:'high',query_text:cue.text};cue.modification_complete=false;cue.exclude_export=true;cue.verification_stage='verify';
    assert(ctx.badgesHtml(cue).includes('参考一致'));assert(ctx.cueExcluded(cue));
  });

  await test('manual_srt_and_mkv_export_choose_directory_before_writing',async()=>{
    for(const format of ['srt','mkv']){
      const calls=[];
      const ctx=make({collectSettings:()=>({keepPunc:true,wrapSubtitles:true}),displayedSubtitleText:String,
        progressBar:{style:{}},api:async(path,body)=>{calls.push({path,body});return path==='/api/dialog'?{paths:['C:/导出目录']}:{path:'C:/导出目录/字幕.'+format,count:1}}},['exportSrt']);
      const result=await ctx.exportSrt(false,format);
      assert.deepEqual(calls.map(c=>c.path),['/api/dialog','/api/srt/export']);
      assert.equal(calls[0].body.kind,'dir');assert.equal(calls[0].body.purpose,'export');
      assert.equal(calls[1].body.options.output_dir,'C:/导出目录');assert.equal(calls[1].body.format,format);
      assert(result.path.endsWith('.'+format));assert.equal(ctx.state.operation,'');
    }
  });

  await test('cancel_export_directory_never_writes_or_reports_success',async()=>{
    let writes=0;const messages=[];
    const ctx=make({collectSettings:()=>({}),displayedSubtitleText:String,toast:text=>messages.push(text),
      api:async(path)=>{if(path==='/api/dialog')return {path:'',paths:[]};writes++;throw Error('不应导出')}},['exportSrt']);
    const before=JSON.stringify(ctx.state.cues);
    assert.equal(await ctx.exportSrt(false),null);assert.equal(writes,0);assert.equal(messages.length,0);
    assert.equal(ctx.state.operation,'');assert.equal(JSON.stringify(ctx.state.cues),before);
  });

  await test('select_all_uses_whole_document_including_export_exclusions_without_changing_flags',async()=>{
    const ctx=make({},['selectAllCues']);
    ctx.state.cues=[{text:'甲'},{text:'乙',exclude_export:true},{text:'丙'},{text:'丁',review_decision:'non_script'}];
    const before=JSON.stringify(ctx.state.cues);
    ctx.state.search='甲';ctx.state.filter='edited';ctx.selectAllCues(true);
    assert.deepEqual([...ctx.selectedCueIndices()],[0,1,2,3]);assert.equal(ctx.state.history.length,0);
    assert.equal(JSON.stringify(ctx.state.cues),before);
    ctx.selectAllCues(false);assert.equal(ctx.state.selectedCues.size,0);assert.equal(ctx.state.selected,-1);
  });

  await test('bulk_delete_and_export_marking_are_single_step_undoable',async()=>{
    const ctx=make({},['selectAllCues','deleteCue','toggleSelectedExport','undo']);
    ctx.state.cues=[{start:0,end:1,text:'甲'},{start:2,end:3,text:'乙',exclude_export:true},{start:4,end:5,text:'丙'}].map(ctx.normalizeCue);
    ctx.state.selectedCues=new Set([0,2]);
    ctx.toggleSelectedExport();assert(ctx.state.cues.every(c=>ctx.cueExcluded(c)));
    ctx.toggleSelectedExport();assert(!ctx.cueExcluded(ctx.state.cues[0]));assert(ctx.cueExcluded(ctx.state.cues[1]));
    ctx.deleteCue();assert.deepEqual([...ctx.state.cues.map(c=>c.text)],['乙']);ctx.undo();assert.equal(ctx.state.cues.length,3);
    ctx.state.selectedCues=new Set([0,1,2]);ctx.deleteCue();assert.equal(ctx.state.cues.length,0);ctx.undo();assert.equal(ctx.state.cues.length,3);
  });

  await test('one_click_calibration_preserves_originals_and_skips_unreliable_reference',async()=>{
    const ctx=make({api:async(path,body)=>copyReferenceCues(body)},['calibrateAll','undo']);ctx.state.script=[{text:'剧本'}];
    const original={text:'原识别',start:0,end:1};
    ctx.state.cues=[{text:'错字',script_text:'正字',reference_match:{status:'typo',reference:{text:'正字'}},asr_original:original,start:0,end:1},
      {text:'歧义',script_text:'其他',reference_match:{status:'ambiguous',reference:{text:'其他'}}},
      {text:'旧建议',script_text:'错误',reference_match:{stale:true,reference:{text:'错误'}}},
      {text:'没参考'}].map(ctx.normalizeCue);
    ctx.state.selectedCues=new Set([0,1,2,3]);await ctx.calibrateAll();
    assert.deepEqual([...ctx.state.cues.map(c=>c.text)],['正字','歧义','旧建议','没参考']);
    assert.equal(ctx.state.cues[0].asr_original.text,'原识别');
    assert.equal(ctx.state.cues[0].reference_match.stale,false);
    assert.equal(ctx.state.cues[0].verification_stage,'calibrate');
    assert.equal(ctx.state.history.length,1);ctx.undo();assert.equal(ctx.state.cues[0].text,'错字');
  });

  await test('calibration_completes_missing_and_extra_reference_without_changing_structure',async()=>{
    const ctx=make({api:async(path,body)=>copyReferenceCues(body)},['calibrateAll','undo']);ctx.state.script=[{text:'剧本'}];
    ctx.state.cues=[
      {text:'我们明天学校',start:0,end:1,script_text:'我们明天去学校',reference_match:{status:'missing',reference:{text:'我们明天去学校'}}},
      {text:'我们明天去去学校',start:1,end:2,script_text:'我们明天去学校',reference_match:{status:'extra',reference:{text:'我们明天去学校'}}},
    ].map(ctx.normalizeCue);
    ctx.state.selectedCues=new Set([0,1]);await ctx.calibrateAll();
    assert.deepEqual([...ctx.state.cues.map(c=>c.text)],['我们明天去学校','我们明天去学校']);
    assert.deepEqual([...ctx.state.cues.map(c=>[c.start,c.end])],[[0,1],[1,2]]);
    assert.equal(ctx.state.cues[0].asr_original.text,'我们明天学校');
    assert.equal(ctx.state.cues[1].asr_original.text,'我们明天去去学校');
  });

  await test('calibration_skips_stale_reference_and_keeps_neighbors',async()=>{
    let requests=0;
    const ctx=make({api:async(path,body)=>{requests++;return copyReferenceCues(body)}},['calibrateAll']);ctx.state.script=[{text:'剧本'}];
    ctx.state.cues=[0,1].map(i=>ctx.normalizeCue({start:i*2,end:i*2+1,text:'错字'+i,script_text:'正字'+i,
      reference_match:{status:'typo',stale:i===1,reference:{text:'正字'+i}}},i));
    ctx.state.selectedCues=new Set([1]);await ctx.calibrateAll();
    assert.equal(requests,1);
    assert.equal(ctx.state.cues[0].text,'错字0');assert.equal(ctx.state.cues[1].text,'错字1');
    assert.equal(ctx.state.cues[1].verification_stage,undefined);
    assert.equal(ctx.state.history.length,0);
  });

  await test('batch_recognition_supports_eleven_selected_in_current_version',async()=>{
    let calls=0,message='';
    const ctx=make({startAsr:async()=>{calls++;return {status:'done'}},toast:text=>message=text},['rerunSelected']);
    ctx.state.cues=Array.from({length:11},(_,i)=>({start:i*2,end:i*2+1,text:String(i)}));
    ctx.state.selectedCues=new Set(ctx.state.cues.map((_,i)=>i));await ctx.rerunSelected();
    assert.equal(calls,11);assert(!message.includes('最多选择10条'));
  });

  await test('batch_recognition_runs_ten_in_reverse_despite_split_results_and_undoes_once',async()=>{
    const calls=[];
    const ctx=make({},['rerunSelected','undo']);
    ctx.state.cues=Array.from({length:11},(_,i)=>ctx.normalizeCue({start:i*2,end:i*2+1,text:String(i)},i));
    const before=JSON.stringify(ctx.state.cues);ctx.state.selectedCues=new Set(Array.from({length:10},(_,i)=>i));
    ctx.startAsr=async(task,target,options)=>{
      assert.equal(task,'region');assert(options.batch);assert(ctx.editLocked());calls.push(target.index);
      const cue=ctx.state.cues[target.index];assert.equal(cue.text,String(target.index));
      ctx.state.cues.splice(target.index,1,{...cue,text:'重识别甲'},{...cue,text:'重识别乙'});
      return {status:'done'};
    };
    await ctx.rerunSelected();
    assert.deepEqual(calls,[9,8,7,6,5,4,3,2,1,0]);assert.equal(ctx.state.cues.at(-1).text,'10');
    assert.equal(ctx.state.history.length,1);assert.equal(ctx.state.batchRecognition,false);
    ctx.undo();assert.equal(JSON.stringify(ctx.state.cues),before);
  });

  await test('cancel_batch_recognition_keeps_completed_results_and_skips_remaining',async()=>{
    const calls=[],message={textContent:''};
    const ctx=make({jobMsg:message,updateJob:job=>{message.textContent=job.message||job.status;}},['rerunSelected','cancelAsr']);
    ctx.state.cues=[0,1,2].map(i=>({start:i*2,end:i*2+1,text:String(i)}));ctx.state.selectedCues=new Set([0,1,2]);
    ctx.startAsr=async(task,target)=>{calls.push(target.index);ctx.state.cues[target.index].text='已处理';await ctx.cancelAsr();return {status:'done'};};
    await ctx.rerunSelected();assert.deepEqual(calls,[2]);assert.equal(ctx.state.cues[2].text,'已处理');
    assert.equal(ctx.state.cues[0].text,'0');assert.equal(ctx.state.batchRecognition,false);
    assert(message.textContent.includes('已取消'));
  });

  await test('export_all_nonexcluded_ignores_selection_search_and_presentation_padding',async()=>{
    let payload;
    const ctx=make({displayedSubtitleText:String,collectSettings:()=>({keepPunc:true,wrapSubtitles:true}),
      api:async(path,body)=>path==='/api/dialog'?{path:'C:/导出'}:(payload=body,{path:'C:/导出/字幕.srt',count:2})},['exportSrt']);
    ctx.state.cues=[{text:'甲',start:1.123,end:1.456,is_noise:true},{text:'乙',start:2.234,end:3.789},{text:'丙',start:4,end:5,exclude_export:true}];
    ctx.state.selectedCues=new Set([0]);ctx.state.search='甲';ctx.state.filter='low';
    Object.assign(ctx.state.settings,{subtitleLead:1,subtitleTail:1,subtitleMin:5,subtitleMax:.2});
    const before=JSON.stringify(ctx.state.cues);await ctx.exportSrt(false);
    assert.deepEqual([...payload.cues.map(c=>[c.start,c.end,c.text])],[[1.123,1.456,'甲'],[2.234,3.789,'乙']]);
    const first=JSON.stringify(payload.cues);ctx.state.selectedCues=new Set([0,1,2]);await ctx.exportSrt(false);
    assert.equal(JSON.stringify(payload.cues),first);
    assert.equal(JSON.stringify(ctx.state.cues),before);
  });

  await test('local_reference_check_includes_neighbors_but_only_updates_target',async()=>{
    let payload;
    const ctx=make({api:async(path,body)=>{payload=body;return {cues:body.cues.map(c=>({...c,script_text:'返回的参考'}))}}},['rematchDerivedCues']);
    ctx.state.script=[{text:'参考'}];ctx.state.scriptDocument={text:'参考'};
    ctx.state.cues=Array.from({length:20},(_,i)=>ctx.normalizeCue({text:'原文'+i,start:i,end:i+.5},i));
    const neighbors=JSON.stringify(ctx.state.cues.filter((c,i)=>i!==1&&i!==18));
    await ctx.rematchDerivedCues([1,18]);
    assert.deepEqual([...payload.sequence_indices],[0,1,2,3,4,15,16,17,18,19]);
    assert.equal(ctx.state.cues[1].script_text,'返回的参考');assert.equal(ctx.state.cues[18].script_text,'返回的参考');
    assert.equal(JSON.stringify(ctx.state.cues.filter((c,i)=>i!==1&&i!==18)),neighbors);
  });

  await test('out_of_context_global_hit_is_red_and_not_adoptable',async()=>{
    const ctx=make({escapeHtml:String},['cueNeedsReview','badgesHtml','suggestHtml']);
    const cue={text:'其他地方存在的话',start:1,end:2,script_text:'',reference_match:{status:'context_mismatch',label:'疑似多余台词',warning:'当前片段无此台词，需试听',context:{status:'outside'},reference:null}};
    assert(ctx.badgesHtml(cue).includes('badge noise'));assert(ctx.badgesHtml(cue).includes('明显问题'));
    assert(ctx.suggestHtml(cue,0).includes('当前片段无此台词'));assert(ctx.cueNeedsReview(cue));assert(!ctx.canAdoptCue(cue));
  });

  await test('editable_subtitle_punctuation_toggle_is_reversible_and_keeps_raw_text',async()=>{
    const chinese={window:{},OpenCC:{Converter:()=>value=>value}};
    vm.runInNewContext(readFileSync(new URL('../frontend/js/chinese.js',import.meta.url),'utf8'),chinese);
    const ctx=make({window:chinese.window},['subtitleText','editableCueText']);
    const raw='你好，世界！12:30，3.5%';ctx.state.settings.keepPunc=false;
    const hidden=ctx.editableCueText(raw);assert(!/[，！]/.test(hidden));assert(hidden.includes('12:30'));assert(hidden.includes('3.5%'));
    ctx.state.settings.keepPunc=true;assert.equal(ctx.editableCueText(raw),raw);
    ctx.state.settings.keepPunc=false;assert.equal(ctx.editableCueText(raw),hidden);
    assert.equal(raw,'你好，世界！12:30，3.5%');
  });

  await test('editing_without_punctuation_preserves_hidden_unchanged_punctuation',async()=>{
    const chinese={window:{},OpenCC:{Converter:()=>value=>value}};
    vm.runInNewContext(readFileSync(new URL('../frontend/js/chinese.js',import.meta.url),'utf8'),chinese);
    const ctx=make({window:chinese.window},['subtitleText','editableCueText','mergeEditedCueText']);ctx.state.settings.keepPunc=false;
    assert.equal(ctx.mergeEditedCueText('你好，世界。','你好世界'),'你好，世界。');
    assert.equal(ctx.mergeEditedCueText('你好，世界。','您好世界'),'您好，世界。');
    assert.equal(ctx.mergeEditedCueText('你好，世界。','你好世界啊'),'你好，世界啊。');
    assert.equal(ctx.mergeEditedCueText('你好，世界。',''),'');
    ctx.state.settings.keepPunc=true;assert.equal(ctx.mergeEditedCueText('你好，世界。','你好世界'),'你好世界');
  });

  await test('split_badge_only_marks_actual_long_text_width_splits',async()=>{
    const ctx=make({escapeHtml:String},['cueNeedsReview','badgesHtml']);
    assert(!ctx.badgesHtml({source:'gap_split'}).includes('已分段'));
    assert(!ctx.badgesHtml({source:'split',segmentation:'manual_midpoint',width_split:false}).includes('已分段'));
    assert(ctx.badgesHtml({source:'split',width_split:true}).includes('已分段'));
    assert(ctx.badgesHtml({source:'split',segmentation:'dynamic_programming_word_pause'}).includes('已分段'));
    assert(!ctx.badgesHtml({source:'merged',width_split:true}).includes('已分段'));
  });

  await test('calibration_leaves_matching_reference_text_and_time_unchanged',async()=>{
    let requests=0;
    const ctx=make({api:async(path,body)=>{requests++;return copyReferenceCues(body)}},['calibrateAll']);
    ctx.state.script=[{text:'正确台词'}];
    ctx.state.cues=[ctx.normalizeCue({text:'正确台词',start:0,end:1,
      reference_match:{status:'high',query_text:'正确台词',reference:{text:'正确台词'}}},0)];
    const before=JSON.parse(JSON.stringify(ctx.state.cues));await ctx.calibrateAll();
    assert.equal(requests,1);
    assert.equal(ctx.state.cues[0].text,before[0].text);
    assert.equal(ctx.state.cues[0].start,before[0].start);assert.equal(ctx.state.cues[0].end,before[0].end);
    assert.equal(ctx.state.cues[0].verification_stage,'calibrate');
    assert(ctx.hasCompletedModification(ctx.state.cues[0]));
    assert.equal(ctx.state.history.length,1);
  });

  await test('calibration_copies_reference_and_updates_report',async()=>{
    const messages=[];
    const ctx=make({api:async(path,body)=>copyReferenceCues(body)},['calibrateAll','badgesHtml']);
    ctx.state.script=[{text:'正字'}];
    ctx.state.cues=[ctx.normalizeCue({text:'错字',start:0,end:1,script_text:'正字',
      reference_match:{status:'typo',label:'疑似错字',query_text:'错字',reference:{text:'正字'}}},0)];
    ctx.state.selectedCues=new Set([0]);await ctx.calibrateAll();
    assert.equal(ctx.state.cues[0].text,'正字');
    assert.equal(ctx.$('#reportAfterChanged').textContent,'1');
    assert.equal(ctx.$('#reportAfterSuspect').textContent,'0');
    assert.equal(ctx.$('#reportAfterPassed').textContent,'1');
    const html=ctx.badgesHtml(ctx.state.cues[0]);
    assert(html.includes('文字已确认'));assert(!html.includes('疑似问题'));
  });

  await test('calibration_without_reference_keeps_text_pending',async()=>{
    const ctx=make({api:async(path,body)=>copyReferenceCues(body),escapeHtml:String},['calibrateAll']);
    ctx.state.script=[{text:'剧本'}];
    ctx.state.cues=[ctx.normalizeCue({text:'原字幕',start:0,end:1},0)];
    const before=JSON.stringify(ctx.state.cues);await ctx.calibrateAll();
    assert.equal(JSON.stringify(ctx.state.cues),before);
    assert.equal(ctx.$('#reportAfterSuspect').textContent,'0');
    assert.equal(ctx.state.history.length,0);
  });

  await test('one_click_calibration_accepts_more_than_ten_selected_cues_and_undoes_once',async()=>{
    const ctx=make({api:async(path,body)=>copyReferenceCues(body)},['calibrateAll','undo']);ctx.state.script=[{text:'剧本'}];
    ctx.state.cues=Array.from({length:50},(_,i)=>ctx.normalizeCue({text:'错字'+i,start:i*2,end:i*2+1,script_text:'正字'+i,
      reference_match:{status:'typo',query_text:'错字'+i,reference:{text:'正字'+i}}},i));
    const before=JSON.stringify(ctx.state.cues);ctx.state.selectedCues=new Set(ctx.state.cues.map((c,i)=>i));
    await ctx.calibrateAll();assert(ctx.state.cues.every(c=>ctx.hasCompletedModification(c)));
    assert.equal(ctx.state.cues[49].text,'正字49');assert.equal(ctx.state.cues[0].reference_match.status,'high');
    assert.equal(ctx.state.history.length,1);ctx.undo();assert.equal(JSON.stringify(ctx.state.cues),before);
  });

  await test('large_context_refresh_chunks_requests_and_commits_after_complete_results',async()=>{
    const sizes=[];const ctx=make({api:async(path,body)=>{sizes.push(body.cues.length);return {cues:body.cues.map(c=>({...c,script_text:'新参考'}))}}},['rematchDerivedCues']);
    ctx.state.script=[{text:'剧本'}];ctx.state.cues=Array.from({length:6001},(_,i)=>({text:String(i),start:i*2,end:i*2+1}));
    assert(await ctx.rematchDerivedCues(ctx.state.cues.map((c,i)=>i)));
    assert.equal(sizes.length,2);assert(sizes.every(n=>n<=5000));assert(ctx.state.cues.every(c=>c.script_text==='新参考'));
  });

  await test('failed_calibration_request_preserves_text_and_reports_error',async()=>{
    const messages=[];
    const ctx=make({api:async()=>{throw new Error('后台不可用')},toast:text=>messages.push(text)},['calibrateAll']);
    ctx.state.script=[{text:'剧本'}];
    ctx.state.cues=[{text:'原字幕',start:0,end:1,script_text:'旧参考',reference_match:{status:'typo',stale:true,reference:{text:'旧参考'}}}];
    const before=JSON.stringify(ctx.state.cues);await ctx.calibrateAll();
    assert.equal(JSON.stringify(ctx.state.cues),before);assert.equal(ctx.state.operation,'');
    assert(messages.some(text=>text.includes('后台不可用')));
  });

  await test('modification_certificate_survives_alignment_time_changes_but_not_text_changes',async()=>{
    const ctx=make({escapeHtml:String},['badgesHtml']);
    const cue={text:'先留着',start:61.35,end:62.03,reference_match:{status:'high',query_text:'先留着'}};
    ctx.completeModification(cue);assert.equal(ctx.cueVerification(cue).status,'modified');
    cue.start=50.18;cue.end=52;assert.equal(ctx.cueVerification(cue).status,'modified');
    cue.text='新台词';assert.equal(ctx.hasCompletedModification(cue),false);
  });

  await test('emotion_punctuation_is_consistent_in_editor_reference_preview_and_toggle',async()=>{
    const chinese={window:{},OpenCC:{Converter:()=>value=>value}};
    vm.runInNewContext(readFileSync(new URL('../frontend/js/chinese.js',import.meta.url),'utf8'),chinese);
    const ctx=make({window:chinese.window},['subtitleText','editableCueText']);
    const raw='“你好，朋友。”你好吗？快走！ 3.5% 12:30';
    ctx.state.settings.keepPunc=true;
    assert.equal(ctx.subtitleText(raw),raw);
    assert.equal(ctx.editableCueText(raw),ctx.subtitleText(raw));
    ctx.state.settings.keepPunc=false;
    const plain=ctx.subtitleText(raw);
    assert(!/[，。？！：“”]/.test(plain));
    assert(plain.includes('3.5% 12:30'));
    ctx.state.settings.keepPunc=true;assert(ctx.subtitleText(raw).includes('你好吗？快走！'));
  });

  await test('calibration_only_submits_selected_cues_and_keeps_unselected_neighbors',async()=>{
    const bodies=[];
    const ctx=make({api:async(path,body)=>{bodies.push(body);return copyReferenceCues(body)}},['calibrateAll']);
    ctx.state.script=[{text:'参考'}];
    ctx.state.cues=['前句','错','毒的人','后句'].map((text,i)=>ctx.normalizeCue({text,start:i,end:i+.8,script_text:'正'+text,
      reference_match:{status:'typo',query_text:text,reference:{text:'正'+text}}},i));
    const beforeFirst=JSON.stringify(ctx.state.cues[0]),beforeLast=JSON.stringify(ctx.state.cues[3]);
    ctx.state.selectedCues=new Set([1,2]);await ctx.calibrateAll();
    assert.equal(bodies.length,1);
    assert.deepEqual([...bodies[0].cues.map(c=>c.text)],['错','毒的人']);
    assert.equal(bodies[0].keep_punc,true);assert.equal(bodies[0].max_width,20);
    assert.equal(bodies[0].keep_interjections,false);
    assert.equal(ctx.state.cues.length,4);
    assert.equal(JSON.stringify(ctx.state.cues[0]),beforeFirst);
    assert.equal(JSON.stringify(ctx.state.cues[3]),beforeLast);
    assert.deepEqual([...ctx.state.cues.map(c=>[c.start,c.end])],[[0,.8],[1,1.8],[2,2.8],[3,3.8]]);
    assert.deepEqual([...ctx.state.selectedCues],[1,2]);
  });

  await test('calibration_rejects_old_backend_and_structural_changes',async()=>{
    const ctx=make({api:async(path,body)=>({mode:'calibration',cues:body.cues,changed_indices:[],
      merged:1,merge_suggestions:1,splits:0})},['calibrateAll']);
    ctx.state.script=[{text:'参考'}];
    ctx.state.cues=[ctx.normalizeCue({text:'原',start:0,end:1,script_text:'新',
      reference_match:{status:'typo',query_text:'原',reference:{text:'新'}}},0)];
    const before=JSON.stringify(ctx.state.cues);
    await ctx.calibrateAll();
    assert.equal(JSON.stringify(ctx.state.cues),before);
    ctx.api=async(path,body)=>({mode:'reference_copy',cues:body.cues.map(c=>({...c,text:'新',end:2})),
      completed_indices:[0],changed_indices:[0],pending_indices:[]});
    await ctx.calibrateAll();
    assert.equal(JSON.stringify(ctx.state.cues),before);
    assert.equal(ctx.state.history.length,0);
  });

  await test('calibration_is_content_only_and_never_runs_audio_alignment',async()=>{
    let audio=0;
    const ctx=make({startAsr:async()=>{audio++;return {status:'done'}},
      api:async(path,body)=>copyReferenceCues(body)},['calibrateAll']);
    ctx.state.script=[{text:'正确台词'}];
    ctx.state.cues=[ctx.normalizeCue({text:'正确台词',start:0,end:1,
      reference_match:{status:'high',query_text:'正确台词',reference:{text:'正确台词'}}},0)];
    await ctx.calibrateAll();
    assert.equal(audio,0);
    assert.equal(ctx.state.cues[0].verification_stage,'calibrate');
    assert.equal(ctx.state.cues[0].start,0);assert.equal(ctx.state.cues[0].end,1);
  });

  await test('pass_label_follows_last_check_and_problem_labels_are_shared',async()=>{
    const ctx=make({escapeHtml:String},['computeCueVerification']);
    const base={text:'正确台词',start:0,end:1,reference_match:{status:'high',query_text:'正确台词'}};
    assert.equal(ctx.computeCueVerification(base),null);
    for(const [stage,label] of [['verify','参考一致'],['calibrate','已校准'],['align','自动对齐完成']]){
      assert.equal(ctx.computeCueVerification({...base,verification_stage:stage}).label,label);
    }
    const typo={text:'错字',start:0,end:1,verification_stage:'align',
      reference_match:{status:'typo',query_text:'错字',reference:{text:'正字'}}};
    assert.equal(ctx.computeCueVerification(typo).label,'疑似问题');
    const failed={...base,verification_stage:'align',alignment_error:'未得到可靠对齐时间'};
    assert.equal(ctx.computeCueVerification(failed).label,'明显问题');
    const merge={direction:'next'};
    assert.equal(ctx.cueReportVerdict({...base,verification_stage:'verify',merge_suggestion:merge}),'passed');
    assert.equal(ctx.cueReportVerdict({...base,verification_stage:'calibrate',merge_suggestion:merge}),'obvious');
  });
  await test('punctuation_toggle_preserves_raw_ellipsis_and_decimal',async()=>{
    const chinese={window:{},OpenCC:{Converter:()=>value=>value}};
    vm.runInNewContext(readFileSync(new URL('../frontend/js/chinese.js',import.meta.url),'utf8'),chinese);
    assert.equal(chinese.window.SubtitleChinese.normalizePunctuation('我...不知道，3.5呢？'),'我...不知道，3.5呢？');
    assert.equal(chinese.window.SubtitleChinese.stripPunctuation('我……不知道，3.5呢？'),'我不知道3.5呢');
  });
  await test('persistent_disconnect_unlocks_editing_and_resumes_same_task_without_resubmit', async () => {
    let submissions=0, offline=true;
    const ctx=make({api:async(path)=>{
      if(path==='/api/jobs'){submissions++;return {job:{status:'queued'}};}
      if(offline)throw new Error('offline');
      return {job:{status:'done',cues:[{start:0,end:1,text:'模型结果'}]}};
    }},['startAsr']);
    await ctx.startAsr();
    assert.equal(ctx.state.jobId,null);assert(!ctx.editLocked());
    const pending=ctx.state.detachedJob;assert(pending?.id);
    assert.equal(await ctx.startAsr(),null);assert.equal(submissions,1);
    ctx.state.cues[0].text='恢复编辑后手动修改';offline=false;
    await ctx.startAsr(pending.task,pending.region,{...pending.options,resume:pending});
    assert.equal(submissions,1);assert.equal(ctx.state.cues[0].text,'恢复编辑后手动修改');
    assert.equal(ctx.state.pendingResult.job.cues[0].text,'模型结果');
  });
  await test('failed_cancel_detaches_without_claiming_backend_was_cancelled', async () => {
    const ctx=make({api:async()=>{throw new Error('offline')}},['cancelAsr']);
    ctx.state.jobId='123';ctx.state.activeJob={id:'123',path:'A.wav',epoch:0};
    await ctx.cancelAsr();
    assert.equal(ctx.state.jobId,null);assert.equal(ctx.state.detachedJob.id,'123');
    assert(!ctx.editLocked());
  });
  await test('conflict_stops_repeated_save_requests_and_keeps_local_edits',async()=>{
    let calls=0;
    const ctx=make({api:async()=>{calls++;throw Object.assign(new Error('conflict'),{status:409})}},['saveProject']);
    assert.equal(await ctx.saveProject(),false);assert.equal(await ctx.saveProject(),false);
    assert.equal(calls,1);assert(ctx.state.dirty);assert(ctx.projectConflicts.has('A.wav'));
    assert.equal(ctx.state.cues[0].text,'原字幕');
  });
  await test('conflict_resolution_backs_up_local_before_loading_remote_revision',async()=>{
    const calls=[];
    const ctx=make({restoredScriptNames:()=>[],applyProjectSettings:noop,api:async(path,body)=>{
      calls.push(path);
      if(path==='/api/projects/backup'){assert.equal(body.cues[0].text,'原字幕');return {path:'backup.json'};}
      return {revision:9,project:{cues:[{start:0,end:1,text:'其他页面'}],script_lines:[],settings:{}}};
    }},['resolveProjectConflict']);
    ctx.projectConflicts.add('A.wav');await ctx.resolveProjectConflict();
    assert.equal(calls[0],'/api/projects/backup');assert.equal(ctx.state.cues[0].text,'其他页面');
    assert.equal(ctx.projectVersions.get('A.wav'),9);assert(!ctx.projectConflicts.has('A.wav'));
    assert(!ctx.state.dirty);
  });
  await test('content_confirmation_never_masks_timing_errors_or_removes_review_filter',async()=>{
    const ctx=make({},['badgesHtml','filteredRows','cueStatistics']);
    const cue={text:'确认文字',start:0,end:1,edited:true,verification_stage:'verify',alignment:{engine:'fixture'},alignment_error:'无法定位'};
    ctx.state.cues=[cue];ctx.state.filter='low';ctx.state.search='';
    assert.equal(ctx.hasCompletedModification(cue),true);
    assert.equal(ctx.computeCueVerification(cue).label,'明显问题');
    const html=ctx.badgesHtml(cue);assert(html.includes('文字已确认'));assert(html.includes('无法定位'));
    assert.equal(ctx.cueReportVerdict(cue),'obvious');assert.equal(ctx.filteredRows().length,1);
    assert.equal(ctx.cueStatistics([cue]).aligned,0);assert.equal(ctx.cueStatistics([cue]).suspect,1);
  });
  await test('unresolved_long_timing_does_not_count_as_aligned_even_with_model_record',async()=>{
    const ctx=make({},['cueStatistics']);
    const long={text:'四个汉字',start:730.08,end:812.96,verification_stage:'align',alignment:{engine:'fixture'},duration_trim:{method:'unresolved'}};
    assert.equal(ctx.computeCueVerification(long).status,'suspect');
    assert.equal(ctx.cueStatistics([long]).aligned,0);
    assert.equal(ctx.cueReportVerdict({...long,edited:true}),'obvious');
  });
  await test('raw_preview_matches_export_unless_timing_preview_explicitly_enabled',async()=>{
    const ctx=make();ctx.state.cues=[{text:'四个汉字',start:1,end:80}];ctx.state.settings.subtitleMax=3;
    assert.equal(ctx.presentationCues()[0].end,80);
    ctx.state.timingPreview=true;assert.equal(ctx.presentationCues()[0].end,4);
    assert.equal(ctx.state.cues[0].end,80);
    ctx.state.timingPreview=false;assert.equal(ctx.presentationCues()[0].end,80);
  });
  await test('timing_apply_rejects_cut_words_or_missing_word_evidence_and_preserves_history',async()=>{
    const ctx=make({},['timingAdjustmentPlan','applyTimingSettings']);
    ctx.state.settings.subtitleMax=3;
    for(const words of [[],[{word:'对白',start:1,end:8}]]){
      ctx.state.cues=[{text:'对白',start:1,end:10,words}];ctx.state.presentationCache=null;
      const before=ctx.snapshot();assert.equal(ctx.applyTimingSettings(),false);assert.equal(ctx.snapshot(),before);assert.equal(ctx.state.history.length,0);
    }
  });
  await test('safe_timing_apply_is_undoable_and_keeps_original_and_export_flags',async()=>{
    const ctx=make({},['timingAdjustmentPlan','applyTimingSettings','undo']);
    ctx.state.settings.subtitleMax=3;ctx.state.media.duration=10;ctx.state.timingPreview=true;
    ctx.state.cues=[ctx.normalizeCue({text:'对白',start:1,end:6,words:[{word:'对白',start:1,end:2}]},0),ctx.normalizeCue({text:'隐藏',start:7,end:9,exclude_export:true},1)];
    assert.equal(ctx.applyTimingSettings(),true);assert.equal(ctx.state.cues[0].end,4);
    assert.equal(ctx.presentationCues()[0].end,4);assert.equal(ctx.state.timingPreview,false);
    assert.equal(ctx.state.cues[0].asr_original.end,6);assert.equal(ctx.state.cues[1].exclude_export,true);
    assert.equal(ctx.state.history.length,1);ctx.undo();assert.equal(ctx.state.cues[0].end,6);
  });
  await test('timing_apply_refuses_existing_overlaps_instead_of_partial_changes',async()=>{
    const ctx=make({},['timingAdjustmentPlan','applyTimingSettings']);ctx.state.settings.subtitleTail=.3;
    ctx.state.cues=[{text:'甲',start:0,end:2},{text:'乙',start:1,end:3}];
    const before=ctx.snapshot();assert.equal(ctx.applyTimingSettings(),false);assert.equal(ctx.snapshot(),before);
  });
  await test('successful_export_keeps_visible_result_path_for_location_and_copy',async()=>{
    const ctx=make({displayedSubtitleText:String,api:async path=>path==='/api/dialog'?{path:'D:/exports'}:{path:'D:/exports/film_2.srt',count:1}},['exportSrt']);
    await ctx.exportSrt(false);assert.equal(ctx.state.lastExport.path,'D:/exports/film_2.srt');
    assert.equal(ctx.$('#exportResult').hidden,false);assert.equal(ctx.$('#exportResultPath').textContent,'D:/exports/film_2.srt');
  });
  return {total: results.length, all_passed: results.every(r => r.passed), results};
}
