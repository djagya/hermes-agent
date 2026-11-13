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
from tests.tools.test_browser_payment_repin import (MERCHANT, PSP, _SCENARIOS, _SYNTHETIC, _ClosureSupervisor,
                                                    _controls, _NodeClosure)
from tools import browser_supervisor_payment as sp
from tools import browser_supervisor_payment_auto as spa
from tools.browser_supervisor_payment import PaymentDocument, PaymentInspection

_FIELDS = """
const exp = input({id: 'exp', autocomplete: 'cc-exp', aria: 'Expiry'}), csc = input({id: 'csc', autocomplete: 'cc-csc', aria: 'CVC'});
const num = input({id: 'num', autocomplete: 'cc-number', aria: 'Card number'});
els = [exp, csc, num];
const twice = e => { if (e.seen) e.held = 'TWICE'; e.seen = true; };  // a second write of one field shows"""
_SCENARIOS.update({
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
num.oninput = () => { twice(num); csc.held = ''; csc.seen = false; };""",
    # a CVC that never keeps its value
    "auto_cvc_never": _FIELDS + """
csc.oninput = () => { csc.held = ''; };""",
    # FlixBus / Payrails: while the approval is open the whole form is built again from fresh elements
    "auto_remounted": _FIELDS + """
for (const e of els) e.oninput = () => twice(e);
approve = () => { for (const e of [exp, csc, num]) { const n = replace(e); n.aria = e.aria; n.oninput = () => twice(n); } };""",
    # a field that changes type (not a label): never accepted in place, inspected again
    "auto_retyped": _FIELDS + """
csc.oninput = () => { exp.type = 'tel'; };""",
})
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


def _auto_commit(scenario, tmp_path, monkeypatch):
    from tools.browser_vault_payment import _document_fills

    js = _NodeClosure(scenario, tmp_path)
    try:
        sup = _AutoSupervisor(js)
        doc = PaymentDocument("card", "s", "o-card", PSP, sp._card_controls(js.call("describe")),
                              clock_anchor=time.monotonic())
        i = PaymentInspection("op", "s", "tab", MERCHANT, (doc,), (("card", "L", PSP + "/card", PSP, ""),),
                              time.monotonic() + 60, owner=sup, approval_id="op")
        assert asyncio.run(sup._arm_payment_auto(i)) is True and i.auto is True
        js.call("approve")
        monkeypatch.setattr(sp, "_SETTLE_SECONDS", 0)
        out = asyncio.run(sup._commit_payment_auto(i, _document_fills(i, _SYNTHETIC), None, False,
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
    ("replacement", None),                    # a re-mounted expiry input: inspected again, written
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
        assert {k: v for k, v in values.items() if v} == {("exp2" if scenario == "replacement" else "exp"): "07/35",
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
    js = _NodeClosure("auto_relabel", tmp_path)
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

def _reading(fields, *, top=("top", "L", MERCHANT + "/pay", MERCHANT, ""), page="tab", unreviewed=(), cart=(), **more):
    docs = tuple(PaymentDocument(f"f{n}", "s", f"o{n}", origin, _controls(*tokens))
                 for n, (origin, tokens) in enumerate(fields))
    tree = (top, *((d.frame_id, "L" + d.frame_id, d.origin + "/card", d.origin, "top") for d in docs))
    return PaymentInspection(more.pop("operation", "op"), "s", page, MERCHANT, docs, tree, time.monotonic() + 60,
                             unreviewed_origins=tuple(unreviewed), cart_pins=tuple(cart), auto=True,
                             approval_id="op", **more)


_APPROVED = [(MERCHANT, ("cc-exp",)), (PSP, ("cc-number", "cc-csc"))]


@pytest.mark.parametrize("change, refused", [
    ({}, None),
    ({"fields": [(MERCHANT, ("cc-exp",)), (PSP, ("cc-number",)), (PSP, ("cc-csc",))]}, None),  # other frames, same origins
    ({"top": ("top", "L2", MERCHANT + "/pay", MERCHANT, "")}, "navigated"),                    # reloaded
    ({"top": ("top", "L", MERCHANT + "/pay?step=2", MERCHANT, "")}, "navigated"),
    ({"page": "tab2"}, "navigated"),
    ({"fields": [(MERCHANT, ("cc-exp",)), ("https://evil.example", ("cc-number", "cc-csc"))]}, "origin_changed"),
    ({"fields": _APPROVED, "unreviewed": (PSP,)}, "origin_changed"),
    ({"fields": [(MERCHANT, ("cc-exp", "cc-csc")), (PSP, ("cc-number",))]}, "fields_changed"),  # a token moved origin
    ({"fields": [(PSP, ("cc-number", "cc-csc"))]}, "fields_changed"),                           # fewer: still rendering
    ({"fields": [(MERCHANT, ("cc-exp", "cc-name")), (PSP, ("cc-number", "cc-csc"))]}, "fields_changed"),
    ({"cart": (("merchant page", '{"state": "ambiguous"}'),)}, "cart_changed"),
])
def test_a_re_inspection_must_show_the_checkout_the_owner_approved(change, refused):
    approval = spa._approval_facts(_reading(_APPROVED, cart=(("merchant page", '{"state": "matched"}'),)))
    fresh = _reading(**{"fields": _APPROVED, "cart": (("merchant page", '{"state": "matched"}'),), **change})
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


def _loop(readings, monkeypatch, wait=5.0, **answers):
    monkeypatch.setattr(spa, "_REINSPECT_POLL_SECONDS", 0)
    monkeypatch.setattr(spa, "_REINSPECT_SETTLE_SECONDS", 0)
    sup = _Loop(readings, **answers)
    prior = _reading(_APPROVED, operation="prior", child_sessions={"f1": "shared"})
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
