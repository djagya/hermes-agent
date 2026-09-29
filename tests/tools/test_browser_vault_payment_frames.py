"""Payment cards filled into a PSP's hosted fields (cross-origin iframe), e.g. SBB checkout + Datatrans.

Contracts:
- Only out-of-process frames on an origin the user bound to THAT card, hanging under the CURRENT page
  session, are inspected or written; any other frame (another PSP, another tab) is never touched.
- The frame's live origin is re-asserted inside the inspection and the fill script; the fill script also
  asserts the frame's ancestor chain (top = merchant origin), so navigation or re-embedding writes nothing.
- Ambiguity (the same card field in two documents) and a missing number/CVC target fail closed.
- The user confirms before any card read; declining writes nothing. No card value reaches a tool result.
All data is synthetic; no browser, network or payment is involved.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Dict, List, Optional
from unittest.mock import patch

import pytest

from agent.vault_backends.base import LoginBackend
from agent.vault_login_classifier import build_fill_js
from agent.vault_store import VaultItemMeta
from tools import browser_vault_tool
from tools.browser_supervisor_frames import FrameTrackingMixin

_MERCHANT, _PSP = "https://www.sbb.ch", "https://pay.datatrans.com"
_CARD = {"card_number": "4111111111111111", "cvc": "737", "cardholder_name": "A User", "exp_month": "7", "exp_year": "2029"}
_PSP_FIELDS = [{"autocomplete": "cc-number", "index": 0, "type": "text"},
               {"autocomplete": "cc-exp", "index": 1, "type": "text"},
               {"autocomplete": "cc-csc", "index": 2, "type": "text"}]
_MERCHANT_FIELDS = [{"autocomplete": "email", "index": 0, "type": "email", "name": "email"}]


class _CardBackend(LoginBackend):
    name, display_name, prefix = "stub", "Stub", "stub:"

    def __init__(self, frame_origins=(_PSP,), origin: Optional[str] = _MERCHANT):
        self.meta = VaultItemMeta(id="stub:card", kind="payment", label="Travel Visa", origin=origin,
                                  created_at="", frame_origins=tuple(frame_origins))
        self.resolved = 0

    def list_items(self):
        return [self.meta]

    def get_meta(self, handle):
        return self.meta if handle == self.meta.id else None

    def resolve_password(self, handle):
        raise AssertionError("a card never resolves a password")

    def resolve_secret(self, handle):
        self.resolved += 1
        return dict(_CARD)


class _FakeSupervisor:
    """Frames keyed by id: {"origin", "controls", "live_origin"}. Records every frame eval."""

    def __init__(self, frames: Dict[str, dict]):
        self.frames = frames
        self.evals: List[tuple] = []

    def frames_on_origin(self, origin):
        return [{"frame_id": fid, "url": f["origin"] + "/upp"} for fid, f in self.frames.items() if f["origin"] == origin]

    def evaluate_in_frame(self, frame_id, expression, timeout=10.0):
        self.evals.append((frame_id, expression))
        frame = self.frames[frame_id]
        live = frame.get("live_origin", frame["origin"])
        if "data-hermes-vault-slot\", nonce" in expression:  # inspection (stamps every control)
            if live != frame["origin"]:
                return {"ok": True, "result": json.dumps({"refused": "origin_changed", "found": live})}
            return {"ok": True, "result": json.dumps(frame["controls"])}
        if frame.get("ancestor_changed"):
            return {"ok": True, "result": json.dumps({"refused": "ancestor_changed"})}
        return {"ok": True, "result": json.dumps({"filled": expression.count('"token":')})}


def _fill(backend, supervisor, *, consent="accept", merchant_controls=_MERCHANT_FIELDS, page_origin=_MERCHANT):
    page_secret_exprs: List[str] = []

    def fake_eval(task_id, expression):
        if "location.href" in expression:
            return {"success": True, "result": page_origin + "/checkout"}
        return {"success": True, "result": json.dumps(merchant_controls)}

    def fake_eval_secret(task_id, expression):
        page_secret_exprs.append(expression)
        return {"success": True, "result": json.dumps({"filled": expression.count('"token":')})}

    with patch("agent.vault_backends.base.enabled_backends", return_value=[backend]), \
         patch.object(browser_vault_tool, "_focus_bound_origin", lambda *a, **k: None), \
         patch.object(browser_vault_tool, "_ensure_supervisor", lambda task_id: supervisor), \
         patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
         patch.object(browser_vault_tool, "_eval_js_secret", side_effect=fake_eval_secret), \
         patch("tools.approval_prompt.request_elicitation_consent", return_value=consent) as prompt:
        raw = browser_vault_tool.browser_vault_fill("stub:card", task_id="t")
    return raw, json.loads(raw), page_secret_exprs, prompt


@pytest.fixture(autouse=True)
def _clear_redaction():
    from agent import redact
    yield
    redact.clear_vault_redaction_values()


def _secret_frame_evals(sup):
    return [e for e in sup.evals if _CARD["card_number"] in e[1]]


def test_datatrans_frame_fill_writes_card_only_into_the_bound_frame():
    sup = _FakeSupervisor({"F-dt": {"origin": _PSP, "controls": _PSP_FIELDS},
                           "F-ads": {"origin": "https://ads.example", "controls": _PSP_FIELDS}})
    backend = _CardBackend()
    raw, out, page_exprs, prompt = _fill(backend, sup)
    assert out["success"] is True and out["fields"] == ["cc-csc", "cc-exp", "cc-number"]
    assert out["frame_origins"] == [_PSP] and out["origin"] == _MERCHANT
    assert _PSP in prompt.call_args.args[0]  # the user sees WHICH provider frame gets the card
    assert page_exprs == []  # nothing card-shaped matched the merchant document
    written = _secret_frame_evals(sup)
    assert [fid for fid, _ in written] == ["F-dt"] and all(fid != "F-ads" for fid, _ in sup.evals)
    js = written[0][1]
    assert json.dumps(_PSP) in js and "ancestorOrigins" in js and json.dumps(_MERCHANT) in js
    assert _CARD["card_number"] not in raw and _CARD["cvc"] not in raw


def test_decline_writes_nothing_and_never_reads_the_card():
    sup = _FakeSupervisor({"F-dt": {"origin": _PSP, "controls": _PSP_FIELDS}})
    backend = _CardBackend()
    _, out, page_exprs, _ = _fill(backend, sup, consent="decline")
    assert out["error_type"] == "payment_declined"
    assert backend.resolved == 0 and sup.evals == [] and page_exprs == []


def test_top_level_on_the_psp_origin_is_refused():
    """The card is bound to the merchant page; a top-level PSP page (or phishing tab) is not the merchant."""
    sup = _FakeSupervisor({})
    backend = _CardBackend()
    _, out, page_exprs, _ = _fill(backend, sup, page_origin=_PSP)
    assert out["error_type"] == "origin_mismatch" and backend.resolved == 0 and page_exprs == []


def test_frame_that_navigated_away_is_refused_before_the_card_is_read():
    sup = _FakeSupervisor({"F-dt": {"origin": _PSP, "live_origin": "https://evil.example", "controls": _PSP_FIELDS}})
    backend = _CardBackend()
    _, out, _, _ = _fill(backend, sup)
    assert out["error_type"] == "origin_changed" and backend.resolved == 0 and _secret_frame_evals(sup) == []


def test_reembedded_frame_refuses_inside_the_fill_script():
    sup = _FakeSupervisor({"F-dt": {"origin": _PSP, "controls": _PSP_FIELDS, "ancestor_changed": True}})
    _, out, _, _ = _fill(_CardBackend(), sup)
    assert out["success"] is False and out["error_type"] == "ancestor_changed"


def test_two_bound_frames_with_the_same_field_are_ambiguous():
    sup = _FakeSupervisor({"F-1": {"origin": _PSP, "controls": _PSP_FIELDS},
                           "F-2": {"origin": _PSP, "controls": _PSP_FIELDS}})
    _, out, _, _ = _fill(_CardBackend(), sup)
    assert out["error_type"] == "ambiguous_fields" and _secret_frame_evals(sup) == []


def test_card_number_on_page_and_in_frame_is_ambiguous():
    sup = _FakeSupervisor({"F-dt": {"origin": _PSP, "controls": _PSP_FIELDS}})
    _, out, page_exprs, _ = _fill(_CardBackend(), sup, merchant_controls=[{"autocomplete": "cc-number", "index": 0, "type": "text"}])
    assert out["error_type"] == "ambiguous_fields" and page_exprs == [] and _secret_frame_evals(sup) == []


def test_missing_cvc_target_fails_closed():
    sup = _FakeSupervisor({"F-dt": {"origin": _PSP, "controls": _PSP_FIELDS[:2]}})
    _, out, page_exprs, _ = _fill(_CardBackend(), sup)
    assert out["error_type"] == "unmatched_fields" and "cc-csc" in out["error"]
    assert page_exprs == [] and _secret_frame_evals(sup) == []


def test_split_fields_page_holds_name_frame_holds_card():
    """Merchant page owns the cardholder name, the PSP frame owns number/expiry/CVC: each document gets
    only its own fields, and the merchant document never receives the PAN."""
    sup = _FakeSupervisor({"F-dt": {"origin": _PSP, "controls": _PSP_FIELDS}})
    _, out, page_exprs, _ = _fill(_CardBackend(), sup, merchant_controls=[{"autocomplete": "cc-name", "index": 0, "type": "text"}])
    assert out["success"] is True and out["fields"] == ["cc-csc", "cc-exp", "cc-name", "cc-number"]
    assert len(page_exprs) == 1 and _CARD["card_number"] not in page_exprs[0] and "A User" in page_exprs[0]
    assert "A User" not in _secret_frame_evals(sup)[0][1]


def test_card_without_frame_binding_never_inspects_frames():
    sup = _FakeSupervisor({"F-dt": {"origin": _PSP, "controls": _PSP_FIELDS}})
    _, out, _, _ = _fill(_CardBackend(frame_origins=()), sup)
    assert out["success"] is False and sup.evals == []


# ── fill-script ancestry guard ─────────────────────────────────────────────

def test_frame_fill_script_asserts_ancestors_before_any_write():
    js = build_fill_js([{"index": 0, "token": "cc-number", "value": "x"}], expected_origin=_PSP, nonce="n",
                       ancestry={"top": _MERCHANT, "allowed": [_MERCHANT, _PSP]})
    assert js.index("ancestor_changed") < js.index("querySelector(") and "ancestorOrigins" in js
    top_level = build_fill_js([{"index": 0, "token": "cc-number", "value": "x"}], expected_origin=_MERCHANT, nonce="n")
    assert "const ancestry = null;" in top_level


# ── supervisor frame tracking: only frames under the CURRENT page session ──

class _Tracker(FrameTrackingMixin):
    def __init__(self):
        self._frames, self._state_lock, self._page_session_id = {}, threading.Lock(), "S-page"

    async def _enable_child_domains(self, sid):
        return None


def _attach(tracker, target_id, sid, via, origin):
    async def go():
        await tracker._on_target_attached({"sessionId": sid, "targetInfo": {"type": "iframe", "targetId": target_id,
                                                                            "url": origin + "/f"}}, via)
    asyncio.run(go())
    tracker._on_frame_navigated({"frame": {"id": target_id, "url": origin + "/f", "securityOrigin": origin}}, sid)


def test_frames_on_origin_only_returns_frames_embedded_by_the_current_page():
    t = _Tracker()
    _attach(t, "F-dt", "S-dt", "S-page", _PSP)
    _attach(t, "F-other-tab", "S-x", "S-other-page", _PSP)
    _attach(t, "F-nested", "S-n", "S-dt", _PSP)  # PSP frame nested in the PSP frame: still under this page
    assert sorted(f["frame_id"] for f in t.frames_on_origin(_PSP)) == ["F-dt", "F-nested"]
    t._page_session_id = "S-other-page"  # tab switch: the old page's frames are no longer candidates
    assert [f["frame_id"] for f in t.frames_on_origin(_PSP)] == ["F-other-tab"]
    t._on_target_detached({"sessionId": "S-x"})
    assert t.frames_on_origin(_PSP) == []
