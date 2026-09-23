"""Estimated Flux / OpenAI spend for Stickman Automation Studio.

Flux 2 Dev (~2MP) ballpark from the operator: ~4.6¢/image, ~$2.60 per ~10-min
video (~56 images), ~$15–16/day at 6 videos.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from studio.paths import PROJECTS_DIR, ensure_dirs
from studio.settings import FLUX_MODEL

# Operator ballpark for fal-ai/flux-2 (~2 megapixels).
FLUX_USD_PER_IMAGE = 0.046
FLUX_IMAGES_PER_10MIN = 56
FLUX_USD_PER_10MIN = 2.60
FLUX_USD_PER_DAY_6_VIDEOS = 15.5  # mid of ~$15–16


def rates_payload() -> dict[str, Any]:
    return {
        "flux_model": FLUX_MODEL,
        "flux_usd_per_image": FLUX_USD_PER_IMAGE,
        "flux_images_per_10min": FLUX_IMAGES_PER_10MIN,
        "flux_usd_per_10min": FLUX_USD_PER_10MIN,
        "flux_usd_per_day_6_videos": FLUX_USD_PER_DAY_6_VIDEOS,
        "estimate_note": (
            f"Estimates assume {FLUX_MODEL} at ~2MP ≈ ${FLUX_USD_PER_IMAGE:.3f}/image "
            f"(~{FLUX_IMAGES_PER_10MIN} images ≈ ${FLUX_USD_PER_10MIN:.2f} per ~10-min video; "
            f"~6 videos/day ≈ ${FLUX_USD_PER_DAY_6_VIDEOS:.0f}–16/day)."
        ),
    }


def estimate_flux_usd(image_count: int) -> float:
    n = max(0, int(image_count or 0))
    return round(n * FLUX_USD_PER_IMAGE, 4)


def estimate_flux_usd_for_duration(duration_seconds: float | int | None) -> dict[str, Any]:
    sec = max(0.0, float(duration_seconds or 0))
    if sec <= 0:
        return {"images": 0, "usd": 0.0}
    images = max(1, int(round(FLUX_IMAGES_PER_10MIN * (sec / 600.0))))
    return {"images": images, "usd": estimate_flux_usd(images)}


def _count_pngs(folder: Path) -> int:
    if not folder.is_dir():
        return 0
    return sum(1 for p in folder.glob("*.png") if p.is_file() and ".staging." not in p.name)


def project_image_counts(project_id: str, folder: Path | None = None) -> dict[str, Any]:
    """Count on-disk illustration PNGs (covers + line art). Marked as estimate source."""
    root = folder if folder is not None else PROJECTS_DIR / project_id
    covers = 0
    for name in (
        "script_cover_16x9.png",
        "script_cover_9x16.png",
        "script_cover.png",
    ):
        if (root / name).is_file():
            covers += 1
    lines_16 = _count_pngs(root / "script_billboards")
    lines_9 = _count_pngs(root / "script_9x16_billboards")
    backgrounds = _count_pngs(root / "script_backgrounds")
    # Prefer billboards; backgrounds are an alternate layout folder — don't double-count
    # if both exist for the same stems. Count max of the two layout folders + covers.
    line_images = max(lines_16, backgrounds) + lines_9
    total = covers + line_images
    return {
        "covers": covers,
        "line_images": line_images,
        "image_count": total,
        "source": "disk_pngs",
    }


def _parse_day(ts: str | None) -> str | None:
    if not ts:
        return None
    raw = str(ts).strip()
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return None


def _audit_flux_by_day(limit: int = 400) -> dict[str, int]:
    """Best-effort daily Flux image units from audit + spend counters context."""
    from studio.audit import read_entries

    days: dict[str, int] = {}
    flux_actions = {
        "flux_images",
        "flux_cover",
        "flux_regen",
        "generate_illustrations_with_flux",
        "generate_illustrations",
        "generate_cover",
        "regenerate_illustration",
        "illustrations/flux",
        "illustrations/generate",
        "illustrations/regenerate",
    }
    for row in read_entries(limit):
        if not row.get("success", True):
            continue
        action = str(row.get("action") or "")
        path = ""
        args = row.get("args") if isinstance(row.get("args"), dict) else {}
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        if isinstance(args, dict):
            path = str(args.get("path") or args.get("action") or "")
        hit = (
            action in flux_actions
            or action.startswith("flux_")
            or "illustrations/flux" in action
            or "illustrations/generate" in path
            or "illustrations/regenerate" in action
        )
        if not hit:
            # HTTP audit often logs path under args
            joined = f"{action} {path} {extra}".lower()
            if "flux" not in joined and "illustrations" not in joined:
                continue
            if "flux" not in joined and "/illustrations/" not in joined:
                continue
        day = _parse_day(row.get("ts"))
        if not day:
            continue
        units = 1
        for blob in (args, extra, row):
            if not isinstance(blob, dict):
                continue
            for key in ("units", "flux_images", "image_count", "count"):
                if key in blob:
                    try:
                        units = max(1, int(blob[key]))
                        break
                    except (TypeError, ValueError):
                        pass
        days[day] = days.get(day, 0) + units
    return days


def cost_report(*, limit_projects: int = 40) -> dict[str, Any]:
    """Per-project Flux estimates plus today counters and a short daily series."""
    ensure_dirs()
    from studio.projects import list_projects
    from studio.spend_guard import public_spend_status

    spend = public_spend_status()
    flux_today = int(spend.get("flux_images_today") or 0)
    openai_today = int(spend.get("openai_calls_today") or 0)

    projects_out: list[dict[str, Any]] = []
    for meta in list_projects()[: max(1, int(limit_projects))]:
        pid = str(meta.get("id") or "")
        if not pid:
            continue
        counts = project_image_counts(pid)
        duration = int(meta.get("duration_seconds") or 0)
        duration_est = estimate_flux_usd_for_duration(duration)
        image_count = int(counts["image_count"])
        # Prefer real PNG count; fall back to duration-based estimate when empty.
        if image_count > 0:
            usd = estimate_flux_usd(image_count)
            basis = "png_count"
            est_images = image_count
        else:
            usd = float(duration_est["usd"])
            basis = "duration_estimate" if duration else "none"
            est_images = int(duration_est["images"])
        projects_out.append(
            {
                "id": pid,
                "owner_id": meta.get("owner_id") or None,
                "title": (meta.get("title") or meta.get("topic") or pid).strip(),
                "duration_seconds": duration,
                "image_provider": meta.get("image_provider") or "",
                "image_count": image_count,
                "estimate_images": est_images,
                "estimate_usd": usd,
                "basis": basis,
                "covers": counts["covers"],
                "line_images": counts["line_images"],
                "updated_at": meta.get("updated_at") or meta.get("created_at") or "",
            }
        )

    # Chart: prefer projects with a non-zero estimate, newest first (list_projects already mtime-sorted).
    chart = [p for p in projects_out if float(p.get("estimate_usd") or 0) > 0][:24]
    if not chart:
        chart = projects_out[:12]

    audit_days = _audit_flux_by_day()
    # Merge today from spend counters (more accurate than audit for Flux units).
    today = spend.get("day") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if flux_today:
        audit_days[today] = max(int(audit_days.get(today) or 0), flux_today)

    daily = []
    for day in sorted(audit_days.keys())[-14:]:
        n = int(audit_days[day])
        daily.append(
            {
                "day": day,
                "flux_images": n,
                "estimate_usd": estimate_flux_usd(n),
            }
        )

    rates = rates_payload()
    return {
        **rates,
        "is_estimate": True,
        "spend": spend,
        "today": {
            "day": today,
            "flux_images": flux_today,
            "flux_usd": estimate_flux_usd(flux_today),
            "openai_calls": openai_today,
        },
        "projects": projects_out,
        "chart": chart,
        "daily": daily,
    }
