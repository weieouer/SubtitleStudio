const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const SETTINGS_KEY = "subtitleStudio.settings.v2";
const THEME_KEY = "subtitleStudio.theme";
const MODE_LABEL = { extract: "字幕提取", align: "字幕对齐" };
const THEMES = ["system", "light", "dark"];
const SESSION_TOKEN = document.querySelector('meta[name="subtitle-session"]')?.content || "";
const APP_VERSION = '2.3.5';
const projectVersions = new Map();
const projectConflicts = new Set();
let projectSaveChain = Promise.resolve();
let configSaveChain = Promise.resolve();
const HISTORY_LIMIT = 60;
const SAVE_DELAY = 1200;
const PARAM_LIMITS = {
  gap: { min: 0, max: 2, step: 0.1 },
  width: { min: 6, max: 30, step: 1 },
};
function windowIsMaximized() {
  return window.outerWidth >= window.screen.availWidth - 16
    && window.outerHeight >= window.screen.availHeight - 16;
}
function preferredPanelWidth(viewport, maximized) {
  const available = Math.min(520, (viewport - 472) / 2);
  return Math.round(Math.max(280, Math.min(available,
    maximized ? 520 : Math.max(336, (viewport - 810) / 2))));
}
function defaultPanelWidth() {
  return preferredPanelWidth(window.innerWidth, windowIsMaximized());
}
const DEFAULT_LAYOUT = { left: defaultPanelWidth(), right: defaultPanelWidth() };
const MODEL_ORDER = ["large-v3", "fireredasr2-aed", "qwen3-asr-1.7b"];
const MEDIA_EXT_RE = /\.(mp4|mkv|mov|avi|flv|ts|m4v|webm|wmv|mp3|wav|m4a|aac|flac|ogg|opus|wma)$/i;
const SCRIPT_EXT_RE = /\.(txt|md|docx)$/i;

const state = {
  media: null,
  srtPath: "",
  subtitleFile: null,
  mediaEpoch: 0,
  uploadEpoch: 0,
  loading: false,
  operation: "",
  submitting: false,
  batchRecognition: false,
  batchCancelRequested: false,
  dirty: false,
  editRevision: 0,
  pendingSaves: 0,
  pendingResult: null,
  retryPoll: null,
  healthTimer: null,
  models: [],
  cues: [],
  initialCues: null,
  selectedCues: new Set(),
  script: [],
  modelDownload: null,
  auxModels: [],
  downloadTimer: null,
  downloadActive: false,
  scriptDocument: null,
  referenceGaps: [],
  scriptName: "",
  scriptNames: [],
  scriptPath: "",
  jobId: null,
  detachedJob: null,
  activeJob: null,
  versionMismatch: false,
  matchRequest: null,
  selected: -1,
  filter: "all",
  search: "",
  history: [],
  redoStack: [],
  editBefore: null,
  saveTimer: null,
  configTimer: null,
  waveform: null,
  videoErrorShown: false,
  asrEpoch: 0,
  uploading: false,
  waveSpan: 0,
  waveStart: 0,
  playingIndex: -1,
  playbackFrame: null,
  listScrollPauseUntil: 0,
  detailWave: null,
  detailWaves: [],
  detailWaveEpoch: 0,
  detailWaveTimer: null,
  waveDefer: false,
  wavePainted: false,
  auditionEnd: null,
  auditionTimer: null,
  uploadPercent: 0,
  uploadName: "",
  uploadXhr: null,
  scriptUploading: false,
  scriptUploadName: "",
  scriptXhr: null,
  scriptEpoch: 0,
  scriptPickerOpen: false,
  mediaPickerOpen: false,
  healthVersion: "",
  theme: "system",
  gpuStatus: null,
  layout: { ...DEFAULT_LAYOUT },
  windowMaximized: windowIsMaximized(),
  timingPreview: false,
  lastExport: null,
  settings: {
    mode: "extract", model: "large-v3", device: "auto", vad: true, keepInterjections: false, gap: 0.6, width: 20,
    keepPunc: true, wrapSubtitles: true, subtitleLead: 0, subtitleTail: 0, subtitleMin: 0, subtitleMax: 0,
    sound: true, volume: 80, arabicNumbers: false,
    matchMinimum: .64, matchHigh: .94, matchPhonetic: .90, matchAmbiguity: .035, matchGroup: 3,
    auxModel: 'qwen3-forcedaligner-0.6b', waveStyle: 'wave',
  },
};

const video = $("#video");
const overlay = $("#overlay");
const waveCanvas = $("#wave");
const cueBody = $("#cueBody");
const emptyHint = $("#emptyHint");
const progressBar = $("#progressBar");
const jobMsg = $("#jobMsg");
const toastEl = $("#toast");
const tipEl = $("#tip");
const themeRoot = document.documentElement;
const appearance = $("#appearance");
const themeButtons = $$(".appearance-btn");
const lampCord = $("#lampCord");
const lampGrip = lampCord.querySelector(".lamp-cord-grip");
const lampLine = lampCord.querySelector(".lamp-cord-line");
const CORD_LINE_BASE = 29;

// ---------- 基础 ----------
async function api(path, body = null, method = null, options = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (options.signal?.aborted) abort();
  options.signal?.addEventListener('abort', abort, {once:true});
  const timeout = setTimeout(() => controller.abort(), options.timeout ?? (path === "/api/dialog" ? 610000 : 30000));
  const opt = { method: method || (body !== null ? "POST" : "GET"),
    ...options, headers: { "X-SubtitleStudio-Token": SESSION_TOKEN, ...options.headers }, signal: controller.signal };
  if (body !== null) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  try {
  const res = await fetch(path, opt);
  let data;
  try {
    data = await res.json();
  } catch {
    throw new Error(`服务返回异常（HTTP ${res.status}）`);
  }
  if (!res.ok || data.ok === false) {
    const err = new Error(data.error || `请求失败（HTTP ${res.status}）`);
    err.data = data;
    err.errors = data.errors || [];
    err.status = res.status;
    throw err;
  }
  return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error(options.signal?.aborted ? '操作已取消' : "请求超时，请检查连接后重试");
    throw error;
  } finally { clearTimeout(timeout); options.signal?.removeEventListener('abort', abort); }
}

async function runReferenceMatch(body) {
  if (state.versionMismatch) throw new Error('后台版本已过期，请保存工程并重启程序');
  if (state.matchRequest) throw new Error('剧本正在校验，请完成或取消后重试');
  const request = {id:crypto.randomUUID().replaceAll('-',''), controller:new AbortController()};
  state.matchRequest = request; updateButtons();
  let timer, finished = false;
  const poll = async () => {
    try {
      const {task} = await api('/api/script/match/status?id='+request.id, null, null, {timeout:3000});
      if (!finished && state.matchRequest === request) {
        progressBar.style.width = Math.round(task.progress*100)+'%';
        jobMsg.textContent = task.message;
      }
    } catch { /* 匹配响应仍在等待，状态查询失败不触发重复提交。 */ }
    if (!finished) timer = setTimeout(poll, 700);
  };
  timer = setTimeout(poll, 500);
  try {
    return await api('/api/script/match', {...body,request_id:request.id}, null,
      {timeout:170000,signal:request.controller.signal});
  } catch (error) {
    try { await api('/api/script/match/cancel', {request_id:request.id}, null, {timeout:3000}); } catch {}
    throw error;
  } finally {
    finished = true; clearTimeout(timer);
    if (state.matchRequest === request) state.matchRequest = null;
    updateButtons();
  }
}

async function cancelReferenceMatch() {
  const request = state.matchRequest;
  if (!request) return;
  try {
    const result = await api('/api/script/match/cancel', {request_id:request.id}, null, {timeout:3000});
    if (result.cancelled) request.controller.abort();
    else toast('匹配请求尚在提交，请稍后再次取消', 'info');
  } catch { request.controller.abort(); }
}

function projectPath() { return state.media?.path || state.srtPath || ""; }
function simplifiedText(text) { return window.SubtitleChinese?.toSimplified(text) ?? String(text ?? ''); }
function subtitleText(text) {
  const simplified = simplifiedText(text);
  // 显示及导出共用去标点规则，底层文字和原ASR记录保留。
  const plain = state.settings.keepPunc === false ? (window.SubtitleChinese?.stripPunctuation(simplified) ?? simplified)
    : (window.SubtitleChinese?.normalizePunctuation(simplified) ?? simplified);
  return window.SubtitleNumbers?.format(plain, !!state.settings.arabicNumbers) ?? plain;
}
function displayedSubtitleText(text) {
  const plain = subtitleText(text);
  return state.settings.wrapSubtitles === false ? plain.replace(/\s*\r?\n\s*/g, ' ')
    : wrapSubtitleText(plain, state.settings.width);
}
function editableCueText(text) {
  const raw=simplifiedText(text);
  return state.settings.keepPunc===false ? (window.SubtitleChinese?.stripPunctuation(raw) ?? raw)
    : (window.SubtitleChinese?.normalizePunctuation(raw) ?? raw);
}
function mergeEditedCueText(original,edited) {
  const input=simplifiedText(edited);
  if(state.settings.keepPunc!==false)return input;
  const raw=[...simplifiedText(original)],old=[...editableCueText(original)],next=[...editableCueText(input)];
  if(!next.length)return '';
  if(old.join('')===next.join(''))return original;
  let prefix=0,suffix=0;
  while(prefix<old.length&&prefix<next.length&&old[prefix]===next[prefix])prefix++;
  while(suffix<old.length-prefix&&suffix<next.length-prefix&&old[old.length-1-suffix]===next[next.length-1-suffix])suffix++;
  const positions=[];let at=0;
  for(const char of old){at=raw.indexOf(char,at);if(at<0)return next.join('');positions.push(at++);}
  const lo=positions[prefix] ?? (positions.length?positions.at(-1)+1:0);
  const hi=old.length-suffix>prefix?positions[old.length-suffix-1]+1:lo;
  // 只替换用户改动的可见文字，未改动前后部分的隐藏标点仍保留。
  return raw.slice(0,lo).join('')+next.slice(prefix,next.length-suffix).join('')+raw.slice(hi).join('');
}
function editLocked() { return !!(state.jobId || state.matchRequest || state.submitting || state.batchRecognition || state.operation || state.loading || state.uploading || state.scriptUploading || state.scriptPickerOpen || downloadBusy()); }
function sameDocument(path, epoch, before) {
  return projectPath() === path && state.mediaEpoch === epoch && snapshot() === before;
}
function replaceCues(cues, { reset = false, before = snapshot(), subtitleFile = state.subtitleFile } = {}) {
  state.editBefore = null;
  state.timingPreview = false;
  state.cues = cues.map(normalizeCue);
  state.subtitleFile = subtitleFile;
  state.selected = -1;
  state.selectedCues = new Set();
  if (reset) { state.history = []; state.redoStack = []; }
  else pushHistory(before);
  renderAll();
  updateOverlay();
  scheduleSave();
}

// 所有提示音均由浏览器本地合成；首次用户操作后解锁，不加载外部音频。
let audioContext = null;
let lastTick = 0;
function playSound(kind = "key") {
  if (!state.settings.sound || Number(state.settings.volume ?? 80) <= 0) return;
  const Audio = window.AudioContext || window.webkitAudioContext;
  if (!Audio) return;
  try {
    audioContext ||= new Audio();
    if (audioContext.state === "suspended") audioContext.resume().catch(() => {});
    const now = audioContext.currentTime;
    if (kind === "tick" && now - lastTick < 0.035) return;
    if (kind === "tick") lastTick = now;
    const notes = ({
      success: [[660, 0, .12], [880, .12, .18], [1100, .27, .24]],
      open: [[440, 0, .045], [660, .025, .06]],
      close: [[440, 0, .04]],
      select: [[960, 0, .035], [640, .025, .05]],
      on: [[740, 0, .035], [990, .025, .055]],
      off: [[520, 0, .04], [390, .025, .055]],
      tick: [[1150, 0, .025]],
      key: [[520, 0, .055]],
    })[kind] || [[520, 0, .055]];
    for (const [frequency, delay, duration] of notes) {
      const oscillator = audioContext.createOscillator();
      const gain = audioContext.createGain();
      oscillator.type = kind === "key" ? "triangle" : "sine";
      oscillator.frequency.setValueAtTime(frequency, now + delay);
      gain.gain.setValueAtTime(0.0001, now + delay);
      const volume = clamp(Number(state.settings.volume ?? 80), 0, 100) / 100;
      const peak = (kind === "success" ? .28 : kind === "key" ? .16 : .10) * volume;
      gain.gain.exponentialRampToValueAtTime(Math.max(0.0001, peak), now + delay + 0.004);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + delay + duration);
      oscillator.connect(gain); gain.connect(audioContext.destination);
      oscillator.start(now + delay); oscillator.stop(now + delay + duration + 0.01);
    }
  } catch { /* 音频设备不可用时不影响操作 */ }
}

function bindFeedback() {
  document.addEventListener("change", (event) => {
    if (event.target.matches('.check input[type="checkbox"]:not(:disabled)')) {
      playSound(event.target.checked ? "on" : "off");
    } else if (event.target.matches('select:not(:disabled):not([hidden])')) playSound("select");
  });
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button || button.disabled) return;
    const kind = button.matches('.model-btn')
      ? (button.getAttribute('aria-expanded') === 'true' ? 'open' : 'close')
      : button.matches('.model-option') ? 'select' : 'key';
    playSound(kind);
  });
  document.addEventListener("keydown", (event) => {
    const button = event.target.closest("button:not(:disabled)");
    if (!event.repeat && ["Enter", " "].includes(event.key) && button) {
      button.classList.add("key-pressed");
      setTimeout(() => button.classList.remove("key-pressed"), 140);
    }
  });
  $("#soundToggle").addEventListener("click", () => {
    state.settings.sound = !state.settings.sound;
    if (state.settings.sound && !state.settings.volume) state.settings.volume = 80;
    syncSettingsUI(); collectSettings(); scheduleConfigSave(); scheduleSave();
  });
  $("#soundVolume").addEventListener("input", (event) => {
    state.settings.volume = Number(event.target.value);
    state.settings.sound = state.settings.volume > 0;
    syncSettingsUI(); collectSettings(); scheduleConfigSave(); scheduleSave(); playSound("tick");
  });
}

function escapeHtml(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

const pad = (n, len = 2) => String(n).padStart(len, "0");

function fmtTime(sec, comma = false) {
  let ticks = Math.round(Math.max(0, Number(sec) || 0) * 100);
  const h = Math.floor(ticks / 360000); ticks %= 360000;
  const m = Math.floor(ticks / 6000); ticks %= 6000;
  const s = Math.floor(ticks / 100); ticks %= 100;
  return `${pad(h)}:${pad(m)}:${pad(s)}${comma ? "," : "."}${pad(ticks)}`;
}

function parseTime(value) {
  const str = String(value).trim().replace(",", ".");
  if (!str) return null;
  let m = str.match(/^(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)$/);
  if (m) {
    const result = Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]);
    return Number(m[2]) < 60 && Number(m[3]) < 60 && Number.isFinite(result) ? result : null;
  }
  m = str.match(/^(\d+):(\d{1,2}(?:\.\d+)?)$/);
  if (m) {
    const result = Number(m[1]) * 60 + Number(m[2]);
    return Number(m[2]) < 60 && Number.isFinite(result) ? result : null;
  }
  const n = Number(str);
  return Number.isFinite(n) ? n : null;
}

function readJson(key) {
  try {
    const value = JSON.parse(localStorage.getItem(key) || "{}");
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}

function writeJson(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ }
}

let toastTimer = null;
function toast(message, type = "info", ms = 3500) {
  toastEl.textContent = message;
  toastEl.className = `toast show ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.className = "toast"; }, ms);
}

function setSaveState(text) {
  $("#topSaveState").textContent = text;
}

// ---------- 悬浮简介 ----------
let tipTarget = null;
let tipTimer = null;
let tipHoverTarget = null;

function positionTip() {
  if (!tipTarget || !tipEl.classList.contains("show")) return;
  const r = tipTarget.getBoundingClientRect();
  const t = tipEl.getBoundingClientRect();
  let left = r.left + r.width / 2 - t.width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - t.width - 8));
  let top = r.top - t.height - 8;
  if (top < 8) top = Math.min(r.bottom + 8, window.innerHeight - t.height - 8);
  tipEl.style.left = `${Math.round(left)}px`;
  tipEl.style.top = `${Math.max(8, Math.round(top))}px`;
}

function showTip(target) {
  const text = target?.dataset.tip;
  if (!text) return;
  if (tipTarget && tipTarget !== target) tipTarget.removeAttribute("aria-describedby");
  tipTarget = target;
  target.setAttribute("aria-describedby", "tip");
  tipEl.textContent = text;
  tipEl.classList.add("show");
  positionTip();
}

function hideTip() {
  clearTimeout(tipTimer);
  tipHoverTarget = null;
  if (tipTarget) tipTarget.removeAttribute("aria-describedby");
  tipTarget = null;
  tipEl.classList.remove("show");
}

function bindTooltips() {
  document.addEventListener("pointerover", (e) => {
    const target = e.target.closest?.("[data-tip]");
    if (!target || target === tipHoverTarget || target === tipTarget) return;
    hideTip();
    tipHoverTarget = target;
    tipTimer = setTimeout(() => {
      if (target.isConnected && target.matches(":hover")) showTip(target);
    }, 600);
  });
  document.addEventListener("pointerout", (e) => {
    const target = tipTarget || tipHoverTarget;
    if (!target) return;
    const to = e.relatedTarget;
    if (to && target.contains(to)) return;
    hideTip();
  });
  document.addEventListener("focusin", (e) => {
    const target = e.target.closest?.("[data-tip]");
    if (target && e.target.matches(":focus-visible")) showTip(target);
  });
  document.addEventListener("focusout", () => hideTip());
  document.addEventListener("pointerdown", hideTip);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") hideTip(); });
  window.addEventListener("scroll", hideTip, true);
  window.addEventListener("resize", hideTip);
  window.addEventListener("resize", () => { positionModelMenu(); positionAuxMenu(); });
  window.addEventListener("scroll", e => {
    if (!e.target.closest?.("#modelMenu")) positionModelMenu();
    if (!e.target.closest?.("#auxMenu")) positionAuxMenu();
  }, true);
}

// ---------- 主题 ----------
let cordPull = 0;
let cordVelocity = 0;
let cordFrame = 0;
let cordRenderFrame = 0;
let cordPendingPull = 0;
let cordPulseTimer = 0;
let cordPointer = null;
const CORD_THRESHOLD = 17;
const CORD_RESIST_FROM = 25;

function reduceMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function cordThemeTarget() {
  let dark = state.theme === "dark";
  if (state.theme === "system") dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  return dark ? "light" : "dark";
}

function applyTheme(theme, persist = true) {
  if (!THEMES.includes(theme)) theme = "system";
  state.theme = theme;
  if (theme === "system") themeRoot.removeAttribute("data-theme");
  else themeRoot.dataset.theme = theme;
  if (persist) {
    try { localStorage.setItem(THEME_KEY, theme); } catch { /* ignore */ }
  }
  appearance.style.setProperty("--theme-index", String(THEMES.indexOf(theme)));
  themeButtons.forEach((button) => {
    const active = button.dataset.theme === theme;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  lampCord.setAttribute("aria-label", `向下拉动，切换为${cordThemeTarget() === "dark" ? "深色" : "浅色"}主题`);
  if (persist) scheduleConfigSave();
  const tc = document.querySelector('meta[name="theme-color"]');
  if (tc) {
    const dark = theme === "dark" || (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    tc.setAttribute("content", dark ? "#111111" : "#ededed");
  }
  if (typeof drawWave === "function") drawWave();
}

window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (state.theme === "system") applyTheme("system", false);
});

function switchTheme(theme, useViewTransition = true) {
  if (theme === state.theme) return;
  if (!useViewTransition || !document.startViewTransition || reduceMotion()) {
    themeRoot.classList.add("theme-changing");
    applyTheme(theme);
    window.setTimeout(() => themeRoot.classList.remove("theme-changing"), 320);
    return;
  }
  try {
    document.startViewTransition(() => applyTheme(theme));
  } catch {
    applyTheme(theme);
  }
}

function applyCord(pull) {
  lampGrip.style.transform = `translate3d(-50%, ${pull.toFixed(2)}px, 0)`;
  const scale = (CORD_LINE_BASE + Math.max(0, pull)) / CORD_LINE_BASE;
  lampLine.style.transform = `translateX(-50%) scaleY(${scale.toFixed(4)})`;
}

function renderCord(pull) {
  cordPull = pull;
  if (cordRenderFrame) cancelAnimationFrame(cordRenderFrame);
  cordRenderFrame = 0;
  applyCord(pull);
}

/* 拖拽时只记录目标位置，真正写样式合并到下一帧：pointermove 可能一帧触发多次，
   直接改样式会让浏览器反复做样式计算、布局和重绘，灯绳就会发涩。 */
function queueCord(pull) {
  cordPull = pull;
  cordPendingPull = pull;
  if (cordRenderFrame) return;
  cordRenderFrame = requestAnimationFrame(() => {
    cordRenderFrame = 0;
    applyCord(cordPendingPull);
  });
}

function stopCordMotion() {
  if (cordFrame) cancelAnimationFrame(cordFrame);
  if (cordRenderFrame) cancelAnimationFrame(cordRenderFrame);
  if (cordPulseTimer) window.clearTimeout(cordPulseTimer);
  cordFrame = 0;
  cordRenderFrame = 0;
  cordPulseTimer = 0;
}

function animateCordSpring(from, velocity) {
  stopCordMotion();
  if (reduceMotion()) {
    lampCord.classList.remove("is-pulling");
    renderCord(0);
    return;
  }
  lampCord.classList.add("is-pulling");
  let position = from;
  let speed = velocity;
  let last = performance.now();
  const tick = (now) => {
    const elapsed = Math.min(0.032, Math.max(0.001, (now - last) / 1000));
    last = now;
    const steps = Math.max(1, Math.ceil(elapsed / 0.008));
    const step = elapsed / steps;
    for (let i = 0; i < steps; i += 1) {
      const accel = -250 * position - 17 * speed;
      speed += accel * step;
      position += speed * step;
    }
    if (position < -4.5) {
      position = -4.5;
      speed = Math.max(0, speed);
    } else if (position > 42) {
      position = 42;
      speed = Math.min(0, speed);
    }
    renderCord(position);
    if (Math.abs(position) < 0.05 && Math.abs(speed) < 0.9) {
      renderCord(0);
      lampCord.classList.remove("is-pulling");
      cordFrame = 0;
      return;
    }
    cordFrame = requestAnimationFrame(tick);
  };
  cordFrame = requestAnimationFrame(tick);
}

function pulseCord() {
  stopCordMotion();
  if (reduceMotion()) {
    switchTheme(cordThemeTarget(), false);
    return;
  }
  renderCord(14);
  lampCord.classList.add("is-pulling");
  cordPulseTimer = window.setTimeout(() => {
    cordPulseTimer = 0;
    switchTheme(cordThemeTarget(), false);
    animateCordSpring(14, -18);
  }, 70);
}

function bindThemeControls() {
  themeButtons.forEach((button) => {
    button.addEventListener("click", () => switchTheme(button.dataset.theme));
  });

  lampCord.addEventListener("pointerdown", (event) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    event.preventDefault();
    stopCordMotion();
    cordPointer = {
      id: event.pointerId,
      startY: event.clientY,
      startPull: cordPull,
      lastTime: performance.now(),
      moved: false,
    };
    cordVelocity = 0;
    lampCord.classList.add("is-pulling");
    if (lampCord.setPointerCapture) lampCord.setPointerCapture(event.pointerId);
  });

  lampCord.addEventListener("pointermove", (event) => {
    if (!cordPointer || event.pointerId !== cordPointer.id) return;
    event.preventDefault();
    const raw = Math.max(0, cordPointer.startPull + event.clientY - cordPointer.startY);
    const pull = raw <= CORD_RESIST_FROM
      ? raw
      : CORD_RESIST_FROM + (raw - CORD_RESIST_FROM) * 0.24;
    const now = performance.now();
    const elapsed = Math.max(1, now - cordPointer.lastTime);
    const instant = ((pull - cordPull) / elapsed) * 1000;
    cordVelocity = cordVelocity * 0.7 + instant * 0.3;
    cordPointer.lastTime = now;
    if (Math.abs(event.clientY - cordPointer.startY) > 3) cordPointer.moved = true;
    queueCord(pull);
  });

  const endCord = (event, cancelled) => {
    if (!cordPointer || event.pointerId !== cordPointer.id) return;
    const wasMoved = cordPointer.moved;
    if (lampCord.hasPointerCapture?.(event.pointerId)) {
      lampCord.releasePointerCapture(event.pointerId);
    }
    cordPointer = null;
    if (!cancelled && cordPull >= CORD_THRESHOLD) {
      switchTheme(cordThemeTarget(), false);
      animateCordSpring(cordPull, cordVelocity);
    } else if (!cancelled && !wasMoved) {
      pulseCord();
    } else {
      animateCordSpring(cordPull, cordVelocity);
    }
    cordVelocity = 0;
  };

  lampCord.addEventListener("pointerup", (event) => endCord(event, false));
  lampCord.addEventListener("pointercancel", (event) => endCord(event, true));
  lampCord.addEventListener("click", (event) => {
    if (event.detail !== 0) {
      event.preventDefault();
      return;
    }
    pulseCord();
  });
}

// ---------- 设置 ----------
function modeLabel() {
  return MODE_LABEL[state.settings.mode] || MODE_LABEL.extract;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function normalizeSettings() {
  const s = state.settings;
  if(s.timingDefaultsVersion!==2){s.subtitleLead=0;s.subtitleTail=0;s.timingDefaultsVersion=2;}
  s.mode = s.mode === "align" ? "align" : "extract";
  s.device = ["auto", "cpu", "cuda"].includes(s.device) ? s.device : "auto";
  s.model = String(s.model || "medium");
  s.vad = s.vad !== false;
  // 旧设置 dropInterjections=true 表示“丢弃”，迁移为 keepInterjections=false。
  if (s.keepInterjections == null && s.dropInterjections != null) s.keepInterjections = s.dropInterjections === false;
  s.keepInterjections = s.keepInterjections === true;
  delete s.dropInterjections;
  s.gap = Math.round(clamp(Number(s.gap) || 0, PARAM_LIMITS.gap.min, PARAM_LIMITS.gap.max) * 10) / 10;
  s.width = Math.round(clamp(Number(s.width) || 20, PARAM_LIMITS.width.min, PARAM_LIMITS.width.max));
  delete s.addPunc;
  delete s.withRole;
  s.keepPunc = s.keepPunc !== false;
  delete s.skipEmpty;
  if (typeof s.singleLine === "boolean") s.wrapSubtitles = !s.singleLine;
  delete s.singleLine;
  s.wrapSubtitles = s.wrapSubtitles !== false;
  for(const key of ['subtitleLead','subtitleTail'])s[key]=Number.isFinite(Number(s[key]))?Math.round(clamp(Number(s[key]),0,1)*100)/100:0;
  s.subtitleMin=Math.round(clamp(Number(s.subtitleMin)||0,0,10)*10)/10;
  s.subtitleMax=Math.round(clamp(Number(s.subtitleMax)||0,0,30)*10)/10;
  if(s.subtitleMax>0&&s.subtitleMin>s.subtitleMax)s.subtitleMin=s.subtitleMax;
  delete s.excludeNoise;
  s.sound = s.sound !== false;
  s.arabicNumbers = s.arabicNumbers === true;
  for (const [key, fallback] of Object.entries({matchMinimum:.64, matchHigh:.94, matchPhonetic:.90, matchAmbiguity:.035})) {
    s[key] = Number.isFinite(Number(s[key])) ? clamp(Number(s[key]), 0, 1) : fallback;
  }
  s.matchHigh = Math.max(s.matchHigh, s.matchMinimum);
  s.matchGroup = Math.round(clamp(Number(s.matchGroup) || 3, 1, 4));
  s.volume = Number.isFinite(Number(s.volume ?? 80)) ? Math.round(clamp(Number(s.volume ?? 80), 0, 100)) : 80;
  s.waveGain=Number.isFinite(Number(s.waveGain))?clamp(Number(s.waveGain),.5,4):1;
  s.waveStyle = s.waveStyle === 'bar' ? 'bar' : 'wave';
}

function updateWidthInfo() {
  const s = state.settings;
  $("#widthInfo").textContent = `${modeLabel()} · 每行 ≤ ${s.width} · 停顿 ≥ ${s.gap}s`;
}

function updateModeNote() {
  if (state.settings.mode !== 'align') {
    $('#modeNote').textContent = '提取视频人声，生成并导出 SRT';
    return;
  }
  $('#modeNote').textContent = '对齐视频音轨，剧本作为纠错参考';
}

const PARAM_FIELDS = [
  { key: "gap", input: "#gapInput", range: "#gapRange", ...PARAM_LIMITS.gap },
  { key: "width", input: "#widthInput", range: "#widthRange", ...PARAM_LIMITS.width },
];

function syncParamControl(param, value = null) {
  const input = $(param.input);
  const range = $(param.range);
  if (!input || !range) return;
  if (value !== null) {
    const v = clamp(Number(value) || param.min, param.min, param.max);
    input.value = param.step < 1 ? String(Number(v.toFixed(1))) : String(Math.round(v));
  }
  const current = clamp(Number(input.value) || param.min, param.min, param.max);
  range.value = String(current);
  const pct = param.max > param.min ? ((current - param.min) / (param.max - param.min)) * 100 : 0;
  range.style.setProperty("--fill", `${pct}%`);
  const stepper = range.closest(".param")?.querySelector(".stepper");
  if (stepper) {
    stepper.querySelector('[data-dir="1"]').disabled = current >= param.max;
    stepper.querySelector('[data-dir="-1"]').disabled = current <= param.min;
  }
}

function syncParamControls() {
  for (const param of PARAM_FIELDS) syncParamControl(param, state.settings[param.key]);
}

function syncSettingsUI() {
  const s = state.settings;
  $("#deviceSelect").value = s.device;
  window.SubtitleDevicePicker?.sync();
  $("#vadCheck").checked = !!s.vad;
  $("#keepInterjections").checked = !!s.keepInterjections;
  for(const key of ['subtitleLead','subtitleTail','subtitleMin','subtitleMax'])$('#'+key).value=String(s[key]);
  $('#waveGain').value=String(s.waveGain || 1);
  $('#waveGainValue').textContent=(s.waveGain || 1)+'×';
  $('#waveStyle').textContent=s.waveStyle === 'bar' ? '矩形' : '波浪';
  for (const key of ["keepPunc", "wrapSubtitles"]) $("#" + key).checked = !!s[key];
  for (const key of ['matchMinimum','matchHigh','matchPhonetic','matchAmbiguity','matchGroup']) $('#' + key).value = s[key];
  $("#arabicNumbers").checked = !!s.arabicNumbers;
  $("#soundToggle").setAttribute("aria-pressed", s.sound ? "true" : "false");
  $("#soundToggle").textContent = s.sound ? "音效 开" : "音效 关";
  $("#soundVolume").value = String(s.volume);
  $("#soundLevel").textContent = s.volume + "%";
  syncParamControls();
  setMode(s.mode, false);
  updateModelButton();
}

function setMode(mode, persist = true) {
  if (persist && editLocked()) return;
  const previous = state.settings.mode;
  state.settings.mode = mode === "align" ? "align" : "extract";
  const next = state.settings.mode;
  $$(".mode-btn").forEach((btn) => {
    const active = btn.dataset.mode === next;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
    btn.tabIndex = active ? 0 : -1;
  });
  updateModeNote();
  updateModeUI();
  updateWidthInfo();
  renderScript();
  updateButtons();
  renderSubtitleSource();
  if (persist && previous !== next && next === "align" && state.subtitleFile?.origin === "generated" && state.cues.length) {
    toast("已带入提取的 SRT，可直接对齐；导入剧本后可用“开始校验”核对参考", "success");
  }
  if (persist) {
    collectSettings();
    scheduleSave();
    scheduleConfigSave();
  }
}

function updateModeUI() {
  const align = state.settings.mode === "align";
  document.body.dataset.mode = align ? 'align' : 'extract';
  $('#settingsTitle').textContent = '参数';
  const zone = $("#scriptZone");
  $("#dropgrid").classList.toggle("single", !align);
  zone.hidden = !align;
  zone.classList.toggle("mode-needed", align && !state.script.length && !state.scriptUploading);
  zone.setAttribute("aria-disabled", align ? "false" : "true");
  zone.tabIndex = align ? 0 : -1;
  $("#extractActions").hidden = false;
  $('#rightModeNote').hidden = false;
  $('#rightModeNote').textContent = state.cues.length ? '顺序：开始校验 → 校准字幕 → 对齐字幕；自动完成不代表人工确认，文字确认不覆盖时间问题。标签不自动关闭导出。点击字幕定位视频。' : '提取或导入字幕后，在这里逐条查看。';
  $("#btnRealignAll").hidden = !align;
  $("#btnCalibrate").hidden = !align;
  $('#referenceSettings').hidden = !align || !state.script.length;
  $('#btnExtract').textContent = align ? '开始校验' : '提取字幕';
  $('#btnExtract').dataset.tip = align ? '按参考文档逐条校验字幕，只标出错误和问题，不改动字幕文字和时间。' : '提取视频或音频中的人声字幕，生成 SRT。';
  const clear = $("#scriptClear");
  clear.hidden = !align || (!state.script.length && !state.scriptUploading);
  clear.disabled = !align;
}

function loadSettings() {
  Object.assign(state.settings, readJson(SETTINGS_KEY));
  normalizeSettings();
  syncSettingsUI();
}

function collectSettings() {
  const s = state.settings;
  normalizeSettings();
  s.model = String($("#modelBtnText").dataset.model || s.model || "medium");
  s.device = $("#deviceSelect").value;
  s.vad = $("#vadCheck").checked;
  s.keepInterjections = $("#keepInterjections").checked;
  for(const key of ['subtitleLead','subtitleTail','subtitleMin','subtitleMax'])s[key]=Number($('#'+key).value);
  s.gap = Math.round(clamp(
    Number($("#gapInput").value) || 0, PARAM_LIMITS.gap.min, PARAM_LIMITS.gap.max
  ) * 10) / 10;
  s.width = Math.round(clamp(
    Number($("#widthInput").value) || 20, PARAM_LIMITS.width.min, PARAM_LIMITS.width.max
  ));
  for (const key of ["keepPunc", "wrapSubtitles"]) s[key] = $("#" + key).checked;
  for (const key of ['matchMinimum','matchHigh','matchPhonetic','matchAmbiguity','matchGroup']) s[key] = Number($('#' + key).value);
  normalizeSettings();
  for(const key of ['subtitleLead','subtitleTail','subtitleMin','subtitleMax'])$('#'+key).value=String(s[key]);
  s.arabicNumbers = $("#arabicNumbers").checked;
  syncParamControls();
  writeJson(SETTINGS_KEY, s);
  updateWidthInfo();
  return s;
}

function commitSettings(event) {
  collectSettings();
  if (['subtitleLead','subtitleTail','subtitleMin','subtitleMax'].includes(event?.target?.id)) state.timingPreview=false;
  updateButtons();
  if (['deviceSelect','vadCheck','subtitleLead','subtitleTail','subtitleMin','subtitleMax','gapInput','gapRange'].includes(event?.target?.id)) drawWave();
  else renderTable();
  updateOverlay();
  scheduleConfigSave();
  scheduleSave();
  if (['gapInput', 'gapRange'].includes(event?.target?.id)) applyPauseSetting();
}

async function applyPauseSetting() {
  if (editLocked() || !state.cues.length || !state.settings.gap) return;
  const before = snapshot(), path = projectPath(), epoch = state.mediaEpoch;
  state.operation = '按停顿分段'; updateButtons();
  try {
    const result = await api('/api/segment', {cues:state.cues, gap_seconds:state.settings.gap,
      max_width:state.settings.width, pauses_only:true});
    if (!sameDocument(path, epoch, before)) throw new Error('字幕已变化，请重新应用停顿断句');
    if (result.gap_splits) {
      replaceCues(result.cues, {before});
      if (state.script.length) await rematchDerivedCues(state.cues.map((cue,i)=>!cue.reference_match&&cue.source==='gap_split'?i:-1).filter(i=>i>=0));
      toast(`已按停顿增加 ${result.gap_splits} 条字幕，可撤销`, 'success');
    } else {
      toast('没有可安全切分的停顿；缺少词时间的字幕请先音轨对齐', 'info', 5000);
    }
  } catch (error) { toast(error.message, 'error'); }
  finally { state.operation = ''; updateButtons(); }
}

async function rematchDerivedCues(indices,{preserveModification=false}={}) {
  if (!indices.length || !state.script.length) return;
  const before=snapshot(),path=projectPath(),epoch=state.mediaEpoch,previous=state.operation;
  const doc=state.scriptDocument || {text:state.script.map(line=>line.text).join('\n')};
  state.operation='校验字幕';updateButtons();
  try {
    const groups=[];let targets=[],neighborhood=new Set();
    for(const i of [...new Set(indices)].sort((a,b)=>a-b)){
      const nearby=[];for(let j=Math.max(0,i-3);j<=Math.min(state.cues.length-1,i+3);j++)nearby.push(j);
      if(targets.length && neighborhood.size+nearby.filter(j=>!neighborhood.has(j)).length>5000){
        groups.push({targets,sequence:[...neighborhood].sort((a,b)=>a-b)});targets=[];neighborhood=new Set();
      }
      targets.push(i);nearby.forEach(j=>neighborhood.add(j));
    }
    if(targets.length)groups.push({targets,sequence:[...neighborhood].sort((a,b)=>a-b)});
    const updates=new Map();
    for(const group of groups){
      const sequence_indices=group.sequence;
      const checked=await runReferenceMatch({cues:sequence_indices.map(i=>state.cues[i]),sequence_indices,document:doc,lines:[],
        options:{minimum:state.settings.matchMinimum,high:state.settings.matchHigh,phonetic:state.settings.matchPhonetic,
          ambiguity:state.settings.matchAmbiguity,max_group:state.settings.matchGroup}});
      if (!sameDocument(path,epoch,before)) throw new Error('字幕已变化，已保留当前内容');
      if(checked.cues?.length!==sequence_indices.length)throw new Error('校验结果不完整，已保留当前内容');
      const positions=new Map(sequence_indices.map((index,i)=>[index,i]));
      group.targets.forEach(index=>updates.set(index,normalizeCue({...checked.cues[positions.get(index)],
        modification_complete:preserveModification?state.cues[index].modification_complete:false},index)));
    }
    updates.forEach((cue,index)=>{state.cues[index]=cue;});
    renderAll();scheduleSave();
    return true;
  } catch(error) {toast('字幕已保留，参考校验未完成：'+error.message,'error');return false;}
  finally {state.operation=previous;updateButtons();}
}

function applyProjectSettings(saved) {
  if (!saved || typeof saved !== "object") return;
  const s = state.settings;
  if (saved.mode === "extract" || saved.mode === "align") s.mode = saved.mode;
  if (saved.width != null) s.width = clamp(
    Number(saved.width) || 20, PARAM_LIMITS.width.min, PARAM_LIMITS.width.max
  );
  if (saved.gap != null) s.gap = clamp(
    Number(saved.gap) || 0, PARAM_LIMITS.gap.min, PARAM_LIMITS.gap.max
  );
  if (saved.vad != null) s.vad = !!saved.vad;
  if (saved.keepInterjections != null) s.keepInterjections = !!saved.keepInterjections;
  else if (saved.dropInterjections != null) s.keepInterjections = saved.dropInterjections === false;
  if (saved.device) s.device = ["auto", "cpu", "cuda"].includes(saved.device) ? saved.device : "auto";
  if (saved.model && modelIsAvailable(saved.model)) s.model = saved.model;
  for (const key of ["keepPunc", "wrapSubtitles"]) if (saved[key] != null) s[key] = !!saved[key];
  for(const key of ['subtitleLead','subtitleTail'])s[key]=saved.timingDefaultsVersion===2 && saved[key]!=null?saved[key]:0;
  for(const key of ['subtitleMin','subtitleMax'])if(saved[key]!=null)s[key]=saved[key];
  if (saved.wrapSubtitles == null && typeof saved.singleLine === 'boolean') s.wrapSubtitles = !saved.singleLine;
  for (const key of ['matchMinimum','matchHigh','matchPhonetic','matchAmbiguity','matchGroup']) if (saved[key] != null) s[key] = saved[key];
  if (saved.sound != null) s.sound = !!saved.sound;
  if (saved.arabicNumbers != null) s.arabicNumbers = !!saved.arabicNumbers;
  if (saved.volume != null) s.volume = saved.volume;
  if(saved.auxModel) s.auxModel=saved.auxModel;
  if(saved.waveGain!=null)s.waveGain=saved.waveGain;
  if(saved.waveStyle!=null)s.waveStyle=saved.waveStyle;
  normalizeSettings();
  syncSettingsUI();
  scheduleConfigSave();
}

// ---------- 界面布局 ----------
function clampPanelWidths() {
  const viewport = window.innerWidth || 1440;
  const rightVisible = viewport > 1180;
  const maxLeft = rightVisible
    ? Math.max(280, Math.min(520, viewport - state.layout.right - 472))
    : Math.max(280, Math.min(520, viewport - 466));
  const maxRight = Math.max(280, Math.min(520, viewport - state.layout.left - 472));
  state.layout.left = Math.round(clamp(state.layout.left, 280, maxLeft));
  state.layout.right = Math.round(clamp(state.layout.right, 280, maxRight));
}

function applyLayout() {
  clampPanelWidths();
  const layout = $("#layout");
  layout.style.setProperty("--left-w", `${state.layout.left}px`);
  layout.style.setProperty("--right-w", `${state.layout.right}px`);
}

function resetPanelWidths() {
  state.windowMaximized = windowIsMaximized();
  const width = defaultPanelWidth();
  state.layout = { left: width, right: width };
  applyLayout();
}

function bindSplitters() {
  $$(".splitter").forEach((splitter) => {
    splitter.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      hideTip();
      const side = splitter.dataset.side;
      const startX = event.clientX;
      const startWidth = side === "left" ? state.layout.left : state.layout.right;
      splitter.setPointerCapture?.(event.pointerId);
      document.body.classList.add("col-resizing");

      const onMove = (moveEvent) => {
        if (moveEvent.pointerId !== event.pointerId) return;
        const delta = moveEvent.clientX - startX;
        if (side === "left") state.layout.left = startWidth + delta;
        else state.layout.right = startWidth - delta;
        applyLayout();
      };
      const onUp = (upEvent) => {
        if (upEvent.pointerId !== event.pointerId) return;
        splitter.removeEventListener("pointermove", onMove);
        splitter.removeEventListener("pointerup", onUp);
        splitter.removeEventListener("pointercancel", onUp);
        document.body.classList.remove("col-resizing");
        if (splitter.hasPointerCapture?.(upEvent.pointerId)) {
          splitter.releasePointerCapture(upEvent.pointerId);
        }
        drawWave();
      };
      splitter.addEventListener("pointermove", onMove);
      splitter.addEventListener("pointerup", onUp);
      splitter.addEventListener("pointercancel", onUp);
    });

    splitter.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      const step = event.shiftKey ? 40 : 10;
      const direction = event.key === "ArrowRight" ? 1 : -1;
      if (splitter.dataset.side === "left") state.layout.left += direction * step;
      else state.layout.right -= direction * step;
      applyLayout();
      drawWave();
    });
  });

  window.addEventListener("resize", () => {
    if (windowIsMaximized() !== state.windowMaximized) resetPanelWidths();
    else applyLayout();
    drawWave();
  });
}

// ---------- 连接与模型 ----------
async function loadConfig() {
  try {
    const d = await api("/api/config");
    const cfg = d.config || {};
    if (!localStorage.getItem(THEME_KEY) && THEMES.includes(cfg.theme)) {
      applyTheme(cfg.theme, false);
      try { localStorage.setItem(THEME_KEY, cfg.theme); } catch { /* ignore */ }
    }
    if (!localStorage.getItem(SETTINGS_KEY) && cfg.settings && typeof cfg.settings === "object") {
      Object.assign(state.settings, cfg.settings);
      if(cfg.settings.timingDefaultsVersion!==2){state.settings.subtitleLead=0;state.settings.subtitleTail=0;}
      normalizeSettings();
      syncSettingsUI();
    }
    // 栏宽按当前窗口分配；旧配置不再覆盖启动时的左右对称布局。
  } catch { /* 配置不存在时使用默认值 */ }
}

async function saveConfig(silent = true) {
  const payload = { settings: { ...collectSettings() }, layout: { ...state.layout }, layoutVersion: 2, theme: state.theme };
  const task = configSaveChain.then(async () => {
  try {
    await api("/api/config", payload);
    return true;
  } catch (error) {
    if (!silent) toast(`配置保存失败：${error.message}`, "error");
    return false;
  }
  });
  configSaveChain = task.catch(() => false);
  return task;
}

function scheduleConfigSave() {
  clearTimeout(state.configTimer);
  state.configTimer = setTimeout(() => saveConfig(true), 800);
}

async function checkHealth() {
  clearTimeout(state.healthTimer);
  try {
    const d = await api("/api/health");
    $("#healthDot").className = "ok";
    $("#healthText").textContent = d.version === APP_VERSION ? `已连接 v${d.version}` : `后台 v${d.version} · 请保存后重启`;
    state.versionMismatch = d.version !== APP_VERSION;
    state.healthVersion = d.version || "";
    state.models = d.models || [];
    state.auxModels = d.auxiliary || [];
    state.gpuStatus = d.gpu || state.gpuStatus;
    renderModels();
  } catch (e) {
    $("#healthDot").className = "bad";
    $("#healthText").textContent = "未连接";
    state.healthTimer = setTimeout(checkHealth, 5000);
  }
}

function modelIsAvailable(name) { return !!state.models.find(m => m.name === name)?.available; }
function orderedModels() {
  return [...state.models].sort((a,b) => (MODEL_ORDER.includes(a.name) ? MODEL_ORDER.indexOf(a.name) : 99) - (MODEL_ORDER.includes(b.name) ? MODEL_ORDER.indexOf(b.name) : 99));
}
function renderModels() { updateModelButton(); renderModelMenu(); renderAuxModels(); renderGpuState(); updateButtons(); }
function gpuStateView() {
  const gpu = state.gpuStatus;
  if (!gpu || !gpu.nvidia) return null;
  const engines = (gpu.engines || []).filter(e => e.prepared);
  if (gpu.state === 'cuda') return { text: 'GPU 环境已就绪', cls: 'ready', action: '', hint: '识别或对齐时可在“设备”选择自动/GPU。' };
  if (gpu.state === 'partial') return { text: '部分引擎已启用 GPU', cls: '', action: '补齐 GPU', hint: '给其余已安装引擎补装 CUDA 版 PyTorch。' };
  if (gpu.state === 'failed') return { text: 'GPU 安装失败，已回退 CPU', cls: 'failed', action: '重试 GPU', hint: 'CPU 环境仍可用；可重试下载 GPU 或继续用 CPU。' };
  if (!engines.length) {
    if (gpu.mode === 'manual') {
      return { text: 'GPU 环境未下载 · 先下载任一模型', cls: '', action: '下载 GPU 环境',
        disabled: true, hint: '先下载任一模型，再按需下载 GPU 环境。' };
    }
    return { text: '下载模型时将自动安装 GPU 环境', cls: '', action: '', hint: 'GPU 环境按需安装，当前不下载任何内容。' };
  }
  return { text: '当前为 CPU 环境 · ' + (gpu.size_hint || ''), cls: '', action: '下载 GPU 环境', hint: '下载 CUDA 版 PyTorch 供 Qwen/FireRed 引擎使用；失败会保留 CPU。' };
}
function renderGpuState() {
  const box = $('#gpuState'), text = $('#gpuStateText'), button = $('#gpuDownload');
  if (!box || !text || !button) return;
  const view = gpuStateView();
  if (!view) {
    box.hidden = true;
    button.hidden = true;
    button.dataset.blocked = '';
    return;
  }
  box.hidden = false;
  box.className = 'gpu-state' + (view.cls ? ' ' + view.cls : '');
  text.textContent = view.text;
  box.dataset.tip = view.hint;
  button.textContent = view.action || '下载 GPU 环境';
  button.hidden = !view.action;
  button.dataset.blocked = view.disabled ? '1' : '';
  button.disabled = view.disabled || !view.action || editLocked() || downloadBusy();
  button.dataset.tip = view.hint;
}
function modelActionHtml(model) {
  const installed=model.downloaded || model.available;
  const ready=installed && model.runtime_ready!==false;
  const name=escapeHtml(model.name), disabled=editLocked()?' disabled':'';
  const actions=[];
  if(model.engine!=='builtin') {
    actions.push('<button type="button" class="model-locate" data-locate="'+name+'" data-tip="选择已下载模型所在的文件夹，不移动、不重复下载模型。"'+disabled+'>选择目录</button>');
    if(installed && !ready) {
      actions.push('<button type="button" class="model-download model-config" data-download="'+name+'" data-tip="检查并配置运行环境；模型文件已存在时不重复下载。"'+disabled+'>配置环境</button>');
    } else if(!installed) {
      actions.push('<button type="button" class="model-download" data-download="'+name+'" data-tip="下载模型并配置运行环境。"'+disabled+'>下载模型</button>');
    }
  }
  if(ready) actions.push('<span class="model-ready is-ready">已就绪</span>');
  return actions.join('');
}
function modelOptionHtml(model) {
  const parts = [model.description].filter(Boolean);
  if (model.size_hint && model.size_hint !== '已内置') parts.push(model.size_hint);
  return '<span class="model-name">' + escapeHtml(model.label || model.name) + '</span>' +
    (parts.length ? '<small class="model-desc">' + escapeHtml(parts.join(' · ')) + '</small>' : '');
}
function renderAuxModels() {
  const selected = state.auxModels.find(m => m.name === state.settings.auxModel) || state.auxModels[0];
  $('#auxBtnText').textContent = selected?.label || 'Qwen3-ForcedAligner-0.6B';
  $('#auxLabel').dataset.tip = state.auxModels.map(m => m.label + '：' + m.description).join('\n');
  $('#auxMenu').innerHTML = state.auxModels.map(m => '<div class="model-choice"><button type="button" class="model-option" role="option" data-aux="' + escapeHtml(m.name) + '" aria-selected="' + (m.name === selected?.name) + '" ' + (!m.available ? 'disabled' : '') + '>' + modelOptionHtml(m) + '</button>' + modelActionHtml(m) + '</div>').join('');
}
function positionAuxMenu() {
  const menu = $('#auxMenu');
  if (menu.hidden) return;
  if (window.placePickerMenu) window.placePickerMenu($('#auxBtn'), menu, 300);
}
function toggleAuxMenu(open) {
  const menu = $('#auxMenu'), next = open ?? menu.hidden; menu.hidden = !next;
  $('#auxBtn').setAttribute('aria-expanded', next ? 'true' : 'false');
  if (next) { renderAuxModels(); positionAuxMenu(); }
}
function renderModelMenu() {
  const menu = $('#modelMenu');
  menu.innerHTML = orderedModels().map(m => '<div class="model-choice">' +
    '<button type="button" class="model-option" role="option" data-name="' + escapeHtml(m.name) + '" aria-selected="' + (m.name === state.settings.model) + '" ' + (!m.available ? 'disabled' : '') + '>' + modelOptionHtml(m) + '</button>' +
    modelActionHtml(m) + '</div>').join('');
}
function updateModelButton() {
  const btn = $('#modelBtn'), text = $('#modelBtnText');
  const available = orderedModels().filter(m => m.available);
  if (!state.models.some(m => m.name === state.settings.model)) state.settings.model = available[0]?.name || 'large-v3';
  const selected = state.models.find(m => m.name === state.settings.model);
  text.textContent = selected?.label || state.settings.model;
  text.dataset.model = state.settings.model;
  btn.disabled = !state.models.length || (!!state.jobId);
  $('#modelLabel').dataset.tip = orderedModels().map(m => (m.label || m.name) + '：' + (m.description || '')).join('\n');
}
function positionModelMenu() {
  const menu = $('#modelMenu');
  if (menu.hidden) return;
  if (window.placePickerMenu) window.placePickerMenu($('#modelBtn'), menu, 300);
}
function toggleModelMenu(open) {
  const menu = $('#modelMenu'), next = open ?? menu.hidden;
  menu.hidden = !next; $('#modelBtn').setAttribute('aria-expanded', next ? 'true' : 'false');
  if (next) { renderModelMenu(); positionModelMenu(); }
}
function chooseModel(name) {
  if (!modelIsAvailable(name)) return;
  state.settings.model = name; updateModelButton(); renderModelMenu(); collectSettings(); updateButtons(); scheduleConfigSave(); toggleModelMenu(false);
}
function modelAvailable() { return modelIsAvailable(state.settings.model); }
function downloadBusy() { return ['downloading','preparing'].includes(state.modelDownload?.status); }
function showDownloadProgress() {
  const d = state.modelDownload; if (!d || d.status === 'idle') return;
  const ratio = Math.round((d.progress || 0) * 100);
  progressBar.style.width = ratio + '%';
  progressBar.style.background = d.status === 'error' ? 'var(--red)' : 'var(--green)';
  progressBar.parentElement.classList.toggle('indeterminate', d.status === 'preparing');
  const bytes = d.total ? ' · ' + (d.downloaded / 1024**2).toFixed(0) + '/' + (d.total / 1024**2).toFixed(0) + ' MB' : '';
  const speed = d.speed_bps > 0 ? ' · ' + (d.speed_bps / 1024**2).toFixed(2) + ' MB/s' : '';
  jobMsg.textContent = d.message + (d.status === 'downloading' ? ' · ' + ratio + '%' + bytes + speed : '');
  $('#btnCancel').textContent = downloadBusy() ? '暂停下载' : '取消';
}
async function pollModelDownload() {
  clearTimeout(state.downloadTimer);
  try {
    state.modelDownload = (await api('/api/models/download')).download;
    if (!state.jobId) showDownloadProgress();
    updateButtons();
    if (downloadBusy()) { state.downloadActive = true; state.downloadTimer = setTimeout(pollModelDownload, 700); }
    else if (state.modelDownload.status !== 'idle') {
      const catalog = await api('/api/models'); state.models = catalog.models || []; state.auxModels = catalog.auxiliary || [];
      state.gpuStatus = catalog.gpu || state.gpuStatus;
      const gpuInstall = state.modelDownload.model === 'gpu';
      if (state.modelDownload.status === 'done' && state.downloadActive) {
        if (gpuInstall) toast('GPU 环境已就绪，可在“设备”选择自动或 GPU', 'success', 7000);
        else if (state.models.some(m => m.name === state.modelDownload.model)) state.settings.model = state.modelDownload.model;
        else state.settings.auxModel = state.modelDownload.model;
        playSound('success');
      } else if (state.modelDownload.status === 'error' && gpuInstall) {
        toast(state.modelDownload.message, 'error', 9000);
      }
      state.downloadActive = false;
      renderModels();
    }
  } catch (error) { jobMsg.textContent = '下载状态查询失败，正在重试：' + error.message; state.downloadTimer = setTimeout(pollModelDownload, 1800); }
}
async function downloadModel(name) {
  if (editLocked()) return;
  try {
    state.modelDownload = (await api('/api/models/download', {model:name})).download;
    state.downloadActive = true;
    toggleModelMenu(false); toggleAuxMenu(false); showDownloadProgress(); updateButtons();
    $('#jobMsg').scrollIntoView({block:'nearest'}); await pollModelDownload();
  } catch(error) { toast(error.message,'error'); }
}
async function downloadGpu() {
  if (editLocked()) return;
  try {
    state.modelDownload = (await api('/api/runtime/gpu', {})).download;
    state.downloadActive = true;
    toggleModelMenu(false); toggleAuxMenu(false); showDownloadProgress(); updateButtons();
    $('#jobMsg').scrollIntoView({block:'nearest'}); await pollModelDownload();
  } catch(error) { toast(error.message,'error'); }
}

async function locateModel(name) {
  if (editLocked()) return;
  state.operation = '选择模型目录';
  toggleModelMenu(false); toggleAuxMenu(false); updateButtons();
  try {
    const picked = await api('/api/dialog', {kind:'dir', title:'选择模型所在文件夹'});
    if (!picked.path) return;
    await api('/api/models/locate', {model:name, path:picked.path});
    const catalog = await api('/api/models');
    state.models = catalog.models || [];
    state.auxModels = catalog.auxiliary || [];
    state.gpuStatus = catalog.gpu || state.gpuStatus;
    renderModels();
    toast('模型目录已关联', 'success');
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    state.operation = '';
    renderModels();
  }
}

function isMediaName(name) {
  return MEDIA_EXT_RE.test(String(name || ""));
}

function isScriptName(name) {
  return SCRIPT_EXT_RE.test(String(name || ""));
}

function uploadFile(file, kind, onProgress = null) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/upload?kind=${encodeURIComponent(kind)}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("X-Upload-Name", encodeURIComponent(file.name));
    xhr.setRequestHeader("X-SubtitleStudio-Token", SESSION_TOKEN);
    xhr.timeout = 30 * 60 * 1000;
    xhr.responseType = "json";
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      let data = xhr.response;
      if (!data) {
        try { data = JSON.parse(xhr.responseText || "{}"); } catch { data = {}; }
      }
      if (xhr.status >= 200 && xhr.status < 300 && data && data.ok !== false) {
        resolve(data);
      } else {
        reject(new Error(data?.error || `导入失败（HTTP ${xhr.status}）`));
      }
    };
    xhr.onerror = () => reject(new Error("无法连接本地服务"));
    xhr.ontimeout = () => reject(new Error("文件导入超时，请重试"));
    xhr.onabort = () => {
      const error = new Error("已取消");
      error.name = "AbortError";
      reject(error);
    };
    if (kind === "media") {
      state.uploadXhr = xhr;
    } else if (kind === "script") {
      state.scriptXhr = xhr;
    }
    xhr.send(file);
  });
}

async function pickMedia() {
  if (editLocked() || state.mediaPickerOpen) return;
  state.mediaPickerOpen = true;
  updateButtons();
  try {
    const d = await api("/api/dialog", { kind: "media", multiple: false });
    if (d.path) await loadMedia(d.path);
  } catch (e) {
    toast(e.message, "error");
  } finally {
    state.mediaPickerOpen = false;
    updateButtons();
  }
}

async function pickScript() {
  if (state.settings.mode !== "align") {
    toast("字幕提取模式不使用剧本；切换到「字幕对齐」后再导入", "error", 5000);
    return;
  }
  if (editLocked() || state.scriptUploading || state.scriptPickerOpen) return;
  state.scriptPickerOpen = true;
  updateButtons();
  try {
    const d = await api("/api/dialog", { kind: "script", multiple: true });
    for (const path of d.paths || (d.path ? [d.path] : [])) {
      const r = await api("/api/script/parse", { path });
      await applyScriptResult(r, path);
    }
  } catch (e) {
    toast(e.message, "error");
  } finally {
    state.scriptPickerOpen = false;
    updateButtons();
  }
}

function restoredScriptNames(project) {
  return Array.isArray(project?.script_names) && project.script_names.length
    ? [...project.script_names] : project?.script_lines?.length ? [project.script_name || '剧本'] : [];
}

async function applyScriptResult(result, sourceName = "") {
  if (!result.lines?.length) throw new Error('剧本没有可导入的内容');
  const name = sourceName.split(/[\\/]/).pop() || result.name || '剧本';
  const names = state.script.length ? (state.scriptNames?.length ? state.scriptNames : [state.scriptName || '剧本']) : [];
  const incoming = result.document?.text ?? result.lines.map(line => line.text).join('\n');
  const previous = state.scriptDocument?.text ?? state.script.map(line => line.text).join('\n');
  const sources = state.scriptDocument?.sources;
  const characters=sources?.length?[...previous]:null;
  const documents = state.script.length ? sources?.length ? sources.map(source=>({
    id:source.id,name:source.name,text:characters.slice(source.start,source.end).join('')}))
    : [{name:state.scriptName || '旧工程合并剧本',text:previous}] : [];
  // Unicode位置由后端计算；每份原文保留独立来源，不依赖文件导入顺序匹配。
  let sourceIndex=documents.length;
  while (documents.some(source=>source.id==='source-'+sourceIndex)) sourceIndex++;
  documents.push({id:'source-'+sourceIndex,name,text:incoming});
  result = await api('/api/script/parse', {documents});
  // 旧后台会忽略documents却返回200和空内容，不能把现有剧本/校验结果清掉。
  if (!Array.isArray(result.lines) || !result.lines.length
      || result.document?.text !== documents.map(source=>source.text).join('\n')
      || !Array.isArray(result.document?.sources) || result.document.sources.length !== documents.length) {
    throw new Error('后台剧本接口版本不一致，本次未导入。请保存工程，结束当前任务后重启字幕工作室再试（仅刷新页面无效）。');
  }
  state.script = result.lines;
  state.scriptDocument = result.document;
  state.scriptNames = [...names, name];
  state.referenceGaps = [];
  state.cues.forEach(cue => { cue.reference_match = null; cue.script_text = ''; cue.script_index = null; cue.match_ratio = null; cue.match_needs_review = false; });
  state.history = []; state.redoStack = []; state.editBefore = null;
  state.scriptName = name;
  state.scriptPath = "";
  renderAll();
  scheduleSave();
  const stats = result.stats || {};
  toast(`参考剧本已导入：${stats.total ?? state.script.length} 段，无需整理格式`, "success");
}

async function startScriptUpload(input) {
  const files = Array.isArray(input) ? input : [input];
  if (!files.length) return;
  if (state.settings.mode !== "align") {
    toast("字幕提取模式不使用剧本；切换到「字幕对齐」后再导入", "error", 5000);
    return;
  }
  if (editLocked() || state.scriptPickerOpen) {
    toast("任务处理中，暂时不能更换剧本", "error");
    return;
  }
  if (files.some(file => !isScriptName(file.name))) {
    toast("剧本仅支持 TXT / MD / DOCX；.doc 请先另存为 .docx", "error", 6000);
    return;
  }
  if (state.scriptUploading) {
    toast("正在读取上一个剧本，请稍候", "error");
    return;
  }
  abortScriptUpload();
  const epoch = ++state.scriptEpoch;
  state.scriptUploading = true;
  state.scriptUploadName = files[0].name;
  renderScript();
  updateButtons();
  try {
    for (const file of files) {
      state.scriptUploadName = file.name; renderScript();
      const uploaded = await uploadFile(file, "script");
      if (epoch !== state.scriptEpoch) return;
      const result = await api("/api/script/parse", { path: uploaded.path, remove_after: true });
      if (epoch !== state.scriptEpoch) return;
      await applyScriptResult(result, file.name);
      if (epoch !== state.scriptEpoch) return;
      state.scriptXhr = null;
    }
  } catch (error) {
    if (epoch === state.scriptEpoch && error.name !== "AbortError") {
      toast(error.message, "error", 7000);
    }
  } finally {
    if (epoch === state.scriptEpoch) {
      state.scriptUploading = false;
      state.scriptXhr = null;
      renderScript();
      updateButtons();
    }
  }
}

function abortScriptUpload() {
  state.scriptEpoch += 1;
  if (state.scriptXhr) {
    const xhr = state.scriptXhr;
    state.scriptXhr = null;
    try { xhr.abort(); } catch { /* ignore */ }
  }
  state.scriptUploading = false;
  state.scriptUploadName = "";
}

function clearScript() {
  if (editLocked()) return;
  if (!state.script.length && !state.scriptUploading) return;
  abortScriptUpload();
  state.script = [];
  state.scriptDocument = null; state.referenceGaps = [];
  state.cues.forEach(cue => { cue.reference_match = null; cue.script_text = ''; cue.script_index = null; cue.match_ratio = null; cue.match_needs_review = false; });
  state.scriptName = "";
  state.scriptNames = [];
  state.history = []; state.redoStack = []; state.editBefore = null;
  state.scriptPath = "";
  renderAll();
  scheduleSave();
  toast("已移除剧本");
}

async function startMediaUpload(file) {
  if (editLocked()) { toast('正在处理，请稍候或先取消导入', 'error'); return; }
  if (!isMediaName(file.name)) { toast('不支持的媒体格式', 'error'); return; }
  const epoch = ++state.uploadEpoch;
  state.uploading = true; state.uploadPercent = 0; state.uploadName = file.name;
  renderMediaMeta(); updateButtons();
  try {
    const uploaded = await uploadFile(file, 'media', ratio => {
      if (epoch !== state.uploadEpoch) return;
      state.uploadPercent = Math.round(ratio * 100);
      $('#mediaMeta').textContent = '正在导入 ' + state.uploadPercent + '%';
      $('#mediaInfo').textContent = file.name; progressBar.style.width = state.uploadPercent + '%';
    });
    if (epoch !== state.uploadEpoch) return;
    state.uploadXhr = null; state.uploading = false;
    const loaded = await loadMedia(uploaded.path, { uploaded: true });
    if (epoch !== state.uploadEpoch) return;
    if (loaded) toast('素材已导入，SRT将输出到工作副本旁', 'success');
  } catch (error) {
    if (epoch === state.uploadEpoch && error.name !== 'AbortError') toast(error.message, 'error');
  } finally {
    if (epoch === state.uploadEpoch) {
      state.uploadXhr = null; state.uploading = false; progressBar.style.width = '0%';
      renderMediaMeta(); updateButtons();
    }
  }
}

function bindDropzones() {
  const setup = (zone, options) => {
    zone.addEventListener("click", (event) => {
      if (event.target.closest(".drop-clear")) return;
      if (options.disabled()) return;
      options.onPick();
    });
    zone.addEventListener("keydown", (event) => {
      if (event.target.closest(".drop-clear")) return;
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      if (options.disabled()) return;
      options.onPick();
    });
    zone.addEventListener("dragover", (event) => {
      event.preventDefault();
      if (options.disabled()) {
        event.dataTransfer.dropEffect = "none";
        return;
      }
      event.dataTransfer.dropEffect = "copy";
      zone.classList.add("over");
    });
    zone.addEventListener("dragleave", (event) => {
      if (!zone.contains(event.relatedTarget)) zone.classList.remove("over");
    });
    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      zone.classList.remove("over");
      if (options.disabled()) return;
      const files = Array.from(event.dataTransfer?.files || []);
      if (!files.length) return;
      options.onFile(options.multiple ? files : files[0]);
    });
  };

  setup($("#mediaZone"), {
    disabled: () => !!state.jobId || state.uploading || state.mediaPickerOpen,
    onPick: pickMedia,
    onFile: startMediaUpload,
  });
  setup($("#scriptZone"), {
    disabled: () => state.settings.mode !== "align" || !!state.jobId
      || state.scriptUploading || state.scriptPickerOpen,
    onPick: pickScript,
    onFile: startScriptUpload,
    multiple: true,
  });

  $("#mediaClear").addEventListener("click", (event) => {
    event.stopPropagation();
    clearMedia();
  });
  $("#scriptClear").addEventListener("click", (event) => {
    event.stopPropagation();
    clearScript();
  });
  const subtitleZone = $('#subtitleSource');
  subtitleZone.addEventListener('dragover', event => {
    event.preventDefault();
    if (state.settings.mode === 'align' && !editLocked()) subtitleZone.classList.add('over');
  });
  subtitleZone.addEventListener('dragleave', event => { if (!subtitleZone.contains(event.relatedTarget)) subtitleZone.classList.remove('over'); });
  subtitleZone.addEventListener('drop', event => {
    event.preventDefault(); subtitleZone.classList.remove('over');
    if (editLocked()) return;
    if (state.settings.mode !== 'align') { toast('此处显示提取生成物，请切换字幕对齐后拖入 SRT'); return; }
    const file = event.dataTransfer?.files?.[0];
    if (!file || !/\.srt$/i.test(file.name) || file.size > 10 * 1024**2) { toast('请拖入 10 MB 以内的 SRT 文件', 'error'); return; }
    importSrt(file);
  });
  subtitleZone.addEventListener('click', event => {
    if (event.target.closest('button') || state.settings.mode !== 'align') return;
    importSrt();
  });

  const swallow = (event) => {
    const insideZone = !!event.target.closest?.(".dropzone, #subtitleSource");
    event.preventDefault();
    if (event.type === "drop" && !insideZone) toast("请把文件拖到左侧的素材框内", "error");
  };
  window.addEventListener("dragover", swallow);
  window.addEventListener("drop", swallow);
}

async function clearMedia() {
  if (state.jobId || state.submitting || state.operation || state.loading || state.scriptUploading || state.scriptPickerOpen) return;
  if (!state.media && !state.uploading) return;
  if (projectPath() && !(await saveProject(false))) { toast('保存失败，已保留当前素材', 'error'); return; }
  state.uploadEpoch++; state.mediaEpoch++; state.asrEpoch++;
  const xhr = state.uploadXhr; state.uploadXhr = null;
  if (xhr) { try { xhr.abort(); } catch {} }
  clearTimeout(state.saveTimer); abortScriptUpload();
  state.uploading = false; state.uploadName = ''; state.uploadPercent = 0;
  state.media = null; state.scriptDocument = null; state.referenceGaps = []; state.srtPath = ''; state.subtitleFile = null; state.cues = []; state.initialCues=null; state.selectedCues=new Set(); state.script = []; state.scriptName = ''; state.scriptNames = [];
  state.waveSpan = 0; state.waveStart = 0; state.auditionEnd = null; clearInterval(state.auditionTimer);
  resetWaveDetail();
  state.selected = -1; state.history = []; state.redoStack = []; state.editBefore = null;
  state.dirty = false; state.editRevision++; state.waveform = null; state.pendingResult = null;
  video.pause(); video.removeAttribute('src'); video.load(); overlay.textContent = '';
  $('#videoEmpty').hidden = false; setSaveState('');
  progressBar.style.width = '0%'; jobMsg.textContent = '等待任务';
  renderAll(); updateButtons();
  toast('已移除素材，已存工程仍保留');
}

function confirmDialog(message) {
  return new Promise(resolve => {
    const dlg = $('#confirmDialog');
    $('#confirmMessage').textContent = message;
    let result = false;
    const cleanup = () => { $('#btnConfirmOk').onclick = null; $('#btnConfirmCancel').onclick = null; dlg.onclose = null; resolve(result); };
    $('#btnConfirmOk').onclick = () => { result = true; dlg.close(); };
    $('#btnConfirmCancel').onclick = () => { result = false; dlg.close(); };
    dlg.onclose = cleanup;
    dlg.showModal();
  });
}

let _termTimer = null, _termOffset = 0, _termSession = 0, _termTick = 0, _termErrors = 0;
const TERM_LINE_RE = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+([\s\S]*)$/;

function termLineNode(line) {
  const row = document.createElement('div');
  const match = TERM_LINE_RE.exec(line);
  if (!match) { row.textContent = line; return row; }
  const [, timestamp, level, message] = match;
  const time = document.createElement('span'); time.className = 'term-ts'; time.textContent = timestamp;
  const tag = document.createElement('span'); tag.className = 'term-lv term-' + level.toLowerCase();
  tag.textContent = level.padEnd(8);
  const text = document.createElement('span'); text.textContent = message;
  row.append(time, ' ', tag, ' ', text);
  if (level === 'ERROR' || level === 'CRITICAL') row.classList.add('term-line-error');
  else if (level === 'WARNING') row.classList.add('term-line-warning');
  return row;
}

function formatUptime(seconds) {
  seconds = Math.max(0, Math.floor(Number(seconds) || 0));
  const pad = v => String(v).padStart(2, '0');
  return `${pad(Math.floor(seconds / 3600))}:${pad(Math.floor(seconds % 3600 / 60))}:${pad(seconds % 60)}`;
}

function renderTerminalStatus(health) {
  const bar = $('#terminalStatus'), text = $('#terminalStatusText');
  if (!bar || !text) return;
  if (!health) { bar.classList.add('bad'); text.textContent = '服务未连接，正在重试…'; return; }
  bar.classList.remove('bad');
  const models = (health.models || []).filter(m => m.available).length;
  const parts = ['运行中', `v${health.version}`, `Python ${health.python}`];
  if (health.port) parts.push(`端口 ${health.port}`);
  if (health.uptime != null) parts.push(`已运行 ${formatUptime(health.uptime)}`);
  parts.push(`模型 ${models} 个`);
  if (_termErrors) parts.push(`错误 ${_termErrors} 条`);
  text.textContent = parts.join(' · ');
  bar.title = health.log_file ? `日志文件：${health.log_file}` : '';
}

function openTerminal() {
  if(_termTimer){clearTimeout(_termTimer);_termTimer=null;}
  const session=++_termSession;
  const dlg = $('#terminalDialog');
  const pre = $('#terminalLog');
  const placeholder = '正在读取运行日志...';
  pre.textContent = placeholder;
  _termOffset = 0; _termTick = 0; _termErrors = 0;
  renderTerminalStatus(null);
  $('#terminalStatusText').textContent = '正在连接服务…';
  const stop = () => { if(session===_termSession)_termSession++;if (_termTimer) { clearTimeout(_termTimer); _termTimer = null; } };
  $('#btnTermClear').onclick = () => { pre.textContent = ''; _termErrors = 0; };
  const tick = async () => {
    if(session!==_termSession || !dlg.open)return;
    try {
      const d = await api(`/api/logs?since=${_termOffset}&lines=800`);
      if(session!==_termSession || !dlg.open)return;
      if (d.ok) {
        if (d.lines.length) {
          if (pre.textContent === placeholder) pre.textContent = '';
          const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 32;
          const frag = document.createDocumentFragment();
          for (const line of d.lines) {
            if (/\s(ERROR|CRITICAL)\s/.test(line)) _termErrors++;
            frag.append(termLineNode(line));
          }
          pre.append(frag);
          while (pre.childElementCount > 6000) pre.firstElementChild.remove();
          if (atBottom) pre.scrollTop = pre.scrollHeight;
        }
        _termOffset = d.offset;
      }
      if (_termTick++ % 5 === 0) {
        try {
          const h = await api('/api/health');
          if(session!==_termSession || !dlg.open)return;
          renderTerminalStatus(h.ok ? h : null);
        } catch (e) { renderTerminalStatus(null); }
      }
    } catch (e) { if(session===_termSession) renderTerminalStatus(null); }
    if(session===_termSession && dlg.open)_termTimer = setTimeout(tick, 1000);
  };
  dlg.onclose = stop;
  dlg.showModal();
  tick();
}

async function loadMedia(path, options = {}) {
  if (state.jobId || state.submitting || state.operation || state.loading || state.scriptUploading || state.scriptPickerOpen) return false;
  const epoch = ++state.mediaEpoch;
  state.loading = true; updateButtons();
  const staged = !state.media && (state.srtPath || state.script.length)
    ? { cues: state.cues, subtitleFile: state.subtitleFile, script: state.script, scriptName: state.scriptName, scriptNames: state.scriptNames, scriptDocument: state.scriptDocument } : null;
  try {
    if (projectPath() && !(await saveProject(false))) throw new Error('当前工程保存失败，已保留当前素材');
    if (epoch !== state.mediaEpoch) return false;
    const [infoResult, projectResult] = await Promise.all([
      api('/api/media/info?path=' + encodeURIComponent(path)),
      api('/api/projects/load?path=' + encodeURIComponent(path)),
    ]);
    if (epoch !== state.mediaEpoch) return false;
    const info = infoResult.info;
    if (!info || info.error) throw new Error(info?.error || '媒体信息不可用');
    const savedProject = projectResult.project;
    const project = savedProject && (options.restore || await confirmDialog('此视频有之前的工程记录。是否恢复之前的参数设置、配置和生成的字幕？\n选择“取消”将从空白字幕开始，原记录在保存前保留。')) ? savedProject : null;
    abortScriptUpload();
    state.media = { ...info, uploaded: !!options.uploaded }; state.srtPath = '';
    state.subtitleFile = typeof project?.subtitle_file?.path === 'string' && project.subtitle_file.path ? project.subtitle_file : null;
    state.cues = (project?.cues || []).map(normalizeCue);
    state.initialCues = project?.initial_cues?.map(normalizeCue) || null;
    state.selectedCues = new Set();
    state.script = project?.script_lines || []; state.scriptName = project?.script_name || ''; state.scriptPath = '';
    state.scriptNames = restoredScriptNames(project);
    state.scriptDocument = project?.script_document || null; state.referenceGaps = [];
    if (!savedProject && staged) {
      state.cues = staged.cues; state.subtitleFile = staged.subtitleFile;
      state.script = staged.script; state.scriptName = staged.scriptName;
      state.scriptNames = staged.scriptNames;
      state.scriptDocument = staged.scriptDocument;
      state.initialCues=JSON.parse(JSON.stringify(staged.cues));
    }
    projectVersions.set(info.path, projectResult.revision ?? savedProject?.revision ?? 0);
    state.history = []; state.redoStack = []; state.editBefore = null; state.selected = -1;
    state.pendingResult = null; state.waveform = null; state.videoErrorShown = false;
    state.waveSpan = 0; state.waveStart = 0; state.auditionEnd = null; clearInterval(state.auditionTimer);
    resetWaveDetail();
    state.dirty = false; state.editRevision++;
    if (project?.settings) applyProjectSettings(project.settings);
    if (options.settings) applyProjectSettings(options.settings);
    if (info.duration > 30) state.waveSpan = 30;
    state.playingIndex = -1;
    video.src = '/api/media/stream?path=' + encodeURIComponent(info.path) + '&token=' + encodeURIComponent(SESSION_TOKEN);
    applyPreviewShape(); $('#videoEmpty').hidden = true;
    progressBar.style.width = '0%'; progressBar.style.background = 'var(--accent)'; jobMsg.textContent = '等待任务';
    setSaveState(project ? '已恢复工程' : '');
    renderAll(); loadWaveform();
    if (!project && staged) scheduleSave();
    if (project && state.cues.length) {
      const flagged = state.cues.filter(c => c.hallucination && !c.hallucination.dismissed && c.exclude_export).length;
      toast('已恢复上次编辑：' + state.cues.length + ' 条' + (flagged ? `（${flagged} 条疑似幻觉已默认不导出）` : ''), 'success', flagged ? 9000 : 3500);
    }
    return true;
  } catch (error) {
    if (epoch === state.mediaEpoch) toast(error.message, 'error');
    return false;
  } finally {
    if (epoch === state.mediaEpoch) { state.loading = false; updateButtons(); }
  }
}

function applyPreviewShape() {
  const wrap = $("#videoWrap");
  if (!wrap) return;
  const w = state.media?.width || video.videoWidth || 0;
  const h = state.media?.height || video.videoHeight || 0;
  if (!w || !h) {
    wrap.classList.remove("portrait");
    return;
  }
  wrap.classList.toggle("portrait", h > w);
  wrap.style.setProperty("--video-ratio", `${w} / ${h}`);
}

function renderMediaMeta() {
  const zone = $("#mediaZone");
  const nameEl = $("#mediaMeta");
  const infoEl = $("#mediaInfo");
  const clear = $("#mediaClear");
  if (state.uploading) {
    zone.classList.add("filled");
    clear.hidden = false;
    nameEl.textContent = `正在导入 ${state.uploadPercent}%`;
    nameEl.title = "";
    infoEl.textContent = state.uploadName;
    return;
  }
  const m = state.media;
  if (!m) {
    zone.classList.remove("filled");
    clear.hidden = true;
    nameEl.textContent = "拖入或点击选择";
    nameEl.title = "";
    infoEl.textContent = "";
    return;
  }
  zone.classList.add("filled");
  clear.hidden = false;
  nameEl.textContent = m.name;
  nameEl.title = m.path;
  const bits = [];
  bits.push(m.duration ? fmtTime(m.duration) : "未知时长");
  if (m.width && m.height) bits.push(`${m.width}×${m.height}`);
  if (m.size) bits.push(`${(m.size / 1048576).toFixed(1)} MB`);
  if (m.uploaded) bits.push("拖入副本");
  infoEl.textContent = bits.join(" · ");
}

async function loadWaveform() {
  const path = state.media?.path, epoch = state.mediaEpoch;
  state.waveform = null; drawWave();
  if (!path) return;
  try {
    const result = await api('/api/waveform?path=' + encodeURIComponent(path) + '&buckets=4000', null, null, { timeout: 120000 });
    if (state.media?.path !== path || state.mediaEpoch !== epoch) return;
    state.waveform = result;
    if (result.error) toast('波形不可用：' + result.error, 'error');
    drawWave();
    if (!result.error) requestWaveDetail();  // 提前加载当前窗口的局部波形，点选字幕时不再临时切换。
  } catch (error) {
    if (state.media?.path === path && state.mediaEpoch === epoch) toast('波形加载失败：' + error.message, 'error');
  }
}

function normalizeCue(c, i) {
  return {
    ...c,
    original_text: c.original_text ?? String(c.text ?? ''),
    original_start: c.original_start ?? c.start,
    original_end: c.original_end ?? c.end,
    asr_original: c.asr_original || {text: String(c.text ?? ''), start:c.start, end:c.end, id:c.id ?? i + 1},
    id: c.id ?? i + 1,
    display_id: String(c.display_id ?? i + 1),
    start: Number(c.start) || 0,
    end: Number(c.end) > Number(c.start) ? Number(c.end) : (Number(c.start) || 0) + 0.001,
    text: simplifiedText(c.text).trim(),
    role: c.role || "",
    script_text: c.script_text || "",
    script_index: c.script_index ?? null,
    match_ratio: c.match_ratio ?? null,
    confidence: c.confidence ?? null,
    avg_logprob: c.avg_logprob,
    no_speech_prob: c.no_speech_prob,
    is_noise: !!c.is_noise,
    needs_review: !!c.needs_review,
    match_needs_review: !!c.match_needs_review,
    edited: !!c.edited,
    estimated: !!c.estimated,
    source: c.source || "",
    words: Array.isArray(c.words) ? c.words.map(w=>({...w,word:simplifiedText(w.word)})) : [],
  };
}

function snapshot() { return JSON.stringify({ cues: state.cues, subtitleFile: state.subtitleFile }); }

function restoreSnapshot(value) {
  const saved = JSON.parse(value);
  state.cues = saved.cues;
  state.subtitleFile = saved.subtitleFile;
  state.selectedCues = new Set();
}

function pushHistory(before) {
  if (!before || before === snapshot()) return;
  state.history.push(before);
  if (state.history.length > HISTORY_LIMIT) state.history.shift();
  state.redoStack = [];
  updateButtons();
}

function undo() {
  if (editLocked()) return;
  if (!state.history.length) return;
  state.redoStack.push(snapshot());
  restoreSnapshot(state.history.pop());
  state.selected = -1;
  renderAll();
  scheduleSave();
}

function redo() {
  if (editLocked()) return;
  if (!state.redoStack.length) return;
  state.history.push(snapshot());
  restoreSnapshot(state.redoStack.pop());
  state.selected = -1;
  renderAll();
  scheduleSave();
}

function hasCompletedModification(cue) {
  if(!cue.edited || !String(cue.text || '').trim() || cue.modification_complete===false)return false;
  const done=cue.modification_complete;
  // “文字已确认”确认的是内容，不是时间：对齐（或手动改时间）不应当作废内容确认。
  return !done || done.text===cue.text;
}

function completeModification(cue) {
  cue.edited=true;cue.modification_complete={text:cue.text,start:cue.start,end:cue.end};
}

// 一次表格渲染内，同一条字幕的状态会被筛选、统计、徽标和详情栏反复询问；
// 渲染期间用 Map 复用结果，渲染结束即释放，不跨编辑保存过期状态。
let verificationCache = null;

// 标签分三关：通过标签属于最后一次执行的检查，问题标签三关通用。
function verificationStageName(stage) {
  return stage==='verify' ? '校验' : stage==='calibrate' ? '校准' : stage==='align' ? '对齐' : '';
}

function verificationStage(cue) {
  return verificationStageName(cue?.verification_stage) ? cue.verification_stage : '';
}

function cueTimingIssues(cue) {
  const reasons=[];
  let obvious=false;
  if(!Number.isFinite(cue.start)||!Number.isFinite(cue.end)||cue.start<0||cue.end<=cue.start){
    reasons.push('时间轴无效');obvious=true;
  }
  if(cue.alignment_error||cue.word_timing_error){reasons.push(cue.alignment_error||cue.word_timing_error);obvious=true;}
  if(cue.duration_trim){
    const unresolved=cue.duration_trim.method==='unresolved';
    reasons.push(unresolved?'对齐时长异常，未能自动定位，请试听复核':'时间经过自动收紧或回退，请试听复核');
    obvious ||= unresolved;
  }
  const length=(String(cue.text||'').match(/[0-9A-Za-z㐀-鿿]/g)||[]).length;
  // 与 backend/core/duration.py 的警戒值一致；只提示，不硬裁字幕。
  const cap=Math.min(12,Math.max(1.2,length*.55+.9));
  if(length&&cue.end-cue.start>cap+1e-6){reasons.push('字幕时间异常偏长，请试听复核');obvious=true;}
  if(cue.alignment?.stale&&!cue.alignment_error)reasons.push('对齐记录已过期，请重新对齐');
  return {reasons,obvious};
}

function cueVerificationIssues(cue, {requireReference=false, stage=null}={}) {
  const timing=cueTimingIssues(cue),reasons=[...timing.reasons];
  let obvious=timing.obvious;
  const phase=stage ?? verificationStage(cue);
  const contentPhase=phase==='verify'||phase==='calibrate';
  if(cue.interjection){
    if(cue.review_decision==='included')reasons.push('整条为笑声、叹气或填充音；已手动开启导出，校准不会自动关闭');
    else if(cue.review_decision==='non_script')reasons.push('整条为笑声、叹气或填充音；已手动关闭导出');
    else reasons.push(state.settings.keepInterjections
      ? '整条为笑声、叹气或填充音；已开启“语气词”，校准后正常导出'
      : '整条为笑声、叹气或填充音；关闭“语气词”，校准会直接删除此条');
    return {reasons,obvious:true};
  }
  // 内容检查（校验、校准）：只看参考文字。
  const match=cue.reference_match;
  if(hasCompletedModification(cue)){
    // 人工确认仅覆盖内容，时间和版式问题继续检查。
  }
  else if(match?.stale || (match?.query_text!=null && match.query_text!==cue.text))reasons.push('参考已过期，可使用重新校验更新');
  else if(match && match.status!=='high'){
    reasons.push(match.warning || (match.status==='unmatched'?'无参考内容':match.label) || '参考内容未确认');
    // 参考里根本不存在的内容按明显问题处理：多出参考、上下文不符、有参考却完全未匹配。
    if(match.status==='extra'||match.status==='context_mismatch'
      ||(match.status==='unmatched'&&requireReference))obvious=true;
  }
  else if(!match && requireReference && contentPhase)reasons.push('无参考内容');
  const text=String(cue.text || '');
  const characters=[...text.replace(/[^\p{L}\p{N}]/gu,'')].length;
  if(!characters){reasons.push('字幕文字为空');obvious=true;}
  // 版式检查（校准、对齐）：每行字数与残留碎片。
  if(phase==='calibrate'||phase==='align'){
    const limit=Math.max(1,Number(state.settings.width)||20);
    const overWidth=text.split(/\r?\n/).some(line=>[...line].length>limit);
    if(overWidth)reasons.push(`超过每行 ${limit} 字，可用校准或对齐分段`);
    if(cue.merge_suggestion){reasons.push('疑似拆出的短碎片，建议合并');obvious=true;}
  }
  if(cue.hallucination && !cue.hallucination.dismissed){
    reasons.push('幻觉特征仍需试听确认');if(cue.hallucination.confirmed!==false)obvious=true;
  }
  return {reasons,obvious};
}

function computeCueVerification(cue) {
  const issues=cueVerificationIssues(cue,{requireReference:state.script.length>0});
  if(cue.interjection&&!cueTimingIssues(cue).reasons.length)return {status:'suspect',obvious:true,label:'语气词',reason:issues.reasons.join('；')};
  if(issues.reasons.length)
    return {status:'suspect',obvious:issues.obvious,label:issues.obvious?'明显问题':'疑似问题',reason:issues.reasons.join('；')};
  if(hasCompletedModification(cue))return {status:'modified',label:'文字已确认',reason:'仅确认当前文字；时间问题仍会单独显示，原始识别记录保留'};
  const stage=verificationStage(cue);
  if(!stage)return null;
  const label=stage==='verify'?'参考一致':stage==='calibrate'?'已校准':'自动对齐完成';
  return {status:'passed',label,reason:'自动检查未发现已知问题，不代表已人工确认文字和音轨'};
}

function cueReportVerdict(cue, options={}) {
  if(cue.interjection)return 'obvious';
  // 建议合并是版式提示，与徽标一致：只在校准、对齐关计入明显问题。
  const phase=options.stage ?? verificationStage(cue);
  if(cue.merge_suggestion&&(phase==='calibrate'||phase==='align'))return 'obvious';
  const issues=cueVerificationIssues(cue,options);
  if(issues.reasons.length)return issues.obvious?'obvious':'suspect';
  if(hasCompletedModification(cue))return 'passed';
  return verificationStage(cue)?'passed':'unchecked';
}

function verificationStatistics(cues, options={}) {
  const stats={total:cues.length,passed:0,suspect:0,obvious:0,unchecked:0};
  for(const cue of cues)stats[cueReportVerdict(cue,options)]++;
  return stats;
}

function cueReportKey(cue) {
  return `${String(cue.text??'').trim()}\u0000${Math.round((Number(cue.start)||0)*1000)}\u0000${Math.round((Number(cue.end)||0)*1000)}`;
}

function countChangedCues(before, after) {
  const remaining=new Map();
  for(const cue of after){const key=cueReportKey(cue);remaining.set(key,(remaining.get(key)||0)+1);}
  let changed=0;
  for(const cue of before){
    const key=cueReportKey(cue), count=remaining.get(key)||0;
    if(count)remaining.set(key,count-1);else changed++;
  }
  return changed;
}

function showVerificationReport(operation, before, after, options={}) {
  const beforeStats=verificationStatistics(before,options);
  const afterStats=verificationStatistics(after,options);
  const changed=countChangedCues(before,after);
  const meta=`${operation}前 ${before.length} 条 → ${operation}后 ${after.length} 条 · 改动 ${changed} 条`;
  const set=(id,value)=>{const node=$(id);if(node)node.textContent=String(value);};
  set('#reportTitle',`${operation}报告`);
  set('#reportMeta',meta);
  set('#reportBeforePassed',beforeStats.passed);
  set('#reportBeforeSuspect',beforeStats.suspect);
  set('#reportBeforeObvious',beforeStats.obvious);
  set('#reportAfterChanged',changed);
  set('#reportAfterPassed',afterStats.passed);
  set('#reportAfterSuspect',afterStats.suspect);
  set('#reportAfterObvious',afterStats.obvious);
  const unchecked=afterStats.unchecked?` · 未校验 ${afterStats.unchecked} 条`:'';
  set('#reportNote',`处理后：自动检查未发现异常 ${afterStats.passed} 条 · 疑似 ${afterStats.suspect} 条 · 明显问题 ${afterStats.obvious} 条${unchecked}。自动完成不代表人工确认；文字确认不会覆盖时间问题。标签不自动关闭导出。改动按处理前的字幕逐条核对，拆分或改字都会计入。`);
  const dialog=$('#verificationReportDialog');
  if(dialog&&typeof dialog.showModal==='function'){
    try{if(!dialog.open)dialog.showModal();}catch(error){}
  }
  return {before:beforeStats,after:afterStats,changed};
}

function cueVerification(cue) {
  if (!verificationCache) return computeCueVerification(cue);
  if (verificationCache.has(cue)) return verificationCache.get(cue);
  const value = computeCueVerification(cue);
  verificationCache.set(cue, value);
  return value;
}

function cueNeedsReview(cue) {
  if(cue.merge_suggestion)return true;
  const check=cueVerification(cue);
  if(check)return !['passed','modified'].includes(check.status);
  return !!(cue.alignment_error || cue.word_timing_error || cue.is_noise || cue.needs_review || cue.match_needs_review
    || (cue.confidence != null && cue.confidence < 0.6));
}

function cueStatistics(cues) {
  let aligned = 0, suspect = 0;
  for (const cue of cues) {
    if (cue.alignment && !cue.alignment.stale && !cueTimingIssues(cue).reasons.length) aligned++;
    if (cueNeedsReview(cue)) suspect++;
  }
  return { total: cues.length, aligned, suspect };
}

function cueExcluded(cue) { return !!cue.exclude_export || cue.review_decision === 'non_script'; }

// 标签只做标注，不再自动关闭导出。旧版本留下的自动排除在再次执行三关时解除；
// 手动“不导出”、幻觉默认排除和语气词规则保持原样。
function releaseVerificationExclusions(cues, indices=null) {
  const scope=indices?new Set(indices):null;
  let restored=0;
  cues.forEach((cue,i)=>{
    if(scope&&!scope.has(i))return;
    if(!cue.verification_excluded)return;
    delete cue.verification_excluded;
    const hallucinated=cue.hallucination&&!cue.hallucination.dismissed;
    if(cue.exclude_export&&!hallucinated&&cue.review_decision!=='non_script'){
      cue.exclude_export=false;
      restored++;
    }
  });
  return restored;
}

function cueReviewHtml(cue, i) {
  const m = cue.reference_match;
  const score = n => Math.round((n || 0)*100)+'%';
  const merge = cue.merge_suggestion
    ? '<p class="merge-hint" data-tip="勾选本条与相邻字幕后点击“合并”，文字按前后顺序拼接，时间取第一条开始到最后一条结束。">建议合并：' +
      escapeHtml(cue.merge_suggestion.reason || '疑似拆出的短碎片') + '</p>'
    : '';
  let details = '';
  if (m) {
    if(hasCompletedModification(cue))return merge + '<details class="match-alternatives"><summary>文字已确认 · 历史参考'+(m.reference?.source_name?' · '+escapeHtml(m.reference.source_name):'')+'</summary><p class="match-metrics">当前修改已保留，原始识别记录不变。</p></details>';
    details = '<details class="match-alternatives"><summary>' + escapeHtml(m.stale ? '参考待复核' : m.label || '匹配详情') + ' · ' + score(m.score) + (m.reference?.source_name?' · '+escapeHtml(m.reference.source_name):'') + '</summary>' +
      '<p class="match-metrics">字符 ' + score(m.character_score) + ' · 拼音 ' + score(m.pinyin_score) + '</p>' +
      (m.warning?'<p class="match-context">'+escapeHtml(m.warning)+'</p>':'') +
      (m.status === 'ambiguous' ? '<button type="button" class="mini" data-review="confirm" data-tip="确认当前参考位置，再手动采用文字。">确认当前位置</button>' : '') +
      (m.alternatives || []).map((c,j) => '<div class="match-candidate"><p>'+escapeHtml(subtitleText(c.text))+'</p><small>'+escapeHtml(c.source_name || '')+' · 第 '+c.paragraph_start+' 段 · '+score(c.score)+'</small>'+(m.status==='ambiguous' && !c.group_only && !c.reference_only ? '<button type="button" class="mini" data-review="candidate" data-candidate="'+j+'">选择此位置</button>' : '')+'</div>').join('')+'</details>';
  }
  return merge + details;
}

let searchCursor = -1;

function filteredRows() {
  const q = state.search.trim().toLowerCase().replace(/^#/, "");
  return state.cues
    .map((cue, i) => ({ cue, i }))
    .filter(({ cue, i }) => {
      const status=cueVerification(cue)?.status;
      if (state.filter === "low" && ['passed','modified'].includes(status) && !cue.merge_suggestion) return false;
      if (state.filter === "noise" && (!cue.is_noise || ['passed','modified'].includes(status))) return false;
      if (state.filter === "edited" && !hasCompletedModification(cue)) return false;
      if (q) {
        const hay = `${cue.display_id ?? i + 1} ${i + 1} ${cue.text || ""} ${cue.script_text || ""} ${cue.role || ""}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
}

/* 像浏览器 Ctrl+F 一样：回车向下找下一个匹配，Shift+回车向上，走到末尾回到第一条。 */
function findCueMatch(backwards = false) {
  const rows = filteredRows();
  if (!rows.length) { toast('没有匹配项', 'info'); return false; }
  const target = backwards
    ? [...rows].reverse().find(({ i }) => i < searchCursor) || rows[rows.length - 1]
    : rows.find(({ i }) => i > searchCursor) || rows[0];
  searchCursor = target.i;
  selectCue(target.i);
  return true;
}

function suggestHtml(cue, i) {
  const body = cue.script_text
    ? `<div class="sugg-text">${escapeHtml(subtitleText(cue.script_text))}</div>`
    : '<div class="sugg-text muted">'+(cue.reference_match?.status==='context_mismatch'?'当前片段无此台词':cue.reference_match?'无参考内容':'尚无参考')+'</div>';
  return `<div class="field-box">${body}</div>`;
}

function badgesHtml(cue) {
  const out = [];
  const verification=cueVerification(cue);
  if (cue.role) out.push(`<span class="badge">${escapeHtml(cue.role)}</span>`);
  if (cue.confidence != null) {
    const pct = Math.round(cue.confidence * 100);
    out.push(`<span class="badge ${pct < 45 ? "low" : ""}">置信 ${pct}%</span>`);
  }
  if(verification) {
    out.push(`<span class="badge ${verification.status==='modified'?'modified-done':verification.status==='passed'?'verified':verification.obvious?'noise':'low'}" data-tip="${escapeHtml(verification.reason)}">${verification.label}</span>`);
  } else if(cueNeedsReview(cue))out.push('<span class="badge low">疑似问题</span>');
  if(hasCompletedModification(cue)&&verification?.status!=='modified')out.push('<span class="badge modified-done" data-tip="文字已确认，时间和版式问题仍需复核">文字已确认</span>');
  if (cue.merge_suggestion) out.push(`<span class="badge noise merge-suggest" data-tip="${escapeHtml(cue.merge_suggestion.reason || '疑似拆出的短碎片')}；勾选相邻字幕并点击“合并”。">建议合并</span>`);
  const widthSplit=cue.width_split===true || (cue.width_split==null && cue.source==='split' && cue.segmentation==='dynamic_programming_word_pause');
  if (widthSplit && ['split','gap_split'].includes(cue.source)) out.push('<span class="badge split">已分段</span>');
  if (cue.estimated && verification?.status!=='modified') out.push('<span class="badge low">时间估算</span>');
  return out.join("") || '<span class="badge">—</span>';
}

function renderTable() {
  const previousVerificationCache = verificationCache;
  verificationCache = new Map();
  try {
    renderTableBody();
  } finally {
    verificationCache = previousVerificationCache;
  }
}

function renderTableBody() {
  state.presentationCache=null;
  state.rawPresentationCache=null;
  const rows = filteredRows();
  cueBody.innerHTML = rows.map(({ cue, i }) => {
    const verification=cueVerification(cue),passed=['passed','modified'].includes(verification?.status);
    const textClass = `${passed ? 'passed' : ''} ${cue.merge_suggestion?'context-warning':''} ${passed ? '' : `${cue.needs_review || (cue.confidence != null && cue.confidence < 0.45) ? "low" : ""} ${cue.is_noise ? "noise" : ""} ${verification?.obvious?'context-warning':''}`}`;
    return `<tr data-i="${i}" class="script-line ${cueExcluded(cue) ? 'excluded' : ''} ${state.selectedCues.has(i) ? "selected" : ""} ${i === state.playingIndex ? 'playing' : ''}">
      <td class="idx"><input type="checkbox" class="cue-pick" ${state.selectedCues.has(i)?'checked':''} aria-label="选择字幕 ${escapeHtml(cue.display_id)}"><b>${escapeHtml(cue.display_id)}</b><span class="cue-time-range"><input class="time" data-field="start" value="${fmtCueTime(cue.start)}" aria-label="第${i+1}条开始时间"><span>→</span><input class="time" data-field="end" value="${fmtCueTime(cue.end)}" aria-label="第${i+1}条结束时间"></span><span class="badges">${badgesHtml(cue)}</span></td>
      <td class="cue-current"><div class="field-box"><div class="cue-text ${textClass}" aria-label="第${i+1}条字幕" contenteditable="true" spellcheck="false">${escapeHtml(editableCueText(cue.text))}</div></div></td>
      <td class="suggest" aria-label="第${i+1}条参考">${suggestHtml(cue, i)}</td>
      <td class="cue-review">${cueReviewHtml(cue,i)}</td>
    </tr>`;
  }).join("");
  emptyHint.style.display = state.cues.length ? "none" : "";
  emptyHint.textContent = state.cues.length ? "" : "选择视频 → 提取字幕，或导入 SRT";
  const stats = cueStatistics(state.cues);
  $("#cueCount").textContent = `共 ${stats.total} 条 · 已对齐 ${stats.aligned} 条 · 疑似 ${stats.suspect} 条${rows.length !== stats.total ? `（显示 ${rows.length}）` : ""}`;
  updateWidthInfo();
  if (state.selected >= 0 && state.cues[state.selected]) {
    const c = state.cues[state.selected];
    $("#selInfo").textContent = `第 ${state.selected + 1} 条 · ${fmtTime(c.start)} → ${fmtTime(c.end)}`;
  } else {
    $("#selInfo").textContent = "";
  }
  updateButtons();
  drawWave();
}

function renderSubtitleSource(animate = false) {
  const file = state.subtitleFile;
  const source = $('#subtitleSource');
  const name = $('#subtitleFileName');
  const note = $('#subtitleFileNote');
  const ready = state.cues.length > 0;
  source.classList.toggle('filled', ready);
  name.textContent = file?.path ? file.path.split(/[\\/]/).pop()
    : ready ? '当前工程字幕' : state.settings.mode === 'align' ? '请选择用于对齐的 SRT' : '尚未生成字幕';
  name.title = file?.path || '';
  note.hidden = !ready;
  note.textContent = ready ? `${file?.origin === 'generated' ? '提取生成' : file?.origin === 'imported' ? '手动导入' : '工程恢复'} · ${state.cues.length} 条${state.settings.mode === 'align' ? ' · 已载入对齐字幕' : ''}` : '';
  name.dataset.tip = state.settings.mode === 'align'
    ? '直接对齐需自行导入 SRT；提取后切换会自动带入当前字幕，并保留校对结果。可用“更换 SRT”选择其他字幕文件。'
    : '提取完成后自动生成 SRT，切换字幕对齐可直接使用。也可导入已有 SRT，继续校对和导出。';
  $('#btnSourceSrt').textContent = state.settings.mode === 'align' ? '导入 SRT' : '导出 SRT';
  if (animate) {
    source.classList.remove('file-arrived');
    void source.offsetWidth;
    source.classList.add('file-arrived');
  }
}

function renderAll() {
  renderMediaMeta();
  renderSubtitleSource();
  updateModeNote();
  renderTable();
  renderScript();
  updateButtons();
}

// ---------- 选择/编辑 ----------
function selectedCueIndices() {
  return [...state.selectedCues].filter(i=>state.cues[i]).sort((a,b)=>a-b);
}

function contiguousSelection(indices) {
  return indices.length >= 2 && indices.every((value,i)=>i===0||value===indices[i-1]+1);
}

function selectAllCues(checked) {
  if(editLocked())return;
  state.selectedCues=new Set(checked ? state.cues.map((cue,i)=>i) : []);
  if(!state.selectedCues.has(state.selected))state.selected=state.selectedCues.values().next().value ?? -1;
  renderTable();updateButtons();
}

function fmtCueTime(sec) { return fmtTime(sec).replace(/^00:/, ''); }

function selectCue(i, seek = true, additive = false) {
  if (i < 0 || i >= state.cues.length) return;
  state.selected = i;
  if (!additive) state.selectedCues = new Set([i]);
  $$("#cueBody tr.selected").forEach((el) => el.classList.remove("selected"));
  const row = document.querySelector(`#cueBody tr[data-i="${i}"]`);
  if (row) {
    row.classList.add("selected");
    row.scrollIntoView({ block: "nearest" });
  }
  const cue = state.cues[i];
  if (seek && state.media && Number.isFinite(cue.start)) {
    video.currentTime = Math.max(0, cue.start + 0.02);
  }
  $("#selInfo").textContent = `第 ${i + 1} 条 · ${fmtTime(cue.start)} → ${fmtTime(cue.end)} · ${cue.text.length} 字`;
  updateOverlay();
  drawWave();
  $$('#cueBody tr[data-i]').forEach(el=>{
    const checked=state.selectedCues.has(Number(el.dataset.i));
    el.classList.toggle('selected',checked);
    const pick=el.querySelector('.cue-pick'); if(pick)pick.checked=checked;
  });
  updateButtons();
}

function updateOverlay() {
  const t = video.currentTime || 0;
  const shown = findCueAt(t), cue = findCueAt(t,true) || shown;
  const displayed=shown ? displayedSubtitleText(shown.text) : "";
  if(overlay.textContent!==displayed)overlay.textContent=displayed;
  const index = cue ? cue._sourceIndex ?? state.cues.indexOf(cue) : -1;
  if(index !== state.playingIndex) $$("#cueBody tr.playing").forEach((el) => el.classList.remove("playing"));
  if (cue && index !== state.playingIndex) {
    const idx = index;
    const row = document.querySelector(`#cueBody tr[data-i="${idx}"]`);
    if (row) row.classList.add("playing");
    const editing=cueBody.contains(document.activeElement) && document.activeElement.matches('input,[contenteditable="true"]');
    if (row && $('#waveFollow').checked && index !== state.playingIndex && Date.now() >= state.listScrollPauseUntil && !editing) {
      const list = row.closest('.table-wrap'), rr = row.getBoundingClientRect(), lr = list.getBoundingClientRect();
      if (rr.top < lr.top || rr.bottom > lr.bottom) list.scrollTop += rr.top - lr.top - Math.max(8,(lr.height-rr.height)/3);
    }
  }
  state.playingIndex = index;
}

function wrapSubtitleText(text, limit = state.settings.width) {
  const width = Math.max(1, Number(limit) || 20);
  return String(text || '').split(/\r?\n/).flatMap(line => {
    const chars = [...line], lines = [];
    for (let i=0;i<chars.length;i+=width) lines.push(chars.slice(i,i+width).join(''));
    return lines.length ? lines : [''];
  }).join('\n');
}

function adjustedPresentationCues() {
  const s=state.settings;
  const lead=clamp(Number(s.subtitleLead)||0,0,1),tail=clamp(Number(s.subtitleTail ?? 0),0,1);
  const minDur=clamp(Number(s.subtitleMin)||0,0,10),maxDur=clamp(Number(s.subtitleMax)||0,0,30);
  const duration=state.media?.duration || Infinity;
  const cache=state.presentationCache;
  if(cache && state.operation!=='校时' && cache.cues===state.cues && cache.revision===state.editRevision && cache.lead===lead && cache.tail===tail && cache.minDur===minDur && cache.maxDur===maxDur && cache.duration===duration)return cache.result;
  const ordered=state.cues.map((c,i)=>({...c,_sourceIndex:i})).filter(c=>!cueExcluded(c)).sort((a,b)=>a.start-b.start);
  const starts=ordered.map((c,i)=>Math.max(0,c.start-lead,i?Math.min(ordered[i-1].end,c.start):0,
    Number.isFinite(c.pause_before)&&c.pause_before<=c.start+.001?c.pause_before:0));
  const quietLimits=ordered.map(c=>Number.isFinite(c.pause_after)&&c.pause_after>=c.end-.001?c.pause_after:Infinity);
  // 先按提前/延后得到显示时间，再拉长短字幕、截断超长字幕；均不越过下一条开始和视频结尾。
  const ends=ordered.map((c,i)=>Math.min(duration,c.end+tail,i+1<ordered.length?starts[i+1]:Infinity,quietLimits[i]));
  ends.forEach((end,i)=>{
    const limit=Math.min(i+1<ordered.length?starts[i+1]:duration,quietLimits[i]);
    if(maxDur>0)ends[i]=Math.min(ends[i],starts[i]+maxDur);
    if(minDur>0)ends[i]=Math.max(ends[i],Math.min(starts[i]+minDur,limit));
  });
  const result=ordered.map((c,i)=>({...c,start:starts[i],end:ends[i]})).filter(c=>c.end>c.start);
  state.presentationCache={cues:state.cues,revision:state.editRevision,lead,tail,minDur,maxDur,duration,result};
  return result;
}

function presentationCues() {
  if(state.timingPreview)return adjustedPresentationCues();
  const cache=state.rawPresentationCache;
  if(cache&&state.operation!=='校时'&&cache.cues===state.cues&&cache.revision===state.editRevision)return cache.result;
  const result=state.cues.map((cue,i)=>({...cue,_sourceIndex:i})).filter(cue=>!cueExcluded(cue)).sort((a,b)=>a.start-b.start);
  state.rawPresentationCache={cues:state.cues,revision:state.editRevision,result};
  return result;
}

function timingAdjustmentPlan() {
  const updates=[],unsafe=[];
  for(const adjusted of adjustedPresentationCues()){
    const cue=state.cues[adjusted._sourceIndex];
    if(Math.abs(adjusted.start-cue.start)<.0005&&Math.abs(adjusted.end-cue.end)<.0005)continue;
    const shrinking=adjusted.start>cue.start+.001||adjusted.end<cue.end-.001;
    const words=cue.words||[];
    const reliable=words.length&&!cue.alignment_error&&!cue.word_timing_error&&!cue.alignment?.stale
      &&words.every(w=>Number.isFinite(w.start)&&Number.isFinite(w.end)&&w.end>w.start);
    if(shrinking&&(!reliable||words.some(w=>w.start<adjusted.start-.001||w.end>adjusted.end+.001))){
      unsafe.push(cue.display_id??adjusted._sourceIndex+1);continue;
    }
    updates.push({index:adjusted._sourceIndex,start:adjusted.start,end:adjusted.end});
  }
  const proposed=new Map(updates.map(update=>[update.index,update]));
  const spans=state.cues.map((cue,index)=>({...cue,...proposed.get(index),index})).filter(cue=>!cueExcluded(cue)).sort((a,b)=>a.start-b.start);
  let end=-Infinity;
  for(const cue of spans){
    if(!Number.isFinite(cue.start)||!Number.isFinite(cue.end)||cue.start<0||cue.end<=cue.start||cue.start<end-.001)unsafe.push(cue.display_id??cue.index+1);
    end=Math.max(end,cue.end);
  }
  return {updates,unsafe};
}

function applyTimingSettings() {
  if(editLocked())return false;
  const plan=timingAdjustmentPlan();
  if(plan.unsafe.length){
    toast(`未应用：${plan.unsafe.length} 条可能截掉发声或缺少可靠词时间（#${plan.unsafe.slice(0,5).join('、#')}），请先校时或关闭最长显示`,'error',9000);
    return false;
  }
  if(!plan.updates.length){toast('没有需要应用的时间变化','info');return false;}
  const before=snapshot(),cues=state.cues.map(cue=>({...cue}));
  for(const update of plan.updates){
    const cue=cues[update.index];cue.start=Math.round(update.start*1000)/1000;cue.end=Math.round(update.end*1000)/1000;
    delete cue.time_confirmation;
  }
  replaceCues(cues,{before});
  toast(`已应用 ${plan.updates.length} 条时间微调，预览和导出使用相同时间，可撤销`,'success');
  return true;
}

function findCueAt(t, includeExcluded = false) {
  return (includeExcluded?state.cues:presentationCues()).find(c => t >= c.start && t < c.end) || null;
}

function canAdoptCue(cue) {
  const match=cue?.reference_match;
  return !!(cue?.script_text && match?.reference && !match.stale && !match.reference.reference_only
    && (match.query_text==null || match.query_text===cue.text)
    && (match.status!=='ambiguous' || match.candidate_confirmed)
    && simplifiedText(cue.script_text)!==cue.text);
}

function adoptCue(cue) {
  cue.text=simplifiedText(cue.script_text);cue.words=[];cue.edited=true;
  delete cue.merge_suggestion;
  delete cue.interjection;
  cue.review_decision='adopted';cue.match_needs_review=false;
  cue.reference_match={...cue.reference_match,stale:true};
  if(cue.alignment)cue.alignment={...cue.alignment,stale:true};
  completeModification(cue);
}

async function calibrateAll() {
  if (editLocked()) return;
  const selected=selectedCueIndices();
  const indices=selected.length?selected:state.cues.map((cue,i)=>i);
  if(!indices.length)return;
  const before=snapshot(),path=projectPath(),epoch=state.mediaEpoch,previous=state.operation;
  const beforeCues=state.cues.map(cue=>JSON.parse(JSON.stringify(cue)));
  const initialHistory=state.history.slice();
  state.operation='一键校准';updateButtons();
  try{
    if(!state.script.length){
      indices.forEach(i=>markNoReference(state.cues[i]));
      toast('没有参考内容，已标出所选条目，未改动字幕文字','info');return;
    }
    const stale=indices.filter(i=>{const c=state.cues[i],m=c.reference_match;return !m||m.stale||m.query_text!==c.text;});
    if(stale.length && !await rematchDerivedCues(stale,{preserveModification:true}))return;
    if(projectPath()!==path||state.mediaEpoch!==epoch)return;
    const referenceFixes={missing:0,extra:0};
    for(const i of indices){
      const cue=state.cues[i],match=cue.reference_match;
      const correctable=['high','typo','homophone','missing','extra'].includes(match?.status)||(match?.status==='ambiguous'&&match.candidate_confirmed);
      if(correctable&&canAdoptCue(cue)){
        if(match?.status==='missing')referenceFixes.missing++;
        else if(match?.status==='extra')referenceFixes.extra++;
        adoptCue(cue);
      }
    }
    const layout=await normalizeCalibrationLayout(indices);
    const targets=layout.indices;
    // 校准只按参考和字数整理内容与分段，不碰音轨；时间留给最后“对齐字幕”按音源重算。
    if(targets.length)await rematchDerivedCues(targets,{preserveModification:false});
    targets.forEach(i=>{if(state.cues[i])state.cues[i].verification_stage='calibrate';});
    const restored=releaseVerificationExclusions(state.cues,targets);
    if(selected.length)state.selectedCues=new Set(targets);
    const report=showVerificationReport('校准',beforeCues,state.cues,{requireReference:state.script.length>0});
    const merged=layout.merged ?? 0;
    const suggestions=layout.merge_suggestions ?? 0;
    const referenceHint=(referenceFixes.missing||referenceFixes.extra)
      ? ` · 其中按参考补全 ${referenceFixes.missing} 条、删除 ${referenceFixes.extra} 条`:'';
    const interjectionHint=[
      layout.interjection_removed?`语气词去除 ${layout.interjection_removed} 条`:'',
      layout.interjection_trimmed?`句首句末语气词清理 ${layout.interjection_trimmed} 条`:'',
      layout.interjection_restored?`语气词恢复导出 ${layout.interjection_restored} 条`:''
    ].filter(Boolean).join(' · ');
    const restoredHint=restored?` · 解除旧自动排除 ${restored} 条`:'';
    const mergeHint=merged?` · 同一句自动合并 ${merged} 处`:'';
    const hint=suggestions?` · ${suggestions} 处红色“建议合并”请勾选相邻字幕后手动合并`:'';
    toast(`校准完成 · 改变 ${report.changed} 条${referenceHint} · 分句 ${layout.splits} 处${mergeHint}${interjectionHint?' · '+interjectionHint:''} · 已校准 ${report.after.passed} 条 · 疑似 ${report.after.suspect} 条 · 明显问题 ${report.after.obvious} 条${restoredHint}${hint} · 未涉及音轨，时间留给对齐字幕`,'success',9000);
  }catch(error){
    toast(error.message,'error',7000);
  }finally{
    state.operation=previous;
    if(projectPath()===path&&state.mediaEpoch===epoch&&snapshot()!==before){state.history=initialHistory;pushHistory(before);scheduleSave();}
    renderAll();updateOverlay();updateButtons();
  }
}

async function normalizeCalibrationLayout(indices) {
  const before=snapshot(),path=projectPath(),epoch=state.mediaEpoch;
  const groups=[];
  for(const i of [...new Set(indices)].sort((a,b)=>a-b)){
    const last=groups.at(-1);
    if(last&&last.at(-1)+1===i&&last.length<5000)last.push(i);else groups.push([i]);
  }
  const staged=[];let merged=0,suggestions=0,splits=0,interjectionRemoved=0,interjectionTrimmed=0,interjectionRestored=0;
  for(const group of groups){
    const result=await api('/api/segment',{mode:'calibration',cues:group.map(i=>state.cues[i]),
      max_width:state.settings.width,gap_seconds:state.settings.gap??.6,keep_punc:state.settings.keepPunc!==false,
      keep_interjections:state.settings.keepInterjections===true});
    if(!sameDocument(path,epoch,before))throw new Error('字幕已变化，已保留当前内容');
    if(result.mode!=='calibration'||!Array.isArray(result.cues)||!Array.isArray(result.changed_indices))
      throw new Error('后台尚未启用校准整理接口，请保存工程并重启程序后再试');
    const modified=new Set(result.completed_indices??result.changed_indices);
    result.cues.forEach((cue,i)=>{if(modified.has(i))completeModification(cue);});
    staged.push({group,cues:result.cues});merged+=result.merged??0;suggestions+=result.merge_suggestions??0;splits+=result.splits;
    interjectionRemoved+=result.interjection_removed??0;
    interjectionTrimmed+=result.interjection_trimmed??0;
    interjectionRestored+=result.interjection_restored??0;
  }
  const all=[],targets=[];let cursor=0;
  for(const {group,cues} of staged){
    all.push(...state.cues.slice(cursor,group[0]));
    targets.push(...cues.map((cue,i)=>all.length+i));all.push(...cues);cursor=group.at(-1)+1;
  }
  all.push(...state.cues.slice(cursor));
  if(JSON.stringify(all)!==JSON.stringify(state.cues)){
    const selected=state.selectedCues.size>0;
    state.cues=all.map(normalizeCue);
    state.selectedCues=new Set(selected?targets:[]);state.selected=selected?(targets[0]??-1):-1;
  }
  return {indices:targets,merged,merge_suggestions:suggestions,splits,
    interjection_removed:interjectionRemoved,interjection_trimmed:interjectionTrimmed,
    interjection_restored:interjectionRestored};
}

function markNoReference(cue) {
  cue.script_text='';cue.script_index=null;cue.match_ratio=null;cue.match_needs_review=true;
  cue.reference_match={status:'unmatched',label:'无参考内容',query_text:cue.text,reference:null,
    stale:false,score:0,alternatives:[],warning:'没有可靠参考内容，原字幕保持不变'};
}

function markReview(i, decision) {
  if (editLocked() || !state.cues[i]) return;
  const before = snapshot(), cue = state.cues[i];
  cue.review_decision = decision;
  cue.exclude_export = decision === 'non_script';
  delete cue.interjection_excluded;
  delete cue.verification_excluded;
  if (decision === 'included' && cue.hallucination) {
    // 用户确认要导出后，不再重复提示、也不再自动排除；重新载入工程时保持这个决定。
    cue.hallucination = { ...cue.hallucination, dismissed: true };
    cue.is_noise = false;
  }
  if (decision === 'kept') { cue.text = cue.original_text; cue.edited = cue.text !== cue.original_text; cue.words = []; delete cue.merge_suggestion; }
  pushHistory(before); renderAll(); scheduleSave(); updateOverlay();
}

function highlighted(text, differences, side) {
  const chars = [...String(text || '')];
  const ranges = (differences || []).filter(d => d[side + '_end'] > d[side + '_start']);
  return chars.map((c, i) => ranges.some(d => i >= d[side + '_start'] && i < d[side + '_end'])
    ? '<mark class="diff-' + side + '">' + escapeHtml(c) + '</mark>' : escapeHtml(c)).join('');
}

function renderScript() {
  const zone = $('#scriptZone'), nameEl = $('#scriptStats'), infoEl = $('#scriptInfo');
  const references = new Map();
  state.cues.forEach((cue, index) => { if (cue.script_index != null && cue.script_text && !references.has(cue.script_index)) references.set(cue.script_index, index); });
  state.script.forEach(line => { line.cue_index = references.get(line.index) ?? null; });
  zone.classList.toggle('filled', !!state.script.length || state.scriptUploading);
  const names = state.scriptNames?.length ? state.scriptNames : state.script.length ? [state.scriptName || '剧本'] : [];
  nameEl.textContent = state.scriptUploading ? '正在读取…' : names.length ? `${state.scriptName} ×${names.length}` : '拖入或点击选择';
  nameEl.title = names.map((name, i) => `${i + 1}. ${name}`).join('\n');
  infoEl.textContent = state.scriptUploading ? state.scriptUploadName : state.script.length ? state.script.length + ' 段 · 跨剧本定位' : '';
  updateModeUI(); updateModeNote(); updateButtons();
}

function waveWindow() {
  const duration = state.media?.duration || state.waveform?.duration || 0;
  const span = duration ? Math.min(duration, state.waveSpan || duration) : 1;
  const start = Math.max(0, Math.min(state.waveStart || 0, Math.max(0, duration - span)));
  return { duration, start, span };
}

function followTimeline() {
  if (!$('#waveFollow').checked || video.paused) return;
  const view = waveWindow(), now = video.currentTime || 0;
  if (!view.duration || view.span >= view.duration) return;
  if (now < view.start || now > view.start + view.span * .85) {
    state.waveStart = clamp(now-view.span*.2,0,Math.max(0,view.duration-view.span));
    requestWaveDetail({ defer: true });
    return true;
  }
}

function updatePlayhead() {
  const view=waveWindow(), now=video.currentTime || 0, cursor=$('#waveCursor');
  cursor.hidden=!view.duration || now<view.start || now>view.start+view.span;
  if(!cursor.hidden)cursor.style.transform='translateX('+((now-view.start)/view.span*waveCanvas.clientWidth)+'px)';
  $('#waveReadout').textContent=fmtTime(now)+' / '+fmtTime(view.duration);
}

function playbackTick() {
  state.playbackFrame=null;
  if(video.paused || video.ended)return;
  updateOverlay();
  if(followTimeline())drawWave();
  updatePlayhead();
  state.playbackFrame=requestAnimationFrame(playbackTick);
}

function setTimelineHeight(height, persist = false) {
  const max = Math.max(190, $('.center').clientHeight-260);
  const value = Math.round(clamp(Number(height)||230,190,max));
  $('#timelinePanel').style.height = value+'px';
  if (persist) writeJson('subtitleStudio.timelineHeight',{height:value});
  drawWave();
}

function bindTimelineLayout() {
  setTimelineHeight(Number(readJson('subtitleStudio.timelineHeight').height) || 230);
  const grip = $('#timelineResize');
  grip.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    event.preventDefault();
    const initialY=event.clientY, initialHeight=$('#timelinePanel').getBoundingClientRect().height;
    grip.setPointerCapture(event.pointerId); document.body.classList.add('row-resizing');
    const move=e=>{if(e.pointerId===event.pointerId)setTimelineHeight(initialHeight+initialY-e.clientY);};
    const finish=e=>{
      if(e.pointerId!==event.pointerId)return;
      window.removeEventListener('pointermove',move,true);window.removeEventListener('pointerup',finish,true);window.removeEventListener('pointercancel',finish,true);
      document.body.classList.remove('row-resizing');
      if(grip.hasPointerCapture(e.pointerId))grip.releasePointerCapture(e.pointerId);
      setTimelineHeight($('#timelinePanel').getBoundingClientRect().height,true);
    };
    window.addEventListener('pointermove',move,true);window.addEventListener('pointerup',finish,true);window.addEventListener('pointercancel',finish,true);
  });
  grip.addEventListener('keydown',e=>{
    if(!['ArrowUp','ArrowDown'].includes(e.key))return;
    e.preventDefault();setTimelineHeight($('#timelinePanel').getBoundingClientRect().height+(e.key==='ArrowUp'?20:-20),true);
  });
  window.addEventListener('resize',()=>setTimelineHeight($('#timelinePanel').getBoundingClientRect().height));
  $('#wavePan').addEventListener('scroll',e=>{
    const pan=e.target,view=waveWindow();
    if(!view.duration || Math.abs(pan.scrollLeft-(pan._syncedLeft || 0))<1)return;
    state.waveStart=clamp(pan.scrollLeft/Math.max(1,pan.clientWidth)*view.span,0,Math.max(0,view.duration-view.span));
    $('#waveFollow').checked=false;
    requestWaveDetail();
  });
  $('#waveFollow').addEventListener('change',()=>{followTimeline();drawWave();});
  $('.right .table-wrap').addEventListener('wheel',()=>{state.listScrollPauseUntil=Date.now()+2500;},{passive:true});
  waveCanvas.addEventListener('wheel',e=>{
    const view=waveWindow();if(!view.duration)return;
    e.preventDefault();
    if(!e.shiftKey && Math.abs(e.deltaY)>=Math.abs(e.deltaX)){
      const r=waveCanvas.getBoundingClientRect(), ratio=clamp((e.clientX-r.left)/r.width,0,1),anchor=view.start+view.span*ratio;
      state.waveSpan=clamp(view.span*Math.exp(e.deltaY*.003),.5,view.duration);
      state.waveStart=clamp(anchor-state.waveSpan*ratio,0,Math.max(0,view.duration-state.waveSpan));
    } else {
      state.waveStart=clamp(view.start+(e.deltaX||e.deltaY)*view.span/Math.max(1,waveCanvas.clientWidth),0,Math.max(0,view.duration-view.span));
      $('#waveFollow').checked=false;
    }
    requestWaveDetail();
  },{passive:false});
}

function focusWave(cue) {
  const duration = waveWindow().duration;
  if (!duration || !cue) return;
  state.waveSpan = Math.min(duration, Math.max(4, (cue.end - cue.start) * 3));
  state.waveStart = Math.max(0, cue.start - (state.waveSpan - (cue.end - cue.start)) / 2);
  requestWaveDetail({ defer: true });
}

function detailCovering(view) {
  const covers = item => item && Array.isArray(item.peaks) && item.peaks.length
    && Number(item.start) <= view.start + .001 && Number(item.end) >= view.start + view.span - .001;
  let best = null;
  for (const item of [state.detailWave, ...(state.detailWaves || [])]) {
    if (!covers(item)) continue;
    if (!best || item.end - item.start < best.end - best.start) best = item;
  }
  return best;
}

function rememberWaveDetail(result) {
  if (!result || !Array.isArray(result.peaks) || !result.peaks.length) return;
  const start = Number(result.start), end = Number(result.end);
  if (!(end > start)) return;
  state.detailWaves = [{ ...result, start, end },
    ...(state.detailWaves || []).filter(item => Math.abs(item.start - start) > .001 || Math.abs(item.end - end) > .001)
  ].slice(0, 8);
}

function resetWaveDetail() {
  state.detailWave = null; state.detailWaves = [];
  state.detailWaveEpoch++; clearTimeout(state.detailWaveTimer);
  state.waveDefer = false; state.wavePainted = false;
}

function requestWaveDetail(options = {}) {
  const defer = options.defer === true;
  clearTimeout(state.detailWaveTimer);
  const view = waveWindow(), path = state.media?.path, epoch = ++state.detailWaveEpoch, mediaEpoch = state.mediaEpoch;
  const covering = detailCovering(view);
  if (covering) { state.detailWave = covering; state.waveDefer = false; drawWave(); return; }
  // 局部接口上限 120 秒，留出余量，避免浮点误差被后端拒绝。
  if (!path || !view.duration || view.span > 118 || view.span >= view.duration) {
    state.waveDefer = false; drawWave(); return;
  }
  // 多取一段相邻音频：附近点选、跟随播放时直接用已缓存的局部波形，避免回到全片波形再切换。
  const limit = Math.max(0, (118 - view.span) / 2);
  const pad = Math.min(Math.max(8, view.span * 3), limit);
  const start = Math.max(0, view.start - pad);
  const end = Math.min(view.duration, view.start + view.span + pad);
  const range = Math.max(.001, end - start);
  const buckets = Math.min(4000, Math.max(1200, Math.ceil(1200 * range / Math.max(.001, view.span))));
  state.waveDefer = defer;
  state.detailWaveTimer = setTimeout(async () => {
    try {
      const result = await api('/api/waveform?path=' + encodeURIComponent(path) + '&buckets=' + buckets + '&start=' + start + '&end=' + end, null, null, { timeout: 20000 });
      if (epoch !== state.detailWaveEpoch || mediaEpoch !== state.mediaEpoch || state.media?.path !== path) return;
      rememberWaveDetail(result);
      state.detailWave = detailCovering(waveWindow()) || state.detailWave;
      state.waveDefer = false;
      drawWave();
    } catch {
      if (epoch !== state.detailWaveEpoch || mediaEpoch !== state.mediaEpoch || state.media?.path !== path) return;
      state.waveDefer = false;
      drawWave();
    }
  }, 100);
  drawWave();
}

function bindWaveCanvas(canvas) {
  let drag = null;
  let scrub = null;
  const timeAt = event => { const rect = canvas.getBoundingClientRect(), view = waveWindow(); return clamp(view.start + (event.clientX - rect.left) / rect.width * view.span, 0, view.duration); };
  canvas.addEventListener('pointerdown', event => {
    const view = waveWindow(); let cue = state.cues[state.selected];
    if (!view.duration) return;
    const rect = canvas.getBoundingClientRect(), x = event.clientX - rect.left;
    let hit=null;
    if(canvas===waveCanvas && event.clientY-rect.top>=22 && event.clientY-rect.top<=54){
      hit=presentationCues().find(c=>timeAt(event)>=c.start && timeAt(event)<c.end);
      if(!hit)hit=presentationCues().find(c=>Math.min(Math.abs(x-(state.cues[c._sourceIndex].start-view.start)/view.span*rect.width),Math.abs(x-(state.cues[c._sourceIndex].end-view.start)/view.span*rect.width))<=9);
      if(hit){selectCue(hit._sourceIndex,false);cue=state.cues[hit._sourceIndex];}
    }
    const x1 = cue ? (cue.start - view.start) / view.span * rect.width : -100;
    const x2 = cue ? (cue.end - view.start) / view.span * rect.width : -100;
    const edge = Math.abs(x - x1) <= 9 ? 'start' : Math.abs(x - x2) <= 9 ? 'end' : null;
    const onCaption = canvas !== waveCanvas || (event.clientY-rect.top>=22 && event.clientY-rect.top<=55);
    if (cue && (edge || hit) && onCaption && !editLocked()) {
      const others=state.cues.filter(c=>c!==cue).sort((a,b)=>a.start-b.start);
      drag = { cue, edge:edge || 'move', before: snapshot(), epoch: state.mediaEpoch, pointerId:event.pointerId,
        anchor:timeAt(event),initialStart:cue.start,initialEnd:cue.end,
        previousEnd:others.filter(c=>c.start<cue.start).at(-1)?.end || 0,
        nextStart:others.find(c=>c.start>=cue.start)?.start ?? view.duration };
      if(video.paused===false)video.pause();
      state.operation = '校时'; updateButtons(); canvas.setPointerCapture(event.pointerId);
    } else { state.auditionEnd = null; scrub=event.pointerId;canvas.setPointerCapture?.(event.pointerId);video.currentTime = timeAt(event); updateOverlay(); drawWave(); }
  });
  canvas.addEventListener('pointermove', event => {
    if(scrub===event.pointerId){video.currentTime=timeAt(event);updateOverlay();updatePlayhead();return;}
    if(!drag && canvas.style && canvas===waveCanvas){
      const r=canvas.getBoundingClientRect(),view=waveWindow(),x=event.clientX-r.left,y=event.clientY-r.top;
      const edge=y>=22&&y<=54&&state.cues.some(c=>Math.min(Math.abs(x-(c.start-view.start)/view.span*r.width),Math.abs(x-(c.end-view.start)/view.span*r.width))<=9);
      const body=y>=22&&y<=54&&presentationCues().some(c=>timeAt(event)>=c.start&&timeAt(event)<c.end);
      canvas.style.cursor=editLocked()?'default':edge?'ew-resize':body?'grab':'crosshair';
    }
    if (!drag || drag.epoch !== state.mediaEpoch || drag.pointerId!==event.pointerId) return;
    const time = Math.round(timeAt(event) * 1000) / 1000;
    const duration = waveWindow().duration;
    if(drag.edge==='move'){
      const length=drag.initialEnd-drag.initialStart;
      const low=Math.max(0,drag.previousEnd), high=Math.min(duration-length,drag.nextStart-length);
      if(high<low)return;
      drag.cue.start=Math.round(clamp(drag.initialStart+time-drag.anchor,low,high)*1000)/1000;
      drag.cue.end=Math.round((drag.cue.start+length)*1000)/1000;
    } else if (drag.edge === 'start') {
      const latestStart = Math.min(drag.cue.end, duration) - .001;
      if (latestStart < 0) return;
      drag.cue.start = clamp(time, Math.min(latestStart,drag.previousEnd), latestStart);
    } else {
      const earliestEnd = drag.cue.start + .001;
      if (earliestEnd > duration) return;
      drag.cue.end = clamp(time, earliestEnd, Math.max(earliestEnd,Math.min(duration,drag.nextStart)));
    }
    // 手工拖动时间与输入时间同等对待：解除自动变灰，手动“不导出”保留。
    if(drag.cue.verification_excluded){
      delete drag.cue.verification_excluded;
      if(drag.cue.review_decision!=='non_script')drag.cue.exclude_export=false;
    }
    completeModification(drag.cue); drag.cue.words = [];
    if(drag.cue.alignment)drag.cue.alignment.stale=true;
    for(const field of ['start','end']){
      const row=cueBody.querySelector('tr[data-i="'+state.cues.indexOf(drag.cue)+'"] input[data-field="'+field+'"]');
      if(row)row.value=fmtCueTime(drag.cue[field]);
    }
    video.currentTime = drag.edge==='move'?drag.cue.start:drag.cue[drag.edge]; drawWave();
  });
  const finish = event => {
    if(scrub===event.pointerId){scrub=null;if(canvas.hasPointerCapture?.(event.pointerId))canvas.releasePointerCapture(event.pointerId);return;}
    if (!drag) return;
    if(drag.pointerId!==event.pointerId)return;
    const saved = drag; drag = null; state.operation = '';
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
    if (saved.epoch === state.mediaEpoch) { pushHistory(saved.before); renderAll(); scheduleSave(); }
    updateButtons();
  };
  canvas.addEventListener('pointerup', finish); canvas.addEventListener('pointercancel', finish);
  canvas.addEventListener('lostpointercapture', finish);
  canvas.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key) || !state.media) return;
    event.preventDefault(); state.auditionEnd = null;
    video.currentTime = clamp(video.currentTime + (event.key === 'ArrowRight' ? 1 : -1) * (event.shiftKey ? 1 : .1), 0, waveWindow().duration);
    updateOverlay(); drawWave();
  });
}

async function matchScript(silent = false, forceIndices = []) {
  if (state.settings.mode !== 'align' || (editLocked() && !silent)) return false;
  if (!state.cues.length || !state.script.length) {
    if (!silent) toast('需要已有字幕和剧本才能匹配', 'error');
    return false;
  }
  const before = snapshot(), path = projectPath(), epoch = state.mediaEpoch;
  const scriptFingerprint = JSON.stringify(state.scriptDocument || state.script);
  const previousOperation = state.operation;
  state.operation = '匹配剧本'; updateButtons();
  try {
    const doc = state.scriptDocument;
    const validDoc = doc?.text && doc.paragraphs?.length === state.script.length && doc.paragraphs.every((p, i) => p.text === state.script[i].text);
    const d = await runReferenceMatch({ cues: state.cues, lines: state.script,
      document: validDoc ? doc : {text:state.script.map(line => line.text).join('\n')},
      options: {minimum: state.settings.matchMinimum, high:state.settings.matchHigh, phonetic:state.settings.matchPhonetic,
        ambiguity:state.settings.matchAmbiguity, max_group:state.settings.matchGroup}, force_indices:forceIndices });
    if (!sameDocument(path, epoch, before) || scriptFingerprint !== JSON.stringify(state.scriptDocument || state.script)) throw new Error('字幕或剧本已变化，已保留当前修改，请重新匹配');
    state.script = d.lines;
    state.scriptDocument = d.document; state.referenceGaps = d.gaps || [];
    replaceCues(d.cues.map(cue=>({...cue,modification_complete:false})), { before });
    // 校验关只判定内容：通过标签记为“参考一致”，问题标签保持通用。
    const stamp = forceIndices.length ? forceIndices.filter(i=>state.cues[i]) : state.cues.map((_,i)=>i);
    stamp.forEach(i=>{state.cues[i].verification_stage='verify';});
    releaseVerificationExclusions(state.cues, stamp);
    // replaceCues 已先渲染过一版，阶段标记和解除旧排除写入后要再刷新一次表格与保存。
    renderAll();scheduleSave();
    const stats = verificationStatistics(state.cues,{requireReference:true});
    const first = d.lines.find(line => line.cue_index != null);
    if (first) { selectCue(first.cue_index); focusWave(state.cues[first.cue_index]); }
    const interjections = d.cues.filter(cue => cue.interjection).length;
    if (!silent) toast('已校验 ' + (d.stats?.total || 0) + ' 条 · ' + (d.stats?.needs_review || 0) + ' 条需复核'
      + ` · 参考一致 ${stats.passed} 条 · 疑似 ${stats.suspect} 条 · 明显问题 ${stats.obvious} 条`
      + (interjections ? ` · 语气词 ${interjections} 条已标红` : '') + '，未改写字幕', 'success');
    return true;
  } catch (e) { toast(e.message, 'error'); return false; }
  finally { state.operation = previousOperation; updateButtons(); }
}

async function startAsr(task = 'asr', region = null, options = {}) {
  if (state.jobId || state.submitting || (state.batchRecognition&&!options.batch) || state.operation || state.loading || state.uploading || state.scriptUploading || state.scriptPickerOpen) return null;
  if (state.versionMismatch) { toast('后台版本已过期，请先保存工程并重启程序', 'error'); return null; }
  if (state.detachedJob && !options.resume) { toast('有尚未取回的任务，请点击“继续查询原任务”，避免重复处理', 'info'); return null; }
  if (!state.media || (task !== 'align' && !modelAvailable())) { toast('请先选择素材和可用模型', 'error'); return null; }
  const s = { ...collectSettings() };
  const taskStarted=Date.now();
  document.activeElement?.blur();
  const path = options.resume?.path ?? projectPath(), mediaEpoch = options.resume?.mediaEpoch ?? state.mediaEpoch,
    before = options.resume?.before ?? snapshot();
  if (path !== projectPath() || mediaEpoch !== state.mediaEpoch) { toast('请先打开原任务所属工程再查询', 'info'); return null; }
  const epoch = ++state.asrEpoch;
  const id = options.resume?.id ?? crypto.randomUUID().replaceAll('-', '');
  const {resume, ...resumeOptions} = options;
  state.activeJob = {id, path, mediaEpoch, before, task, region, options:resumeOptions, epoch};
  state.detachedJob = null;
  state.jobId = id; state.submitting = true; state.pendingResult = null;
  updateButtons();
  let job = { id, status: 'queued' }, failures = 0;
  try {
    try {
      if (!options.resume) job = (await api(task === 'align' ? '/api/alignment/jobs' : '/api/jobs', { path, model: s.model, device: s.device,
        cues: task === 'align' ? (options.indices?options.indices.map(i=>state.cues[i]):state.cues) : undefined, vad: s.vad, keep_interjections: s.keepInterjections,
        gap_seconds: s.gap, max_width: s.width,
        width_split: task === 'align' ? true : undefined,
        region: region ? {start:region.start,end:region.end} : undefined, request_id: id })).job;
    } catch (error) {
      if (error.status) throw error;
      toast('提交响应中断，正在按原任务标识查询；请勿重复提交', 'info');
    }
    if (epoch !== state.asrEpoch) return null;
    state.submitting = false;
    while (epoch === state.asrEpoch) {
      try {
        job = (await api('/api/jobs/' + id, null, null, {timeout:5000})).job;
        if (epoch !== state.asrEpoch) return null;
        failures = 0; $('#btnReconnect').hidden = true; $('#btnStopWaiting').hidden = true;
        updateJob(job);
        if (['done', 'error', 'cancelled'].includes(job.status)) break;
      } catch (error) {
        if ([403, 404].includes(error.status)) throw error;
        if (epoch !== state.asrEpoch) return null;
        failures++;
        jobMsg.textContent = '连接中断，保留任务并等待重连（' + failures + '）';
        $('#btnReconnect').hidden = false;
        $('#btnStopWaiting').hidden = false;
        if (failures >= 5) { stopWaitingForJob(); throw new Error('持续无法连接，已恢复编辑；可继续查询原任务'); }
        if (failures === 1) toast('任务仍在后台，正在重试查询', 'info');
      }
      await new Promise(resolve => {
        const timer = setTimeout(() => { state.retryPoll = null; resolve(); }, failures ? Math.min(5000, 600 * 2 ** Math.min(failures, 4)) : 600);
        state.retryPoll = () => { clearTimeout(timer); state.retryPoll = null; resolve(); };
      });
    }
    if (epoch !== state.asrEpoch) return null;
    if (job.status === 'done') {
      while (epoch === state.asrEpoch) {
        try { job = (await api('/api/jobs/' + id + '?result=1', null, null, {timeout:5000})).job; break; }
        catch (error) {
          if ([403, 404].includes(error.status)) throw error;
          if (epoch !== state.asrEpoch) return null;
          jobMsg.textContent = '识别已完成，正在重试获取结果';
          $('#btnReconnect').hidden = false;
          $('#btnStopWaiting').hidden = false;
          if (++failures >= 5) { stopWaitingForJob(); throw new Error('结果暂时无法取回，已恢复编辑；可继续查询原任务'); }
          await sleep(1000);
        }
      }
      if (epoch !== state.asrEpoch) return null;
      if (!sameDocument(path, mediaEpoch, before)) {
        if(!region) {
          state.pendingResult = { job, path, task };
          throw new Error('字幕已变化，已保留当前修改；可点击“采用识别结果”另行采用');
        }
        throw new Error('字幕已变化，已保留当前修改；请重新处理所选字幕');
      }
      let nextCues=job.cues || [];
      if(region){
        // 本段只识别到语气词时允许删除原条目；真的没识别到内容才保留原字幕。
        if(!nextCues.length && !(job.info?.interjections?.count > 0) && !(job.info?.interjections_removed > 0))throw new Error('本段未识别到字幕，已保留原内容');
        const original=state.cues[region.index];
        // 只沿用用户手工设置的“不导出”；旧条目的幻觉排除不能盖掉本段新结果自己的标记。
        const manualExcluded=cueExcluded(original)&&!original.hallucination;
        nextCues=nextCues.map(c=>({...c,previous_asr:original.asr_original,
          exclude_export:cueExcluded(c)||manualExcluded,
          review_decision:manualExcluded?'non_script':(c.review_decision||'pending')}));
        if(state.script.length && s.mode==='align'){
          const doc=state.scriptDocument || {text:state.script.map(p=>p.text).join('\n')};
          const previous=state.cues.slice(0,region.index).reverse().find(c=>c.reference_match?.reference && !c.reference_match.stale);
          const next=state.cues.slice(region.index+1).find(c=>c.reference_match?.reference && !c.reference_match.stale);
          try{
            const left=previous?.reference_match.reference,right=next?.reference_match.reference;
            const position_bounds=left&&right&&left.source_id===right.source_id&&left.end<=right.start?[left.end,right.start]:undefined;
            const checked=await runReferenceMatch({cues:nextCues,document:doc,lines:[],position_bounds,
              options:{minimum:s.matchMinimum,high:s.matchHigh,phonetic:s.matchPhonetic,ambiguity:s.matchAmbiguity,max_group:s.matchGroup}});
            nextCues=checked.cues;
          }catch(error){toast('本段已识别，剧本校验未完成：'+error.message,'error');}
        }
        if(!sameDocument(path,mediaEpoch,before))throw new Error('字幕已变化，已保留当前内容，请重新处理本段');
        const all=state.cues.slice();all.splice(region.index,1,...nextCues);nextCues=all;
      }
      replaceCues(nextCues, { before,
        subtitleFile: task === 'align' || region ? state.subtitleFile : job.subtitle_path ? { path: job.subtitle_path, origin: 'generated' } : null });
      if(task === 'asr' && !region) state.initialCues=JSON.parse(JSON.stringify(state.cues));
      renderSubtitleSource(true);
      if (task === 'asr' && s.mode === 'align' && state.script.length) await matchScript(true);
      if (!(await saveProject(false))) throw new Error('识别已完成，工程保存失败，请先保存或导出');
      jobMsg.textContent='完成 · 用时 '+formatElapsed(Date.now()-taskStarted);
      if(!options.deferCompletionSound)playSound('success');
      toast(region ? '本段重识别完成，其余字幕已保留' : (task === 'align' ? '时间轴对齐完成：' : '提取成功：') + state.cues.length + ' 条字幕', 'success');
      const hallucinated = job.info?.hallucination?.count || 0;
      if (hallucinated) toast(`已自动排除 ${hallucinated} 条疑似幻觉字幕（预览和导出不含，可在列表中恢复导出或删除）`, 'info', 9000);
      const interjections = job.info?.interjections?.count || 0;
      const removedInterjections = job.info?.interjections_removed || 0;
      const trimmedInterjections = job.info?.interjections_trimmed || 0;
      if ((interjections || removedInterjections || trimmedInterjections) && !region) {
        const details = [];
        if (removedInterjections) details.push(`删除 ${removedInterjections} 条整条语气词`);
        if (trimmedInterjections) details.push(`去掉 ${trimmedInterjections} 条句首/句末语气词`);
        if (interjections) details.push(`保留并标红 ${interjections} 条语气词，校准后正常导出`);
        toast(`语气词：${details.join('，')}`, 'info', 7000);
      }
      const skipped = task === 'align' ? (job.info?.skipped || []) : [];
      if (skipped.length) toast(`${skipped.length} 条对齐时间不可靠，已保留原时间，请试听复核：第 ${skipped.slice(0, 3).map(item => item.id).filter(Boolean).join('、')} 条`, 'error', 9000);
    } else if (job.status === 'cancelled') toast('已取消');
    else if (job.error) toast(job.error, 'error');
    return job;
  } catch (error) {
    toast(error.message, 'error', 8000);
    return { ...job, status: 'error', error: error.message };
  } finally {
    if (epoch === state.asrEpoch || !state.jobId) {
      if (state.jobId === id) state.jobId = null;
      if (state.activeJob?.epoch === epoch) state.activeJob = null;
      state.submitting = false; state.retryPoll = null;
      $('#btnStopWaiting').hidden = true;
      $('#btnReconnect').hidden = !state.detachedJob;
      $('#btnReconnect').textContent = state.detachedJob ? '继续查询原任务' : '重新查询任务';
    }
    updateButtons();
  }
}

function stopWaitingForJob() {
  if (!state.activeJob) return;
  state.detachedJob = state.activeJob;
  state.activeJob = null;
  state.asrEpoch++;
  state.jobId = null; state.submitting = false;
  state.batchCancelRequested = true;
  state.retryPoll?.();
  $('#btnStopWaiting').hidden = true;
  $('#btnReconnect').hidden = false;
  $('#btnReconnect').textContent = '继续查询原任务';
  jobMsg.textContent = '已停止等待，原任务可能仍在后台；重连后可查询结果';
  updateButtons();
}

/* 只校验：对照参考文档标出错误和问题，不改字幕文字，也不改时间。 */
async function verifyOnly() {
  if (editLocked()) return;
  if (!state.cues.length) { toast('请先导入字幕', 'error'); return; }
  if (!state.script.length) { toast('请先导入参考文档', 'error'); return; }
  const taskStarted = Date.now();
  if (await matchScript()) jobMsg.textContent = '校验完成 · 用时 ' + formatElapsed(Date.now() - taskStarted);
}

async function alignAndVerify() {
  // 对齐负责时间：同一句内按声音停顿拆条，参考明确断句处按句界拆，过长按每行字数分段；不改字词、不合并。
  if (editLocked()) return;
  if (!state.media || !state.cues.length) { toast('请先导入媒体和 SRT', 'error'); return; }
  const beforeCues=state.cues.map(cue=>JSON.parse(JSON.stringify(cue)));
  const taskStarted=Date.now();
  const job = await startAsr('align',null,{deferCompletionSound:true});
  if (job?.status !== 'done') return;
  // 对齐只改时间和分段：不重跑参考校验，不清“文字已确认”，也不改导出标记。
  state.cues.forEach(cue=>{cue.verification_stage='align';});
  const restored=releaseVerificationExclusions(state.cues);
  renderAll();updateOverlay();updateButtons();scheduleSave();
  const report=showVerificationReport('对齐',beforeCues,state.cues,{requireReference:state.script.length>0});
  playSound('success');
  jobMsg.textContent='完成 · 用时 '+formatElapsed(Date.now()-taskStarted);
  const gapSplits=Number(job.info?.gap_splits)||0,widthSplits=Number(job.info?.width_splits)||0;
  const durationRepairs=Number(job.info?.duration_repairs?.length)||0;
  const parts=[];
  if(gapSplits)parts.push(`停顿断句 ${gapSplits} 条`);
  if(widthSplits)parts.push(`按每行字数分段 ${widthSplits} 条`);
  if(durationRepairs)parts.push(`按字数收紧 ${durationRepairs} 条`);
  const restoredHint=restored?` · 解除旧自动排除 ${restored} 条`:'';
  toast(`音轨对齐完成 · 改变 ${report.changed} 条 · 自动对齐完成 ${report.after.passed} 条 · 疑似 ${report.after.suspect} 条 · 明显问题 ${report.after.obvious} 条${restoredHint}${parts.length?' · '+parts.join(' · '):''}`,'success',9000);
}

function formatElapsed(milliseconds) {
  const seconds=Math.max(0,Math.round((Number(milliseconds)||0)/1000));
  const hours=Math.floor(seconds/3600),minutes=Math.floor(seconds%3600/60),rest=seconds%60;
  return (hours?hours+' 小时 ':'')+(minutes?minutes+' 分 ':'')+rest+' 秒';
}

function updateJob(job) {
  progressBar.parentElement.classList.remove('indeterminate');
  const pct = Math.round((job.progress || 0) * 100);
  progressBar.style.width = `${pct}%`;
  const seconds=job.elapsed_seconds ?? (job.finished && job.started ? Math.max(0,job.finished-job.started):null);
  jobMsg.textContent = (job.message || job.status)+(job.status==='done' && seconds!=null?' · 用时 '+formatElapsed(seconds*1000):'');
  if (job.status === "error") progressBar.style.background = "var(--red)";
  else if (job.status === "done") progressBar.style.background = "var(--green)";
  else progressBar.style.background = "var(--accent)";
  updateButtons();
}

async function cancelAsr() {
  if(state.batchRecognition)state.batchCancelRequested=true;
  if (!state.jobId) return;
  try {
    await api(`/api/jobs/${state.jobId}/cancel`, {});
    toast("已请求取消");
  } catch (e) { stopWaitingForJob(); toast('未能确认后台取消，已停止本页等待；可继续查询原任务', 'error'); }
}

async function rerunSelected() {
  if(editLocked())return;
  const indices=selectedCueIndices();
  if(!indices.length || !state.media || !modelAvailable())return;
  const targets=indices.slice().reverse().map(index=>({index,start:state.cues[index].start,end:state.cues[index].end}));
  const before=snapshot(),history=state.history.slice(),path=projectPath(),epoch=state.mediaEpoch;
  let done=0;
  state.batchRecognition=true;state.batchCancelRequested=false;updateButtons();
  try {
    // 从后向前逐条处理，后面的字幕即使拆成多条，也不会改变前面待处理条目的索引。
    for(const target of targets){
      if(state.batchCancelRequested || projectPath()!==path || state.mediaEpoch!==epoch)break;
      const job=await startAsr('region',target,{batch:true,deferCompletionSound:true});
      if(job?.status!=='done')break;
      done++;
    }
  } finally {
    state.batchRecognition=false;
    if(projectPath()===path && state.mediaEpoch===epoch && snapshot()!==before){
      state.history=history;pushHistory(before);scheduleSave();
    }
    updateButtons();
    jobMsg.textContent=`重识别${done===targets.length?'完成':state.batchCancelRequested?'已取消':'已停止'} · ${done}/${targets.length} 条`;
    if(done===targets.length)playSound('success');
  }
}

function toggleSelectedExport() {
  if(editLocked())return;
  const indices=selectedCueIndices();if(!indices.length)return;
  const include=indices.every(i=>cueExcluded(state.cues[i])),before=snapshot();
  for(const i of indices){
    const cue=state.cues[i];cue.exclude_export=!include;cue.review_decision=include?'included':'non_script';
    delete cue.interjection_excluded;
    delete cue.verification_excluded;
    if(include && cue.hallucination){cue.hallucination={...cue.hallucination,dismissed:true};cue.is_noise=false;}
  }
  pushHistory(before);renderAll();scheduleSave();updateOverlay();
  toast(`已将 ${indices.length} 条字幕${include?'恢复导出':'标为不导出'}`,'success');
}

// ---------- 分段/编辑操作 ----------
async function doSegment() {
  const selected = selectedCueIndices();
  if (editLocked() || selected.length !== 1) return;
  const index = selected[0], original = state.cues[index];
  const start = Number(original.start), end = Number(original.end);
  if (end - start < 0.02) { toast('字幕时间过短，无法分段', 'error'); return; }
  const before = snapshot();
  const path=projectPath(),epoch=state.mediaEpoch;
  state.operation='分段';updateButtons();
  try{
    const result=await api('/api/segment',{cues:[original],max_width:state.settings.width,mode:'midpoint'});
    if(!sameDocument(path,epoch,before))throw new Error('字幕已变化，已保留当前内容');
    if(!result.splits){toast('没有可拆开的完整词边界，已保留原句','info');return;}
    result.cues.forEach(cue=>{delete cue.merge_suggestion;completeModification(cue);});
    const all=state.cues.slice();all.splice(index,1,...result.cues);
    replaceCues(all,{before});selectCue(index,false);
    if(state.script.length){
      const missing=result.cues.map((cue,i)=>cue.reference_match?-1:index+i).filter(i=>i>=0);
      if(missing.length)await rematchDerivedCues(missing,{preserveModification:true});
    }
    toast('已在中间附近独立分段，后面的原字幕保持不变','success');
  }catch(error){toast(error.message,'error');}
  finally{state.operation='';updateButtons();}
}

async function doMerge() {
  const indices=selectedCueIndices();
  if(editLocked() || indices.length<2) return;
  if(!contiguousSelection(indices)){toast('请选择两条或多条连续相邻字幕合并','error');return;}
  const before=snapshot(), index=indices[0], cues=indices.map(i=>state.cues[i]);
  const first=cues[0], last=cues.at(-1);
  const sources=[...new Map(cues.flatMap(c=>c.asr_sources || [c.asr_original]).filter(Boolean)
    .map(c=>[JSON.stringify([c.id,c.start,c.end,c.text]),c])).values()];
  const starts=cues.map(c=>Number(c.start)).filter(Number.isFinite);
  const ends=cues.map(c=>Number(c.end)).filter(Number.isFinite);
  const originalStarts=cues.map(c=>Number(c.original_start ?? c.start)).filter(Number.isFinite);
  const originalEnds=cues.map(c=>Number(c.original_end ?? c.end)).filter(Number.isFinite);
  const start=starts.length?Math.min(...starts):first.start, end=ends.length?Math.max(...ends):last.end;
  const originalStart=originalStarts.length?Math.min(...originalStarts):first.original_start;
  const originalEnd=originalEnds.length?Math.max(...originalEnds):last.original_end;
  const words=cues.every(c=>Array.isArray(c.words)&&c.words.length)?cues.flatMap(c=>c.words):[];
  const merged={...first,start,end,text:cues.map(c=>c.text).join(''),
    original_text:sources.map(c=>c.text).join(''),original_start:originalStart,original_end:originalEnd,
    asr_sources:sources,
    asr_original:{text:sources.map(c=>c.text).join(''),start:originalStart,end:originalEnd,id:first.id},
    words,edited:true,source:'merged',width_split:false,manual_split:false,
    script_text:'',script_index:null,reference_match:null,match_ratio:null,match_needs_review:false,
    alignment:first.alignment?{...first.alignment,stale:true}:null,is_noise:cues.some(c=>c.is_noise),
    needs_review:cues.some(c=>c.needs_review),
    // 自动变灰只是校验结论，合并后短碎片问题已经解决；只要有一条手动“不导出”，合并结果继续不导出。
    exclude_export:cues.some(c=>cueExcluded(c)&&!c.verification_excluded),review_decision:'manual'};
  delete merged.merge_suggestion;
  delete merged.verification_excluded;
  delete merged.segmentation;
  delete merged.split_parent;
  delete merged.pause_after;
  if (Number.isFinite(last.pause_after)) merged.pause_after=last.pause_after;
  if (!(merged.end>merged.start)){toast('合并后的结束时间必须晚于开始时间','error');return;}
  const all=state.cues.slice();all.splice(index,indices.length,merged);
  // 相邻条目上的旧建议可能指向刚被合并的编号，避免继续显示过期提示。
  for(const neighbor of [all[index-1],all[index+1]]) if(neighbor) delete neighbor.merge_suggestion;
  completeModification(merged);
  replaceCues(all,{before});selectCue(index,false);toast(`已合并选中的 ${indices.length} 条字幕`,'success');
  if (state.script.length) await rematchDerivedCues([index],{preserveModification:true});
}

async function resetCues() {
  if(editLocked())return;
  const before=snapshot(),path=projectPath(),epoch=state.mediaEpoch;
  state.operation='重置';updateButtons();
  try{
    let initial=state.initialCues;
    if(!initial && state.subtitleFile?.path){
      initial=(await api('/api/srt/parse',{path:state.subtitleFile.path})).cues;
    }
    if(!sameDocument(path,epoch,before))throw new Error('字幕已变化，请重新重置');
    if(!initial)throw new Error('旧工程没有初始记录，请重新导入原始 SRT 后再使用重置');
    state.initialCues=JSON.parse(JSON.stringify(initial));
    replaceCues(JSON.parse(JSON.stringify(initial)),{before});toast('已恢复全部初始字幕，可撤销','success');
  }catch(error){toast(error.message,'error');}
  finally{state.operation='';updateButtons();}
}

function addCue() {
  if (editLocked()) return;
  const before = snapshot();
  const idx = state.selected >= 0 ? state.selected + 1 : state.cues.length;
  const prev = state.cues[idx - 1];
  const next=state.cues[idx], start=prev?prev.end:(video.currentTime || 0);
  const end=Math.min(start+2,next?.start ?? Infinity,state.media?.duration || Infinity);
  if(end<=start){toast('此处没有空余时间，请先拉开相邻字幕或缩短上一条','error');return;}
  state.cues.splice(idx, 0, normalizeCue({
    start, end, text: '', edited: true, source: "manual",
    display_id:String(Math.max(0,...state.cues.map(c=>parseInt(c.display_id,10)||0))+1),
  }, idx));
  pushHistory(before);
  state.selected = idx;
  state.selectedCues=new Set([idx]);
  renderAll();
  scheduleSave();
  const row = document.querySelector(`#cueBody tr[data-i="${idx}"] .cue-text`);
  if (row) {
    row.focus();
    const range = document.createRange();
    range.selectNodeContents(row);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
}

function deleteCue() {
  if (editLocked()) return;
  const indices=selectedCueIndices();if(!indices.length)return;
  const before = snapshot();
  const deleted=new Set(indices);state.cues=state.cues.filter((cue,i)=>!deleted.has(i));
  state.cues.forEach(cue=>delete cue.merge_suggestion);
  pushHistory(before);
  state.selected = -1;
  state.selectedCues=new Set();
  renderAll();
  scheduleSave();
}

async function importSrt(file = null) {
  if (editLocked()) return;
  state.operation = '导入字幕'; updateButtons();
  const source = projectPath();
  const requestedMode = state.settings.mode;
  try {
    const picked = file instanceof File ? await uploadFile(file, 'srt', ratio => {
      progressBar.style.width = Math.round(ratio * 100) + '%'; jobMsg.textContent = '导入字幕 ' + Math.round(ratio * 100) + '%';
    }) : await api('/api/dialog', { kind: 'srt', multiple: false });
    if (!picked.path) return;
    if (source && !(await saveProject(false))) throw new Error('当前工程尚未保存，已取消导入');
    const parsed = await api('/api/srt/parse', { path: picked.path });
    if (!parsed.cues?.length) throw new Error('没有可导入的字幕');
    let cues = parsed.cues, restored = false, initial=parsed.cues;
    if (!state.media) {
      const loaded = await api('/api/projects/load?path=' + encodeURIComponent(picked.path));
      state.mediaEpoch++;
      state.srtPath = picked.path;
      state.script = loaded.project?.script_lines || [];
      state.scriptDocument = loaded.project?.script_document || null;
      state.scriptName = loaded.project?.script_name || '';
      state.scriptNames = restoredScriptNames(loaded.project);
      projectVersions.set(picked.path, loaded.project?.revision || 0);
      if (loaded.project) {
        cues = loaded.project.cues || [];
        initial=loaded.project.initial_cues || parsed.cues;
        if (loaded.project.settings) applyProjectSettings(loaded.project.settings);
        restored = true;
      }
    }
    if (requestedMode === 'align') setMode('align', false);
    replaceCues(cues, { reset: !state.media, subtitleFile: { path: picked.path, origin: 'imported' } });
    state.initialCues=JSON.parse(JSON.stringify(initial));
    renderSubtitleSource(true);
    if (!(await saveProject(false))) throw new Error('字幕已导入，但工程尚未保存，请重试');
    const flagged = cues.filter(c => c.hallucination && !c.hallucination.dismissed && c.exclude_export).length;
    toast((restored ? '已恢复SRT工程：' : '已导入：') + cues.length + ' 条'
      + (parsed.warnings?.length ? '，有格式提醒' : '')
      + (flagged ? `（${flagged} 条疑似幻觉已默认不导出）` : ''), 'success', flagged ? 9000 : 3500);
  } catch (error) { toast(error.message, 'error'); }
  finally { state.operation = ''; updateButtons(); }
}

function showExportOptions() {
  if(editLocked() || !state.cues.length)return;
  $('#exportFormat option[value="mkv"]').disabled=!state.media;
  if(!state.media)$('#exportFormat').value='srt';
  window.SubtitlePickers?.syncAll();
  $('#exportDialog').showModal();
}

async function exportSrt(silent = false, format = 'srt') {
  if (editLocked() && !silent) return null;
  if (!state.cues.length) { toast("没有可导出的字幕", "error"); return null; }
  const s = collectSettings();
  const cues = state.cues.filter(c=>!cueExcluded(c)).map((c) => ({ start: c.start, end: c.end, text: displayedSubtitleText(c.text), role: c.role || "" }));
  if(!cues.length){toast('没有可导出的字幕，全部条目已标为不导出','info');return null;}
  if(!silent){state.operation='导出';updateButtons();jobMsg.textContent=format==='mkv'?'正在封装 MKV 视频、音轨和字幕…':'正在导出字幕…';}
  try {
    let outputDir=null;
    if(!silent){
      const picked=await api('/api/dialog',{kind:'dir',purpose:'export',title:format==='mkv'?'选择 MKV 导出文件夹':'选择字幕导出文件夹'});
      outputDir=picked.path || picked.paths?.[0];
      if(!outputDir)return null;
    }
    const d = await api("/api/srt/export", {
      format,
      media_path: state.media ? state.media.path : "",
      cues,
      options: { keep_punctuation: s.keepPunc,
        single_line: !s.wrapSubtitles, output_dir: outputDir },
    },null,{timeout:format==='mkv'?600000:30000});
    if(!silent){
      state.lastExport=d;
      $('#exportResult').hidden=false;
      $('#exportResultPath').textContent=d.path;
      $('#exportResultPath').title=d.path;
    }
    if (!silent) { toast(`已导出 ${d.count} 条 → ${d.path}`, "success", 7000); playSound("success"); }
    return d;
  } catch (e) {
    if (e.errors && e.errors.length) {
      toast(`导出检查未通过：${e.errors.slice(0, 3).join("；")}`, "error", 8000);
    } else {
      toast(e.message, "error");
    }
    return null;
  } finally {
    if(!silent){state.operation='';updateButtons();jobMsg.textContent='等待任务';}
  }
}

// ---------- 时间轴 ----------
function waveformHeights(peaks, first, last, count, gain=1) {
  const columns=new Float32Array(count);
  if(!peaks.length || !count)return columns;
  for(let x=0;x<count;x++){
    const a=clamp(Math.floor(first+(last-first)*x/count),0,peaks.length);
    const b=clamp(Math.max(a+1,Math.ceil(first+(last-first)*(x+1)/count)),0,peaks.length);
    let peak=0;for(let i=a;i<b;i++)peak=Math.max(peak,Number(peaks[i]) || 0);
    columns[x]=peak;
  }
  const ordered=Array.from(columns).sort((a,b)=>a-b);
  const reference=Math.max(.01,ordered[Math.floor((ordered.length-1)*.98)] || 0);
  return columns.map(peak=>peak<.0001?0:clamp(Math.pow(peak/reference,.82)*gain,0,1));
}

function drawWave() {
  const view = waveWindow();
  updatePlayhead();
  const pan=$('#wavePan');
  $('#wavePanExtent').style.width=Math.max(pan.clientWidth,pan.clientWidth*view.duration/view.span)+'px';
  pan.scrollLeft=view.start/view.span*pan.clientWidth;
  pan._syncedLeft=pan.scrollLeft;
  const css = getComputedStyle(themeRoot);
  const color = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
  const now = video.currentTime || 0;
  $('#waveReadout').textContent = fmtTime(now) + ' / ' + fmtTime(view.duration);
  const detail = detailCovering(view);
  if (detail) state.detailWave = detail;
  const useDetail = !!detail;
  for (const canvas of [waveCanvas]) {
    if (!canvas || !canvas.clientWidth) continue;
    const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight || 96;
    const resized = canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr);
    // 局部波形还没到时不重画：保留上一帧，避免“全片波形 → 局部波形”闪一下。
    if (!resized && state.waveDefer && state.wavePainted && !useDetail) continue;
    if (resized) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
    const ctx = canvas.getContext('2d'); if (!ctx) continue;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = color('--panel-2', '#232327'); ctx.fillRect(0, 0, w, h);
    const xAt = time => (time - view.start) / view.span * w;
    const step = [0.1, .2, .5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600].find(value => value >= view.span / Math.max(2, w / 75)) || 7200;
    ctx.font = '10px ' + getComputedStyle(document.body).fontFamily; ctx.textBaseline = 'top';
    for (let t = Math.ceil(view.start / step) * step; t <= view.start + view.span + .00001; t += step) {
      const x = xAt(t); ctx.strokeStyle = color('--line', '#34343a'); ctx.beginPath(); ctx.moveTo(x, 20); ctx.lineTo(x, h); ctx.stroke();
      const tenths = Math.round(t * 10), seconds = Math.round(t);
      const label = step < 1 ? Math.floor(tenths / 600) + ':' + pad(Math.floor(tenths / 10) % 60) + '.' + tenths % 10
        : Math.floor(seconds / 60) + ':' + pad(seconds % 60);
      ctx.fillStyle = color('--muted', '#a6a6ad'); ctx.fillText(label, clamp(x + 3, 1, Math.max(1, w - ctx.measureText(label).width - 2)), 3);
    }
    const wave = useDetail ? detail : state.waveform;
    const peaks = wave?.peaks || [], peakStart = useDetail ? detail.start : 0;
    const peakSpan = useDetail ? detail.end - detail.start : view.duration;
    const main=canvas===waveCanvas, top=main?72:24, waveformHeight=Math.max(12,h-top-12);
    if(main){
      ctx.strokeStyle=color('--line','#333');ctx.beginPath();ctx.moveTo(0,57);ctx.lineTo(w,57);ctx.stroke();
      ctx.fillStyle=color('--muted-2','#999');ctx.fillText('音频',6,60);
    }
    const selected=state.cues[state.selected];
    if(selected){
      ctx.fillStyle=color('--accent','#7f9fc5');ctx.globalAlpha=.08;
      ctx.fillRect(clamp(xAt(selected.start),0,w),top,Math.max(0,clamp(xAt(selected.end),0,w)-clamp(xAt(selected.start),0,w)),waveformHeight);
      ctx.globalAlpha=1;
    }
    ctx.fillStyle = color('--text', '#d4d4d4');ctx.globalAlpha=.78;
    if (peaks.length && view.duration) {
      const columns=waveformHeights(peaks,(view.start-peakStart)/peakSpan*peaks.length,(view.start+view.span-peakStart)/peakSpan*peaks.length,Math.ceil(w),state.settings.waveGain || 1);
      if (state.settings.waveStyle === 'bar') {
        for (let x = 0; x < w; x++) {
          const height = Math.max(1, columns[x] * waveformHeight*.94);
          ctx.fillRect(x, top + (waveformHeight - height) / 2, 1, height);
        }
      } else {
        // 正常波浪：邻域平滑后用对称二次曲线填充包络，避免逐列锯齿。
        const smooth=new Float32Array(w);
        for (let x = 0; x < w; x++) {
          let sum=0, count=0;
          for (let k = -2; k <= 2; k++) {
            const j=x+k;
            if (j>=0 && j<w) { sum+=columns[j]; count++; }
          }
          smooth[x]=sum/count;
        }
        const mid=top+waveformHeight/2, amp=waveformHeight*.47;
        const topAt=x=>mid-smooth[x]*amp, botAt=x=>mid+smooth[x]*amp;
        ctx.beginPath();
        ctx.moveTo(0, topAt(0));
        for (let x = 1; x < w; x++) {
          const px=x-1, cx=x-.5;
          ctx.quadraticCurveTo(px, topAt(px), cx, (topAt(px)+topAt(x))/2);
        }
        ctx.lineTo(w-1, topAt(w-1));
        ctx.lineTo(w-1, botAt(w-1));
        for (let x = w-1; x > 0; x--) {
          const px=x-1, cx=x-.5;
          ctx.quadraticCurveTo(px, botAt(px), cx, (botAt(px)+botAt(x))/2);
        }
        ctx.lineTo(0, botAt(0));
        ctx.closePath();
        ctx.fill();
      }
    } else { ctx.globalAlpha = .4; ctx.fillRect(0, top + waveformHeight / 2, w, 1); ctx.globalAlpha = 1; }
    ctx.globalAlpha=1;
    if(selected){
      ctx.strokeStyle=color('--accent-2','#a7c1e3');ctx.globalAlpha=.7;ctx.setLineDash([3,3]);
      for(const time of [selected.start,selected.end]){
        if(time<view.start || time>view.start+view.span)continue;
        const x=xAt(time);ctx.beginPath();ctx.moveTo(x,55);ctx.lineTo(x,h);ctx.stroke();
      }
      ctx.setLineDash([]);ctx.globalAlpha=1;
    }
    if (view.duration) {
      const timing=new Map(presentationCues().map(c=>[c._sourceIndex,c]));
      state.cues.forEach((cue, index) => {
        const span=main?(timing.get(index)||cue):cue;
        if (span.end < view.start || span.start > view.start + view.span) return;
        const x1 = clamp(xAt(span.start), 0, w), x2 = clamp(xAt(span.end), 0, w);
        if(main){
          const width=Math.max(1,x2-x1-1);
          ctx.fillStyle=color(cueExcluded(cue)?'--line-2':'--accent','#7f9fc5');ctx.globalAlpha=cueExcluded(cue) ? .15 : .22;
          ctx.beginPath();ctx.roundRect(x1,25,width,27,3);ctx.fill();ctx.globalAlpha=1;
          const coreStart=clamp(xAt(cue.start),x1,x2),coreEnd=clamp(xAt(cue.end),x1,x2);
          ctx.globalAlpha=cueExcluded(cue) ? .2 : (index===state.selected ? .65 : .4);
          ctx.fillRect(coreStart,25,Math.max(0,coreEnd-coreStart-1),27);ctx.globalAlpha=1;
          if(index===state.selected && (!editLocked() || state.operation==='校时')){
            ctx.fillStyle=color('--accent-2','#a7c1e3');
            if(cue.start>=view.start && cue.start<=view.start+view.span)ctx.fillRect(coreStart,25,5,27);
            if(cue.end>=view.start && cue.end<=view.start+view.span)ctx.fillRect(coreEnd-5,25,5,27);
          }
          if(width>16){
            ctx.save();ctx.beginPath();ctx.rect(x1+3,26,Math.max(1,width-6),24);ctx.clip();
            ctx.fillStyle=color('--text','#ddd');ctx.fillText(subtitleText(cue.text).replace(/\s+/g,' '),x1+5,32);ctx.restore();
          }
          return;
        }
        ctx.fillStyle = color('--accent-2', '#66b3ff'); ctx.globalAlpha = index === state.selected ? .25 : .1;
        ctx.fillRect(x1, 22, Math.max(1, x2 - x1), h - 22); ctx.globalAlpha = 1;
        ctx.fillRect(x1, h - 8, Math.max(1, x2 - x1), 5);
        if (index === state.selected) { ctx.fillRect(xAt(cue.start) - 2, 22, 4, h - 22); ctx.fillRect(xAt(cue.end) - 2, 22, 4, h - 22); }
      });
      if (canvas !== waveCanvas && now >= view.start && now <= view.start + view.span) { ctx.fillStyle = color('--red', '#ff453a'); ctx.fillRect(xAt(now), 0, 1.5, h); }
    }
    canvas.setAttribute('aria-valuemax', String(view.duration)); canvas.setAttribute('aria-valuenow', now.toFixed(2));
    state.wavePainted = true;
  }
}

async function saveProject(silent = false, options = {}) {
  const source = projectPath();
  if (!source) {
    if (state.cues.length) { setSaveState('字幕未保存，请先导出SRT'); return false; }
    return true;
  }
  clearTimeout(state.saveTimer);
  const revision = state.editRevision;
  const payload = JSON.parse(JSON.stringify({ media_path: state.media?.path || '', source_path: source,
    cues: state.cues, initial_cues: state.initialCues, script_lines: state.script, script_name: state.scriptName || '', script_names: state.scriptNames || [], script_document: state.scriptDocument,
    subtitle_file: state.subtitleFile, settings: collectSettings() }));
  state.pendingSaves++;
  if (!silent) setSaveState('保存中…');
  const task = projectSaveChain.then(async () => {
    try {
      if (projectConflicts.has(source)) { setSaveState('保存冲突，请备份本页并载入最新工程'); return false; }
      payload.expected_revision = projectVersions.get(source) || 0;
      const result = await api('/api/projects/save', payload, null, options);
      projectVersions.set(source, result.revision);
      if (projectPath() === source && state.editRevision === revision) {
        state.dirty = false;
        if (!silent) setSaveState('已保存 ' + String(result.updated_at).slice(11));
      }
      return true;
    } catch (error) {
      if (error.status === 409) projectConflicts.add(source);
      if (projectPath() === source) {
        state.dirty = true; setSaveState('保存失败，修改仍保留');
        $('#btnResolveConflict').hidden = !projectConflicts.has(source);
        if (!silent || error.status === 409) toast(error.message, 'error', 8000);
      }
      return false;
    } finally { state.pendingSaves--; }
  });
  projectSaveChain = task.catch(() => false);
  return task;
}

async function resolveProjectConflict() {
  if (editLocked()) return;
  const source = projectPath();
  if (!projectConflicts.has(source)) return;
  if (!await confirmDialog('将先备份本页的完整工程，再载入另一页面保存的最新版本。备份保存在 data/projects/backups，可用于恢复本页修改。是否继续？')) return;
  const before = snapshot(), epoch = state.mediaEpoch;
  state.operation = '处理保存冲突'; updateButtons();
  clearTimeout(state.saveTimer);
  try {
    await projectSaveChain;
    const payload = {source_path:source, media_path:state.media?.path || '', cues:state.cues,
      initial_cues:state.initialCues, script_lines:state.script, script_name:state.scriptName,
      script_names:state.scriptNames, script_document:state.scriptDocument,
      subtitle_file:state.subtitleFile, settings:collectSettings()};
    const backup = await api('/api/projects/backup', payload);
    const loaded = await api('/api/projects/load?path='+encodeURIComponent(source)), p = loaded.project;
    if (!p) throw new Error('最新工程不存在，本页修改及备份已保留');
    if (!sameDocument(source,epoch,before)) throw new Error('本页内容已变化，已保留当前修改及备份，请重新处理');
    state.script = p.script_lines || []; state.scriptDocument = p.script_document || null;
    state.scriptName = p.script_name || ''; state.scriptNames = restoredScriptNames(p);
    state.initialCues = p.initial_cues?.map(normalizeCue) || null;
    state.subtitleFile = p.subtitle_file?.path ? p.subtitle_file : null;
    if (p.settings) applyProjectSettings(p.settings);
    state.cues = (p.cues || []).map(normalizeCue);
    state.history = []; state.redoStack = []; state.selected = -1; state.selectedCues = new Set();
    state.editBefore = null; state.pendingResult = null; state.referenceGaps = [];
    state.editRevision++; state.dirty = false;
    projectVersions.set(source,loaded.revision ?? p.revision ?? 0);
    projectConflicts.delete(source); $('#btnResolveConflict').hidden = true;
    renderAll(); updateOverlay(); setSaveState('已载入最新工程');
    toast('本页工程已备份：'+backup.path, 'success', 10000);
  } catch (error) { toast(error.message, 'error', 8000); }
  finally { state.operation = ''; updateButtons(); }
}

function scheduleSave() {
  if (!projectPath() && !state.cues.length && !state.script.length) return;
  state.editRevision++; state.dirty = true;
  setSaveState(projectPath() ? '未保存' : '字幕未保存，请先导出SRT');
  clearTimeout(state.saveTimer);
  if (projectPath()) state.saveTimer = setTimeout(() => saveProject(false), SAVE_DELAY);
}

async function saveAll() {
  if (state.uploading || state.loading) { toast('素材还在导入中，请稍候', 'error'); return false; }
  clearTimeout(state.saveTimer); clearTimeout(state.configTimer);
  const projectOk = await saveProject(false), configOk = await saveConfig(false);
  if (projectOk && configOk && !state.dirty) setSaveState('已保存 ' + new Date().toTimeString().slice(0, 8));
  else if (!projectOk || !configOk) setSaveState('保存失败，修改仍保留');
  return projectOk && configOk && !state.dirty;
}

async function showProjectLibrary() {
  if(editLocked())return;
  $('#projectList').textContent='正在读取…';$('#projectDialog').showModal();
  try{
    const result=await api('/api/projects');state.recentProjects=result.projects || [];
    state.projectDirectory=result.directory||'';
    $('#projectList').innerHTML=state.recentProjects.map((p,i)=>'<button type="button" data-project="'+i+'" '+(!p.available?'disabled':'')+' title="'+escapeHtml(p.source_path)+'"><b>'+escapeHtml(p.name)+'</b><span>'+escapeHtml(p.updated_at)+' · '+p.count+' 条字幕'+(!p.available?' · 素材已移动或不存在':'')+'</span></button>').join('') || '<p class="muted">暂无保存的项目</p>';
  }catch(error){$('#projectList').textContent=error.message;}
}

async function openSavedProject(source) {
  if(editLocked())return;
  if(!/\.srt$/i.test(source)){
    if(await loadMedia(source,{restore:true}))scheduleConfigSave();
    return;
  }
  state.operation='打开项目';updateButtons();
  try{
    if(projectPath() && !(await saveProject(false)))throw new Error('当前项目保存失败，已保留当前内容');
    const result=await api('/api/projects/load?path='+encodeURIComponent(source)),p=result.project;
    if(!p)throw new Error('项目记录不存在');
    video.pause();video.removeAttribute('src');video.load();$('#videoEmpty').hidden=false;
    state.mediaEpoch++;state.media=null;state.srtPath=source;state.waveform=null;resetWaveDetail();state.waveSpan=0;state.waveStart=0;
    state.script=p.script_lines || [];state.scriptDocument=p.script_document || null;state.scriptName=p.script_name || '';state.scriptPath='';
    state.scriptNames=restoredScriptNames(p);
    state.initialCues=p.initial_cues?.map(normalizeCue) || null;
    state.referenceGaps=[];state.pendingResult=null;state.playingIndex=-1;
    projectVersions.set(source,result.revision ?? p.revision ?? 0);
    if(p.settings)applyProjectSettings(p.settings);
    replaceCues(p.cues || [],{reset:true,subtitleFile:p.subtitle_file?.path?p.subtitle_file:{path:source,origin:'imported'}});
    scheduleConfigSave();toast('已打开项目：'+source.split(/[\\/]/).pop(),'success');
  }catch(error){toast(error.message,'error');}
  finally{state.operation='';updateButtons();}
}

function updateButtons() {
  const hasCues = state.cues.length > 0, hasScript = state.script.length > 0;
  const align = state.settings.mode === 'align', locked = editLocked();
  $('#btnPreviewTiming').disabled=locked||!hasCues;
  $('#btnApplyTiming').disabled=locked||!hasCues;
  $('#btnPreviewTiming').textContent=state.timingPreview?'结束预览':'预览微调';
  $('#btnPreviewTiming').setAttribute('aria-pressed',String(!!state.timingPreview));
  $('#timingPreviewNote').textContent=state.timingPreview?'正在预览微调，尚未应用；导出仍使用编辑区时间':'预览和导出使用编辑区时间；下列微调参数需手动应用';
  $('#btnResolveConflict').hidden = !projectConflicts.has(projectPath());
  $('#btnResolveConflict').disabled = locked;
  $('#btnOpenProject').disabled=locked;
  const indices=selectedCueIndices(), single=indices.length===1?state.cues[indices[0]]:null;
  const eligible=state.cues.map((cue,i)=>i);
  const allSelected=eligible.length>0 && eligible.every(i=>state.selectedCues.has(i));
  $('#selectAllCues').checked=allSelected;
  $('#selectAllCues').indeterminate=indices.length>0&&!allSelected;
  $('#selectAllCues').disabled=locked||!hasCues;
  $('#btnExport').disabled=locked||!hasCues;
  $('#btnSegment').disabled=locked||!single;
  $('#btnMerge').disabled=locked||!contiguousSelection(indices);
  $('#selectedCueLabel').textContent=indices.length===1?'选中 #'+single.display_id:indices.length?'已选 '+indices.length+' 条':'请选择字幕';
  $('#btnExcludeSelected').disabled=locked||!indices.length;
  $('#btnExcludeSelected').textContent=indices.length&&indices.every(i=>cueExcluded(state.cues[i]))?'开启导出':'关闭导出';
  $('#btnRerunSelected').disabled=locked||!indices.length||!state.media||!modelAvailable();
  $('#btnAddCue').disabled = locked || (!state.media && !hasCues);
  $('#btnDelete').disabled = locked || !indices.length;
  $('#btnUndo').disabled = locked || !state.history.length;
  $('#btnReset').disabled = locked || !(state.initialCues || state.subtitleFile?.path);
  $('#btnSourceSrt').disabled = locked || (!align && !hasCues);
  $('#btnRealignAll').disabled=locked||!state.media||!hasCues||!state.auxModels.some(m=>m.name==='qwen3-forcedaligner-0.6b'&&m.available);
  $('#btnCalibrate').disabled = locked || !(hasCues && align);
  $('#btnExtract').disabled = locked || (align
    ? !hasCues || !state.script.length
    : !state.media || !modelAvailable());
  $('#btnCancel').disabled = !state.jobId && !state.matchRequest && !state.batchRecognition && !downloadBusy();
  $('#btnCancel').hidden = !state.jobId && !state.matchRequest && !state.batchRecognition && !downloadBusy();
  $('#btnOpenFolder').disabled = !state.media || state.uploading || state.loading;
  $('#mediaClear').disabled = !!(state.jobId || state.submitting || state.operation || state.loading);
  $('#scriptClear').disabled = locked;
  $('#btnUseResult').hidden = !state.pendingResult || state.pendingResult.path !== projectPath();
  $('#btnUseResult').disabled = locked;
  for (const id of ['deviceSelect', 'gapInput', 'gapRange', 'widthInput', 'widthRange',
    'arabicNumbers', 'keepPunc', 'wrapSubtitles', 'subtitleLead', 'subtitleTail', 'subtitleMin', 'subtitleMax']) $('#' + id).disabled = locked;
  const gpuButton = $('#gpuDownload');
  if (gpuButton) gpuButton.disabled = locked || downloadBusy() || gpuButton.hidden || gpuButton.dataset.blocked === '1';
  $('#vadCheck').disabled = locked || align;
  $('#vadCheck').closest('label').dataset.tip = align ? '仅人声用于字幕提取；音轨对齐按当前字幕处理，此模式下不适用。' : '提取前检测人声区间，不改变原音频；边界判断自动估计底噪，轻声对白仍保留供模型识别。';
  $('#keepInterjections').disabled = locked;
  $('#keepInterjections').closest('label').dataset.tip = '开启后保留笑声、叹气、咂嘴和单独的“啊、哈哈、嗯？”等整条语气词，校准后正常导出；关闭时提取和校准会直接删除这些条目，并去掉句首、句末的“啊、呀、哎、唉”等语气词。参考中没有的语气词也会去掉；句中“你呀，就是……”这类语气词不受影响。';
  $('#subtitleMin').max=String(state.settings.subtitleMax>0?Math.min(10,state.settings.subtitleMax):10);
  for (const id of ['matchMinimum','matchHigh','matchPhonetic','matchAmbiguity','matchGroup']) $('#' + id).disabled = locked;
  $('#modelBtn').disabled = locked || !state.models.length;
  $('#auxBtn').disabled = locked || !state.auxModels.length;
  $$('.step-btn').forEach(button => {
    const param = PARAM_FIELDS.find(p => p.key === button.dataset.param);
    const value = Number($(param.input).value);
    button.disabled = locked || (button.dataset.dir === '1' ? value >= param.max : value <= param.min);
  });
  $$('.mode-btn').forEach(button => { button.disabled = locked; });
  cueBody.querySelectorAll('input.time').forEach(input => { input.disabled = locked; });
  cueBody.querySelectorAll('.cue-pick').forEach(input => { input.disabled = locked; });
  cueBody.querySelectorAll('.cue-text').forEach(cell => {
    cell.contentEditable = locked ? 'false' : 'true'; cell.setAttribute('aria-readonly', locked ? 'true' : 'false');
  });
  cueBody.querySelectorAll('button').forEach(button => { button.disabled = locked; });
  $('#mediaZone').setAttribute('aria-disabled', locked ? 'true' : 'false');
  $('#scriptZone').setAttribute('aria-disabled', locked || !align ? 'true' : 'false');
}

function bindEvents() {
  $('#btnPreviewTiming').addEventListener('click',()=>{
    if(editLocked())return;
    state.timingPreview=!state.timingPreview;updateButtons();updateOverlay();drawWave();
  });
  $('#btnApplyTiming').addEventListener('click',applyTimingSettings);
  $('#btnLocateExport').addEventListener('click',async()=>{
    if(!state.lastExport?.path)return;
    try{await api('/api/open_folder',{path:state.lastExport.path});}catch(error){toast(error.message,'error');}
  });
  $('#btnCopyExportPath').addEventListener('click',async()=>{
    if(!state.lastExport?.path)return;
    try{await navigator.clipboard.writeText(state.lastExport.path);toast('已复制导出路径','success');}
    catch{toast('无法访问剪贴板，请选择上方完整路径复制','error');}
  });
  $('#waveStyle').addEventListener('click',()=>{
    state.settings.waveStyle = state.settings.waveStyle === 'bar' ? 'wave' : 'bar';
    syncSettingsUI(); drawWave(); scheduleConfigSave();
  });
  $('#waveGain').addEventListener('input',e=>{state.settings.waveGain=Number(e.target.value);$('#waveGainValue').textContent=e.target.value+'×';drawWave();});
  $('#waveGain').addEventListener('change',()=>{collectSettings();scheduleConfigSave();});
  $('#btnOpenProject').addEventListener('click',showProjectLibrary);
  $('#projectList').addEventListener('click',e=>{const button=e.target.closest('[data-project]');if(!button||button.disabled)return;const p=state.recentProjects[Number(button.dataset.project)];if(p){$('#projectDialog').close();openSavedProject(p.source_path);}});
  $('#btnOpenProjectFolder').addEventListener('click', async () => {
    // 使用后端返回的工程存储目录，避免误打开素材文件夹。
    const target=state.projectDirectory||'';
    if(!target){toast('还没有可打开的项目文件夹','info');return;}
    try{await api('/api/open_folder',{path:target});}
    catch(error){toast(error.message,'error');}
  });
  $$(".mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
    btn.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key) || editLocked()) return;
      event.preventDefault();
      const buttons = $$(".mode-btn");
      const current = buttons.indexOf(btn);
      const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
        : (current + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
      setMode(buttons[next].dataset.mode); buttons[next].focus(); playSound("key");
    });
  });
  $("#btnSave").addEventListener("click", saveAll);
  $("#btnSourceSrt").addEventListener("click", () => state.settings.mode === 'align' ? importSrt() : exportSrt(false));
  $("#btnHelp").addEventListener("click", () => {
    const version = $("#helpVersion");
    if (version && state.healthVersion) version.textContent = state.healthVersion;
    $("#helpDialog").showModal();
  });
  $("#btnOpenTerminal").addEventListener("click", openTerminal);
  $("#btnOpenFolder").addEventListener("click", async () => {
    if (!state.media) return;
    try { await api("/api/open_folder", { path: state.media.path }); }
    catch (e) { toast(e.message, "error"); }
  });
  $("#btnExtract").addEventListener("click", () => state.settings.mode === 'align' ? verifyOnly() : startAsr());
  $("#btnCancel").addEventListener("click", async () => {
    if (state.matchRequest) await cancelReferenceMatch();
    else if (downloadBusy()) { await api('/api/models/cancel', {}); await pollModelDownload(); }
    else await cancelAsr();
  });
  $('#btnStopWaiting').addEventListener('click', stopWaitingForJob);
  $('#btnResolveConflict').addEventListener('click', resolveProjectConflict);
  $("#btnReconnect").addEventListener("click", () => {
    if (state.detachedJob && !editLocked()) {
      const pending = state.detachedJob;
      startAsr(pending.task,pending.region,{...pending.options,resume:pending});
    } else state.retryPoll?.();
    checkHealth();
  });
  $("#btnUseResult").addEventListener("click", async () => {
    const pending = state.pendingResult;
    if (editLocked() || !pending || pending.path !== projectPath()) return;
    replaceCues(pending.job.cues || [], { subtitleFile: pending.task === 'align' ? state.subtitleFile : pending.job.subtitle_path
      ? { path: pending.job.subtitle_path, origin: 'generated' } : null });
    renderSubtitleSource(true);
    state.pendingResult = null; updateButtons();
    if (await saveProject(false)) { playSound("success"); toast("已采用识别结果，可撤销", "success"); }
  });
  $("#btnRealignAll").addEventListener("click",alignAndVerify);
  $("#btnCalibrate").addEventListener("click", () => calibrateAll());
  $("#waveZoomIn").addEventListener("click", () => {
    const view = waveWindow(); if (!view.duration) return;
    state.waveSpan = Math.max(.5, view.span / 2); state.waveStart = Math.max(0, (video.currentTime || 0) - state.waveSpan / 2); requestWaveDetail();
  });
  $("#waveZoomOut").addEventListener("click", () => {
    const view = waveWindow(); state.waveSpan = Math.min(view.duration, view.span * 2); state.waveStart = Math.max(0, (video.currentTime || 0) - state.waveSpan / 2); requestWaveDetail();
  });
  $("#waveReset").addEventListener("click", () => { state.waveSpan = 0; state.waveStart = 0; requestWaveDetail(); });

  $("#btnSegment").addEventListener("click", doSegment);
  $("#btnMerge").addEventListener("click", doMerge);
  $("#btnAddCue").addEventListener("click", addCue);
  $("#btnDelete").addEventListener("click", deleteCue);
  $("#btnUndo").addEventListener("click", undo);
  $('#btnReset').addEventListener('click',resetCues);
  $('#selectAllCues').addEventListener('change',e=>selectAllCues(e.target.checked));
  $('#btnExcludeSelected').addEventListener('click',toggleSelectedExport);
  $('#btnRerunSelected').addEventListener('click',rerunSelected);
  $('#btnExport').addEventListener('click',showExportOptions);
  $('#btnConfirmExport').addEventListener('click',()=>{$('#exportDialog').close();exportSrt(false,$('#exportFormat').value);});

  $("#filterSelect").addEventListener("change", (e) => { state.filter = e.target.value; renderTable(); });
  let searchTimer;
  $("#searchInput").addEventListener("input", (e) => {
    state.search = e.target.value; searchCursor = -1; clearTimeout(searchTimer); searchTimer = setTimeout(renderTable, 120);
  });
  $("#searchInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      clearTimeout(searchTimer); renderTable();
      findCueMatch(event.shiftKey);
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.target.value = ""; state.search = ""; searchCursor = -1;
      clearTimeout(searchTimer); renderTable(); hideTip();
    }
  });

  for (const id of ["#deviceSelect", "#vadCheck", "#keepInterjections", "#arabicNumbers", "#keepPunc", "#wrapSubtitles", "#subtitleLead", "#subtitleTail", "#subtitleMin", "#subtitleMax"]) {
    $(id).addEventListener("change", commitSettings);
  }
  for (const param of PARAM_FIELDS) {
    const input = $(param.input);
    const range = $(param.range);
    input.addEventListener("input", () => syncParamControl(param));
    input.addEventListener("change", commitSettings);
    range.addEventListener("input", () => {
      if (Number(input.value) !== Number(range.value)) playSound("tick");
      input.value = range.value;
      syncParamControl(param);
      state.settings[param.key] = Number(range.value);
      updateWidthInfo();
      scheduleConfigSave();
      scheduleSave();
    });
    range.addEventListener("change", commitSettings);
  }
  $$(".step-btn").forEach((btn) => {
    const param = PARAM_FIELDS.find((item) => item.key === btn.dataset.param);
    const input = param && $(param.input);
    if (!input) return;
    btn.addEventListener("click", () => {
      syncParamControl(param);
      const next = clamp(
        (Number(input.value) || param.min) + Number(btn.dataset.dir) * param.step,
        param.min,
        param.max,
      );
      syncParamControl(param, next);
      commitSettings({target:input});
    });
  });

  $("#modelBtn").addEventListener("click", () => toggleModelMenu());
  $('#gpuDownload').addEventListener('click', downloadGpu);
  $('#auxBtn').addEventListener('click', () => toggleAuxMenu());
  $('#auxMenu').addEventListener('click', event => {
    const locate = event.target.closest('[data-locate]');
    if (locate && !locate.disabled) { locateModel(locate.dataset.locate); return; }
    const download = event.target.closest('[data-download]');
    if (download && !download.disabled) { downloadModel(download.dataset.download); return; }
    const option = event.target.closest('[data-aux]');
    if (option && !option.disabled) { state.settings.auxModel = option.dataset.aux; renderAuxModels(); toggleAuxMenu(false); scheduleConfigSave(); }
  });
  $("#modelMenu").addEventListener("click", (e) => {
    const locate = e.target.closest('[data-locate]');
    if (locate && !locate.disabled) { locateModel(locate.dataset.locate); return; }
    const download = e.target.closest('[data-download]');
    if (download && !download.disabled) { downloadModel(download.dataset.download); return; }
    const option = e.target.closest(".model-option");
    if (option && !option.disabled) chooseModel(option.dataset.name);
  });
  document.addEventListener("pointerdown", (e) => {
    if (!$("#modelPicker").contains(e.target)) toggleModelMenu(false);
    if (!$('#auxPicker').contains(e.target)) toggleAuxMenu(false);
  });
  $("#modelPicker").addEventListener("keydown", (e) => {
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
      e.preventDefault();
      if ($("#modelMenu").hidden) { toggleModelMenu(true); return; }
      const options = [...$("#modelMenu").querySelectorAll(".model-option:not(:disabled)")];
      if (!options.length) return;
      const index = options.indexOf(document.activeElement);
      const next = e.key === "Home" ? 0 : e.key === "End" ? options.length - 1
        : (index + (e.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
      options[next].focus(); playSound("tick");
    }
    if (e.key === "Escape") {
      e.preventDefault();
      toggleModelMenu(false);
      $("#modelBtn").focus();
    }
  });

  cueBody.addEventListener('pointerdown', e=>{
    state.additiveFocus=!!(e.ctrlKey||e.metaKey);
    if(state.additiveFocus && !e.target.closest('input,button'))e.preventDefault();
  });
  cueBody.addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-i]");
    if (!tr) return;
    if (e.target.closest("button")) return;
    const i = Number(tr.dataset.i);
    if(e.target.matches('.cue-pick')){
      if(editLocked())return;
      if(e.target.checked)state.selectedCues.add(i);else state.selectedCues.delete(i);
      selectCue(i,true,true);return;
    }
    if(e.ctrlKey || e.metaKey){
      if(state.selectedCues.has(i))state.selectedCues.delete(i);else state.selectedCues.add(i);
      selectCue(i,true,true);return;
    }
    selectCue(i, !e.target.closest('input, [contenteditable="true"]'));
  });

  cueBody.addEventListener("focusin", (e) => {
    if (editLocked()) return;
    if (e.target.matches(".cue-text, input.time")) {
      state.editBefore = snapshot();
      const tr = e.target.closest("tr[data-i]");
      if (tr && !state.additiveFocus) selectCue(Number(tr.dataset.i), false);
    }
  });

  cueBody.addEventListener("input", (e) => {
    if (editLocked()) return;
    if (!e.target.classList.contains("cue-text")) return;
    const tr = e.target.closest("tr[data-i]");
    if (!tr) return;
    const cue = state.cues[Number(tr.dataset.i)];
    if (!cue) return;
    const manualExcluded = cue.review_decision === 'non_script';
    const edited=mergeEditedCueText(cue.text,e.target.innerText.replace(/ /g, " "));
    if(edited===cue.text)return;
    cue.text = edited;
    delete cue.merge_suggestion;
    delete cue.interjection;
    if(cue.interjection_excluded){
      delete cue.interjection_excluded;
      if(!manualExcluded)cue.exclude_export=false;
    }
    if(cue.verification_excluded){
      delete cue.verification_excluded;
      if(!manualExcluded)cue.exclude_export=false;
    }
    completeModification(cue);
    cue.review_decision = manualExcluded ? 'non_script' : 'manual';
    // 用户亲手改过文字，说明不是幻觉原句：解除自动排除，但保留手动的“不导出”。
    if (cue.hallucination) {
      cue.hallucination = { ...cue.hallucination, dismissed: true };
      cue.is_noise = false;
      cue.exclude_export = manualExcluded;
      const badges = tr.querySelector('.badges');
      if (badges) badges.innerHTML = badgesHtml(cue);
      tr.classList.toggle('excluded', cueExcluded(cue));
    }
    if (cue.reference_match) cue.reference_match.stale = true;
    cue.words = [];
    const badges=tr.querySelector('.badges');if(badges)badges.innerHTML=badgesHtml(cue);
    const review=tr.querySelector('.cue-review');if(review)review.innerHTML=cueReviewHtml(cue,Number(tr.dataset.i));
    e.target.classList.remove('low','noise','context-warning');
    tr.classList.toggle('excluded', cueExcluded(cue));
    updateOverlay();
    scheduleSave();
  });

  cueBody.addEventListener("change", (e) => {
    if (editLocked()) return;
    if (!e.target.matches("input.time")) return;
    const tr = e.target.closest("tr[data-i]");
    if (!tr) return;
    const cue = state.cues[Number(tr.dataset.i)];
    if (!cue) return;
    const field = e.target.dataset.field;
    const value = parseTime(e.target.value);
    if (value == null || value < 0) {
      toast("时间格式无效", "error");
      e.target.value = fmtCueTime(cue[field]);
      return;
    }
    const old = cue[field];
    cue[field] = value;
    if (cue.end <= cue.start) {
      toast("结束时间必须晚于开始时间", "error");
      cue[field] = old;
      e.target.value = fmtCueTime(old);
      return;
    }
    const others=state.cues.filter(c=>c!==cue),anchor=field==='start'?old:cue.start;
    const previous=others.filter(c=>c.start<anchor).sort((a,b)=>a.end-b.end).at(-1);
    const next=others.filter(c=>c.start>=anchor).sort((a,b)=>a.start-b.start)[0];
    if((previous&&cue.start<previous.end)||(next&&cue.end>next.start)||(state.media?.duration&&cue.end>state.media.duration)){
      cue[field]=old;e.target.value=fmtCueTime(old);toast('时间不能与前后字幕重叠或超出视频','error');return;
    }
    delete cue.merge_suggestion;
    // 与改字一致：手工修正时间后不再沿用此前的自动变灰，手动“不导出”仍保留。
    if(cue.verification_excluded){
      delete cue.verification_excluded;
      if(cue.review_decision!=='non_script')cue.exclude_export=false;
    }
    completeModification(cue);cue.words=[];
    if(cue.alignment)cue.alignment.stale=true;
    const badges=tr.querySelector('.badges');if(badges)badges.innerHTML=badgesHtml(cue);
    const review=tr.querySelector('.cue-review');if(review)review.innerHTML=cueReviewHtml(cue,Number(tr.dataset.i));
    tr.querySelector('.cue-text')?.classList.remove('low','noise','context-warning');
    tr.classList.toggle('excluded',cueExcluded(cue));
    pushHistory(state.editBefore);
    state.editBefore = snapshot();
    scheduleSave();
    updateOverlay();
    drawWave();
  });

  cueBody.addEventListener("focusout", (e) => {
    if (editLocked()) { state.editBefore = null; return; }
    if (!e.target.matches(".cue-text, input.time")) return;
    if(e.target.matches('.cue-text')){
      const row=e.target.closest('tr[data-i]'),cue=row&&state.cues[Number(row.dataset.i)];
      if(cue)e.target.textContent=editableCueText(cue.text);
    }
    const changed = !!state.editBefore && snapshot() !== state.editBefore;
    if (changed) {
      state.history.push(state.editBefore);
      if (state.history.length > HISTORY_LIMIT) state.history.shift();
      state.redoStack = [];
    }
    state.editBefore = null;
    // 文本和时间编辑已在 input/change 中安排保存；只点进点出不应写工程文件。
    if (changed) scheduleSave();
    renderScript();
    updateButtons();
  });

  cueBody.addEventListener("keydown", (e) => {
    if (e.target.classList.contains("cue-text")) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        e.target.blur();
      } else if (e.key === "Escape") {
        e.preventDefault();
        if (state.editBefore) restoreSnapshot(state.editBefore);
        state.editBefore = null;
        renderTable();
        scheduleSave();
      }
    } else if (e.target.matches("input.time") && e.key === "Enter") {
      e.target.blur();
    }
  });

  cueBody.addEventListener("click", async (e) => {
    const row = e.target.closest('.script-line[data-i]');
    if (!row) return;
    const i = Number(row.dataset.i), cue = state.cues[i], action = e.target.closest('[data-review]')?.dataset.review;
    if (action && editLocked()) return;
    if (action === 'exclude') markReview(i, cueExcluded(cue) ? 'included' : 'non_script');
    else if (action === 'retranscribe') await startAsr('region',{index:i,start:cue.start,end:cue.end});
    else if (action === 'rematch') await matchScript(false, [i]);
    else if (['kept','non_script','skipped'].includes(action)) markReview(i, action);
    else if (action === 'edit') { selectCue(i, false); const cell = $(`#cueBody tr[data-i="${i}"] .cue-text`); cell?.scrollIntoView({block:'center'}); cell?.focus(); }
    else if (action === 'confirm') { const before = snapshot(); cue.reference_match.candidate_confirmed = true; pushHistory(before); renderScript(); scheduleSave(); }
    else if (action === 'candidate') {
      const candidate = cue.reference_match.alternatives[Number(e.target.closest('[data-candidate]').dataset.candidate)];
      const before = snapshot(); cue.script_text = candidate.text; cue.script_index = candidate.paragraph_start - 1;
      Object.assign(cue.reference_match, {reference:candidate, score:candidate.score, character_score:candidate.character_score, pinyin_score:candidate.pinyin_score, candidate_confirmed:true, differences:candidate.differences || []});
      pushHistory(before); renderAll(); scheduleSave();
    } else if (!e.target.closest('details,button,input,[contenteditable="true"]') && !e.ctrlKey && !e.metaKey) { focusWave(cue); }
  });
  cueBody.addEventListener('keydown', e => { if (e.target.matches('.script-line') && ['Enter',' '].includes(e.key)) { e.preventDefault(); selectCue(Number(e.target.dataset.i)); } });
  for (const key of ['matchMinimum','matchHigh','matchPhonetic','matchAmbiguity','matchGroup']) {
    $('#' + key).addEventListener('change', commitSettings);
  }

  bindWaveCanvas(waveCanvas);

  video.addEventListener("timeupdate", () => { updateOverlay(); if(followTimeline())drawWave(); updatePlayhead(); });
  video.addEventListener('seeked', () => { updateOverlay(); followTimeline(); drawWave(); });
  video.addEventListener('play', () => { if(state.playbackFrame===null)playbackTick(); });
  for(const event of ['pause','ended'])video.addEventListener(event,()=>{
    if(state.playbackFrame!==null)cancelAnimationFrame(state.playbackFrame);
    state.playbackFrame=null;updateOverlay();updatePlayhead();
  });
  video.addEventListener("loadedmetadata", () => {
    if (state.media && !state.media.duration) state.media.duration = video.duration || 0;
    applyPreviewShape();
    renderMediaMeta();
    drawWave();
  });
  video.addEventListener("error", () => {
    if (!state.media || state.videoErrorShown) return;
    state.videoErrorShown = true;
    toast("浏览器无法播放该格式，建议转成 MP4（H.264）后预览；不影响识别和导出", "error", 7000);
  });

  window.addEventListener("beforeunload", (event) => {
    if (state.dirty || state.pendingSaves || state.jobId) { event.preventDefault(); event.returnValue = ""; }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden" && state.dirty) {
      clearTimeout(state.saveTimer);
      saveProject(true, { keepalive: true });
    }
  });

  document.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    const editing = e.target.isContentEditable || tag === "input" || tag === "select" || tag === "textarea";
    if (e.defaultPrevented || e.isComposing) return;
    if (e.ctrlKey && e.key.toLowerCase() === "s") {
      e.preventDefault();
      saveAll();
      return;
    }
    /* 和浏览器查找一致：Ctrl+F 聚焦搜索框并全选当前关键词。 */
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
      e.preventDefault();
      const input = $("#searchInput");
      searchCursor = -1;
      input.focus();
      input.select();
      return;
    }
    if (editing) return;
    if (e.code === "Space") {
      if (e.target.closest?.('button,a[href],summary,[role="button"],[role="checkbox"],[role="slider"],video,dialog')) return;
      e.preventDefault();
      if (video.paused) video.play().catch(() => {}); else video.pause();
    } else if (e.ctrlKey && e.key.toLowerCase() === "z") {
      e.preventDefault();
      undo();
    } else if (e.ctrlKey && e.key.toLowerCase() === "y") {
      e.preventDefault();
      redo();
    }
  });

  bindDropzones();
  bindSplitters();
  bindTimelineLayout();
}

function revealApp() {
  clearTimeout(window.__bootTimer);
  document.documentElement.classList.remove("booting");
}

async function init() {
  let initialTheme = "system";
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (THEMES.includes(saved)) initialTheme = saved;
  } catch { /* ignore */ }
  applyTheme(initialTheme, false);
  bindThemeControls();
  bindFeedback();
  loadSettings();
  resetPanelWidths();
  bindTooltips();
  bindEvents();
  await loadConfig();
  collectSettings();
  renderAll();
  updateModeUI();
  applyLayout();
  revealApp();
  await checkHealth();
  pollModelDownload();
}

init();
