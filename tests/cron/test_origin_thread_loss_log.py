"""The "origin has thread_id but delivery target lost it" warning is for origin targets only.

Home-channel (``deliver: telegram``), explicit ``platform:chat_id`` and broadcast targets are
chosen destinations without the origin thread; warning on them fires on every run.
"""
import logging

import pytest

from cron.scheduler_delivery import _prepare_target_delivery


@pytest.mark.parametrize("resolved_from,level", [
    ("origin", logging.WARNING),
    ("home", logging.DEBUG),
    ("explicit", logging.DEBUG),
    (None, logging.DEBUG),  # broadcast expansion: untagged
])
def test_lost_origin_thread_warns_only_for_origin_targets(caplog, resolved_from, level):
    caplog.set_level(logging.DEBUG, logger="cron.scheduler")
    job = {"id": "j1", "deliver": "telegram",
           "origin": {"platform": "telegram", "chat_id": "-1001", "thread_id": "42"}}
    target = {"platform": "no-such-platform", "chat_id": "-1002", "thread_id": None}
    if resolved_from:
        target["_resolved_from"] = resolved_from

    _prepare_target_delivery(
        job, target, adapters=None, loop=None, config=None, notify_delivery=True,
        mirror_enabled=False, mirror_text="", delivery_errors=[])

    lost = [r.levelno for r in caplog.records if "lost it" in r.getMessage()]
    assert lost == [level]
