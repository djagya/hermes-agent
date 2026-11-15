"""The visibility rule for the frames of an owner-approved (unreviewed) card destination (dlz, 11-15, D-0094 "b").

Two FlixBus / Payrails fills on 11-14 stopped about a second after the card read, nothing written: the guess is the
first focus scrolls the card frame under the merchant's sticky bottom bar, and the strict exposure rule then calls it
covered. By default such frames are now judged as a reviewed provider's are (``_OWNER_VISIBLE``: connected, laid out,
larger than 2x2 px, not hidden, not transparent); ``vault.payment_consent_strict_exposure`` restores the strict rule.

Fake CDP for the Python path (discovery, the post-approval freshness check, the per-setter check, the commit), Node
for the page-side predicates. Synthetic card values only.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.vault_payment_policy import PaymentDestinationError
from tests.tools.test_browser_vault_payment_destination import (_CARD_FORM, _DOM, _KPS_SET, _KPS_URL, KPS, MERCHANT,
                                                                STRIPE, _DiscoveryPage, _node)
from tools import browser_supervisor_payment as sp
from tools import browser_supervisor_payment_stop as stop

OTHER = "https://other-psp.example"
_FRAMES = (("top", "L0", MERCHANT + "/checkout", MERCHANT, ""),
           ("observer", "L1", KPS + "/observability/index.html", KPS, "top"),  # Payrails' 1px telemetry frame
           ("card", "L2", _KPS_URL, KPS, "top"))


def _v(value):
    return {"result": {"result": {"value": value}}}


class _Checkout(_DiscoveryPage):
    """A tap-approved card frame behind a sticky bar. ``owners``: per frame "ok", "covered" (laid out and visible, but
    a merchant element lies over its centre: only the strict rule's hit tests see that) or "hidden" (zero-size,
    display:none, visibility:hidden or opacity:0: each is a case of the Node table below). ``on_focus``: the owner
    state the card frame takes at the first focus (the focus scroll). The closure pauses after each focus (paced)."""

    def __init__(self, owners, on_focus=None, frames=_FRAMES, controls=None):
        super().__init__(frames, exposed=(), controls=controls or {"card": _CARD_FORM})
        self.owners, self.on_focus, self.writes, self.queue, self.done = dict(owners), on_focus, [], [], 0

    def _shown(self, obj, fn):
        state = self.owners.get(obj[len("owner-"):], "ok")
        return state == "ok" or (state == "covered" and "elementFromPoint" not in fn)

    def _next(self):
        if not self.queue:
            return _v({"success": True, "filled_fields": self.done})
        if self.on_focus:
            self.owners["card"], self.on_focus = self.on_focus, None
        return _v({"pending": True, "filled_fields": self.done, "x": 20, "y": 12})

    async def _cdp(self, method, params=None, session_id=None, timeout=None):
        params = params or {}
        if method != "Runtime.callFunctionOn":
            return await super()._cdp(method, params, session_id, timeout)
        obj, fn = params["objectId"], params["functionDeclaration"]
        args = [a.get("value", a.get("objectId")) for a in params.get("arguments", [])]
        if obj.startswith("owner-"):
            if fn in (sp._OWNER_HIT, sp._OWNER_AT):
                return _v([args[2] + 10, args[3] + 10] if self._shown(obj, fn) else None)
            owners = [a for a in args if isinstance(a, str) and a.startswith("owner-")]
            return _v([self._shown(o, fn) for o in owners] if owners else self._shown(obj, fn))
        if "this.commit(" in fn:
            assert args[2] is True and args[3] is True  # strict in its own document, paced
            self.queue, self.done = list(args[0]), 0
            return self._next()
        if "resume()" in fn:
            self.writes.append(self.queue.pop(0)["token"])
            self.done += 1
            return self._next()
        if "retained()" in fn:
            return _v([{"token": t, "state": "held", "length": 1} for t in self.writes])
        if "valid()" in fn:
            return _v(True)
        if "scan()" in fn:
            return _v([])
        return await super()._cdp(method, params, session_id, timeout)


def _inspect(page, strict):
    return asyncio.run(page._inspect_payment("", (), (STRIPE,), unreviewed=True, cart=(2266, "EUR"),
                                             strict_exposure=strict))


def _fill(page, i):
    fills = [{"index": n, "token": t, "value": "x"} for n, t in enumerate(_KPS_SET)]
    return asyncio.run(page._commit_payment(i, fills, None, strict=True))


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    monkeypatch.setattr(sp, "_SETTLE_SECONDS", 0)


@pytest.mark.parametrize("on_focus", [None, "covered"])
def test_a_covered_card_frame_fills_under_the_default_rule(on_focus):
    """Covered from the start (discovery) or from the first focus on (the freshness and per-setter checks): the
    approved frame is still the one the user sees, so every field is written. The observer frame stays out."""
    page = _Checkout({"observer": "hidden", "card": "ok" if on_focus else "covered"}, on_focus=on_focus)
    i = _inspect(page, strict=False)
    assert [d.frame_id for d in i.documents] == ["card"] and i.unreviewed_origins == (KPS,)
    assert stop.exposure_rule(i) == "shown"
    out = _fill(page, i)
    assert out == {"success": True, "filled_fields": len(_KPS_SET), "retention_verified": True}, out
    assert page.writes == list(_KPS_SET)


def test_with_the_strict_switch_a_covered_card_frame_is_refused_as_before():
    """vault.payment_consent_strict_exposure: covered at discovery, only the observer is judged (and it is hidden), so
    the form is not found; covered at the first focus, the fill stops before that field's setter, nothing written,
    and the stop record says which rule ran."""
    page = _Checkout({"observer": "hidden", "card": "covered"})
    with pytest.raises(PaymentDestinationError) as caught:
        _inspect(page, strict=True)
    assert caught.value.error_type == "payment_fields_unsupported"
    assert caught.value.detail == {"unreviewed_frames_inspected": 0}
    page = _Checkout({"observer": "hidden", "card": "ok"}, on_focus="covered")
    i = _inspect(page, strict=True)
    assert stop.exposure_rule(i) == "strict"
    out = _fill(page, i)
    assert out == {"success": False, "error_type": "payment_page_changed", "filled_fields": 0,
                   "partially_filled": False}, out
    assert page.writes == [] and i.stale == "focus_shown"
    assert stop._stopped(out, i, {}) == {"stage": "after_focus", "reason": "shown", "exposure": "strict"}


@pytest.mark.parametrize("at_focus", [False, True])
def test_a_hidden_card_frame_is_still_refused_under_the_default_rule(at_focus):
    """Zero-size, display:none, visibility:hidden or opacity:0 (the Node table): not shown, at discovery or after the
    first focus (nothing written)."""
    if not at_focus:
        with pytest.raises(PaymentDestinationError) as caught:
            _inspect(_Checkout({"observer": "hidden", "card": "hidden"}), strict=False)
        assert caught.value.error_type == "payment_fields_unsupported"
        return
    page = _Checkout({"observer": "hidden"}, on_focus="hidden")
    i = _inspect(page, strict=False)
    out = _fill(page, i)
    assert out["error_type"] == "payment_page_changed" and out["filled_fields"] == 0 and page.writes == [], out
    assert stop._stopped(out, i, {}) == {"stage": "after_focus", "reason": "shown", "exposure": "shown"}


def test_a_card_frame_moved_to_another_origin_after_the_approval_writes_nothing():
    """The approval binds the card frame's origin: a frame that navigated elsewhere fails the commit's first check."""
    page = _Checkout({"observer": "hidden", "card": "covered"})
    i = _inspect(page, strict=False)
    page.frames = tuple(("card", "L9", OTHER + "/card", OTHER, "top") if f[0] == "card" else f for f in page.frames)
    out = _fill(page, i)
    assert out == {"success": False, "error_type": "payment_page_changed"} and page.writes == [], out


def test_a_second_unreviewed_origin_is_refused_even_when_covered():
    """One unreviewed origin per fill: a card form on a second one is ambiguous. Under the default rule a covered copy
    counts as shown (the strict rule skipped it), so it refuses rather than picking one."""
    frames = _FRAMES + (("decoy", "L4", OTHER + "/customer/wallet/pc/WalletInput.jsp", OTHER, "top"),)
    controls = {"card": _CARD_FORM, "decoy": _CARD_FORM}
    for decoy, strict in (("ok", False), ("covered", False), ("ok", True)):
        with pytest.raises(PaymentDestinationError) as caught:
            _inspect(_Checkout({"observer": "hidden", "decoy": decoy}, frames=frames, controls=controls), strict)
        assert caught.value.error_type == "payment_fields_ambiguous", (decoy, strict)
    page = _Checkout({"observer": "hidden", "decoy": "covered"}, frames=frames, controls=controls)
    assert [d.origin for d in _inspect(page, strict=True).documents] == [KPS]  # strict: the covered copy is skipped


def test_a_re_inspection_keeps_the_rule_of_its_approval():
    page = _Checkout({"observer": "hidden", "card": "covered"})
    prior = _inspect(page, strict=False)
    again = asyncio.run(page._inspect_payment("", (), (STRIPE,), True, (2266, "EUR"), prior=prior))
    assert again.strict_exposure is False and [d.frame_id for d in again.documents] == ["card"]
    prior.strict_exposure = True
    with pytest.raises(PaymentDestinationError):
        asyncio.run(page._inspect_payment("", (), (STRIPE,), True, (2266, "EUR"), prior=prior))


# ---------------------------------------------------------------------------- page-side predicates under Node

_OWNER_HARNESS = _DOM + r"""
const location = {href: 'https://shop.example/checkout', origin: 'https://shop.example'};
const spec = __CASE__;
const el = node(R(...spec.rect), spec.css, body, {clientLeft: 0, clientTop: 0});
hit = spec.covered ? body : el;
const VISIBLE = __VISIBLE__, AT = __AT__;
process.stdout.write(JSON.stringify({visible: VISIBLE(el), at: AT.call(el, spec.href || location.href,
  location.origin, 5, 5)}));
"""


@pytest.mark.parametrize("case, shown", [
    ({"rect": [100, 100, 300, 120]}, True),
    ({"rect": [100, 100, 300, 120], "covered": True}, True),  # a sticky bar over its centre: still shown
    ({"rect": [100, 100, 300, 120], "css": {"pointerEvents": "none"}}, True),  # no hit test at all
    ({"rect": [100, 100, 0, 0]}, False),  # display:none (no box, no client rects) or zero-size
    ({"rect": [100, 100, 2, 2]}, False),  # 2x2 px or smaller (the SDK observer is 1x1)
    ({"rect": [100, 100, 300, 120], "css": {"visibility": "hidden"}}, False),
    ({"rect": [100, 100, 300, 120], "css": {"opacity": "0"}}, False),
    ({"rect": [100, 100, 300, 120], "href": "https://shop.example/elsewhere"}, None),  # the parent navigated
])
def test_the_default_owner_predicates(case, shown):
    case = {"css": {}, "covered": False, **case}
    script = (_OWNER_HARNESS.replace("__VISIBLE__", sp._OWNER_VISIBLE).replace("__AT__", "(" + sp._OWNER_AT + ")")
              .replace("__CASE__", json.dumps(case)))
    out = _node(script)
    assert out["at"] == ([105, 105] if shown else None), out
    if shown is not None:
        assert out["visible"] is shown, out
