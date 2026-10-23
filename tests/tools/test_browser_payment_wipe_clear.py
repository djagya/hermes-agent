"""Security: a temporary slot's card-fill mark clears only on control's attested wipe; fail closed otherwise."""

import json

import pytest

from tools import browser_control_route, browser_payment_privacy as guard


@pytest.fixture
def control(monkeypatch):
    rows = {}
    monkeypatch.setattr(guard, "_managed", lambda: True)
    monkeypatch.setattr(browser_control_route, "slot_status", lambda slot: rows.get(slot, {"error": "unavailable"}))
    return rows


def _mark(slot="research-1", gen="g1", wipe="4"):
    guard.mark_payment_session("checkout", {"slot_id": slot, "browser_generation": gen, "wipe_count": wipe})


def _row(slot="research-1", kind="temporary", wipe=5, gen="g2"):
    return {"slot_id": slot, "kind": kind, "wipe_count": wipe, "browser_generation": gen}


def test_attested_wipe_clears_the_mark_and_audits_it(control):
    _mark()
    control["research-1"] = _row()
    assert guard.capture_blocked_for_surface("research-1", "g2") is False
    log = guard._state_path().with_suffix(".log").read_text(encoding="utf-8-sig").splitlines()
    assert json.loads(log[-1])["confirmed_by"] == "auto:temporary-profile-wiped"


@pytest.mark.parametrize("row", [
    {"error": "unavailable"},                      # control unreachable / refused
    _row(wipe=None),                               # counter missing
    _row(wipe="5"),                                # counter not an int
    _row(wipe=4),                                  # no wipe since the mark
    _row(gen="g1"),                                # same Chrome generation
    _row(kind="persistent"),                       # persistent profile: operator clear only
    _row(slot="research-2"),                       # row for another slot
])
def test_any_unknown_or_unattested_state_keeps_the_mark(control, row):
    _mark()
    control["research-1"] = row
    assert guard.capture_blocked_for_surface("research-1", "g2") is True
    assert guard.payment_session_sensitive(None) is True


def test_mark_without_counter_never_auto_clears(control):
    _mark(wipe="")
    control["research-1"] = _row(wipe=99)
    assert guard.capture_blocked_for_surface("research-1", "g2") is True
