"""Card fill results that say what the form still needs (D-0358 a, D-0359 a, D-0360 a, D-0367 b; 2026-11-18).

A fill writes every card field it recognises and then reports the fields of the card form it left empty, the
billing postcode comes from the saved card, the provider's own invalid marks are reported as fixed codes, refusals
and re-inspection stops name fixed codes and counts, and a released payment window masks only its slot's own browser
results, with currency amounts readable. Synthetic cards and pages only.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess

import pytest

from agent.vault_login_classifier import LoginControl, classify_checkout_control
from tools import browser_supervisor_payment as sp
from tools import browser_supervisor_payment_results as rs
from tools.browser_supervisor_payment import PaymentDocument, PaymentInspection, _card_documents

MERCHANT = "https://shop.example"
PSP = "https://pay.provider.example"
CARD = {"card_number": "4242424242424242", "exp_month": "07", "exp_year": "2035", "cvc": "837",
        "cardholder_name": "Ada Lovelace"}


def _row(index, name, label, form=0, autocomplete=""):
    return {"index": index, "autocomplete": autocomplete, "name": name + " ", "label": label, "type": "text",
            "formIndex": form, "maxLength": None}


_FORM = [_row(0, "ccnum", "Card number"), _row(1, "exp", "Expiry date"), _row(2, "cvc", "CVC")]


def _inspection(*documents, cardless=0):
    return PaymentInspection("op", "page", "target", MERCHANT, tuple(documents), (), 1e12, cardless_frames=cardless)


# --- D-0359 a: the billing postcode -------------------------------------------------------------------------------


def test_the_billing_postcode_passes_card_validation_and_a_malformed_one_is_left_out():
    from agent.vault_payment_card import validate_card

    assert validate_card({**CARD, "billing_postal_code": " 10115 "})["billing_postal_code"] == "10115"
    assert validate_card({**CARD, "billing_postal_code": "SW1A 1AA"})["billing_postal_code"] == "SW1A 1AA"
    for bad in ("", "1", "10115; DROP", "x" * 13):
        assert "billing_postal_code" not in validate_card({**CARD, "billing_postal_code": bad})


def test_the_1password_card_takes_its_postcode_from_one_custom_field():
    from agent.vault_payment_card import parse_onepassword_card

    base = {"id": "c", "category": "CREDIT_CARD", "vault": {"id": "v"}, "fields": [
        {"id": "ccnum", "value": CARD["card_number"]}, {"id": "cvv", "value": "837"},
        {"id": "expiry", "value": "203507"}, {"id": "cardholder", "value": "Ada Lovelace"}]}

    def parsed(*extra):
        return parse_onepassword_card({**base, "fields": base["fields"] + list(extra)}, item_id="c", vault_id="v")

    assert parsed({"id": "x1", "label": "Billing postal code", "value": "10115"})["billing_postal_code"] == "10115"
    assert parsed({"id": "x1", "label": "PLZ", "value": "8001"})["billing_postal_code"] == "8001"
    assert "billing_postal_code" not in parsed()
    # two different postcodes name none (skipped and reported), never a refused card
    assert "billing_postal_code" not in parsed({"id": "a", "label": "ZIP", "value": "10115"},
                                               {"id": "b", "label": "Postcode", "value": "20095"})


def test_a_card_document_keeps_its_one_postcode_field_and_fills_it_from_the_card():
    from tools import browser_vault_payment as bvp

    rows = [*_FORM, _row(3, "zip", "Card holder ZIP code")]
    controls = sp._card_controls(rows, merchant_doc=True)
    assert [c.token for c in controls] == ["cc-number", "cc-exp", "cc-csc", "postal-code"]
    docs = _card_documents([("f", "s", "o", MERCHANT, controls)], MERCHANT)
    docs[0].blank = frozenset({3})  # the postcode field was empty at inspection
    fills = bvp._document_fills(_inspection(*docs), {**CARD, "billing_postal_code": "10115"})
    assert [(f["index"], f["value"]) for f in fills if f["token"] == "postal-code"] == [(3, "10115")]
    # a saved card without one: the field is skipped (optional), the card fields are still written
    fills = bvp._document_fills(_inspection(*docs), CARD)
    assert sorted(f["token"] for f in fills) == ["cc-csc", "cc-exp", "cc-number"]


@pytest.mark.parametrize("rows, merchant_doc", [
    # the merchant page's shipping address form beside the card form is not the card's postcode
    ([*_FORM, _row(3, "zip", "ZIP code", form=1)], True),
    ([*_FORM, _row(3, "zip", "ZIP code", form=None)], True),
    # two postcode fields name none
    ([*_FORM, _row(3, "zip", "ZIP code"), _row(4, "zip2", "Postcode")], False),
    # a postcode without card fields is no card document
    ([_row(0, "zip", "ZIP code")], False),
])
def test_a_postcode_outside_the_card_form_stays_out(rows, merchant_doc):
    assert "postal-code" not in [c.token for c in sp._card_controls(rows, merchant_doc=merchant_doc)]


def test_a_hosted_card_frame_keeps_its_postcode_without_a_form():
    rows = [*(dict(r, formIndex=None) for r in _FORM), _row(3, "postal", "Postal code", form=None)]
    assert [c.token for c in sp._card_controls(rows)][-1] == "postal-code"


def test_postcode_fields_in_two_card_documents_are_dropped_instead_of_refusing_the_form():
    a = ("a", "sa", "oa", MERCHANT, sp._card_controls([_row(0, "ccnum", "Card number"),
                                                        _row(1, "zip", "ZIP code")], merchant_doc=True))
    b = ("b", "sb", "ob", PSP, sp._card_controls([_row(0, "exp", "Expiry date", form=None),
                                                   _row(1, "cvc", "CVC", form=None),
                                                   _row(2, "postal", "Postal code", form=None)]))
    candidates = [a, b]
    with pytest.raises(sp.PaymentPolicyError, match="Incomplete or ambiguous"):
        _card_documents(candidates, MERCHANT)  # what the duplicate would do
    rs._one_postcode(candidates)
    docs = _card_documents(candidates, MERCHANT)
    assert all("postal-code" not in [c.token for c in d.controls] for d in docs)


def test_the_re_pin_reads_the_postcode_the_inspection_made_a_card_field():
    from tools.browser_supervisor_payment_repin import repin_pins

    rows = [*(dict(r, formIndex=None) for r in _FORM), _row(3, "postal", "Postal code", form=None)]
    controls = sp._card_controls(rows)
    answer = {"fields": rows, "kept": {"postal-code": 3}, "inspected": [[n, n] for n in range(4)]}
    pins, refused = repin_pins(controls, ["postal-code"], answer, ["postal-code"], True, planned={"postal-code": 3})
    assert refused is None and pins == {"postal-code": 3}


# --- D-0358 a / D-0360 a: fields left for the user ---------------------------------------------------------------


def _document(rows, origin=MERCHANT, blank=None):
    classified = rs._classified(rows)
    controls = rs._card_of(classified, origin == MERCHANT)
    return PaymentDocument("f", "s", "o", origin, controls, left_for_user=rs._left_for_user(
        classified, controls, origin == MERCHANT), blank=blank)


def _report(document, card, locked=False, result=None, cardless=0):
    from tools import browser_vault_payment as bvp

    inspection = _inspection(document, cardless=cardless)
    fills = bvp._document_fills(inspection, card)
    return bvp._form_report(result or {"success": True}, inspection, fills, locked)


def test_a_complete_card_form_is_reported_complete():
    assert _report(_document(_FORM + [_row(3, "holder", "Name on card")]), CARD) == {"form_complete": True}


def test_a_holder_field_the_card_has_no_name_for_makes_the_form_incomplete():
    card = {k: v for k, v in CARD.items() if k != "cardholder_name"}
    out = _report(_document(_FORM + [_row(3, "holder", "Name on card")], blank=frozenset({3})), card)
    assert out["form_complete"] is False
    assert out["fields_left_empty"] == {"card_fields": ["cc-name"], "other_fields": 0, "by_document": [0],
                                        "frames_without_card_fields": 0}
    assert "holder name, postcode" in out["next"] and "do not press Pay" in out["next"]
    # the same field typed by the user before the fill holds a value: nothing is left
    assert _report(_document(_FORM + [_row(3, "holder", "Name on card")], blank=frozenset()), card) == \
        {"form_complete": True}


def test_an_unrecognised_empty_field_of_the_card_form_is_counted_never_named():
    rows = _FORM + [_row(3, "x9", "Kontoinhaber Vorname")]  # nothing the classifier names
    assert classify_checkout_control(LoginControl.from_dict(rows[3])) is None
    out = _report(_document(rows, blank=frozenset({3})), CARD)
    assert out["form_complete"] is False and out["fields_left_empty"]["other_fields"] == 1
    assert "Kontoinhaber" not in json.dumps(out) and "x9" not in json.dumps(out)
    # an unknown blank state counts as empty; a filled one does not
    assert _report(_document(rows), CARD)["fields_left_empty"]["other_fields"] == 1
    assert _report(_document(rows, blank=frozenset()), CARD) == {"form_complete": True}


def test_the_merchant_page_header_search_is_not_part_of_the_card_form():
    rows = _FORM + [_row(3, "q", "Search", form=1), _row(4, "nl", "Newsletter", form=None)]
    assert _report(_document(rows, blank=frozenset({3, 4})), CARD) == {"form_complete": True}


def test_a_hosted_card_document_reports_every_field_it_does_not_write():
    rows = [dict(r, formIndex=None) for r in _FORM] + [_row(3, "fn", "First name", form=None)]
    out = _report(_document(rows, origin=PSP, blank=frozenset({3})), CARD)
    assert out["fields_left_empty"]["other_fields"] == 1


def test_a_cardless_frame_of_the_card_origin_makes_the_form_incomplete():
    out = _report(_document(_FORM), CARD, cardless=1)
    assert out["form_complete"] is False and out["fields_left_empty"]["frames_without_card_fields"] == 1


def test_a_locked_browser_gets_the_close_and_type_first_text():
    # D-0360 a: a missing holder name or postcode is first answered by filling again with the argument (see
    # test_browser_payment_supplied_fields); a field the fill cannot take at all still gets the close-reopen-type path.
    rows = _FORM + [_row(3, "x9", "Kontoinhaber Vorname")]
    out = _report(_document(rows), CARD, locked=True)
    assert "payment_close" in out["next"] and "locked" in out["next"] and "do not press Pay" in out["next"]
    card = {k: v for k, v in CARD.items() if k != "cardholder_name"}
    named = _report(_document(_FORM + [_row(3, "holder", "Name on card")]), card, locked=True)
    assert "holder_name" in named["next"] and "locked" in named["next"] and "payment_close" not in named["next"]


def test_provider_invalid_marks_make_the_form_incomplete():
    out = _report(_document(_FORM), CARD, result={"success": True, "fields_invalid": ["cc-csc"]})
    assert out["form_complete"] is False and "fields_invalid" in out["next"] and "do not press Pay" in out["next"]


def test_a_stopped_fill_gets_no_form_report():
    assert _report(_document(_FORM), CARD, result={"success": False}) == {}


def test_retention_reports_the_tokens_the_provider_marks_invalid():
    fills = [{"token": "cc-number"}, {"token": "cc-csc"}]
    answer = [{"token": "cc-number", "state": "held", "length": 16, "validity": "valid"},
              {"token": "cc-csc", "state": "held", "length": 3, "validity": "invalid"}]
    assert sp._retention_result([(fills, answer)]) == {"success": True, "filled_fields": 2, "retention_verified": True,
                                                     "fields_invalid": ["cc-csc"]}
    answer[1]["validity"] = "<b>bad</b>"  # anything but the fixed code is unknown, never echoed
    assert sp._retention_result([(fills, answer)]) == {"success": True, "filled_fields": 2,
                                                     "retention_verified": True}


# The closure itself, under Node: blanks() names indexes only; retained() adds the fixed validity code.
_HARNESS = r"""
const C = CASE;
const performance = {now: () => 1000};
class HTMLInputElement { get value() { return this.held || ''; } set value(v) { this.held = v; } }
class HTMLSelectElement { get value() { return ''; } set value(v) {} }
class Event {} class InputEvent extends Event {} class FocusEvent extends Event {}
// the intrinsic the closure reads ``novalidate`` through: it works on the form object whatever its controls are named
class Element { hasAttribute(n) { return n === 'novalidate' && (this.novalidateAttr ?? this.noValidate === true); } }
const document = {forms: [], getElementById: () => null};
const location = {href: 'https://shop.example/checkout'};
// ``matches(':invalid')`` follows a real browser: a field is :invalid only when it is validated (willValidate: false for
// readonly, disabled, fieldset-disabled and datalist fields) and its ValidityState is not valid. Without a ValidityState
// (``flags`` null) the case's own ``invalid`` stands in.
const isInvalid = f => f.flags ? f.willValidate !== false && Object.values(f.flags).some(Boolean) : !!f.invalid;
const els = C.fields.map(f => Object.assign(new HTMLInputElement(), {tagName: f.tagName || 'INPUT', autocomplete: '',
  name: f.name, id: f.name, labels: [{textContent: f.label}], placeholder: '', type: 'text', held: f.held || '',
  form: f.form || null, maxLength: 0, disabled: false, readOnly: false, isConnected: true, tabIndex: 0, shadowRoot: null,
  ownerDocument: document, style: {setProperty() {}}, hasAttribute: () => false, dispatchEvent: () => true, blur() {},
  focus() {}, getAttribute: n => ((f.attrs || {})[n] ?? null), getBoundingClientRect: () => ({width: 100, height: 20}),
  getClientRects: () => [1], getRootNode: () => document,
  parentElement: {classList: {contains: c => f.parentClass === c}, parentElement: null},
  classList: {contains: () => false}, matches: s => s === ':invalid' && isInvalid(f),
  validity: f.flags ? Object.assign({valueMissing: false, typeMismatch: false, patternMismatch: false, tooShort: false,
    tooLong: false, rangeUnderflow: false, rangeOverflow: false, stepMismatch: false, badInput: false,
    customError: false}, f.flags, {valid: !Object.values(f.flags).some(Boolean)}) : undefined}));
// A field's form may list ``controls`` (what ``root.querySelectorAll('button[formnovalidate],input[formnovalidate]')``
// finds): each has a ``tagName`` (BUTTON unless given) and belongs to that form unless ``otherForm``; the selector is
// honoured (a control is returned when one comma part starts with ``<tagname>[``); ``fieldsetDisabled`` makes
// ``matches(':disabled')`` true like a disabled fieldset does. ``novalidateAttr`` is the form's novalidate attribute
// (it follows ``noValidate`` when absent); a ``hasAttribute`` the case gives the form stays as it is (a shadowing control).
const controls = [];
const bySelector = sel => controls.filter(c => sel.split(',').some(p => p.trim().toLowerCase().startsWith(String(c.tagName).toLowerCase() + '[')));
for (const f of C.fields) if (f.form) {
  if (!('hasAttribute' in f.form)) f.form.hasAttribute = n => n === 'novalidate' && (f.form.novalidateAttr ?? f.form.noValidate === true);
  for (const c of f.form.controls || [])
    controls.push(Object.assign({tagName: 'BUTTON', matches: s => s === ':disabled' && !!c.fieldsetDisabled}, c, {form: c.otherForm ? {} : f.form}));
  // what a browser's ``form.elements`` lists: this form's controls, never an ``input type=image`` (unless shadowed)
  if (!('elements' in f.form)) f.form.elements = controls.filter(c => c.form === f.form && c.type !== 'image');
}
document.querySelectorAll = sel => (/formnovalidate/.test(sel) ? bySelector(sel) : els);
const getComputedStyle = () => ({visibility: 'visible', opacity: '1', pointerEvents: 'auto'});
const api = INSPECT;
const blanks = api.blanks();
const result = C.plan ? api.commit(C.plan, 60000) : null;
process.stdout.write(JSON.stringify({blanks, result, retained: C.plan ? api.retained() : null}));
"""


def _node(case):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for the synthetic inspection closure")
    values = {"CASE": json.dumps(case), "INSPECT": sp._INSPECT}
    harness = re.sub(r"\bCASE\b|\bINSPECT\b", lambda m: values[m.group(0)], _HARNESS)
    run = subprocess.run([node], input=harness, text=True, capture_output=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    return run.stdout, json.loads(run.stdout)


@pytest.mark.parametrize("attrs, invalid, parent_class, code", [
    ({}, False, "", "valid"),
    ({"aria-invalid": "true"}, False, "", "invalid"),
    ({}, True, "", "invalid"),
    ({}, False, "StripeElement--invalid", "invalid"),
])
def test_the_closure_reports_blank_fields_by_index_and_a_fixed_validity_code(attrs, invalid, parent_class, code):
    fields = [{"name": "ccnum", "label": "Card number"}, {"name": "cvc", "label": "CVC", "attrs": attrs,
                                                         "invalid": invalid, "parentClass": parent_class},
              {"name": "note", "label": "Reference", "held": "typed by the user"}]
    plan = [{"index": 0, "token": "cc-number", "value": CARD["card_number"]},
            {"index": 1, "token": "cc-csc", "value": "837"}]
    raw, out = _node({"fields": fields, "plan": plan})
    assert out["blanks"] == [0, 1]  # the user's typed field is not blank; indexes only
    assert "typed by the user" not in raw
    assert out["result"]["success"] is True
    *states, marker = out["retained"]
    assert marker == {"revealed": []}  # B final: no field appeared during the fill (descriptors only, never a value)
    assert [(r["token"], r["state"], r["validity"]) for r in states] == [
        ("cc-number", "held", "valid"), ("cc-csc", "held", code)]


def _validity_of(attrs, flags, parent_class="", invalid=False, **extra):
    """The fixed validity code the closure reports for one filled cvc-like or name field."""
    field = {"name": "holder", "label": "Name on card", "attrs": attrs, "flags": flags, "parentClass": parent_class,
             "invalid": invalid, **extra}
    plan = [{"index": 0, "token": "cc-name", "value": "Example Person"}]
    raw, out = _node({"fields": [field], "plan": plan})
    assert "Example Person" not in raw
    return out["retained"][0]["validity"]


# D-0358 a note (11-19): a present-but-empty ``pattern`` makes every filled value a patternMismatch (a hosted
# cardholder-name input inside a novalidate form); that HTML artefact alone is not the provider's verdict.
@pytest.mark.parametrize("pattern", ["", "   ", "\t\n"])
def test_an_empty_pattern_alone_does_not_make_a_filled_field_invalid(pattern):
    attrs = {"aria-invalid": "false", "pattern": pattern}
    assert _validity_of(attrs, {"patternMismatch": True}) == "valid"


@pytest.mark.parametrize("attrs, flags, parent_class, code", [
    ({"pattern": "[A-Z]+"}, {"patternMismatch": True}, "", "invalid"),  # a real pattern the value breaks
    ({"pattern": "[A-Z]+"}, {}, "", "valid"),  # a real pattern the value meets
    ({"pattern": "", "aria-invalid": "true"}, {"patternMismatch": True}, "", "invalid"),  # the provider says so
    ({"pattern": ""}, {"patternMismatch": True}, "StripeElement--invalid", "invalid"),  # its own invalid class
    ({"pattern": ""}, {"patternMismatch": True, "valueMissing": True}, "", "invalid"),
    ({"pattern": ""}, {"patternMismatch": True, "customError": True}, "", "invalid"),
    ({"pattern": ""}, {"patternMismatch": True, "typeMismatch": True}, "", "invalid"),
    ({"pattern": ""}, {"patternMismatch": True, "tooShort": True}, "", "invalid"),
    ({"pattern": ""}, {"patternMismatch": True, "tooLong": True}, "", "invalid"),
    ({"pattern": ""}, {"patternMismatch": True, "rangeUnderflow": True}, "", "invalid"),
    ({"pattern": ""}, {"patternMismatch": True, "rangeOverflow": True}, "", "invalid"),
    ({"pattern": ""}, {"patternMismatch": True, "stepMismatch": True}, "", "invalid"),
    ({"pattern": ""}, {"patternMismatch": True, "badInput": True}, "", "invalid"),
    ({}, {"valueMissing": True}, "", "invalid"),  # required and empty, no pattern at all
    ({}, {"patternMismatch": True}, "", "invalid"),  # a mismatch with no pattern attribute is never excused
    ({}, {"customError": True}, "", "invalid"),
    ({}, {}, "is-invalid", "invalid"),
    ({}, {}, "", "valid"),
])
def test_other_constraint_failures_stay_invalid(attrs, flags, parent_class, code):
    assert _validity_of(attrs, flags, parent_class) == code


# The excuse applies only where the browser would not block the submission anyway. A form lists the controls the field's
# root finds with ``[formnovalidate]`` (``controls``, each a BUTTON unless it names a ``tagName``, and the mock honours
# the selector); ``elements`` is never read (``input type=image`` is not in it and a control named ``elements``
# replaces it).
def _form(*controls, **kw):
    return {"noValidate": False, "controls": list(controls), **kw}


_FNV_SUBMIT = {"type": "submit", "formNoValidate": True}
_SHADOW = {"tagName": "INPUT", "name": "shadow"}
@pytest.mark.parametrize("extra, code", [
    ({}, "valid"),  # no form at all
    ({"form": {"noValidate": True}}, "valid"),  # Shopify's hosted frame form
    ({"form": {"noValidate": False}}, "invalid"),  # a native form: its submit button would never submit
    # a submit control with formnovalidate: the browser skips constraint validation for that submission
    ({"form": _form(_FNV_SUBMIT)}, "valid"),
    # <input type=image formnovalidate>: the root finds it, form.elements would not
    ({"form": _form({"type": "image", "formNoValidate": True})}, "valid"),
    # the selector must reach <input> controls too (a `button[formnovalidate]` selector would miss both)
    ({"form": _form({"tagName": "INPUT", "type": "submit", "formNoValidate": True})}, "valid"),
    ({"form": _form({"tagName": "INPUT", "type": "image", "formNoValidate": True})}, "valid"),
    ({"form": _form({"type": "submit", "formNoValidate": False})}, "invalid"),
    ({"form": _form({"type": "button", "formNoValidate": True})}, "invalid"),
    ({"form": _form({"type": "submit"})}, "invalid"),
    # disabled counts like hidden: Pay is often disabled until a terms box is ticked, so a wrong block costs the payment
    ({"form": _form({**_FNV_SUBMIT, "disabled": True})}, "valid"),
    ({"form": _form({**_FNV_SUBMIT, "fieldsetDisabled": True})}, "valid"),
    # a control of another form does not count
    ({"form": _form({**_FNV_SUBMIT, "otherForm": True})}, "invalid"),
    # hidden but enabled: not told apart from Pay, so it counts (a wrong excuse costs a refused submit, no charge)
    ({"form": _form({**_FNV_SUBMIT, "hidden": True})}, "valid"),
    # a control named ``elements`` (it replaces form.elements) or ``noValidate`` (it shadows the property) hides nothing
    ({"form": _form(_FNV_SUBMIT, elements={"tagName": "INPUT", "name": "elements"})}, "valid"),
    ({"form": {"noValidate": {"tagName": "INPUT", "name": "noValidate"}, "novalidateAttr": True}}, "valid"),
    ({"form": {"noValidate": {"tagName": "INPUT", "name": "noValidate"}, "novalidateAttr": False}}, "invalid"),
    # ... and so does one named ``hasAttribute``, even together with ``noValidate`` (the intrinsic is read)
    ({"form": {"noValidate": _SHADOW, "hasAttribute": _SHADOW, "novalidateAttr": True}}, "valid"),
    ({"form": {"noValidate": _SHADOW, "hasAttribute": _SHADOW, "novalidateAttr": False}}, "invalid"),
    ({"form": _form(_FNV_SUBMIT, noValidate=_SHADOW, hasAttribute=_SHADOW, novalidateAttr=False)}, "valid"),
])
def test_an_empty_pattern_is_excused_only_where_the_browser_would_not_block(extra, code):
    assert _validity_of({"pattern": ""}, {"patternMismatch": True}, **extra) == code


def test_formnovalidate_excuses_nothing_but_the_empty_pattern_artefact():
    form = _form(_FNV_SUBMIT)
    for flags in ({"valueMissing": True}, {"patternMismatch": True, "typeMismatch": True}):
        assert _validity_of({"pattern": ""}, flags, form=form) == "invalid"
    assert _validity_of({"pattern": "[0-9]+"}, {"patternMismatch": True}, form=form) == "invalid"


# ... and only on an <input>. The closure only ever enumerates INPUT and SELECT, so a custom element (a form-associated
# one can report any ValidityState) is exercised on the validity decision itself, cut out of the closure text.
_DECISION = r"""
class Element { hasAttribute(n) { return n === 'novalidate' && (this.novalidateAttr ?? this.noValidate === true); } }
const CASE_EL = EL;
const form = CASE_EL.form || null;
const controls = [];
const bySelector = sel => controls.filter(c => sel.split(',').some(p => p.trim().toLowerCase().startsWith(String(c.tagName).toLowerCase() + '[')));
if (form) {
  if (!('hasAttribute' in form)) form.hasAttribute = n => n === 'novalidate' && (form.novalidateAttr ?? form.noValidate === true);
  for (const c of form.controls || []) controls.push(Object.assign({tagName: 'BUTTON', matches: s => s === ':disabled' && !!c.fieldsetDisabled}, c, {form}));
  if (!('elements' in form)) form.elements = controls.filter(c => c.type !== 'image');  // never lists type=image
}
const el = {tagName: CASE_EL.tagName, form, getRootNode: () => ({querySelectorAll: bySelector}), validity: CASE_EL.validity,
  getAttribute: n => (n === 'pattern' ? CASE_EL.pattern : null), classList: {contains: () => false},
  parentElement: null, matches: s => s === ':invalid'};
const boom = () => { throw new Error('unreadable'); };
if (CASE_EL.throwOn === 'validity') Object.defineProperty(el, 'validity', {get: boom});
if (CASE_EL.throwOn === 'matches') el.matches = boom;
if (CASE_EL.throwOn === 'pattern') el.getAttribute = n => (n === 'pattern' ? boom() : null);
if (CASE_EL.throwOn === 'controls') el.getRootNode = boom;
if (CASE_EL.throwOn === 'hasAttribute') Element.prototype.hasAttribute = boom;
SEGMENT
process.stdout.write(JSON.stringify(validity(el)));
"""


def _decision_of(**el):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for the synthetic inspection closure")
    start = sp._INSPECT.index("  const INVALID_CLASSES")
    segment = sp._INSPECT[start:sp._INSPECT.index("  // dlz (B final, Codex P1)", start)]
    values = {"EL": json.dumps(el), "SEGMENT": segment}
    harness = re.sub(r"\bEL\b|\bSEGMENT\b", lambda m: values[m.group(0)], _DECISION)
    run = subprocess.run([node], input=harness, text=True, capture_output=True, timeout=10, check=False)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.parametrize("tag, form, code", [
    ("INPUT", None, "valid"),
    ("INPUT", {"noValidate": True}, "valid"),
    ("INPUT", {"noValidate": False}, "invalid"),
    ("input", {"noValidate": True}, "valid"),  # tagName casing does not matter
    ("FA-INPUT", None, "invalid"),  # a custom element is not the HTML input artefact
    ("FA-INPUT", {"noValidate": True}, "invalid"),
    ("SELECT", None, "invalid"),
])
def test_the_empty_pattern_excuse_is_for_an_input_element_only(tag, form, code):
    validity = {"valid": False, "patternMismatch": True}
    assert _decision_of(tagName=tag, form=form, pattern="", validity=validity) == code


# J-0269: once the browser calls the field :invalid, a validity read that throws keeps that verdict (it never becomes
# 'unknown', which counts as nothing); a throw before that gate is unreadable, as before.
@pytest.mark.parametrize("throw_on, code", [
    ("validity", "invalid"),
    ("pattern", "invalid"),
    ("controls", "invalid"),
    ("hasAttribute", "invalid"),  # no novalidate read, so no excuse: the formnovalidate control below is not reached
    ("matches", "unknown"),
])
def test_a_read_that_throws_after_the_invalid_gate_stays_invalid(throw_on, code):
    validity = {"valid": False, "patternMismatch": True}
    form = _form(_FNV_SUBMIT)  # a native form with a formnovalidate control, so every read above is reached
    assert _decision_of(tagName="INPUT", form=form, pattern="", validity=validity, throwOn=throw_on) == code


# GLM review nit: a ValidityState-like object with no boolean ``valid`` is read like no ValidityState at all.
@pytest.mark.parametrize("validity", [{"patternMismatch": True}, {"valid": "false", "patternMismatch": True},
                                      {"valid": None, "patternMismatch": True}])
def test_a_validity_without_a_boolean_valid_falls_back_to_the_invalid_selector(validity):
    assert _decision_of(tagName="INPUT", form=None, pattern="", validity=validity) == "invalid"


# A field the browser does not validate is never :invalid, whatever its ValidityState says (as before the change).
@pytest.mark.parametrize("why, flags", [
    ("readonly", {"patternMismatch": True}),
    ("readonly", {"valueMissing": True}),
    ("disabled, or inside a disabled fieldset", {"patternMismatch": True}),
    ("inside a datalist", {"patternMismatch": True}),
    ("empty pattern, custom error", {"patternMismatch": True, "customError": True}),
])
def test_a_field_the_browser_does_not_validate_stays_valid(why, flags):
    # the mock cannot model the DOM reasons themselves: ``willValidate`` false is what a browser reports for each
    assert _validity_of({"pattern": "[0-9]+"}, flags, willValidate=False) == "valid"


def test_a_native_form_empty_pattern_stays_invalid_with_a_provider_marker_or_not():
    native = {"form": {"noValidate": False}}
    assert _validity_of({"pattern": "", "aria-invalid": "false"}, {"patternMismatch": True}, **native) == "invalid"
    assert _validity_of({"pattern": ""}, {"patternMismatch": True}, willValidate=False, **native) == "valid"


def test_without_validity_state_the_old_invalid_selector_still_decides():
    assert _validity_of({"pattern": ""}, None, invalid=True) == "invalid"
    assert _validity_of({"pattern": ""}, None, invalid=False) == "valid"


# --- records: refusals and stops name fixed codes and counts ------------------------------------------------------


def test_an_unsupported_discovery_reading_carries_its_inspection_summary():
    from agent.vault_payment_policy import PaymentDestinationError

    holder = sp._card_controls([_row(0, "holder", "Name on card", form=None)])
    candidates = [("f", "s", "o", PSP, holder)]
    found = {"stats": {"frames_inspected": 3, "frames_cardless": 1, "inputs_unrecognised": 2}}
    summary = rs._summary(((), (), ()), candidates, found)
    with pytest.raises(PaymentDestinationError) as caught:
        sp._discovery_documents(candidates, MERCHANT, (), 2, summary)
    assert caught.value.error_type == "payment_fields_unsupported"
    assert caught.value.detail == {"unreviewed_frames_inspected": 2, "inspection": {
        "frames": 3, "documents": 1, "tokens": ["cc-name"], "tokens_duplicated": 0, "frames_inspected": 3,
        "frames_cardless": 1, "inputs_unrecognised": 2}}


def test_an_incomplete_re_inspection_keeps_the_tokens_found_and_the_counts():
    from tools.browser_supervisor_payment_auto import _note_found, clean_found

    summary = {"frames": 5, "documents": 1, "tokens": ["cc-name", "cc-number"], "tokens_duplicated": 0,
               "frames_inspected": 4, "frames_cardless": 2, "inputs_unrecognised": 3}
    approval: dict = {}
    _note_found(approval, "incomplete", summary)
    assert approval["found"] == summary
    _note_found(approval, "cart_changed", summary)  # the last reading speaks
    assert "found" not in approval
    assert clean_found({**summary, "tokens": ["https://evil.example/"]}) is None
    assert clean_found({**summary, "extra": 1}) is None


def test_the_re_inspection_stop_result_and_log_line_carry_the_record(caplog):
    from types import SimpleNamespace

    from tools import browser_vault_payment as bvp

    summary = {"frames": 5, "documents": 1, "tokens": ["cc-name"], "tokens_duplicated": 0, "frames_inspected": 4,
               "frames_cardless": 2, "inputs_unrecognised": 3}
    inspected = SimpleNamespace(approval={"found": summary, "seen": {"unreviewed_frames_inspected": 2}})
    with caplog.at_level(logging.WARNING, logger="tools.browser_vault_payment"):
        out = bvp._reinspect_stop("incomplete", inspected)
    assert out["fill_reinspect"]["found"] == summary and out["fill_reinspect"]["refused"] == "incomplete"
    assert "tokens cc-name" in caplog.text and "frames_cardless=2" in caplog.text


def test_an_ambiguous_page_check_logs_reason_codes_and_counts_never_amounts():
    pins = [("merchant page", {"state": "ambiguous", "labelled": [32765, 1999], "stated": 2, "unclear": False}),
            ("payment form", {"state": "ambiguous", "labelled": [], "stated": 1, "unclear": True})]
    assert rs._page_match_reasons(pins, 32765) == {"reasons": ["unclear", "other_total"], "labelled": 2, "other": 1,
                                                   "stated": 3, "documents": 2}
    only_stated = [("merchant page", {"state": "ambiguous", "labelled": [], "stated": 1, "unclear": False})]
    assert rs._page_match_reasons(only_stated, 32765)["reasons"] == ["unlabelled"]


def test_payment_fill_refused_names_the_step_and_the_card_check():
    from tools import browser_vault_payment as bvp

    out = bvp._fill_refused("card_check", "card_expired")
    assert out["failed_check"] == "card_check" and out["card_check"] == "card_expired"
    assert out["error_type"] == "payment_fill_refused" and "nothing was submitted" in out["error"]
    assert "card_check" not in bvp._fill_refused("commit")


def test_an_expired_saved_card_is_refused_with_its_code(monkeypatch):
    from types import SimpleNamespace

    from agent.vault_payment_card import CardInvalid, validate_card
    from tools import browser_vault_payment as bvp

    with pytest.raises(CardInvalid) as caught:
        validate_card({**CARD, "exp_year": "2001"})
    assert caught.value.code == "card_expired" and isinstance(caught.value, ValueError)

    inspection = _inspection(*_card_documents([("f", "s", "o", MERCHANT, sp._card_controls(_FORM))], MERCHANT))

    class Supervisor:
        def inspect_payment(self, *a, **k):
            return inspection

        def payment_is_current(self, i):
            return True

        def commit_payment(self, *a, **k):
            raise AssertionError("an invalid card is never written")

        def discard_payment(self, i):
            pass

    monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor", lambda task_id: Supervisor())
    monkeypatch.setattr("tools.browser_vault_tool._bot_desktop_browser_session", lambda task_id: False)
    monkeypatch.setattr("tools.browser_payment_privacy.mark_payment_session", lambda task_id, surface=None: None)
    monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", lambda *args, **k: "accept")
    meta = SimpleNamespace(id="h", kind="payment", label="Card", allowed_origins=(), origin=None,
                           payment_scope="confirmed_page", payment_frame_origins=())
    backend = SimpleNamespace(name="local", resolve_secret=lambda handle: {**CARD, "exp_year": "2001"},
                              get_meta=lambda handle: meta)
    out = json.loads(bvp.fill_payment(backend, meta, "task"))
    assert out == {"success": False, "error_type": "payment_fill_refused", "error": bvp._FILL_REFUSED,
                   "failed_check": "card_check", "card_check": "card_expired"}


# --- D-0367 b: a released window masks only its slot's own browser results ----------------------------------------


@pytest.fixture
def released(monkeypatch, tmp_path):
    from agent import redact_vault as rv

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rv.register_card_window("slot-a", digits=(CARD["card_number"], "837"), words=("Ada Lovelace",))
    yield rv
    rv.clear_vault_redaction_values()


_PAGE = "Total 327,65 EUR | CHF 149.00 | 149 € | CVC 837 | order 123 | card 4242 4242 4242 4242 | Ada Lovelace"


def test_an_open_window_still_masks_every_output_amounts_too(released):
    with released.browser_result_slot("slot-b"):
        out = released.redact_registered_vault_values(_PAGE, audience="browser")
    assert "327" not in out and "149" not in out and "837" not in out and "Ada" not in out
    assert "837" not in released.redact_registered_vault_values(_PAGE, audience="model")


def test_a_released_window_masks_its_own_slot_and_leaves_amounts_readable(released):
    released.release_card_window("slot-a", keep_for_browser=True)
    with released.browser_result_slot("slot-a"):
        own = released.redact_registered_vault_values(_PAGE, audience="browser")
    assert "327,65 EUR" in own and "CHF 149.00" in own and "149 €" in own
    assert "CVC •••" in own and "order •••" in own and "4242" not in own and "Ada Lovelace" not in own
    with released.browser_result_slot("slot-b"):
        assert released.redact_registered_vault_values(_PAGE, audience="browser") == _PAGE
    with released.browser_result_slot(""):  # a browser call with no slot route
        assert released.redact_registered_vault_values(_PAGE, audience="browser") == _PAGE
    # undeclared browser output and logs keep the masking (logs: amounts too)
    assert "837" not in released.redact_registered_vault_values(_PAGE, audience="browser")
    logged = released.redact_registered_vault_values(_PAGE, audience="log")
    assert "837" not in logged and "327" not in logged
    assert released.redact_registered_vault_values(_PAGE, audience="model") == _PAGE


def test_a_slot_less_window_masks_results_without_a_slot(released):
    released.register_card_window("supervisor:7", digits=("4111111111111111", "4321"))
    released.release_card_window("supervisor:7", keep_for_browser=True)
    released.release_card_window("slot-a")  # closed
    with released.browser_result_slot(""):
        assert "4321" not in released.redact_registered_vault_values("code 4321", audience="browser")
    with released.browser_result_slot("slot-b"):
        assert released.redact_registered_vault_values("code 4321", audience="browser") == "code 4321"


def test_browser_exec_declares_its_slot_for_the_output_scrub(monkeypatch):
    import inspect

    from tools import browser_use_cli

    source = inspect.getsource(browser_use_cli.browser_exec)
    assert source.count("with browser_result_slot(result_slot):") == 2
    assert 'result_slot = str(env.get("HERMES_BROWSER_SLOT_ID") or "")' in source


# --- the inspection itself, over the discovery tests' fake CDP page ----------------------------------------------


def _page_with_blanks(blanks, controls):
    from tests.tools.test_browser_vault_payment_destination import _PAGE, _DiscoveryPage

    class Page(_DiscoveryPage):
        async def _cdp(self, method, params=None, session_id=None, timeout=None):
            if method == "Runtime.callFunctionOn" and "blanks()" in (params or {}).get("functionDeclaration", ""):
                return {"result": {"result": {"value": blanks.get(params["objectId"][4:])}}}
            return await super()._cdp(method, params, session_id, timeout)

    return Page(_PAGE, exposed={"kps", "ad"}, controls=controls)


def test_the_inspection_records_the_fields_it_leaves_and_counts_cardless_frames():
    import asyncio

    from tests.tools.test_browser_vault_payment_destination import _CARD_FORM, KPS, STRIPE, _desc

    form = [*_CARD_FORM, _desc(len(_CARD_FORM), "", label="Kontoinhaber Vorname")]
    page = _page_with_blanks({"kps": [len(_CARD_FORM)]}, {"kps": form})
    i = asyncio.run(page._inspect_payment("", (), (STRIPE,), unreviewed=True, cart=(2266, "EUR")))
    (doc,) = i.documents
    assert doc.origin == KPS and doc.left_for_user == (len(_CARD_FORM),) and doc.blank == frozenset({len(_CARD_FORM)})
    assert i.cardless_frames == 0
    assert i.summary["documents"] == 1 and i.summary["inputs_unrecognised"] == 1
    assert "cc-number" in i.summary["tokens"] and i.summary["frames"] == 4


def test_a_document_that_cannot_say_which_fields_are_blank_counts_them_all():
    import asyncio

    from tests.tools.test_browser_vault_payment_destination import _CARD_FORM, STRIPE, _desc

    form = [*_CARD_FORM, _desc(len(_CARD_FORM), "", label="Kontoinhaber Vorname")]
    page = _page_with_blanks({"kps": {"not": "a list"}}, {"kps": form})
    i = asyncio.run(page._inspect_payment("", (), (STRIPE,), unreviewed=True, cart=(2266, "EUR")))
    assert i.documents[0].blank is None and i.documents[0].left_for_user == (len(_CARD_FORM),)


def test_a_form_report_that_cannot_be_read_never_breaks_the_fill_result():
    from types import SimpleNamespace

    from tools import browser_vault_payment as bvp

    broken = SimpleNamespace(documents=[SimpleNamespace(controls=None)], merchant_origin=MERCHANT, frame_origin=MERCHANT,
                             target_origins=(MERCHANT,), page_match=("not_checked", ""))
    prompt = {"fields": [], "checks": []}
    out = bvp._describe_result({"success": True}, SimpleNamespace(name="local"), broken, [], prompt, "", False, None)
    assert out["form_complete"] is False and "do not press Pay" in out["next"] and out["kind"] == "payment"
