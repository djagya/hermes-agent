"""Where a refused card-fill re-inspection stopped and how its window was spent (dlz, 11-14;
tools.browser_supervisor_payment_stop), and the lock-aware texts of a stop with nothing written. Without a browser.
The real Payrails shape: test_browser_vault_payment_payrails_live.py."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from agent.vault_payment_policy import PaymentDestinationError, PaymentPolicyError
from tests.tools.test_browser_payment_fill_auto import _APPROVED, _fresh, _loop, _reading
from tools import browser_supervisor_payment as sp
from tools import browser_supervisor_payment_auto as spa
from tools import browser_supervisor_payment_stop as stop

SHOP, PAY = "https://shop.example", "https://pay.example"
_TOP = ("top", "L0", SHOP + "/pay", SHOP, "")
_PAGE_CHOSEN = ["https://john-q-public-4111.evil.example", "4242424242424242", "Card number", "/pay?session=abc",
                "John Q Public", "cc-number; drop table", "A" * 40, -1, 1.5, None, 10**9]


def test_a_stop_record_carries_counts_and_fixed_codes_and_nothing_a_page_chose():
    """Whatever a page (or a bug) puts into a record, only the schema's keys with their own types and enumerated
    codes leave it (an allow-list): keys and strings a pattern would have passed are dropped too."""
    dirty = {"polls": 3, "ms": 1200, "inspected_ms_ago": 5000, "leak": _PAGE_CHOSEN, "cardholder": "john",
             "codes": {"incomplete": 2, "john": 1, "cardholder": 3, "https://evil.example": 1},
             "errors": {"runtimeerror": 1, "secret_4242": 1}}
    clean = stop.clean_window(dirty)
    assert clean == {"polls": 3, "ms": 1200, "inspected_ms_ago": 5000, "codes": {"incomplete": 2},
                     "errors": {"runtimeerror": 1}}
    assert stop.clean_window({"polls": "3", "ms": -1, "codes": {"john": 1}}) is None and stop.clean_window("x") is None
    assert not any(w in json.dumps(clean) for w in ("john", "evil", "secret", "4242"))
    assert stop._clean({"stage": "before_write", "reason": "frames_card", "x": 1}, stop.STOPPED_SCHEMA) == \
        {"stage": "before_write", "reason": "frames_card"}
    assert stop.stopped_record("after_approval", "john_card") == {"stage": "after_approval", "reason": "unknown"}
    assert stop.stopped_record("nowhere", "frames_card_error") == {"stage": "in_document", "reason": "frames_card_error"}


def test_a_record_reads_a_container_only_up_to_its_bound():
    """Bounded iteration before allocation: an endless mapping is read through islice, never copied."""
    import itertools

    class Wide(dict):
        def items(self):
            return ((f"k{n}", 1) for n in itertools.count())

    assert stop.clean_window({"codes": Wide()}) is None


class _Clock:
    """A controlled clock: every reading is ``step`` later than the one before, so a window of ``wait`` seconds holds
    a fixed number of polls whatever the scheduler does."""

    def __init__(self, step=0.01):
        self.now, self.step = time.monotonic(), step

    def monotonic(self):
        self.now += self.step
        return self.now


def test_a_window_that_ends_without_a_matching_form_leaves_its_record_and_an_adopted_one_none(monkeypatch):
    """The refused window: how often each code came (an exception by its class name, never its text), in the stop
    record at once. The form still refuses. A window that ends in a matching reading leaves no record."""
    clock = _Clock()
    monkeypatch.setattr(spa, "time", clock)
    monkeypatch.setattr(stop, "time", clock)
    _sup, prior, fresh, refused = _loop([RuntimeError("ws closed: https://evil.example/?card=4242"),
                                         PaymentDestinationError("payment_fields_unsupported", "x",
                                                                 {"unreviewed_frames_inspected": 1})],
                                        monkeypatch, wait=0.05)
    window = prior.approval["window"]
    assert fresh is None and refused == "incomplete" and prior.approval["seen"] == {"unreviewed_frames_inspected": 1}
    assert window["codes"]["unavailable"] == 1 and window["codes"]["incomplete"] == window["polls"] - 1 >= 1
    assert window["errors"] == {"runtimeerror": 1} and window["ms"] >= 10
    assert set(window) == {"polls", "ms", "codes", "errors", "inspected_ms_ago"}  # nothing of the page
    assert 0 <= window["inspected_ms_ago"] - 240_000 < 5000  # the test's approval window ends in 60 s of its 300
    monkeypatch.undo()  # the real clock again for the adopted reading
    assert "evil" not in json.dumps(window) and "4242" not in json.dumps(window)
    _sup, prior, fresh, refused = _loop([PaymentPolicyError("Missing card expiry fields"), _fresh(_APPROVED, "match")],
                                        monkeypatch)
    assert refused == "" and fresh.operation_id == "match" and "window" not in prior.approval


def test_the_armed_result_names_where_a_page_stop_fell_and_how_the_window_was_spent_only_when_refused():
    st = {"reinspections": 0, "repairs": 0, "refused": "incomplete", "wrote": 0, "seen": {"unreviewed_frames_inspected": 1},
          "stopped": {"stage": "before_write", "reason": "frames_card"},
          "window": {"polls": 24, "ms": 9000, "codes": {"incomplete": 24}, "page_text": "John Q Public"}}
    out = spa._auto_result({"success": False, "error_type": "payment_page_changed"}, st)
    assert out["fill_reinspect"] == {"count": 0, "repairs": 0, "refused": "incomplete",
                                     "seen": {"unreviewed_frames_inspected": 1},
                                     "stopped": {"stage": "before_write", "reason": "frames_card"},
                                     "window": {"polls": 24, "ms": 9000, "codes": {"incomplete": 24}}}
    done = spa._auto_result({"success": True, "filled_fields": 3}, {**st, "reinspections": 1, "refused": ""})
    assert done["fill_reinspect"] == {"count": 1, "repairs": 0}


@pytest.mark.parametrize("result, stale, stage, expected", [
    ({"error_type": "payment_page_changed"}, "frames_card", "before_write", {"stage": "before_write", "reason": "frames_card"}),
    ({"error_type": "payment_page_changed"}, "focus_shown", "", {"stage": "after_focus", "reason": "shown"}),
    ({"error_type": "payment_page_changed"}, "focus_owner_hit", "", {"stage": "after_focus", "reason": "owner_hit"}),
    ({"error_type": "payment_page_changed"}, "scope_frame", "between_documents",
     {"stage": "between_documents", "reason": "scope_frame"}),
    # a document's own closure refused and no check of the approved page is on record: not "between documents"
    ({"error_type": "payment_fields_changed"}, "", "", {"stage": "in_document", "reason": "unknown"}),
    ({"error_type": "payment_page_changed"}, "frames_card", "", {"stage": "in_document", "reason": "unknown"}),
    ({"error_type": "payment_fields_changed", "fill_check": {"reason": "fields_replaced"}}, "x", "", None),
    ({"error_type": "payment_fields_not_retained"}, "x", "", None),
])
def test_a_generation_stop_is_placed_before_a_write_after_a_focus_or_between_documents(result, stale, stage, expected):
    st = {"stage": stage} if stage else {}
    assert stop._stopped(result, type("I", (), {"stale": stale})(), st) == expected and "stage" not in st


def test_a_stop_with_nothing_written_under_the_payment_lock_names_payment_close_not_the_page():
    """A managed fill claims the payment lock before the card is read, and a zero-write stop keeps it: the agent cannot
    read the page then, so the text sends it to payment_close and a new fill. Without the lock, and for a partial
    stop, the texts are the ones they were."""
    from tools import browser_vault_payment as bvp

    def text(result, locked):
        bvp._commit_text(result, locked=locked)
        return result["error"]

    for error_type in ("payment_page_changed", "payment_fields_changed", "payment_cart_changed"):
        locked = text({"success": False, "error_type": error_type, "filled_fields": 0}, True)
        assert "payment_close" in locked and "no card field was written" in locked, locked
        assert "Check the page" not in locked and "Check the cart" not in locked, locked
        assert text({"success": False, "error_type": error_type, "filled_fields": 0}, False) == bvp._COMMIT_TEXTS[error_type][0]
        partial = {"success": False, "error_type": error_type, "filled_fields": 1, "partially_filled": True}
        assert text(dict(partial), False) == bvp._COMMIT_TEXTS[error_type][1]
        assert text(dict(partial), True) == bvp._COMMIT_TEXTS[error_type][1].replace(
            bvp._SUBMITTED_NOTHING, bvp._SUBMITTED_NOTHING_LOCKED)
    # an expiry under the lock names payment_close and a fresh approval; partial stays a stop
    expired = {"success": False, "error_type": "payment_consent_expired"}
    assert text(dict(expired), False) == bvp._CONSENT_EXPIRED
    locked = text(dict(expired), True)
    assert "payment_close" in locked and "new approval" in locked and "no card field was written" in locked
    partial = {**expired, "filled_fields": 1, "partially_filled": True}
    assert "do not retry" in text(dict(partial), True) and "do not retry" in bvp._CONSENT_EXPIRED_PARTIAL
    uncertain = {**expired, "write_uncertain": True}
    assert text(dict(uncertain), False) == bvp._TRANSPORT_UNCERTAIN
    assert "do not retry" in text(dict(uncertain), True)
    # the expiry's lock text is true of a stop before any write, also after a field's focus
    focused = {**expired, "filled_fields": 0, "fill_reinspect": {"stopped": {"stage": "after_focus", "reason": "expired"}}}
    after_focus = text(focused, True)
    assert "before any card field could be written" in after_focus and "before the fill reached the page" not in after_focus
    assert "payment_close" in after_focus and "no card field was written" in after_focus
    # the cart stop keeps "with the cart as the page shows it now"
    assert "then call fill once more with the cart as the page shows it now" in bvp._CART_CHANGED_LOCKED


def test_a_split_fill_stop_between_documents_names_the_check_that_refused():
    """Each refusal of ``_payment_scope_current`` leaves a fixed code, so ``between_documents`` says what."""
    from tools.browser_supervisor_payment import PaymentDocument, PaymentInspection

    card = ("card", "L1", PAY + "/form", PAY, "top")

    class Page(sp.PaymentSupervisionMixin):
        def __init__(self, loader="L1", owners=(True,)):
            self.loader, self.owners = loader, owners

        async def _cdp(self, method, params=None, session_id=None, timeout=10.0):
            if method == "Page.getFrameTree":
                return {"result": {"frameTree": {"frame": {"id": "card", "loaderId": self.loader,
                                                           "url": PAY + "/form", "securityOrigin": PAY}}}}
            return {"result": {"result": {"value": list(self.owners)}}}

    def stale(page, owners=None):
        d = PaymentDocument("card", "s-card", "o", PAY, ())
        i = PaymentInspection("op", "page", "tab", SHOP, (d,), (_TOP, card), time.monotonic() + 60,
                              child_sessions={"card": "s-card"},
                              frame_owners=owners if owners is not None else {"top": ("page", (("card", "pin"),))})
        verdict = asyncio.run(page._payment_scope_current(i, [d]))
        return verdict, i.stale

    assert stale(Page()) == (True, "")
    assert stale(Page(loader="L9")) == (False, "scope_frame")
    assert stale(Page(owners=(False,))) == (False, "scope_owner")
    assert stale(Page(), owners={}) == (False, "scope_unpinned")
    assert stop._stopped({"error_type": "payment_page_changed"}, type("I", (), {"stale": "scope_frame"})(),
                         {"stage": "between_documents"}) == {"stage": "between_documents", "reason": "scope_frame"}


def test_a_split_fill_checks_the_owner_scope_with_the_exposure_rule_only_under_the_strict_switch():
    """r11 (Opus final review): between the documents of an unreviewed split fill, the strict switch
    (``vault.payment_consent_strict_exposure``) asks each parent for the owner <iframe>'s scope with the exposure test
    (``_OWNERS_SCOPE_EXPOSED``); the default rule asks without it (``_OWNERS_SCOPE``). An owner that is shown but not
    exposed stops the strict fill at ``scope_owner`` and passes the default one."""
    from tools.browser_supervisor_payment import PaymentDocument, PaymentInspection

    card = ("card", "L1", PAY + "/form", PAY, "top")
    asked: list = []

    class Page(sp.PaymentSupervisionMixin):
        async def _cdp(self, method, params=None, session_id=None, timeout=10.0):
            if method == "Page.getFrameTree":
                return {"result": {"frameTree": {"frame": {"id": "card", "loaderId": "L1",
                                                           "url": PAY + "/form", "securityOrigin": PAY}}}}
            fn = params["functionDeclaration"]
            asked.append(fn)
            return {"result": {"result": {"value": [fn != sp._OWNERS_SCOPE_EXPOSED]}}}  # shown, but not exposed

    def scope(strict):
        d = PaymentDocument("card", "s-card", "o", PAY, ())
        i = PaymentInspection("op", "page", "tab", SHOP, (d,), (_TOP, card), time.monotonic() + 60,
                              child_sessions={"card": "s-card"}, frame_owners={"top": ("page", (("card", "pin"),))},
                              unreviewed_origins=(PAY,), strict_exposure=strict)
        asked.clear()
        return asyncio.run(Page()._payment_scope_current(i, [d])), i.stale

    assert scope(True) == (False, "scope_owner") and asked == [sp._OWNERS_SCOPE_EXPOSED]
    assert scope(False) == (True, "") and asked == [sp._OWNERS_SCOPE]


def test_a_page_stop_before_the_claim_names_payment_close_only_when_the_slot_is_locked(monkeypatch):
    """Before the claim the slot is normally unlocked ("check the page" stays); a lock still held from an earlier fill
    (``locally_locked``, no status read) gets the lock-aware text. An object the lock record cannot hold is not locked."""
    from types import SimpleNamespace

    from tools import browser_vault_payment as bvp

    sup = SimpleNamespace(payment_is_current=lambda i: False)
    assert bvp._locally_locked(sup) is False and bvp._locally_locked(object()) is False
    assert bvp._page_refusal(sup, None, False, None)["error"] == bvp._PAGE_CHANGED
    monkeypatch.setattr(bvp, "_locally_locked", lambda supervisor: True)
    assert bvp._page_refusal(sup, None, False, None)["error"] == bvp._PAGE_CHANGED_LOCKED
    assert bvp._reinspect_stop("incomplete", None, True)["error"] == bvp._PAGE_CHANGED_LOCKED
    assert bvp._reinspect_stop("cart_changed", None, False)["error"] == bvp._CART_CHANGED
    stopped = bvp._reinspect_stop("incomplete", SimpleNamespace(stale="cardholder john", approval={}), False)
    assert stopped["fill_reinspect"]["stopped"] == {"stage": "after_approval", "reason": "unknown"}
    window = {"polls": 2, "ms": 800, "codes": {"incomplete": 2}, "url": "https://evil.example"}
    kept = bvp._reinspect_stop("incomplete", SimpleNamespace(stale="", approval={"window": window}), False)
    assert kept["fill_reinspect"]["window"] == {"polls": 2, "ms": 800, "codes": {"incomplete": 2}}


_LOCKED_FILLED = ("The browser is now locked for this payment, so the page cannot be read or checked", "do not retry",
                  "a handoff", "bank")


@pytest.mark.parametrize("error_type, extra", [
    ("payment_page_changed", {"filled_fields": 1, "partially_filled": True}),
    ("payment_fields_not_retained", {"filled_fields": 3}),
    ("payment_validation_failed", {"filled_fields": 3}),
    ("payment_transport_uncertain", {"write_uncertain": True}),
    ("payment_result_invalid", {"filled_fields": 0}),
    ("payment_browser_changed", {"filled_fields": 1, "partially_filled": True}),
    ("human_has_control", {"filled_fields": 3, "partially_filled": True}),
    ("payment_destination_disabled", {"filled_fields": 1, "partially_filled": True}),
])
def test_a_stop_with_fields_written_or_unknown_under_the_lock_sends_the_agent_to_the_user_not_to_the_page(
        error_type, extra):
    """The page cannot be read under the lock, so "check the page before anything else" is not available: the locked text
    keeps what may have happened (fields hold values, an order may exist), says do not submit and do not retry, asks the
    user to look and to check the merchant, the email and the bank, and puts payment_close after that look. Unlocked,
    the text is the one it was."""
    from tools import browser_vault_payment as bvp

    def text(locked):
        result = {"success": False, "error_type": error_type, **extra}
        bvp._commit_text(result, locked=locked)
        return result["error"]

    unlocked, locked = text(False), text(True)
    assert "Check the page before anything else" in unlocked
    assert "Check the page" not in locked and "check the page" not in locked, locked
    assert "may exist" in locked and "do not retry" in locked and "bank" in locked, locked
    if error_type in ("payment_browser_changed", "human_has_control"):
        # r6 P2: the browser is no longer Hermes's, so payment_close is not the way out even after the user looked.
        assert "do not call payment_close" in locked and "only after the user has looked" not in locked, locked
    else:
        assert all(part in locked.lower() or part in locked for part in _LOCKED_FILLED), locked
        assert "only after the user has looked" in locked, locked


def test_the_other_zero_write_stops_after_the_claim_name_the_lock(monkeypatch):
    """After the claim (a managed browser holds the lock) a stop with nothing written has no page to check either:
    expiry-option mismatch, a changed or unrevisioned card on read, an incomplete card, a switch turned off after the
    read, a changed browser. Not locked, each text is the one it was."""
    from tools import browser_vault_payment as bvp

    bare = {"success": False, "filled_fields": 0}
    pairs = [("payment_expiry_option_unmatched", bvp._EXPIRY_UNMATCHED, bvp._EXPIRY_UNMATCHED_LOCKED),
             ("payment_fields_incomplete", bvp._FIELDS_INCOMPLETE, bvp._FIELDS_INCOMPLETE_LOCKED),
             ("payment_browser_changed", bvp._BROWSER_CHANGED, bvp._BROWSER_CHANGED_LOCKED),
             ("human_has_control", bvp._BROWSER_CHANGED, bvp._BROWSER_CHANGED_LOCKED)]
    for error_type, plain, locked in pairs:
        assert bvp._lock_aware({**bare, "error_type": error_type, "error": plain}, False)["error"] == plain
        assert bvp._lock_aware({**bare, "error_type": error_type, "error": plain}, True)["error"] == locked
        assert "payment_close" in locked and "no card field was written" in locked, locked
    assert "do not call payment_close" in bvp._BROWSER_CHANGED_LOCKED  # a browser that is no longer Hermes' to change
    for text in (bvp._ITEM_CHANGED_ON_READ_LOCKED, bvp._REVISION_UNAVAILABLE_ON_READ_LOCKED,
                 bvp._DESTINATION_DISABLED_LOCKED):
        assert "payment_close" in text and "locked" in text and "Check the" not in text, text
    assert "no card field was written" in bvp._ITEM_CHANGED_ON_READ_LOCKED
    assert bvp._DESTINATION_DISABLED_LOCKED.startswith(bvp._DESTINATION_DISABLED)
    assert bvp._lock_aware({**bare, "error_type": "payment_item_changed", "error": bvp._ITEM_CHANGED_ON_READ}, True,
                           bvp._ITEM_CHANGED_ON_READ_LOCKED)["error"] == bvp._ITEM_CHANGED_ON_READ_LOCKED
    written = {"success": False, "error_type": "payment_browser_changed", "filled_fields": 1, "partially_filled": True,
               "error": bvp._PARTIAL_STOPPED}
    assert bvp._lock_aware(dict(written), True)["error"] == bvp._PARTIAL_STOPPED  # a write is never a zero-write text


def _generation(commit_document, scope_current=True):
    """A real ``_commit_generation`` over two card documents; the closure's answer and the scope check are scripted."""
    class Sup(sp.PaymentSupervisionMixin):
        async def _payment_is_current(self, i, during_fill=False):
            return True

        async def _payment_scope_current(self, i, receiving):
            return scope_current

        async def _commit_payment_document(self, document, fills, **kw):
            return commit_document(document)

    inspected = _reading(_APPROVED)
    fills = [{"document": n, "token": t, "index": 0, "value": "x"} for n, t in enumerate(("cc-exp", "cc-number"))]
    st = {"reinspections": 0, "repairs": 0, "refused": "", "wrote": 0}
    again, result = asyncio.run(Sup()._commit_generation(inspected, fills, time.monotonic() + 30, False, "fill", False, st))
    return again, result, inspected, st


def test_a_check_between_documents_places_the_stop_there_and_a_closure_stop_without_a_check_does_not():
    """``between_documents`` only where ``_commit_step``'s own checks before a later document refuse; a document's own
    closure refusing (no check of the approved page on record) is ``in_document`` with reason ``unknown``."""
    def first_then(refusal):
        calls = []

        def commit(document):
            calls.append(document)
            return {"success": True, "filled_fields": 1} if len(calls) == 1 else refusal
        return commit

    # the scope check before the second document refuses
    again, _result, inspected, st = _generation(first_then(None), scope_current=False)
    assert again == "changed"
    inspected.stale = "scope_frame"
    assert stop._stopped(_result, inspected, st) == {"stage": "between_documents", "reason": "scope_frame"}
    # the second document's own closure refuses (the scope check held): no stage from a check
    again, result, inspected, st = _generation(first_then({"success": False, "error_type": "payment_fields_changed",
                                                           "filled_fields": 0}))
    assert again == "changed" and "stage" not in st
    assert stop._stopped(result, inspected, st) == {"stage": "in_document", "reason": "unknown"}
