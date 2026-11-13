"""The armed card fill (vault.payment_fill_auto, 11-13): one approval, several inspections. Without a browser: the
real commit closure in Node driven by the real supervisor (label-only changes, the role record, kept fields, the
repair pass), the approval binding, the re-inspection loop and the vault's refusal before any card read. Chromium
receipts for the three real stops (SBB/Datatrans, Eurowings, FlixBus/Payrails): test_browser_vault_payment_live.py
(test_fill_auto_*)."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent.vault_payment_policy import PaymentDestinationError, PaymentPolicyError
from tests.tools.test_browser_payment_repin import _SCENARIOS as _REPIN_SCENARIOS
from tests.tools.test_browser_payment_repin import (MERCHANT, PSP, _SYNTHETIC, _ClosureSupervisor, _controls,
                                                    _NodeClosure)
from tools import browser_supervisor_payment as sp
from tools import browser_supervisor_payment_auto as spa
from tools.browser_supervisor_payment import PaymentDocument, PaymentInspection

_FIELDS = """
const exp = input({id: 'exp', autocomplete: 'cc-exp', aria: 'Expiry'}), csc = input({id: 'csc', autocomplete: 'cc-csc', aria: 'CVC'});
const num = input({id: 'num', autocomplete: 'cc-number', aria: 'Card number'});
els = [exp, csc, num];
// a second write of one field shows as 'TWICE' (events without a write, from the kept-field no-ops, do not count); a
// value cleared by page code starts the count again
const track = e => { let h = '', n = 0; Object.defineProperty(e, 'held', {get: () => h, set: v => {
  if (v === '') { h = ''; n = 0; } else { n++; h = n > 1 ? 'TWICE' : v; } }}); };
for (const e of els) track(e);
const twice = () => {};"""
_AUTO_SCENARIOS = {
    # SBB / pay.datatrans.com: every focus and every write re-labels the other fields, and the approval wait one
    "auto_relabel": _FIELDS + """
let n = 0;
const relabel = (e, why) => { n++; for (const o of els) if (o !== e) o.aria = o.aria.split(' - ')[0] + ' - ' + why + n; };
for (const e of els) { e.oninput = () => { twice(e); relabel(e, 'required '); }; e.onfocus = () => relabel(e, 'check '); }
approve = () => { num.aria = 'Card number - please check'; };""",
    # a label-only change that moves a role: after the expiry write the CVC's and a promo field's labels swap
    "auto_role_swap": """
const promo = input({id: 'a', aria: 'Promo code'}), csc = input({id: 'b', aria: 'Security code', maxLength: 4});
const exp = input({id: 'exp', autocomplete: 'cc-exp'}), num = input({id: 'num', autocomplete: 'cc-number'});
els = [promo, exp, csc, num];
exp.oninput = () => { promo.aria = 'Security code'; csc.aria = 'Promo code'; };""",
    # Eurowings: the first card number write clears the CVC
    "auto_cvc_cleared": _FIELDS + """
for (const e of els) e.oninput = () => twice(e);
num.oninput = () => { csc.held = ''; };""",
    # a CVC that never keeps its value
    "auto_cvc_never": _FIELDS + """
csc.oninput = () => { csc.held = ''; };""",
    # FlixBus / Payrails: while the approval is open the whole form is built again from fresh elements
    "auto_remounted": _FIELDS + """
for (const e of els) e.oninput = () => twice(e);
approve = () => { for (const e of [exp, csc, num]) { const n = replace(e); n.aria = e.aria; track(n); } };""",
    # r1 F3: the first focus re-labels the other fields once (a label-only change, accepted in the focused phase)
    "auto_focus_relabel": _FIELDS + """
let first = true;
for (const e of els) e.onfocus = () => { if (first) { first = false; for (const o of els) if (o !== e) o.aria = o.aria + ' - check'; } };""",
    # r1 F4: the form is built again while the approval is open, and a merchant overlay covers the new expiry field
    "auto_remounted_covered": _FIELDS + """
approve = () => { for (const e of [exp, csc, num]) { const n = replace(e); n.aria = e.aria; if (e === exp) covers.push({top: n.top}); } };""",
    # a re-mounted expiry input that appears once, after the CVC's write (the repin test's "replacement" re-does it on each CVC event)
    "auto_replacement": """
const exp = input({id: 'exp', autocomplete: 'cc-exp'}), csc = input({id: 'csc', autocomplete: 'cc-csc'});
const num = input({id: 'num', autocomplete: 'cc-number'});
els = [exp, csc, num];
let done = false;
csc.oninput = () => { if (!done) { done = true; replace(exp); } };""",
    # r1 F14: the CVC is cleared by the card number's write; every event each field gets is logged
    "auto_kept_events": """
const exp = input({id: 'exp', autocomplete: 'cc-exp', aria: 'Expiry'}), csc = input({id: 'csc', autocomplete: 'cc-csc', aria: 'CVC'});
const num = input({id: 'num', autocomplete: 'cc-number', aria: 'Card number'}), log = input({id: 'log'});
log.held = '';
els = [exp, csc, num];
for (const e of els) e.dispatchEvent = function (ev) { log.held += this.id + ':' + ev.type + ' '; if (ev.type === 'input' && this.oninput) this.oninput(); return true; };
num.oninput = () => { csc.held = ''; };""",
    # r1 F14, a select: the year select is cleared by the number's write
    "auto_kept_select": """
const month = select({id: 'month', autocomplete: 'cc-exp-month', options: months(1), aria: 'Month'});
const year = select({id: 'year', autocomplete: 'cc-exp-year', options: [{value: '2035', text: '2035', disabled: false}], aria: 'Year'});
const csc = input({id: 'csc', autocomplete: 'cc-csc', aria: 'CVC'}), num = input({id: 'num', autocomplete: 'cc-number', aria: 'Card number'});
const log = input({id: 'log'}); log.held = '';
els = [month, year, csc, num];
for (const e of els) e.dispatchEvent = function (ev) { log.held += this.id + ':' + ev.type + ' '; if (ev.type === 'input' && this.oninput) this.oninput(); return true; };
num.oninput = () => { csc.held = ''; };""",
    # a field that changes type (not a label): never accepted in place, inspected again
    "auto_retyped": _FIELDS + """
csc.oninput = () => { exp.type = 'tel'; };""",
}
# the repin test's own table is never touched (both files run in one pytest process)
_SCENARIOS = {**_REPIN_SCENARIOS, **_AUTO_SCENARIOS}
_FILLED = {"exp": "07/35", "csc": "123", "num": "4111111111111111"}


class _AutoSupervisor(_ClosureSupervisor):
    """The real armed commit, label acceptance and role record over the Node closure; a re-inspection is a fresh
    closure over the document as it is now, judged by the real binding and role rules (the frame walk is Chromium's)."""

    async def _reinspect(self, prior, stop, wait):
        self.events.append("reinspect")
        self.js.call("inspect")
        d = prior.documents[0]
        document = PaymentDocument(d.frame_id, d.frame_session, d.object_id, d.origin,
                                   sp._card_controls(self.js.call("describe")), clock_anchor=time.monotonic())
        fresh = replace(prior, documents=(document,), consumed=False, release_task=None)
        refused = spa._approval_mismatch(prior.approval, fresh) \
            or await self._remember_roles(fresh, frozenset(prior.approval["documents"]))
        return (None, refused) if refused else (fresh, "")


def _auto_commit(scenario, tmp_path, monkeypatch, strict=False, parent=False, **checks):
    """``parent``: the card document sits in an iframe of the merchant page (its owner chain is checked: ``owner``)."""
    from tools.browser_vault_payment import _document_fills

    js = _NodeClosure(scenario, tmp_path, _SCENARIOS)
    try:
        sup = _AutoSupervisor(js, **checks)
        doc = PaymentDocument("card", "s", "o-card", PSP, sp._card_controls(js.call("describe")),
                              clock_anchor=time.monotonic())
        tree = (("card", "L", PSP + "/card", PSP, "top"),) if parent else (("card", "L", PSP + "/card", PSP, ""),)
        i = PaymentInspection("op", "s", "tab", MERCHANT, (doc,), tree,
                              time.monotonic() + 60, owner=sup, approval_id="op")
        assert asyncio.run(sup._arm_payment_auto(i)) is True and i.auto is True
        js.call("approve")
        monkeypatch.setattr(sp, "_SETTLE_SECONDS", 0)
        out = asyncio.run(sup._commit_payment_auto(i, _document_fills(i, _SYNTHETIC), None, strict,
                                                   lambda fresh: _document_fills(fresh, _SYNTHETIC), None))
        return out, js.call("values"), sup.events
    finally:
        js.close()


def test_label_only_changes_are_classified_again_in_place_and_use_no_re_inspection(tmp_path, monkeypatch):
    """11-13 SBB: each focus and each write re-labels the other fields. Every field is written once; each change is
    read (relabeled), classified and accepted; no re-inspection, whatever their number."""
    out, values, events = _auto_commit("auto_relabel", tmp_path, monkeypatch)
    assert out == {"success": True, "filled_fields": 3, "retention_verified": True}, out
    assert values == _FILLED and "reinspect" not in events
    assert events.count("relabeled") == events.count("accept") >= 5, events  # more than any re-inspection budget


def test_a_label_change_that_moves_a_role_is_not_accepted_and_the_re_inspection_refuses_it(tmp_path, monkeypatch):
    out, values, events = _auto_commit("auto_role_swap", tmp_path, monkeypatch)
    assert out["error_type"] == "payment_fields_changed" and out["partially_filled"] is True, out
    assert out["fill_check"]["reason"] == "fields_relabeled" and out["fill_check"]["keys"] == ["label"]
    assert out["fill_reinspect"] == {"count": 0, "repairs": 0, "refused": "role_changed"}, out
    assert "accept" not in events and values["a"] == "" and values["num"] == "", (events, sorted(values))


@pytest.mark.parametrize("scenario, refused", [
    ("auto_replacement", None),               # a re-mounted expiry input: inspected again, written
    ("auto_retyped", None),                   # a changed input type is no label change: inspected again, same role
    ("promo_becomes_expiry", "role_changed"),  # same token set, but the promo field took the expiry's role
    ("holder_becomes_number", "fields_changed"),
    ("gift_card", "fields_changed"),          # the document replaced by a gift-card form during the approval
])
def test_a_changed_field_set_goes_on_only_on_a_matching_re_inspection(scenario, refused, tmp_path, monkeypatch):
    out, values, events = _auto_commit(scenario, tmp_path, monkeypatch)
    assert events.count("reinspect") == 1, events
    if refused is None:
        assert out["success"] and out["fill_reinspect"] == {"count": 1, "repairs": 0}, out
        assert {k: v for k, v in values.items() if v} == {("exp2" if scenario == "auto_replacement" else "exp"): "07/35",
                                                          "csc": "123", "num": "4111111111111111"}
        return
    assert out["error_type"] == "payment_fields_changed" and out["fill_reinspect"]["refused"] == refused, out
    assert out["fill_reinspect"]["count"] == 0 and values.get("gift", "") == "" and values.get("promo", "") == ""
    assert values.get("name", "") == "" and len([v for v in values.values() if v]) == out["filled_fields"], out


def test_a_form_re_mounted_before_any_write_is_retried_once_under_the_same_approval(tmp_path, monkeypatch):
    """11-13 FlixBus (owner: "auto-retry inside approval"): the fill stops on a changed page before any field was
    written (the commit's own start check: fields_replaced, filled 0); the page is inspected again and the fill runs
    once on that reading, each field written once, no second approval."""
    out, values, events = _auto_commit("auto_remounted", tmp_path, monkeypatch)
    assert out == {"success": True, "filled_fields": 3, "retention_verified": True,
                   "fill_reinspect": {"count": 1, "repairs": 0}}, out
    assert {k: v for k, v in values.items() if v} == {"exp2": "07/35", "csc2": "123", "num2": "4111111111111111"}
    assert events.count("commit") == 2 and events.count("reinspect") == 1, events


def test_a_cleared_cvc_gets_one_repair_pass_and_held_fields_are_not_written_again(tmp_path, monkeypatch):
    """11-13 Eurowings: retention finds the CVC empty; one re-inspection, then only the CVC is written again."""
    out, values, events = _auto_commit("auto_cvc_cleared", tmp_path, monkeypatch)
    assert out["success"] and out["retention_verified"] is True, out
    assert out["fill_reinspect"] == {"count": 1, "repairs": 1} and values == _FILLED, (out, sorted(values))
    assert events.count("reinspect") == 1 and events.count("commit") == 2


def test_a_cvc_that_never_keeps_its_value_stops_after_the_one_repair(tmp_path, monkeypatch):
    out, values, events = _auto_commit("auto_cvc_never", tmp_path, monkeypatch)
    assert out["error_type"] == "payment_fields_not_retained" and out["partially_filled"] is True, out
    assert out["field_retention"] == {"cc-csc": "empty", "cc-exp": "held", "cc-number": "held"}
    assert out["fill_reinspect"] == {"count": 1, "repairs": 1, "refused": "budget"}, out
    assert events.count("reinspect") == 1 and values["csc"] == ""


def test_the_unarmed_commit_never_takes_the_armed_path(tmp_path, monkeypatch):
    """vault.payment_fill_auto off (or a supervisor that could not arm): refill is ignored, the base commit runs."""
    js = _NodeClosure("auto_relabel", tmp_path, _SCENARIOS)
    try:
        from tools.browser_vault_payment import _document_fills

        sup = _ClosureSupervisor(js)
        doc = PaymentDocument("card", "s", "o-card", PSP, sp._card_controls(js.call("describe")),
                              clock_anchor=time.monotonic())
        i = PaymentInspection("op", "s", "tab", MERCHANT, (doc,), (("card", "L", PSP + "/card", PSP, ""),),
                              time.monotonic() + 60, owner=sup)
        monkeypatch.setattr(sp, "_SETTLE_SECONDS", 0)
        out = asyncio.run(sup._commit_payment(i, _document_fills(i, _SYNTHETIC), None, False, False,
                                              lambda fresh: pytest.fail("refill"), None))
        # the first focus re-labels the other fields: the base commit stops there, before any write
        assert out["error_type"] == "payment_fields_changed" and out["filled_fields"] == 0, out
        assert out["fill_check"]["reason"] == "fields_relabeled" and not any(js.call("values").values())
        assert sup.events == ["commit"] and sup.calls[0][1] == 4 and "fill_reinspect" not in out
    finally:
        js.close()


# -- What the approval binds --

def _reading(fields, *, top=("top", "L", MERCHANT + "/pay", MERCHANT, ""), page="tab", unreviewed=(), cart=(),
             free=(), **more):
    """``fields``: ``(origin, tokens)`` or ``(origin, tokens, frame id)``; ``free``: ``(origin, frame id)`` of card-free
    admissible documents."""
    docs = tuple(PaymentDocument(f[2] if len(f) > 2 else f"f{n}", "s", f"o{n}", f[0], _controls(*f[1]))
                 for n, f in enumerate(fields))
    tree = (top, *((d.frame_id, "L" + d.frame_id, d.origin + "/card", d.origin, "top") for d in docs),
            *((fid, "L" + fid, origin + "/promo", origin, "top") for origin, fid in free))
    return PaymentInspection(more.pop("operation", "op"), "s", page, MERCHANT, docs, tree, time.monotonic() + 60,
                             unreviewed_origins=tuple(unreviewed), cart_pins=tuple(cart), auto=True,
                             approval_id="op", free_frames=tuple(fid for _o, fid in free), **more)


_APPROVED = [(MERCHANT, ("cc-exp",)), (PSP, ("cc-number", "cc-csc"))]


def _pin(labelled=(2266,), stated=1, unclear=False, state="matched"):
    """One witnessed document's pin, as the supervisor stores it (``cart_pins``)."""
    return json.dumps({"state": state, "labelled": list(labelled), "stated": stated, "unclear": unclear}, sort_keys=True)


@pytest.mark.parametrize("change, refused", [
    ({}, None),
    ({"fields": [(MERCHANT, ("cc-exp",)), (PSP, ("cc-number",)), (PSP, ("cc-csc",))]}, "documents_changed"),  # 1 -> 2 frames
    ({"top": ("top", "L2", MERCHANT + "/pay", MERCHANT, "")}, "navigated"),                    # reloaded
    ({"top": ("top", "L", MERCHANT + "/pay?step=2", MERCHANT, "")}, "navigated"),
    ({"page": "tab2"}, "navigated"),
    ({"fields": [(MERCHANT, ("cc-exp",)), ("https://evil.example", ("cc-number", "cc-csc"))]}, "origin_changed"),
    ({"fields": _APPROVED, "unreviewed": (PSP,)}, "origin_changed"),
    ({"fields": [(MERCHANT, ("cc-exp", "cc-csc")), (PSP, ("cc-number",))]}, "fields_changed"),  # a token moved origin
    ({"fields": [(PSP, ("cc-number", "cc-csc"))]}, "fields_changed"),                           # fewer: still rendering
    ({"fields": [(MERCHANT, ("cc-exp", "cc-name")), (PSP, ("cc-number", "cc-csc"))]}, "fields_changed"),
    ({"cart": (("merchant page", _pin(labelled=[])),)}, "cart_changed"),
    ({"cart": (("merchant page", _pin(labelled=[2266, 5])),)}, "cart_changed"),
])
def test_a_re_inspection_must_show_the_checkout_the_owner_approved(change, refused):
    approval = spa._approval_facts(_reading(_APPROVED, cart=(("merchant page", _pin()),)))
    fresh = _reading(**{"fields": _APPROVED, "cart": (("merchant page", _pin()),), **change})
    assert spa._approval_mismatch(approval, fresh) == refused


class _Loop(sp.PaymentSupervisionMixin):
    """``_reinspect`` over scripted readings: an exception, or a callable ``prior -> PaymentInspection``; the last
    one repeats."""

    def __init__(self, readings, current=True, roles=None):
        self.readings, self.current, self.roles, self.released = list(readings), current, roles, []

    async def _inspect_payment(self, origin, bound, allowed, unreviewed=False, cart=None, prior=None):
        assert origin == "" and prior is not None
        reading = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
        if isinstance(reading, Exception):
            raise reading
        return reading(prior)

    async def _payment_is_current(self, i, during_fill=False):
        return self.current

    async def _remember_roles(self, i, earlier):
        return self.roles

    async def _release_payment_documents(self, i):
        self.released.append((i.operation_id, dict(i.child_sessions)))


def _loop(readings, monkeypatch, wait=5.0, prior_more=None, **answers):
    monkeypatch.setattr(spa, "_REINSPECT_POLL_SECONDS", 0)
    monkeypatch.setattr(spa, "_REINSPECT_SETTLE_SECONDS", 0)
    sup = _Loop(readings, **answers)
    prior = _reading(_APPROVED, operation="prior", child_sessions={"f1": "shared"}, **(prior_more or {}))
    prior.approval = spa._approval_facts(prior)
    fresh, refused = asyncio.run(sup._reinspect(prior, time.monotonic() + wait, wait))
    return sup, prior, fresh, refused


def _fresh(fields, operation, **more):
    return lambda prior: _reading(fields, operation=operation, approval=prior.approval,
                                  child_sessions={**prior.child_sessions, "new": operation}, **more)


def test_a_re_inspection_waits_for_a_complete_form_and_takes_over_the_shared_frame_sessions(monkeypatch):
    """A form still rendering (nothing complete, then fewer fields than approved) is read again; the matching reading
    continues the approval: same window, the earlier inspection released without its frame sessions, and a rejected
    reading released with only the sessions it attached itself."""
    sup, prior, fresh, refused = _loop([
        PaymentPolicyError("Incomplete or ambiguous card fields (no reviewed provider frame)"),
        PaymentDestinationError("payment_fields_unsupported", "x"),
        _fresh([(PSP, ("cc-number", "cc-csc"))], "partial"),
        _fresh(_APPROVED, "match"),
    ], monkeypatch)
    assert refused == "" and fresh.operation_id == "match" and fresh.expires_at == pytest.approx(prior.expires_at, abs=1)
    assert sup.released == [("partial", {"new": "partial"}), ("prior", {})]
    assert fresh.child_sessions == {"f1": "shared", "new": "match"} and prior.consumed and not fresh.consumed


@pytest.mark.parametrize("readings, answers, refused, left", [
    ([_fresh([(MERCHANT, ("cc-exp",)), ("https://evil.example", ("cc-number", "cc-csc"))], "x"), None], {}, "origin_changed", 1),
    ([_fresh(_APPROVED, "x", page="other"), None], {}, "navigated", 1),
    ([PaymentPolicyError("Merchant origin mismatch"), None], {}, "navigated", 1),
    ([_fresh(_APPROVED, "x"), None], {"roles": "role_changed"}, "role_changed", 1),
    ([_fresh(_APPROVED, "x"), None], {"roles": "role_unproven"}, "role_unproven", 1),
    ([PaymentPolicyError("Missing card expiry fields")], {"wait": 0.05}, "incomplete", None),
    ([_fresh(_APPROVED, "x")], {"wait": 0.05, "current": False}, "unsettled", None),
    ([RuntimeError("ws closed")], {"wait": 0.05}, "unavailable", None),
    ([], {"wait": 0}, "too_late", 0),
])
def test_a_re_inspection_that_does_not_match_never_continues_the_fill(readings, answers, refused, left, monkeypatch):
    """Another origin, another page, a moved role: refused at once. An incomplete, unsettled or unreadable form:
    read again until the wait is over, then refused. The earlier inspection is left for its caller either way."""
    wait = answers.pop("wait", 5.0)
    sup, prior, fresh, code = _loop(readings, monkeypatch, wait=wait, **answers)
    assert fresh is None and code == refused and not prior.consumed
    assert left is None or len(sup.readings) == left
    assert all(operation != "prior" for operation, _sessions in sup.released)
    assert prior.child_sessions == {"f1": "shared"}


def test_the_armed_result_counts_writes_and_names_the_refusal():
    st = {"reinspections": 1, "repairs": 0, "refused": "incomplete", "wrote": 2}
    out = spa._auto_result({"success": False, "error_type": "payment_page_changed", "filled_fields": 0}, st)
    assert out == {"success": False, "error_type": "payment_page_changed", "filled_fields": 2, "partially_filled": True,
                   "fill_reinspect": {"count": 1, "repairs": 0, "refused": "incomplete"}}
    assert spa._auto_result({"success": True, "filled_fields": 3}, {**st, "reinspections": 0, "refused": ""}) == {
        "success": True, "filled_fields": 3}
    assert "refused" not in spa._auto_result({"success": True}, {**st, "refused": "<b>"})["fill_reinspect"]


# -- The vault: a checkout that changed during the approval is re-inspected before any card byte is read --

@pytest.mark.parametrize("switch, armed, code, expected", [
    (True, True, "origin_changed", "payment_page_changed"),
    (True, True, "cart_changed", "payment_cart_changed"),
    (False, True, None, "payment_page_changed"),   # switched off: never armed, never re-inspected
    (True, False, None, "payment_page_changed"),   # the supervisor could not arm: the fill before 11-13
])
def test_fill_payment_re_inspects_before_the_card_is_read_or_refuses(switch, armed, code, expected, monkeypatch):
    from tests.tools.test_browser_vault_payment_split import CARD, _candidate, _card_documents, _inspection
    from tools import browser_vault_payment as bvp

    inspection = _inspection(*_card_documents([_candidate(MERCHANT, "cc-number", "cc-exp", "cc-csc")], MERCHANT))
    calls = []

    class Supervisor:
        def inspect_payment(self, *a, **k):
            return inspection

        def arm_payment_auto(self, i):
            calls.append("arm")
            return armed

        def payment_is_current(self, i):
            return False

        def reinspect_payment(self, i):
            calls.append("reinspect")
            return None, code

        def commit_payment(self, *a, **k):
            pytest.fail("no commit")

        def discard_payment(self, i):
            calls.append("discard")

    monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor", lambda task_id: Supervisor())
    monkeypatch.setattr("tools.browser_vault_tool._bot_desktop_browser_session", lambda task_id: False)
    monkeypatch.setattr("tools.browser_vault_payment.payment_fill_auto_enabled", lambda: switch)
    monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", lambda *a, **k: "accept")
    meta = SimpleNamespace(id="h", kind="payment", label="Card", allowed_origins=(), origin=None,
                           payment_scope="confirmed_page", payment_frame_origins=())
    backend = SimpleNamespace(name="local", resolve_secret=lambda handle: pytest.fail("card read"),
                              get_meta=lambda handle: meta)
    out = json.loads(bvp.fill_payment(backend, meta, "task"))
    assert out["error_type"] == expected and out["success"] is False and CARD["card_number"] not in json.dumps(out)
    assert calls == (["arm"] if switch else []) + (["reinspect"] if code else []) + ["discard"], calls
    assert out.get("fill_reinspect") == ({"count": 0, "repairs": 0, "refused": code} if code else None)


# -- Round 2 (r1 review findings F1-F9) --

_HOLDER = "https://john-q-public-4111.evil.example"  # a page-chosen origin that carries a (synthetic) holder name


def test_a_refused_re_inspection_never_copies_a_page_chosen_origin_into_the_result_or_the_log(monkeypatch, caplog):
    """F1 (Codex P1-1, peer P2-1): values are in the page when a repair re-inspects; the page chooses its origins."""
    import logging

    from tools import browser_vault_payment as bvp

    sup, prior, fresh, refused = _loop([_fresh([(MERCHANT, ("cc-exp",)), (_HOLDER, ("cc-number", "cc-csc"))], "x"), None],
                                       monkeypatch)
    assert fresh is None and refused == "origin_changed"
    assert prior.approval["seen"] == {MERCHANT: ["cc-exp"], "other": 1}
    # the discovery pass reports its origins in the error's detail: same rule
    detail = {"receiving_origins": [{"origin": PSP, "fields": ["cc-number"]}, {"origin": _HOLDER, "fields": ["cc-csc"]}]}
    sup, prior, fresh, refused = _loop([PaymentDestinationError("payment_fields_ambiguous", "x", detail)], monkeypatch,
                                       wait=0.05)
    assert prior.approval["seen"] == {PSP: ["cc-number"], "other": 1}
    st = {"reinspections": 0, "repairs": 0, "refused": "origin_changed", "wrote": 3, "seen": prior.approval["seen"]}
    out = spa._auto_result({"success": False, "error_type": "payment_page_changed"}, st)
    with caplog.at_level(logging.INFO, logger="tools.browser_vault_payment"):
        bvp._log_commit(out)
    assert "evil" not in json.dumps(out) + caplog.text and "john" not in json.dumps(out) + caplog.text
    assert "other 1" in caplog.text and PSP in caplog.text


def test_a_rejected_re_inspection_releases_only_the_sessions_it_attached_by_session_not_by_frame(monkeypatch):
    """F6 (Codex P2-6, peer P2-4, GLM P3-1): a new child frame shares its parent's session; the frame key is new, the
    session is the earlier inspection's and must stay attached."""
    shared = lambda prior: _reading(_APPROVED, operation="x", approval=prior.approval,   # noqa: E731
                                    child_sessions={"f1": "shared", "child": "shared", "mine": "own"}, page="other")
    sup, prior, fresh, refused = _loop([shared, None], monkeypatch)
    assert fresh is None and refused == "navigated"
    assert sup.released == [("x", {"mine": "own"})] and prior.child_sessions == {"f1": "shared"}


def test_inspect_payment_cleanup_keeps_the_sessions_the_earlier_inspection_uses(monkeypatch):
    """F6, the other place: ``_inspect_payment(prior=)`` failing midway detaches only sessions it attached itself."""
    import threading

    detached = []

    class Supervisor(sp.PaymentSupervisionMixin):
        _state_lock, _page_session_id, _active = threading.Lock(), "page", True

        async def _cdp(self, method, params=None, session_id=None, timeout=None):
            return {"result": {"targetInfo": {}}}

        async def _payment_tree(self, sid, child_sessions):
            child_sessions["child"] = "shared"  # a same-process child frame maps to its parent's session
            child_sessions["mine"] = "own"
            return (("top", "L", MERCHANT + "/pay", MERCHANT, ""),)

        async def _inspect_frames(self, *args):
            raise PaymentPolicyError("Missing card expiry fields")

        async def _release_all(self, objects, sessions):
            detached.extend(sessions.values())

    prior = _reading(_APPROVED, child_sessions={"f1": "shared"}, owner=None)
    sup = Supervisor()
    prior.owner, prior.page_session = sup, "page"
    with pytest.raises(PaymentPolicyError):
        asyncio.run(sup._inspect_payment("", (), (), False, None, prior=prior))
    assert detached == ["own"]


def test_a_cancelled_re_inspection_releases_the_fresh_reading(monkeypatch):
    """F5 (Codex P2-5, peer P3-1, GLM P3-2): a reading that was read but not yet handed over is not leaked when the
    commit is cancelled (its timeout) while it settles."""
    monkeypatch.setattr(spa, "_REINSPECT_POLL_SECONDS", 0)
    monkeypatch.setattr(spa, "_REINSPECT_SETTLE_SECONDS", 30)  # cancelled in the settle wait
    sup = _Loop([_fresh(_APPROVED, "x")])
    prior = _reading(_APPROVED, operation="prior", child_sessions={"f1": "shared"})
    prior.approval = spa._approval_facts(prior)

    async def main():
        task = asyncio.ensure_future(sup._reinspect(prior, time.monotonic() + 60, 60))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert sup.released == [("x", {"new": "x"})] and not prior.consumed and prior.child_sessions == {"f1": "shared"}


def test_a_commit_cancelled_while_the_earlier_reading_is_released_still_releases_the_fresh_one(monkeypatch):
    """F5: the hand-over of the shared sessions is one step; a cancellation in the earlier reading's release leaves
    the fresh reading released (all its sessions: the earlier one no longer uses any)."""
    monkeypatch.setattr(spa, "_REINSPECT_POLL_SECONDS", 0)
    monkeypatch.setattr(spa, "_REINSPECT_SETTLE_SECONDS", 0)
    sup = _Loop([_fresh(_APPROVED, "x")])
    prior = _reading(_APPROVED, operation="prior", child_sessions={"f1": "shared"})
    prior.approval = spa._approval_facts(prior)
    slow = sup._release_payment_documents

    async def release(i):
        if i.operation_id == "prior":
            await asyncio.sleep(30)  # the earlier reading's release is slow
        await slow(i)

    sup._release_payment_documents = release

    async def main():
        task = asyncio.ensure_future(sup._reinspect(prior, time.monotonic() + 60, 60))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)

    asyncio.run(main())
    assert sup.released == [("x", {"f1": "shared", "new": "x"})]


def test_cart_changed_mid_render_is_read_again_and_adopted_only_on_an_equal_cart(monkeypatch):
    """F9 (peer P3-3): a Payrails reading caught mid-render lacks its amount text; it is read again."""
    pins = (("merchant page", _pin()), ("payment form", _pin()))
    mid_render = _fresh(_APPROVED, "mid", cart=(("merchant page", _pin()), ("payment form", _pin(labelled=[]))))
    sup, prior, fresh, refused = _loop([mid_render, _fresh(_APPROVED, "ok", cart=pins)], monkeypatch,
                                       prior_more={"cart": pins})
    assert refused == "" and fresh.operation_id == "ok"
    sup, prior, fresh, refused = _loop([mid_render], monkeypatch, wait=0.05, prior_more={"cart": pins})
    assert fresh is None and refused == "cart_changed" and not prior.consumed  # still not the approved cart: no adoption


def test_two_same_origin_documents_re_shaped_into_one_are_the_same_cart_reading():
    """F9 (GLM P3-3): the witnessed place is the same; only the number of documents under it changed."""
    two = _reading([(MERCHANT, ("cc-exp",)), (PSP, ("cc-number",)), (PSP, ("cc-csc",))],
                   cart=(("merchant page", _pin()), ("payment form", _pin()), ("payment form", _pin(labelled=[], stated=0))))
    one = _reading([(MERCHANT, ("cc-exp",)), (PSP, ("cc-number", "cc-csc"))],
                   cart=(("merchant page", _pin()), ("payment form", _pin())))
    assert spa._cart_reading(two) == spa._cart_reading(one)
    assert spa._cart_reading(one) != spa._cart_reading(_reading(_APPROVED, cart=(("merchant page", _pin(labelled=[])),)))


@pytest.mark.parametrize("fresh_url, refused", [
    (PSP + "/card?mid=ATTACKER&txn=B#frag", None),             # query and fragment are re-minted on each render
    (PSP + "/other", "documents_changed"),                      # another path on an approved origin
    (PSP + "/card/../checkout", "documents_changed"),
])
def test_a_card_document_must_be_at_a_path_the_approval_showed(fresh_url, refused):
    """F8 (peer P2-2): the approval binds origin and path of each card document, not its session query."""
    approval = spa._approval_facts(_reading(_APPROVED))
    fresh = _reading(_APPROVED)
    tree = tuple((f[0], f[1], fresh_url, *f[3:]) if f[3] == PSP else f for f in fresh.tree)
    assert spa._approval_mismatch(approval, replace(fresh, tree=tree)) == refused


def test_a_re_inspection_may_not_have_more_card_documents_than_approved_on_an_origin():
    approval = spa._approval_facts(_reading([(MERCHANT, ("cc-exp",)), (PSP, ("cc-number",), "a"), (PSP, ("cc-csc",), "b")]))
    same_count = _reading([(MERCHANT, ("cc-exp",)), (PSP, ("cc-number",), "c"), (PSP, ("cc-csc",), "d")])
    fewer = _reading([(MERCHANT, ("cc-exp",)), (PSP, ("cc-number", "cc-csc"), "c")])
    assert [spa._approval_mismatch(approval, r) for r in (same_count, fewer)] == [None, None]
    three = _reading([(MERCHANT, ("cc-exp",)), (PSP, ("cc-number",), "c"), (PSP, ("cc-csc",), "d"), (PSP, ("cc-csc",), "e")])
    assert spa._approval_mismatch(approval, three) == "documents_changed"


def test_a_document_that_held_no_card_field_may_not_become_card_bearing():
    """F2 (Codex P1-2, peer P3-4): the card form's frame goes away and a sibling's promo/address controls are
    re-labelled as the card fields; the sibling never had a role record as a card document."""
    approved = _reading([(MERCHANT, ("cc-exp",)), (PSP, ("cc-number", "cc-csc"), "a")], free=[(PSP, "b")])
    approval = spa._approval_facts(approved)
    assert approval["free"] == {("b", "Lb")} and approval["documents"] == {("a", "La"), ("f0", "Lf0")}
    reclassified = _reading([(MERCHANT, ("cc-exp",)), (PSP, ("cc-number", "cc-csc"), "b")])
    assert spa._approval_mismatch(approval, reclassified) == "role_changed"
    brand_new = _reading([(MERCHANT, ("cc-exp",)), (PSP, ("cc-number", "cc-csc"), "c")])
    assert spa._approval_mismatch(approval, brand_new) is None  # a new document inside the same binding
    still = _reading([(MERCHANT, ("cc-exp",)), (PSP, ("cc-number", "cc-csc"), "a")], free=[(PSP, "b")])
    assert spa._approval_mismatch(approval, still) is None


def test_an_adopted_reading_extends_the_free_documents(monkeypatch):
    """A document that was free in an adopted reading may not become card-bearing in the next one either."""
    sup, prior, fresh, refused = _loop([lambda p: _reading(_APPROVED, operation="ok", approval=p.approval,
                                                          free=[(PSP, "fX")])], monkeypatch)
    assert refused == "" and prior.approval["free"] == {("fX", "LfX")}


def test_the_reviewed_and_the_discovery_pass_both_name_the_card_free_documents():
    """F2: the inspection records the frame of every admissible document it found without a card field."""
    rows = {"o-a": [{"index": 0, "autocomplete": "cc-number", "name": "", "label": "", "type": "text", "formIndex": None,
                     "maxLength": None}], "o-b": [], "o-c": [], "o-top": []}

    class Supervisor(sp.PaymentSupervisionMixin):
        async def _frame_inspectable(self, record, *args):
            return "reviewed" if record[0] != "skipped" else ""

        async def _cdp(self, method, params=None, session_id=None, timeout=None):
            if method == "Page.createIsolatedWorld":
                return {"result": {"executionContextId": params["frameId"]}}
            if method == "Runtime.evaluate":
                return {"result": {"result": {"objectId": "o-" + params["contextId"]}}}
            return {"result": {"result": {"value": rows[params["objectId"]]}}}

        async def _release_payment_object(self, sid, obj):
            pass

    tree = tuple((fid, "L" + fid, MERCHANT + "/x", MERCHANT, "" if fid == "top" else "top")
                 for fid in ("top", "a", "b", "skipped", "c"))
    found = {"candidates": [], "anchors": {}, "absence": [], "witnesses": [], "owners": {}, "pending": [], "free": []}
    asyncio.run(Supervisor()._inspect_frames(tree, MERCHANT, (), False, "s", {}, "op", found))
    assert [c[0] for c in found["candidates"]] == ["a"] and found["free"] == ["top", "b", "c"]  # not the skipped one


def test_an_accepted_relabel_goes_through_the_owner_chain_check_again_before_the_setter(tmp_path, monkeypatch):
    """F3 (Codex P1-3, peer P2-3): the relabel pause is asynchronous; the cross-origin owner chain (or the cart) can
    change in it. After the acceptance the focused, paced field is checked again before anything is written."""
    out, values, events = _auto_commit("auto_focus_relabel", tmp_path, monkeypatch, strict=True, parent=True,
                                       owner=lambda n: n < 2)  # the check after the focus passes, the next one fails
    assert out["success"] is False and out["error_type"] == "payment_page_changed", out
    # focus, check (passes), relabel pause, accepted, check again (fails): nothing written; the re-inspections that
    # follow (a changed page is read again) stop the same way at their first check
    assert events[:9] == ["remember", "commit", "owner_check", "resume", "relabeled", "accept", "resume", "owner_check",
                          "reinspect"], events
    assert not any(values.values()) and out.get("filled_fields", 0) == 0, (values, out)
    # the same page with a check that holds: written, one more check than fields' own focus checks
    out, values, events = _auto_commit("auto_focus_relabel", tmp_path, monkeypatch, strict=True, parent=True)
    assert out["success"] is True and values == _FILLED and events.count("owner_check") == 4, (out, events)


def test_a_field_adopted_through_a_re_inspection_must_be_exposed_even_on_a_reviewed_destination(tmp_path, monkeypatch):
    """F4 (Codex P2-4, peer P3-5): the first inspection of a reviewed form stays as it was (no overlay test); a field
    the first inspection never saw (the form re-mounted during the approval) is hit-tested, locally and, in a frame,
    from the parents."""
    out, values, events = _auto_commit("auto_remounted_covered", tmp_path, monkeypatch)
    assert out["success"] is False and out["error_type"] == "payment_fields_changed", out
    assert out["fill_check"]["reason"] == "hit_test" and out["fill_check"]["token"] == "cc-exp", out
    assert values["exp2"] == "" and values["num2"] == "" and out["fill_reinspect"]["count"] >= 1  # never written
    # uncovered, in a frame: written, and every write of the adopted generation had its owner-chain check
    out, values, events = _auto_commit("auto_remounted", tmp_path, monkeypatch, parent=True)
    assert out["success"] is True and events.count("owner_check") == 3 and values["exp2"] == "07/35", (out, events)


def test_a_first_generation_reviewed_fill_has_no_owner_chain_check_as_before(tmp_path, monkeypatch):
    out, values, events = _auto_commit("auto_relabel", tmp_path, monkeypatch, parent=True)
    assert out["success"] is True and "owner_check" not in events


@pytest.mark.parametrize("scenario, kept", [("auto_kept_events", {"exp", "num"}), ("auto_kept_select", {"month", "year", "num"})])
def test_a_kept_field_still_gets_the_events_of_a_write(scenario, kept, tmp_path, monkeypatch):
    """F14 (GLM P3-4): a field that already holds its value is not written again, but the page's own state may not
    have it (a controlled field re-mounted with the value): input and change are dispatched, selects included."""
    out, values, events = _auto_commit(scenario, tmp_path, monkeypatch)
    assert out["success"] is True and out["fill_reinspect"] == {"count": 1, "repairs": 1}, out
    log = values["log"].split()
    for field in kept:  # once for its write in the first generation, once more, as no-ops, in the repair generation
        assert log.count(field + ":change") == 2 and log.count(field + ":input") == 2, (field, log)
    assert log.count("csc:change") == 2 and log.count("csc:input") == 2, log  # the cleared one: written again
