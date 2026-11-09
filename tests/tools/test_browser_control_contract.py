"""Cross-repo contract (browser fault hunt G5): the fork against monolith's control-v1 file.

``tests/fixtures/browser_control/control-v1.json`` is vendored from djagya/monolith
``services/browser/contract/control-v1.json`` and pinned by sha: refresh both together.
The B2 (capture-guard human clear) and B10 (handoff end notice) seams are checked
against what control declares, so a change on either side fails here, not live.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

from gateway.platforms import api_server_slot_handback as hb
from tools import browser_payment_privacy as privacy

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "tests/fixtures/browser_control/control-v1.json"
# monolith chore/v0230-handback-20261109 (durable handback event_id, /v1/handback/pending,
# handoff_ended reason_codes incl. expired_unused), 2026-11-09
CONTRACT_SHA256 = "00cc4c3012f5cfb1b861b266c48bb944f92da6778ab5eeb8f49bd0fc7c5a90cd"
# The body the fork's handback route reads for B10, as declared by control-v1.
HANDOFF_ENDED_BODY = ["event", "session_id", "slot_id", "lease_id", "reason_code", "event_id"]


@pytest.fixture(scope="module")
def contract():
    raw = CONTRACT_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == CONTRACT_SHA256, "vendored control-v1.json changed: re-pin the sha"
    data = json.loads(raw)
    assert data["protocol"] == "browser-control.v1"
    return data


def _route(contract, method, path):
    rows = [r for r in contract["routes"] if r["method"] == method and r["path"] == path]
    assert rows, f"control-v1 has no {method} {path}"
    return rows[0]


def test_every_control_route_the_fork_calls_is_in_the_contract(contract):
    called = set()
    for name in ("browser_control_route.py", "browser_control_tool.py", "browser_payment_privacy.py"):
        text = (ROOT / "tools" / name).read_text(encoding="utf-8")
        called |= set(re.findall(r'"(/v1/(?!browser-slots)[a-z-]+(?:/[a-z-]+)?)', text))
    declared = {r["path"] for r in contract["routes"]}
    missing = sorted(p for p in called if p not in declared and not p.startswith("/v1/files/"))
    assert not missing, f"fork calls routes control-v1 does not declare: {missing}"
    assert {"/v1/files/downloads/{name}", "/v1/files/staging/{name}"} <= declared


def test_b2_capture_guard_view_carries_the_human_clear_the_fork_reads(contract):
    row = _route(contract, "GET", "/v1/capture-guard")
    assert row["auth"] == "controller" and "?slot=" in row["body"]
    for field in ("slot_id", "capture_blocked", "human_clear {at, principal}"):
        assert field in row["reply"]
    # Only a human (Serve/Access principal) can write human_clear; the controller key cannot.
    assert _route(contract, "POST", "/v1/capture-guard/clear")["auth"] == "human"
    assert _route(contract, "POST", "/v1/capture-guard")["reply"].endswith("(set-only)")
    view = {"slot_id": "shopping", "capture_blocked": False, "human_clear": {"at": 2e9, "principal": "d@x"}}
    assert privacy._human_cleared_since({"slot_id": "shopping", "marked_epoch": 1e9}, view) == "human:d@x"


def test_release_handoff_pending_is_declared_deferred(contract):
    errors = " ".join(_route(contract, "POST", "/v1/release")["errors"])
    assert "handoff_pending" in errors and "deferred=true" in errors
    assert contract["reasons"]["handoff_pending"]["code"] == "busy"
    assert contract["reasons"]["lease_ended"]["code"] == "stale"  # release maps stale to already_ended


def test_d10b_lease_status_reports_lease_state(contract):
    row = _route(contract, "GET", "/v1/status")
    assert "?lease=|?lease_id=" in row["body"] and "lease_state=live" in row["reply"]


def _accepts(name, fields_list):
    body = {field: "x-1" for field in fields_list}
    body.update(event=name, session_id="s-1", slot_id="shopping")
    if "reason_code" in body:
        body["reason_code"] = "human_ended"
    fields, reason = hb.parse(body)
    assert fields is not None, f"{name}: {reason}"
    assert fields["event"] == name


def test_every_event_control_declares_is_accepted_by_the_handback_route(contract):
    assert contract["events"], "control-v1 declares no events"
    for name, spec in contract["events"].items():
        assert name in hb.EVENTS, f"control sends {name}; the handback route refuses it"
        _accepts(name, spec["body"])


def test_b10_control_declares_handoff_ended_with_the_body_the_fork_reads(contract):
    spec = contract["events"]["handoff_ended"]
    assert set(spec["body"]) == set(HANDOFF_ENDED_BODY)
    _accepts("handoff_ended", spec["body"])


def test_durable_handback_seams_match_control(contract):
    """Codex qol #10 / expired-link ask: control sends event_id, answers the restart confirmation
    on a controller-only route, and every handoff_ended reason it sends has fixed text here."""
    row = _route(contract, "GET", "/v1/handback/pending")
    assert row["auth"] == "controller" and row["body"] == ["?event_id="] and row["lanes"] == ["private"]
    assert "pending=true" in row["reply"] and "pending=false" in row["reply"]
    for spec in contract["events"].values():
        assert "event_id" in spec["body"]
    assert set(contract["events"]["handoff_ended"]["reason_codes"]) == set(hb.HANDOFF_ENDED_REASONS)
    fields, _ = hb.parse({"event": "handback", "session_id": "s-1", "slot_id": "x", "lease_id": "l",
                          "event_id": "a" * 32})
    assert fields["event_id"] == "a" * 32
    assert hb.parse({"event": "handback", "session_id": "s-1", "slot_id": "x", "lease_id": "l",
                     "event_id": "../x"})[0]["event_id"] == ""
