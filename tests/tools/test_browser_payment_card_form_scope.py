"""Which fields a card fill counts as the card's form, and what its result describes (build B r4, 2026-11-18).

Review findings of the card-fill results: a checkout without a ``<form>``, a postcode dropped because two documents
have one, a shipping postcode, the reading an armed fill finished on, a holder typed before the fill, masking of
results that name no slot, and no re-export shim. Owner rule: never report ``form_complete: true`` while a field of
the card's form is empty; otherwise make the fill work. Synthetic cards and pages only.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from tests.tools.test_browser_payment_fill_results import CARD, MERCHANT, PSP, _document, _FORM, _inspection, _report, _row
from tools import browser_supervisor_payment as sp
from tools import browser_supervisor_payment_results as rs
from tools import browser_vault_payment as bvp
from tools.browser_supervisor_payment import PaymentDocument, PaymentInspection

_FORMLESS = [dict(r, formIndex=None) for r in _FORM]


# --- 1: a checkout without a <form> ------------------------------------------------------------------------------


def test_a_formless_checkout_counts_its_formless_fields_as_the_card_form():
    rows = [*_FORMLESS, _row(3, "first", "First name", form=None), _row(4, "q", "Search", form=1)]
    document = _document(rows, blank=frozenset({3, 4}))
    assert document.left_for_user == (3,)  # the field of an unrelated form (the header search) stays out
    out = _report(document, CARD)
    assert out["form_complete"] is False and out["fields_left_empty"]["other_fields"] == 1
    # the same field holding a value: nothing is left
    assert _report(_document(rows, blank=frozenset({4})), CARD) == {"form_complete": True}
    # a reading that cannot say which are blank counts them all
    assert _report(_document(rows), CARD)["form_complete"] is False


@pytest.mark.parametrize("name,label,kind", [("q", "Search", "text"), ("s", "", "search"), ("coupon", "Coupon code", "text"),
                                             ("giftCard", "Gift card", "text"), ("nl", "Newsletter e-mail", "email"),
                                             ("gutschein", "Gutscheincode", "text")])
def test_formless_page_chrome_is_not_left_for_the_user(name, label, kind):
    """Owner direction ("make it all work"): with no <form> around the card, an empty search, coupon, gift-card or
    newsletter box beside it is page chrome, not checkout: it does not hold Pay. A real empty field still does."""
    chrome = dict(_row(3, name, label, form=None), type=kind)
    document = _document([*_FORMLESS, chrome], blank=frozenset({3}))
    assert document.left_for_user == ()
    assert _report(document, CARD) == {"form_complete": True}
    real = _document([*_FORMLESS, chrome, _row(4, "lastName", "Last name", form=None)], blank=frozenset({3, 4}))
    assert real.left_for_user == (4,)


def test_a_formless_billing_postcode_joins_formless_card_fields_but_not_a_form_beside_them():
    assert [c.token for c in sp._card_controls([*_FORMLESS, _row(3, "zip", "Billing ZIP", form=None)], True)][-1] \
        == "postal-code"
    assert "postal-code" not in [c.token for c in sp._card_controls([*_FORMLESS, _row(3, "zip", "ZIP", form=1)], True)]
    # card fields inside a form: a formless postcode still stays out (as before)
    assert "postal-code" not in [c.token for c in sp._card_controls([*_FORM, _row(3, "zip", "ZIP", form=None)], True)]


# --- 2: a postcode dropped because two documents have one --------------------------------------------------------


def _two_postcode_page(blanks):
    from tests.tools.test_browser_vault_payment_destination import _CARD_FORM, KPS, STRIPE, _desc, _DiscoveryPage

    merchant = [_row(0, "holder", "Name on card"), _row(1, "zip", "Billing postcode")]
    hosted = [*_CARD_FORM[1:], _desc(len(_CARD_FORM), "postal-code")]  # number, csc, expiry, ZIP (no holder)

    class Page(_DiscoveryPage):
        async def _cdp(self, method, params=None, session_id=None, timeout=None):
            if method == "Runtime.callFunctionOn" and "blanks()" in (params or {}).get("functionDeclaration", ""):
                return {"result": {"result": {"value": blanks.get(params["objectId"][4:])}}}
            return await super()._cdp(method, params, session_id, timeout)

    frames = (("top", "L0", MERCHANT + "/checkout", MERCHANT, ""), ("kps", "L2", KPS + "/pay", KPS, "top"))
    return Page(frames, exposed={"kps"}, controls={"top": merchant, "kps": hosted}), STRIPE


def test_a_postcode_dropped_for_being_in_two_documents_is_left_for_the_user_and_counted():
    page, stripe = _two_postcode_page({"top": [0, 1], "kps": [5]})
    i = asyncio.run(page._inspect_payment("", (), (stripe,), unreviewed=True, cart=(2266, "EUR")))
    by_frame = {d.frame_id: d for d in i.documents}
    assert all("postal-code" not in [c.token for c in d.controls] for d in i.documents)
    assert 1 in by_frame["top"].left_for_user and by_frame["top"].blank >= {1}
    assert any(n in by_frame["kps"].blank for n in by_frame["kps"].left_for_user)
    out = bvp._form_report({"success": True}, i, [], locked=False)
    assert out["form_complete"] is False and sum(out["fields_left_empty"]["by_document"]) == 2


def test_a_holder_typed_before_the_fill_is_not_reported_empty():
    page, stripe = _two_postcode_page({"top": [1], "kps": [5]})  # the holder (index 0) holds a value
    i = asyncio.run(page._inspect_payment("", (), (stripe,), unreviewed=True, cart=(2266, "EUR")))
    top = next(d for d in i.documents if d.frame_id == "top")
    assert top.blank == frozenset({1})
    fills = bvp._document_fills(i, {**CARD, "cardholder_name": ""})
    assert "cc-name" not in {f["token"] for f in fills}
    assert "cc-name" not in bvp._form_report({"success": True}, i, fills, locked=False)["fields_left_empty"]["card_fields"]


def test_relabel_acceptance_and_repin_read_the_same_post_drop_token_set():
    from tools import browser_supervisor_payment_auto as spa
    from tools.browser_supervisor_payment_repin import repin_pins

    rows = [*_FORM, _row(3, "zip", "Billing postcode")]
    controls = tuple(c for c in sp._card_controls(rows, True) if c.token != "postal-code")  # dropped by _one_postcode

    class Supervisor(spa.PaymentAutoMixin):
        async def _cdp(self, method, params=None, session_id=None, timeout=None):
            fn = params["functionDeclaration"]
            return {"result": {"result": {"value": rows if "relabeled()" in fn else True}}}

    i = SimpleNamespace(unreviewed_origins=(), merchant_origin=MERCHANT)
    d = SimpleNamespace(object_id="o", frame_session="s", origin=MERCHANT, controls=controls)
    assert asyncio.run(Supervisor()._accept_relabel(i, d)) is True
    answer = {"fields": rows, "kept": {"cc-number": 0}, "inspected": [[n, n] for n in range(4)]}
    pins, refused = repin_pins(controls, ["cc-number"], answer, [], False, planned={"cc-number": 0})
    assert refused is None and pins == {"cc-number": 0}


def test_one_postcode_hands_back_what_it_dropped():
    a = ("a", "sa", "oa", MERCHANT, sp._card_controls([_row(0, "ccnum", "Card number"), _row(1, "zip", "ZIP")], True))
    b = ("b", "sb", "ob", PSP, sp._card_controls([dict(r, formIndex=None) for r in _FORM[1:]]
                                                   + [_row(5, "postal", "Postal code", form=None)]))
    assert rs._one_postcode([a, b]) == [("sa", "oa", 1), ("sb", "ob", 5)]
    assert rs._one_postcode([a]) == []


# --- 3: a shipping postcode --------------------------------------------------------------------------------------



def classify_postal(row):
    from agent.vault_login_classifier import LoginControl, classify_checkout_control

    classified = classify_checkout_control(LoginControl.from_dict(row))
    return classified.token if classified else None


@pytest.mark.parametrize("row", [
    _row(3, "zip", "ZIP code", autocomplete="shipping postal-code"),
    _row(3, "ship_zip", "ZIP code"),
    _row(3, "shippingZip", "ZIP"),
    _row(3, "zip", "Delivery postcode"),
    _row(3, "plz", "Lieferadresse Postleitzahl ZIP"),
    _row(3, "zip", "Code postal de livraison (postal)"),
    _row(3, "zip", "Código postal de envío (postal)"),
    _row(3, "zip", "Postcode bezorgadres"),
    _row(3, "zip", "Indirizzo di spedizione postal code"),
])
def test_a_shipping_postcode_is_never_the_billing_postcode(row):
    assert classify_postal(row) == "postal-code"  # the field is a postcode field ...
    for merchant_doc in (True, False):
        assert "postal-code" not in [c.token for c in sp._card_controls([*_FORM, row], merchant_doc)]


def test_a_billing_postcode_beside_a_shipping_one_is_the_one_postcode():
    rows = [*_FORM, _row(3, "ship_zip", "Shipping ZIP"), _row(4, "zip", "Billing postcode")]
    assert [(c.token, c.control.index) for c in sp._card_controls(rows, True)][-1] == ("postal-code", 4)
    # the shipping field is a field of the card's form the fill leaves: counted
    assert _document(rows, blank=frozenset({3, 4})).left_for_user == (3,)


def test_the_merchants_own_postcode_is_written_only_into_a_field_that_was_blank():
    rows = [*_FORM, _row(3, "zip", "Postcode")]
    card = {**CARD, "billing_postal_code": "10115"}

    def written(blank):
        document = _document(rows, blank=blank)
        return {f["token"] for f in bvp._document_fills(_inspection(document), card)}

    assert "postal-code" in written(frozenset({3}))
    assert "postal-code" not in written(frozenset())  # already holds the user's value
    assert "postal-code" not in written(None)  # a reading that cannot say is not a blank
    # a hosted document keeps writing its postcode as before
    hosted = _document([dict(r, formIndex=None) for r in _FORM] + [_row(3, "postal", "Postal code", form=None)],
                       origin=PSP)
    assert "postal-code" in {f["token"] for f in bvp._document_fills(_inspection(hosted), card)}


# --- 4: the report describes the reading that finished -----------------------------------------------------------


def test_the_result_describes_the_re_inspection_the_commit_finished_on():
    rows = [*_FORM, _row(3, "first", "First name")]
    first = _inspection(_document(_FORM))  # the approved reading: a complete form
    fresh = _inspection(_document(rows, blank=frozenset({3})))  # the re-rendered form has an empty required field
    fills = bvp._document_fills(first, CARD)
    first.settled = (fresh, bvp._document_fills(fresh, CARD))
    prompt = {"fields": [], "checks": []}
    out = bvp._describe_result({"success": True}, SimpleNamespace(name="local"), first, fills, prompt, "", False, None)
    assert out["form_complete"] is False and out["fields_left_empty"]["other_fields"] == 1
    assert out["fields"] == ["cc-csc", "cc-exp", "cc-number"]
    first.settled = None  # an unarmed or uncontinued commit: the reading it was given
    out = bvp._describe_result({"success": True}, SimpleNamespace(name="local"), first, fills, prompt, "", False, None)
    assert out["form_complete"] is True


def test_an_armed_commit_hands_back_the_reading_and_fills_it_finished_on(tmp_path, monkeypatch):
    from tests.tools.test_browser_payment_fill_auto import _auto_commit
    from tools import browser_supervisor_payment_auto as spa

    seen, run = [], spa.PaymentAutoMixin._commit_payment_auto

    async def spy(self, i, *a, **k):
        seen.append(i)
        return await run(self, i, *a, **k)

    monkeypatch.setattr(spa.PaymentAutoMixin, "_commit_payment_auto", spy)
    out, _values, events = _auto_commit("auto_remounted", tmp_path, monkeypatch)  # the form is built again: one re-inspection
    assert out["success"] and events.count("reinspect") == 1
    (first,) = seen
    fresh, fills = first.settled
    assert fresh is not first and fresh.adopted is True and {f["token"] for f in fills} == {"cc-exp", "cc-csc", "cc-number"}


def test_the_blank_state_is_read_again_after_the_fill(tmp_path, monkeypatch):
    from tests.tools.test_browser_payment_repin import MERCHANT as _M
    from tests.tools.test_browser_payment_repin import _SYNTHETIC, _ClosureSupervisor, _NodeClosure

    scenarios = {"typed_after": """
const holder = input({id: 'holder', autocomplete: 'cc-name'}), exp = input({id: 'exp', autocomplete: 'cc-exp'});
const csc = input({id: 'csc', autocomplete: 'cc-csc'}), num = input({id: 'num', autocomplete: 'cc-number'});
els = [holder, exp, csc, num];
approve = () => { holder.held = 'Grace Hopper'; };  // the user types it after the inspection read the form"""}
    js = _NodeClosure("typed_after", tmp_path, scenarios)
    try:
        controls = sp._card_controls(js.call("describe"))
        assert next(c.token for c in controls) == "cc-name"
        doc = PaymentDocument("card", "s", "o-card", PSP, controls, clock_anchor=time.monotonic(), blank=frozenset({0}))
        i = PaymentInspection("op", "s", "tab", _M, (doc,), (("card", "L", PSP + "/card", PSP, ""),),
                              time.monotonic() + 60)
        fills = bvp._document_fills(i, _SYNTHETIC)
        assert "cc-name" not in {f["token"] for f in fills}
        js.call("approve")
        monkeypatch.setattr(sp, "_SETTLE_SECONDS", 0)
        out = asyncio.run(_ClosureSupervisor(js)._commit_payment_once(i, fills, None))
        assert out["success"] is True, out
        assert doc.blank == frozenset()  # read after the fill
        assert bvp._form_report(out, i, fills, locked=False) == {"form_complete": True}
    finally:
        js.close()


# --- 5: masking of results that name no slot ----------------------------------------------------------------------


def test_a_browser_result_that_declares_no_slot_keeps_the_full_masking_after_the_release(monkeypatch, tmp_path):
    from agent import redact_vault as rv

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rv.register_card_window("slot-a", digits=(CARD["card_number"], "837"), words=("Ada Lovelace",))
    try:
        rv.release_card_window("slot-a", keep_for_browser=True)
        page = "total 837,00 EUR | CVC 837 | EUR 837"
        undeclared = rv.redact_registered_vault_values(page, audience="browser")
        assert "837" not in undeclared  # a CVC-length run inside an amount too: the slot is unknown
        with rv.browser_result_slot("slot-b"):  # another slot's own result: amounts stay readable (D-0367 b)
            assert rv.redact_registered_vault_values(page, audience="browser") == page
        with rv.browser_result_slot("slot-a"):
            own = rv.redact_registered_vault_values(page, audience="browser")
        assert "837,00 EUR" in own and "CVC •••" in own
    finally:
        rv.clear_vault_redaction_values()


# --- 7: no re-export shim ------------------------------------------------------------------------------------------


def test_the_result_helpers_are_not_re_exported_from_the_supervisor_module():
    for name in ("_classified", "_card_of", "_left_for_user", "_one_postcode", "_summary", "_blank_indexes",
                 "_page_match_reasons", "_SUMMARY_COUNTS"):
        assert hasattr(rs, name) and not hasattr(sp, name), name


def test_a_formless_checkout_result_serialises_without_values():
    document = _document([*_FORMLESS, _row(3, "first", "First name", form=None)], blank=frozenset({3}))
    assert "First" not in json.dumps(_report(document, CARD)) and "first" not in json.dumps(_report(document, CARD))
