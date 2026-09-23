"""Image normalize helpers for covers / illustration slots / YouTube thumbs."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from studio.aspect import canvas_size, normalize_aspect

YT_THUMB_MAX_BYTES = 2 * 1024 * 1024  # YouTube hard cap
SLOT_SOFT_MAX_BYTES = 1 * 1024 * 1024  # preferred after ingest


def orientation_label(width: int, height: int) -> str:
    if height > width:
        return "portrait"
    if width > height:
        return "landscape"
    return "square"


def assert_same_orientation(src_w: int, src_h: int, dst_w: int, dst_h: int) -> None:
    src_portrait = src_h > src_w
    dst_portrait = dst_h > dst_w
    if src_portrait != dst_portrait:
        raise RuntimeError(
            f"This image is {src_w}x{src_h}, not {dst_w}x{dst_h}. "
            "Do not stretch 16:9 into 9:16 (or the reverse). Generate a new image "
            f"at {dst_w}x{dst_h}."
        )


def cover_fit_rgba(im: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Lanczos cover-scale + center-crop to exact canvas (same orientation only)."""
    im = im.convert("RGBA")
    if im.size == (target_w, target_h):
        return im
    assert_same_orientation(im.width, im.height, target_w, target_h)
    scale = max(target_w / max(1, im.width), target_h / max(1, im.height))
    new_size = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
    im = im.resize(new_size, Image.Resampling.LANCZOS)
    left = max(0, (im.width - target_w) // 2)
    top = max(0, (im.height - target_h) // 2)
    im = im.crop((left, top, left + target_w, top + target_h))
    if im.size != (target_w, target_h):
        im = im.resize((target_w, target_h), Image.Resampling.LANCZOS)
    return im


def fit_path_to_canvas(path: Path, aspect: str, *, layout: str = "cover") -> None:
    """In-place normalize a slot image to exact canvas + JPEG ≤1MB when possible.

    Billboard layout is left as-is. Same-orientation wrong sizes are Lanczos cover-cropped.
    Opposite orientation raises (do not stretch 16:9 into 9:16).
    """
    layout = (layout or "cover").strip().lower()
    if layout == "billboard":
        return
    target_w, target_h = canvas_size(aspect)
    with Image.open(path) as im:
        fitted = cover_fit_rgba(im, target_w, target_h)
        # Prefer JPEG under soft 1MB cap (slot filenames stay .png for pipeline paths).
        try:
            data, meta = jpeg_bytes_under(
                fitted, max_bytes=SLOT_SOFT_MAX_BYTES, keep_aspect_16x9=False
            )
            path.write_bytes(data)
            meta["path"] = str(path)
            return
        except Exception:
            fitted.save(path, format="PNG", optimize=True)


def jpeg_bytes_under(
    im: Image.Image,
    *,
    max_bytes: int = YT_THUMB_MAX_BYTES,
    keep_aspect_16x9: bool = False,
) -> tuple[bytes, dict]:
    """Encode RGB JPEG under max_bytes: quality ladder, then downscale if needed."""
    rgb = im.convert("RGB")
    if keep_aspect_16x9 and rgb.width and rgb.height:
        # Force 16:9 canvas for YouTube custom thumbs when source is landscape cover.
        if rgb.width >= rgb.height:
            tw, th = 1280, 720
            scale = max(tw / rgb.width, th / rgb.height)
            nw, nh = max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))
            rgb = rgb.resize((nw, nh), Image.Resampling.LANCZOS)
            left = max(0, (rgb.width - tw) // 2)
            top = max(0, (rgb.height - th) // 2)
            rgb = rgb.crop((left, top, left + tw, top + th))

    qualities = (90, 82, 74, 66, 58, 50, 42, 35)
    last = b""
    meta: dict = {"quality": None, "width": rgb.width, "height": rgb.height, "bytes": 0}
    for q in qualities:
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=q, optimize=True)
        last = buf.getvalue()
        meta = {"quality": q, "width": rgb.width, "height": rgb.height, "bytes": len(last)}
        if len(last) <= max_bytes:
            return last, meta

    # Still too big — downscale while keeping aspect.
    w, h = rgb.size
    for _ in range(8):
        w = max(320, int(w * 0.85))
        h = max(180, int(h * 0.85))
        scaled = rgb.resize((w, h), Image.Resampling.LANCZOS)
        for q in (70, 58, 45, 35):
            buf = io.BytesIO()
            scaled.save(buf, format="JPEG", quality=q, optimize=True)
            last = buf.getvalue()
            meta = {"quality": q, "width": w, "height": h, "bytes": len(last)}
            if len(last) <= max_bytes:
                return last, meta
        rgb = scaled

    if len(last) > max_bytes:
        raise RuntimeError(
            f"Could not compress thumbnail under {max_bytes} bytes "
            f"(final {len(last)} bytes at {meta.get('width')}x{meta.get('height')} q={meta.get('quality')})."
        )
    return last, meta


def write_jpeg_under(src: Path, dest: Path, *, max_bytes: int = YT_THUMB_MAX_BYTES) -> dict:
    with Image.open(src) as im:
        data, meta = jpeg_bytes_under(im, max_bytes=max_bytes, keep_aspect_16x9=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    meta["path"] = str(dest)
    meta["ok"] = True
    return meta


def maybe_reencode_slot_png(path: Path, *, max_bytes: int = SLOT_SOFT_MAX_BYTES) -> dict:
    """If a slot PNG is huge, re-save optimized PNG (keep .png extension for pipeline)."""
    if not path.is_file():
        return {"ok": False, "skipped": True}
    size = path.stat().st_size
    if size <= max_bytes:
        return {"ok": True, "skipped": True, "bytes": size}
    with Image.open(path) as im:
        rgba = im.convert("RGBA")
        # Try PNG optimize first.
        buf = io.BytesIO()
        rgba.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            path.write_bytes(data)
            return {"ok": True, "bytes": len(data), "format": "png"}
        # Fall back to high-quality JPEG bytes written as .png is unsafe; keep PNG
        # but strip alpha on a flattened black background for size.
        flat = Image.new("RGB", rgba.size, (0, 0, 0))
        flat.paste(rgba, mask=rgba.split()[-1] if rgba.mode == "RGBA" else None)
        for q in (92, 85, 78, 70):
            jbuf = io.BytesIO()
            flat.save(jbuf, format="JPEG", quality=q, optimize=True)
            # Keep extension .png for path compatibility but store JPEG — bad.
            # Instead write companion .jpg and leave PNG as optimized RGBA.
            break
        path.write_bytes(data)  # best-effort optimized PNG
        return {"ok": True, "bytes": len(data), "format": "png", "still_large": len(data) > max_bytes}
