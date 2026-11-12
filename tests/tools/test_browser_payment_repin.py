"""Re-pinning a re-rendered card document during one approved commit (11-12 SBB/Datatrans stop), without a
browser: the rule that accepts or refuses new pins, the stop record's shape, and the commit's bookkeeping when a
document waits for the card number in a later one. Chromium receipts: test_browser_vault_payment_live.py
(test_datatrans_shaped_re_render_is_re_pinned_inside_one_approval)."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from agent.vault_login_classifier import LoginControl, classify_checkout_control
from tools import browser_supervisor_payment as sp
from tools.browser_supervisor_payment import PaymentDocument, PaymentInspection
from tools.browser_supervisor_payment_repin import located, repin_pins, valid_check, valid_pause

MERCHANT, PSP = "https://shop.example", "https://pay.datatrans.com"


def _row(index, autocomplete, label=""):
    return {"index": index, "autocomplete": autocomplete, "name": "", "label": label, "type": "text",
            "formIndex": None, "maxLength": None}


def _controls(*tokens):
    return tuple(classify_checkout_control(LoginControl.from_dict(_row(n, t))) for n, t in enumerate(tokens))


EXP_CVC_NUMBER = _controls("cc-exp", "cc-csc", "cc-number")
FILLS = ["cc-exp", "cc-csc", "cc-number"]


@pytest.mark.parametrize("rows, kept, written, number_in, expected", [
    # the CVC input was replaced by a fresh one (index 3); the others stay where they were
    ([_row(0, "cc-exp"), _row(2, "cc-number"), _row(3, "cc-csc")], {"cc-exp": 0, "cc-csc": -1, "cc-number": 2},
     ["cc-exp"], False, ({"cc-exp": 0, "cc-csc": 3, "cc-number": 2}, None)),
    # the CVC is hidden until the number is in: it waits (-1), nothing else moves
    ([_row(0, "cc-exp"), _row(2, "cc-number")], {"cc-exp": 0, "cc-csc": 1, "cc-number": 2}, ["cc-exp"], False,
     ({"cc-exp": 0, "cc-csc": -1, "cc-number": 2}, None)),
    # ... and once the number is in, it must be there
    ([_row(0, "cc-exp"), _row(2, "cc-number")], {"cc-exp": 0, "cc-csc": 1, "cc-number": 2}, ["cc-exp", "cc-number"],
     True, (None, "still_missing")),
    # two visible CVC fields: never a guess
    ([_row(0, "cc-exp"), _row(1, "cc-csc"), _row(2, "cc-number"), _row(3, "cc-csc")],
     {"cc-exp": 0, "cc-csc": 1, "cc-number": 2}, ["cc-exp"], False, (None, "token_twice")),
    # a card field the inspection never saw in this document
    ([_row(0, "cc-exp"), _row(1, "cc-csc"), _row(2, "cc-number"), _row(3, "cc-name")],
     {"cc-exp": 0, "cc-csc": 1, "cc-number": 2}, ["cc-exp"], False, (None, "new_token")),
    # the pinned CVC is still shown but re-described as something else, beside a new CVC: no role swap
    ([_row(0, "cc-exp"), _row(1, "off", "Promo code"), _row(2, "cc-number"), _row(3, "cc-csc")],
     {"cc-exp": 0, "cc-csc": 1, "cc-number": 2}, ["cc-exp"], False, (None, "role_changed")),
    # a written field is gone / the number is gone: stop, never defer them
    ([_row(1, "cc-csc"), _row(2, "cc-number")], {"cc-exp": -1, "cc-csc": 1, "cc-number": 2}, ["cc-exp"], False,
     (None, "written_gone")),
    ([_row(0, "cc-exp"), _row(1, "cc-csc")], {"cc-exp": 0, "cc-csc": 1, "cc-number": -1}, ["cc-exp"], False,
     (None, "number_gone")),
])
def test_repin_rule(rows, kept, written, number_in, expected):
    answer = {"fields": rows, "kept": kept, "inspected": _inspected(kept)}
    assert repin_pins(EXP_CVC_NUMBER, FILLS, answer, written, number_in) == expected


def _inspected(kept, controls=EXP_CVC_NUMBER):
    """rebind()'s inspected pairs when the inspected controls are the fill pins (their new index: ``kept``)."""
    return [[c.control.index, kept.get(c.token, c.control.index)] for c in controls]


def _select(index, token, options):
    return {**_row(index, token), "type": "select", "options": [[v, t, False] for v, t in options]}


MONTHS = [(str(k), f"{k:02d}") for k in range(1, 13)]
SELECTS = tuple(classify_checkout_control(LoginControl.from_dict(r)) for r in (
    _select(0, "cc-exp-month", MONTHS), {**_row(1, "cc-exp-year"), "maxLength": 2}, _row(2, "cc-csc"),
    _row(3, "cc-number")))
SELECT_FILLS = ["cc-exp-month", "cc-exp-year", "cc-csc", "cc-number"]


@pytest.mark.parametrize("month, year, csc, expected", [
    (MONTHS, 2, 3, None),  # re-described the same way: accepted
    (MONTHS, 2, 4, None),  # a CVC that grows a digit (brand detection) keeps its value shape
    ([(str(k - 1), f"{k:02d}") for k in range(1, 13)], 2, 3, "shape_changed"),  # zero-based: "7" now shows 08
    (MONTHS, 4, 3, "shape_changed"),  # "35" would sit in a four-digit year field
    (MONTHS, None, 3, "shape_changed"),
])
def test_a_re_pinned_field_keeps_the_shape_its_value_was_computed_for(month, year, csc, expected):
    rows = [_select(0, "cc-exp-month", month), {**_row(1, "cc-exp-year"), "maxLength": year},
            {**_row(2, "cc-csc"), "maxLength": csc}, _row(3, "cc-number")]
    kept = {"cc-exp-month": 0, "cc-exp-year": 1, "cc-csc": 2, "cc-number": 3}
    pins, refused = repin_pins(SELECTS, SELECT_FILLS, {"fields": rows, "kept": kept, "inspected": _inspected(kept, SELECTS)},
                               [], False)
    assert refused == expected and (pins == kept if expected is None else pins is None)
    # an expiry input re-rendered as a select (or back) is another shape too
    rows[1] = _select(1, "cc-exp-year", [("2035", "2035")])
    assert repin_pins(SELECTS, SELECT_FILLS, {"fields": rows, "kept": kept, "inspected": _inspected(kept, SELECTS)},
                      [], False) == (None, "shape_changed")


HOLDER_EXP_NUMBER = _controls("cc-name", "cc-exp", "cc-number")


def test_an_unfilled_inspected_control_keeps_its_role():
    """Codex r1 P1-2: the holder field the saved card does not fill is relabelled as the card number after the number
    input was removed; ``kept`` (fill pins) alone does not see it, the inspected pairs do."""
    rows = [_row(0, "cc-number", "Name on card"), _row(1, "cc-exp")]
    answer = {"fields": rows, "kept": {"cc-exp": 1, "cc-number": -1}, "inspected": [[0, 0], [1, 1], [2, -1]]}
    assert repin_pins(HOLDER_EXP_NUMBER, ["cc-exp", "cc-number"], answer, ["cc-exp"], False) == (None, "role_changed")
    # a non-card control of the inspection (index 5: a promo field) shown now as a CVC is a role change as well
    answer = {"fields": [_row(0, "cc-exp"), _row(2, "cc-number"), _row(5, "cc-csc")],
              "kept": {"cc-exp": 0, "cc-csc": -1, "cc-number": 2}, "inspected": [[0, 0], [1, -1], [2, 2], [3, 5]]}
    assert repin_pins(EXP_CVC_NUMBER, FILLS, answer, ["cc-exp"], False) == (None, "role_changed")
    # every inspected card control must be accounted for
    answer = {"fields": [_row(1, "cc-exp")], "kept": {"cc-exp": 1, "cc-number": -1}, "inspected": [[1, 1], [2, -1]]}
    assert repin_pins(HOLDER_EXP_NUMBER, ["cc-exp", "cc-number"], answer, ["cc-exp"], False) == (None, "unreadable")


@pytest.mark.parametrize("inspected, written, expected", [
    ([[0, -1], [1, -1], [2, -1]], [], (None, "set_changed")),  # all of it replaced by a number-only form
    ([[0, 0], [1, 1], [2, -1]], [], ({"cc-exp": -1, "cc-csc": -1, "cc-number": 5}, None)),  # only hidden: they wait
    ([[0, 0], [1, -1], [2, -1]], ["cc-exp"], ({"cc-exp": 0, "cc-csc": -1, "cc-number": 5}, None)),  # after a write
])
def test_before_any_write_a_removed_field_must_be_shown_again(inspected, written, expected):
    """Opus r1 P3-3: a document re-rendered before its first write into fewer card fields than the inspection saw
    (a gift-card number and PIN in place of the card form) is refused; a field merely hidden may still wait."""
    rows = [_row(5, "cc-number")] if not written else [_row(0, "cc-exp"), _row(5, "cc-number")]
    kept = {"cc-exp": inspected[0][1], "cc-csc": inspected[1][1], "cc-number": -1}
    assert repin_pins(EXP_CVC_NUMBER, FILLS, {"fields": rows, "kept": kept, "inspected": inspected}, written,
                      False) == expected


@pytest.mark.parametrize("answer", [
    None, {"fields": []}, {"fields": [], "kept": {}},  # wrong shape, kept must name every fill token
    {"fields": [_row(1, "cc-csc"), _row(0, "cc-exp")], "kept": dict.fromkeys(FILLS, 0)},  # indexes not increasing
    {"fields": [{**_row(0, "cc-exp"), "value": "07/35"}], "kept": dict.fromkeys(FILLS, 0)},  # an extra key
    {"fields": [], "kept": dict.fromkeys(FILLS, -1), "inspected": [[1, 0], [0, 1], [2, 2]]},  # not increasing
    {"fields": [], "kept": dict.fromkeys(FILLS, -1), "inspected": [[0, 0], [1, 1], [2, True]]},
])
def test_an_unreadable_rebind_is_refused(answer):
    assert repin_pins(EXP_CVC_NUMBER, FILLS, answer, [], False) == (None, "unreadable")


def test_stop_records_carry_fixed_codes_only():
    tokens = frozenset(FILLS)
    assert valid_check({"token": "cc-csc", "reason": "fields_relabeled", "step": 2, "fields": ["cc-csc"],
                        "keys": ["label", "maxLength"]}, tokens, 3)
    for bad in ({"token": "cc-csc", "reason": "because", "step": 1},  # unknown reason
                {"token": "4242", "reason": "hidden", "step": 1},  # never a value
                {"token": "cc-csc", "reason": "hidden", "step": 4},  # past the document's fills
                {"token": "cc-csc", "reason": "hidden", "step": 1, "keys": ["placeholder text"]},
                {"token": "cc-csc", "reason": "hidden", "step": 1, "repin_refused": "token_twice"}):  # supervisor's
        assert not valid_check(bad, tokens, 3), bad
    request = {"repin": True, "filled_fields": 1, "check": {"token": "cc-csc", "reason": "hidden", "step": 2},
               "written": ["cc-exp"]}
    assert valid_pause(request, "repin", tokens, 3, 1)
    assert not valid_pause({**request, "written": []}, "repin", tokens, 3, 1)  # count and tokens disagree
    assert not valid_pause(request, "repin", tokens, 3, 2)  # a count never goes backwards
    assert valid_pause({"deferred": True, "filled_fields": 0}, "deferred", tokens, 1, 0)
    out = located({"success": False, "check": {"token": "cc-csc", "reason": "hidden", "step": 1}}, 2, 3, "role_changed")
    assert out == {"success": False, "fill_check": {"token": "cc-csc", "reason": "hidden", "step": 1, "document": 2,
                                                     "documents": 3, "repin_refused": "role_changed"}}


class _Deferring(sp.PaymentSupervisionMixin):
    """Scripted document answers: the CVC document defers until the number document is written, then finishes
    (or stops) when resumed. Records the order of commit and resume calls."""

    def __init__(self, resumed_answer):
        self.calls, self.resumed_answer = [], resumed_answer

    async def _payment_is_current(self, i, during_fill=False):
        return True

    async def _payment_scope_current(self, i, receiving):
        return True

    async def _shown_documents(self, i):
        return {d.frame_id for d in i.documents}

    async def _payment_retention(self, i, d):
        return [{"token": t, "state": "held", "length": 1} for t in [c.token for c in d.controls]]

    async def _top_navigated(self, i):
        return False

    async def _release_payment_documents(self, i):
        pass

    async def _commit_payment_document(self, d, fills, deadline_ms=None, strict=False, paced=None, repin=None,
                                       resumed=None, exposed=None):
        self.calls.append((d.frame_id, resumed))
        if d.frame_id == "cvc" and resumed is None:
            return {"deferred": True, "filled_fields": 0}
        if d.frame_id == "cvc":
            return self.resumed_answer
        return {"success": True, "filled_fields": len(fills)}


def _deferring_commit(resumed_answer, monkeypatch):
    docs = (PaymentDocument("page", "s", "o-page", PSP, _controls("cc-exp"), clock_anchor=time.monotonic()),
            PaymentDocument("cvc", "s", "o-cvc", PSP, _controls("cc-csc"), clock_anchor=time.monotonic()),
            PaymentDocument("num", "s", "o-num", PSP, _controls("cc-number"), clock_anchor=time.monotonic()))
    i = PaymentInspection("op", "s", "tab", MERCHANT, docs, (), time.monotonic() + 60)
    fills = [{"document": k, "index": 0, "token": t, "value": "x"} for k, t in enumerate(("cc-exp", "cc-csc", "cc-number"))]
    sup = _Deferring(resumed_answer)
    monkeypatch.setattr(sp, "_SETTLE_SECONDS", 0)
    return sup, asyncio.run(sup._commit_payment_once(i, fills, None))


def test_a_deferred_document_is_resumed_once_after_the_number_document(monkeypatch):
    sup, out = _deferring_commit({"success": True, "filled_fields": 1}, monkeypatch)
    assert sup.calls == [("page", None), ("cvc", None), ("num", None), ("cvc", 0)]
    assert out == {"success": True, "filled_fields": 3, "retention_verified": True}


@pytest.mark.parametrize("answer, filled", [
    ({"success": False, "error_type": "payment_fields_changed", "filled_fields": 0, "partially_filled": False}, 2),
    ({"deferred": True, "filled_fields": 0}, 2),  # deferred again after the number: a stop, never a second wait
])
def test_a_resumed_document_that_stops_reports_every_write_so_far(answer, filled, monkeypatch):
    _sup, out = _deferring_commit(answer, monkeypatch)
    assert out["success"] is False and out["error_type"] == "payment_fields_changed"
    assert out["filled_fields"] == filled and out["partially_filled"] is True


# -- The real closure in Node, driven by the real supervisor (commit, re-pin rule, retention) over a fake CDP. --
# Round-2 regressions (Codex r1 P1-1/P1-2/P2, Opus r1 P2-2/P3-1/P3-3): each scenario is a card document whose
# page code re-renders after the approval; the probes of round 1 (.scratch/release-p2/pay-dt/probes) became these.
# The vault's fill order puts the holder first, then the CVC, then the expiry, the card number last.

_DOM = r"""
let mono = 1000;
const performance = {now: () => mono};
globalThis.innerWidth = 800; globalThis.innerHeight = 600;
class Event { constructor(type) { this.type = type; } }
class InputEvent extends Event {}
class FocusEvent extends Event {}
let els = [], all = [], covers = [];
class Field {
  init(tag, a) {
    Object.assign(this, {tagName: tag, autocomplete: '', name: '', id: '', labels: null, placeholder: '', aria: '',
      form: null, maxLength: tag === 'SELECT' ? undefined : -1, disabled: false, readOnly: false, isConnected: true,
      tabIndex: 0, shadowRoot: null, ownerDocument: document, multiple: false, shown: true, top: 40 * all.length,
      style: {setProperty() {}}, onfocus: null, oninput: null, type: tag === 'SELECT' ? 'select-one' : 'text'}, a);
    all.push(this);
    return this;
  }
  getAttribute(n) { return n === 'aria-label' ? (this.aria || null) : null; }
  hasAttribute() { return false; }
  getBoundingClientRect() { return this.shown ? {left: 10, top: this.top, width: 200, height: 20, right: 210,
                                                 bottom: this.top + 20} : {left: 0, top: 0, width: 0, height: 0}; }
  getClientRects() { return this.shown ? [1] : []; }
  getRootNode() { return document; }
  contains(o) { return o === this; }
  focus() { if (this.onfocus) this.onfocus(); }
  blur() {}
  dispatchEvent(e) { if (e.type === 'input' && this.oninput) this.oninput(); return true; }
}
class HTMLInputElement extends Field { get value() { return this.held || ''; } set value(v) { this.held = v; } }
class HTMLSelectElement extends Field { get value() { return this.held || ''; } set value(v) { this.held = v; } }
const document = {forms: [], querySelectorAll: () => els.filter(e => e.isConnected),
  elementFromPoint: (x, y) => covers.find(c => y >= c.top && y < c.top + 20) ||
    els.find(e => e.isConnected && e.shown && y >= e.top && y < e.top + 20) || null};
const location = {href: 'https://pay.example/card'};
const getComputedStyle = e => ({visibility: e.shown ? 'visible' : 'hidden', opacity: '1', pointerEvents: 'auto'});
const input = a => new HTMLInputElement().init('INPUT', a);
const select = a => new HTMLSelectElement().init('SELECT', a);
const months = base => Array.from({length: 12}, (_, k) => ({value: String(k + base), text: String(k + 1).padStart(2, '0'),
                                                            disabled: false}));
const remove = e => { e.isConnected = false; els = els.filter(x => x !== e); };
const replace = (e, a) => { const n = input({autocomplete: e.autocomplete, id: e.id + '2', top: e.top, ...a});
                            els = els.map(x => x === e ? n : x); e.isConnected = false; return n; };
let approve = () => {};  // page code that runs during the approval, after the inspection
SCENARIO
const api = INSPECT;
require('readline').createInterface({input: process.stdin}).on('line', line => {
  const {fn, args} = JSON.parse(line);
  let ok;
  if (fn === 'approve') ok = approve();
  else if (fn === 'values') ok = Object.fromEntries(all.map(e => [e.id, e.value]));
  else ok = api[fn](...args);
  process.stdout.write(JSON.stringify({ok: ok === undefined ? null : ok}) + '\n');
});
"""

_SCENARIOS = {
    # p1: the provider hides the expiry and the CVC until the card number is in (both hidden at commit start)
    "both_hidden": """
const exp = input({id: 'exp', autocomplete: 'cc-exp'}), csc = input({id: 'csc', autocomplete: 'cc-csc'});
const num = input({id: 'num', autocomplete: 'cc-number'});
els = [exp, csc, num];
approve = () => { exp.shown = false; csc.shown = false; };
num.oninput = () => { exp.shown = true; csc.shown = true; };""",
    # p1b: only the expiry, which is not the next field (the CVC is), is hidden until the number is in
    "not_next_hidden": """
const exp = input({id: 'exp', autocomplete: 'cc-exp'}), csc = input({id: 'csc', autocomplete: 'cc-csc'});
const num = input({id: 'num', autocomplete: 'cc-number'});
els = [exp, csc, num];
approve = () => { exp.shown = false; };
num.oninput = () => { exp.shown = true; };""",
    # ... and the same when the first write (the holder's) hides it while the CVC is next
    "hidden_by_a_write": """
const exp = input({id: 'exp', autocomplete: 'cc-exp'}), csc = input({id: 'csc', autocomplete: 'cc-csc'});
const name = input({id: 'name', autocomplete: 'cc-name'}), num = input({id: 'num', autocomplete: 'cc-number'});
els = [name, exp, csc, num];
name.oninput = () => { exp.shown = false; };
num.oninput = () => { exp.shown = true; };""",
    # still hidden once the number is in: a stop, never a second wait
    "never_shown": """
const exp = input({id: 'exp', autocomplete: 'cc-exp'}), csc = input({id: 'csc', autocomplete: 'cc-csc'});
const num = input({id: 'num', autocomplete: 'cc-number'});
els = [exp, csc, num];
approve = () => { csc.shown = false; };""",
    # Codex P1-1: after the CVC write the expiry is replaced by a fresh input under a merchant overlay
    "covered_replacement": """
const exp = input({id: 'exp', autocomplete: 'cc-exp'}), csc = input({id: 'csc', autocomplete: 'cc-csc'});
const num = input({id: 'num', autocomplete: 'cc-number'});
els = [exp, csc, num];
csc.oninput = () => { const n = replace(exp); covers.push({top: n.top}); };""",
    # ... the same replacement, uncovered: written
    "replacement": """
const exp = input({id: 'exp', autocomplete: 'cc-exp'}), csc = input({id: 'csc', autocomplete: 'cc-csc'});
const num = input({id: 'num', autocomplete: 'cc-number'});
els = [exp, csc, num];
csc.oninput = () => { replace(exp); };""",
    # ... and one placed outside the document's viewport
    "offscreen_replacement": """
const exp = input({id: 'exp', autocomplete: 'cc-exp'}), csc = input({id: 'csc', autocomplete: 'cc-csc'});
const num = input({id: 'num', autocomplete: 'cc-number'});
els = [exp, csc, num];
csc.oninput = () => { replace(exp, {top: 900}); };""",
    # Codex P1-2: the saved card has no holder; page code removes the number input and relabels the inspected,
    # unfilled holder input as the card number
    "holder_becomes_number": """
const name = input({id: 'name', autocomplete: 'cc-name', aria: 'Name on card'});
const exp = input({id: 'exp', autocomplete: 'cc-exp'}), num = input({id: 'num', autocomplete: 'cc-number'});
els = [name, exp, num];
exp.oninput = () => { remove(num); name.autocomplete = 'cc-number'; };""",
    # a non-card control of the inspection (a promo code) turned into the expiry beside the gone expiry
    "promo_becomes_expiry": """
const promo = input({id: 'promo', autocomplete: 'off', aria: 'Promo code'});
const exp = input({id: 'exp', autocomplete: 'cc-exp'}), csc = input({id: 'csc', autocomplete: 'cc-csc'});
const num = input({id: 'num', autocomplete: 'cc-number'});
els = [promo, exp, csc, num];
csc.oninput = () => { remove(exp); promo.autocomplete = 'cc-exp'; promo.aria = 'Expiry date'; };""",
    # Codex P2 (p2): the month select is re-listed zero-based on focus: the planned "7" would show as 08
    "month_relisted": """
const month = select({id: 'month', autocomplete: 'cc-exp-month', options: months(1)});
const year = input({id: 'year', autocomplete: 'cc-exp-year', maxLength: 2});
const num = input({id: 'num', autocomplete: 'cc-number'});
els = [month, year, num];
month.onfocus = () => { if (!month.done) { month.done = true; month.options = months(0); } };""",
    # ... the separate year's two-character limit dropped: the planned "35" would sit in a four-digit field
    "year_widened": """
const month = select({id: 'month', autocomplete: 'cc-exp-month', options: months(1)});
const year = input({id: 'year', autocomplete: 'cc-exp-year', maxLength: 2});
const num = input({id: 'num', autocomplete: 'cc-number'});
els = [month, year, num];
month.oninput = () => { year.maxLength = 4; };""",
    # ... while a CVC that grows from 3 to 4 characters (brand detection) is still written
    "cvc_widened": """
const exp = input({id: 'exp', autocomplete: 'cc-exp'}), csc = input({id: 'csc', autocomplete: 'cc-csc', maxLength: 3});
const num = input({id: 'num', autocomplete: 'cc-number'});
els = [exp, csc, num];
csc.onfocus = () => { csc.maxLength = 4; };""",
    # Opus P3-3: during the approval the whole document is replaced by a gift-card form (number and PIN)
    "gift_card": """
const exp = input({id: 'exp', autocomplete: 'cc-exp'}), csc = input({id: 'csc', autocomplete: 'cc-csc'});
const num = input({id: 'num', autocomplete: 'cc-number'});
els = [exp, csc, num];
approve = () => { for (const e of [exp, csc, num]) e.isConnected = false;
  els = [input({id: 'gift', autocomplete: 'cc-number', top: 200}), input({id: 'pin', aria: 'PIN', top: 240})]; };""",
}

_SYNTHETIC = {"card_number": "4111111111111111", "exp_month": "07", "exp_year": "2035", "cvc": "123"}


class _NodeClosure:
    """One card document: the real closure over a fake DOM in a Node process, one JSON line per call."""

    def __init__(self, scenario, tmp_path):
        import re
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            pytest.skip("Node required for the synthetic commit closure")
        script = tmp_path / "closure.js"
        values = {"SCENARIO": _SCENARIOS[scenario], "INSPECT": sp._INSPECT}
        script.write_text(re.sub(r"\bSCENARIO\b|\bINSPECT\b", lambda m: values[m.group(0)], _DOM))
        self.proc = subprocess.Popen([node, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)

    def call(self, fn, *args):
        self.proc.stdin.write(json.dumps({"fn": fn, "args": list(args)}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, self.proc.stderr.read()
        return json.loads(line)["ok"]

    def close(self):
        self.proc.kill()
        self.proc.wait(timeout=10)


class _ClosureSupervisor(sp.PaymentSupervisionMixin):
    """The supervisor's commit, re-pin and retention code over a fake CDP that forwards each closure call to Node.
    ``events`` records closure calls, scope re-proofs and owner-chain checks in order; ``scope``/``owner`` answer
    those checks (by call number)."""

    def __init__(self, js, scope=lambda n: True, owner=lambda n: True):
        self.js, self.events, self.scope, self.owner = js, [], scope, owner

    async def _cdp(self, method, params=None, session_id=None, timeout=None):
        import re

        assert method == "Runtime.callFunctionOn", method
        name = re.search(r"return this\.(\w+)\(", params["functionDeclaration"]).group(1)
        self.events.append(name)
        return {"result": {"result": {"value": self.js.call(name, *[a["value"] for a in params.get("arguments", [])])}}}

    async def _payment_is_current(self, i, during_fill=False):
        return True

    async def _payment_scope_current(self, i, receiving):
        self.events.append("scope")
        return self.scope(self.events.count("scope"))

    async def _paced_check(self, i, d, point):
        self.events.append("owner_check")
        return True if self.owner(self.events.count("owner_check")) else "payment_page_changed"

    async def _shown_documents(self, i):
        return {d.frame_id for d in i.documents}

    async def _top_navigated(self, i):
        return False

    async def _release_payment_documents(self, i):
        pass


def _closure_commit(scenario, tmp_path, monkeypatch, secret=None, **checks):
    """Inspect the scenario's document, build the fills as the vault does, run the approval's page code, commit."""
    from tools.browser_vault_payment import _document_fills

    js = _NodeClosure(scenario, tmp_path)
    try:
        controls = sp._card_controls(js.call("describe"))
        doc = PaymentDocument("card", "s", "o-card", PSP, controls, clock_anchor=time.monotonic())
        i = PaymentInspection("op", "s", "tab", MERCHANT, (doc,), (("card", "L", PSP + "/card", PSP, ""),),
                              time.monotonic() + 60)
        fills = _document_fills(i, secret or _SYNTHETIC)
        js.call("approve")
        sup = _ClosureSupervisor(js, **checks)
        monkeypatch.setattr(sp, "_SETTLE_SECONDS", 0)
        out = asyncio.run(sup._commit_payment_once(i, fills, None))
        return out, js.call("values"), sup.events, fills
    finally:
        js.close()


_FILLED = {"exp": "07/35", "csc": "123", "num": "4111111111111111"}


@pytest.mark.parametrize("scenario, holder", [
    ("both_hidden", None), ("not_next_hidden", None), ("hidden_by_a_write", "JANE DOE"),
])
def test_fields_hidden_until_the_number_wait_for_it_wherever_they_stand(scenario, holder, tmp_path, monkeypatch):
    """Opus r1 P2-2: a field not shown yet waits for the card number whether or not it is next in the queue, at
    commit start or after a write; it is re-pinned when the number is in and must then be shown (and exposed)."""
    secret = {**_SYNTHETIC, **({"cardholder_name": holder} if holder else {})}
    out, values, events, _fills = _closure_commit(scenario, tmp_path, monkeypatch, secret=secret)
    assert out["success"] is True and out["filled_fields"] == 3 + bool(holder) and out["fill_repins"] >= 2, out
    assert values == {**_FILLED, **({"name": holder} if holder else {})}
    assert events.count("owner_check") >= 2  # every re-pinned field, after its focus and before its setter


def test_a_field_still_hidden_after_the_number_stops(tmp_path, monkeypatch):
    out, values, _events, _fills = _closure_commit("never_shown", tmp_path, monkeypatch)
    assert out["success"] is False and out["error_type"] == "payment_fields_changed" and out["filled_fields"] == 2
    assert out["fill_check"]["repin_refused"] == "still_missing" and values == {**_FILLED, "csc": ""}
    assert out["fill_check"]["reason"] == "deferred" and out["fill_check"]["token"] == "cc-csc"


@pytest.mark.parametrize("scenario", ["covered_replacement", "offscreen_replacement"])
def test_a_replacement_the_user_cannot_see_is_never_written(scenario, tmp_path, monkeypatch):
    """Codex r1 P1-1: a re-pinned field must be fully exposed (its centre in the viewport and hit there) right before
    its setter, on a reviewed destination too (strict=False)."""
    out, values, _events, _fills = _closure_commit(scenario, tmp_path, monkeypatch)
    assert out["success"] is False and out["filled_fields"] == 1 and out["partially_filled"] is True, out
    assert out["fill_check"]["reason"] == "hit_test" and out["fill_check"]["token"] == "cc-exp"
    assert values["csc"] == "123" and values["exp2"] == "" and values["num"] == ""


def test_an_exposed_replacement_is_written_after_the_owner_chain_check(tmp_path, monkeypatch):
    out, values, events, _fills = _closure_commit("replacement", tmp_path, monkeypatch)
    assert out["success"] is True and out["filled_fields"] == 3 and out["fill_repins"] == 1, out
    assert values == {"exp": "", "exp2": "07/35", "csc": "123", "num": "4111111111111111"}
    # the frame is re-proved after the rule accepted (P3-1); each re-pinned field's owner chain before its setter
    assert events[events.index("rebind"):events.index("rebind") + 4] == ["rebind", "scope", "resume", "owner_check"]


def test_a_re_pinned_field_whose_owner_chain_fails_is_not_written(tmp_path, monkeypatch):
    out, values, _events, _fills = _closure_commit("replacement", tmp_path, monkeypatch, owner=lambda n: False)
    assert out["success"] is False and out["error_type"] == "payment_page_changed" and out["filled_fields"] == 1
    assert values["exp2"] == "" and values["num"] == ""


def test_a_frame_changed_after_the_rule_accepted_is_not_re_pinned(tmp_path, monkeypatch):
    """Opus r1 P3-1: the scope re-proof runs after rebind() and the rule, right before the closure resumes."""
    out, values, events, _fills = _closure_commit("replacement", tmp_path, monkeypatch, scope=lambda n: False)
    assert out["fill_check"]["repin_refused"] == "frame_changed" and out["filled_fields"] == 1
    assert events.index("rebind") < events.index("scope") and values["exp2"] == ""


@pytest.mark.parametrize("scenario, written", [("holder_becomes_number", {"exp": "07/35"}),
                                               ("promo_becomes_expiry", {"csc": "123"})])
def test_an_inspected_control_never_takes_another_role(scenario, written, tmp_path, monkeypatch):
    """Codex r1 P1-2: every control the inspection judged exposed keeps its role, the unfilled holder field and
    non-card controls included: the number never lands in the field labelled "Name on card"."""
    out, values, _events, _fills = _closure_commit(scenario, tmp_path, monkeypatch)
    assert out["success"] is False and out["fill_check"]["repin_refused"] == "role_changed", out
    assert {k: v for k, v in values.items() if v} == written


@pytest.mark.parametrize("scenario, refused", [("month_relisted", "shape_changed"), ("year_widened", "shape_changed")])
def test_a_re_pinned_expiry_with_another_shape_is_refused(scenario, refused, tmp_path, monkeypatch):
    """Codex r1 P2: the planned value was computed on the inspected options and length; a re-listed month select or
    a widened year field would show another expiry, so the re-pin is refused before that field is written."""
    out, values, _events, fills = _closure_commit(scenario, tmp_path, monkeypatch)
    assert {f["token"]: f["value"] for f in fills}["cc-exp-month"] == "7"  # option "7" is month 07 at inspection
    assert out["success"] is False and out["fill_check"]["repin_refused"] == refused, out
    assert values["num"] == "" and (values["month"] == "" if scenario == "month_relisted" else values["year"] == "")


def test_a_cvc_that_grows_a_digit_is_still_written(tmp_path, monkeypatch):
    out, values, _events, _fills = _closure_commit("cvc_widened", tmp_path, monkeypatch)
    assert out["success"] is True and out["fill_repins"] == 1 and values == _FILLED


def test_a_document_replaced_by_fewer_card_fields_before_any_write_gets_nothing(tmp_path, monkeypatch):
    """Opus r1 P3-3: replaced as a whole during the approval by a gift-card form (a number and a PIN): the inspected
    expiry and CVC left the document, so the number is not written into the new form."""
    out, values, _events, _fills = _closure_commit("gift_card", tmp_path, monkeypatch)
    assert out["success"] is False and out["filled_fields"] == 0, out
    assert out["fill_check"]["repin_refused"] == "set_changed"
    assert not any(values.values())
