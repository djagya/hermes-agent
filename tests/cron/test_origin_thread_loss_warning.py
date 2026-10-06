"""The lost-origin-thread warning fires only for targets that were meant to be the origin."""
import logging

import pytest

from cron.scheduler_delivery import _prepare_target_delivery

_ORIGIN = {"platform": "telegram", "chat_id": "-100fixture", "thread_id": "8383"}


def _prepare(target):
    job = {"id": "fixture-job", "deliver": "telegram", "origin": dict(_ORIGIN)}
    errors: list = []
    _prepare_target_delivery(
        job, target, adapters=None, loop=None, config=None, notify_delivery=True,
        mirror_enabled=False, mirror_text="", delivery_errors=errors)
    return errors


def _lost(caplog):
    return [r for r in caplog.records if "lost it" in r.getMessage()]


def test_origin_target_without_thread_warns(caplog):
    caplog.set_level(logging.DEBUG, logger="cron.scheduler")
    _prepare({"platform": "no-such-platform", "chat_id": "-100fixture", "thread_id": None,
              "_resolved_from": "origin"})
    assert [r.levelno for r in _lost(caplog)] == [logging.WARNING]


@pytest.mark.parametrize("resolved_from", ["home", "explicit", None])
def test_deliberate_non_origin_target_is_not_a_warning(caplog, resolved_from):
    # deliver='telegram' (home DM), an explicit chat, or a broadcast is a chosen route.
    caplog.set_level(logging.DEBUG, logger="cron.scheduler")
    target = {"platform": "no-such-platform", "chat_id": "home-dm", "thread_id": None}
    if resolved_from:
        target["_resolved_from"] = resolved_from
    _prepare(target)
    assert [r.levelno for r in _lost(caplog)] == [logging.DEBUG]
