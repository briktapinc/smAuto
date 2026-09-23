/**
 * Fail the Windows build if packaged resources contain secrets or machine URLs
 * that must never ship in the installer (API keys, ngrok hosts, YouTube tokens).
 */
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..", "dist", "win-unpacked", "resources", "lazykh");

const FORBIDDEN_NAMES = new Set([
  "settings.json",
  "auth.json",
  "members.json",
  "youtube_token.json",
  "ngrok.pid",
  "ngrok_traffic_policy.json",
  "gpu.lock",
  "audit.log",
  ".env",
  ".env.local",
]);

const FORBIDDEN_NAME_RE = /^\.env(\..+)?$/i;

/** Real secret / tunnel patterns — not UI placeholders like "your-subdomain". */
const FORBIDDEN_CONTENT = [
  /\bsk-proj-[A-Za-z0-9_-]{20,}/,
  /\bsk-live-[A-Za-z0-9_-]{10,}/,
  /\bsk_live_[A-Za-z0-9]{20,}/,
  /\bsk_test_[A-Za-z0-9]{20,}/,
  /\bGOCSPX-[A-Za-z0-9_-]+/,
  /\bAIza[0-9A-Za-z_-]{20,}/,
  /\bwhsec_[A-Za-z0-9]{16,}/,
  /https?:\/\/(?!your-subdomain\.)[a-z0-9-]+\.ngrok-free\.(app|dev)\b/i,
  /https?:\/\/(?!your-subdomain\.)[a-z0-9-]+\.ngrok\.io\b/i,
];

const SKIP_DIR_NAMES = new Set(["__pycache__", ".git", "node_modules"]);
const SKIP_FILE_RE = /(^|[/\\])(smoke_|test_|.*_test\.|.*\.test\.)/i;

function walk(dir, out = []) {
  if (!fs.existsSync(dir)) return out;
  for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
    if (ent.name === "." || ent.name === "..") continue;
    const full = path.join(dir, ent.name);
    if (ent.isDirectory()) {
      if (SKIP_DIR_NAMES.has(ent.name)) continue;
      walk(full, out);
    } else if (ent.isFile()) {
      out.push(full);
    }
  }
  return out;
}

function main() {
  if (!fs.existsSync(ROOT)) {
    console.error(`[assert-clean-package] Missing package tree: ${ROOT}`);
    process.exit(1);
  }

  const bad = [];
  const files = walk(ROOT);

  for (const file of files) {
    const base = path.basename(file);
    const rel = path.relative(ROOT, file);
    if (FORBIDDEN_NAMES.has(base) || FORBIDDEN_NAME_RE.test(base)) {
      bad.push(`forbidden file: ${rel}`);
      continue;
    }
    // Only scan text-ish sources (avoid binary false positives).
    if (!/\.(py|js|json|html|css|md|txt|yml|yaml|toml|env)$/i.test(base)) continue;
    if (SKIP_FILE_RE.test(rel)) continue;
    let text;
    try {
      text = fs.readFileSync(file, "utf8");
    } catch {
      continue;
    }
    for (const re of FORBIDDEN_CONTENT) {
      if (re.test(text)) {
        bad.push(`secret/url pattern ${re} in ${rel}`);
        break;
      }
    }
  }

  // user_data must never appear under packaged lazykh
  const ud = path.join(ROOT, "user_data");
  if (fs.existsSync(ud)) {
    bad.push("user_data/ directory is present in packaged resources");
  }

  if (bad.length) {
    console.error("[assert-clean-package] Installer must not ship secrets / ngrok / YouTube tokens:");
    for (const line of bad) console.error(`  - ${line}`);
    process.exit(1);
  }
  console.log(`[assert-clean-package] OK — ${files.length} files under resources/lazykh`);
}

main();
