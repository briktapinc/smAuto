const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const STORE_JOB = "lazykh.activeJob";
const STORE_STEP = "lazykh.activeStep";
const STORE_SCRIPT_TAB = "lazykh.scriptTab";
const STORE_LIB_FILTER = "lazykh.libraryFilter";
const STORE_TOKEN = "bubblepod.authToken";
const LIBRARY_PAGE = 12;
const APP_STEPS = [
  "library", "jobs", "topics", "costs", "create", "settings", "prompts",
  "watch", "script", "pictures", "voice", "backgrounds", "music", "video",
];

let current = null;
let pollTimer = null;
let illustTimer = null;
let illustSeq = 0;
let lastIllust = null;
let regenTargetKey = "";
const ILLUST_POLL_MS = 2500;
let jobIndex = [];
let libraryPage = 0;
let libraryFilter = "all";
let pendingDeleteId = null;
let pendingDeleteFiles = false;
let bgCatalog = [];
let topicList = [];
let topicQueue = [];
let topicsBusy = false;
let topicMeta = {};
let topicsTimer = null;
let scheduleDialogTopicId = "";
let jobsPage = 0;
let jobsFilter = "all";
let jobsSearch = "";
let topicsPage = 0;
let topicsFilter = "all";
let topicsSearch = "";
let costsPage = 0;
let costsSearch = "";
let costsRows = [];
let lastErrorModalKey = "";
let promptsPage = 0;
let promptDrafts = {};
/** True when Stripe membership is required and this user lacks access (non-admin). */
let membershipLocked = false;
/** True when running as local Electron/.exe single-user app (no login / tenants / Stripe). */
let desktopMode = !!(typeof window !== "undefined" && window.bubblePod && window.bubblePod.isDesktop);

function applyDesktopModeUi() {
  document.body.classList.add("desktop-mode", "is-admin");
  document.body.classList.remove("is-member", "membership-locked");
  membershipLocked = false;
  [
    "#login-gate",
    "#pricing-link",
    "#admin-link",
    "#logout-btn",
    "#membership-banner",
    "#settings-membership-card",
    "#settings-account-card",
  ].forEach((sel) => {
    const el = $(sel);
    if (el) el.hidden = true;
  });
  $$(".admin-only").forEach((el) => {
    if (el.id === "admin-link") {
      el.hidden = true;
      return;
    }
    el.hidden = false;
  });
  $$(".member-only").forEach((el) => { el.hidden = true; });
  try { applyMembershipUiLocks(); } catch { /* ignore */ }
}
const LIST_PAGE = 12;
const PROMPTS_PAGE = 3;

function getAuthToken() {
  try { return localStorage.getItem(STORE_TOKEN) || ""; } catch { return ""; }
}

function setAuthToken(token) {
  try {
    if (token) localStorage.setItem(STORE_TOKEN, token);
    else localStorage.removeItem(STORE_TOKEN);
  } catch {}
}

function showLoginGate(message = "", view = "login") {
  if (desktopMode) {
    applyDesktopModeUi();
    showStudioApp();
    return;
  }
  const app = $("#studio-app");
  const gate = $("#login-gate");
  if (app) app.hidden = true;
  if (gate) gate.hidden = false;
  const show = (id) => {
    ["#login-form", "#signup-form", "#forgot-form", "#reset-form"].forEach((sel) => {
      const el = $(sel);
      if (el) el.hidden = sel !== id;
    });
  };
  if (view === "signup") show("#signup-form");
  else if (view === "forgot") show("#forgot-form");
  else if (view === "reset") show("#reset-form");
  else show("#login-form");
  const err = $("#login-error");
  if (err) {
    err.hidden = !message;
    err.textContent = message || "";
  }
  $("#login-pass") && ($("#login-pass").value = "");
  if (view === "login") $("#login-user")?.focus();
  if (view === "forgot") $("#forgot-email")?.focus();
  if (view === "reset") $("#reset-pass")?.focus();
}

function showStudioApp() {
  const app = $("#studio-app");
  const gate = $("#login-gate");
  if (gate) gate.hidden = true;
  if (app) app.hidden = false;
}
function toast(msg, bad = false) {
  const banner = $("#run-status");
  // Status banner already shows job detail — don't stack an identical green alert.
  if (banner && !banner.hidden && banner.textContent === msg) return;
  // Long pipeline errors go to the modal instead of a giant toast.
  if (bad && shouldShowErrorModal(msg)) {
    showJobErrorModal(msg);
    return;
  }
  const el = $("#toast");
  el.hidden = false;
  el.classList.toggle("bad", bad);
  el.textContent = msg;
  setTimeout(() => { el.hidden = true; }, 5000);
}

function requireMembership(action = "do that") {
  if (!membershipLocked) return true;
  toast(
    `Membership required to ${action}. You can still browse and delete jobs — subscribe via the banner.`,
    true
  );
  return false;
}

function applyMembershipUiLocks() {
  const locked = !!membershipLocked;
  document.body.classList.toggle("membership-locked", locked);
  // Create / topics controls are membership-only (not job-running).
  [
    "#create-form button", "#create-form input", "#create-form select", "#create-form textarea",
    "#topics-generate", "#topics-form input", "#topics-form select",
  ].forEach((sel) => {
    $$(sel).forEach((el) => { el.disabled = locked; });
  });
  [
    "#render-aspect", "#render-layout", "#render-art-style", "#render-character-size", "#include-bubblehead",
    "#image-provider", "#voice-provider", "#voice-id",
  ].forEach((sel) => {
    $$(sel).forEach((el) => { el.disabled = locked; });
  });
  if (current) {
    setBusy(!!(current.running || current.busy), $("#run-status-text")?.textContent || "");
  } else {
    ["#gen-script", "#save-script", "#gen-audio", "#align", "#render", "#gen-flux",
      "#job-yt-upload", "#watch-yt-upload", "#header-resume", "#watch-resume", "#video-resume",
    ].forEach((sel) => {
      $$(sel).forEach((el) => { el.disabled = locked; });
    });
  }
  renderJobsQueue();
  if ($("#view-topics")?.classList.contains("on")) renderTopics();
  syncResumeButtons();
}

function shouldShowErrorModal(msg) {
  const m = String(msg || "");
  if (m.length < 80 && !/failed|error|traceback|scheduler|gentle|align/i.test(m)) return false;
  return /failed|error|traceback|scheduler\.py|gentle script\.json|substring not found|ValueError|exit 1/i.test(m);
}

function simplifyJobError(raw) {
  const m = String(raw || "").trim();
  if (/Gentle script\.json does not match|substring not found|scheduler\.py failed|never schedule from a stale json/i.test(m)) {
    return {
      title: "Render failed — timing out of sync",
      message: "The lip-sync timing file no longer matches the script. Re-align phonemes on the Voice step, then render again.",
      hint: "This usually happens after the script or audio changed without a fresh align.",
      raw: m,
    };
  }
  if (/traceback|exit 1|File \".*\", line /i.test(m)) {
    const first = m.split(/\n/).find((line) => line.trim()) || "The job failed.";
    const short = first.replace(/^Render failed\.\s*/i, "").slice(0, 160);
    return {
      title: "Job failed",
      message: short + (short.length >= 160 ? "…" : ""),
      hint: "Open technical details if you need the full log.",
      raw: m,
    };
  }
  return {
    title: "Something went wrong",
    message: m.slice(0, 220) + (m.length > 220 ? "…" : ""),
    hint: "",
    raw: m.length > 220 ? m : "",
  };
}

function showJobErrorModal(raw, opts = {}) {
  const info = simplifyJobError(raw);
  const key = `${info.title}|${info.message}|${(info.raw || "").slice(0, 120)}`;
  if (!opts.force && key === lastErrorModalKey) return;
  lastErrorModalKey = key;
  const title = $("#job-error-title");
  const message = $("#job-error-message");
  const hint = $("#job-error-hint");
  const details = $("#job-error-details");
  const rawEl = $("#job-error-raw");
  if (title) title.textContent = info.title;
  if (message) message.textContent = info.message;
  if (hint) {
    hint.hidden = !info.hint;
    hint.textContent = info.hint || "";
  }
  if (details && rawEl) {
    const showRaw = Boolean(info.raw && info.raw !== info.message);
    details.hidden = !showRaw;
    if (showRaw) details.open = false;
    rawEl.textContent = info.raw || "";
  }
  $("#job-error-dialog")?.showModal();
}

function paginate(items, page, pageSize = LIST_PAGE) {
  const total = items.length;
  const pages = Math.max(1, Math.ceil(total / pageSize) || 1);
  const safePage = Math.min(Math.max(0, page), pages - 1);
  const slice = items.slice(safePage * pageSize, (safePage + 1) * pageSize);
  return { items: slice, page: safePage, pages, total };
}

function updatePager(pagerId, labelId, prevId, nextId, page, pages, total, pageSize = LIST_PAGE) {
  const pager = $(pagerId);
  if (!pager) return page;
  pager.hidden = total <= pageSize;
  const label = $(labelId);
  if (label) label.textContent = total ? `Page ${page + 1} of ${pages} · ${total}` : "No results";
  const prev = $(prevId);
  const next = $(nextId);
  if (prev) prev.disabled = page <= 0;
  if (next) next.disabled = page >= pages - 1;
  return page;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function currentLayout() {
  return current?.video_layout || "cover";
}

function isStudioImages(provider) {
  const p = provider || current?.image_provider || "flux";
  return p === "flux" || p === "comfyui";
}

function generateAtLine(aspect, layout, provider) {
  const studio = isStudioImages(provider);
  if ((layout || currentLayout()) === "billboard") {
    if (studio) {
      return "16:9 at 1920×1080, opaque TV-screen billboard (even on 9:16 video). Topic art only — no presenter in the picture.";
    }
    return "In ChatGPT's image UI pick the 16:9 landscape preset at exactly 1920×1080 for an opaque TV-screen billboard (even on 9:16 video). Never square. Topic art only — no presenter.";
  }
  if (studio) {
    return (aspect || "16:9") === "9:16"
      ? "9:16 at 1080×1920. Topic art only — no presenter in the picture."
      : "16:9 at 1920×1080. Topic art only — no presenter in the picture.";
  }
  return (aspect || "16:9") === "9:16"
    ? "In ChatGPT's image UI pick the 9:16 portrait preset at exactly 1080×1920. Never square. Topic art only — no presenter."
    : "In ChatGPT's image UI pick the 16:9 landscape preset at exactly 1920×1080. Never square. Topic art only — no presenter.";
}

const FLUX_MODEL_ID = "fal-ai/flux-2";
const FLUX_USD_PER_IMAGE = 0.046;

function formatUsd(n) {
  const v = Number(n) || 0;
  if (v < 0.01 && v > 0) return `~$${v.toFixed(3)}`;
  return `~$${v.toFixed(2)}`;
}

function fluxCostHint(imageCount) {
  const n = Math.max(0, Number(imageCount) || 0);
  if (n <= 0) {
    return `Flux Dev 2 (${FLUX_MODEL_ID}) ≈ ~4.6¢/image at ~2MP; ~$2.60 per ~10-min video (~56 images).`;
  }
  return `Estimate: ${n} image${n === 1 ? "" : "s"} × $0.046 ≈ ${formatUsd(n * FLUX_USD_PER_IMAGE)} (Flux Dev 2 ~2MP).`;
}

function picturesSubtitle() {
  const aspect = current?.aspect || "16:9";
  const layout = currentLayout();
  const provider = current?.image_provider || "flux";
  const size = layout === "billboard"
    ? "16:9 (1920×1080) opaque TV-screen billboard"
    : (aspect === "9:16"
      ? "exactly 9:16 (1080×1920 portrait)"
      : aspect === "both"
        ? "both 16:9 (1920×1080) and 9:16 (1080×1920)"
        : "exactly 16:9 (1920×1080 landscape)");
  if (layout === "billboard") {
    if (provider === "chatgpt") {
      return `5s topic title card first (full-bleed, no narrator) at the video size (16:9 → 1920×1080, 9:16 → 1080×1920), then 16:9 TV-screen billboards at 1920×1080 even on portrait video. Generate in ChatGPT’s built-in image tool using those presets, then upload.`;
    }
    if (provider === "comfyui") {
      return `5s topic title card first (full-bleed, no narrator), then 16:9 TV-screen billboards. ComfyUI generates the cover and missing line images at ${size} from your uploaded API JSON. Safe to refresh while the job runs.`;
    }
    return `5s topic title card first (full-bleed, no narrator), then 16:9 TV-screen billboards. Flux (${FLUX_MODEL_ID}) generates the cover and missing line images at ${size}. Safe to refresh while the job runs.`;
  }
  if (provider === "chatgpt") {
    return `5s topic title card first, then cover backgrounds for the whole frame. Generate each image at ${size} in ChatGPT’s built-in image tool (16:9 or 9:16 preset — never square), then upload.`;
  }
  if (provider === "comfyui") {
    return `5s topic title card first, then cover backgrounds for the whole frame. ComfyUI generates the cover and missing images at ${size} from your uploaded API JSON. Safe to refresh while the job runs.`;
  }
  return `5s topic title card first, then cover backgrounds for the whole frame. Flux (${FLUX_MODEL_ID}) generates the cover and missing images at ${size}. Safe to refresh while the job runs.`;
}

function formatDuration(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

async function api(path, opts = {}) {
  const headers = {
    ...(opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {}),
    ...(opts.headers || {}),
  };
  const token = getAuthToken();
  if (token && !headers.Authorization) headers.Authorization = `Bearer ${token}`;
  const timeoutMs = Number(opts.timeoutMs);
  const useTimeout = Number.isFinite(timeoutMs) && timeoutMs > 0;
  const controller = useTimeout ? new AbortController() : null;
  const signal = opts.signal || controller?.signal;
  let timer;
  if (useTimeout && controller) {
    timer = setTimeout(() => controller.abort(), timeoutMs);
  }
  let res;
  try {
    const { timeoutMs: _t, signal: _s, ...fetchOpts } = opts;
    res = await fetch(path, {
      credentials: "same-origin",
      ...fetchOpts,
      headers,
      ...(signal ? { signal } : {}),
      body: opts.body && !(opts.body instanceof FormData) ? JSON.stringify(opts.body) : opts.body,
    });
  } catch (err) {
    const msg = String(err && err.message ? err.message : err);
    if (useTimeout && (err?.name === "AbortError" || /aborted/i.test(msg))) {
      throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)}s (${path}). Try Restart API if Studio is stuck.`);
    }
    if (/failed to fetch|networkerror|load failed/i.test(msg)) {
      throw new Error(
        "Could not reach Studio (Failed to fetch). Is the API running? If you changed the port, Restart API. Large uploads can also fail while rate-limited — wait a minute or raise the API limit in Settings."
      );
    }
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
  }
  if (res.status === 401 && path !== "/api/auth/login" && path !== "/api/auth/me") {
    if (!desktopMode) {
      setAuthToken("");
      showLoginGate("Session expired. Sign in again.");
    }
    throw new Error("Not authenticated");
  }
  if (!res.ok) {
    let detail = res.statusText;
    let code = "";
    try {
      const payload = await res.json();
      detail = payload.detail ?? detail;
      code = payload.code || "";
      if (detail && typeof detail === "object") {
        code = detail.code || code;
        detail = detail.detail || detail.message || JSON.stringify(detail);
      }
    } catch {}
    if (res.status === 402 || code === "membership_required") {
      membershipLocked = true;
      applyMembershipUiLocks();
      detail = typeof detail === "string" && detail
        ? detail
        : "Membership required. You can still browse and delete jobs — subscribe to create, edit, or generate.";
    }
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = res.status;
    err.code = code || (res.status === 402 ? "membership_required" : "");
    throw err;
  }
  const type = res.headers.get("content-type") || "";
  if (type.includes("application/json")) return res.json();
  return res;
}

function spendConfirmRequired() {
  return lastSettings.require_spend_confirm !== false;
}

function needsOpenAiSpend() {
  return textProvider() === "openai";
}

function needsFluxSpend(project = current) {
  const p = project?.image_provider || lastSettings.image_provider || "flux";
  return p === "flux";
}

function needsOpenAiTtsSpend() {
  const p = $("#voice-provider")?.value || lastSettings.tts_provider || lastSettings.voice_provider || "openai";
  return p === "openai";
}

function confirmSpend(message, opts = {}) {
  if (!spendConfirmRequired()) return Promise.resolve(true);
  const dlg = $("#spend-confirm-dialog");
  if (!dlg) return Promise.resolve(window.confirm(message));
  const msg = $("#spend-confirm-message");
  if (msg) msg.textContent = message || "This will spend OpenAI or fal credits.";
  const costHint = $("#spend-confirm-cost-hint");
  if (costHint) {
    const showFlux = opts.flux !== false && (
      opts.flux === true
      || /flux|fal/i.test(message || "")
    );
    costHint.hidden = !showFlux;
    if (showFlux) {
      costHint.textContent = opts.costHint || fluxCostHint(opts.images);
    }
  }
  return new Promise((resolve) => {
    const onClose = () => {
      dlg.removeEventListener("close", onClose);
      resolve(dlg.returnValue === "confirm");
    };
    dlg.addEventListener("close", onClose);
    dlg.showModal();
  });
}

function pauseWatch() {
  const player = $("#watch-player");
  if (player && !player.paused) player.pause();
}

const JOB_STEPS = ["script", "pictures", "voice", "backgrounds", "music", "video"];

function stepButtons() {
  return $$("#content-tabs [data-step], #app-tabs [data-step]");
}

function syncJobTabs() {
  const row = $("#job-tabs");
  if (!row) return;
  row.hidden = !current;
}

function readStoredStep() {
  try { return (localStorage.getItem(STORE_STEP) || "").trim(); } catch { return ""; }
}

function readStoredJob() {
  try { return (localStorage.getItem(STORE_JOB) || "").trim(); } catch { return ""; }
}

function parseLocationRoute() {
  const raw = (location.hash || "").replace(/^#/, "").trim();
  if (!raw) return null;
  const parts = raw.split("/").filter(Boolean);
  const step = decodeURIComponent(parts[0] || "").trim();
  if (!APP_STEPS.includes(step)) return null;
  const jobId = parts[1] ? decodeURIComponent(parts.slice(1).join("/")).trim() : "";
  return { step, jobId };
}

function syncLocationRoute(step, jobId = "") {
  if (!APP_STEPS.includes(step)) return;
  let next = step;
  if ((JOB_STEPS.includes(step) || step === "watch") && jobId) {
    next = `${step}/${encodeURIComponent(jobId)}`;
  }
  const hash = `#${next}`;
  if (location.hash !== hash) {
    history.replaceState(null, "", `${location.pathname}${location.search}${hash}`);
  }
}

function applyScriptTab(tab) {
  const name = tab === "raw" ? "raw" : "tagged";
  try { localStorage.setItem(STORE_SCRIPT_TAB, name); } catch { /* ignore */ }
  $$(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  if ($("#script-tagged")) $("#script-tagged").hidden = name !== "tagged";
  if ($("#script-raw")) $("#script-raw").hidden = name !== "raw";
}

function applyLibraryFilter(filter) {
  const allowed = ["all", "ready", "progress"];
  const name = allowed.includes(filter) ? filter : "all";
  libraryFilter = name;
  libraryPage = 0;
  try { localStorage.setItem(STORE_LIB_FILTER, name); } catch { /* ignore */ }
  $$("#lib-filters button").forEach((b) => b.classList.toggle("on", b.dataset.filter === name));
}

function setStep(name) {
  if (!APP_STEPS.includes(name)) name = "library";
  if (name !== "watch") pauseWatch();
  try { localStorage.setItem(STORE_STEP, name); } catch { /* ignore */ }
  if ((JOB_STEPS.includes(name) || name === "watch") && current?.id) {
    try { localStorage.setItem(STORE_JOB, current.id); } catch { /* ignore */ }
  }
  syncLocationRoute(name, current?.id || "");
  stepButtons().forEach((b) => {
    b.classList.toggle("on", b.dataset.step === name);
  });
  syncJobTabs();
  $$(".view").forEach((v) => v.classList.toggle("on", v.id === `view-${name}`));
  const titles = {
    library: ["Library", "Finished videos. Click a ready video to watch."],
    jobs: ["Jobs", "Work queue. Start runs the pipeline from empty or the next missing step. Stop halts after the current step. Resume continues and skips finished artifacts."],
    topics: ["Topics", "Set a date and time, then Schedule. Studio starts due queued topics about every 30 seconds, one at a time. Run now starts as soon as the queue is free."],
    costs: ["Costs", "Estimated Flux spend per video from illustration counts × ~4.6¢, plus today’s counters. Open the Costs tab anytime — estimates, not a fal invoice."],
    watch: ["Watch", "Play the rendered mp4, then jump into script, pictures, voice, or render."],
    create: ["New job", "Topic, length, and aspect (16:9 default). Jobs keep running on the server if you refresh."],
    script: ["Script", "Tagged version drives emotions and billboards. Raw is TTS and Gentle. Open with a topic hook (also the 9:16 short); close with a subscribe outro (16:9). Generate 9:16 builds hook+CTA audio and portrait art."],
    pictures: ["Pictures", picturesSubtitle()],
    voice: ["Voice", "OpenAI / ChatGPT TTS, ElevenLabs, or Local (Resemble Chatterbox). Audio is a WAV that Gentle will align."],
    backgrounds: ["Backgrounds", "Studio room behind the stick figure (clock, wall, floor). Billboard uses this at full color; cover stays full-bleed art."],
    music: ["Music", "Upload tracks, set loudness, pick a specific track for this job, or shuffle. Loops under the whole video including the 5s title card."],
    video: ["Video", "Gentle: Docker or local on Windows/Linux; on Mac prefer the Gentle app (or set URL). Frames and ffmpeg produce the mp4. Room and music are on their own tabs."],
    settings: ["Settings", "Keys stay on this machine in user_data/settings.json. MCP + Ngrok: remote agents use Public MCP URL with HTTP Basic only (Settings → Ngrok). Optional MCP PIN or Studio JWT. Connect YouTube in the system browser."],
    prompts: ["Prompts", "Script, Flux (fal) image prompts, ChatGPT-native image instructions, and TTS. Flux uses images.flux_instructions + art.* only — the ChatGPT playbook is never sent to fal. Your overrides save for your account only (Restore my defaults clears them)."],
  };
  const pair = titles[name] || titles.library;
  $("#page-title").textContent = pair[0];
  $("#page-sub").textContent = pair[1];
  if (name === "library") renderLibrary();
  if (name === "jobs") renderJobsQueue();
  if (name === "topics") loadTopics();
  if (name === "costs") loadCosts();
  if (name === "prompts") loadPrompts();
  if (name === "settings") {
    refreshGentleStatus();
    loadYoutube();
    loadMcpSettings();
    refreshNgrokStatus();
  }
  if (name === "backgrounds") {
    loadBackgrounds();
  }
  if (name === "music") {
    loadMusicList();
    loadSettingsForMusic();
  }
  if (name === "video") {
    fillJobYoutube();
  }
  if (name === "pictures") {
    refreshIllustrations({ lite: false });
  }
  if (name === "script") {
    try { applyScriptTab(localStorage.getItem(STORE_SCRIPT_TAB) || "tagged"); } catch { applyScriptTab("tagged"); }
  }
  if (current) setBusy(!!current.running, current.job?.error || current.job?.detail || "");
  syncIllustrationPolling();
}

async function restoreRoute() {
  const fromHash = parseLocationRoute();
  let step = (fromHash && fromHash.step) || readStoredStep() || "";
  let jobId = (fromHash && fromHash.jobId) || readStoredJob() || "";
  if (!APP_STEPS.includes(step)) {
    const running = jobIndex.find((j) => j.running || j.busy);
    step = running ? "jobs" : "library";
    jobId = "";
  }
  if (JOB_STEPS.includes(step) || step === "watch") {
    if (!jobId) {
      setStep("library");
      return;
    }
    try {
      if (step === "watch") {
        await openWatch(jobId);
      } else {
        await loadJob(jobId);
        setStep(step);
      }
      if (current?.running) startPolling();
    } catch (err) {
      try { localStorage.removeItem(STORE_JOB); } catch { /* ignore */ }
      toast(err.message || "Could not reopen the last job.", true);
      setStep("library");
    }
    return;
  }
  setStep(step);
  if (step === "jobs" && jobIndex.some((j) => j.running || j.busy)) startPolling();
}

function jobLabel(item) {
  return item.title || item.topic || item.id;
}

function jobState(item) {
  if (item.running) return item.job?.detail || item.job?.step || "running";
  if (item.stopping) return item.job?.detail || "stopping";
  if (item.paused || item.job?.step === "paused") return "paused";
  if (item.job?.step === "stopped") return "stopped";
  if (item.job?.error) return "error";
  if (item.has_video || item.status === "rendered") return "rendered";
  if (item.has_audio || item.status === "audio") return "audio";
  if (item.status === "scripted" || item.status === "aligned") return item.status;
  return item.status || "draft";
}

function isReady(item) {
  return !!(
    item?.has_video
    || item?.library_ready
    || item?.has_youtube
    || item?.youtube_url
    || item?.youtube?.url
    || item?.youtube?.video_id
  );
}

function youtubeWatchUrl(item) {
  if (!item) return "";
  return item.youtube_url || item.youtube?.url || (item.youtube_video_id || item.youtube?.video_id
    ? `https://youtu.be/${item.youtube_video_id || item.youtube.video_id}`
    : "");
}

function youtubeEmbedId(item) {
  const id = item?.youtube_video_id || item?.youtube?.video_id || "";
  if (id) return id;
  const url = youtubeWatchUrl(item);
  if (!url) return "";
  try {
    const u = new URL(url);
    if (u.hostname.includes("youtu.be")) return u.pathname.replace(/^\//, "").split("/")[0];
    return u.searchParams.get("v") || "";
  } catch {
    return "";
  }
}

function hasLocalVideo(item) {
  if (!item) return false;
  if (item.has_video) return true;
  const renders = item.renders || {};
  return ["16:9", "9:16"].some((asp) => renders[asp]?.ready);
}

function canStart(item) {
  if (!item) return false;
  if (item.running || item.busy) return false;
  if (item.can_start != null) return !!item.can_start;
  return true;
}

function canStop(item) {
  if (!item) return false;
  if (item.can_stop != null) return !!item.can_stop;
  if (item.can_pause != null) return !!item.can_pause;
  return !!(item.running || item.busy);
}

function canResume(item) {
  if (!item) return false;
  if (item.running) return false;
  if (item.can_resume != null) return !!item.can_resume;
  if (item.job?.error || item.job?.youtube_error) return true;
  if (item.paused || item.job?.step === "paused" || item.job?.step === "stopped") return true;
  if (item.resume_from && item.resume_from !== "done") return true;
  return !item.has_video;
}

function resumeLabel(item) {
  const from = item?.resume_from || item?.resumed_from || item?.job?.resumed_from;
  return from && from !== "done" ? `Resume from ${from}` : "Resume";
}

function cardHtml(item, compact = false) {
  const ready = isReady(item);
  const local = hasLocalVideo(item);
  const ytOnly = ready && !local;
  const portrait = (item.aspect || "16:9") === "9:16";
  const label = esc(jobLabel(item));
  const state = ytOnly ? "on YouTube" : jobState(item);
  const stamp = encodeURIComponent(item.updated_at || item.id);
  return `<div class="yt-wrap" data-id="${esc(item.id)}">
    <div class="yt-card ${portrait ? "portrait" : ""} ${ready ? "ready" : "pending"} ${ytOnly ? "youtube-only" : ""} ${compact ? "compact" : ""} ${item.running ? "running" : ""}" data-id="${esc(item.id)}">
    <div class="thumb">
      <div class="ph-doodle" aria-hidden="true">LK</div>
      <img src="/api/projects/${esc(item.id)}/thumbnail?t=${stamp}" alt="" onerror="this.remove()">
      ${ready ? `<span class="play" aria-hidden="true"></span>` : `<span class="chip">${esc(state)}</span>`}
      ${ytOnly ? `<span class="chip yt-chip">YouTube</span>` : ""}
      <span class="dur">${formatDuration(item.duration_seconds)}</span>
      <button type="button" class="thumb-x" data-delete="${esc(item.id)}" title="Delete job" aria-label="Delete ${label}">×</button>
    </div>
    <div class="yt-info">
      <strong>${label}</strong>
      <small>${esc(item.aspect || "16:9")} · ${esc(state)}</small>
    </div>
  </div>
  </div>`;
}

function renderLibrary() {
  const grid = $("#library-grid");
  if (!grid) return;
  const ready = jobIndex.filter(isReady);
  const items = ready;
  const pages = Math.max(1, Math.ceil(items.length / LIBRARY_PAGE) || 1);
  libraryPage = Math.min(libraryPage, pages - 1);
  const slice = items.slice(libraryPage * LIBRARY_PAGE, (libraryPage + 1) * LIBRARY_PAGE);
  const heading = $("#library-heading");
  if (heading) heading.textContent = "Ready to watch";
  grid.innerHTML = slice.length
    ? slice.map((item) => cardHtml(item)).join("")
    : `<p class="empty-lib">No finished videos yet. <button type="button" id="lib-empty-jobs">Open jobs</button> or <button type="button" id="lib-empty-create">Create a job</button></p>`;
  const count = $("#lib-count");
  if (count) count.textContent = `${ready.length} ready`;
  const pager = $("#library-pager");
  if (pager) {
    pager.hidden = items.length <= LIBRARY_PAGE;
    $("#lib-page-label").textContent = `Page ${libraryPage + 1} of ${pages}`;
    $("#lib-prev").disabled = libraryPage <= 0;
    $("#lib-next").disabled = libraryPage >= pages - 1;
  }
}

function renderJobsQueue() {
  const list = $("#jobs-queue");
  if (!list) return;
  const filtered = filteredJobs();
  if (!jobIndex.length) {
    list.innerHTML = `<p class="empty-lib">No jobs yet. <button type="button" id="jobs-empty-create">Create a job</button></p>`;
    updatePager("#jobs-pager", "#jobs-page-label", "#jobs-prev", "#jobs-next", 0, 1, 0);
    return;
  }
  if (!filtered.length) {
    list.innerHTML = `<p class="empty-lib">No jobs match this filter.</p>`;
    updatePager("#jobs-pager", "#jobs-page-label", "#jobs-prev", "#jobs-next", 0, 1, 0);
    return;
  }
  const pageData = paginate(filtered, jobsPage);
  jobsPage = pageData.page;
  updatePager("#jobs-pager", "#jobs-page-label", "#jobs-prev", "#jobs-next", pageData.page, pageData.pages, pageData.total);
  list.innerHTML = pageData.items.map((item) => {
    const pulse = item.running ? "running" : (item.paused || item.job?.step === "paused" ? "paused" : (item.job?.step === "stopped" ? "stopped" : ""));
    const startOff = (canStart(item) && !membershipLocked) ? "" : "disabled";
    const stopOff = canStop(item) ? "" : "disabled";
    const resumeOff = (canResume(item) && !membershipLocked) ? "" : "disabled";
    const errRaw = item.job?.error || item.job?.youtube_error || item.youtube_error || "";
    const errSmall = errRaw
      ? `<small class="job-card-error">${esc(simplifyJobError(errRaw).message)}</small>`
      : "";
    return `<div class="job-card ${pulse}${errRaw ? " has-error" : ""}" data-id="${esc(item.id)}">
      <button type="button" class="job-card-main" data-open="${esc(item.id)}">
        <span class="dot"></span>
        <span>
          <strong>${esc(jobLabel(item))}</strong>
          <small>${item.duration_seconds}s · ${esc(item.aspect || "16:9")} · ${esc(jobState(item))}</small>
          ${errSmall}
        </span>
      </button>
      <div class="job-actions">
        <button type="button" data-start="${esc(item.id)}" ${startOff} title="Run the pipeline from empty or the next missing step">Start</button>
        <button type="button" data-stop="${esc(item.id)}" ${stopOff} title="Pause/Stop — halt after the current step">Stop</button>
        <button type="button" class="job-resume" data-resume="${esc(item.id)}" ${resumeOff} title="${esc(resumeLabel(item))}">Resume</button>
        ${errRaw ? `<button type="button" data-job-error="${esc(item.id)}">Details</button>` : ""}
      </div>
    </div>`;
  }).join("");
}

function jobMatchesFilter(item, filter) {
  if (filter === "all") return true;
  if (filter === "running") return !!(item.running || item.busy);
  if (filter === "paused") {
    return !!(item.paused || item.job?.step === "paused" || item.job?.step === "stopped");
  }
  if (filter === "ready") return isReady(item);
  if (filter === "draft") return !isReady(item) && !(item.running || item.busy) && !item.job?.error;
  if (filter === "error") return !!(item.job?.error || item.job?.youtube_error || item.youtube_error);
  if (filter === "startable") return canStart(item);
  if (filter === "resumable") return canResume(item);
  return true;
}

function filteredJobs() {
  const q = jobsSearch.trim().toLowerCase();
  return jobIndex.filter((item) => {
    if (!jobMatchesFilter(item, jobsFilter)) return false;
    if (!q) return true;
    const hay = `${jobLabel(item)} ${item.id || ""} ${item.topic || ""} ${jobState(item)}`.toLowerCase();
    return hay.includes(q);
  });
}

function renderWatchRelated() {
  const more = $("#watch-more");
  if (!more) return;
  const others = jobIndex.filter((j) => isReady(j) && j.id !== current?.id).slice(0, 6);
  const heading = $("#watch-more-heading");
  if (heading) heading.hidden = !others.length;
  more.innerHTML = others.map((item) => cardHtml(item)).join("");
}

function renderJobList() {
  renderJobsQueue();
}

function setBusy(running, detail = "") {
  const banner = $("#run-status");
  const textEl = $("#run-status-text");
  const text = detail || (running ? "Working…" : "Idle.");
  const hasError = Boolean(current?.job?.error || current?.job?.youtube_error || current?.youtube_error) && !running;
  const hideIdle = !running && !hasError && (!detail || detail === "Idle." || detail === "Done.");
  const rawPct = current?.job?.progress_pct ?? current?.progress_pct;
  let pct = Number(rawPct);
  if (!Number.isFinite(pct)) pct = running ? 2 : (hasError ? 0 : 100);
  pct = Math.max(0, Math.min(100, Math.round(pct)));
  if (hasError && shouldShowErrorModal(current?.job?.error || current?.job?.youtube_error || current?.youtube_error || detail)) {
    showJobErrorModal(current?.job?.error || current?.job?.youtube_error || current?.youtube_error || detail);
  }
  if (banner) {
    banner.classList.toggle("busy", !!running);
    banner.classList.toggle("bad", hasError);
    banner.classList.toggle("clickable", hasError);
    banner.style.setProperty("--run-pct", `${running || hasError || detail === "Done." ? pct : 0}%`);
    banner.setAttribute("aria-valuenow", String(pct));
    if (hasError) {
      banner.dataset.errorRaw = current?.job?.error || current?.job?.youtube_error || current?.youtube_error || detail || "";
      banner.title = "Click for details";
    } else {
      delete banner.dataset.errorRaw;
      banner.removeAttribute("title");
    }
    const shortErr = hasError ? simplifyJobError(text).message : text;
    const label = running
      ? `${jobLabel(current || {})} · ${text} · ${pct}% (safe to refresh)`
      : (hasError ? `${shortErr} · Details` : (detail || ""));
    if (textEl) textEl.textContent = label;
    else banner.textContent = label;
    banner.hidden = hideIdle;
  }
  ["#gen-script", "#save-script", "#gen-audio", "#align", "#render", "#gen-flux", "#job-yt-upload", "#watch-yt-upload", "#create-form button"].forEach((sel) => {
    $$(sel).forEach((el) => { el.disabled = !!running || membershipLocked; });
  });
  // Image provider stays enabled mid-generation so the user can switch backends — unless locked.
  if ($("#image-provider")) $("#image-provider").disabled = !!membershipLocked;
  $$(".pic-regen").forEach((el) => {
    const article = el.closest("article.pic");
    const thisRegen = article && regenTargetKey && article.dataset.file === regenTargetKey;
    el.disabled = !!running || !!thisRegen || membershipLocked;
    if (thisRegen) el.textContent = "Regenerating…";
    else if (!running) el.textContent = "Regenerate";
  });
  syncResumeButtons();
}

$("#run-status")?.addEventListener("click", () => {
  const banner = $("#run-status");
  const raw = banner?.dataset?.errorRaw;
  if (!raw || !banner.classList.contains("bad")) return;
  showJobErrorModal(raw, { force: true });
});

async function refreshJobs(selectId) {
  jobIndex = await api("/api/jobs");
  renderJobList();
  if ($("#view-library")?.classList.contains("on")) renderLibrary();
  if ($("#view-jobs")?.classList.contains("on")) renderJobsQueue();
  if ($("#view-topics")?.classList.contains("on")) await loadTopics({ silent: true });
  if ($("#view-watch")?.classList.contains("on")) renderWatchRelated();
  if (selectId) {
    const match = jobIndex.find((j) => j.id === selectId);
    if (match && current && current.id === selectId) {
      current.running = match.running;
      current.busy = match.busy;
      current.paused = match.paused;
      current.job = match.job;
      current.progress_pct = match.progress_pct ?? match.job?.progress_pct;
      current.can_resume = match.can_resume;
      current.can_start = match.can_start;
      current.can_stop = match.can_stop;
      current.resume_from = match.resume_from;
      current.has_video = match.has_video;
      current.has_audio = match.has_audio;
      syncResumeButtons();
      syncJobTabs();
    }
  }
}

async function loadJob(id, { poll = true } = {}) {
  current = await api(`/api/projects/${id}`);
  localStorage.setItem(STORE_JOB, id);
  $("#script-tagged").value = current.script_tagged || "";
  $("#script-raw").value = current.script_raw || "";
  fillYoutubeMetaFields(current);
  if (current.word_count) {
    updateScriptMetaMissing();
  } else {
    $("#script-meta").textContent = "No script yet.";
  }
  await refreshIllustrations({ lite: false, rebuild: true });
  if (current.has_audio) {
    $("#audio-player").hidden = false;
    $("#audio-player").src = `/api/projects/${id}/audio-file?t=${Date.now()}`;
  } else {
    $("#audio-player").hidden = true;
  }
  fillRenderAspectStatus();
  fillVideoPlayer(id);
  if (current.tts_provider || current.voice_provider) {
    $("#voice-provider").value = current.tts_provider || current.voice_provider;
  }
  updateVoiceHint();
  if ($("#image-provider")) $("#image-provider").value = current.image_provider || "flux";
  updatePicturesHelp();
  await loadVoices();
  if (current.voice_id) $("#voice-id").value = current.voice_id;
  const aspect = current.aspect || "16:9";
  if ($("#render-aspect")) $("#render-aspect").value = aspect;
  if ($("#render-layout")) $("#render-layout").value = current.video_layout || "cover";
  if ($("#render-art-style")) $("#render-art-style").value = current.art_style || "classic";
  if ($("#render-character-size")) $("#render-character-size").value = current.character_size || "large";
  if ($("#include-bubblehead")) $("#include-bubblehead").checked = current.include_bubblehead !== false;
  if ($("#generate-9x16")) $("#generate-9x16").checked = current.generate_9x16 === true;
  updateMusicPick();
  loadBackgrounds();
  loadMusicList();
  fillJobYoutube();
  renderJobList();
  const detail = current.job?.error || current.job?.detail || "";
  if (!(current.job?.error || current.job?.youtube_error || current.youtube_error)) {
    lastErrorModalKey = "";
  }
  setBusy(!!current.running, detail);
  syncResumeButtons();
  syncJobTabs();
  if (current.id) {
    lastTickState[current.id] = {
      ...(lastTickState[current.id] || {}),
      running: !!current.running,
      busy: !!current.busy,
      step: current.job?.step,
      has_video: !!current.has_video || readyAspects().length > 0,
    };
  }
  if (poll && current.running) startPolling();
  else syncIllustrationPolling();
}

function readyAspects() {
  const renders = current?.renders || {};
  return ["16:9", "9:16"].filter((asp) => renders[asp]?.ready);
}

function preferredPlayAspect() {
  const ready = readyAspects();
  const last = current?.last_render_aspect;
  if (last && ready.includes(last)) return last;
  const currentAsp = current?.aspect || "16:9";
  if (ready.includes(currentAsp)) return currentAsp;
  return ready[0] || currentAsp;
}

function videoSrc(id, aspect) {
  const q = aspect ? `&aspect=${encodeURIComponent(aspect)}` : "";
  return `/api/projects/${id}/video?t=${Date.now()}${q}`;
}

function fillAspectPick(select, wrap, selected) {
  if (!select || !wrap) return;
  const ready = readyAspects();
  wrap.hidden = ready.length < 1;
  if (!ready.length) return;
  select.innerHTML = ready.map((asp) => {
    const file = current?.renders?.[asp]?.file || "";
    return `<option value="${esc(asp)}"${asp === selected ? " selected" : ""}>${esc(asp)}${file ? ` · ${esc(file)}` : ""}</option>`;
  }).join("");
}

function fillRenderAspectStatus() {
  const el = $("#render-aspect-status");
  if (!el || !current) return;
  const renders = current.renders || {};
  const a16 = renders["16:9"]?.ready;
  const a9 = renders["9:16"]?.ready;
  el.textContent = `${a16 ? "16:9 ready" : "16:9 not rendered"} · ${a9 ? "9:16 ready" : "9:16 not rendered"}`;
}

function fillVideoPlayer(id, { autoplay = false } = {}) {
  const player = $("#video-player");
  if (!player) return;
  const playAsp = preferredPlayAspect();
  const ready = readyAspects();
  fillAspectPick($("#video-aspect-pick"), $("#video-aspect-pick-wrap"), playAsp);
  if (!ready.length && !current.has_video) {
    player.hidden = true;
    player.removeAttribute("src");
    return;
  }
  player.hidden = false;
  player.classList.toggle("portrait", playAsp === "9:16");
  player.src = videoSrc(id, playAsp);
  if (autoplay) player.play().catch(() => {});
}

function autoloadReadyVideo({ switchToVideo = true } = {}) {
  if (!current?.id) return false;
  const hasVideo = !!current.has_video || readyAspects().length > 0;
  if (!hasVideo) return false;
  fillVideoPlayer(current.id, { autoplay: true });
  const watchOn = $("#view-watch")?.classList.contains("on");
  if (watchOn) {
    const playAsp = preferredPlayAspect();
    const player = $("#watch-player");
    if (player) {
      fillAspectPick($("#watch-aspect-pick"), $("#watch-aspect-pick-wrap"), playAsp);
      player.classList.toggle("portrait", playAsp === "9:16");
      player.src = videoSrc(current.id, playAsp);
      player.play().catch(() => {});
    }
    if ($("#watch-sub")) {
      $("#watch-sub").textContent = `${formatDuration(current.duration_seconds)} · ${playAsp} · Ready`;
    }
  } else if (switchToVideo) {
    const active = document.querySelector(".content-panel .view.on, #view-watch.on, #view-library.on")?.id || "";
    const onJobTab = ["view-script", "view-pictures", "view-voice", "view-backgrounds", "view-music", "view-video"].includes(active);
    if (onJobTab || active === "view-video") setStep("video");
  }
  syncYoutubeUploadButtons();
  fillJobYoutube();
  fillRenderAspectStatus();
  return true;
}

async function openWatch(id) {
  await loadJob(id, { poll: false });
  setStep("watch");
  $("#page-title").textContent = jobLabel(current);
  $("#page-sub").textContent = "Play it large, or open the job to edit script, pictures, voice, or render.";
  $("#watch-title").textContent = jobLabel(current);
  const playAsp = preferredPlayAspect();
  const local = hasLocalVideo(current);
  const ytUrl = youtubeWatchUrl(current);
  const embedId = youtubeEmbedId(current);
  $("#watch-sub").textContent = local
    ? `${formatDuration(current.duration_seconds)} · ${playAsp} · Ready`
    : (ytUrl
      ? `${formatDuration(current.duration_seconds)} · on YouTube (local file missing)`
      : `${formatDuration(current.duration_seconds)} · Ready`);
  const player = $("#watch-player");
  const embedWrap = $("#watch-youtube-embed");
  const frame = $("#watch-youtube-frame");
  const fallback = $("#watch-youtube-fallback");
  const link = $("#watch-youtube-link");
  fillAspectPick($("#watch-aspect-pick"), $("#watch-aspect-pick-wrap"), playAsp);
  if (local) {
    if (embedWrap) embedWrap.hidden = true;
    if (frame) frame.removeAttribute("src");
    player.hidden = false;
    player.classList.toggle("portrait", playAsp === "9:16");
    player.src = videoSrc(id, playAsp);
    player.play().catch(() => {});
  } else if (ytUrl) {
    player.hidden = true;
    player.removeAttribute("src");
    if (embedWrap) embedWrap.hidden = false;
    if (frame && embedId) {
      frame.src = `https://www.youtube.com/embed/${encodeURIComponent(embedId)}`;
      if (fallback) fallback.hidden = true;
    } else if (fallback) {
      fallback.hidden = false;
    }
    if (link) {
      link.href = ytUrl;
      link.textContent = "open on YouTube";
    }
  } else {
    player.hidden = true;
    player.removeAttribute("src");
    if (embedWrap) embedWrap.hidden = true;
  }
  renderWatchRelated();
  syncYoutubeUploadButtons();
  fillWatchYoutube();
}

async function openFromLibrary(id) {
  const item = jobIndex.find((j) => j.id === id);
  if (isReady(item)) {
    await openWatch(id);
    return;
  }
  await loadJob(id);
  const step = current.script_tagged ? (current.has_audio ? "video" : "script") : "script";
  setStep(step);
}

function updatePicturesHelp() {
  const help = $("#pictures-help");
  if (!help) return;
  const aspect = current?.aspect || "16:9";
  const layout = currentLayout();
  const size = layout === "billboard"
    ? "16:9 (1920×1080) opaque TV-screen billboard"
    : (aspect === "9:16"
      ? "exactly 9:16 (1080×1920 portrait)"
      : aspect === "both"
        ? "both 16:9 (1920×1080) and 9:16 (1080×1920)"
        : "exactly 16:9 (1920×1080 landscape)");
  const provider = current?.image_provider || "flux";
  const shape = layout === "billboard"
    ? "Billboard layout uses a 16:9 TV screen (left on 16:9, top on 9:16), fully opaque, no brown frame. Line art stays 1920×1080 even on 9:16 video; only the cover uses 1080×1920."
    : "Cover layout fills the whole frame at the job size (16:9 → 1920×1080, 9:16 → 1080×1920). Not square or 4:5.";
  if (provider === "chatgpt") {
    help.innerHTML = `This job uses <strong>ChatGPT native</strong> images. Generate the <strong>5s title card</strong> first (studio room + Bubblehead + TV with topic title on screen) at the video size, then each line. ${shape} Upload here or MCP-save with <code>kind=cover</code> for the intro. <strong>Regenerate cover</strong> keeps prior versions in history — pick one with <strong>Use this</strong>.`;
  } else if (provider === "comfyui") {
    help.innerHTML = `This job uses <strong>ComfyUI</strong>. Click <strong>Generate backgrounds with ComfyUI</strong> to POST the uploaded API JSON to your local ComfyUI at the job size (${size}), or <strong>Regenerate</strong> / <strong>Regenerate cover</strong> next to one picture. Cover history keeps prior title cards. Safe to refresh. ${shape}`;
  } else {
    help.innerHTML = `This job uses <strong>Flux</strong> (<code>${FLUX_MODEL_ID}</code>). Click <strong>Generate backgrounds with Flux</strong> to queue missing files at ${size}, or <strong>Regenerate</strong> / <strong>Regenerate cover</strong> next to one picture. Cover history keeps prior title cards. Safe to refresh. ~4.6¢/image at ~2MP. ${shape}`;
  }
  updateGenerateButton();
}

function updateGenerateButton() {
  const btn = $("#gen-flux");
  if (!btn) return;
  const p = current?.image_provider || lastSettings.image_provider || "flux";
  if (p === "comfyui") btn.textContent = "Generate backgrounds with ComfyUI";
  else if (p === "chatgpt") btn.textContent = "Generate backgrounds";
  else btn.textContent = "Generate backgrounds with Flux";
}

function stepForJobKind(kind) {
  if (kind === "script") return "script";
  if (kind === "audio") return "voice";
  if (kind === "illustrations" || kind === "cover") return "pictures";
  if (kind === "resume" || kind === "start") return "video";
  return "video";
}

function stepForResume(from) {
  if (from === "script") return "script";
  if (from === "illustrations" || from === "cover") return "pictures";
  if (from === "audio") return "voice";
  return "video";
}

function syncResumeButtons() {
  const show = !!(current && canResume(current));
  ["#header-resume", "#watch-resume", "#video-resume"].forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    el.hidden = !show;
    el.disabled = !!membershipLocked;
    el.title = membershipLocked
      ? "Membership required to resume"
      : (show ? resumeLabel(current) : "Resume");
  });
}

async function resumeJob(id, { stay = false } = {}) {
  if (!requireMembership("resume jobs")) return;
  if (!id) return toast("Create or pick a job first.", true);
  try {
    const started = await api(`/api/projects/${encodeURIComponent(id)}/resume`, { method: "POST", body: {} });
    if (started.attached) {
      toast(started.stopping || started.busy ? "Continuing this job’s current step." : "This job is already running.");
      if (current?.id === id) await loadJob(id);
      startPolling();
      await refreshJobs(id);
      return;
    }
    const from = started.resumed_from || started.resume_from || started.step || "next step";
    if (!started.running && from === "done") {
      toast(started.detail || started.job?.detail || "Already complete.");
      await refreshJobs();
      if (current?.id === id) await loadJob(id, { poll: false });
      return;
    }
    toast(`Resuming from ${from}.`);
    if (!stay && (!current || current.id !== id)) await loadJob(id);
    else if (current?.id === id) {
      current.running = true;
      current.job = started.job || started;
      current.resume_from = started.resume_from;
      current.can_resume = false;
    }
    if (!stay) setStep(stepForResume(from));
    if (current?.id === id) setBusy(true, started.detail || started.job?.detail || `Resuming from ${from}…`);
    startPolling();
    await refreshJobs(id);
  } catch (err) {
    toast(err.message, true);
  }
}

async function startJob(id) {
  if (!requireMembership("start jobs")) return;
  if (!id) return toast("Create or pick a job first.", true);
  try {
    const started = await api(`/api/projects/${encodeURIComponent(id)}/start`, { method: "POST", body: {} });
    if (started.attached) {
      toast("This job is already running.");
      startPolling();
      await refreshJobs(id);
      return;
    }
    const from = started.resumed_from || started.resume_from || started.step || "start";
    if (!started.running && from === "done") {
      toast(started.detail || started.job?.detail || "Already complete.");
      await refreshJobs();
      return;
    }
    toast(`Starting from ${from}.`);
    if (current?.id === id) {
      current.running = true;
      current.job = started.job || started;
      setBusy(true, started.detail || started.job?.detail || `Starting from ${from}…`);
    }
    startPolling();
    await refreshJobs(id);
  } catch (err) {
    toast(err.message, true);
  }
}

async function stopJob(id) {
  if (!id) return toast("Create or pick a job first.", true);
  try {
    const stopped = await api(`/api/projects/${encodeURIComponent(id)}/stop`, { method: "POST", body: {} });
    toast(stopped.detail || stopped.job?.detail || "Stopping after the current step.");
    if (current?.id === id) {
      current.running = false;
      current.busy = stopped.busy;
      current.job = stopped.job || stopped;
      setBusy(false, stopped.detail || "Stopping after the current step…");
    }
    startPolling();
    await refreshJobs(id);
  } catch (err) {
    toast(err.message, true);
  }
}

function handleJobActionClick(e) {
  const errBtn = e.target.closest("[data-job-error]");
  if (errBtn) {
    e.preventDefault();
    e.stopPropagation();
    const item = jobIndex.find((j) => j.id === errBtn.dataset.jobError);
    const raw = item?.job?.error || item?.job?.youtube_error || item?.youtube_error || "";
    if (raw) showJobErrorModal(raw, { force: true });
    return true;
  }
  const startBtn = e.target.closest("[data-start]");
  if (startBtn) {
    e.preventDefault();
    e.stopPropagation();
    startJob(startBtn.dataset.start);
    return true;
  }
  const stopBtn = e.target.closest("[data-stop]");
  if (stopBtn) {
    e.preventDefault();
    e.stopPropagation();
    stopJob(stopBtn.dataset.stop);
    return true;
  }
  if (handleResumeClick(e)) return true;
  return false;
}

function handleResumeClick(e) {
  const btn = e.target.closest("[data-resume]");
  if (!btn) return false;
  e.preventDefault();
  e.stopPropagation();
  const stay = Boolean(e.target.closest("#view-jobs"));
  resumeJob(btn.dataset.resume, { stay });
  return true;
}

function pictureFileKey(job) {
  if (job?.role === "shorts_line") {
    const name = String(job?.filename || "").replace(/\.png$/i, "");
    return name ? `9x16:${name}` : "9x16:line";
  }
  const name = String(job?.filename || "").replace(/\.png$/i, "");
  if (name) return name;
  if (job?.role === "cover") return "script_cover";
  return name;
}

function pictureIsCover(job) {
  const key = pictureFileKey(job);
  return job?.role === "cover" || key === "script_cover" || key.startsWith("script_cover");
}

function coverAspectOf(job) {
  if (job?.video_aspect) return job.video_aspect;
  const key = pictureFileKey(job);
  if (key.includes("9x16") || key.includes("9_16")) return "9:16";
  if (key.includes("16x9") || key.includes("16_9")) return "16:9";
  return job?.aspect || current?.aspect || "16:9";
}

function pictureSrc(job) {
  if (job?.url && (job.ready ?? job.has_image)) return job.url;
  if (!current?.id || !job?.filename) return "";
  const mtime = job.mtime || Date.now();
  if (pictureIsCover(job)) {
    const aspect = coverAspectOf(job);
    return `/api/projects/${current.id}/cover?aspect=${encodeURIComponent(aspect)}&t=${mtime}`;
  }
  return `/api/projects/${current.id}/billboards/${job.filename}?t=${mtime}`;
}

function picturePlaceholderHtml(job) {
  const name = esc(job.filename || (pictureIsCover(job) ? "script_cover.png" : "pending.png"));
  return `<div class="ph"><span>waiting</span><small>${name}</small></div>`;
}

function pictureCardHtml(job) {
  const key = pictureFileKey(job);
  const ready = !!(job.ready ?? job.has_image);
  const isCover = pictureIsCover(job);
  const isShorts = job?.role === "shorts_line";
  const coverAspect = isCover ? coverAspectOf(job) : "";
  const genAt = isCover
    ? generateAtLine(coverAspect, "cover")
    : isShorts
      ? generateAtLine("9:16", "cover")
      : generateAtLine(current?.aspect, current?.video_layout);
  const title = isCover
    ? `Title card (5s intro, ${coverAspect})`
    : isShorts
      ? `9:16 short · ${job.topic || key}`
      : (job.topic || key);
  const sizeHint = job.needs_size_regen
    ? "wrong size — will regenerate at render (do not stretch the other aspect)"
    : (isCover
      ? `${genAt} · studio room + Bubblehead + TV title card`
      : isShorts
        ? `${genAt} · hook/CTA line for the 9:16 short (portrait, not stretched 16:9)`
        : genAt);
  const body = isCover
    ? "Cold-open still for this aspect. Studio room, Bubblehead outside, topic title on the TV. Plays before lip-sync."
    : (job.line || job.raw || "");
  const prompt = job.prompt || job.image_prompt || (isCover ? current?.cover_prompt : "") || "";
  const filename = job.filename || (isCover ? `script_cover_${coverAspect === "9:16" ? "9x16" : "16x9"}.png` : `${key}.png`);
  const kind = isCover ? "cover" : (isShorts ? "shorts_line" : "billboard");
  const regenKind = isCover ? "cover" : "line";
  const waiting = !!(job.needs_regen || job.waiting_for === "chatgpt");
  const regenerating = regenTargetKey === key;
  const indexVal = job.index != null ? job.index : (isCover ? (coverAspect === "9:16" ? -2 : -1) : "");
  const media = ready
    ? `<img src="${esc(pictureSrc(job))}" alt="" data-mtime="${esc(job.mtime || "")}" />`
    : picturePlaceholderHtml(job);
  return `<article class="pic ${ready ? "ready" : "pending"}${waiting ? " waiting" : ""}${regenerating ? " regenerating" : ""}" data-file="${esc(key)}" data-mtime="${esc(ready ? (job.mtime || "") : "")}">
      <div class="pic-media">
        ${media}
        <button type="button" class="pic-regen" data-regen="${esc(filename)}" data-kind="${regenKind}" data-index="${esc(indexVal)}" data-aspect="${esc(isShorts ? "9:16" : (isCover ? coverAspect : ""))}"${regenerating ? " disabled" : ""}>${regenerating ? "Regenerating…" : (isCover ? "Regenerate cover" : "Regenerate")}</button>
        ${waiting ? `<div class="pic-waiting">waiting for ChatGPT</div>` : ""}
      </div>
      <div>
        <strong>${esc(title)}</strong>
        <div class="hint">${esc(sizeHint)}</div>
        <div>${esc(body)}</div>
        ${isCover ? `<div class="cover-history" data-cover-aspect="${esc(coverAspect)}" data-loaded="0"><div class="hint">Previous covers load here after generate/regenerate.</div></div>` : ""}
        <label>Upload PNG/JPG
          <input type="file" accept="image/*" data-filename="${esc(filename)}" data-kind="${kind}" data-aspect="${esc(isCover ? coverAspect : (isShorts ? "9:16" : ""))}" />
        </label>
        <pre>${esc(prompt)}</pre>
        ${prompt ? `<div class="hint">${prompt.length} chars${job.prompt_for ? ` · ${esc(job.prompt_for)}` : ""}</div>` : ""}
      </div>
    </article>`;
}

function linesAsIllustrationPayload(lines) {
  const jobs = [];
  ["16:9", "9:16"].forEach((aspect) => {
    const slug = aspect === "9:16" ? "9x16" : "16x9";
    const info = current?.covers?.[aspect] || {};
    jobs.push({
      role: "cover",
      filename: `script_cover_${slug}.png`,
      video_aspect: aspect,
      aspect,
      ready: !!info.ready,
      has_image: !!info.ready,
      mtime: Date.now(),
      prompt: current?.cover_prompt || "",
      topic: current?.topic || "",
      line: "",
      index: aspect === "9:16" ? -2 : -1,
    });
  });
  const seen = new Set();
  (lines || []).forEach((line, i) => {
    if (!line.filename || seen.has(line.filename)) return;
    seen.add(line.filename);
    jobs.push({
      index: i,
      filename: `${line.filename}.png`,
      ready: !!line.has_image,
      has_image: !!line.has_image,
      mtime: Date.now(),
      prompt: line.image_prompt || "",
      topic: line.topic || line.filename,
      line: line.raw || "",
    });
  });
  return { jobs, missing: jobs.filter((job) => !job.ready).length };
}

function picturesStructureMatches(data) {
  const list = $("#picture-list");
  if (!list) return false;
  const keys = (data.jobs || []).map(pictureFileKey);
  const existing = [...list.querySelectorAll("article.pic")];
  return existing.length === keys.length && existing.every((el, i) => el.dataset.file === keys[i]);
}

function syncPictureRegenState(article, job) {
  const key = pictureFileKey(job);
  const waiting = !!(job.needs_regen || job.waiting_for === "chatgpt");
  const regenerating = regenTargetKey === key;
  article.classList.toggle("waiting", waiting);
  article.classList.toggle("regenerating", regenerating);
  const mediaCol = article.querySelector(".pic-media") || article;
  let waitEl = article.querySelector(".pic-waiting");
  if (waiting) {
    if (!waitEl) {
      waitEl = document.createElement("div");
      waitEl.className = "pic-waiting";
      waitEl.textContent = "waiting for ChatGPT";
      mediaCol.append(waitEl);
    }
  } else if (waitEl) {
    waitEl.remove();
  }
  const regenBtn = article.querySelector(".pic-regen");
  if (regenBtn) {
    const busy = regenerating || !!(current?.running || current?.busy);
    regenBtn.disabled = busy;
    regenBtn.textContent = regenerating
      ? "Regenerating…"
      : (pictureIsCover(job) ? "Regenerate cover" : "Regenerate");
  }
}

function patchPictureSlots(data) {
  const list = $("#picture-list");
  if (!list) return;
  (data.jobs || []).forEach((job) => {
    const key = pictureFileKey(job);
    const article = list.querySelector(`article.pic[data-file="${CSS.escape(key)}"]`);
    if (!article) return;
    const ready = !!(job.ready ?? job.has_image);
    const mtime = String(job.mtime || "");
    const prevMtime = article.dataset.mtime || "";
    const media = article.querySelector("img, .ph");
    if (ready) {
      article.classList.add("ready");
      article.classList.remove("pending");
      const src = pictureSrc(job);
      if (media?.tagName === "IMG") {
        if (media.dataset.mtime !== mtime) {
          media.src = src;
          media.dataset.mtime = mtime;
        }
      } else {
        const img = document.createElement("img");
        img.alt = "";
        img.src = src;
        img.dataset.mtime = mtime;
        media?.replaceWith(img);
      }
      article.dataset.mtime = mtime;
      if (regenTargetKey === key && mtime && mtime !== prevMtime) regenTargetKey = "";
    } else {
      article.classList.add("pending");
      article.classList.remove("ready");
      if (!media || media.tagName === "IMG") {
        const wrap = document.createElement("div");
        wrap.innerHTML = picturePlaceholderHtml(job);
        const ph = wrap.firstElementChild;
        if (media) media.replaceWith(ph);
        else (article.querySelector(".pic-media") || article).prepend(ph);
      }
      article.dataset.mtime = "";
    }
    syncPictureRegenState(article, job);
    if (pictureIsCover(job) && ready && mtime && mtime !== prevMtime) {
      const hist = article.querySelector(".cover-history");
      if (hist) {
        hist.dataset.loaded = "0";
        loadCoverHistoryInto(hist);
      }
    }
  });
}

function paintPictures(data, { rebuild = false } = {}) {
  const list = $("#picture-list");
  if (!list) return;
  const jobs = data?.jobs || [];
  if (!jobs.length) {
    list.innerHTML = "<p>Generate a script first.</p>";
    return;
  }
  if (!rebuild && picturesStructureMatches(data)) {
    patchPictureSlots(data);
    return;
  }
  list.innerHTML = jobs.map(pictureCardHtml).join("");
  refreshCoverHistoryGalleries();
}

async function refreshCoverHistoryGalleries({ force = false } = {}) {
  if (!current?.id) return;
  const nodes = [...document.querySelectorAll(".cover-history[data-cover-aspect]")];
  if (force) nodes.forEach((el) => { el.dataset.loaded = "0"; });
  await Promise.all(nodes.map((el) => loadCoverHistoryInto(el, { force })));
}

async function loadCoverHistoryInto(el, { force = false } = {}) {
  if (!current?.id || !el) return;
  if (!force && el.dataset.loaded === "1") return;
  const aspect = el.dataset.coverAspect || "16:9";
  try {
    const data = await api(`/api/projects/${current.id}/covers/history?aspect=${encodeURIComponent(aspect)}`);
    const versions = (data.history && data.history[aspect]) || [];
    if (!versions.length) {
      el.innerHTML = `<div class="hint">No prior covers yet — regenerate to start history.</div>`;
      el.dataset.loaded = "1";
      return;
    }
    const cards = versions.slice().reverse().map((v) => {
      const active = v.active ? " active" : "";
      return `<button type="button" class="cover-hist-item${active}" data-use-cover="${esc(v.version_id)}" data-aspect="${esc(aspect)}" title="${esc(v.version_id)}">
        <img src="${esc(v.url)}" alt="" />
        <span>${esc(v.version_id)}${v.active ? " · active" : ""}</span>
      </button>`;
    }).join("");
    el.innerHTML = `<div class="cover-hist-label">Cover history</div><div class="cover-hist-row">${cards}</div>`;
    el.dataset.loaded = "1";
  } catch (_) {
    el.innerHTML = `<div class="hint">Could not load cover history.</div>`;
  }
}

function renderPictures(lines) {
  paintPictures(linesAsIllustrationPayload(lines), { rebuild: true });
}

function illustrationWorkRunning() {
  const job = current?.job || {};
  const step = String(job.step || job.kind || "").toLowerCase();
  return !!(current?.running || current?.busy) && (step === "illustrations" || step === "cover");
}

function picturesViewOn() {
  return !!$("#view-pictures")?.classList.contains("on");
}

function shouldPollIllustrations() {
  if (!current?.id) return false;
  if (illustrationWorkRunning()) return true;
  if (!picturesViewOn()) return false;
  const missing = lastIllust?.missing ?? current.missing_illustrations ?? 0;
  const pending = lastIllust?.pending_regen?.length || 0;
  return missing > 0 || pending > 0 || !!regenTargetKey;
}

function stopIllustrationPolling() {
  clearInterval(illustTimer);
  illustTimer = null;
}

function syncIllustrationPolling() {
  if (shouldPollIllustrations()) {
    if (!illustTimer) {
      illustTimer = setInterval(() => { refreshIllustrations({ lite: true }); }, ILLUST_POLL_MS);
      refreshIllustrations({ lite: true });
    }
  } else {
    stopIllustrationPolling();
  }
}

function updatePicturesLive(data) {
  const el = $("#pictures-live");
  if (!el) return;
  const jobs = data?.jobs || [];
  if (!jobs.length) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  const ready = jobs.filter((job) => job.ready ?? job.has_image).length;
  const missing = data?.missing ?? Math.max(0, jobs.length - ready);
  el.hidden = false;
  const pending = (data?.pending_regen || jobs.filter((job) => job.needs_regen)).length;
  if (regenTargetKey || illustrationWorkRunning()) {
    el.textContent = `${ready} of ${jobs.length} pictures ready · regenerating`;
  } else if (pending > 0) {
    el.textContent = `${ready} of ${jobs.length} pictures ready · waiting for ChatGPT`;
  } else if (missing === 0) {
    el.textContent = `${ready} of ${jobs.length} pictures ready.`;
  } else if (illustTimer) {
    el.textContent = `${ready} of ${jobs.length} pictures ready · loading as files appear`;
  } else {
    el.textContent = `${ready} of ${jobs.length} pictures ready · ${missing} still missing`;
  }
}

function updateScriptMetaMissing() {
  const el = $("#script-meta");
  if (!el || !current?.word_count) return;
  const missing = lastIllust?.missing ?? current.missing_illustrations ?? 0;
  let text = `${current.word_count} spoken words · target ${current.duration_seconds}s · ${missing} pictures still missing`;
  const warn = current.script_warnings?.[0];
  if (warn) text += ` · ${warn}`;
  el.textContent = text;
}

function applyIllustrationPayload(data) {
  lastIllust = data;
  if (!current || !data) return;
  const jobs = data.jobs || [];
  const aspect = current.aspect || "16:9";
  const cover = jobs.find((job) => pictureIsCover(job) && coverAspectOf(job) === aspect)
    || jobs.find((job) => pictureIsCover(job));
  current.has_cover = !!(cover && (cover.ready ?? cover.has_image));
  current.missing_illustrations = data.missing ?? jobs.filter((job) => !(job.ready ?? job.has_image)).length;
  if (current.lines) {
    const ready = new Set(
      jobs.filter((job) => job.ready ?? job.has_image).map((job) => pictureFileKey(job))
    );
    current.lines.forEach((line) => {
      if (line.filename) line.has_image = ready.has(line.filename);
    });
  }
}

async function refreshIllustrations({ lite = true, rebuild = false } = {}) {
  if (!current?.id) {
    stopIllustrationPolling();
    return;
  }
  const id = current.id;
  const seq = ++illustSeq;
  try {
    let data = await api(`/api/projects/${id}/illustrations${lite ? "?lite=1" : ""}`);
    if (seq !== illustSeq || current?.id !== id) return;
    const keys = (data.jobs || []).map(pictureFileKey);
    const needFull = rebuild || !picturesStructureMatches(data) || (lite && keys.length && !$("#picture-list")?.querySelector("article.pic pre"));
    if (needFull && lite) {
      data = await api(`/api/projects/${id}/illustrations`);
      if (seq !== illustSeq || current?.id !== id) return;
      rebuild = true;
    }
    applyIllustrationPayload(data);
    paintPictures(data, { rebuild });
    updatePicturesLive(data);
    updateScriptMetaMissing();
    syncIllustrationPolling();
  } catch {
    if (rebuild && current?.id === id && (current.lines || []).length) {
      renderPictures(current.lines);
    }
  }
}

async function loadVoices() {
  const provider = $("#voice-provider").value;
  updateVoiceHint();
  try {
    const data = await api(`/api/voices?provider=${provider}`);
    if (data.ready === false && data.error) {
      $("#voice-id").innerHTML = "";
      toast(data.error, true);
      return;
    }
    $("#voice-id").innerHTML = (data.voices || []).map((v) =>
      `<option value="${esc(v.id)}">${esc(v.name)}${v.style ? " — " + esc(v.style) : ""}</option>`
    ).join("");
    if (current?.voice_id) $("#voice-id").value = current.voice_id;
  } catch (err) {
    $("#voice-id").innerHTML = "";
    toast(err.message, true);
  }
}

function updateVoiceHint() {
  const hint = $("#voice-local-hint");
  if (!hint) return;
  const local = ($("#voice-provider")?.value || "") === "local";
  hint.hidden = !local;
}

function anyJobBusy() {
  return jobIndex.some((j) => j.running || j.busy);
}

function startPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(tickJob, 2000);
  tickJob();
  syncIllustrationPolling();
}

let lastTickState = {};
let tickInFlight = false;
let tickJobsCounter = 0;

async function tickJob() {
  if (tickInFlight) return;
  tickInFlight = true;
  try {
    // Full job-list refresh is expensive; only every few ticks (or when current finishes).
    tickJobsCounter += 1;
    const wantJobs = tickJobsCounter === 1 || tickJobsCounter % 3 === 0;
    if (wantJobs) await refreshJobs(current?.id);
    else if (current?.id) {
      // Still sync flags from cached index when we skip the network list call
      const match = jobIndex.find((j) => j.id === current.id);
      if (match) {
        current.running = match.running;
        current.busy = match.busy;
        current.paused = match.paused;
        current.has_video = match.has_video;
      }
    }
    if (current) {
      const job = await api(`/api/projects/${current.id}/job`);
      const running = !!job.running;
      const busy = !!job.busy;
      current.running = running;
      current.busy = busy;
      current.job = job;
      current.progress_pct = job.progress_pct;
      setBusy(running, job.error || job.detail || job.step || "");
      const prev = lastTickState[current.id] || {};
      const match = jobIndex.find((j) => j.id === current.id);
      const hasVideo = !!(match?.has_video || current.has_video);
      lastTickState[current.id] = {
        running,
        busy,
        step: job.step,
        progress_pct: job.progress_pct,
        has_video: hasVideo,
      };
      syncIllustrationPolling();

      // Video file appeared (e.g. render done, YouTube upload still running)
      if (hasVideo && !prev.has_video) {
        try {
          const fresh = await api(`/api/projects/${current.id}`);
          current = { ...current, ...fresh, running, busy, job };
          autoloadReadyVideo({ switchToVideo: true });
        } catch {
          current.has_video = true;
          autoloadReadyVideo({ switchToVideo: true });
        }
      }

      if (prev && (prev.running || prev.busy) && !running && !busy) {
        regenTargetKey = "";
        const id = current.id;
        await refreshJobs(id);
        await loadJob(id, { poll: false });
        if (current.has_video || readyAspects().length) {
          autoloadReadyVideo({ switchToVideo: true });
        }
      }
    }
    if (!anyJobBusy() && !(current?.running || current?.busy)) {
      clearInterval(pollTimer);
      pollTimer = null;
      tickJobsCounter = 0;
    }
  } catch (err) {
    const msg = String(err.message || "");
    if (current && /unknown project|not found/i.test(msg)) {
      stopIllustrationPolling();
      lastIllust = null;
      clearInterval(pollTimer);
      pollTimer = null;
      tickJobsCounter = 0;
      current = null;
      localStorage.removeItem(STORE_JOB);
      goLibrary();
      try { await refreshJobs(); } catch {}
      return;
    }
    if (current) setBusy(true, "Reconnecting to job…");
  } finally {
    tickInFlight = false;
  }
}

async function kick(path, body, step) {
  if (!requireMembership("generate or update videos")) return;
  if (!current) return toast("Create or pick a job first.", true);
  if (current.running) return toast("This job is already running.", true);
  try {
    const started = await api(path, { method: "POST", body });
    if (started?.use_mcp) {
      toast(started.message || `Use ${textProviderLabel()} Desktop MCP save_script.`);
      return;
    }
    current.running = true;
    current.job = started.job || started;
    if (step) setStep(step);
    setBusy(true, started.detail || started.job?.detail || "Working…");
    toast("Job running on the server. You can refresh.");
    startPolling();
    await refreshJobs(current.id);
  } catch (err) { toast(err.message, true); }
}

function goLibrary() {
  pauseWatch();
  setStep("library");
}

function jobById(id) {
  if (current?.id === id) return current;
  return jobIndex.find((j) => j.id === id) || { id };
}

function askDelete(id) {
  pendingDeleteId = id;
  pendingDeleteFiles = false;
  const dlg = $("#delete-dialog");
  if (!dlg) return;
  $("#delete-job-name").textContent = jobLabel(jobById(id));
  $("#delete-files").checked = false;
  dlg.showModal();
}

function releaseMedia(sel) {
  const el = $(sel);
  if (!el) return;
  try { el.pause(); } catch {}
  el.removeAttribute("src");
  if (typeof el.load === "function") el.load();
}

async function confirmDelete() {
  const id = pendingDeleteId;
  const deleteFiles = pendingDeleteFiles;
  pendingDeleteId = null;
  pendingDeleteFiles = false;
  if (!id) return;
  const wasCurrent = current?.id === id;
  try {
    if (wasCurrent) {
      pauseWatch();
      stopIllustrationPolling();
      lastIllust = null;
      clearInterval(pollTimer);
      pollTimer = null;
      releaseMedia("#watch-player");
      releaseMedia("#video-player");
      releaseMedia("#audio-player");
    }
    await api(`/api/projects/${encodeURIComponent(id)}?delete_files=${deleteFiles ? "true" : "false"}`, {
      method: "DELETE",
      body: { delete_files: deleteFiles },
    });
    if (wasCurrent) {
      current = null;
      localStorage.removeItem(STORE_JOB);
      syncJobTabs();
      goLibrary();
    }
    await refreshJobs();
    toast(deleteFiles ? "Job and files deleted." : "Job removed from Studio. Files kept on disk.");
  } catch (err) {
    toast(err.message, true);
  }
}

function handleDeleteClick(e) {
  const del = e.target.closest("[data-delete]");
  if (!del) return false;
  e.preventDefault();
  e.stopPropagation();
  askDelete(del.dataset.delete);
  return true;
}

stepButtons().forEach((btn) => btn.addEventListener("click", () => {
  const name = btn.dataset.step;
  if (JOB_STEPS.includes(name) && !current) {
    toast("Open a job from Library or Jobs first.", true);
    return;
  }
  setStep(name);
}));
$$(".tabs button").forEach((btn) => btn.addEventListener("click", () => {
  applyScriptTab(btn.dataset.tab);
}));

$("#brand-home")?.addEventListener("click", goLibrary);
$("#brand-home")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    goLibrary();
  }
});

$("#lib-filters")?.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-filter]");
  if (!btn) return;
  applyLibraryFilter(btn.dataset.filter);
  renderLibrary();
});

$("#lib-prev")?.addEventListener("click", () => {
  libraryPage = Math.max(0, libraryPage - 1);
  renderLibrary();
});
$("#lib-next")?.addEventListener("click", () => {
  libraryPage += 1;
  renderLibrary();
});

$("#view-library")?.addEventListener("click", async (e) => {
  if (handleDeleteClick(e)) return;
  if (e.target.closest("#lib-empty-create")) {
    setStep("create");
    return;
  }
  if (e.target.closest("#lib-empty-jobs")) {
    setStep("jobs");
    return;
  }
  const card = e.target.closest("[data-id]");
  if (!card || e.target.closest("#library-pager")) return;
  try { await openFromLibrary(card.dataset.id); }
  catch (err) { toast(err.message, true); }
});

$("#view-jobs")?.addEventListener("click", async (e) => {
  if (e.target.closest("#jobs-empty-create")) {
    setStep("create");
    return;
  }
  if (handleJobActionClick(e)) return;
  const open = e.target.closest("[data-open]");
  if (open) {
    try { await openFromLibrary(open.dataset.open); }
    catch (err) { toast(err.message, true); }
  }
});

$("#watch-more")?.addEventListener("click", async (e) => {
  if (handleResumeClick(e)) return;
  if (handleDeleteClick(e)) return;
  const card = e.target.closest("[data-id]");
  if (!card) return;
  try { await openFromLibrary(card.dataset.id); }
  catch (err) { toast(err.message, true); }
});

$("#watch-edit")?.addEventListener("click", () => {
  pauseWatch();
  setStep(current?.script_tagged ? "script" : "create");
});
$("#watch-back")?.addEventListener("click", goLibrary);
$("#watch-resume")?.addEventListener("click", (e) => {
  e.preventDefault();
  e.stopPropagation();
  if (current?.id) resumeJob(current.id);
});
$("#header-resume")?.addEventListener("click", () => {
  if (current?.id) resumeJob(current.id);
});
$("#video-resume")?.addEventListener("click", () => {
  if (current?.id) resumeJob(current.id);
});
$("#watch-delete")?.addEventListener("click", (e) => {
  e.preventDefault();
  e.stopPropagation();
  if (current?.id) askDelete(current.id);
});
$("#delete-form")?.addEventListener("submit", (e) => {
  pendingDeleteFiles = e.submitter?.value === "confirm" && !!$("#delete-files")?.checked;
});
$("#delete-dialog")?.addEventListener("close", () => {
  if ($("#delete-dialog").returnValue === "confirm") confirmDelete();
  else {
    pendingDeleteId = null;
    pendingDeleteFiles = false;
  }
});

$("#create-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!requireMembership("create jobs")) return;
  const fd = new FormData(e.target);
  try {
    const job = await api("/api/projects", {
      method: "POST",
      body: {
        topic: fd.get("topic"),
        duration_seconds: Number(fd.get("duration")),
        title: fd.get("title"),
        aspect: fd.get("aspect") || "16:9",
      },
    });
    await refreshJobs(job.id);
    await loadJob(job.id);
    setStep("script");
    toast("Job created. Generate the script next.");
  } catch (err) { toast(err.message, true); }
});

$("#render-aspect")?.addEventListener("change", async (e) => {
  if (!current) return;
  try {
    current = await api(`/api/projects/${current.id}`, { method: "PATCH", body: { aspect: e.target.value } });
    fillRenderAspectStatus();
    fillVideoPlayer(current.id);
    toast(current.aspect === "both"
      ? "Aspect set to Both. Render writes 16:9 then 9:16 as separate files."
      : `Aspect set to ${current.aspect}. Render writes only that ratio.`);
  } catch (err) { toast(err.message, true); }
});

$("#render-layout")?.addEventListener("change", async (e) => {
  if (!current) return;
  try {
    current = await api(`/api/projects/${current.id}`, { method: "PATCH", body: { video_layout: e.target.value } });
    updatePicturesHelp();
    await refreshIllustrations({ lite: false, rebuild: true });
    if ($("#view-pictures")?.classList.contains("on")) {
      $("#page-sub").textContent = picturesSubtitle();
    }
    toast(current.video_layout === "billboard"
      ? "This job will render as billboard (16:9 TV screen)."
      : "This job will render as full-bleed cover.");
  } catch (err) { toast(err.message, true); }
});

$("#render-art-style")?.addEventListener("change", async (e) => {
  if (!current) return;
  try {
    current = await api(`/api/projects/${current.id}`, { method: "PATCH", body: { art_style: e.target.value } });
    updatePicturesHelp();
    await refreshIllustrations({ lite: false, rebuild: true });
    const label = $("#render-art-style")?.selectedOptions?.[0]?.textContent || current.art_style || e.target.value;
    toast(`Art style set to ${label}. Regenerate covers/line art to apply.`);
  } catch (err) { toast(err.message, true); }
});

$("#render-character-size")?.addEventListener("change", async (e) => {
  if (!current) return;
  try {
    current = await api(`/api/projects/${current.id}`, { method: "PATCH", body: { character_size: e.target.value } });
    const label = current.character_size || e.target.value;
    toast(label === "medium"
      ? "This job will render the bubble-head at half size (feet stay at the bottom)."
      : label === "small"
        ? "This job will render the bubble-head at 1/3 size (feet stay at the bottom)."
        : "This job will render the bubble-head at current (large) size.");
  } catch (err) { toast(err.message, true); }
});

$("#include-bubblehead")?.addEventListener("change", async (e) => {
  if (!current) return;
  try {
    current = await api(`/api/projects/${current.id}`, {
      method: "PATCH",
      body: { include_bubblehead: !!e.target.checked },
    });
    toast(current.include_bubblehead !== false
      ? "Bubblehead on: stick-figure narrator will appear in the video."
      : "Bubblehead off: video keeps pictures and voice without the stick figure.");
  } catch (err) { toast(err.message, true); }
});

$("#gen-script").addEventListener("click", async () => {
  if (!current) return toast("Create or pick a job first.", true);
  const body = {
    extra: $("#script-extra").value,
    regenerate_pictures: $("#regen-pictures") ? $("#regen-pictures").checked : true,
    regenerate_audio: $("#regen-audio") ? $("#regen-audio").checked : true,
    generate_9x16: $("#generate-9x16") ? $("#generate-9x16").checked : false,
    provider: $("#voice-provider")?.value || lastSettings.tts_provider || "openai",
    voice_id: $("#voice-id")?.value || "",
  };
  if (isNativeTextProvider()) {
    try {
      const data = await api(`/api/projects/${current.id}/generate-script`, { method: "POST", body });
      toast(data.message || `Use ${textProviderLabel()} Desktop MCP save_script.`);
    } catch (err) { toast(err.message, true); }
    return;
  }
  if (needsOpenAiSpend()) {
    const ok = await confirmSpend("Generate script with the billed OpenAI API? This may also queue pictures/audio if those checkboxes are on.");
    if (!ok) return;
    body.confirm_spend = true;
  }
  kick(`/api/projects/${current.id}/generate-script`, body, "script");
});

$("#generate-9x16")?.addEventListener("change", async (e) => {
  if (!current) return;
  try {
    current = await api(`/api/projects/${current.id}`, {
      method: "PATCH",
      body: { generate_9x16: !!e.target.checked },
    });
    toast(current.generate_9x16 === true
      ? "Generate 9:16 on: hook short script, 1080×1920 art, and hook+CTA audio."
      : "Generate 9:16 off: skip short assets; 16:9 full explainer only.");
  } catch (err) { toast(err.message, true); }
});

$("#script-text-provider")?.addEventListener("change", async (e) => {
  try {
    await setTextProvider(e.target.value);
    const p = textProvider();
    toast(p === "openai"
      ? "Scripts and topics will use the billed OpenAI API."
      : p === "lmstudio"
        ? "Scripts and topics will use LM Studio's local API."
        : `${textProviderLabel()} Desktop MCP will write scripts and topics (save_script / create_topic).`);
  } catch (err) {
    toast(err.message, true);
    syncTextProviderUi();
  }
});

$("#save-script").addEventListener("click", async () => {
  if (!current) return;
  try {
    const ytMeta = readYoutubeMetaFromDom("script");
    current = await api(`/api/projects/${current.id}/script`, {
      method: "PUT",
      body: {
        script_tagged: $("#script-tagged").value,
        ...ytMeta,
      },
    });
    const warnings = current.script_warnings;
    await loadJob(current.id, { poll: false });
    if (warnings?.length) toast(`Saved with warning: ${warnings[0]}`, true);
    else toast("Saved. Raw version rebuilt from tags.");
  } catch (err) { toast(err.message, true); }
});

$("#picture-list").addEventListener("click", async (e) => {
  const useBtn = e.target.closest("[data-use-cover]");
  if (useBtn && current) {
    e.preventDefault();
    if (current.running || current.busy) return toast("This job is already running.", true);
    const versionId = useBtn.dataset.useCover;
    const aspect = useBtn.dataset.aspect || "16:9";
    try {
      await api(`/api/projects/${current.id}/covers/active`, {
        method: "POST",
        body: { aspect, version_id: versionId },
      });
      toast(`Active cover set to ${versionId} (${aspect}).`);
      await refreshIllustrations({ rebuild: false });
      refreshCoverHistoryGalleries({ force: true });
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  const btn = e.target.closest("[data-regen]");
  if (!btn || !current) return;
  e.preventDefault();
  if (current.running || current.busy) return toast("This job is already running.", true);
  const filename = btn.dataset.regen;
  const kind = btn.dataset.kind || "line";
  const indexRaw = btn.dataset.index;
  const article = btn.closest("article.pic");
  regenTargetKey = article?.dataset.file || pictureFileKey({ filename, role: kind === "cover" ? "cover" : "" });
  article?.classList.add("regenerating");
  btn.disabled = true;
  btn.textContent = "Regenerating…";
  try {
    const body = { filename, kind };
    if (indexRaw !== "" && Number.isFinite(Number(indexRaw))) body.index = Number(indexRaw);
    if (needsFluxSpend()) {
      const ok = await confirmSpend(
        `Regenerate ${filename} with Flux (${FLUX_MODEL_ID})? This spends fal credits.`,
        { flux: true, images: 1 }
      );
      if (!ok) {
        regenTargetKey = "";
        article?.classList.remove("regenerating");
        btn.disabled = false;
        btn.textContent = kind === "cover" ? "Regenerate cover" : "Regenerate";
        return;
      }
      body.confirm_spend = true;
    }
    const data = await api(`/api/projects/${current.id}/illustrations/regenerate`, { method: "POST", body });
    if (data.native || data.waiting || data.waiting_for === "chatgpt" || data.image_provider === "chatgpt") {
      regenTargetKey = "";
      await refreshIllustrations({ lite: false, rebuild: false });
      toast("Waiting for ChatGPT — generate natively, then MCP-save or upload this file.");
      return;
    }
    current.running = true;
    current.busy = true;
    current.job = data.job || data;
    setBusy(true, data.detail || data.job?.detail || `Regenerating ${filename}…`);
    toast(data.image_provider === "comfyui"
      ? "Regenerating this picture with ComfyUI."
      : "Regenerating this picture with Flux.");
    startPolling();
    await refreshJobs(current.id);
    syncIllustrationPolling();
  } catch (err) {
    regenTargetKey = "";
    article?.classList.remove("regenerating");
    btn.disabled = false;
    btn.textContent = kind === "cover" ? "Regenerate cover" : "Regenerate";
    toast(err.message, true);
  }
});

$("#picture-list").addEventListener("change", async (e) => {
  if (e.target.type !== "file" || !e.target.files[0] || !current) return;
  const fd = new FormData();
  fd.append("file", e.target.files[0]);
  fd.append("filename", e.target.dataset.filename);
  fd.append("kind", e.target.dataset.kind || "billboard");
  if (e.target.dataset.aspect) fd.append("aspect", e.target.dataset.aspect);
  try {
    await api(`/api/projects/${current.id}/illustrations`, { method: "POST", body: fd });
    await refreshIllustrations({ lite: false, rebuild: false });
    toast("Illustration saved.");
  } catch (err) { toast(err.message, true); }
});

$("#image-provider")?.addEventListener("change", async (e) => {
  if (!current) return;
  const next = e.target.value;
  const wasGenerating = illustrationWorkRunning();
  const label = next === "comfyui" ? "ComfyUI" : next === "chatgpt" ? "ChatGPT" : "Flux";
  try {
    if (wasGenerating) {
      setBusy(true, `Switching to ${label}… canceling current image`);
      toast(`Switching to ${label}… canceling current image`);
    }
    current = await api(`/api/projects/${current.id}`, {
      method: "PATCH",
      body: { image_provider: next },
    });
    updatePicturesHelp();
    if ($("#view-pictures")?.classList.contains("on")) {
      $("#page-sub").textContent = picturesSubtitle();
    }
    if (current.provider_switch?.switching) {
      toast(current.provider_switch.detail || `Switching to ${label}…`);
    } else if (!wasGenerating) {
      toast(current.image_provider === "flux"
        ? `This job will use Flux (${FLUX_MODEL_ID}).`
        : current.image_provider === "comfyui"
          ? "This job will use ComfyUI (uploaded API JSON)."
          : "This job will use ChatGPT’s native image tool.");
    }
  } catch (err) {
    toast(err.message, true);
    if ($("#image-provider") && current?.image_provider) {
      $("#image-provider").value = current.image_provider;
    }
  }
});

$("#gen-flux")?.addEventListener("click", async () => {
  if (!current) return toast("Create or pick a job first.", true);
  const body = {};
  if (needsFluxSpend()) {
    const missing = Number(current.missing_illustrations) || 0;
    const images = Math.max(1, missing);
    const ok = await confirmSpend(
      `Generate backgrounds with Flux (${FLUX_MODEL_ID})? This spends fal credits.`,
      { flux: true, images }
    );
    if (!ok) return;
    body.confirm_spend = true;
  }
  kick(`/api/projects/${current.id}/illustrations/flux`, body, "pictures");
});

$("#voice-provider").addEventListener("change", async () => {
  await loadVoices();
  if (!current) return;
  try {
    current = await api(`/api/projects/${current.id}`, {
      method: "PATCH",
      body: {
        tts_provider: $("#voice-provider").value,
        voice_provider: $("#voice-provider").value,
        voice_id: $("#voice-id").value || "",
      },
    });
    toast($("#voice-provider").value === "local"
      ? "This job will use local Resemble Chatterbox (no OpenAI TTS bill)."
      : $("#voice-provider").value === "elevenlabs"
        ? "This job will use ElevenLabs."
        : "This job will use OpenAI TTS.");
  } catch (err) { toast(err.message, true); }
});

$("#voice-id")?.addEventListener("change", async () => {
  if (!current) return;
  try {
    current = await api(`/api/projects/${current.id}`, {
      method: "PATCH",
      body: { voice_id: $("#voice-id").value || "" },
    });
  } catch (err) { toast(err.message, true); }
});

$("#gen-audio").addEventListener("click", async () => {
  const body = {
    provider: $("#voice-provider").value,
    voice_id: $("#voice-id").value,
  };
  if (needsOpenAiTtsSpend()) {
    const ok = await confirmSpend("Generate narration with OpenAI TTS? This spends OpenAI credits.");
    if (!ok) return;
    body.confirm_spend = true;
  }
  kick(`/api/projects/${current.id}/audio`, body, "voice");
});

let lastGentleUrl = "";
let gentleCopyResetTimer = null;
let gentleHealthTimer = null;
const GENTLE_HEALTH_POLL_MS = 10000;

function setGentleChrome(st) {
  const dot = $("#gentle-dot");
  const labelEl = $("#gentle-label");
  const copyBtn = $("#gentle-copy");
  const backend = st?.backend || "none";
  const label = gentleBackendLabel(backend);
  const ok = !!st?.ok;
  const url = (st?.url || "").trim();
  lastGentleUrl = url;

  if (dot) dot.dataset.state = ok ? "up" : "down";
  if (labelEl) {
    labelEl.textContent = ok ? `Gentle ${label}` : "Gentle down";
  }
  if (copyBtn) {
    copyBtn.hidden = !url;
    if (!copyBtn.dataset.bound) {
      copyBtn.dataset.bound = "1";
      copyBtn.addEventListener("click", async () => {
        if (!lastGentleUrl) return;
        try {
          await navigator.clipboard.writeText(lastGentleUrl);
          copyBtn.title = "Copied!";
          copyBtn.setAttribute("aria-label", "Copied!");
          clearTimeout(gentleCopyResetTimer);
          gentleCopyResetTimer = setTimeout(() => {
            copyBtn.title = "Copy Gentle URL";
            copyBtn.setAttribute("aria-label", "Copy Gentle URL");
          }, 1500);
        } catch {
          toast("Could not copy Gentle URL.", true);
        }
      });
    }
  }
}

function gentleBackendLabel(backend) {
  if (backend === "docker") return "Docker";
  if (backend === "local") return "Local";
  if (backend === "app") return "App";
  if (backend === "remote") return "Remote";
  return "None";
}

function applyGentleStatus(st) {
  if (!st) return;
  const backend = st.backend || "none";
  const label = gentleBackendLabel(backend);
  setGentleChrome(st);
  const backendPill = $("#gentle-backend-pill");
  const okPill = $("#gentle-ok-pill");
  const line = $("#gentle-status-line");
  const portLine = $("#gentle-port-line");
  const lead = $("#gentle-settings-lead");
  if (backendPill) {
    backendPill.textContent = `Backend: ${label}`;
    backendPill.className = `pill ${backend === "docker" ? "docker" : backend === "local" ? "local" : backend === "app" ? "local" : ""}`;
  }
  if (okPill) {
    okPill.textContent = st.ok ? "Status: up" : "Status: down";
    okPill.className = `pill ${st.ok ? "ok" : "bad"}`;
  }
  if (line) line.textContent = st.detail || "";
  if (portLine) {
    const docker = st.prefer_app
      ? (st.docker_available ? "Docker optional" : "Gentle app preferred (no Docker needed)")
      : (st.docker_available ? "Docker available" : "Docker not available");
    portLine.textContent = `Port: ${st.port ?? "—"} · ${docker}`;
  }
  if (lead && st.prefer_app) {
    lead.textContent =
      "On macOS, open the Gentle app (often http://127.0.0.1:8765) or set Gentle URL below. Docker is not required; Studio can also start a local fallback aligner.";
  }
  const urlInput = document.querySelector('#settings-form [name="gentle_url"]');
  if (urlInput && st.url) urlInput.value = st.url;
}

async function refreshGentleStatus() {
  try {
    applyGentleStatus(await api("/api/gentle"));
  } catch {
    applyGentleStatus({ ok: false, backend: "none", detail: "Studio API not reachable.", url: lastGentleUrl || "" });
  }
}

let lastMcpInfo = null;

function fillMcpSettings(data) {
  lastMcpInfo = data || null;
  if (!data) return;
  const buildPill = $("#mcp-build-pill");
  const httpPill = $("#mcp-http-pill");
  const toolsPill = $("#mcp-tools-pill");
  const pinPill = $("#mcp-pin-pill");
  const statusLine = $("#mcp-status-line");
  if (buildPill) {
    buildPill.textContent = `Build: ${data.mcp_build || "—"}`;
    buildPill.className = "pill ok";
  }
  if (httpPill) {
    httpPill.textContent = data.http_mounted ? "HTTP: mounted" : "HTTP: off";
    httpPill.className = `pill ${data.http_mounted ? "ok" : "bad"}`;
  }
  if (toolsPill) {
    toolsPill.textContent = `Tools: ${data.tool_count ?? (data.mcp_tools || []).length}`;
  }
  if (pinPill) {
    pinPill.textContent = data.mcp_pin_set ? "PIN: set" : "PIN: not set";
    pinPill.className = `pill ${data.mcp_pin_set ? "ok" : "bad"}`;
  }
  const pinStatus = $("#mcp-pin-status");
  if (pinStatus) {
    pinStatus.textContent = data.mcp_pin_set
      ? "PIN: set (hashed at rest — enter a new value to change)"
      : "PIN: not set — HTTP /mcp will refuse connections until you save a PIN";
  }
  const pinInput = $("#mcp-pin-input");
  if (pinInput) pinInput.value = "";
  if (statusLine) {
    statusLine.textContent = data.http_url
      ? `Live endpoint ${data.http_url}`
      : "MCP status loaded.";
  }
  const httpInput = $("#mcp-http-url");
  if (httpInput && data.http_url) httpInput.value = data.http_url;
  const publicInput = $("#mcp-public-url");
  if (publicInput) {
    const pub = data.public_mcp_url || data.public_url || (data.ngrok && data.ngrok.running ? data.ngrok.mcp_url : "");
    publicInput.value = pub || "";
    publicInput.placeholder = pub
      ? ""
      : "Set PUBLIC_BASE_URL or start ngrok to expose /mcp";
  }
  if (data.ngrok) applyNgrokStatus(data.ngrok);
  const stdioInput = $("#mcp-stdio-line");
  if (stdioInput && data.stdio_line) stdioInput.value = data.stdio_line;
  const chatgptHint = $("#mcp-chatgpt-hint");
  if (chatgptHint && data.chatgpt_hint) chatgptHint.textContent = data.chatgpt_hint;
  const claudeHint = $("#mcp-claude-hint");
  if (claudeHint && data.claude_hint) claudeHint.textContent = data.claude_hint;
  const authHint = $("#mcp-auth-hint");
  if (authHint && data.auth_hint) authHint.textContent = data.auth_hint;
  const desk = data.claude_desktop || {};
  const code = data.claude_code || {};
  const deskLine = $("#mcp-claude-desktop-line");
  if (deskLine) {
    deskLine.textContent = desk.configured
      ? `Claude Desktop: configured (${desk.key}) · ${desk.path}`
      : desk.exists
        ? `Claude Desktop: config exists but Stickman Automation not listed · ${desk.path}`
        : `Claude Desktop: no config yet · ${desk.path || "…"}`;
  }
  const codeLine = $("#mcp-claude-code-line");
  if (codeLine) {
    codeLine.textContent = code.configured
      ? `Claude Code: configured (${code.key}) · ${code.path}`
      : code.exists
        ? `Claude Code: config exists but Stickman Automation not listed · ${code.path}`
        : `Claude Code: no ~/.claude.json yet (Install only updates it if the file already exists)`;
  }
  const toolsList = $("#mcp-tools-list");
  if (toolsList) {
    const tools = data.mcp_tools || [];
    toolsList.textContent = tools.length ? tools.join("\n") : "(none)";
  }
  const snippet = $("#mcp-codex-snippet");
  if (snippet && data.codex_snippet) snippet.value = data.codex_snippet;
  const chrome = document.querySelector(".chrome-mcp");
  if (chrome && data.mcp_build) {
    chrome.innerHTML = `MCP <code>/mcp</code> · <span title="${esc(data.mcp_build)}">${esc(String(data.mcp_build).slice(0, 24))}</span>`;
  }
}

async function loadMcpSettings() {
  try {
    fillMcpSettings(await api("/api/mcp"));
  } catch (err) {
    const statusLine = $("#mcp-status-line");
    if (statusLine) statusLine.textContent = err.message || "Could not load MCP status.";
    const buildPill = $("#mcp-build-pill");
    if (buildPill) {
      buildPill.textContent = "Build: unavailable";
      buildPill.className = "pill bad";
    }
  }
}

async function copyText(text, label) {
  const value = (text || "").trim();
  if (!value) return toast(`Nothing to copy for ${label}.`, true);
  try {
    await navigator.clipboard.writeText(value);
    toast(`Copied ${label}.`);
  } catch {
    toast(`Could not copy ${label}.`, true);
  }
}

$("#mcp-refresh")?.addEventListener("click", () => loadMcpSettings());
$("#mcp-copy-http")?.addEventListener("click", () => {
  copyText($("#mcp-http-url")?.value || lastMcpInfo?.http_url, "HTTP URL");
});
$("#mcp-copy-public")?.addEventListener("click", () => {
  copyText(
    $("#mcp-public-url")?.value || lastMcpInfo?.public_mcp_url || lastMcpInfo?.public_url,
    "public MCP URL"
  );
});
$("#mcp-copy-stdio")?.addEventListener("click", () => {
  copyText($("#mcp-stdio-line")?.value || lastMcpInfo?.stdio_line, "stdio command");
});
$("#mcp-copy-codex")?.addEventListener("click", () => {
  copyText($("#mcp-codex-snippet")?.value || lastMcpInfo?.codex_snippet, "Codex snippet");
});
$("#mcp-install-claude")?.addEventListener("click", async () => {
  try {
    const data = await api("/api/mcp/install-claude", { method: "POST", body: {} });
    fillMcpSettings(data);
    toast("Claude MCP config written. Quit and reopen Claude Desktop.");
  } catch (err) { toast(err.message, true); }
});

function applyNgrokStatus(st) {
  const data = st || {};
  const okPill = $("#ngrok-ok-pill");
  const foundPill = $("#ngrok-found-pill");
  const statusLine = $("#ngrok-status-line");
  const commandLine = $("#ngrok-command-line");
  const commandCode = $("#ngrok-command-code");
  const errLine = $("#ngrok-error-line");
  const mcpUrl = $("#ngrok-mcp-url");
  const urlInput = $("#ngrok-url");
  const portInput = $("#ngrok-local-port");
  const autoInput = $("#ngrok-autostart");
  const userInput = $("#ngrok-basic-user");
  const passInput = $("#ngrok-basic-pass");
  const basicLine = $("#ngrok-basic-line");
  if (okPill) {
    okPill.textContent = data.running ? "Status: running" : "Status: stopped";
    okPill.className = `pill ${data.running ? "ok" : "bad"}`;
  }
  if (foundPill) {
    foundPill.textContent = data.ngrok_found ? "Binary: found" : "Binary: missing";
    foundPill.className = `pill ${data.ngrok_found ? "ok" : "bad"}`;
  }
  if (statusLine) statusLine.textContent = data.detail || (data.running ? "Tunnel running." : "ngrok stopped.");
  if (commandLine) commandLine.textContent = `Command: ${data.command || "—"}`;
  if (commandCode && data.command) commandCode.textContent = data.command;
  if (mcpUrl && data.mcp_url) mcpUrl.value = data.mcp_url;
  if (urlInput && data.public_url) urlInput.value = data.public_url;
  if (portInput && data.local_port != null) portInput.value = data.local_port;
  if (autoInput && typeof data.ngrok_autostart === "boolean") autoInput.checked = data.ngrok_autostart;
  const user = data.basic_auth_user || data.ngrok_basic_auth_user || "";
  if (userInput && user) userInput.value = user;
  const revealed = data.basic_auth_password_revealed && data.basic_auth_password && data.basic_auth_password !== "********";
  if (passInput && revealed) passInput.value = data.basic_auth_password;
  if (basicLine) {
    if (revealed) {
      basicLine.textContent = `Basic auth: ${user} / ${data.basic_auth_password}`;
    } else if (data.basic_auth_password_set || data.ngrok_basic_auth_password_set) {
      basicLine.textContent = `Basic auth: user=${user || "—"} · password set (shown when starting)`;
    } else {
      basicLine.textContent = "Basic auth: password will be generated on first start";
    }
  }
  if (errLine) {
    const err = (!data.ngrok_found && data.detail) || data.error || "";
    if (err && !data.running) {
      errLine.hidden = false;
      errLine.textContent = err;
    } else {
      errLine.hidden = true;
      errLine.textContent = "";
    }
  }
  const publicMcp = $("#mcp-public-url");
  if (publicMcp) {
    publicMcp.value = data.running ? (data.mcp_url || "") : (publicMcp.value || "");
    if (!data.running && !data.mcp_url) publicMcp.value = "";
  }
}

async function refreshNgrokStatus() {
  try {
    applyNgrokStatus(await api("/api/ngrok"));
  } catch (err) {
    applyNgrokStatus({
      ok: false,
      running: false,
      ngrok_found: false,
      detail: err.message || "Could not load ngrok status.",
      error: err.message,
    });
  }
}

async function startNgrok(extra = {}) {
  const url = ($("#ngrok-url")?.value || "").trim();
  const port = Number($("#ngrok-local-port")?.value || 7878);
  const autostart = !!$("#ngrok-autostart")?.checked;
  const basic_auth_user = ($("#ngrok-basic-user")?.value || "").trim();
  const basic_auth_password = ($("#ngrok-basic-pass")?.value || "").trim();
  try {
    const body = { url, local_port: port, autostart, ...extra };
    if (basic_auth_user) body.basic_auth_user = basic_auth_user;
    if (basic_auth_password && basic_auth_password !== "********") {
      body.basic_auth_password = basic_auth_password;
    }
    const st = await api("/api/ngrok/start", {
      method: "POST",
      body,
    });
    applyNgrokStatus(st);
    loadMcpSettings();
    if (st.running || st.ok) {
      const user = st.basic_auth_user || basic_auth_user || "bubblepod";
      const pass = st.basic_auth_password_revealed ? st.basic_auth_password : "";
      toast(
        pass
          ? (st.detail || "ngrok started.") + ` Basic auth: ${user} / ${pass}`
          : (st.detail || "ngrok started.")
      );
    } else toast(st.error || st.detail || "ngrok failed to start.", true);
  } catch (err) {
    toast(err.message || "ngrok failed to start.", true);
  }
}

async function stopNgrok() {
  try {
    const st = await api("/api/ngrok/stop", { method: "POST", body: {} });
    applyNgrokStatus(st);
    toast(st.detail || "ngrok stopped.");
  } catch (err) {
    toast(err.message, true);
  }
}
$("#ngrok-start")?.addEventListener("click", () => startNgrok());
$("#ngrok-stop")?.addEventListener("click", stopNgrok);
$("#ngrok-refresh")?.addEventListener("click", () => refreshNgrokStatus());
$("#ngrok-rotate-pass")?.addEventListener("click", () => startNgrok({ rotate_password: true }));
$("#ngrok-copy-url")?.addEventListener("click", () => {
  copyText($("#ngrok-url")?.value, "public URL");
});
$("#ngrok-copy-mcp")?.addEventListener("click", () => {
  copyText($("#ngrok-mcp-url")?.value, "public MCP URL");
});
$("#ngrok-copy-basic")?.addEventListener("click", () => {
  const user = ($("#ngrok-basic-user")?.value || "").trim();
  const pass = ($("#ngrok-basic-pass")?.value || "").trim();
  copyText(pass && pass !== "********" ? `${user}:${pass}` : "", "basic auth");
});
function syncNgrokCommandPreview() {
  const url = ($("#ngrok-url")?.value || "").trim();
  const port = Number($("#ngrok-local-port")?.value || $("#studio-port")?.value || 7878) || 7878;
  const cmd = url
    ? `ngrok http ${port} --url ${url} --traffic-policy-file <user_data>/ngrok_traffic_policy.json`
    : `ngrok http ${port} --url <your-ngrok-url> --traffic-policy-file <user_data>/ngrok_traffic_policy.json`;
  const code = $("#ngrok-command-code");
  const line = $("#ngrok-command-line");
  if (code) code.textContent = cmd;
  if (line) line.textContent = `Command: ${cmd}`;
  const mcp = $("#ngrok-mcp-url");
  if (mcp) mcp.value = url ? `${url.replace(/\/$/, "")}/mcp` : "";
}
$("#ngrok-url")?.addEventListener("input", syncNgrokCommandPreview);
$("#ngrok-local-port")?.addEventListener("input", syncNgrokCommandPreview);

async function pollGentleHealth() {
  try {
    const health = await api("/api/health");
    if (health?.gentle) applyGentleStatus(health.gentle);
    const chrome = document.querySelector(".chrome-mcp");
    if (chrome && health?.mcp_build) {
      chrome.innerHTML = `MCP <code>${esc(health.mcp || "/mcp")}</code> · ${esc(String(health.mcp_build).slice(0, 28))}`;
    }
  } catch {
    setGentleChrome({ ok: false, backend: "none", url: lastGentleUrl || "" });
    const labelEl = $("#gentle-label");
    if (labelEl) labelEl.textContent = "Gentle down";
  }
}

function startGentleHealthPolling() {
  if (gentleHealthTimer) return;
  gentleHealthTimer = setInterval(pollGentleHealth, GENTLE_HEALTH_POLL_MS);
}

async function startGentle() {
  try {
    const st = await api("/api/gentle/start", { method: "POST", body: {} });
    applyGentleStatus(st);
    toast(st.detail || "Gentle started.");
  } catch (err) { toast(err.message, true); }
}

async function stopGentle() {
  try {
    const st = await api("/api/gentle/stop", { method: "POST", body: {} });
    applyGentleStatus(st);
    toast(st.detail || "Gentle stopped.");
  } catch (err) { toast(err.message, true); }
}

$("#start-gentle")?.addEventListener("click", startGentle);
$("#start-gentle-settings")?.addEventListener("click", startGentle);
$("#stop-gentle-settings")?.addEventListener("click", stopGentle);

$("#align").addEventListener("click", () => {
  kick(`/api/projects/${current.id}/align`, {}, "video");
});

$("#render").addEventListener("click", () => {
  kick(`/api/projects/${current.id}/render`, {
    use_billboards: true,
    aspect: $("#render-aspect")?.value || current.aspect || "16:9",
    layout: $("#render-layout")?.value || current.video_layout || "cover",
    art_style: $("#render-art-style")?.value || current.art_style || "classic",
    character_size: $("#render-character-size")?.value || current.character_size || "large",
    include_bubblehead: $("#include-bubblehead") ? $("#include-bubblehead").checked : current.include_bubblehead !== false,
  }, "video");
});

$("#video-aspect-pick")?.addEventListener("change", (e) => {
  if (!current?.id) return;
  const asp = e.target.value;
  const player = $("#video-player");
  if (!player) return;
  player.classList.toggle("portrait", asp === "9:16");
  player.src = videoSrc(current.id, asp);
});

$("#watch-aspect-pick")?.addEventListener("change", (e) => {
  if (!current?.id) return;
  const asp = e.target.value;
  const player = $("#watch-player");
  if (!player) return;
  player.classList.toggle("portrait", asp === "9:16");
  player.src = videoSrc(current.id, asp);
  if ($("#watch-sub")) {
    $("#watch-sub").textContent = `${formatDuration(current.duration_seconds)} · ${asp} · Ready`;
  }
  player.play().catch(() => {});
});

$("#shuffle-music")?.addEventListener("click", async () => {
  if (!current) return toast("Create or pick a job first.", true);
  try {
    current = await api(`/api/projects/${current.id}/shuffle-music`, { method: "POST", body: {} });
    updateMusicPick();
    renderMusicList(libraryTracks);
    toast(current.music_name ? `Next render will use ${current.music_name}.` : "Library is empty — upload tracks above.");
  } catch (err) { toast(err.message, true); }
});

$("#music-mode-shuffle")?.addEventListener("click", async () => {
  if (!current) return toast("Create or pick a job first.", true);
  try {
    current = await api(`/api/projects/${current.id}/set-music`, {
      method: "POST",
      body: { mode: "shuffle" },
    });
    updateMusicPick();
    renderMusicList(libraryTracks);
    toast("Shuffle: a random track will be chosen at first render (or click Shuffle again).");
  } catch (err) { toast(err.message, true); }
});

$("#bg-picker")?.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-bg]");
  if (!btn || !current) return;
  const filename = btn.dataset.bg;
  if (!filename) return;
  try {
    current = await api(`/api/projects/${current.id}`, {
      method: "PATCH",
      body: { background_file: filename },
    });
    renderBackgroundPicker();
    toast(`Studio room: ${current.background_file}`);
  } catch (err) { toast(err.message, true); }
});

$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = Object.fromEntries(fd.entries());
  body.youtube_auto_upload = !!$("#yt-auto")?.checked;
  body.youtube_delete_file_after_upload = !!$("#yt-delete-after")?.checked;
  body.auto_scheduler = !!$("#auto-scheduler")?.checked;
  body.hands_off = !!$("#chrome-hands-off")?.checked;
  body.ngrok_autostart = !!$("#ngrok-autostart")?.checked;
  body.rate_limit_enabled = !!$("#rate-limit-enabled")?.checked;
  body.require_spend_confirm = !!$("#require-spend-confirm")?.checked;
  if (body.hands_off_interval_hours != null && body.hands_off_interval_hours !== "") {
    body.hands_off_interval_hours = Number(body.hands_off_interval_hours);
  }
  if (body.hands_off_min_queue != null && body.hands_off_min_queue !== "") {
    body.hands_off_min_queue = Number(body.hands_off_min_queue);
  }
  if (body.ngrok_local_port != null && body.ngrok_local_port !== "") {
    body.ngrok_local_port = Number(body.ngrok_local_port);
  }
  if (body.port != null && body.port !== "") {
    body.port = Number(body.port);
  }
  for (const key of [
    "rate_limit_login",
    "rate_limit_login_window_sec",
    "rate_limit_api",
    "rate_limit_api_window_sec",
    "max_openai_calls_per_day",
    "max_flux_images_per_day",
  ]) {
    if (body[key] != null && body[key] !== "") body[key] = Number(body[key]);
  }
  try {
    const prevPort = Number(lastSettings.port || 7878) || 7878;
    const saved = await api("/api/settings", { method: "PUT", body });
    fillSettings(saved);
    loadYoutube();
    const nextPort = Number(saved.port || 7878) || 7878;
    if (saved.tts_provider === "local" && saved.local_tts && !saved.local_tts.ready) {
      toast(saved.local_tts.error || "Local TTS is not installed.", true);
    } else if (nextPort !== prevPort) {
      toast(`Port saved (${nextPort}). Click Restart API to apply.`);
    } else {
      toast("Settings saved.");
    }
  } catch (err) { toast(err.message, true); }
});

function updateMusicVolumeOut(pct) {
  const out = $("#music-volume-out");
  if (out) out.textContent = `${Number(pct) || 0}%`;
}

let lastSettings = {};

function textProvider() {
  return lastSettings.text_provider || lastSettings.script_provider || "openai";
}

function textProviderLabel() {
  const p = textProvider();
  if (p === "chatgpt") return "ChatGPT";
  if (p === "claude") return "Claude";
  if (p === "lmstudio") return "LM Studio";
  return "OpenAI";
}

function isNativeTextProvider() {
  const p = textProvider();
  return p === "chatgpt" || p === "claude";
}

function generateButtonLabel() {
  const p = textProvider();
  if (p === "chatgpt") return "Generate with ChatGPT";
  if (p === "claude") return "Generate with Claude";
  if (p === "lmstudio") return "Generate with LM Studio";
  return "";
}

function syncTextProviderUi() {
  const p = textProvider();
  const label = textProviderLabel();
  const genLabel = generateButtonLabel();
  const btn = $("#gen-script");
  if (btn) {
    btn.textContent = genLabel || "Generate script";
  }
  const scriptSel = $("#script-text-provider");
  if (scriptSel && scriptSel.value !== p) scriptSel.value = p;
  const formSel = document.querySelector('#settings-form [name="text_provider"]');
  if (formSel && formSel.value !== p) formSel.value = p;
  const hint = $("#script-shape-hint");
  if (hint) {
    hint.textContent = p === "openai"
      ? "OpenAI writes the tagged script. Hook = 9:16 short; subscribe closes 16:9."
      : p === "lmstudio"
        ? "LM Studio writes the tagged script. Hook = 9:16 short; subscribe closes 16:9."
        : `${label} MCP writes the script (save_script). Generate returns the playbook — no OpenAI tokens.`;
  }
  const topicsBtn = $("#topics-generate");
  if (topicsBtn) {
    topicsBtn.textContent = genLabel || "Generate topics";
  }
  const topicsHint = $("#topics-hint");
  if (topicsHint) {
    topicsHint.textContent = p === "openai"
      ? "Uses the live topics.generate prompt (edit it on Prompts). Drafts save to user_data/topics.json. Set a date and time, then Schedule. Studio starts due queued topics about every 30 seconds, one at a time."
      : p === "lmstudio"
        ? "Uses the live topics.generate prompt via LM Studio (no OpenAI cloud). Set a date and time, then Schedule. Studio starts due queued topics about every 30 seconds, one at a time."
        : `${label} invents topics in Desktop MCP, then create_topic / schedule_topic with a date and time. This button returns the playbook and does not call the OpenAI API.`;
  }
}

async function setTextProvider(provider) {
  const saved = await api("/api/settings", { method: "PUT", body: { text_provider: provider } });
  fillSettings(saved);
  return saved;
}

function fillSettings(data) {
  lastSettings = data || {};
  const form = $("#settings-form");
  for (const [k, v] of Object.entries(data)) {
    if (form[k] && (typeof v === "string" || typeof v === "number")) form[k].value = v;
  }
  if (data.music_volume_pct != null) updateMusicVolumeOut(data.music_volume_pct);
  const createAspect = document.querySelector('#create-form [name="aspect"]');
  if (createAspect && data.default_aspect) createAspect.value = data.default_aspect;
  const ytAuto = $("#yt-auto");
  if (ytAuto) ytAuto.checked = !!data.youtube_auto_upload;
  const ytDeleteAfter = $("#yt-delete-after");
  if (ytDeleteAfter) ytDeleteAfter.checked = data.youtube_delete_file_after_upload !== false;
  const autoSched = $("#auto-scheduler");
  if (autoSched) autoSched.checked = data.auto_scheduler !== false;
  const handsOff = $("#chrome-hands-off");
  if (handsOff) handsOff.checked = !!data.hands_off;
  const topicsAuto = $("#topics-auto-scheduler");
  if (topicsAuto) topicsAuto.checked = data.auto_scheduler !== false;
  if ($("#yt-privacy") && data.youtube_privacy) $("#yt-privacy").value = data.youtube_privacy;
  if ($("#hands-off-interval") && data.hands_off_interval_hours != null) {
    $("#hands-off-interval").value = data.hands_off_interval_hours;
  }
  if ($("#hands-off-min-queue") && data.hands_off_min_queue != null) {
    $("#hands-off-min-queue").value = data.hands_off_min_queue;
  }
  const ngrokAuto = $("#ngrok-autostart");
  if (ngrokAuto) ngrokAuto.checked = !!data.ngrok_autostart;
  if (data.ngrok) applyNgrokStatus(data.ngrok);
  else syncNgrokCommandPreview();
  const pinStatus = $("#mcp-pin-status");
  if (pinStatus) {
    pinStatus.textContent = data.mcp_pin_set
      ? "PIN: set (hashed at rest — enter a new value to change)"
      : "PIN: not set — HTTP /mcp will refuse connections until you save a PIN";
  }
  const pinPill = $("#mcp-pin-pill");
  if (pinPill) {
    pinPill.textContent = data.mcp_pin_set ? "PIN: set" : "PIN: not set";
    pinPill.className = `pill ${data.mcp_pin_set ? "ok" : "bad"}`;
  }
  const pinInput = $("#mcp-pin-input");
  if (pinInput) pinInput.value = "";
  const rateEn = $("#rate-limit-enabled");
  if (rateEn) rateEn.checked = data.rate_limit_enabled !== false;
  const spendEn = $("#require-spend-confirm");
  if (spendEn) spendEn.checked = data.require_spend_confirm !== false;
  const spendLine = $("#spend-status-line");
  if (spendLine) {
    const sp = data.spend || {};
    const fluxUsd = sp.flux_usd_today_estimate != null
      ? formatUsd(sp.flux_usd_today_estimate)
      : formatUsd((sp.flux_images_today || 0) * FLUX_USD_PER_IMAGE);
    spendLine.textContent = `Spend today (UTC ${sp.day || "—"}): OpenAI ${sp.openai_calls_today ?? 0}`
      + (sp.max_openai_calls_per_day ? ` / ${sp.max_openai_calls_per_day}` : "")
      + `, Flux ${sp.flux_images_today ?? 0}`
      + (sp.max_flux_images_per_day ? ` / ${sp.max_flux_images_per_day}` : "")
      + ` ≈ ${fluxUsd}`
      + (sp.require_spend_confirm === false ? " · confirm off" : " · confirm on")
      + ` · ${sp.flux_model || FLUX_MODEL_ID} ~4.6¢/img`;
  }
  const accountLine = $("#account-user-line");
  if (accountLine) {
    const who = data.signed_in_as || data.auth_username || "";
    accountLine.textContent = who ? `Signed in as: ${who}` : "Signed in as: …";
  }
  const ytCb = $("#yt-oauth-callback-hint");
  if (ytCb && data.youtube_oauth_callback) {
    ytCb.textContent = data.youtube_oauth_callback;
  }
  const restartBtn = $("#restart-api");
  if (restartBtn) {
    const p = Number(data.port || 7878) || 7878;
    restartBtn.title = `Restart Studio API (port ${p}). Gentle and VoiceSync stay up.`;
  }
  applyLocalTtsStatus(data);
  syncTextProviderUi();
  fillComfyuiWorkflow(data);
  syncHandsOffWarning(data);
  fillStickmanHead(data);
  if ($("#view-settings")?.classList.contains("on") || $("#settings-audit-card")?.open) {
    loadAuditLog({ silent: true });
  }
}

function _normHex(value) {
  let raw = String(value || "").trim();
  if (!raw) return "";
  if (!raw.startsWith("#")) raw = `#${raw}`;
  if (!/^#[0-9a-fA-F]{6}$/.test(raw)) return "";
  return raw.toUpperCase();
}

/** Mirror server HSL shadow formula for live swatches. */
function stickmanDarkFromLight(hex) {
  const h = _normHex(hex);
  if (!h) return "#F8AF05";
  const r = parseInt(h.slice(1, 3), 16) / 255;
  const g = parseInt(h.slice(3, 5), 16) / 255;
  const b = parseInt(h.slice(5, 7), 16) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  let hue = 0;
  const l = (max + min) / 2;
  const d = max - min;
  let s = 0;
  if (d !== 0) {
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    switch (max) {
      case r: hue = ((g - b) / d + (g < b ? 6 : 0)) / 6; break;
      case g: hue = ((b - r) / d + 2) / 6; break;
      default: hue = ((r - g) / d + 4) / 6; break;
    }
  }
  let H = hue * 360 - 10.38;
  const S = s;
  let L = l * 100 - 8.43;
  H = ((H % 360) + 360) % 360;
  L = Math.max(0, Math.min(100, L)) / 100;
  const C = (1 - Math.abs(2 * L - 1)) * S;
  const X = C * (1 - Math.abs(((H / 60) % 2) - 1));
  const m = L - C / 2;
  let rp = 0;
  let gp = 0;
  let bp = 0;
  if (H < 60) [rp, gp, bp] = [C, X, 0];
  else if (H < 120) [rp, gp, bp] = [X, C, 0];
  else if (H < 180) [rp, gp, bp] = [0, C, X];
  else if (H < 240) [rp, gp, bp] = [0, X, C];
  else if (H < 300) [rp, gp, bp] = [X, 0, C];
  else [rp, gp, bp] = [C, 0, X];
  const toHex = (v) => Math.round((v + m) * 255).toString(16).padStart(2, "0");
  return `#${toHex(rp)}${toHex(gp)}${toHex(bp)}`.toUpperCase();
}

function refreshStickmanSwatches(hex) {
  const light = _normHex(hex) || "#FAE02E";
  const dark = stickmanDarkFromLight(light);
  const lightEl = $("#stickman-swatch-light");
  const darkEl = $("#stickman-swatch-dark");
  if (lightEl) lightEl.style.background = light;
  if (darkEl) darkEl.style.background = dark;
  return { light, dark };
}

function refreshStickmanPreview() {
  const img = $("#stickman-head-preview");
  if (!img) return;
  img.src = `/api/poses/preview?t=${Date.now()}`;
}

function fillStickmanHead(data) {
  const src = data?.stickman_head || data || {};
  const color = _normHex(src.color || data?.stickman_head_color) || "#FAE02E";
  const picker = $("#stickman-head-picker");
  const hex = $("#stickman-head-hex");
  if (picker) picker.value = color;
  if (hex) hex.value = color;
  refreshStickmanSwatches(color);
  refreshStickmanPreview();
  const status = $("#stickman-head-status");
  if (status && src.files != null) {
    status.textContent = `${src.files} pose files · shadow ${src.dark || stickmanDarkFromLight(color)}`;
  }
}

function syncStickmanInputs(fromPicker) {
  const picker = $("#stickman-head-picker");
  const hex = $("#stickman-head-hex");
  if (!picker || !hex) return;
  if (fromPicker) {
    hex.value = _normHex(picker.value) || picker.value.toUpperCase();
  } else {
    const n = _normHex(hex.value);
    if (n) {
      hex.value = n;
      picker.value = n;
    }
  }
  refreshStickmanSwatches(hex.value || picker.value);
}

$("#stickman-head-picker")?.addEventListener("input", () => syncStickmanInputs(true));
$("#stickman-head-hex")?.addEventListener("input", () => syncStickmanInputs(false));
$("#stickman-head-hex")?.addEventListener("change", () => syncStickmanInputs(false));

$("#stickman-head-apply")?.addEventListener("click", async () => {
  const hex = _normHex($("#stickman-head-hex")?.value || $("#stickman-head-picker")?.value);
  if (!hex) {
    toast("Enter a valid #RRGGBB color.", true);
    return;
  }
  const btn = $("#stickman-head-apply");
  const prev = btn?.textContent;
  try {
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Applying…";
    }
    const fromColor = _normHex(lastSettings?.stickman_head_color) || undefined;
    const result = await api("/api/poses/head-color", {
      method: "POST",
      body: { color: hex, from_color: fromColor },
    });
    fillSettings({ ...lastSettings, stickman_head_color: result.color, stickman_head_dark: result.dark, stickman_head: result.stickman_head });
    fillStickmanHead({ stickman_head_color: result.color, stickman_head: result.stickman_head });
    toast(`Updated ${result.files_changed || result.files || 0} poses → ${result.color}`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = prev || "Apply to all poses";
    }
  }
});

$("#stickman-head-reset")?.addEventListener("click", async () => {
  const btn = $("#stickman-head-reset");
  try {
    if (btn) btn.disabled = true;
    const result = await api("/api/poses/head-color/reset", { method: "POST", body: {} });
    fillSettings({
      ...lastSettings,
      stickman_head_color: result.color || "#FAE02E",
      stickman_head_dark: result.dark,
      stickman_head: result.stickman_head,
    });
    fillStickmanHead({ stickman_head_color: result.color || "#FAE02E", stickman_head: result.stickman_head });
    toast("Stickman head reset to stock yellow.");
  } catch (err) {
    toast(err.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
});

async function loadAuditLog(opts = {}) {
  const pre = $("#audit-log");
  if (!pre) return;
  try {
    const data = await api("/api/audit?limit=40");
    const rows = data.entries || [];
    if (!rows.length) {
      pre.textContent = `No entries yet. Path: ${data.path || "user_data/audit.log"}`;
      return;
    }
    pre.textContent = rows.map((row) => {
      const ts = row.ts || "";
      const action = row.action || "";
      const who = row.username || row.auth_method || "";
      const ok = row.success === false ? "ERR" : "ok";
      const err = row.error ? ` ${row.error}` : "";
      return `${ts}  ${ok}  ${action}${who ? `  (${who})` : ""}${err}`;
    }).join("\n");
  } catch (err) {
    if (!opts.silent) toast(err.message, true);
    pre.textContent = err.message || "Could not load audit log.";
  }
}

function renderCostBars(items, opts = {}) {
  const max = Math.max(...items.map((x) => Number(x.value) || 0), 0.01);
  const labelKey = opts.labelKey || "label";
  return `<div class="cost-bars">${items.map((item) => {
    const v = Number(item.value) || 0;
    const pct = Math.max(4, Math.round((v / max) * 100));
    const label = item[labelKey] || item.label || "";
    const sub = item.sub || formatUsd(v);
    return `<div class="cost-bar-row" title="${esc(label)} — ${esc(sub)}">
      <span class="cost-bar-label">${esc(label)}</span>
      <span class="cost-bar-track"><span class="cost-bar-fill" style="width:${pct}%"></span></span>
      <span class="cost-bar-value">${esc(sub)}</span>
    </div>`;
  }).join("")}</div>`;
}

function filteredCosts() {
  const q = costsSearch.trim().toLowerCase();
  if (!q) return costsRows;
  return costsRows.filter((p) => {
    const hay = `${p.title || ""} ${p.id || ""} ${p.basis || ""}`.toLowerCase();
    return hay.includes(q);
  });
}

function renderCostsTable() {
  const tableEl = $("#costs-table");
  if (!tableEl) return;
  if (!costsRows.length) {
    tableEl.innerHTML = `<p class="meta">No projects yet.</p>`;
    updatePager("#costs-pager", "#costs-page-label", "#costs-prev", "#costs-next", 0, 1, 0);
    return;
  }
  const filtered = filteredCosts();
  if (!filtered.length) {
    tableEl.innerHTML = `<p class="meta">No videos match this search.</p>`;
    updatePager("#costs-pager", "#costs-page-label", "#costs-prev", "#costs-next", 0, 1, 0);
    return;
  }
  const pageData = paginate(filtered, costsPage);
  costsPage = pageData.page;
  updatePager("#costs-pager", "#costs-page-label", "#costs-prev", "#costs-next", pageData.page, pageData.pages, pageData.total);
  tableEl.innerHTML = `<table class="costs-data">
    <thead><tr><th>Video</th><th>Images</th><th>Est. Flux $</th><th>Basis</th></tr></thead>
    <tbody>${pageData.items.map((p) => `<tr>
      <td>${esc(p.title || p.id)}</td>
      <td>${esc(p.image_count ?? 0)}</td>
      <td>${esc(formatUsd(p.estimate_usd))}</td>
      <td class="meta">${esc(p.basis || "estimate")}</td>
    </tr>`).join("")}</tbody>
  </table>`;
}

async function loadCosts() {
  const todayEl = $("#costs-today-line");
  const chartEl = $("#costs-chart");
  const dailyEl = $("#costs-daily-chart");
  const tableEl = $("#costs-table");
  const ratesEl = $("#costs-rates-hint");
  if (todayEl) todayEl.textContent = "Loading spend…";
  try {
    const data = await api("/api/costs?limit=100");
    if (ratesEl && data.estimate_note) {
      ratesEl.innerHTML = `Flux Dev 2 (<code>${esc(data.flux_model || FLUX_MODEL_ID)}</code>) ≈ <strong>~4.6¢</strong> per ~2MP image. A ~10-minute video needs about <strong>~56 images</strong> ≈ <strong>~$2.60</strong>. At 6 videos/day, ballpark <strong>~$15–16/day</strong>.`;
    }
    const today = data.today || {};
    if (todayEl) {
      todayEl.textContent = `Today (UTC ${today.day || "—"}): Flux ${today.flux_images ?? 0} images ≈ ${formatUsd(today.flux_usd)} · OpenAI calls ${today.openai_calls ?? 0} (tracked). Estimates — not a fal invoice.`;
    }
    const chart = (data.chart || []).map((p) => ({
      label: (p.title || p.id || "").slice(0, 28),
      value: Number(p.estimate_usd) || 0,
      sub: `${formatUsd(p.estimate_usd)} · ${p.estimate_images || p.image_count || 0} img`,
    }));
    if (chartEl) {
      chartEl.innerHTML = chart.length
        ? renderCostBars(chart)
        : `<p class="meta">No projects with images yet. After Flux generates PNGs, bars appear here.</p>`;
    }
    const daily = (data.daily || []).map((d) => ({
      label: (d.day || "").slice(5),
      value: Number(d.estimate_usd) || 0,
      sub: formatUsd(d.estimate_usd),
    }));
    if (dailyEl) {
      if (daily.length) {
        dailyEl.hidden = false;
        dailyEl.innerHTML = `<h4 class="costs-subhead">Daily Flux estimate</h4>${renderCostBars(daily)}`;
      } else {
        dailyEl.hidden = true;
        dailyEl.innerHTML = "";
      }
    }
    costsRows = data.projects || [];
    renderCostsTable();
  } catch (err) {
    if (todayEl) todayEl.textContent = err.message || "Could not load costs.";
    if (chartEl) chartEl.innerHTML = "";
    if (tableEl) tableEl.innerHTML = "";
    costsRows = [];
    updatePager("#costs-pager", "#costs-page-label", "#costs-prev", "#costs-next", 0, 1, 0);
  }
}

$("#audit-refresh")?.addEventListener("click", () => loadAuditLog());
$("#settings-audit-card")?.addEventListener("toggle", (e) => {
  if (e.target.open) loadAuditLog({ silent: true });
});

function fillComfyuiWorkflow(data) {
  const line = $("#comfyui-workflow-status");
  if (!line) return;
  const wf = (data && data.comfyui_workflow) || {};
  const loaded = !!(data && (data.comfyui_workflow_loaded || wf.loaded));
  const name = (data && data.comfyui_workflow_filename) || wf.filename || "";
  const del = $("#comfyui-workflow-delete");
  if (del) del.hidden = !loaded;
  if (!loaded) {
    line.textContent = "No ComfyUI API workflow uploaded. Pictures generate will fail until you upload one.";
    return;
  }
  const node = wf.prompt_node || {};
  const nodeBit = node.found
    ? ` Prompt node: ${node.class_type || "CLIPTextEncode"} #${node.node_id} (${(node.fields || ["text"]).join(", ")}).`
    : "";
  line.textContent = `Loaded: ${name}.${nodeBit}`;
}

$("#comfyui-workflow-file")?.addEventListener("change", async (e) => {
  const input = e.target;
  const file = input?.files?.[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  try {
    toast("Uploading ComfyUI workflow…");
    const saved = await api("/api/settings/comfyui-workflow", { method: "POST", body: fd });
    fillSettings({ ...lastSettings, ...saved, comfyui_workflow: saved, comfyui_workflow_loaded: !!saved.loaded });
    toast(saved.loaded ? `Workflow saved: ${saved.filename || file.name}` : "Workflow uploaded.");
  } catch (err) {
    toast(err.message, true);
  } finally {
    input.value = "";
  }
});

$("#comfyui-workflow-delete")?.addEventListener("click", async () => {
  if (!window.confirm("Remove the uploaded ComfyUI API workflow?")) return;
  try {
    const saved = await api("/api/settings/comfyui-workflow", { method: "DELETE" });
    fillSettings({ ...lastSettings, ...saved, comfyui_workflow: saved, comfyui_workflow_loaded: !!saved.loaded });
    toast("ComfyUI workflow removed.");
  } catch (err) {
    toast(err.message, true);
  }
});

$("#comfyui-test")?.addEventListener("click", async () => {
  try {
    const st = await api("/api/comfyui/test", { method: "POST", body: {} });
    const line = $("#comfyui-workflow-status");
    if (st.reachable) {
      toast(st.workflow_loaded ? `ComfyUI OK at ${st.url}` : `ComfyUI reachable at ${st.url}, but no workflow uploaded yet.`);
      if (line && st.workflow_loaded) fillComfyuiWorkflow({ comfyui_workflow: st, comfyui_workflow_loaded: true, comfyui_workflow_filename: st.workflow_filename });
    } else {
      toast(st.error || `ComfyUI not reachable at ${st.url || "…"}`, true);
    }
  } catch (err) {
    toast(err.message, true);
  }
});

function applyLocalTtsStatus(data) {
  const line = $("#local-tts-status");
  if (!line) return;
  const st = data.local_tts || {};
  const provider = data.tts_provider || data.voice_provider || "";
  if (!st.ready) {
    line.hidden = false;
    line.classList.add("bad");
    line.textContent = st.error || "Local TTS is not installed. pip install chatterbox-tts then restart Studio.";
    return;
  }
  line.classList.remove("bad");
  if (provider === "local") {
    line.hidden = false;
    const engine = st.engine === "piper" ? "Piper (Chatterbox was not available)" : "Resemble Chatterbox";
    line.textContent = `Local TTS ready: ${engine}. ${st.warning || "No OpenAI TTS charges."}`;
  } else {
    line.hidden = false;
    line.textContent = `Local TTS available (${st.label || st.engine || "Chatterbox"}). Pick Local above to skip OpenAI TTS billing.`;
  }
}

let ytPollTimer = null;
let ytConnected = false;

function youtubePrivacyForUpload() {
  return (
    $("#job-yt-privacy")?.value
    || current?.youtube_privacy
    || lastSettings?.youtube_privacy
    || "unlisted"
  );
}

function youtubeKeywordsText(data) {
  if (!data) return "";
  if (data.youtube_keywords_text) return data.youtube_keywords_text;
  const kw = data.youtube_keywords;
  if (Array.isArray(kw)) return kw.join(", ");
  return (kw || "").toString();
}

function youtubeHashtagsText(data) {
  if (!data) return "";
  if (data.youtube_hashtags_text) return data.youtube_hashtags_text;
  const ht = data.youtube_hashtags;
  if (Array.isArray(ht)) return ht.join(" ");
  return (ht || "").toString();
}

function fillYoutubeMetaFields(data) {
  const desc = data?.youtube_description || "";
  const keywords = youtubeKeywordsText(data);
  const hashtags = youtubeHashtagsText(data);
  const pairs = [
    ["#script-yt-description", desc],
    ["#script-yt-keywords", keywords],
    ["#script-yt-hashtags", hashtags],
    ["#job-yt-description", desc],
    ["#job-yt-keywords", keywords],
    ["#job-yt-hashtags", hashtags],
  ];
  pairs.forEach(([sel, value]) => {
    const el = $(sel);
    if (el && document.activeElement !== el) el.value = value;
  });
}

function readYoutubeMetaFromDom(prefer = "job") {
  const scriptDesc = $("#script-yt-description")?.value ?? "";
  const scriptKw = $("#script-yt-keywords")?.value ?? "";
  const scriptHt = $("#script-yt-hashtags")?.value ?? "";
  const jobDesc = $("#job-yt-description")?.value ?? "";
  const jobKw = $("#job-yt-keywords")?.value ?? "";
  const jobHt = $("#job-yt-hashtags")?.value ?? "";
  if (prefer === "script") {
    return {
      youtube_description: scriptDesc,
      youtube_keywords: scriptKw,
      youtube_hashtags: scriptHt,
    };
  }
  return {
    youtube_description: jobDesc || scriptDesc,
    youtube_keywords: jobKw || scriptKw,
    youtube_hashtags: jobHt || scriptHt,
  };
}

function syncYoutubeMetaDom(source) {
  const meta = readYoutubeMetaFromDom(source);
  const map = {
    script: {
      "#script-yt-description": meta.youtube_description,
      "#script-yt-keywords": meta.youtube_keywords,
      "#script-yt-hashtags": meta.youtube_hashtags,
    },
    job: {
      "#job-yt-description": meta.youtube_description,
      "#job-yt-keywords": meta.youtube_keywords,
      "#job-yt-hashtags": meta.youtube_hashtags,
    },
  };
  const other = source === "script" ? "job" : "script";
  Object.entries(map[other]).forEach(([sel, value]) => {
    const el = $(sel);
    if (el && document.activeElement !== el) el.value = value;
  });
  if (current) {
    current.youtube_description = meta.youtube_description;
    current.youtube_keywords = meta.youtube_keywords;
    current.youtube_hashtags = meta.youtube_hashtags;
    current.youtube_keywords_text = meta.youtube_keywords;
    current.youtube_hashtags_text = meta.youtube_hashtags;
  }
}

let ytMetaSaveTimer = null;
async function persistYoutubeMeta(source = "job") {
  if (!current?.id) return;
  syncYoutubeMetaDom(source);
  const meta = readYoutubeMetaFromDom(source);
  try {
    current = await api(`/api/projects/${current.id}`, {
      method: "PATCH",
      body: meta,
    });
    fillYoutubeMetaFields(current);
  } catch (err) {
    toast(err.message, true);
  }
}

function scheduleYoutubeMetaSave(source) {
  syncYoutubeMetaDom(source);
  clearTimeout(ytMetaSaveTimer);
  ytMetaSaveTimer = setTimeout(() => {
    persistYoutubeMeta(source);
  }, 600);
}

function uploadAspectForSource(source) {
  if (source === "watch") {
    return $("#watch-aspect-pick")?.value || preferredPlayAspect();
  }
  if (source === "video") {
    return $("#video-aspect-pick")?.value || preferredPlayAspect();
  }
  return preferredPlayAspect();
}

function syncYoutubeUploadButtons() {
  const hasVideo = !!current?.has_video || readyAspects().length > 0;
  const connected = !!ytConnected;
  const tip = connected
    ? "Upload the finished video to YouTube"
    : "Connect YouTube in Settings first";
  const watchBtn = $("#watch-yt-upload");
  if (watchBtn) {
    watchBtn.hidden = !hasVideo;
    watchBtn.disabled = !connected || !hasVideo;
    watchBtn.title = tip;
  }
  const jobBtn = $("#job-yt-upload");
  if (jobBtn) {
    jobBtn.disabled = !connected || !hasVideo;
    jobBtn.title = tip;
  }
}

function fillWatchYoutube() {
  const line = $("#watch-yt-line");
  if (!line) return;
  const ytUrl = youtubeWatchUrl(current);
  if (!hasLocalVideo(current) && !ytUrl) {
    line.hidden = true;
    line.textContent = "";
    return;
  }
  line.hidden = false;
  const uploadErr = current?.youtube_error || current?.job?.youtube_error;
  if (ytUrl) {
    line.classList.remove("bad");
    const privacy = current.youtube?.privacy || current.youtube_privacy || "unlisted";
    line.innerHTML = hasLocalVideo(current)
      ? `Uploaded as ${esc(privacy)}: <a href="${esc(ytUrl)}" target="_blank" rel="noopener">${esc(ytUrl)}</a>`
      : `Local mp4 missing — watching from YouTube (<a href="${esc(ytUrl)}" target="_blank" rel="noopener">${esc(ytUrl)}</a>). Resume the job to re-render locally.`;
  } else if (uploadErr) {
    line.classList.add("bad");
    line.textContent = `YouTube: ${uploadErr}`;
  } else if (!ytConnected) {
    line.classList.remove("bad");
    line.textContent = "Connect YouTube in Settings to send this video.";
  } else if (current?.youtube_pending) {
    line.classList.remove("bad");
    line.textContent = `YouTube upload pending as ${current.youtube_privacy || "private"}.`;
  } else {
    line.classList.remove("bad");
    line.textContent = `Ready to send as ${youtubePrivacyForUpload()}.`;
  }
}

function fillJobYoutube() {
  const auto = $("#job-yt-auto");
  const priv = $("#job-yt-privacy");
  const line = $("#job-yt-line");
  if (!current) return;
  if (auto) auto.checked = !!current.youtube_auto_upload;
  if (priv && current.youtube_privacy) priv.value = current.youtube_privacy;
  fillYoutubeMetaFields(current);
  syncYoutubeUploadButtons();
  fillWatchYoutube();
  if (!line) return;
  const uploadErr = current.youtube_error || current.job?.youtube_error;
  if (current.youtube?.url) {
    line.textContent = `Uploaded as ${current.youtube.privacy || "unlisted"}: ${current.youtube.url}`;
  } else if (uploadErr) {
    line.textContent = `YouTube: ${uploadErr}`;
  } else if (current.youtube_pending) {
    line.textContent = `YouTube upload pending as ${current.youtube_privacy || "private"}. Connect YouTube in Settings, then upload.`;
  } else if (current.youtube_auto_upload) {
    line.textContent = `Auto-upload on (${current.youtube_privacy || "unlisted"}). Connect YouTube in Settings if needed.`;
  } else {
    line.textContent = "Auto-upload off. Enable the checkbox or send after render.";
  }
}

function renderYoutube(data) {
  ytConnected = !!data?.connected;
  syncYoutubeUploadButtons();
  fillWatchYoutube();
  const status = $("#yt-status");
  if (status) {
    if (data.connected) {
      const ch = data.channel_title || data.channel_id || "";
      status.textContent = data.error
        ? `Connected, but listing channels failed: ${data.error}`
        : (ch ? `Connected · ${ch}` : "Connected. Pick a channel.");
    } else if (data.pending) {
      status.textContent = "Waiting for Google sign-in in your system browser…";
    } else {
      status.textContent = data.has_client
        ? "Not connected. Click Connect YouTube (opens your browser)."
        : "Add a Google Cloud client ID and secret, save settings, then Connect.";
    }
  }
  const sel = $("#yt-channel");
  if (sel) {
    const channels = data.channels || [];
    const currentId = data.channel_id || "";
    if (!channels.length) {
      sel.innerHTML = `<option value="">${data.connected ? "No channels on this account" : "Connect first, then pick a channel"}</option>`;
    } else {
      sel.innerHTML = channels.map((c) => (
        `<option value="${esc(c.id)}"${c.id === currentId ? " selected" : ""}>${esc(c.title || c.id)}</option>`
      )).join("");
      if (currentId && !channels.some((c) => c.id === currentId)) {
        sel.insertAdjacentHTML("afterbegin", `<option value="${esc(currentId)}" selected>${esc(data.channel_title || currentId)}</option>`);
      }
    }
  }
  const auto = $("#yt-auto");
  if (auto && data.auto_upload != null) auto.checked = !!data.auto_upload;
  if ($("#yt-privacy") && data.privacy) $("#yt-privacy").value = data.privacy;
}

async function loadYoutube() {
  try {
    renderYoutube(await api("/api/youtube"));
  } catch (err) {
    const el = $("#yt-status");
    if (el) el.textContent = err.message;
  }
}

function startYtPoll() {
  if (ytPollTimer) clearInterval(ytPollTimer);
  let n = 0;
  ytPollTimer = setInterval(async () => {
    n += 1;
    try {
      const data = await api("/api/youtube");
      renderYoutube(data);
      if (data.connected || n > 90) {
        clearInterval(ytPollTimer);
        ytPollTimer = null;
        if (data.connected) toast("YouTube connected.");
      }
    } catch {
      if (n > 90) {
        clearInterval(ytPollTimer);
        ytPollTimer = null;
      }
    }
  }, 2000);
}

function updateMusicVolumeOut(pct) {
  const out = $("#music-volume-out");
  if (out) out.textContent = `${Number(pct) || 0}%`;
  const slider = $("#music-volume");
  if (slider && String(slider.value) !== String(pct)) slider.value = Number(pct) || 0;
}

async function loadSettingsForMusic() {
  try {
    const data = await api("/api/settings");
    if (data.music_volume_pct != null) updateMusicVolumeOut(data.music_volume_pct);
    lastSettings = { ...lastSettings, ...data };
  } catch {
    /* ignore */
  }
}

function updateMusicPick() {
  const el = $("#music-pick");
  const shuffleBtn = $("#music-mode-shuffle");
  const mode = current?.music_mode || (current?.music_id ? "fixed" : "shuffle");
  if (shuffleBtn) shuffleBtn.classList.toggle("on", mode === "shuffle" && !current?.music_id);
  if (!el) return;
  if (!current) {
    el.textContent = "Background music: picked at first render from the library.";
    return;
  }
  if (mode === "shuffle" && !current.music_id) {
    el.textContent = "Shuffle: a random library track is chosen at first render.";
    return;
  }
  if (current.music_name) {
    el.textContent = current.music_missing
      ? `${current.music_name} is missing and will be re-picked on render.`
      : mode === "fixed"
        ? `Locked: ${current.music_name}`
        : `Shuffled: ${current.music_name} (kept on rerender unless you shuffle again).`;
    return;
  }
  el.textContent = "Shuffle: a random library track is chosen at first render. Upload tracks above.";
}

let libraryTracks = [];

async function loadBackgrounds() {
  try {
    const data = await api("/api/backgrounds");
    bgCatalog = data.backgrounds || [];
    renderBackgroundPicker();
  } catch (err) {
    const root = $("#bg-picker");
    if (root) root.innerHTML = `<p class="hint">${esc(err.message)}</p>`;
  }
}

function renderBackgroundPicker() {
  const root = $("#bg-picker");
  if (!root) return;
  if (!bgCatalog.length) {
    root.innerHTML = `<p class="hint">No studio room images yet. Drop PNG/JPG files into <code>backgrounds/</code> or <code>user_data/backgrounds/</code>.</p>`;
    return;
  }
  const selected = current?.background_file || "";
  root.innerHTML = bgCatalog.map((b) => `
    <button type="button" class="bg-thumb${b.filename === selected ? " on" : ""}" data-bg="${esc(b.filename)}" title="${esc(b.filename)}">
      <img src="/api/backgrounds/${encodeURIComponent(b.filename)}" alt="${esc(b.name)}" />
      <span>${esc(b.filename)}</span>
    </button>
  `).join("");
}

function renderMusicList(tracks) {
  libraryTracks = tracks || [];
  const root = $("#music-list");
  if (!root) return;
  if (!libraryTracks.length) {
    root.innerHTML = `<p class="hint">No tracks yet. Upload one or more files above — an empty library skips music with no error.</p>`;
    updateMusicPick();
    return;
  }
  const selected = current?.music_id || "";
  const mode = current?.music_mode || (selected ? "fixed" : "shuffle");
  root.innerHTML = libraryTracks.map((t) => {
    const on = t.id === selected ? " on" : "";
    const tag = t.id === selected
      ? (mode === "fixed" ? " · locked" : " · shuffled")
      : "";
    return `
    <div class="music-track music-pick-item${on}" data-id="${esc(t.id)}">
      <button type="button" class="music-pick-btn" data-music-pick="${esc(t.id)}" title="Use for this job">
        <strong>${esc(t.name)}${tag}</strong>
      </button>
      <audio controls preload="none" src="/api/music/${esc(t.id)}"></audio>
      <button type="button" class="danger" data-music-delete="${esc(t.id)}">Delete</button>
    </div>`;
  }).join("");
  updateMusicPick();
}

async function loadMusicList() {
  try {
    const data = await api("/api/music");
    if (data.music_volume_pct != null) updateMusicVolumeOut(data.music_volume_pct);
    renderMusicList(data.tracks || []);
  } catch (err) {
    toast(err.message, true);
  }
}

$("#music-upload")?.addEventListener("change", async (e) => {
  const files = [...(e.target.files || [])];
  e.target.value = "";
  if (!files.length) return;
  try {
    for (const file of files) {
      const fd = new FormData();
      fd.append("file", file);
      await api("/api/music", { method: "POST", body: fd });
    }
    await loadMusicList();
    toast(files.length === 1 ? "Track uploaded." : `${files.length} tracks uploaded.`);
  } catch (err) {
    toast(err.message, true);
    try { await loadMusicList(); } catch {}
  }
});

$("#music-list")?.addEventListener("click", async (e) => {
  const del = e.target.closest("[data-music-delete]");
  if (del) {
    try {
      const data = await api(`/api/music/${encodeURIComponent(del.dataset.musicDelete)}`, { method: "DELETE" });
      renderMusicList(data.tracks || []);
      toast("Track deleted.");
    } catch (err) { toast(err.message, true); }
    return;
  }
  const pick = e.target.closest("[data-music-pick]");
  if (!pick) return;
  if (!current) return toast("Create or pick a job first.", true);
  const trackId = pick.dataset.musicPick;
  if (!trackId) return;
  try {
    current = await api(`/api/projects/${current.id}/set-music`, {
      method: "POST",
      body: { music_id: trackId },
    });
    updateMusicPick();
    renderMusicList(libraryTracks);
    toast(current.music_name ? `Locked: ${current.music_name}` : "Track selected.");
  } catch (err) { toast(err.message, true); }
});

$("#music-volume")?.addEventListener("input", (e) => {
  updateMusicVolumeOut(e.target.value);
});

$("#music-volume")?.addEventListener("change", async (e) => {
  const pct = Number(e.target.value) || 0;
  updateMusicVolumeOut(pct);
  try {
    const saved = await api("/api/settings", { method: "PUT", body: { music_volume_pct: pct } });
    lastSettings = { ...lastSettings, ...saved };
    toast(`Loudness saved at ${pct}%.`);
  } catch (err) { toast(err.message, true); }
});

document.querySelector('[name="music_volume_pct"]')?.addEventListener("input", (e) => {
  updateMusicVolumeOut(e.target.value);
});

$("#yt-connect")?.addEventListener("click", async () => {
  try {
    const data = await api("/api/youtube/connect", { method: "POST", body: {} });
    const hint = $("#yt-auth-url");
    if (hint && data.auth_url) {
      hint.hidden = false;
      hint.textContent = data.browser_opened
        ? `Browser opened. If nothing happened, copy this URL: ${data.auth_url}`
        : `Open this URL in your system browser: ${data.auth_url}`;
    }
    toast(data.browser_opened ? "Opened your system browser. Sign in to YouTube." : "Copy the connect URL into your browser.");
    startYtPoll();
  } catch (err) { toast(err.message, true); }
});

$("#yt-disconnect")?.addEventListener("click", async () => {
  try {
    renderYoutube(await api("/api/youtube/disconnect", { method: "POST", body: {} }));
    toast("YouTube disconnected.");
  } catch (err) { toast(err.message, true); }
});

$("#yt-channel")?.addEventListener("change", async (e) => {
  const channelId = e.target.value;
  const title = e.target.selectedOptions?.[0]?.textContent || "";
  try {
    renderYoutube(await api("/api/youtube/channel", { method: "PUT", body: { channel_id: channelId, title } }));
    toast(channelId ? `Uploads will go to ${title}` : "Channel cleared.");
  } catch (err) { toast(err.message, true); }
});

$("#yt-paste")?.addEventListener("click", async () => {
  const raw = ($("#yt-code")?.value || "").trim();
  if (!raw) return toast("Paste the redirect URL or the code from Google.", true);
  try {
    const data = await api("/api/youtube/oauth/code", { method: "POST", body: { url: raw, code: raw } });
    renderYoutube(data);
    if ($("#yt-code")) $("#yt-code").value = "";
    toast("YouTube connected.");
  } catch (err) { toast(err.message, true); }
});

$("#job-yt-auto")?.addEventListener("change", async (e) => {
  if (!current) return;
  try {
    current = await api(`/api/projects/${current.id}`, {
      method: "PATCH",
      body: { youtube_auto_upload: !!e.target.checked },
    });
    fillJobYoutube();
    toast(current.youtube_auto_upload ? "This job will auto-upload when the video completes." : "Auto-upload off for this job.");
  } catch (err) { toast(err.message, true); }
});

$("#job-yt-privacy")?.addEventListener("change", async (e) => {
  if (!current) return;
  try {
    current = await api(`/api/projects/${current.id}`, {
      method: "PATCH",
      body: { youtube_privacy: e.target.value },
    });
    fillJobYoutube();
    toast(`This job will upload as ${current.youtube_privacy}.`);
  } catch (err) { toast(err.message, true); }
});

[
  ["#script-yt-description", "script"],
  ["#script-yt-keywords", "script"],
  ["#script-yt-hashtags", "script"],
  ["#job-yt-description", "job"],
  ["#job-yt-keywords", "job"],
  ["#job-yt-hashtags", "job"],
].forEach(([sel, source]) => {
  const el = $(sel);
  if (!el) return;
  el.addEventListener("input", () => scheduleYoutubeMetaSave(source));
  el.addEventListener("change", () => persistYoutubeMeta(source));
});

$("#job-yt-upload")?.addEventListener("click", async () => {
  await sendToYoutube("video");
});

$("#watch-yt-upload")?.addEventListener("click", async () => {
  await sendToYoutube("watch");
});

async function sendToYoutube(source) {
  if (!current) return toast("Create or pick a job first.", true);
  if (!current.has_video && !readyAspects().length) return toast("Render the video first.", true);
  if (!ytConnected) return toast("Connect YouTube in Settings first.", true);
  const aspect = uploadAspectForSource(source);
  const privacy = youtubePrivacyForUpload();
  const ytMeta = readYoutubeMetaFromDom(source === "watch" ? "job" : "job");
  try {
    await persistYoutubeMeta("job");
  } catch (_) { /* upload still uses body overrides */ }
  const watchLine = $("#watch-yt-line");
  const jobLine = $("#job-yt-line");
  try {
    setBusy(true, `Uploading to YouTube as ${privacy}…`);
    if (watchLine) {
      watchLine.hidden = false;
      watchLine.classList.remove("bad");
      watchLine.textContent = `Uploading ${aspect || "video"} as ${privacy}…`;
    }
    if (jobLine) jobLine.textContent = `Uploading ${aspect || "video"} as ${privacy}…`;
    const data = await api(`/api/projects/${current.id}/youtube`, {
      method: "POST",
      body: {
        privacy_status: privacy,
        aspect: aspect || "",
        description: ytMeta.youtube_description || "",
        tags: ytMeta.youtube_keywords || "",
      },
    });
    await loadJob(current.id, { poll: false });
    fillJobYoutube();
    toast(data.url ? `Uploaded as ${data.privacy}: ${data.url}` : "Uploaded to YouTube.");
  } catch (err) {
    try { await loadJob(current.id, { poll: false }); } catch (_) { /* keep prior state */ }
    fillJobYoutube();
    if (watchLine) {
      watchLine.hidden = false;
      watchLine.classList.add("bad");
      watchLine.textContent = `YouTube: ${err.message}`;
    }
    toast(err.message, true);
  } finally {
    if (!current?.running) setBusy(false, $("#run-status")?.textContent || "");
  }
}

function topicMinutes(item) {
  if (item?.duration_min != null && item.duration_min !== "") return Number(item.duration_min);
  return Math.max(1, Math.round((Number(item?.duration_seconds) || 300) / 60));
}

function pad2(n) {
  return String(n).padStart(2, "0");
}

function isoToLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

function localInputToIso(value) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return d.toISOString();
}

function defaultLocalInput() {
  return isoToLocalInput(new Date().toISOString());
}

function formatDue(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function splitLocalInput(value) {
  const raw = value || defaultLocalInput();
  const [date, time] = raw.split("T");
  return { date: date || "", time: (time || "").slice(0, 5) };
}

function joinLocalInput(date, time) {
  if (!date || !time) return "";
  return `${date}T${time}`;
}

function applyTopicCatalog(data) {
  topicList = data.topics || [];
  topicQueue = data.queue || [];
  topicsBusy = !!data.busy;
  topicMeta = data || {};
  const auto = $("#topics-auto-scheduler");
  if (auto && typeof data.auto_scheduler === "boolean") auto.checked = data.auto_scheduler;
  const settingsAuto = $("#auto-scheduler");
  if (settingsAuto && typeof data.auto_scheduler === "boolean") settingsAuto.checked = data.auto_scheduler;
  const chromeHands = $("#chrome-hands-off");
  if (chromeHands && typeof data.hands_off === "boolean") chromeHands.checked = data.hands_off;
  syncHandsOffWarning({ ...lastSettings, ...data });
}

function topicIsRendered(item) {
  const status = item?.status || "draft";
  if (status === "done") return true;
  const jobId = (item?.job_id || "").trim();
  if (!jobId) return false;
  // Opportunistic: if Jobs catalog is already loaded, catch status lag without extra API calls.
  const job = jobIndex.find((j) => j.id === jobId);
  if (!job) return false;
  return !!(
    job.has_video
    || job.status === "rendered"
    || job.job?.step === "done"
    || job.step === "done"
  );
}

function filteredTopics() {
  const q = topicsSearch.trim().toLowerCase();
  return topicList.filter((item) => {
    const status = item.status || "draft";
    const rendered = topicIsRendered(item);
    if (topicsFilter === "all") {
      // Default grid: hide finished / already-rendered topics.
      if (rendered) return false;
    } else if (topicsFilter === "done") {
      if (!rendered) return false;
    } else if (rendered || status !== topicsFilter) {
      // draft / queued / running — also exclude rendered lag cases
      return false;
    }
    if (!q) return true;
    const hay = `${item.title || ""} ${item.angle || ""} ${item.id || ""} ${status}`.toLowerCase();
    return hay.includes(q);
  });
}

function renderTopics() {
  const line = $("#topics-queue-line");
  if (line) {
    const waiting = topicQueue.length;
    const running = topicList.filter((t) => t.status === "running").length;
    const dueCount = Number(topicMeta.due_count || 0);
    const nextDue = formatDue(topicMeta.next_due_at_local || topicMeta.next_due_at);
    const tz = topicMeta.timezone ? ` (${topicMeta.timezone})` : "";
    const autoOn = topicMeta.auto_scheduler !== false;
    const handsOn = !!topicMeta.hands_off;
    const bits = [];
    if (topicsBusy && waiting) {
      bits.push(`${running ? "A job is running. " : ""}${waiting} topic${waiting === 1 ? "" : "s"} queued — the next starts when the current job finishes.`);
    } else if (waiting) {
      bits.push(`${waiting} topic${waiting === 1 ? "" : "s"} in the pipeline queue.`);
    } else if (running) {
      bits.push("Pipeline running for a scheduled topic.");
    }
    if (dueCount && handsOn && autoOn) bits.push(`${dueCount} due now.`);
    if (nextDue) bits.push(`Next due ${nextDue}${tz}.`);
    else if (!waiting && !running) {
      if (!handsOn) bits.push("Hands-off is off — scheduled topics stay queued until you turn it on or use Run now.");
      else if (!autoOn) bits.push("Auto-run due topics is off.");
      else bits.push("No upcoming scheduled topics.");
    }
    if (handsOn && !autoOn && (waiting || nextDue)) bits.push("Auto-run due topics is off — due topics will wait.");
    else if (!handsOn && (waiting || nextDue || dueCount)) bits.push("Hands-off is off — due topics will not auto-start.");
    line.textContent = bits.join(" ");
  }
  const root = $("#topics-list");
  if (!root) return;
  if (!topicList.length) {
    root.innerHTML = `<p class="empty-lib">No topics yet. Generate a batch, set a date and time, then Schedule.</p>`;
    updatePager("#topics-pager", "#topics-page-label", "#topics-prev", "#topics-next", 0, 1, 0);
    return;
  }
  const filtered = filteredTopics();
  if (!filtered.length) {
    root.innerHTML = `<p class="empty-lib">No topics match this filter.</p>`;
    updatePager("#topics-pager", "#topics-page-label", "#topics-prev", "#topics-next", 0, 1, 0);
    return;
  }
  const pageData = paginate(filtered, topicsPage);
  topicsPage = pageData.page;
  updatePager("#topics-pager", "#topics-page-label", "#topics-prev", "#topics-next", pageData.page, pageData.pages, pageData.total);
  root.innerHTML = pageData.items.map((item) => {
    const status = item.status || "draft";
    const mins = topicMinutes(item);
    const job = item.job_id
      ? `<button type="button" data-open-job="${esc(item.job_id)}">Open job</button>`
      : "";
    const when = isoToLocalInput(item.scheduled_at_local || item.scheduled_at) || defaultLocalInput();
    const dueLabel = item.scheduled_at ? formatDue(item.scheduled_at_local || item.scheduled_at) : "";
    const schedule = status === "running" || status === "done" || membershipLocked
      ? ""
      : `<button type="button" class="primary" data-schedule="${esc(item.id)}" data-run="queue">Schedule</button>
         <button type="button" data-schedule="${esc(item.id)}" data-run="now">Run now</button>`;
    const cancelSchedule = (status === "queued" || !!item.scheduled_at) && status !== "running" && status !== "done"
      ? `<button type="button" data-topic-unschedule="${esc(item.id)}">Cancel schedule</button>`
      : "";
    const editLock = status === "running" || status === "done" || membershipLocked;
    const errInfo = item.error ? simplifyJobError(item.error) : null;
    const statusLabel = item.error && status === "draft" ? "error" : status;
    const errBlock = errInfo
      ? `<div class="topic-error">
          <p class="topic-error-msg">${esc(errInfo.message)}</p>
          <button type="button" class="topic-error-details" data-topic-error="${esc(item.id)}">Details</button>
        </div>`
      : "";
    return `<article class="topic-card ${esc(status)}${item.error ? " has-error" : ""}" data-id="${esc(item.id)}">
      <div class="topic-card-top">
        <span class="pill topic-status ${esc(statusLabel)}">${esc(statusLabel)}</span>
        <label class="topic-duration">
          <input type="number" class="topic-duration-input" min="1" max="30" step="1" value="${esc(String(mins))}" ${editLock ? "readonly" : ""} />
          <span>min</span>
        </label>
      </div>
      <label>Title
        <input type="text" class="topic-title" value="${esc(item.title)}" ${editLock ? "readonly" : ""} />
      </label>
      <label>Angle
        <input type="text" class="topic-angle" value="${esc(item.angle || "")}" placeholder="one-line take" ${editLock ? "readonly" : ""} />
      </label>
      <label class="topic-when">Date &amp; time
        <input type="datetime-local" class="topic-datetime" value="${esc(when)}" ${editLock ? "readonly" : ""} />
      </label>
      ${dueLabel ? `<p class="topic-due">${status === "queued" ? (item.due ? "Due now · " : "Due ") : ""}${esc(dueLabel)}</p>` : ""}
      <div class="job-actions topic-actions">
        ${schedule}
        ${cancelSchedule}
        ${job}
        <button type="button" class="danger" data-topic-delete="${esc(item.id)}">Delete</button>
      </div>
      ${errBlock}
    </article>`;
  }).join("");
}

async function loadTopics(opts = {}) {
  try {
    const data = await api("/api/topics", { timeoutMs: 15000 });
    applyTopicCatalog(data);
    renderTopics();
  } catch (err) {
    if (!opts.silent) toast(err.message, true);
  }
}

function startTopicsPoll() {
  if (topicsTimer) return;
  topicsTimer = setInterval(() => {
    if ($("#view-topics")?.classList.contains("on")) loadTopics({ silent: true });
  }, 15000);
}

async function saveTopicEdits(id, card, extra = {}) {
  if (!requireMembership("edit topics")) return null;
  const title = card?.querySelector(".topic-title")?.value?.trim();
  const angle = card?.querySelector(".topic-angle")?.value?.trim() || "";
  const durationRaw = card?.querySelector(".topic-duration-input")?.value;
  const duration_min = Number(durationRaw);
  if (!title) throw new Error("Title cannot be empty.");
  const body = { title, angle, ...extra };
  if (Number.isFinite(duration_min) && duration_min > 0) body.duration_min = duration_min;
  const data = await api(`/api/topics/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body,
  });
  applyTopicCatalog(data);
  return data.topic;
}

$("#topics-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!requireMembership("generate topics")) return;
  const btn = $("#topics-generate");
  const fd = new FormData(e.target);
  if (btn) btn.disabled = true;
  try {
    const body = {
      seed: fd.get("seed") || "",
      count: Number(fd.get("count") || 8),
      duration_min: Number(fd.get("duration") || 5),
    };
    if (needsOpenAiSpend()) {
      const ok = await confirmSpend(`Generate ${body.count} topics with the billed OpenAI API?`);
      if (!ok) return;
      body.confirm_spend = true;
    }
    const data = await api("/api/topics/generate", {
      method: "POST",
      body,
    });
    applyTopicCatalog(data);
    renderTopics();
    if (data.use_mcp) {
      toast(data.message || `Use ${textProviderLabel()} Desktop MCP to invent topics, then create_topic / schedule_topic.`);
    } else {
      toast(`Generated ${data.count || (data.created || []).length} topics.`);
    }
  } catch (err) {
    toast(err.message, true);
  } finally {
    if (btn) btn.disabled = membershipLocked;
  }
});

$("#topics-list")?.addEventListener("click", async (e) => {
  const errBtn = e.target.closest("[data-topic-error]");
  if (errBtn) {
    const topic = topicList.find((t) => t.id === errBtn.dataset.topicError);
    if (topic?.error) showJobErrorModal(topic.error, { force: true });
    return;
  }
  const open = e.target.closest("[data-open-job]");
  if (open) {
    try {
      await refreshJobs(open.dataset.openJob);
      await openFromLibrary(open.dataset.openJob);
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  const del = e.target.closest("[data-topic-delete]");
  if (del) {
    try {
      const data = await api(`/api/topics/${encodeURIComponent(del.dataset.topicDelete)}`, { method: "DELETE" });
      applyTopicCatalog(data);
      renderTopics();
      toast("Topic deleted.");
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  const unschedule = e.target.closest("[data-topic-unschedule]");
  if (unschedule) {
    try {
      const data = await api(`/api/topics/${encodeURIComponent(unschedule.dataset.topicUnschedule)}/unschedule`, {
        method: "POST",
      });
      applyTopicCatalog(data);
      renderTopics();
      toast("Schedule canceled.");
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  const sched = e.target.closest("[data-schedule]");
  if (!sched) return;
  const card = sched.closest(".topic-card");
  const id = sched.dataset.schedule;
  const runNow = (sched.dataset.run || "queue") === "now";
  try {
    await saveTopicEdits(id, card);
    if (membershipLocked) return;
    if (runNow) {
      await submitTopicSchedule(id, { run_now: true, run: "now" });
      return;
    }
    openScheduleDialog(id, card);
  } catch (err) {
    toast(err.message, true);
  }
});

$("#topics-list")?.addEventListener("focusout", async (e) => {
  const field = e.target.closest(".topic-title, .topic-angle, .topic-datetime, .topic-duration-input");
  if (!field) return;
  const card = field.closest(".topic-card");
  const id = card?.dataset.id;
  if (!id) return;
  try {
    const extra = {};
    if (field.classList.contains("topic-datetime")) {
      extra.scheduled_at = localInputToIso(field.value) || null;
    }
    if (membershipLocked) return;
    await saveTopicEdits(id, card, extra);
    if (field.classList.contains("topic-datetime")) renderTopics();
  } catch (err) {
    toast(err.message, true);
  }
});

async function submitTopicSchedule(id, body) {
  if (!requireMembership("schedule topics")) return null;
  const data = await api(`/api/topics/${encodeURIComponent(id)}/schedule`, {
    method: "POST",
    body,
  });
  applyTopicCatalog(data);
  renderTopics();
  if (data.job_id) await refreshJobs(data.job_id);
  if (data.started || data.busy) startPolling();
  toast(data.message || (data.started ? "Pipeline started." : "Queued."));
  return data;
}

function openScheduleDialog(id, card) {
  const dlg = $("#topic-schedule-dialog");
  if (!dlg) {
    const when = card?.querySelector(".topic-datetime")?.value;
    submitTopicSchedule(id, { run: "queue", scheduled_at: localInputToIso(when) || undefined }).catch((err) => toast(err.message, true));
    return;
  }
  scheduleDialogTopicId = id;
  const item = topicList.find((t) => t.id === id);
  const title = $("#topic-schedule-title");
  if (title) title.textContent = item?.title || card?.querySelector(".topic-title")?.value || "Topic";
  const fromCard = card?.querySelector(".topic-datetime")?.value || isoToLocalInput(item?.scheduled_at_local || item?.scheduled_at) || defaultLocalInput();
  const parts = splitLocalInput(fromCard);
  const dateEl = $("#topic-schedule-date");
  const timeEl = $("#topic-schedule-time");
  if (dateEl) dateEl.value = parts.date;
  if (timeEl) timeEl.value = parts.time;
  if (typeof dlg.showModal === "function") dlg.showModal();
  else dlg.setAttribute("open", "");
}

function closeScheduleDialog() {
  const dlg = $("#topic-schedule-dialog");
  scheduleDialogTopicId = "";
  if (!dlg) return;
  if (typeof dlg.close === "function") dlg.close();
  else dlg.removeAttribute("open");
}

$("#topic-schedule-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = scheduleDialogTopicId;
  if (!id) return closeScheduleDialog();
  const scheduled_at = localInputToIso(joinLocalInput($("#topic-schedule-date")?.value, $("#topic-schedule-time")?.value));
  try {
    await submitTopicSchedule(id, { run: "queue", scheduled_at });
    closeScheduleDialog();
  } catch (err) {
    toast(err.message, true);
  }
});

$("#topic-schedule-now")?.addEventListener("click", async () => {
  const id = scheduleDialogTopicId;
  if (!id) return;
  try {
    await submitTopicSchedule(id, { run_now: true, run: "now" });
    closeScheduleDialog();
  } catch (err) {
    toast(err.message, true);
  }
});

$("#topic-schedule-cancel")?.addEventListener("click", () => closeScheduleDialog());

$("#topics-auto-scheduler")?.addEventListener("change", async (e) => {
  try {
    const saved = await api("/api/settings", { method: "PUT", body: { auto_scheduler: !!e.target.checked } });
    fillSettings(saved);
    toast(saved.auto_scheduler
      ? "Auto-run due topics on — starts only while Hands-off is also on."
      : "Auto-run due topics off. Use Run now, or turn Hands-off + Auto-run on.");
    await loadTopics({ silent: true });
  } catch (err) {
    toast(err.message, true);
  }
});

function handsOffWarnText(data) {
  const src = data || lastSettings || {};
  const bits = [];
  if ((src.image_provider || "") === "chatgpt") {
    bits.push("ChatGPT images cannot run unsupervised. Switch to Flux or ComfyUI for hands-off.");
  }
  const text = src.text_provider || src.script_provider || "";
  if (text === "chatgpt" || text === "claude") {
    bits.push("Auto topic generation is skipped for ChatGPT/Claude; existing drafts still schedule.");
  }
  return bits.join(" ");
}

function syncHandsOffWarning(data) {
  const msg = handsOffWarnText(data || lastSettings);
  const el = $("#hands-off-warn");
  if (!el) return;
  el.hidden = !msg;
  el.textContent = msg;
}

async function saveHandsOff(enabled) {
  const interval = Number($("#hands-off-interval")?.value);
  const minQueue = Number($("#hands-off-min-queue")?.value);
  const body = { hands_off: !!enabled };
  if (Number.isFinite(interval) && interval >= 0) body.hands_off_interval_hours = interval;
  if (Number.isFinite(minQueue) && minQueue >= 1) body.hands_off_min_queue = minQueue;
  const saved = await api("/api/settings", { method: "PUT", body });
  fillSettings(saved);
  return saved;
}

$("#chrome-hands-off")?.addEventListener("change", async (e) => {
  try {
    const saved = await saveHandsOff(!!e.target.checked);
    toast(saved.hands_off
      ? "Hands-off on — Studio will generate, schedule, auto-start due topics, render, and upload as private."
      : "Hands-off off — scheduled topics stay queued until you turn it on or use Run now.");
  } catch (err) {
    e.target.checked = !e.target.checked;
    toast(err.message, true);
  }
});

document.querySelector('#settings-form [name="image_provider"]')?.addEventListener("change", (e) => {
  syncHandsOffWarning({ ...lastSettings, image_provider: e.target.value });
});

$("#restart-api")?.addEventListener("click", async () => {
  const btn = $("#restart-api");
  const desktop = typeof window.bubblePod?.restartApi === "function";
  const targetPort = Number($("#studio-port")?.value || lastSettings.port || 7878) || 7878;
  const targetUrl = `${location.protocol}//${location.hostname}:${targetPort}/`;
  if (!desktop && !window.confirm(`Restart the Studio API? This page will come back when ${targetUrl} is healthy.`)) {
    return;
  }
  if (btn) btn.disabled = true;
  toast("Restarting API…");
  try {
    if (desktop) {
      const result = await window.bubblePod.restartApi();
      if (result && result.ok === false) throw new Error(result.error || "Restart failed");
      toast("API restarted.");
      return;
    }
    try {
      await api("/api/admin/restart", { method: "POST", body: {} });
    } catch {
      /* connection drop is expected as uvicorn exits */
    }
    for (let i = 0; i < 60; i += 1) {
      await new Promise((r) => setTimeout(r, 400));
      try {
        const health = await fetch(`${targetUrl}api/health`, { cache: "no-store" });
        if (health.ok) {
          if (String(location.port || "") !== String(targetPort) && !(targetPort === 80 && !location.port)) {
            location.href = targetUrl;
          } else {
            location.reload();
          }
          return;
        }
      } catch {
        /* still down */
      }
    }
    toast(`API did not come back on port ${targetPort}.`, true);
  } catch (err) {
    toast(err.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
});

$("#account-change-password")?.addEventListener("click", async () => {
  const currentPassword = ($("#account-current-password")?.value || "");
  const newPassword = ($("#account-new-password")?.value || "").trim();
  const confirmPassword = ($("#account-confirm-password")?.value || "").trim();
  if (!currentPassword || !newPassword) {
    toast("Enter current and new password.", true);
    return;
  }
  if (newPassword !== confirmPassword) {
    toast("New password and confirmation do not match.", true);
    return;
  }
  try {
    const result = await api("/api/auth/change-password", {
      method: "POST",
      body: {
        current_password: currentPassword,
        new_password: newPassword,
        confirm_password: confirmPassword,
      },
    });
    if (result.token) {
      try { localStorage.setItem("bubblepod_token", result.token); } catch { /* ignore */ }
    }
    ["#account-current-password", "#account-new-password", "#account-confirm-password"].forEach((sel) => {
      const el = $(sel);
      if (el) el.value = "";
    });
    const line = $("#account-user-line");
    if (line && result.username) line.textContent = `Signed in as: ${result.username}`;
    toast("Password changed.");
  } catch (err) {
    toast(err.message, true);
  }
});

$("#studio-port")?.addEventListener("change", () => {
  const port = Number($("#studio-port")?.value || 7878) || 7878;
  const ngrokPort = $("#ngrok-local-port");
  if (ngrokPort && !ngrokPort.dataset.userEdited) {
    ngrokPort.value = String(port);
    syncNgrokCommandPreview();
  }
});
$("#ngrok-local-port")?.addEventListener("input", () => {
  const el = $("#ngrok-local-port");
  if (el) el.dataset.userEdited = "1";
});

let promptCatalog = [];

function stashVisiblePromptDrafts() {
  $$("#prompt-list textarea.prompt-text").forEach((el) => {
    if (el.dataset.key) promptDrafts[el.dataset.key] = el.value;
  });
}

function promptDisplayValue(p) {
  if (Object.prototype.hasOwnProperty.call(promptDrafts, p.key)) return promptDrafts[p.key];
  return p.value;
}

function collectPromptValues() {
  stashVisiblePromptDrafts();
  const out = {};
  for (const p of promptCatalog) {
    out[p.key] = promptDisplayValue(p);
  }
  return out;
}

function orderedPrompts() {
  const groups = {};
  for (const p of promptCatalog) {
    (groups[p.category] ||= []).push(p);
  }
  const order = ["Script", "Topics", "Images / art", "Pipeline", "MCP"];
  const cats = [...order.filter((c) => groups[c]), ...Object.keys(groups).filter((c) => !order.includes(c))];
  return cats.flatMap((cat) => groups[cat] || []);
}

function promptCard(p) {
  const value = promptDisplayValue(p);
  const tall = (p.default || "").length > 800 || (value || "").length > 800;
  const editable = p.editable !== false;
  const lock = p.admin_only ? ` <span class="badge">admin</span>` : "";
  const actions = editable
    ? `<div class="prompt-actions">
        <button type="button" data-reset="${esc(p.key)}">Reset this prompt</button>
        <button type="button" class="primary" data-save="${esc(p.key)}">Save</button>
      </div>`
    : `<div class="prompt-actions"><span class="hint">Read-only (admin)</span></div>`;
  return `<article class="card prompt-card ${tall ? "tall" : ""}${editable ? "" : " locked"}" data-key="${esc(p.key)}">
    <div class="prompt-head">
      <div>
        <strong>${esc(p.label)}</strong>
        <div class="prompt-key"><code>${esc(p.key)}</code>${p.is_overridden ? ` <span class="badge">override</span>` : ""}${lock}</div>
        <p class="hint">${esc(p.description)}</p>
      </div>
      ${actions}
    </div>
    <textarea class="prompt-text" data-key="${esc(p.key)}" spellcheck="false" ${editable ? "" : "readonly"}>${esc(value)}</textarea>
  </article>`;
}

function renderPrompts() {
  stashVisiblePromptDrafts();
  const root = $("#prompt-list");
  if (!root) return;
  const all = orderedPrompts();
  if (!all.length) {
    root.innerHTML = `<p class="hint">No prompts in the catalog.</p>`;
    updatePager("#prompts-pager", "#prompts-page-label", "#prompts-prev", "#prompts-next", 0, 1, 0, PROMPTS_PAGE);
    return;
  }
  const pageData = paginate(all, promptsPage, PROMPTS_PAGE);
  promptsPage = pageData.page;
  updatePager("#prompts-pager", "#prompts-page-label", "#prompts-prev", "#prompts-next", pageData.page, pageData.pages, pageData.total, PROMPTS_PAGE);
  let lastCat = null;
  root.innerHTML = pageData.items.map((p) => {
    const head = p.category !== lastCat ? `<h3 class="lib-heading">${esc(p.category)}</h3>` : "";
    lastCat = p.category;
    return head + promptCard(p);
  }).join("");
}

async function loadPrompts() {
  try {
    const data = await api("/api/prompts");
    promptCatalog = data.prompts || [];
    promptDrafts = {};
    const note = $("#prompts-note");
    if (note && data.note) note.textContent = data.note;
    renderPrompts();
  } catch (err) {
    toast(err.message, true);
  }
}

async function savePromptValues(updates, msg) {
  const safe = {};
  for (const [key, text] of Object.entries(updates || {})) {
    const entry = promptCatalog.find((p) => p.key === key);
    if (entry && entry.editable === false) continue;
    safe[key] = text;
  }
  if (!Object.keys(safe).length) {
    toast("Nothing editable to save.", true);
    return;
  }
  const data = await api("/api/prompts", { method: "PUT", body: { prompts: safe } });
  promptCatalog = data.prompts || [];
  promptDrafts = {};
  renderPrompts();
  toast(msg || "Prompts saved.");
}

$("#prompt-list")?.addEventListener("click", async (e) => {
  const saveBtn = e.target.closest("[data-save]");
  const resetBtn = e.target.closest("[data-reset]");
  const card = e.target.closest(".prompt-card");
  try {
    if (saveBtn) {
      const key = saveBtn.dataset.save;
      const entry = promptCatalog.find((p) => p.key === key);
      if (entry && entry.editable === false) {
        toast("Only admins can edit this prompt.", true);
        return;
      }
      const area = card?.querySelector("textarea.prompt-text");
      if (!area) return;
      const data = await api("/api/prompts", { method: "PUT", body: { key, text: area.value } });
      promptCatalog = data.prompts || [];
      delete promptDrafts[key];
      renderPrompts();
      toast(`Saved ${key}.`);
      return;
    }
    if (resetBtn) {
      const key = resetBtn.dataset.reset;
      const entry = promptCatalog.find((p) => p.key === key);
      if (entry && entry.editable === false) {
        toast("Only admins can reset this prompt.", true);
        return;
      }
      const data = await api("/api/prompts/reset", { method: "POST", body: { key } });
      promptCatalog = data.prompts || [];
      delete promptDrafts[key];
      renderPrompts();
      toast(`Reset ${key} to default.`);
    }
  } catch (err) {
    toast(err.message, true);
  }
});

$("#prompt-list")?.addEventListener("input", (e) => {
  const area = e.target.closest("textarea.prompt-text");
  if (!area?.dataset.key) return;
  promptDrafts[area.dataset.key] = area.value;
});

$("#prompts-save-all")?.addEventListener("click", async () => {
  try {
    await savePromptValues(collectPromptValues(), "All prompts saved.");
  } catch (err) {
    toast(err.message, true);
  }
});

$("#prompts-restore-all")?.addEventListener("click", () => {
  $("#prompts-reset-dialog")?.showModal();
});
$("#prompts-reset-dialog")?.addEventListener("close", async () => {
  if ($("#prompts-reset-dialog").returnValue !== "confirm") return;
  try {
    const data = await api("/api/prompts/reset", { method: "POST", body: {} });
    promptCatalog = data.prompts || [];
    promptDrafts = {};
    promptsPage = 0;
    renderPrompts();
    toast("All prompts restored to defaults.");
  } catch (err) {
    toast(err.message, true);
  }
});

$("#prompts-prev")?.addEventListener("click", () => {
  stashVisiblePromptDrafts();
  promptsPage = Math.max(0, promptsPage - 1);
  renderPrompts();
});
$("#prompts-next")?.addEventListener("click", () => {
  stashVisiblePromptDrafts();
  promptsPage += 1;
  renderPrompts();
});

async function boot() {
  try {
    const health = await api("/api/health?full=1");
    applyGentleStatus(health.gentle);
    if (health.warnings?.length) toast(health.warnings[0], true);
  } catch {
    setGentleChrome({ ok: false, backend: "none", url: "" });
    const labelEl = $("#gentle-label");
    if (labelEl) labelEl.textContent = "Studio API not reachable.";
  }
  startGentleHealthPolling();
  try {
    const settings = await api("/api/settings");
    fillSettings(settings);
  } catch (err) {
    toast(err.message, true);
  }
  try {
    await refreshJobs();
  } catch (err) {
    toast(err.message, true);
  }
  try {
    applyLibraryFilter(localStorage.getItem(STORE_LIB_FILTER) || "all");
  } catch {
    applyLibraryFilter("all");
  }
  await restoreRoute();
  await loadVoices();
  startTopicsPoll();
}

async function checkSession() {
  try {
    await api("/api/auth/me");
    return true;
  } catch {
    return false;
  }
}

async function startApp() {
  try {
    const health = await fetch("/api/health").then((r) => (r.ok ? r.json() : null));
    if (health && health.desktop_mode) desktopMode = true;
  } catch { /* ignore */ }
  if (desktopMode || (typeof window !== "undefined" && window.bubblePod && window.bubblePod.isDesktop)) {
    desktopMode = true;
    applyDesktopModeUi();
    showStudioApp();
    await boot();
    try {
      const me = await api("/api/auth/me");
      await refreshMembershipUi({ ...me, stripe_configured: false, desktop_mode: true });
    } catch { /* ignore */ }
    applyDesktopModeUi();
    return;
  }
  const hash = location.hash || "";
  const resetMatch = hash.match(/^#reset=([^&]+)/);
  if (resetMatch) {
    window.__resetToken = decodeURIComponent(resetMatch[1]);
    showLoginGate("", "reset");
    return;
  }
  const ok = await checkSession();
  if (!ok) {
    showLoginGate();
    return;
  }
  showStudioApp();
  await boot();
  try { await refreshMembershipUi(); } catch { /* ignore */ }
}

$("#login-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = ($("#login-user")?.value || "").trim();
  const password = $("#login-pass")?.value || "";
  const btn = $("#login-submit");
  const err = $("#login-error");
  if (err) { err.hidden = true; err.textContent = ""; }
  if (btn) btn.disabled = true;
  try {
    const data = await api("/api/auth/login", { method: "POST", body: { username, password } });
    setAuthToken(data.token || "");
    showStudioApp();
    await boot();
    await refreshMembershipUi(data);
  } catch (ex) {
    if (err) {
      err.hidden = false;
      err.textContent = ex.message || "Sign in failed";
    }
  } finally {
    if (btn) btn.disabled = false;
  }
});

$("#login-signup-toggle")?.addEventListener("click", () => {
  showLoginGate("", "signup");
});
$("#login-forgot-toggle")?.addEventListener("click", () => {
  showLoginGate("", "forgot");
});
$("#signup-login-toggle")?.addEventListener("click", () => {
  showLoginGate("", "login");
});
$("#forgot-login-toggle")?.addEventListener("click", () => {
  showLoginGate("", "login");
});
$("#reset-login-toggle")?.addEventListener("click", () => {
  location.hash = "";
  window.__resetToken = "";
  showLoginGate("", "login");
});

$("#forgot-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#forgot-error");
  const ok = $("#forgot-ok");
  if (err) { err.hidden = true; err.textContent = ""; }
  if (ok) { ok.hidden = true; ok.textContent = ""; }
  const btn = $("#forgot-submit");
  if (btn) btn.disabled = true;
  try {
    const data = await api("/api/auth/forgot-password", {
      method: "POST",
      body: {
        email: ($("#forgot-email")?.value || "").trim(),
        username: ($("#forgot-user")?.value || "").trim(),
      },
    });
    if (ok) {
      ok.hidden = false;
      ok.textContent = data.message || "If that account has an email, a reset link was sent.";
    }
  } catch (ex) {
    if (err) {
      err.hidden = false;
      err.textContent = ex.message || "Could not send reset email";
    }
  } finally {
    if (btn) btn.disabled = false;
  }
});

$("#reset-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#reset-error");
  if (err) { err.hidden = true; err.textContent = ""; }
  const btn = $("#reset-submit");
  if (btn) btn.disabled = true;
  try {
    const data = await api("/api/auth/reset-password", {
      method: "POST",
      body: {
        token: window.__resetToken || "",
        new_password: $("#reset-pass")?.value || "",
        confirm_password: $("#reset-pass2")?.value || "",
      },
    });
    setAuthToken(data.token || "");
    location.hash = "";
    window.__resetToken = "";
    showStudioApp();
    await boot();
    await refreshMembershipUi(data);
    toast("Password updated.");
  } catch (ex) {
    if (err) {
      err.hidden = false;
      err.textContent = ex.message || "Reset failed";
    }
  } finally {
    if (btn) btn.disabled = false;
  }
});

$("#signup-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#signup-submit");
  const err = $("#signup-error");
  if (err) { err.hidden = true; err.textContent = ""; }
  if (btn) btn.disabled = true;
  try {
    const data = await api("/api/auth/signup", {
      method: "POST",
      body: {
        username: ($("#signup-user")?.value || "").trim(),
        email: ($("#signup-email")?.value || "").trim(),
        password: $("#signup-pass")?.value || "",
      },
    });
    setAuthToken(data.token || "");
    showStudioApp();
    await boot();
    await refreshMembershipUi(data);
    if (!data.has_access) {
      toast("Account created. Open Pricing to subscribe and unlock Studio.");
    }
  } catch (ex) {
    if (err) {
      err.hidden = false;
      err.textContent = ex.message || "Sign up failed";
    }
  } finally {
    if (btn) btn.disabled = false;
  }
});

async function refreshMembershipUi(session) {
  if (desktopMode || session?.desktop_mode) {
    desktopMode = true;
    applyDesktopModeUi();
    return;
  }
  let me = session;
  try {
    me = await api("/api/billing/status");
  } catch {
    me = session || {};
  }
  if (me.desktop_mode) {
    desktopMode = true;
    applyDesktopModeUi();
    return;
  }
  const isAdmin = !!me.is_admin;
  document.body.classList.toggle("is-admin", isAdmin);
  document.body.classList.toggle("is-member", !isAdmin);
  const adminLink = $("#admin-link");
  if (adminLink) adminLink.hidden = !isAdmin;
  const pricingLink = $("#pricing-link");
  if (pricingLink) pricingLink.hidden = !!isAdmin;
  $$(".admin-only").forEach((el) => {
    if (el.id === "admin-link") return;
    el.hidden = !isAdmin;
  });
  $$(".member-only").forEach((el) => {
    if (el.id === "pricing-link") {
      el.hidden = !!isAdmin;
      return;
    }
    el.hidden = isAdmin;
  });

  const status = (me.subscription_status || "none").toLowerCase();
  const hasAccess = !!me.has_access;
  const stripeOk = !!me.stripe_configured;
  const needsPay = !!(stripeOk && !hasAccess && !isAdmin);
  const isTrial = !isAdmin && status === "trialing";
  const isPastDue = !isAdmin && (status === "past_due" || status === "unpaid");
  const canSubscribe = !isAdmin && stripeOk && !["active", "trialing"].includes(status);
  const canManage = !isAdmin && stripeOk;
  const showBanner = !isAdmin && stripeOk && (needsPay || isTrial || isPastDue);

  membershipLocked = needsPay;
  const banner = $("#membership-banner");
  if (banner) {
    banner.hidden = !showBanner;
    banner.dataset.tone = isTrial ? "trial" : (needsPay || isPastDue ? "pay" : "");
    if (showBanner) {
      const amount = me.catalog?.amount_cents;
      const cur = (me.catalog?.currency || "usd").toUpperCase();
      const interval = me.catalog?.interval || "month";
      const price = amount != null ? `$${(Number(amount) / 100).toFixed(2)} ${cur}/${interval}` : "membership";
      const text = $("#membership-banner-text");
      if (text) {
        if (isTrial) {
          text.textContent =
            `Trial active (${price}). Open Pricing to manage or cancel before it converts.`;
        } else if (isPastDue) {
          text.textContent =
            `Payment issue (${status}). Update your card or resubscribe on the Pricing page (${price}).`;
        } else {
          text.textContent =
            `Membership required (${price}). Browse and delete stay open — subscribe to create, edit, generate topics, or render.`;
        }
      }
    }
  }

  const statusLine = $("#membership-status-line");
  if (statusLine) {
    const access = hasAccess ? "full access" : "locked";
    statusLine.textContent = isAdmin
      ? `Admin · ${access}`
      : `Status: ${status} · ${access}`;
  }

  const checkoutBtn = $("#settings-membership-checkout");
  const portalBtn = $("#settings-membership-portal");
  const bannerCheckout = $("#membership-checkout");
  const bannerPortal = $("#membership-portal");
  const bannerPricing = $("#membership-pricing");
  if (checkoutBtn) {
    checkoutBtn.hidden = !canSubscribe;
    checkoutBtn.disabled = !stripeOk;
    checkoutBtn.textContent = ["canceled", "incomplete", "incomplete_expired"].includes(status)
      ? "Resubscribe"
      : "Subscribe";
  }
  if (portalBtn) {
    portalBtn.hidden = !canManage;
    portalBtn.textContent = ["active", "trialing", "past_due"].includes(status)
      ? "Manage / cancel"
      : "Manage billing";
  }
  if (bannerCheckout) {
    bannerCheckout.hidden = !canSubscribe;
    bannerCheckout.textContent = checkoutBtn?.textContent || "Subscribe";
  }
  if (bannerPortal) {
    bannerPortal.hidden = !canManage;
    bannerPortal.textContent = portalBtn?.textContent || "Manage / cancel";
  }
  if (bannerPricing) bannerPricing.hidden = !showBanner;

  const memLead = $("#membership-settings-lead");
  if (memLead) {
    if (!stripeOk) {
      memLead.textContent = "Billing is not configured yet. Ask an admin to connect Stripe.";
    } else if (isTrial) {
      memLead.textContent = "Your trial is active. Open the pricing page to cancel or manage billing.";
    } else if (needsPay || isPastDue) {
      const amount = me.catalog?.amount_cents;
      const cur = (me.catalog?.currency || "usd").toUpperCase();
      const interval = me.catalog?.interval || "month";
      const price = amount != null ? `$${(Number(amount) / 100).toFixed(2)} ${cur}/${interval}` : "a membership";
      memLead.textContent = `Subscribe for ${price} on the pricing page. Every member gets the same privileges.`;
    } else {
      memLead.textContent = "All members share the same Studio privileges for their own jobs. Manage or cancel anytime on Pricing.";
    }
  }
  applyMembershipUiLocks();
  if (me.must_change_password && isAdmin) {
    toast("Change the default admin password in Settings → Account before going live.", true);
    try { setStep("settings"); } catch { /* ignore */ }
  }
}

async function startMembershipCheckout() {
  const data = await api("/api/billing/checkout", { method: "POST", body: {} });
  if (data.url) location.href = data.url;
  else toast(data.reason || data.next || "Checkout unavailable", true);
}

async function openMembershipPortal() {
  const data = await api("/api/billing/portal", { method: "POST", body: {} });
  if (data.url) location.href = data.url;
  else toast("Portal unavailable", true);
}

$("#membership-checkout")?.addEventListener("click", async () => {
  try {
    await startMembershipCheckout();
  } catch (err) {
    toast(err.message, true);
  }
});

$("#membership-portal")?.addEventListener("click", async () => {
  try {
    await openMembershipPortal();
  } catch (err) {
    toast(err.message, true);
  }
});

$("#settings-membership-checkout")?.addEventListener("click", async () => {
  try {
    await startMembershipCheckout();
  } catch (err) {
    toast(err.message, true);
  }
});

$("#settings-membership-portal")?.addEventListener("click", async () => {
  try {
    await openMembershipPortal();
  } catch (err) {
    toast(err.message, true);
  }
});

$("#logout-btn")?.addEventListener("click", async () => {
  if (desktopMode) return;
  try {
    await api("/api/auth/logout", { method: "POST", body: {} });
  } catch {}
  setAuthToken("");
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (topicsTimer) { clearInterval(topicsTimer); topicsTimer = null; }
  showLoginGate();
});

$("#jobs-search")?.addEventListener("input", (e) => {
  jobsSearch = e.target.value || "";
  jobsPage = 0;
  renderJobsQueue();
});
$("#jobs-filters")?.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-jobs-filter]");
  if (!btn) return;
  jobsFilter = btn.dataset.jobsFilter || "all";
  jobsPage = 0;
  $$("#jobs-filters [data-jobs-filter]").forEach((el) => {
    el.classList.toggle("on", el === btn);
  });
  renderJobsQueue();
});
$("#jobs-prev")?.addEventListener("click", () => {
  jobsPage = Math.max(0, jobsPage - 1);
  renderJobsQueue();
});
$("#jobs-next")?.addEventListener("click", () => {
  jobsPage += 1;
  renderJobsQueue();
});

$("#topics-search")?.addEventListener("input", (e) => {
  topicsSearch = e.target.value || "";
  topicsPage = 0;
  renderTopics();
});
$("#topics-filters")?.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-topics-filter]");
  if (!btn) return;
  topicsFilter = btn.dataset.topicsFilter || "all";
  topicsPage = 0;
  $$("#topics-filters [data-topics-filter]").forEach((el) => {
    el.classList.toggle("on", el === btn);
  });
  renderTopics();
});
$("#topics-prev")?.addEventListener("click", () => {
  topicsPage = Math.max(0, topicsPage - 1);
  renderTopics();
});
$("#topics-next")?.addEventListener("click", () => {
  topicsPage += 1;
  renderTopics();
});

$("#costs-search")?.addEventListener("input", (e) => {
  costsSearch = e.target.value || "";
  costsPage = 0;
  renderCostsTable();
});
$("#costs-prev")?.addEventListener("click", () => {
  costsPage = Math.max(0, costsPage - 1);
  renderCostsTable();
});
$("#costs-next")?.addEventListener("click", () => {
  costsPage += 1;
  renderCostsTable();
});

startApp();