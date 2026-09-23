const { app, BrowserWindow, dialog, ipcMain, shell } = require("electron");
const { spawn, spawnSync } = require("child_process");
const http = require("http");
const fs = require("fs");
const path = require("path");

const HOST = "127.0.0.1";
const DEFAULT_PORT = 7878;
const IS_WIN = process.platform === "win32";
const IS_MAC = process.platform === "darwin";

let mainWindow = null;
let child = null;
let startedByUs = false;
let restarting = false;
let healthWatch = null;
let wasOffline = false;
let studioPort = DEFAULT_PORT;

function studioUrl() {
  return `http://${HOST}:${studioPort}`;
}

app.setName("Bubble Pod");
if (IS_WIN) {
  app.setAppUserModelId("com.lazykh.studio");
}

function resolveIcon() {
  const icns = path.join(__dirname, "icons", "icon.icns");
  const ico = path.join(__dirname, "icons", "icon.ico");
  const png = path.join(__dirname, "icons", "icon.png");
  if (IS_MAC && fs.existsSync(icns)) return icns;
  if (IS_WIN && fs.existsSync(ico)) return ico;
  if (fs.existsSync(png)) return png;
  if (fs.existsSync(ico)) return ico;
  if (fs.existsSync(icns)) return icns;
  return undefined;
}

function repoRoot() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "lazykh");
  }
  return path.join(__dirname, "..");
}

function bundledFfmpegDir() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "ffmpeg");
  }
  return path.join(__dirname, "bin", "ffmpeg");
}

/** Writable user_data (projects, music library, settings). Packaged → app userData. */
function userDataRoot() {
  if (app.isPackaged) {
    return path.join(app.getPath("userData"), "user_data");
  }
  return path.join(repoRoot(), "user_data");
}

function readStudioPort() {
  const candidates = [
    path.join(userDataRoot(), "listen_port.json"),
    path.join(userDataRoot(), "settings.json"),
  ];
  for (const file of candidates) {
    try {
      if (!fs.existsSync(file)) continue;
      const data = JSON.parse(fs.readFileSync(file, "utf8"));
      const p = Number(data.port);
      if (Number.isFinite(p) && p >= 1 && p <= 65535) return p;
    } catch {
      /* ignore */
    }
  }
  const envPort = Number(process.env.BUBBLEPOD_PORT || process.env.LAZYKH_PORT || 0);
  if (Number.isFinite(envPort) && envPort >= 1 && envPort <= 65535) return envPort;
  return DEFAULT_PORT;
}

function refreshStudioPort() {
  studioPort = readStudioPort();
  return studioPort;
}

function ffmpegBinaryName(base) {
  return IS_WIN ? `${base}.exe` : base;
}

/** Env vars that must never flow from the build/host machine into the packaged app. */
const SECRET_ENV_KEYS = [
  "OPENAI_API_KEY",
  "OPENAI_MODEL",
  "ELEVENLABS_API_KEY",
  "FAL_KEY",
  "FAL_API_KEY",
  "YOUTUBE_CLIENT_ID",
  "YOUTUBE_CLIENT_SECRET",
  "STRIPE_SECRET_KEY",
  "STRIPE_PUBLISHABLE_KEY",
  "STRIPE_WEBHOOK_SECRET",
  "STRIPE_PRICE_ID",
  "BUBBLEPOD_PUBLIC_BASE_URL",
  "LAZYKH_PUBLIC_BASE_URL",
  "PUBLIC_BASE_URL",
  "NGROK_AUTHTOKEN",
  "NGROK_URL",
  "SMTP_HOST",
  "SMTP_USER",
  "SMTP_PASSWORD",
  "SMTP_PORT",
  "EMAIL_FROM",
  "EMAIL_FROM_NAME",
  "EMAIL_REPLY_TO",
  "EMAIL_ENABLED",
  "BUBBLEPOD_USER",
  "BUBBLEPOD_PASSWORD",
  "LAZYKH_USER",
  "LAZYKH_PASSWORD",
  "BUBBLEPOD_RESET_AUTH",
  "LAZYKH_RESET_AUTH",
];

/** Env for the Studio Python child: prefer bundled ffmpeg, then system PATH. */
function studioEnv() {
  const env = { ...process.env, PYTHONUNBUFFERED: "1" };
  // Desktop .exe / Electron: single-user local app — no login, tenants, or Stripe SaaS.
  env.BUBBLEPOD_DESKTOP = "1";
  env.BUBBLEPOD_USER_DATA = userDataRoot();
  // Packaged installer must not inherit API keys / ngrok / YouTube / Stripe from the host env.
  if (app.isPackaged) {
    for (const key of SECRET_ENV_KEYS) {
      delete env[key];
    }
  }
  try {
    fs.mkdirSync(env.BUBBLEPOD_USER_DATA, { recursive: true });
  } catch {
    /* best-effort; Python ensure_dirs will retry */
  }
  const dir = bundledFfmpegDir();
  const ffmpegExe = path.join(dir, ffmpegBinaryName("ffmpeg"));
  const ffprobeExe = path.join(dir, ffmpegBinaryName("ffprobe"));
  if (fs.existsSync(ffmpegExe)) {
    env.FFMPEG_BINARY = ffmpegExe;
    env.PATH = `${dir}${path.delimiter}${env.PATH || ""}`;
  }
  if (fs.existsSync(ffprobeExe)) {
    env.FFPROBE_BINARY = ffprobeExe;
  }
  return env;
}

function pythonCandidates() {
  const list = [];
  if (process.env.PYTHON) list.push(process.env.PYTHON);

  if (IS_WIN) {
    const local = process.env.LOCALAPPDATA;
    if (local) {
      for (const ver of ["Python311", "Python312", "Python313", "Python310"]) {
        list.push(path.join(local, "Programs", "Python", ver, "python.exe"));
      }
    }
    list.push("py", "python", "python3");
    return list;
  }

  // macOS / Linux: prefer python3; check common install locations first.
  const homes = [];
  if (process.env.HOME) homes.push(process.env.HOME);
  const macPaths = [
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
    "/usr/bin/python3",
    "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
    "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3",
    "/Library/Frameworks/Python.framework/Versions/3.10/bin/python3",
  ];
  for (const home of homes) {
    macPaths.push(
      path.join(home, "miniconda3", "bin", "python3"),
      path.join(home, "anaconda3", "bin", "python3"),
      path.join(home, ".pyenv", "shims", "python3")
    );
  }
  for (const p of macPaths) list.push(p);
  list.push("python3", "python");
  return list;
}

function resolvePython() {
  for (const cand of pythonCandidates()) {
    const isPath =
      cand.includes("/") || cand.includes("\\") || cand.endsWith(".exe");
    if (isPath) {
      if (fs.existsSync(cand)) return { cmd: cand, prefix: [] };
      continue;
    }
    // On Mac/Linux skip the Windows `py` launcher.
    if (cand === "py" && !IS_WIN) continue;
    const args = cand === "py" ? ["-3", "-c", "print(1)"] : ["-c", "print(1)"];
    const result = spawnSync(cand, args, { windowsHide: true, encoding: "utf8" });
    if (result.status === 0) {
      return { cmd: cand, prefix: cand === "py" ? ["-3"] : [] };
    }
  }
  return null;
}

function pingHealth() {
  return new Promise((resolve) => {
    const req = http.get(`${studioUrl()}/api/health`, { timeout: 1500 }, (res) => {
      resolve(res.statusCode === 200);
      res.resume();
    });
    req.on("error", () => resolve(false));
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function ensureStudio() {
  refreshStudioPort();
  if (await pingHealth()) {
    startedByUs = false;
    return;
  }
  const py = resolvePython();
  if (!py) {
    throw new Error(
      IS_MAC
        ? "Python 3 was not found. Install Python 3.11+ (Homebrew: brew install python@3.11), pip install -r requirements.txt, or set the PYTHON environment variable to python3."
        : "Python was not found. Install Python 3.11+ (and pip install -r requirements.txt), or set the PYTHON environment variable."
    );
  }
  const script = path.join(repoRoot(), "run_studio.py");
  if (!fs.existsSync(script)) {
    throw new Error(`Could not find run_studio.py in ${repoRoot()}`);
  }
  child = spawn(py.cmd, [...py.prefix, "run_studio.py"], {
    cwd: repoRoot(),
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
    env: studioEnv(),
  });
  startedByUs = true;
  const log = (buf) => {
    const text = buf.toString().trim();
    if (text) console.log("[studio]", text);
  };
  child.stdout.on("data", log);
  child.stderr.on("data", log);
  child.on("exit", (code) => {
    child = null;
    if (restarting) return;
    if (startedByUs && mainWindow && !mainWindow.isDestroyed() && code) {
      // MCP or browser restart may have recycled the API; reattach instead of alarming.
      refreshStudioPort();
      pingHealth().then((ok) => {
        if (ok) {
          startedByUs = false;
          reloadWindow();
          return;
        }
        dialog.showErrorBox("Bubble Pod", `Studio process exited (${code}).`);
      });
    }
  });
  const deadline = Date.now() + 50000;
  while (Date.now() < deadline) {
    refreshStudioPort();
    if (await pingHealth()) return;
    if (child && child.exitCode != null) {
      throw new Error(`Studio process exited before ${studioUrl()} became ready.`);
    }
    await new Promise((r) => setTimeout(r, 400));
  }
  throw new Error(`Studio did not start on ${studioUrl()}`);
}

function stopChild({ tree = true } = {}) {
  if (!startedByUs || !child) return;
  const proc = child;
  startedByUs = false;
  child = null;
  if (IS_WIN && proc.pid) {
    const args = ["/pid", String(proc.pid), "/F"];
    if (tree) args.push("/T");
    spawn("taskkill", args, {
      windowsHide: true,
      stdio: "ignore",
    });
    return;
  }
  if (proc.pid && tree) {
    try {
      // Kill the process group when possible (negative pid on Unix).
      process.kill(-proc.pid, "SIGTERM");
      return;
    } catch {
      /* fall through to single-process kill */
    }
  }
  try {
    proc.kill("SIGTERM");
  } catch {
    /* already gone */
  }
}

function pidOnPort(port) {
  if (IS_WIN) {
    const result = spawnSync("netstat", ["-ano", "-p", "tcp"], {
      windowsHide: true,
      encoding: "utf8",
    });
    const text = result.stdout || "";
    const needle = `:${port}`;
    for (const raw of text.split(/\r?\n/)) {
      if (!/LISTENING/i.test(raw) || !raw.includes(needle)) continue;
      const parts = raw.trim().split(/\s+/);
      const local = parts[1] || "";
      if (!local.endsWith(needle)) continue;
      const pid = Number(parts[parts.length - 1]);
      if (pid > 0) return pid;
    }
    return null;
  }

  // macOS / Linux: lsof
  const result = spawnSync(
    "lsof",
    ["-nP", `-iTCP:${port}`, "-sTCP:LISTEN", "-t"],
    { encoding: "utf8" }
  );
  const line = (result.stdout || "").trim().split(/\r?\n/)[0];
  const pid = Number(line);
  return pid > 0 ? pid : null;
}

function killListener(port) {
  if (port === 8765 || port === 8766) return;
  const pid = pidOnPort(port);
  if (!pid || pid === process.pid) return;
  if (IS_WIN) {
    spawnSync("taskkill", ["/PID", String(pid), "/F"], {
      windowsHide: true,
      stdio: "ignore",
    });
    return;
  }
  try {
    process.kill(pid, "SIGTERM");
  } catch {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      /* already gone */
    }
  }
}

function reloadWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.loadURL(studioUrl());
  }
}

async function waitForHealth(timeoutMs = 50000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    refreshStudioPort();
    if (await pingHealth()) return true;
    await new Promise((r) => setTimeout(r, 350));
  }
  return false;
}

async function restartStudio() {
  restarting = true;
  try {
    const oldPort = studioPort;
    const nextPort = readStudioPort();
    if (startedByUs && child) {
      stopChild({ tree: false });
      await new Promise((r) => setTimeout(r, 400));
    } else {
      killListener(oldPort);
      await new Promise((r) => setTimeout(r, 400));
    }
    studioPort = nextPort;
    if (nextPort !== oldPort) {
      killListener(nextPort);
      await new Promise((r) => setTimeout(r, 200));
    }
    await ensureStudio();
    const ok = await waitForHealth();
    if (!ok) {
      throw new Error("Studio did not return /api/health 200 after restart.");
    }
    reloadWindow();
    return { ok: true, health: 200, port: studioPort, url: studioUrl() };
  } finally {
    restarting = false;
  }
}

function startHealthWatch() {
  if (healthWatch) return;
    healthWatch = setInterval(async () => {
    if (restarting) return;
    const ok = await pingHealth();
    if (!ok) {
      wasOffline = true;
      return;
    }
    if (wasOffline && mainWindow && !mainWindow.isDestroyed()) {
      wasOffline = false;
      reloadWindow();
    }
  }, 5000);
}

function createWindow() {
  const icon = resolveIcon();
  refreshStudioPort();
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 960,
    minHeight: 640,
    title: "Bubble Pod",
    autoHideMenuBar: true,
    ...(icon ? { icon } : {}),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      nodeIntegration: false,
      contextIsolation: true,
    },
  });
  mainWindow.loadURL(studioUrl());
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url && /^https?:\/\//i.test(url)) {
      shell.openExternal(url);
    }
    return { action: "deny" };
  });
  const openExternal = (event, url) => {
    if (url.startsWith(studioUrl())) return;
    if (/^https?:\/\//i.test(url)) {
      event.preventDefault();
      shell.openExternal(url);
    }
  };
  mainWindow.webContents.on("will-navigate", openExternal);
  mainWindow.webContents.on("will-redirect", openExternal);
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

app.whenReady().then(async () => {
  try {
    await ensureStudio();
    createWindow();
    startHealthWatch();
  } catch (err) {
    dialog.showErrorBox("Bubble Pod", String(err && err.message ? err.message : err));
    app.quit();
  }
});

ipcMain.handle("studio-restart", async () => {
  try {
    return await restartStudio();
  } catch (err) {
    return { ok: false, error: String(err && err.message ? err.message : err) };
  }
});

app.on("window-all-closed", () => {
  stopChild({ tree: true });
  app.quit();
});

app.on("before-quit", () => stopChild({ tree: true }));

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0 && !mainWindow) {
    createWindow();
  }
});
