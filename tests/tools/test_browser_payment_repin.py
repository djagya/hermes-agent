"""Re-pinning a re-rendered card document during one approved commit (11-12 SBB/Datatrans stop), without a
browser: the rule that accepts or refuses new pins, the stop record's shape, and the commit's bookkeeping when a
document waits for the card number in a later one. Chromium receipts: test_browser_vault_payment_live.py
(test_datatrans_shaped_re_render_is_re_pinned_inside_one_approval)."""

from __future__ import annotations

import asyncio
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
    assert repin_pins(EXP_CVC_NUMBER, FILLS, {"fields": rows, "kept": kept}, written, number_in) == expected


@pytest.mark.parametrize("answer", [
    None, {"fields": []}, {"fields": [], "kept": {}},  # wrong shape, kept must name every fill token
    {"fields": [_row(1, "cc-csc"), _row(0, "cc-exp")], "kept": dict.fromkeys(FILLS, 0)},  # indexes not increasing
    {"fields": [{**_row(0, "cc-exp"), "value": "07/35"}], "kept": dict.fromkeys(FILLS, 0)},  # an extra key
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
                                       resumed=None):
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
