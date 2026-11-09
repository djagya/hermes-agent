"""dlz: fork cron behaviour carried through the v0.23.0 merge (PLAN §4 cron rows).

- A media-only Telegram image set must arrive as ONE album or not at all: the live lane marks it
  ``_require_native_media_group`` and a failed album never falls back to per-photo sends. (The
  old album tests were lost in ``3992a14043``.)
- An operator stop (``status="cancelled"``) leaves the failure streak and alert markers alone.
- Profile distributions keep the fork's per-job script timeout and alert knobs.
"""

import asyncio
import threading

import pytest

import cron.scheduler_delivery as sd
from cron.jobs import _record_run_outcome
from cron.job_definition import merge_job_definition
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import SendResult


class _Telegram:
    """A native Telegram adapter whose album send fails."""

    platform = Platform.TELEGRAM

    def __init__(self):
        self.albums: list = []
        self.photos: list = []

    async def send(self, chat_id, content, metadata=None):
        return SendResult(success=True, message_id="t1")

    async def send_multiple_images(self, chat_id, images, metadata=None):
        self.albums.append((images, dict(metadata or {})))
        return SendResult(success=False, error="album rejected")

    async def send_image_file(self, chat_id, image_path, metadata=None, **_kw):
        self.photos.append(image_path)
        return SendResult(success=True, message_id="p1")


@pytest.fixture
def photos(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("gateway.platforms.base._HERMES_ROOT", tmp_path / "home")
    root = tmp_path / "media-cache"
    root.mkdir()
    monkeypatch.setattr("gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS", (root,))
    paths = []
    for name in ("a.png", "b.jpg"):
        (root / name).write_bytes(b"img")
        paths.append((str((root / name).resolve()), False))
    return paths


def _deliver(text, media_files, monkeypatch):
    monkeypatch.setattr(sd, "_maybe_mirror_cron_delivery", lambda *a, **k: None)
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()
    adapter = _Telegram()
    try:
        t = sd._prepare_target_delivery(
            {"id": "job-1", "origin": {"platform": "telegram", "chat_id": "555"}},
            {"platform": "telegram", "chat_id": "555"}, adapters={Platform.TELEGRAM: adapter},
            loop=loop, config=GatewayConfig(), notify_delivery=True, mirror_enabled=False,
            mirror_text="", delivery_errors=[])
        assert t is not None
        delivery_errors: list = []
        sd._deliver_via_live_adapter(
            t, text, media_files, target_errors=[], delivery_errors=delivery_errors,
            unverified_targets=[])
    finally:
        loop.call_soon_threadsafe(loop.stop)
        th.join(5)
        loop.close()
    return adapter, delivery_errors


def test_media_only_album_fails_closed_on_the_live_lane(photos, monkeypatch):
    adapter, errors = _deliver("", photos, monkeypatch)
    assert len(adapter.albums) == 1
    assert adapter.albums[0][1].get("_require_native_media_group") is True
    assert adapter.photos == []  # never split into loose photos
    assert any("album" in e for e in errors)


def test_album_with_caption_text_may_fall_back_to_single_photos(photos, monkeypatch):
    """Control: with visible text the set is not album-only, so a failed album degrades."""
    adapter, _errors = _deliver("caption", photos, monkeypatch)
    assert "_require_native_media_group" not in adapter.albums[0][1]
    assert len(adapter.photos) == 2


def test_cancelled_runs_leave_streak_and_alert_markers():
    job = {"failure_streak": 2, "failing_since": "2026-11-09T08:00:00+00:00",
           "preflight_alerted": True, "preflight_alerted_at": "2026-11-09T08:00:00+00:00",
           "last_failure": {"at": "2026-11-09T08:00:00+00:00", "detail": "boom"},
           "last_error": "boom"}
    before = {k: job[k] for k in
              ("failure_streak", "failing_since", "preflight_alerted", "preflight_alerted_at",
               "last_failure")}
    for at in ("2026-11-09T09:00:00+00:00", "2026-11-09T10:00:00+00:00"):
        _record_run_outcome(job, False, "stopped by operator", None, "cancelled", at)
    assert {k: job[k] for k in before} == before
    assert job["last_status"] == "cancelled" and job["last_error"] is None


def test_profile_distribution_keeps_fork_job_knobs():
    local = {"id": "j", "schedule": {"kind": "interval", "minutes": 60}, "repeat": {"completed": 3},
             "next_run_at": "2026-11-09T10:00:00+00:00", "state": "scheduled", "enabled": True}
    authored = {"schedule": {"kind": "interval", "minutes": 60}, "script_timeout_seconds": 5400,
                "alert_after_consecutive_failures": 3, "alert_when_stale_minutes": 90}
    merged = merge_job_definition(local, authored)
    assert (merged["script_timeout_seconds"], merged["alert_after_consecutive_failures"],
            merged["alert_when_stale_minutes"]) == (5400, 3, 90)
