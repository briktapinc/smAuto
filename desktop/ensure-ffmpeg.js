/**
 * Ensure platform ffmpeg/ffprobe exist under desktop/bin/ffmpeg/
 * before electron-builder packs extraResources.
 *
 * Windows: https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
 * macOS:   evermeet.cx (Intel) or Homebrew copy; arm64 prefers Homebrew / PATH
 */
const fs = require("fs");
const path = require("path");
const https = require("https");
const http = require("http");
const os = require("os");
const { execFileSync, spawnSync } = require("child_process");

const DEST = path.join(__dirname, "bin", "ffmpeg");
const IS_WIN = process.platform === "win32";
const IS_MAC = process.platform === "darwin";
const EXE = (name) => (IS_WIN ? `${name}.exe` : name);
const FFMPEG = path.join(DEST, EXE("ffmpeg"));
const FFPROBE = path.join(DEST, EXE("ffprobe"));
const ZIP_URL_WIN = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip";
const EVERMEET_FFMPEG = "https://evermeet.cx/ffmpeg/getrelease/zip";
const EVERMEET_FFPROBE = "https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip";

function existsOk(p) {
  try {
    return fs.existsSync(p) && fs.statSync(p).size > 500_000;
  } catch {
    return false;
  }
}

function download(url, dest) {
  return new Promise((resolve, reject) => {
    const file = fs.createWriteStream(dest);
    const get = url.startsWith("https") ? https.get : http.get;
    const req = get(url, { headers: { "User-Agent": "BubblePod-ensure-ffmpeg" } }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        file.close();
        try {
          fs.unlinkSync(dest);
        } catch {
          /* ignore */
        }
        download(res.headers.location, dest).then(resolve, reject);
        return;
      }
      if (res.statusCode !== 200) {
        reject(new Error(`Download failed: HTTP ${res.statusCode} for ${url}`));
        return;
      }
      res.pipe(file);
      file.on("finish", () => file.close(() => resolve()));
    });
    req.on("error", (err) => {
      try {
        file.close();
        fs.unlinkSync(dest);
      } catch {
        /* ignore */
      }
      reject(err);
    });
  });
}

function which(cmd) {
  const probe = IS_WIN ? "where" : "which";
  const result = spawnSync(probe, [cmd], { encoding: "utf8", windowsHide: true });
  if (result.status !== 0) return null;
  const line = (result.stdout || "").trim().split(/\r?\n/)[0];
  return line && fs.existsSync(line) ? line : null;
}

function copyFromSystem() {
  const ffmpegSys = which("ffmpeg");
  const ffprobeSys = which("ffprobe");
  const brewFfmpeg = [
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
  ].find((p) => fs.existsSync(p));
  const brewFfprobe = [
    "/opt/homebrew/bin/ffprobe",
    "/usr/local/bin/ffprobe",
  ].find((p) => fs.existsSync(p));
  const srcF = brewFfmpeg || ffmpegSys;
  const srcP = brewFfprobe || ffprobeSys;
  if (!srcF || !srcP) return false;
  fs.mkdirSync(DEST, { recursive: true });
  fs.copyFileSync(srcF, FFMPEG);
  fs.copyFileSync(srcP, FFPROBE);
  try {
    fs.chmodSync(FFMPEG, 0o755);
    fs.chmodSync(FFPROBE, 0o755);
  } catch {
    /* ignore */
  }
  console.log("[ensure-ffmpeg] Copied system ffmpeg to", DEST);
  return true;
}

function extractZip(zipPath, extractDir) {
  if (IS_WIN) {
    try {
      execFileSync(
        "powershell.exe",
        [
          "-NoProfile",
          "-Command",
          `Expand-Archive -LiteralPath '${zipPath.replace(/'/g, "''")}' -DestinationPath '${extractDir.replace(/'/g, "''")}' -Force`,
        ],
        { stdio: "inherit" }
      );
      return;
    } catch {
      /* fall through to tar */
    }
  }
  execFileSync("tar", ["-xf", zipPath, "-C", extractDir], { stdio: "inherit" });
}

function findFile(root, names) {
  const want = new Set(names.map((n) => n.toLowerCase()));
  const stack = [root];
  while (stack.length) {
    const dir = stack.pop();
    for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, ent.name);
      if (ent.isDirectory()) stack.push(full);
      else if (want.has(ent.name.toLowerCase())) return full;
    }
  }
  return null;
}

async function ensureWindows() {
  fs.mkdirSync(DEST, { recursive: true });
  const zipPath = path.join(os.tmpdir(), "bubblepod-ffmpeg-essentials.zip");
  const extractDir = path.join(os.tmpdir(), "bubblepod-ffmpeg-extract");
  console.log("[ensure-ffmpeg] Downloading", ZIP_URL_WIN);
  await download(ZIP_URL_WIN, zipPath);
  if (fs.existsSync(extractDir)) {
    fs.rmSync(extractDir, { recursive: true, force: true });
  }
  fs.mkdirSync(extractDir, { recursive: true });
  extractZip(zipPath, extractDir);
  const ffmpegSrc = findFile(extractDir, ["ffmpeg.exe"]);
  const ffprobeSrc = findFile(extractDir, ["ffprobe.exe"]);
  if (!ffmpegSrc || !ffprobeSrc) {
    throw new Error("Zip extracted but ffmpeg.exe / ffprobe.exe were not found");
  }
  fs.copyFileSync(ffmpegSrc, FFMPEG);
  fs.copyFileSync(ffprobeSrc, FFPROBE);
  console.log("[ensure-ffmpeg] Installed to", DEST);
}

async function downloadEvermeet(url, destName) {
  const zipPath = path.join(os.tmpdir(), `bubblepod-${destName}.zip`);
  const extractDir = path.join(os.tmpdir(), `bubblepod-${destName}-extract`);
  console.log("[ensure-ffmpeg] Downloading", url);
  await download(url, zipPath);
  if (fs.existsSync(extractDir)) {
    fs.rmSync(extractDir, { recursive: true, force: true });
  }
  fs.mkdirSync(extractDir, { recursive: true });
  extractZip(zipPath, extractDir);
  const found = findFile(extractDir, [destName, `${destName}.exe`]);
  if (!found) {
    throw new Error(`evermeet zip did not contain ${destName}`);
  }
  return found;
}

async function ensureDarwin() {
  // Prefer existing Homebrew / PATH binaries (works for Intel and Apple Silicon).
  if (copyFromSystem()) return;

  if (process.arch === "arm64") {
    throw new Error(
      "ffmpeg not found for Apple Silicon. Install with: brew install ffmpeg\n" +
        "Then re-run: npm run ensure-ffmpeg"
    );
  }

  // Intel Mac: evermeet.cx static builds
  fs.mkdirSync(DEST, { recursive: true });
  const ffmpegSrc = await downloadEvermeet(EVERMEET_FFMPEG, "ffmpeg");
  const ffprobeSrc = await downloadEvermeet(EVERMEET_FFPROBE, "ffprobe");
  fs.copyFileSync(ffmpegSrc, FFMPEG);
  fs.copyFileSync(ffprobeSrc, FFPROBE);
  try {
    fs.chmodSync(FFMPEG, 0o755);
    fs.chmodSync(FFPROBE, 0o755);
  } catch {
    /* ignore */
  }
  console.log("[ensure-ffmpeg] Installed to", DEST);
}

async function main() {
  if (existsOk(FFMPEG) && existsOk(FFPROBE)) {
    console.log("[ensure-ffmpeg] Already present:", DEST);
    return;
  }

  if (IS_WIN) {
    await ensureWindows();
    return;
  }

  if (IS_MAC) {
    await ensureDarwin();
    return;
  }

  // Linux and others: copy from PATH if available; otherwise skip with a clear note
  // so Windows/Linux Docker users can still develop without bundling.
  if (copyFromSystem()) return;
  console.warn(
    "[ensure-ffmpeg] No bundled download for",
    process.platform,
    "- install ffmpeg on PATH or skip bundling. Continuing."
  );
}

main().catch((err) => {
  console.error("[ensure-ffmpeg]", err && err.message ? err.message : err);
  process.exit(1);
});
