"""Payment windows (tools/browser_payment_card_values.py) r9: releases fenced by the window generation they
observed, slot-less supervisors kept apart, browser results masked until the mark clears, a slot clear
drops every supervisor's values, every browser_cdp branch redacted, payment_done redacted before release.
Fake supervisors and fake CDP only; synthetic data."""

import json
from types import SimpleNamespace

import pytest

from agent import redact
from tools import browser_cdp_tool as cdp_tool
from tools import browser_payment_card_values as card_values
from tools import browser_payment_privacy as privacy
from tools import browser_payment_surface as surface
from tools import browser_use_cli
from tools import browser_vault_payment as bvp
from tests.tools.test_browser_payment_done import CHECKOUT, CONFIRM, _fast, _paid  # _fast: its autouse fixture
from tests.tools.test_browser_payment_surface import FakeSup

CARD = {"card_number": "4000056655665556", "cvc": "737", "cardholder_name": "Ada Testperson",
        "exp_month": "03", "exp_year": "2031"}
LOCKED = {"locked": True, "native": True, "tabs": ["TAB"], "unverified": False}
UNLOCKED = {"locked": False, "native": False, "tabs": [], "unverified": False}
PROBE = "printed 737 by Ada Testperson"
MASKED = "printed ••• by «redacted-card»"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(surface, "_managed", lambda: False)
    redact.clear_vault_redaction_values()
    yield
    redact.clear_vault_redaction_values()


def _routes(monkeypatch, **routes):
    monkeypatch.setattr(browser_use_cli, "vault_route", lambda task_id: routes.get(task_id, {}))


def _sup(task_id, status=LOCKED):
    sup = FakeSup()
    sup.task_id, sup.status = task_id, dict(status)
    return sup


def _paying(task_id, card=CARD):
    """A supervisor that claimed (this process saw it locked) and filled ``card``."""
    sup = _sup(task_id)
    with surface._LOCK:
        surface._HELD.add(sup)
    bvp._register_card_redaction(card, sup)
    card_values.register_dialog_card_values(card, sup)
    return sup


def test_a_stale_unlock_never_closes_a_window_reopened_meanwhile(monkeypatch):
    # Codex P1: another supervisor on the slot claimed and filled while this one's status read was on the
    # wire; the old "unlocked" answer closed the new payment's window
    _routes(monkeypatch, t1={"slot_id": "shopping"}, t2={"slot_id": "shopping"})
    old = _paying("t1")
    old.status = dict(UNLOCKED)
    real = old.on

    def straddled(method, params, session):
        if method == "Hermes.paymentSurfaceStatus":
            _paying("t2")  # the newer claim and fill on the same slot
        return real(method, params, session)

    old.on = straddled
    assert surface.lock_status(old) == "unlocked"
    assert redact.redact_sensitive_text(PROBE, force=True) == MASKED


def test_an_old_marks_prune_keeps_the_live_payments_window(monkeypatch):
    # peer P3-1: a wiped OLD mark on the slot closed the window and the dialog values of the payment in progress
    import tools.browser_control_route as control

    _routes(monkeypatch, t1={"slot_id": "shopping"})
    privacy.mark_payment_session("old", {"slot_id": "shopping", "browser_generation": "g1",
                                         "profile_kind": "temporary", "wipe_count": "1"})
    privacy.mark_payment_session("new", {"slot_id": "shopping", "browser_generation": "g2",
                                         "profile_kind": "temporary", "wipe_count": "2"})
    sup = _paying("t1")
    monkeypatch.setattr(privacy, "_managed", lambda: True)
    monkeypatch.setattr(control, "slot_status", lambda slot_id: {"slot_id": slot_id, "kind": "temporary",
                                                                   "wipe_count": 2, "browser_generation": "g2"})
    monkeypatch.setattr(control, "capture_guard_view", lambda slot_id: None)
    try:
        assert privacy.capture_blocked_for_surface("shopping", "g2") and len(privacy._all_marks()) == 1
        assert redact.redact_sensitive_text(PROBE, force=True) == MASKED
        assert "Ada Testperson" in card_values.dialog_card_values(sup)
    finally:
        card_values.release_slot("shopping")


def test_slotless_supervisors_keep_their_own_windows(monkeypatch):
    # peer P3-2: two supervisors without a slot route shared the window key "", so one's release closed both
    _routes(monkeypatch)
    _paying("a")
    other = _paying("b", {**CARD, "cvc": "1234", "card_number": "378282246310005", "cardholder_name": "Bo Other"})
    other.status = dict(UNLOCKED)
    assert surface.lock_status(other) == "unlocked"
    assert redact.redact_sensitive_text(PROBE, force=True) == MASKED


def test_after_the_release_browser_results_stay_masked_until_the_mark_clears(monkeypatch):
    # Codex P2: the page can still show card text after the unlock; terminal output stops masking there
    _routes(monkeypatch, t1={"slot_id": "shopping"})
    privacy.mark_payment_session("checkout", {"slot_id": "shopping", "browser_generation": "g1"})
    sup = _paying("t1")
    sup.status = dict(UNLOCKED)
    assert surface.lock_status(sup) == "unlocked"
    assert redact.redact_sensitive_text(PROBE, force=True) == PROBE
    assert cdp_tool._redact_cdp_output({"result": {"value": PROBE}}) == {"result": {"value": MASKED}}
    assert privacy.clear_payment_surface("shopping", confirmed_by="test") == 1
    assert cdp_tool._redact_cdp_output({"result": {"value": PROBE}}) == {"result": {"value": PROBE}}


def test_a_slot_clear_drops_every_supervisors_dialog_values(monkeypatch):
    # Codex P3: the slot clear kept the paying supervisor's own values, so its ordinary dialogs stayed masked
    from tools.browser_payment_done import payment_text_exposed

    _routes(monkeypatch, t1={"slot_id": "shopping"})
    privacy.mark_payment_session("checkout", {"slot_id": "shopping", "browser_generation": "g1"})
    sup = _paying("t1")
    with surface._LOCK:
        surface._HELD.discard(sup)
    assert payment_text_exposed(sup)
    privacy.clear_payment_surface("shopping", confirmed_by="test")
    assert not card_values.has_dialog_card_values(sup) and not payment_text_exposed(sup)


def test_every_browser_cdp_branch_and_number_is_redacted(monkeypatch):
    # Codex P2: the frame route returned its result raw, and a numeric leaf kept a login PIN (no payment lock)
    redact.register_vault_redaction_value("4711")
    assert cdp_tool._redact_cdp_output({"result": {"type": "number", "value": 4711}}) == \
        {"result": {"type": "number", "value": "••••"}}
    sup = FakeSup(on=lambda method, params, session: {"result": {"type": "string", "value": "pin 4711"}})
    frames = [{"frame_id": "PAGE"}, {"frame_id": "ADS", "session_id": "S-ads"}]
    sup.snapshot = lambda: SimpleNamespace(frame_tree={"top": frames[0], "children": frames[1:]})
    sup._frames = {}
    monkeypatch.setattr("tools.browser_supervisor.SUPERVISOR_REGISTRY.get", lambda task_id: sup)
    out = json.loads(cdp_tool._browser_cdp_via_supervisor("t", "ADS", "Runtime.evaluate", {"expression": "x"}, 5))
    assert out["result"]["result"]["value"] == "pin ••••", out


def test_payment_done_redacts_its_confirmation_before_the_window_narrows(monkeypatch):
    # peer P3-5: the confirmation snapshot went back after the release closed the window, and unredacted
    _routes(monkeypatch)
    sup = _paid([CHECKOUT, CONFIRM])
    sup.page = {**sup.page, "text": "Thank you Ada Testperson, card 4000 0566 5566 5556",
                "links": [{"url": "https://shop.test/r/77", "text": "Receipt for Ada Testperson"}]}
    with surface._LOCK:
        surface._KNOWN_LOCKED.add(sup)
    bvp._register_card_redaction(CARD, sup)
    out = done_payment(sup)
    assert out["success"] and out["text"] == "Thank you «redacted-card», card •••• •••• •••• ••••", out
    assert out["links"][0]["text"] == "Receipt for «redacted-card»"


def done_payment(sup):
    from tools.browser_payment_done import payment_done

    return payment_done(sup)


def test_payment_done_opens_the_raw_confirmation_url_and_returns_the_masked_one(monkeypatch):
    # r9 Codex P1: the masked URL was what the new tab opened ("/c/•••e7d••••bc/..."): a dead page
    _routes(monkeypatch)
    sup = _paid([CHECKOUT, CONFIRM])
    raw = "https://shop.test/c/9f8e7d123abc/thank-you"
    sup.page = {**sup.page, "url": raw}
    with surface._LOCK:
        surface._KNOWN_LOCKED.add(sup)
    bvp._register_card_redaction(CARD, sup)  # a 3-digit CVC window is open
    out = done_payment(sup)
    assert out["success"] and out["new_tab"] == "NEWTAB", out
    assert [c[1]["url"] for c in sup.sent("Target.createTarget")] == [raw]
    assert out["url"] == "https://shop.test/c/9f8e7d•••abc/thank-you", out


def test_browser_cdp_guard_refusals_are_redacted(monkeypatch):
    # r9 Codex P2: the page-URL refusal quoted a registered password sitting in the page URL
    import tools.browser_url_policy as policy

    redact.register_vault_redaction_value("hunter-42-pw")
    monkeypatch.setattr(cdp_tool, "_WS_AVAILABLE", True)
    monkeypatch.setattr(cdp_tool, "_resolve_cdp_endpoint", lambda: "ws://127.0.0.1:9/devtools/browser/x")
    monkeypatch.setattr(policy, "eval_ssrf_guard_active", lambda: True)
    monkeypatch.setattr(cdp_tool, "_current_page_private_url", lambda endpoint, target_id: "http://10.0.0.5/?p=hunter-42-pw")
    out = cdp_tool.browser_cdp("DOM.getDocument")
    assert "private or internal address" in out and "hunter-42-pw" not in out, out


def test_origins_and_vault_errors_shown_to_the_model_are_scrubbed(monkeypatch):
    # r10 Codex/peer P2: a planted tab at a card-number subdomain came back raw in the locked tab list and
    # in payment_done refusals; a page-quoting vault error returned a registered password verbatim
    from tools import browser_payment_done as done
    from tools import browser_vault_tool as vault

    redact.register_card_window("shopping", digits=(CARD["card_number"], CARD["cvc"]))
    shown = done._shown_origin("https://4000056655665556.exfil.test/x")
    assert "4000056655665556" not in shown and shown.startswith("https://")
    assert surface._origin_only("https://4000056655665556.exfil.test/") == shown
    redact.register_vault_redaction_value("hunter-42-pw")
    monkeypatch.setattr(vault, "_handle_vault_inner", lambda args, **kw: json.dumps(
        {"success": False, "error": "Could not inspect page inputs: hunter-42-pw"}))
    out = json.loads(vault._handle_vault({"action": "fill", "handle": "op:x"}))
    assert "hunter-42-pw" not in out["error"] and out["success"] is False


def test_vault_page_fields_and_raised_errors_are_scrubbed(monkeypatch):
    # r11 Codex P2: receiving_origins / origin returned raw; a raised error was quoted by the registry fallback
    from tools import browser_vault_tool as vault

    redact.register_card_window("shopping", digits=(CARD["card_number"], CARD["cvc"]))
    host = "https://4000056655665556.exfil.test"
    monkeypatch.setattr(vault, "_handle_vault_inner", lambda args, **kw: json.dumps(
        {"success": False, "error_type": "payment_fields_ambiguous", "receiving_origins": [host],
         "receiving": {host: ["card_number"]}, "handle": "op:item737"}))
    out = vault._handle_vault({"action": "fill", "handle": "op:item737"})
    assert "4000056655665556" not in out and json.loads(out)["handle"] == "op:item737"
    redact.register_vault_redaction_value("hunter-42-pw")

    def boom(args, **kw):
        raise ValueError("invalid literal for int(): 'hunter-42-pw'")

    monkeypatch.setattr(vault, "_handle_vault_inner", boom)
    out = json.loads(vault._handle_vault({"action": "enter_code"}))
    assert out["success"] is False and "hunter-42-pw" not in out["error"]


def test_exec_dialog_origins_are_scrubbed(monkeypatch):
    # r11 Codex P2: pending dialog url/page at a digit host came back raw after the window narrowed
    from tools import browser_exec_dialogs as dialogs

    redact.register_card_window("shopping", digits=(CARD["card_number"], CARD["cvc"]))
    host = "https://4000056655665556.exfil.test/pay"
    dialog = SimpleNamespace(url=host, top_url=host, opened_locked=False,
                             to_dict=lambda: {"id": "d-1", "type": "alert", "message": "hi"})
    sup = SimpleNamespace(snapshot=lambda: SimpleNamespace(pending_dialogs=[dialog], recent_dialogs=[]))
    monkeypatch.setattr(dialogs, "_supervisor", lambda task_id: sup)
    monkeypatch.setattr(surface, "slot_locked", lambda s: False)
    out = dialogs.collect("t1", 0.0)
    assert "4000056655665556" not in json.dumps(out), out


def test_bridge_kinds_echo_fields_and_dotted_card_groups(monkeypatch):
    # r12 Codex P2: a page-chosen bridge `kind` became the dialog type verbatim; r11 peer: masking the echoed
    # approval fields refused every fill on a slot with an open window; dotted card groups were not masked
    import asyncio

    from tools import browser_supervisor_dialogs as bsd
    from tools import browser_vault_tool as vault

    calls, admitted = [], []

    class Sup(bsd.DialogSupervisionMixin):
        async def _cdp_quiet(self, method, params, **kw):
            calls.append(method)

        def _admit_dialog(self, **kw):
            admitted.append(kw)

    url = f"https://{bsd.DIALOG_BRIDGE_HOST}/x?kind=4000056655665556&message=hi"
    asyncio.run(Sup()._on_fetch_paused({"requestId": "r1", "request": {"url": url}}, "s1"))
    assert calls == ["Fetch.failRequest"] and not admitted
    asyncio.run(Sup()._on_fetch_paused({"requestId": "r2", "request": {"url": url.replace("4000056655665556", "confirm")}}, "s1"))
    assert admitted and admitted[0]["type"] == "confirm"
    redact.register_card_window("shopping", digits=(CARD["card_number"], CARD["cvc"]))
    monkeypatch.setattr(vault, "_handle_vault_inner", lambda args, **kw: json.dumps(
        {"success": False, "approval_origins": ["https://shop365.example"], "merchant_origin": "https://shop365.example"}))
    out = json.loads(vault._handle_vault({"action": "fill", "handle": "op:x"}))
    assert out["approval_origins"] == ["https://shop365.example"] and out["merchant_origin"] == "https://shop365.example"
    assert "4000" not in redact.redact_sensitive_text("https://4000.0566.5566.5556.host/", force=True)


def test_cdp_protocol_ids_survive_a_mask_window_but_page_values_do_not():
    # r13 Codex P2: targetId "A1234…" / nodeId 1234 were masked during a PIN window, breaking the next call
    redact.register_vault_redaction_value("4711")
    tid = "A1234" + "B" * 27
    out = cdp_tool._redact_cdp_output({"targetInfos": [{"targetId": tid, "title": "pin 4711"}], "nodeId": 1234,
                                       "backendNodeIds": [1234, 5678],
                                       "result": {"type": "object", "value": {"targetId": "4711", "n": 4711}}})
    assert out["targetInfos"][0]["targetId"] == tid and out["nodeId"] == 1234 and out["backendNodeIds"] == [1234, 5678]
    assert out["targetInfos"][0]["title"] == "pin ••••"
    assert out["result"]["value"] == {"targetId": "••••", "n": "••••"}
