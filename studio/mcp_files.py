"""Safe MCP file retrieval for ChatGPT/Claude (artifacts only, no secrets)."""

from __future__ import annotations

import base64
import mimetypes
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from studio.aspect import ASPECT_16_9, ASPECT_9_16, normalize_aspect
from studio.paths import (
    BACKGROUNDS_DIR,
    MUSIC_DIR,
    PROJECTS_DIR,
    REPO_ROOT,
    USER_BACKGROUNDS_DIR,
    USER_DATA,
    ensure_dirs,
)
from studio.projects import (
    cover_path,
    final_video_path,
    input_prefix,
    last_video_path,
    load_meta,
    project_dir,
    shorts_input_prefix,
)

# ChatGPT MCP often embeds base64; keep payloads bounded.
MAX_BASE64_BYTES = 15 * 1024 * 1024
DOWNLOAD_TOKEN_TTL_SEC = 10 * 60

_TOKEN_LOCK = threading.Lock()
_DOWNLOAD_TOKENS: dict[str, dict[str, Any]] = {}

_REFUSED_NAMES = frozenset(
    {
        "auth.json",
        "settings.json",
        "youtube_token.json",
        "youtube_accounts.json",
        ".env",
        ".env.local",
        "credentials.json",
        "client_secret.json",
        "service_account.json",
    }
)
_REFUSED_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".crt", ".p8", ".env"})
_SECRET_NAME_RE = re.compile(
    r"(^|\.)(secret|credential|password|passwd|api[_-]?key|private[_-]?key|jwt|token)(\.|$)",
    re.I,
)
# Only these filenames are allowed when resolving under user_data/ root (not subdirs).
_SAFE_USER_DATA_FILES = frozenset(
    {
        "audit.log",
        "prompts.json",
        "topics.json",
        "deleted_ids.json",
        "gentle_local.log",
        "ngrok.log",
        "spend_counters.json",
        "library.json",  # only if somehow at root; music/library.json preferred via path
    }
)

_KIND_ALIASES = {
    "mp4": "video",
    "final": "video",
    "final_video": "video",
    "wav": "audio",
    "speech": "audio",
    "tts": "audio",
    "script": "script_tagged",
    "tagged": "script_tagged",
    "raw": "script_raw",
    "image": "illustration",
    "billboard": "illustration",
    "png": "illustration",
    "track": "music",
    "song": "music",
    "bg": "background",
    "audit": "log",
    "audit_log": "log",
}


def _purge_expired_tokens(now: float | None = None) -> None:
    ts = now if now is not None else time.time()
    dead = [k for k, v in _DOWNLOAD_TOKENS.items() if float(v.get("expires_at") or 0) <= ts]
    for k in dead:
        _DOWNLOAD_TOKENS.pop(k, None)


def mint_download_token(path: Path, *, ttl_sec: int = DOWNLOAD_TOKEN_TTL_SEC) -> dict[str, Any]:
    """Issue a short-lived token that maps to an already-authorized absolute path."""
    ensure_dirs()
    resolved = path.resolve()
    token = secrets.token_urlsafe(24)
    expires = time.time() + max(30, int(ttl_sec))
    with _TOKEN_LOCK:
        _purge_expired_tokens()
        _DOWNLOAD_TOKENS[token] = {
            "path": str(resolved),
            "expires_at": expires,
            "name": resolved.name,
        }
    return {"token": token, "expires_at": expires, "expires_in_sec": int(expires - time.time())}


def resolve_download_token(token: str) -> Path:
    raw = (token or "").strip()
    if not raw or len(raw) > 128:
        raise FileNotFoundError("Invalid or expired download token.")
    with _TOKEN_LOCK:
        _purge_expired_tokens()
        entry = _DOWNLOAD_TOKENS.get(raw)
        if not entry:
            raise FileNotFoundError("Invalid or expired download token.")
        path = Path(str(entry["path"]))
    # Re-check containment in case roots moved / token reused after delete.
    _assert_allowed_file(path)
    if not path.is_file():
        raise FileNotFoundError(f"File gone: {path.name}")
    return path


def _allowed_roots() -> list[Path]:
    ensure_dirs()
    roots = [
        PROJECTS_DIR.resolve(),
        MUSIC_DIR.resolve(),
        USER_BACKGROUNDS_DIR.resolve(),
        BACKGROUNDS_DIR.resolve(),
        USER_DATA.resolve(),
    ]
    # Bundled music/ at repo root (read-only seed); safe and useful.
    bundled = (REPO_ROOT / "music").resolve()
    if bundled.is_dir():
        roots.append(bundled)
    return roots


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_refused_name(name: str) -> bool:
    base = Path(name or "").name
    if not base:
        return True
    lower = base.lower()
    if lower in _REFUSED_NAMES:
        return True
    if Path(lower).suffix in _REFUSED_SUFFIXES:
        return True
    if _SECRET_NAME_RE.search(lower):
        return True
    return False


def _assert_allowed_file(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {resolved.name}")
    if not resolved.is_file():
        raise RuntimeError(f"Not a file: {resolved.name}")
    if _is_refused_name(resolved.name):
        raise RuntimeError(f"Refused: secret or credential file ({resolved.name}).")

    roots = _allowed_roots()
    if not any(_is_under(resolved, root) for root in roots):
        raise RuntimeError("Path escapes allowed Studio roots (projects/music/user_data/backgrounds).")

    # user_data root: only known safe artifact/log names (not arbitrary JSON next to auth).
    try:
        rel = resolved.relative_to(USER_DATA.resolve())
    except ValueError:
        rel = None
    if rel is not None and len(rel.parts) == 1:
        if resolved.name.lower() not in {n.lower() for n in _SAFE_USER_DATA_FILES}:
            raise RuntimeError(
                f"Refused user_data root file '{resolved.name}'. "
                "Use project_id+kind, projects/…, music/…, or allowlisted logs (audit.log, prompts.json, …)."
            )
    return resolved


def _safe_rel_path(raw: str) -> Path:
    """Resolve a relative path under allowed roots. Rejects abs paths and .. escapes."""
    text = (raw or "").strip().replace("\\", "/")
    if not text:
        raise RuntimeError("path is empty.")
    if Path(text).is_absolute() or re.match(r"^[A-Za-z]:/", text):
        raise RuntimeError("Absolute paths are not allowed; use a relative path under projects/, music/, or user_data/.")
    parts = [p for p in text.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise RuntimeError("Path must not contain '..'.")
    if not parts:
        raise RuntimeError("path is empty.")

    # Normalize common prefixes.
    head = parts[0].lower()
    if head in {"user_data", "userdata"}:
        candidate = USER_DATA.joinpath(*parts[1:]) if len(parts) > 1 else USER_DATA
    elif head == "projects":
        candidate = PROJECTS_DIR.joinpath(*parts[1:]) if len(parts) > 1 else PROJECTS_DIR
    elif head == "music":
        # Prefer writable library; fall back to bundled if missing.
        under_user = MUSIC_DIR.joinpath(*parts[1:]) if len(parts) > 1 else MUSIC_DIR
        if under_user.is_file() or under_user.is_dir():
            candidate = under_user
        else:
            bundled = REPO_ROOT / "music"
            candidate = bundled.joinpath(*parts[1:]) if len(parts) > 1 else bundled
    elif head in {"backgrounds", "background"}:
        name_parts = parts[1:]
        user_cand = USER_BACKGROUNDS_DIR.joinpath(*name_parts) if name_parts else USER_BACKGROUNDS_DIR
        repo_cand = BACKGROUNDS_DIR.joinpath(*name_parts) if name_parts else BACKGROUNDS_DIR
        if user_cand.is_file():
            candidate = user_cand
        elif repo_cand.is_file():
            candidate = repo_cand
        else:
            candidate = user_cand
    else:
        # Bare relative → treat as under projects/
        candidate = PROJECTS_DIR.joinpath(*parts)

    return _assert_allowed_file(candidate)


def _normalize_kind(kind: str) -> str:
    raw = (kind or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _KIND_ALIASES.get(raw, raw)


def _guess_media_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    if guessed:
        return guessed
    ext = path.suffix.lower()
    return {
        ".mp4": "video/mp4",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".json": "application/json",
        ".txt": "text/plain",
        ".csv": "text/csv",
        ".log": "text/plain",
    }.get(ext, "application/octet-stream")


def _resolve_kind_path(
    project_id: str,
    kind: str,
    *,
    filename: str = "",
    index: int | None = None,
    aspect: str = "",
    music_id: str = "",
) -> Path:
    kind_n = _normalize_kind(kind)
    if kind_n in {"log", "audit.log"}:
        path = USER_DATA / "audit.log"
        return _assert_allowed_file(path)

    if kind_n in {"music", "track"}:
        from studio.music import library_payload

        lib = library_payload()
        tracks = lib.get("tracks") or []
        mid = (music_id or filename or "").strip()
        if not mid:
            raise RuntimeError("For kind=music pass filename or music_id.")
        for track in tracks:
            if str(track.get("id")) == mid or str(track.get("filename")) == mid or str(track.get("name")) == mid:
                path = Path(track.get("path") or (MUSIC_DIR / Path(str(track.get("filename") or "")).name))
                return _assert_allowed_file(path)
        # Direct filename under music/
        path = MUSIC_DIR / Path(mid).name
        if path.is_file():
            return _assert_allowed_file(path)
        raise FileNotFoundError(f"Music track not found: {mid}")

    if kind_n in {"background", "bg"}:
        name = Path(filename or "").name
        if not name:
            raise RuntimeError("For kind=background pass filename.")
        from studio.backgrounds import resolve_background

        item = resolve_background(name)
        if isinstance(item, dict) and item.get("path"):
            return _assert_allowed_file(Path(item["path"]))
        for cand in (USER_BACKGROUNDS_DIR / name, BACKGROUNDS_DIR / name):
            if cand.is_file():
                return _assert_allowed_file(cand)
        raise FileNotFoundError(f"Background not found: {name}")

    pid = (project_id or "").strip()
    if not pid:
        raise RuntimeError("project_id is required for this kind (or pass path=).")

    folder = project_dir(pid)
    if not folder.is_dir():
        raise FileNotFoundError(f"Unknown project: {pid}")

    asp = normalize_aspect(aspect) if aspect else None
    prefix = Path(input_prefix(pid))
    shorts = Path(shorts_input_prefix(pid))

    if kind_n in {"video", "video_16x9", "video_9x16"}:
        if kind_n == "video_16x9" or (asp == ASPECT_16_9):
            path = final_video_path(pid, ASPECT_16_9)
        elif kind_n == "video_9x16" or (asp == ASPECT_9_16):
            path = final_video_path(pid, ASPECT_9_16)
        else:
            path = last_video_path(pid)
            if not path.is_file():
                try:
                    meta = load_meta(pid)
                    last = meta.get("last_render_aspect")
                    if last:
                        path = final_video_path(pid, normalize_aspect(last))
                except FileNotFoundError:
                    pass
            if not path.is_file():
                for try_asp in (ASPECT_16_9, ASPECT_9_16):
                    cand = final_video_path(pid, try_asp)
                    if cand.is_file():
                        path = cand
                        break
        return _assert_allowed_file(path)

    if kind_n in {"audio", "audio_9x16", "shorts_audio"}:
        if kind_n in {"audio_9x16", "shorts_audio"} or asp == ASPECT_9_16:
            path = shorts.with_suffix(".wav")
        else:
            path = prefix.with_suffix(".wav")
        return _assert_allowed_file(path)

    if kind_n in {"cover", "cover_16x9", "cover_9x16"}:
        if kind_n == "cover_16x9" or asp == ASPECT_16_9:
            path = cover_path(pid, ASPECT_16_9)
        elif kind_n == "cover_9x16" or asp == ASPECT_9_16:
            path = cover_path(pid, ASPECT_9_16)
        else:
            path = cover_path(pid, asp)
        return _assert_allowed_file(path)

    if kind_n in {"script_tagged", "script"}:
        return _assert_allowed_file(prefix.with_suffix(".txt"))
    if kind_n in {"script_raw"}:
        return _assert_allowed_file(prefix.with_name(prefix.name + "_raw.txt"))
    if kind_n in {"script_9x16", "script_tagged_9x16", "shorts_script"}:
        return _assert_allowed_file(shorts.with_suffix(".txt"))
    if kind_n in {"script_9x16_raw", "shorts_raw"}:
        return _assert_allowed_file(shorts.with_name(shorts.name + "_raw.txt"))

    if kind_n in {"alignment", "gentle", "script_json"}:
        return _assert_allowed_file(prefix.with_suffix(".json"))
    if kind_n in {"alignment_9x16", "gentle_9x16"}:
        return _assert_allowed_file(shorts.with_suffix(".json"))

    if kind_n in {"lines", "lines_json"}:
        return _assert_allowed_file(folder / "lines.json")
    if kind_n in {"meta", "meta_json"}:
        return _assert_allowed_file(folder / "meta.json")

    if kind_n in {"thumbnail", "thumb"}:
        from studio.thumbs import ensure_thumbnail

        return _assert_allowed_file(ensure_thumbnail(pid))

    if kind_n in {"illustration", "image", "billboard"}:
        from studio.illustrations import illustration_jobs

        jobs = illustration_jobs(pid).get("jobs") or []
        name = Path(filename or "").name
        if name:
            for job in jobs:
                if job.get("filename") == name or job.get("filename") == name + ".png":
                    path = Path(job.get("path") or "") if job.get("path") else folder / job["filename"]
                    # Prefer explicit path from job; else search billboard dirs + project root.
                    if not path.is_file():
                        path = folder / Path(job["filename"]).name
                    if not path.is_file():
                        from studio.projects import billboards_dir

                        for try_asp in (None, ASPECT_16_9, ASPECT_9_16):
                            cand = billboards_dir(pid, try_asp) / Path(job["filename"]).name
                            if cand.is_file():
                                path = cand
                                break
                    return _assert_allowed_file(path)
            # Direct under project folder / billboards
            direct = folder / name
            if direct.is_file():
                return _assert_allowed_file(direct)
            from studio.projects import billboards_dir

            for try_asp in (None, ASPECT_16_9, ASPECT_9_16):
                cand = billboards_dir(pid, try_asp) / name
                if cand.is_file():
                    return _assert_allowed_file(cand)
            raise FileNotFoundError(f"Illustration not found: {name}")

        if index is None:
            raise RuntimeError("For kind=illustration pass filename= or index= (from list_illustration_jobs).")
        idx = int(index)
        # Match regenerate_illustration semantics: -1 current cover, -2 9:16 cover, else line index.
        if idx == -1:
            return _assert_allowed_file(cover_path(pid))
        if idx == -2:
            return _assert_allowed_file(cover_path(pid, ASPECT_9_16))
        line_jobs = [j for j in jobs if j.get("role") != "cover"]
        if idx < 0 or idx >= len(line_jobs):
            raise FileNotFoundError(f"Illustration index out of range: {idx}")
        job = line_jobs[idx]
        name = Path(str(job.get("filename") or "")).name
        from studio.projects import billboards_dir

        for try_asp in (None, ASPECT_16_9, ASPECT_9_16):
            cand = billboards_dir(pid, try_asp) / name
            if cand.is_file():
                return _assert_allowed_file(cand)
        cand = folder / name
        return _assert_allowed_file(cand)

    raise RuntimeError(
        f"Unknown kind '{kind}'. Use video, audio, cover, script_tagged, script_raw, "
        "script_9x16, illustration, lines, meta, alignment, music, background, log, thumbnail "
        "(or pass a relative path=)."
    )


def _studio_base_url() -> str:
    from studio.settings import resolve_public_base_url

    # Prefer configured PUBLIC_BASE_URL; fall back to live ngrok, then local host:port.
    base = resolve_public_base_url()
    try:
        from studio.settings import load_settings, local_base_url

        settings = load_settings()
        configured = (settings.get("public_base_url") or "").strip()
        if configured:
            return base
        from studio.ngrok_tunnel import ngrok_status

        status = ngrok_status(reveal_password=False)
        public = str(status.get("public_url") or "").rstrip("/")
        if public and status.get("running"):
            return public
        return local_base_url(settings)
    except Exception:
        return base


def _relative_display(path: Path) -> str:
    resolved = path.resolve()
    for root, label in (
        (PROJECTS_DIR.resolve(), "projects"),
        (MUSIC_DIR.resolve(), "music"),
        (USER_BACKGROUNDS_DIR.resolve(), "user_data/backgrounds"),
        (BACKGROUNDS_DIR.resolve(), "backgrounds"),
        (USER_DATA.resolve(), "user_data"),
        ((REPO_ROOT / "music").resolve(), "music"),
    ):
        try:
            return f"{label}/{resolved.relative_to(root).as_posix()}"
        except ValueError:
            continue
    return resolved.name


def get_file_payload(
    *,
    project_id: str = "",
    kind: str = "",
    path: str = "",
    filename: str = "",
    index: int | None = None,
    aspect: str = "",
    music_id: str = "",
    mode: str = "auto",
    max_base64_bytes: int = MAX_BASE64_BYTES,
) -> dict[str, Any]:
    """Resolve an artifact and return text/base64 and/or a short-lived download URL.

    mode: auto | base64 | url
      - auto: inline base64 (and text for utf-8) when size <= max_base64_bytes; else URL
      - base64: require size <= limit
      - url: always return download_url (JWT or MCP PIN not required — token is the auth)
    """
    ensure_dirs()
    rel = (path or "").strip()
    kind_n = _normalize_kind(kind)
    if rel:
        file_path = _safe_rel_path(rel)
    elif kind_n:
        file_path = _resolve_kind_path(
            project_id,
            kind_n,
            filename=filename,
            index=index,
            aspect=aspect,
            music_id=music_id,
        )
    else:
        raise RuntimeError(
            "Pass path= (relative under projects/, music/, user_data/, backgrounds/) "
            "or project_id + kind= (video|audio|cover|script_tagged|illustration|…)."
        )

    size = file_path.stat().st_size
    media_type = _guess_media_type(file_path)
    display = _relative_display(file_path)
    mode_n = (mode or "auto").strip().lower()
    if mode_n not in {"auto", "base64", "url"}:
        raise RuntimeError("mode must be auto, base64, or url.")

    limit = max(1, min(int(max_base64_bytes or MAX_BASE64_BYTES), MAX_BASE64_BYTES))
    want_inline = mode_n == "base64" or (mode_n == "auto" and size <= limit)
    if mode_n == "base64" and size > limit:
        raise RuntimeError(
            f"File is {size} bytes; max base64 is {limit}. "
            "Call again with mode='url' (or omit mode for auto→URL)."
        )

    out: dict[str, Any] = {
        "ok": True,
        "path": display,
        "name": file_path.name,
        "size": size,
        "media_type": media_type,
        "max_base64_bytes": MAX_BASE64_BYTES,
        "mode": mode_n,
        "project_id": (project_id or "").strip() or None,
        "kind": kind_n or None,
    }

    if want_inline:
        data = file_path.read_bytes()
        out["encoding"] = "base64"
        out["content_base64"] = base64.b64encode(data).decode("ascii")
        if media_type.startswith("text/") or file_path.suffix.lower() in {
            ".txt",
            ".json",
            ".csv",
            ".log",
            ".md",
            ".xml",
            ".svg",
        }:
            try:
                out["text"] = data.decode("utf-8")
            except UnicodeDecodeError:
                pass
        out["delivery"] = "inline"
        return out

    token_info = mint_download_token(file_path)
    base = _studio_base_url().rstrip("/")
    url = f"{base}/api/mcp/download/{token_info['token']}"
    out["delivery"] = "url"
    out["download_url"] = url
    out["download_token"] = token_info["token"]
    out["expires_in_sec"] = token_info["expires_in_sec"]
    out["note"] = (
        "Short-lived URL (no JWT/PIN required — the token is the credential). "
        f"Fetch within {token_info['expires_in_sec']}s. For ChatGPT MCP, prefer mode=auto "
        f"(inline base64 up to {MAX_BASE64_BYTES // (1024 * 1024)}MB)."
    )
    return out
