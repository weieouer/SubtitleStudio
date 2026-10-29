const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

let jobMode = "align";
let mediaFile = null;
let mediaLocalPath = "";
let mediaLocalSize = 0;
let mediaLocalRegistering = false;
let scriptFile = null;
let scriptInspectOk = false;
let scriptInspectData = null;
let scriptInspectEpoch = 0;
let mediaMetaTimer = 0;
let mediaUrl = null;
let appPaths = { outputs: "data/outputs" };
let currentJobId = null;
let pollTimer = null;
let jobEpoch = 0;
let jobRunning = false;
let activeUpload = null;
let pollFailures = 0;
let gpuFallbackNotified = false;
let resultData = null;
let previewCues = [];
let activeCueText = "";
let previewExpanded = false;
let overlayFrame = null;
let fileMenuState = null;
let modelStates = {};
let modelFetching = false;
let modelPollTimer = 0;

const THEME_KEY = "subtitle-studio-theme";
const EXPORT_KEY = "subtitle-studio-export-dir";
const MODE_KEY = "subtitle-studio-mode";
const LAST_JOB_KEY = "subtitle-studio-last-job";
const THEMES = ["system", "light", "dark"];
const themeRoot = document.documentElement;
const appearance = $(".appearance");
const themeButtons = $$(".appearance-btn");
let currentTheme = "system";

function applyTheme(theme) {
  if (!THEMES.includes(theme)) theme = "system";
  currentTheme = theme;
  if (theme === "system") themeRoot.removeAttribute("data-theme");
  else themeRoot.setAttribute("data-theme", theme);

  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch (error) {}

  appearance.style.setProperty("--theme-index", THEMES.indexOf(theme));
  themeButtons.forEach((button) => {
    const active = button.dataset.theme === theme;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function switchTheme(theme, useViewTransition = true) {
  if (theme === currentTheme) return;

  if (
    !useViewTransition ||
    !document.startViewTransition ||
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  ) {
    themeRoot.classList.add("theme-changing");
    applyTheme(theme);
    window.setTimeout(() => themeRoot.classList.remove("theme-changing"), 320);
    return;
  }

  try {
    document.startViewTransition(() => applyTheme(theme));
  } catch (error) {
    applyTheme(theme);
  }
}

themeButtons.forEach((button) => {
  button.addEventListener("click", () => switchTheme(button.dataset.theme));
});

let initialTheme = "system";
try {
  initialTheme = localStorage.getItem(THEME_KEY) || "system";
} catch (error) {}
applyTheme(initialTheme);

/* ---------- 灯绳：拉一下切换明暗 ---------- */

const lampCord = $("#lamp-cord");
const CORD_THRESHOLD = 17;
const CORD_RESIST_FROM = 25;
let cordPull = 0;
let cordVelocity = 0;
let cordFrame = 0;
let cordPulseTimer = 0;
let cordPointer = null;

function reduceMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function renderCord(pull) {
  cordPull = pull;
  const stretch = Math.max(0, pull);
  lampCord.style.setProperty("--cord-pull", `${pull.toFixed(2)}px`);
  lampCord.style.setProperty("--cord-stretch", `${stretch.toFixed(2)}px`);
}

function cordThemeTarget() {
  let dark = currentTheme === "dark";
  if (currentTheme === "system") {
    dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  return dark ? "light" : "dark";
}

function toggleThemeFromCord() {
  // 不走 view transition：让灯绳的回弹不被截图冻结。
  switchTheme(cordThemeTarget(), false);
}

function stopCordMotion() {
  if (cordFrame) cancelAnimationFrame(cordFrame);
  if (cordPulseTimer) window.clearTimeout(cordPulseTimer);
  cordFrame = 0;
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

    // 小步积分，保证低帧率时弹簧也不会发散。
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
    toggleThemeFromCord();
    return;
  }

  renderCord(14);
  lampCord.classList.add("is-pulling");
  cordPulseTimer = window.setTimeout(() => {
    cordPulseTimer = 0;
    toggleThemeFromCord();
    animateCordSpring(14, -18);
  }, 70);
}

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

  const raw = Math.max(
    0,
    cordPointer.startPull + event.clientY - cordPointer.startY
  );
  const pull =
    raw <= CORD_RESIST_FROM
      ? raw
      : CORD_RESIST_FROM + (raw - CORD_RESIST_FROM) * 0.24;

  const now = performance.now();
  const elapsed = Math.max(1, now - cordPointer.lastTime);
  const instant = ((pull - cordPull) / elapsed) * 1000;
  cordVelocity = cordVelocity * 0.7 + instant * 0.3;
  cordPointer.lastTime = now;
  if (Math.abs(event.clientY - cordPointer.startY) > 3) {
    cordPointer.moved = true;
  }
  renderCord(pull);
});

function endCord(event, cancelled) {
  if (!cordPointer || event.pointerId !== cordPointer.id) return;
  const wasMoved = cordPointer.moved;
  if (lampCord.hasPointerCapture?.(event.pointerId)) {
    lampCord.releasePointerCapture(event.pointerId);
  }
  cordPointer = null;

  if (!cancelled && cordPull >= CORD_THRESHOLD) {
    toggleThemeFromCord();
    animateCordSpring(cordPull, cordVelocity);
  } else if (!cancelled && !wasMoved) {
    pulseCord();
  } else {
    animateCordSpring(cordPull, cordVelocity);
  }
  cordVelocity = 0;
}

lampCord.addEventListener("pointerup", (event) => endCord(event, false));
lampCord.addEventListener("pointercancel", (event) => endCord(event, true));

lampCord.addEventListener("click", (event) => {
  // 鼠标/触控的一次轻触已经在上面的 pointerup 里处理。
  if (event.detail !== 0) {
    event.preventDefault();
    return;
  }
  pulseCord();
});

function rememberExportDir() {
  const value = $("#export-dir").value.trim();
  try {
    if (value) localStorage.setItem(EXPORT_KEY, value);
    else localStorage.removeItem(EXPORT_KEY);
  } catch (error) {}
}

try {
  const savedExportDir = localStorage.getItem(EXPORT_KEY);
  if (savedExportDir) $("#export-dir").value = savedExportDir;
} catch (error) {}
$("#export-dir").addEventListener("change", rememberExportDir);
$("#export-dir").addEventListener("blur", rememberExportDir);
$("#export-dir").addEventListener("input", renderExportHint);
$("#max-chars").addEventListener("input", () => {
  if (scriptInspectData) renderScriptMeta(scriptFile ? scriptFile.size : 0);
});

function shortDefaultPath(path) {
  const parts = String(path).split(/[\\/]+/).filter(Boolean);
  if (parts.length <= 4) return path;
  const sep = path.includes("\\") ? "\\" : "/";
  return `…${sep}${parts.slice(-3).join(sep)}`;
}

function renderExportHint() {
  const input = $("#export-dir");
  if (!input) return;
  const defaultPath = appPaths.outputs || "data/outputs";
  const custom = input.value.trim();
  input.placeholder = `默认 · ${shortDefaultPath(defaultPath)}`;
  input.title = custom || `留空时导出到 ${defaultPath}`;
}

async function loadPaths() {
  try {
    const response = await fetch("/api/paths");
    const data = await response.json().catch(() => ({}));
    if (response.ok && data.outputs) appPaths = data;
  } catch (error) {}
  renderExportHint();
}

async function copyText(text, message) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    toast(message);
  } catch (error) {
    toast("复制失败，请手动选择路径");
  }
}

function fmtSize(bytes) {
  if (!bytes && bytes !== 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(value >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtTime(seconds) {
  if (seconds === null || seconds === undefined) return "--:--";
  const total = Number(seconds);
  const m = Math.floor(total / 60);
  const s = total - m * 60;
  return `${String(m).padStart(2, "0")}:${s.toFixed(1).padStart(4, "0")}`;
}

function fmtDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "--:--";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

function fmtCount(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return "0";
  return Math.round(number).toLocaleString("zh-CN");
}

function setDropMeta(meta, text, state = "") {
  if (!meta) return;
  meta.textContent = text || "";
  meta.dataset.state = text ? state : "";
}

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.remove("hidden");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add("hidden"), 6000);
}

function selectedModelReady() {
  const state = modelStates[$("#model").value];
  return !state || state.status === "ready";
}

function setMode(mode, persist = true) {
  jobMode = mode === "extract" ? "extract" : "align";
  const extracting = jobMode === "extract";

  $$(".mode-btn").forEach((button) => {
    const active = button.dataset.mode === jobMode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });

  const scriptZone = $("#drop-script");
  scriptZone.classList.toggle("is-muted", extracting);
  scriptZone.setAttribute("aria-disabled", extracting ? "true" : "false");
  $("#script-input").disabled = extracting;
  $(".script-format").classList.toggle("is-muted", extracting);

  $$(".align-only").forEach((el) => {
    const control = el.querySelector("input, select");
    if (control) control.disabled = extracting;
    el.classList.toggle("is-muted", extracting);

    const tipEl = el.classList.contains("switches")
      ? el.querySelector(".switch")
      : el;
    if (!tipEl) return;
    if (!tipEl.dataset.tipAlign) tipEl.dataset.tipAlign = tipEl.dataset.tip || "";
    tipEl.dataset.tip = extracting
      ? tipEl.dataset.tipExtract || "字幕提取按整段输出，此项不适用"
      : tipEl.dataset.tipAlign;
  });

  $("#mode-note").textContent = extracting
    ? "不需要剧本，按识别到的人声段落整段输出。"
    : "用剧本对齐人声，文字以剧本为准。";
  $("#export-label").textContent = extracting
    ? "导出位置 · 字幕提取"
    : "导出位置 · 字幕对齐";
  $("#start").textContent = extracting ? "提取字幕" : "开始处理";

  if (persist) {
    try {
      localStorage.setItem(MODE_KEY, jobMode);
    } catch (error) {}
  }
  updateStart();
}

function updateStart() {
  const start = $("#start");
  const extracting = jobMode === "extract";
  const hasMedia = Boolean(mediaFile || mediaLocalPath);
  const hasScript = extracting || Boolean(scriptFile && scriptInspectOk);
  const modelReady = selectedModelReady();
  const ready = hasMedia && hasScript;
  start.disabled = jobRunning || !(ready && modelReady);
  start.dataset.tip = !hasMedia
    ? "先导入成片，再开始处理"
    : extracting
      ? "直接识别人声并生成 SRT，不需要剧本"
      : !hasScript
        ? "导入剧本后才能对齐字幕"
        : !modelReady
          ? "先下载所选模型，再开始处理"
          : "开始识别语音并对齐剧本";
  $$(".mode-btn").forEach((button) => {
    button.disabled = jobRunning;
  });
}

function bindDrop(zoneId, inputId, nameId, setter, clearer) {
  const zone = document.getElementById(zoneId);
  const input = document.getElementById(inputId);
  const name = document.getElementById(nameId);
  const meta = zone.querySelector(".dz-meta");

  const accept = (file) => {
    if (!file) return;
    name.textContent = file.name;
    zone.classList.add("filled");
    setDropMeta(meta, "", "");
    setter(file);
    updateStart();
  };

  const clear = () => {
    if (!zone.classList.contains("filled")) return;
    clearer();
    name.textContent = "拖入或点击选择";
    setDropMeta(meta, "", "");
    zone.classList.remove("filled");
    input.value = "";
    updateStart();
  };

  zone.querySelector(".drop-clear").addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    clear();
  });

  zone.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    event.stopPropagation();
    openFileMenu(event, zone, input, clear);
  });

  zone.addEventListener("dragover", (event) => {
    event.preventDefault();
    zone.classList.add("over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("over");
    accept(event.dataTransfer.files[0]);
  });
  input.addEventListener("change", () => accept(input.files[0]));
}

async function inspectScript(file) {
  const epoch = ++scriptInspectEpoch;
  const meta = $("#script-meta");
  setDropMeta(meta, `读取中 · ${fmtSize(file.size)}`, "loading");

  const form = new FormData();
  form.append("script", file);
  try {
    const response = await fetch("/api/script/inspect", {
      method: "POST",
      body: form,
    });
    const data = await response.json().catch(() => ({}));
    if (epoch !== scriptInspectEpoch || scriptFile !== file) return;
    if (!response.ok) throw new Error(data.detail || "剧本读取失败");

    scriptInspectData = data;
    renderScriptMeta(file.size);
    scriptInspectOk = true;
  } catch (error) {
    if (epoch !== scriptInspectEpoch || scriptFile !== file) return;
    scriptInspectData = null;
    setDropMeta(meta, error.message || "剧本读取失败", "error");
    scriptInspectOk = false;
  }
  updateStart();
}

function renderScriptMeta(fallbackSize = 0) {
  const data = scriptInspectData;
  if (!data) return;
  const maxChars = Math.max(1, Number($("#max-chars").value) || 14);
  const estimate = Math.max(1, Math.ceil(Number(data.chars || 0) / maxChars));
  const bits = [`${fmtCount(data.lines)} 句台词`];
  if (data.os) bits.push(`${fmtCount(data.os)} 句独白`);
  bits.push(`${fmtCount(data.chars)} 字`);
  bits.push(`约 ${fmtCount(estimate)} 条字幕`);
  bits.push(fmtSize(data.size ?? fallbackSize));
  setDropMeta($("#script-meta"), bits.join(" · "), "ready");
  const meta = $("#script-meta");
  if (meta) meta.title = "字幕条数按单行字数上限粗估，实际以分词结果为准";
}

function renderMediaMeta() {
  if (!mediaFile && !mediaLocalPath) return;
  clearTimeout(mediaMetaTimer);
  mediaMetaTimer = 0;

  const player = $("#player");
  const size = mediaFile ? mediaFile.size : mediaLocalSize;
  const bits = [];
  if (Number.isFinite(player.duration) && player.duration > 0) {
    bits.push(`时长 ${fmtDuration(player.duration)}`);
  }
  if (player.videoWidth && player.videoHeight) {
    bits.push(`${player.videoWidth}×${player.videoHeight}`);
  } else if (Number.isFinite(player.duration)) {
    bits.push("音频");
  }
  if (size) bits.push(fmtSize(size));
  const summary = bits.join(" · ");
  setDropMeta($("#media-meta"), summary, "ready");
}

function scheduleModelPoll() {
  if (modelPollTimer) window.clearTimeout(modelPollTimer);
  modelPollTimer = 0;
  const state = modelStates[$("#model").value];
  if (state && state.status === "downloading") {
    modelPollTimer = window.setTimeout(refreshModels, 700);
  }
}

async function refreshModels() {
  if (modelPollTimer) window.clearTimeout(modelPollTimer);
  modelPollTimer = 0;
  if (modelFetching) return;

  modelFetching = true;
  const watchedModel = $("#model").value;
  const previous = modelStates[watchedModel];
  try {
    const response = await fetch("/api/models");
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "无法读取模型状态");

    const next = {};
    for (const item of data.models || []) next[item.id] = item;
    modelStates = next;
    renderModelState();
    updateStart();

    const after = modelStates[watchedModel];
    if (watchedModel === $("#model").value && previous?.status === "downloading" && after) {
      if (after.status === "ready") toast("模型已下载，可以开始处理");
      else if (after.status === "failed") toast(after.error || "模型下载失败");
    }
  } catch (error) {
    // 状态接口暂时不可用时不阻塞已有模型；真正的启动校验在后端。
  } finally {
    modelFetching = false;
    scheduleModelPoll();
  }
}

function renderModelState() {
  const box = $("#model-state");
  const text = $("#model-state-text");
  const action = $("#model-state-action");
  const bar = $("#model-state-bar");
  const state = modelStates[$("#model").value];

  if (!state || state.status === "ready") {
    box.classList.add("hidden");
    action.classList.add("hidden");
    bar.style.width = "0%";
    return;
  }

  box.classList.remove("hidden");
  box.dataset.state = state.status;
  const label = state.label || $("#model").value;

  if (state.status === "downloading") {
    const percent = Math.round((state.progress || 0) * 100);
    text.textContent = `正在下载 ${label} 模型 · ${percent}% · ${state.size_label}`;
    action.classList.add("hidden");
    bar.style.width = `${Math.max(0, Math.min(100, percent))}%`;
    return;
  }

  if (state.status === "failed") {
    text.textContent = state.error || `${label} 模型下载失败`;
    action.textContent = "重试";
    action.dataset.tip = "重试下载，已下载的部分会续传";
  } else {
    text.textContent = `${label} 模型未下载 · ${state.size_label}`;
    action.textContent = "下载";
    action.dataset.tip = "优先从国内源下载到本机 data/models；中断后再次下载会续传";
  }
  action.disabled = false;
  action.classList.remove("hidden");
  bar.style.width = "0%";
}

function cancelActiveJob() {
  jobEpoch += 1;
  jobRunning = false;
  pollFailures = 0;
  currentJobId = null;
  if (activeUpload) {
    const upload = activeUpload;
    activeUpload = null;
    upload.abort();
  }
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
  $("#progress-card").classList.add("hidden");
}

function clearUploadedMedia() {
  mediaFile = null;
  clearTimeout(mediaMetaTimer);
  mediaMetaTimer = 0;
  if (mediaUrl) {
    URL.revokeObjectURL(mediaUrl);
    mediaUrl = null;
  }
  const zone = $("#drop-media");
  zone.classList.remove("filled");
  $("#media-name").textContent = "拖入或点击选择";
  setDropMeta($("#media-meta"), "", "");
  $("#media-input").value = "";
}

function clearMedia() {
  cancelActiveJob();
  clearLocalMedia();
  clearUploadedMedia();

  const player = $("#player");
  player.pause();
  player.removeAttribute("src");
  player.load();

  $("#video-frame").classList.add("hidden");
  $("#video-frame").style.width = "";
  $("#video-frame").style.height = "";
  $("#preview-empty").classList.remove("hidden");
  $("#preview-stage").classList.remove("has-video", "audio-only", "expanded");
  $("#subtitle-overlay").style.fontSize = "";
  document.body.classList.remove("preview-open");
  $("#preview-close").classList.add("hidden");
  $("#preview-expand").disabled = true;
  $("#preview-expand").setAttribute("aria-expanded", "false");
  $("#preview-expand").setAttribute("data-tip", "放大预览");
  previewExpanded = false;
  resetResultView();
}

bindDrop(
  "drop-media",
  "media-input",
  "media-name",
  (file) => {
    mediaFile = file;
    clearLocalMedia();  // 上传与本机直读二选一
    loadLocalPreview(file);
  },
  clearMedia
);
bindDrop(
  "drop-script",
  "script-input",
  "script-name",
  (file) => {
    if (jobRunning) cancelActiveJob();
    scriptFile = file;
    scriptInspectOk = false;
    scriptInspectData = null;
    inspectScript(file);
  },
  () => {
    if (jobRunning) cancelActiveJob();
    scriptInspectEpoch += 1;
    scriptFile = null;
    scriptInspectOk = false;
    scriptInspectData = null;
  }
);

function closeFileMenu() {
  $("#file-menu").classList.add("hidden");
  fileMenuState = null;
}

function openFileMenu(event, zone, input, clear) {
  const menu = $("#file-menu");
  const remove = menu.querySelector('[data-menu-action="remove"]');
  remove.disabled = !zone.classList.contains("filled");

  menu.classList.remove("hidden");
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  const left = Math.min(event.clientX, window.innerWidth - width - 8);
  const top = Math.min(event.clientY, window.innerHeight - height - 8);
  menu.style.left = `${Math.max(8, left)}px`;
  menu.style.top = `${Math.max(8, top)}px`;
  fileMenuState = { input, clear };
}

$("#file-menu").addEventListener("click", (event) => {
  const action = event.target.closest("[data-menu-action]")?.dataset.menuAction;
  if (!action || !fileMenuState) return;
  const state = fileMenuState;
  closeFileMenu();
  if (action === "reselect") state.input.click();
  else if (action === "remove") state.clear();
});

document.addEventListener("click", (event) => {
  if (!event.target.closest("#file-menu")) closeFileMenu();
});

document.addEventListener("scroll", closeFileMenu, true);
window.addEventListener("resize", closeFileMenu);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeFileMenu();
});
window.addEventListener("blur", closeFileMenu);

function clearOverlay() {
  const overlay = $("#subtitle-overlay");
  overlay.classList.remove("show", "flash");
  overlay.querySelector("span").textContent = "";
  activeCueText = "";
}

function setExportPath(path) {
  const value = path || "—";
  const target = $("#export-path");
  target.textContent = value;
  if (value === "—") target.removeAttribute("title");
  else target.title = value;
}

function resetResultView() {
  resultData = null;
  previewCues = [];
  clearOverlay();
  $("#result-card").classList.add("hidden");
  setExportPath("");
}

function loadLocalPreview(file) {
  cancelActiveJob();
  if (mediaUrl) URL.revokeObjectURL(mediaUrl);
  mediaUrl = URL.createObjectURL(file);
  resetResultView();
  startPreview(mediaUrl, fmtSize(file.size));
}

/** 统一的预览入口：上传的 objectURL 和本机文件的流式地址都走这里。 */
function startPreview(url, sizeLabel) {
  const meta = $("#media-meta");
  setDropMeta(meta, sizeLabel ? `读取中 · ${sizeLabel}` : "读取中…", "loading");
  clearTimeout(mediaMetaTimer);
  mediaMetaTimer = window.setTimeout(() => {
    if (meta.dataset.state !== "loading") return;
    setDropMeta(meta, sizeLabel || "", "");
  }, 5000);

  const player = $("#player");
  $("#preview-empty").classList.add("hidden");
  $("#preview-stage").classList.add("has-video");
  $("#video-frame").classList.remove("hidden");
  $("#preview-expand").disabled = false;
  player.src = url;
  player.load();
}

/* ---------- 大文件免上传：直接读本机文件 ---------- */

function clearLocalMedia() {
  mediaLocalPath = "";
  mediaLocalSize = 0;
  const input = $("#media-local");
  if (input) input.value = "";
  const box = $("#local-media");
  if (box) box.classList.remove("is-active");
  setLocalNote("", "");
}

function setLocalNote(text, state = "") {
  const note = $("#media-local-note");
  if (!note) return;
  note.textContent = text || "";
  note.dataset.state = text ? state : "";
}

function useLocalMedia(path, token, name, size, sizeLabel, preview = true) {
  clearUploadedMedia();
  mediaLocalPath = path;
  mediaLocalSize = size || 0;
  const input = $("#media-local");
  if (input) input.value = path;
  const box = $("#local-media");
  if (box) box.classList.add("is-active");
  $("#media-name").textContent = name || path;
  setLocalNote(
    `${name || path} · ${sizeLabel || fmtSize(size)} · 直接读原文件，不上传`,
    "ready"
  );
  updateStart();

  if (preview) {
    cancelActiveJob();
    resetResultView();
    startPreview(`/api/media/stream?token=${encodeURIComponent(token)}`, sizeLabel);
  }
}

async function registerLocalMedia(path) {
  const value = (path || "").trim();
  if (!value) {
    clearLocalMedia();
    clearUploadedMedia();
    updateStart();
    return false;
  }
  if (mediaLocalRegistering) return false;

  mediaLocalRegistering = true;
  setLocalNote("正在读取文件信息…", "loading");
  try {
    const response = await fetch("/api/media/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: value }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "这个文件用不了");
    useLocalMedia(
      value,
      data.token,
      data.name,
      data.size,
      data.size_label,
      true
    );
    return true;
  } catch (error) {
    setLocalNote(error.message || "这个文件用不了", "error");
    mediaLocalPath = "";
    updateStart();
    return false;
  } finally {
    mediaLocalRegistering = false;
  }
}

const mediaLocalInput = $("#media-local");
if (mediaLocalInput) {
  mediaLocalInput.addEventListener("change", (event) => {
    registerLocalMedia(event.target.value);
  });
  mediaLocalInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      registerLocalMedia(event.target.value);
    }
  });
}

const pickMediaButton = $("#pick-media");
if (pickMediaButton) {
  pickMediaButton.addEventListener("click", async () => {
    pickMediaButton.disabled = true;
    try {
      const response = await fetch("/api/media/pick", { method: "POST" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "无法打开文件选择窗口");
      if (data.path) await registerLocalMedia(data.path);
    } catch (error) {
      setLocalNote(error.message, "error");
    } finally {
      pickMediaButton.disabled = false;
    }
  });
}

function fitPreview() {
  const stage = $("#preview-stage");
  const frame = $("#video-frame");
  const player = $("#player");
  if (frame.classList.contains("hidden")) return;

  if (!player.videoWidth || !player.videoHeight) {
    stage.classList.add("audio-only");
    frame.style.width = "100%";
    frame.style.height = stage.classList.contains("expanded") ? "100%" : "160px";
    $("#subtitle-overlay").style.fontSize = "16px";
    return;
  }

  stage.classList.remove("audio-only");
  const styles = getComputedStyle(stage);
  const padX =
    parseFloat(styles.paddingLeft) + parseFloat(styles.paddingRight);
  const padY =
    parseFloat(styles.paddingTop) + parseFloat(styles.paddingBottom);
  const availW = Math.max(0, stage.clientWidth - padX);
  const availH = Math.max(0, stage.clientHeight - padY);
  const ratio = player.videoWidth / player.videoHeight;

  let width = availW;
  let height = width / ratio;
  if (height > availH) {
    height = availH;
    width = height * ratio;
  }

  frame.style.width = `${Math.round(width)}px`;
  frame.style.height = `${Math.round(height)}px`;
  $("#subtitle-overlay").style.fontSize =
    `${Math.max(15, Math.min(34, width * 0.06)).toFixed(1)}px`;
}

function setPreviewExpanded(expanded) {
  if (expanded && $("#video-frame").classList.contains("hidden")) return;

  previewExpanded = expanded;
  const stage = $("#preview-stage");
  const expand = $("#preview-expand");
  stage.classList.toggle("expanded", expanded);
  document.body.classList.toggle("preview-open", expanded);
  $("#preview-close").classList.toggle("hidden", !expanded);
  expand.setAttribute("aria-expanded", expanded ? "true" : "false");
  expand.setAttribute("data-tip", expanded ? "关闭预览" : "放大预览");
  requestAnimationFrame(fitPreview);
}

/** 二分查找当前时间对应的字幕；cues 按 start 升序。 */
function cueAt(time) {
  let lo = 0;
  let hi = previewCues.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (previewCues[mid].start <= time) {
      found = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  if (found < 0) return null;
  const cue = previewCues[found];
  return time < cue.end ? cue : null;
}

function syncSubtitle() {
  const player = $("#player");
  const overlay = $("#subtitle-overlay");
  const span = overlay.querySelector("span");

  if (!previewCues.length) {
    if (overlay.classList.contains("show") || activeCueText) clearOverlay();
    return;
  }

  const time = player.currentTime;
  const cue = cueAt(time);

  if (!cue) {
    if (overlay.classList.contains("show") || activeCueText) clearOverlay();
    return;
  }

  if (cue.text !== activeCueText) {
    activeCueText = cue.text;
    span.textContent = cue.text;
    overlay.classList.add("show");
    overlay.classList.remove("flash");
    void span.offsetWidth;
    overlay.classList.add("flash");
  } else if (!overlay.classList.contains("show")) {
    overlay.classList.add("show");
  }
}

function subtitleTick() {
  syncSubtitle();
  if (!$("#player").paused) overlayFrame = requestAnimationFrame(subtitleTick);
}

$("#preview-expand").addEventListener("click", () => {
  setPreviewExpanded(!previewExpanded);
});
$("#preview-close").addEventListener("click", () => setPreviewExpanded(false));

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && previewExpanded) setPreviewExpanded(false);
});

$("#player").addEventListener("loadedmetadata", () => {
  fitPreview();
  renderMediaMeta();
});
$("#player").addEventListener("error", () => {
  if ((!mediaFile && !mediaLocalPath) || !$("#player").src) return;
  clearTimeout(mediaMetaTimer);
  mediaMetaTimer = 0;
  const size = mediaFile ? mediaFile.size : mediaLocalSize;
  setDropMeta($("#media-meta"), `${fmtSize(size)} · 无法预览`, "");
});
$("#player").addEventListener("play", () => {
  if (overlayFrame) cancelAnimationFrame(overlayFrame);
  subtitleTick();
});
$("#player").addEventListener("pause", syncSubtitle);
$("#player").addEventListener("seeked", syncSubtitle);
$("#player").addEventListener("timeupdate", syncSubtitle);
$("#player").addEventListener("ended", syncSubtitle);
$("#player").addEventListener("emptied", clearOverlay);

window.addEventListener("resize", () => {
  if (!$("#video-frame").classList.contains("hidden")) fitPreview();
});

function setProgress(job) {
  const progress = Math.max(0, Math.min(1, Number(job.progress) || 0));
  $("#progress-bar").classList.remove("error");
  $("#progress-bar").style.width = `${(progress * 100).toFixed(1)}%`;
  $("#progress-percent").textContent = `${Math.round(progress * 100)}%`;
  $("#progress-stage").textContent = job.stage || "处理中";
  $("#progress-message").textContent = job.message || "";
}

function noteGpuFallback(job) {
  if (gpuFallbackNotified) return;
  const text = `${job.message || ""} ${job.error || ""}`;
  if (!text.includes("GPU 运行库不可用") && !text.includes("GPU 不可用")) return;
  gpuFallbackNotified = true;
  $("#device").value = "cpu";
  toast("GPU 运行库不可用，已自动切换 CPU");
}

/** FastAPI 的 422 detail 是数组；旧版服务缺少字段时也要说清楚。 */
function uploadErrorMessage(status, raw, data) {
  const detail = data && data.detail;
  if (typeof detail === "string" && detail.trim()) return detail.trim();

  if (Array.isArray(detail)) {
    const missingScript = detail.some((item) => {
      const loc = Array.isArray(item.loc) ? item.loc : [];
      return loc.includes("script");
    });
    if (status === 422 && missingScript) {
      return "本地服务还是旧版本，请关闭启动窗口后重新运行，再刷新页面。";
    }
    const fields = detail
      .map((item) => {
        const loc = Array.isArray(item.loc)
          ? item.loc.filter((part) => part !== "body")
          : [];
        const where = loc.length ? `${loc.join(".")}：` : "";
        return `${where}${item.msg || "参数不正确"}`;
      })
      .filter(Boolean);
    return fields.length
      ? `提交参数不正确：${fields.join("；")}`
      : "提交参数不正确";
  }

  if (data && typeof data.message === "string" && data.message.trim()) {
    return data.message.trim();
  }
  if (status === 413) return "上传文件太大，请改用「本机文件」零上传。";
  if (status === 507) return "磁盘空间不足，请清理后重试。";
  if (status >= 500) {
    return `服务端出错（HTTP ${status}），请查看启动窗口里的报错。`;
  }

  const text = String(raw || "")
    .replace(/<[^>]*>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return text.slice(0, 240) || `上传失败（HTTP ${status}）`;
}

/** 用 XHR 提交任务，上传大文件时进度条能真实走动。 */
function uploadJob(form, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/jobs");

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress(event.loaded / event.total);
      }
    };
    xhr.onload = () => {
      if (activeUpload === xhr) activeUpload = null;
      const raw = xhr.responseText || "";
      let data = {};
      try {
        data = raw ? JSON.parse(raw) : {};
      } catch (error) {}

      if (xhr.status >= 200 && xhr.status < 300) {
        if (data && data.id) resolve(data);
        else reject(new Error("服务端没有返回任务编号，请重启本地服务后再试。"));
        return;
      }
      reject(new Error(uploadErrorMessage(xhr.status, raw, data)));
    };
    xhr.onerror = () => {
      if (activeUpload === xhr) activeUpload = null;
      reject(new Error("上传失败，请确认本地服务仍在运行"));
    };
    xhr.onabort = () => {
      if (activeUpload === xhr) activeUpload = null;
      reject(new Error("上传已取消"));
    };

    activeUpload = xhr;
    xhr.send(form);
  });
}

async function startJob() {
  const extracting = jobMode === "extract";
  if (!extracting && (!scriptFile || !scriptInspectOk)) {
    toast("字幕对齐需要先导入剧本，或切换到字幕提取");
    return;
  }
  if (!mediaFile && !mediaLocalPath) {
    toast("请先选择成片，或填写本机文件路径");
    return;
  }
  if (!selectedModelReady()) {
    toast("所选模型还没有下载，请先点击下载");
    return;
  }

  const form = new FormData();
  form.append("mode", jobMode);
  if (mediaLocalPath) {
    form.append("media_path", mediaLocalPath);
  } else if (mediaFile) {
    form.append("media", mediaFile);
  }
  if (!extracting) form.append("script", scriptFile);
  form.append("max_chars", $("#max-chars").value);
  form.append("model", $("#model").value);
  form.append("device", $("#device").value);
  form.append("include_os", "true");
  form.append("no_gap", $("#no-gap").checked ? "true" : "false");
  form.append("max_fill", $("#max-fill").value);
  if (!extracting) {
    form.append("lead_in", $("#lead-in").value);
    form.append("tail_delay", $("#tail-delay").value);
  }
  rememberExportDir();
  form.append("export_dir", $("#export-dir").value.trim());

  jobEpoch += 1;
  const epoch = jobEpoch;
  const uploading = Boolean(mediaFile) && !mediaLocalPath;
  jobRunning = true;
  pollFailures = 0;
  gpuFallbackNotified = false;
  updateStart();
  currentJobId = null;
  previewCues = [];
  clearOverlay();
  $("#result-card").classList.add("hidden");
  $("#progress-card").classList.remove("hidden");
  setProgress({
    stage: uploading ? "上传中" : "提交任务",
    message: uploading ? "正在把素材传到本地服务…" : "正在提交任务…",
    progress: 0,
  });

  try {
    const data = await uploadJob(form, (ratio) => {
      if (epoch !== jobEpoch || !uploading) return;
      setProgress({
        stage: "上传中",
        message: `正在把素材传到本地服务… ${Math.round(ratio * 100)}%`,
        progress: ratio,
      });
    });
    if (epoch !== jobEpoch) return;
    currentJobId = data.id;
    poll(epoch);
  } catch (error) {
    if (epoch !== jobEpoch) return;
    showError(error.message);
  }
}

async function poll(epoch) {
  if (epoch !== jobEpoch || !currentJobId) return;
  const jobId = currentJobId;
  try {
    const response = await fetch(`/api/jobs/${jobId}`);
    if (epoch !== jobEpoch) return;
    if (!response.ok) {
      const error = new Error(
        response.status === 404 ? "任务不存在或服务已重启" : "读取任务状态失败"
      );
      error.fatal = response.status === 404;
      throw error;
    }
    const job = await response.json();
    if (epoch !== jobEpoch) return;
    pollFailures = 0;
    setProgress(job);
    noteGpuFallback(job);

    if (job.status === "done") {
      jobRunning = false;
      renderResult(job);
      return;
    }
    if (job.status === "error") {
      jobRunning = false;
      showError(job.error || "处理失败");
      return;
    }
    pollTimer = setTimeout(() => poll(epoch), 700);
  } catch (error) {
    if (epoch !== jobEpoch) return;
    // 本地服务重启或瞬时断网时，后台任务可能还在跑；连续失败几次再报错。
    if (!error.fatal && pollFailures < 4) {
      pollFailures += 1;
      $("#progress-message").textContent = "连接暂时中断，正在重试…";
      pollTimer = setTimeout(() => poll(epoch), 1200);
      return;
    }
    jobRunning = false;
    showError(error.message);
  }
}

function showError(message) {
  jobRunning = false;
  $("#progress-card").classList.remove("hidden");
  $("#progress-stage").textContent = "处理失败";
  $("#progress-message").textContent = message;
  $("#progress-bar").style.width = "100%";
  $("#progress-bar").classList.add("error");
  $("#progress-percent").textContent = "";
  updateStart();
  toast(message);
}

function stat(label, value, unit) {
  return `<div class="stat">
    <div class="stat-label">${esc(label)}</div>
    <div class="stat-value">${esc(value)}<small>${esc(unit)}</small></div>
  </div>`;
}

function summaryStats(summary) {
  if ((summary.mode || "align") === "extract") {
    return [
      stat("人声", summary.lines, "段"),
      stat("字幕", summary.cues, "条"),
      stat("时长", fmtDuration(summary.duration || 0), ""),
    ].join("");
  }
  return [
    stat("对白", summary.lines, "句"),
    stat("匹配率", Math.round(summary.matched_ratio * 100), "%"),
    stat("待确认", summary.low + summary.unaligned, "句"),
    stat("噪声", summary.noise, "段"),
    stat("字幕", summary.cues, "条"),
  ].join("");
}

function renderResult(job) {
  resultData = job.result;
  currentJobId = job.id;
  previewCues = resultData.cues || [];
  const summary = resultData.summary;

  $("#progress-card").classList.add("hidden");
  $("#result-card").classList.remove("hidden");
  updateStart();

  setExportPath(job.output_path);
  renderResultMeta(job);

  if (summary.align_mode === "difflib") {
    $("#result-sub").textContent +=
      " · 注意：本机缺少 numpy，长剧本的同音匹配已降级";
    toast("这台机器没装 numpy，长剧本的同音匹配会降级，建议执行 pip install numpy");
  }

  try {
    localStorage.setItem(LAST_JOB_KEY, job.id);
  } catch (error) {}

  renderTab((summary.mode || "align") === "extract" ? "cues" : "review");
  syncSubtitle();
}

function statusTag(status) {
  if (status === "low") return '<span class="status low"><i></i>匹配度低</span>';
  if (status === "unaligned") return '<span class="status unaligned"><i></i>未找到</span>';
  if (status === "os") return '<span class="status"><i></i>旁白未输出</span>';
  return '<span class="status ok"><i></i>正常</span>';
}

function reviewTable() {
  const lines = resultData.lines.filter(
    (line) => line.status === "low" || line.status === "unaligned"
  );

  if (!lines.length) {
    return '<div class="empty">没有需要确认的台词。</div>';
  }

  const rows = lines.map((line) => {
    const startAttr = line.start === null ? "" : ` data-start="${line.start}"`;
    return `<tr${startAttr}>
      <td class="mono">${line.idx}</td>
      <td class="speaker">${esc(line.speaker)}${line.is_os ? " · OS" : ""}</td>
      <td class="text">${esc(line.text)}</td>
      <td class="mono">${fmtTime(line.start)}</td>
      <td class="mono">${Math.round(line.ratio * 100)}%</td>
      <td>${statusTag(line.status)}</td>
    </tr>`;
  }).join("");

  return `<table>
    <thead><tr><th>#</th><th>角色</th><th>台词</th><th>时间</th><th>匹配</th><th>状态</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function noiseTable() {
  const items = resultData.noise;
  if (!items.length) {
    return '<div class="empty">没有检测到噪声。</div>';
  }

  const rows = items.map((item) => `<tr data-start="${item.start}">
    <td class="mono">${fmtTime(item.start)}</td>
    <td class="mono">${item.duration.toFixed(1)}s</td>
    <td class="text">${esc(item.text) || '<span class="hint">（没识别出文字）</span>'}</td>
  </tr>`).join("");

  return `<table>
    <thead><tr><th>时间</th><th>时长</th><th>识别文字</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function cueTable() {
  const items = resultData.cues;
  if (!items.length) {
    return '<div class="empty">没有生成字幕。</div>';
  }

  const extracting = (resultData.summary.mode || "align") === "extract";
  const rows = items.map((cue) => `<tr data-start="${cue.start}">
    <td class="mono">${cue.index}</td>
    <td class="mono">${fmtTime(cue.start)} → ${fmtTime(cue.end)}</td>
    ${extracting ? "" : `<td class="speaker">${esc(cue.speaker)}</td>`}
    <td class="text">${esc(cue.text)}</td>
  </tr>`).join("");

  return `<table>
    <thead><tr><th>#</th><th>时间</th>${extracting ? "" : "<th>角色</th>"}<th>字幕</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function renderTab(name) {
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  const content = $("#tab-content");
  if (name === "review") content.innerHTML = reviewTable();
  else if (name === "noise") content.innerHTML = noiseTable();
  else content.innerHTML = cueTable();
}

function seek(seconds) {
  const player = $("#player");
  if (!player || !player.src) return;
  player.currentTime = Number(seconds);
  player.play().catch(() => {});
  if (!previewExpanded) {
    $(".preview-card").scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

$("#tab-content").addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-start]");
  if (!row) return;
  seek(row.dataset.start);
});

$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => renderTab(tab.dataset.tab));
});

$("#model").addEventListener("change", () => {
  renderModelState();
  updateStart();
  refreshModels();
});

$("#model-state-action").addEventListener("click", async () => {
  const button = $("#model-state-action");
  const model = $("#model").value;
  const state = modelStates[model];
  if (!state || button.disabled) return;

  const label = state.label || model;
  button.disabled = true;
  try {
    const response = await fetch(
      `/api/models/${encodeURIComponent(model)}/download`,
      { method: "POST" }
    );
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "无法开始下载");
    if (data.model) modelStates[model] = data.model;
    renderModelState();
    updateStart();
    scheduleModelPoll();
    toast(`${label} 模型开始下载`);
    refreshModels();
  } catch (error) {
    button.disabled = false;
    toast(error.message);
    refreshModels();
  }
});

$("#start").addEventListener("click", startJob);

$$(".mode-btn").forEach((button) => {
  button.addEventListener("click", () => {
    if (jobRunning) return;
    setMode(button.dataset.mode);
  });
});

/* ---------- 下一集：清掉记录和上传素材，保留已导出的 SRT ---------- */

async function nextEpisode() {
  const jobId = currentJobId;
  if (jobId) {
    try {
      // 只清任务记录和上传素材；已经生成的 SRT 留在导出目录里。
      await fetch(`/api/jobs/${encodeURIComponent(jobId)}?keep_output=true`, {
        method: "DELETE",
      });
    } catch (error) {}
    try {
      localStorage.removeItem(LAST_JOB_KEY);
    } catch (error) {}
  }
  window.location.reload();
}

$("#again").addEventListener("click", nextEpisode);

// 任务在跑时关掉/刷新页面要拦一下，否则后端还在算，前端已经走了。
window.addEventListener("beforeunload", (event) => {
  if (!jobRunning) return;
  event.preventDefault();
  event.returnValue = "";
});

// 刷新或重启后，把上次完成的结果找回来（报告已落盘）。
async function restoreLastJob() {
  let jobId = null;
  try {
    jobId = localStorage.getItem(LAST_JOB_KEY);
  } catch (error) {
    return;
  }
  if (!jobId) return;

  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (!response.ok) {
      localStorage.removeItem(LAST_JOB_KEY);
      return;
    }
    const job = await response.json();

    // 刷新页面时任务可能还在跑：接回轮询，不把它丢掉。
    if (job.status === "queued" || job.status === "running") {
      if (job.mode) setMode(job.mode, false);
      currentJobId = job.id;
      jobRunning = true;
      pollFailures = 0;
      $("#result-card").classList.add("hidden");
      $("#progress-card").classList.remove("hidden");
      $("#media-name").textContent = job.stem || "本次成片";
      setProgress(job);
      updateStart();
      startPreview(`/api/jobs/${job.id}/media`, "");
      toast("已接回正在处理的任务");
      poll(jobEpoch);
      return;
    }

    if (job.status === "error" || !job.result) {
      localStorage.removeItem(LAST_JOB_KEY);
      return;
    }
    if (job.status !== "done") return;

    currentJobId = job.id;
    resultData = job.result;
    previewCues = resultData.cues || [];
    if (job.mode) setMode(job.mode, false);
    $("#result-card").classList.remove("hidden");
    setExportPath(job.output_path);
    $("#media-name").textContent = job.stem || "上次的成片";
    renderResultMeta(job);
    renderTab(
      (resultData.summary.mode || "align") === "extract" ? "cues" : "review"
    );
    startPreview(`/api/jobs/${job.id}/media`, "");
    toast("已恢复上次的处理结果");
  } catch (error) {}
}

/** 渲染结果页除表格以外的部分（恢复任务时也要用）。 */
function renderResultMeta(job) {
  const summary = job.result.summary;
  const extracting = (summary.mode || "align") === "extract";
  const card = $("#result-card");
  card.dataset.mode = extracting ? "extract" : "align";
  $("#summary").dataset.mode = extracting ? "extract" : "align";
  $("#summary").innerHTML = summaryStats(summary);
  $("#badge-review").textContent = (summary.low || 0) + (summary.unaligned || 0);
  $("#badge-noise").textContent = summary.noise || 0;
  $("#badge-cues").textContent = summary.cues;
  $("#result-sub").textContent = extracting
    ? `${summary.lines} 段人声 · ${summary.cues} 条字幕`
    : `${summary.lines} 句对白 · ${summary.cues} 条字幕` +
      (summary.noise ? ` · 已过滤 ${summary.noise} 段非台词声音` : "");
}

let initialMode = "align";
try {
  initialMode = localStorage.getItem(MODE_KEY) || "align";
} catch (error) {}
setMode(initialMode, false);

loadPaths();

restoreLastJob();
refreshModels();

$("#pick-export").addEventListener("click", async () => {
  const button = $("#pick-export");
  button.disabled = true;
  try {
    const response = await fetch("/api/export-dir/pick", { method: "POST" });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "无法打开文件夹选择窗口");
    if (data.path) {
      $("#export-dir").value = data.path;
      rememberExportDir();
      renderExportHint();
      toast("已选择导出位置");
    }
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

$("#export-srt").addEventListener("click", async () => {
  if (!currentJobId) return;
  const button = $("#export-srt");
  button.disabled = true;
  try {
    const form = new FormData();
    form.append("export_dir", $("#export-dir").value.trim());
    const response = await fetch(
      `/api/jobs/${encodeURIComponent(currentJobId)}/export`,
      { method: "POST", body: form }
    );
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "导出失败");

    setExportPath(data.output_path);
    toast(
      data.moved ? `已导出到 ${data.output_path}` : "字幕已经在导出位置"
    );
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

$("#copy-path").addEventListener("click", () => {
  const path = $("#export-path").textContent;
  if (path && path !== "—") copyText(path, "导出路径已复制");
});

$("#open-folder").addEventListener("click", async () => {
  if (!currentJobId) return;
  try {
    const response = await fetch(`/api/jobs/${currentJobId}/reveal`, {
      method: "POST",
    });
    if (!response.ok) throw new Error("无法打开文件夹");
  } catch (error) {
    toast(error.message);
  }
});
