"""Unit tests for completed-job cleanup eligibility."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from studio.job_cleanup import (
    is_completed_job,
    is_protected_job,
    list_eligible_completed_jobs,
    story_slug,
)


def test_story_slug_strips_retry_suffix():
    assert story_slug("why-do-cats-purr") == "why-do-cats-purr"
    assert story_slug("why-do-cats-purr-a1b2c3") == "why-do-cats-purr"
    assert story_slug("Why-Do-Cats-Purr-ABCDEF") == "why-do-cats-purr"


def test_completed_requires_youtube_confirmation():
    assert not is_completed_job({"has_video": True, "status": "rendered"})
    assert is_completed_job(
        {
            "has_video": True,
            "has_youtube": True,
            "youtube_url": "https://youtu.be/abc",
            "youtube_video_id": "abc",
        }
    )
    # Local mp4 may be deleted after upload.
    assert is_completed_job(
        {
            "has_video": False,
            "has_youtube": True,
            "youtube": {"video_id": "abc", "url": "https://youtu.be/abc", "ok": True},
        }
    )


def test_protected_running_pending_failed():
    assert is_protected_job({"running": True, "has_youtube": True, "has_video": True})
    assert is_protected_job({"queued": True})
    assert is_protected_job({"youtube_pending": True, "has_video": True})
    assert is_protected_job({"has_video": True, "job": {"error": "boom"}})
    assert not is_protected_job(
        {
            "has_video": True,
            "has_youtube": True,
            "youtube_video_id": "abc",
            "job": {"error": ""},
        }
    )


def test_eligibility_retention_and_duplicates(monkeypatch):
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    old = (now - timedelta(days=10)).isoformat()
    recent = (now - timedelta(days=2)).isoformat()

    items = [
        {
            "id": "why-cats",
            "title": "Why Cats",
            "created_at": old,
            "updated_at": old,
            "has_video": True,
            "has_youtube": True,
            "youtube_url": "https://youtu.be/keep",
            "youtube": {"video_id": "keep", "uploaded_at": old},
        },
        {
            "id": "why-cats-aaaaaa",
            "title": "Why Cats retry",
            "created_at": recent,
            "updated_at": recent,
            "has_video": True,
            "has_youtube": True,
            "youtube_url": "https://youtu.be/new",
            "youtube": {"video_id": "new", "uploaded_at": recent},
        },
        {
            "id": "fresh-upload",
            "title": "Fresh",
            "created_at": recent,
            "updated_at": recent,
            "has_video": True,
            "has_youtube": True,
            "youtube_url": "https://youtu.be/fresh",
            "youtube": {"video_id": "fresh", "uploaded_at": recent},
        },
        {
            "id": "still-running",
            "title": "Running",
            "created_at": old,
            "has_video": True,
            "has_youtube": True,
            "youtube_url": "https://youtu.be/run",
            "running": True,
        },
        {
            "id": "failed-job",
            "title": "Failed",
            "created_at": old,
            "has_video": True,
            "job": {"error": "render failed"},
        },
    ]

    monkeypatch.setattr(
        "studio.job_cleanup.load_settings",
        lambda: {"completed_job_retention_days": 7},
    )

    eligible = list_eligible_completed_jobs(is_admin=True, now=now, items=items)
    ids = {row["id"] for row in eligible}
    # Older completed duplicate of why-cats is eligible even inside retention.
    assert "why-cats" in ids
    assert any(row["id"] == "why-cats" and row["reason"] == "older_completed_duplicate" for row in eligible)
    # Latest why-cats retry is recent → not aged out.
    assert "why-cats-aaaaaa" not in ids
    # Fresh unique job inside retention stays.
    assert "fresh-upload" not in ids
    assert "still-running" not in ids
    assert "failed-job" not in ids
