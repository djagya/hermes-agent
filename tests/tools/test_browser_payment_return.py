"""D-0878 a (owner 2026-11-24): Sera takes a card payment back after the owner took the tab and pressed Return to
Sera. The real CDPSupervisor (its ``_run`` loop, the 403 exit, the registry, the real attach) on a fake
browser-control socket that also serves the payment tab's page, entered through the vault tool
(``_payment_surface_action``). The fake control keeps the contract the monolith side builds: a ticket from the lock's
native connection, an adoption with that ticket on a later connection, every other connection refused while locked.
D-0942 a, D-0961 (owner 2026-11-25): after the return only the owner's tap on a fresh approval card lets Sera
press, and then she presses what the checkout needs; the card says what one read of the page showed (that read never
decides a press) and how often she already pressed Pay. The fresh card goes through the real ``fill_payment``
(facts, card text, claim, binding); the inspection and the commit are faked.
No real Chrome; synthetic data."""

from __future__ import annotations

import asyncio
import itertools
import json
import re
import secrets
import threading
import time
from types import SimpleNamespace

import pytest
import websockets

from tests.tools import test_browser_payment_detect as shop
from tests.tools.test_browser_payment_done import STRIPE
from tests.tools.test_browser_payment_native_recovery import FakeWS
from tests.tools.test_browser_payment_surface_honesty import _row
from tests.waiting import wait_until
from tools import browser_payment_detect as detect
from tools import browser_payment_done as done
from tools import browser_payment_return as ret
from tools import browser_payment_surface as surface
from tools import browser_supervisor as bs
from tools import browser_supervisor_payment as sp
from tools import browser_vault_payment as bvp
from tools import browser_vault_tool as vault_tool
from tools import payment_consent_prompt as prompt

TICKET, ADOPT = "Hermes.paymentSurfaceTicket", "Hermes.adoptPaymentSurface"  # control-v1 ``cdp_methods``
LEASE = "ws://127.0.0.1:9/devtools/browser/lease-%d"
INSPECTED = SimpleNamespace(page_id="TAB", merchant_origin=shop.SHOP, tree=shop.CHECKOUT, unreviewed_origins=(),
                            documents=(SimpleNamespace(frame_id="CARD", origin=STRIPE,
                                                       controls=(SimpleNamespace(token="cc-number"),)),))
PAID = {"page": shop.THANKS, "rows": [_row("Weiter einkaufen", role="link"), _row("Jetzt kaufen", role="link", i=1)]}
CART = {"items": [{"description": "16 Pakete", "quantity": 1}], "total": "22.66", "currency": "EUR"}
APPROVE = "Approve confirms you have not already paid this purchase. Sera fills the card and presses Pay for 22.66 EUR."
PRESS_ONLY = ("Approve confirms you have not already paid this purchase. Sera presses Pay for 22.66 EUR (the card is "
              "already entered on the page).")
PRESSED = "<b>Sera already pressed Pay %s in this payment; check your e-mail or bank before you approve.</b>"
META = SimpleNamespace(id="h", kind="payment", label="Card", allowed_origins=(shop.SHOP,), origin=None,
                       payment_scope="bound", payment_frame_origins=())
BACKEND = SimpleNamespace(name="local", get_meta=lambda h: META, resolve_secret=lambda h: {"n": "synthetic"})
_ids = itertools.count(1)


class Refused(Exception):
    """The WebSocket upgrade's 403: control refuses a lease that ended."""

    status_code = 403


class Control:
    """browser-control as the fork sees it over CDP: one live agent lease, the lock with its native connection and
    its adoption ticket; every other call is answered by the payment tab's page (the detection tests' shop)."""

    def __init__(self):
        self.page = shop.ShopSup([shop.CHECKOUT])
        self.page.page.update(shop.CART)
        self.lease, self.sockets, self.calls = 1, [], []
        self.lock, self.native, self.ticket, self.adopts = set(), None, None, True
        self.fault, self.fired = None, False  # (index in ``calls``, "fail" | "slow" | "never"): that call's reply
        self.adopt_reply = None  # what a granted adoption answers instead
        self.tabs = {}  # target id -> (opener id or None, url): other tabs of the browser

    async def connect(self, url, **_kwargs):
        if url != LEASE % self.lease:
            raise Refused()
        self.sockets.append(FakeWS(self))
        return self.sockets[-1]

    def handle(self, ws, msg):
        at = len(self.calls)
        self.calls.append((ws, msg.get("method"), msg.get("params") or {}))
        try:
            reply = {"id": msg["id"], "result": self.answer(ws, *self.calls[-1][1:], msg.get("sessionId"))}
        except RuntimeError as exc:
            reply = {"id": msg["id"], "error": {"message": str(exc)}}
        if self.fault and self.fault[0] == at:  # control did its part; the answer is what goes wrong
            self.fired = True
            if self.fault[1] == "fail":
                return {"id": msg["id"], "error": {"message": "injected"}}
            if self.fault[1] == "slow":
                ws.loop.call_later(0.6, ws.queue.put_nowait, json.dumps(reply))
            return None
        return reply

    def answer(self, ws, method, params, sid):
        if self.native is not None and self.native.closed:  # the lock's native connection ended; the lock stays
            self.native = None
        locked = bool(self.lock)
        if method == surface.CLAIM_METHOD:
            if locked and ws is not self.native:
                raise RuntimeError("payment_surface_unavailable")
            self.lock, self.native = self.lock | set(params["tabs"]), ws
            return {"claimed": True, "locked": True, "tabs": sorted(self.lock)}
        if method == surface.STATUS_METHOD:
            return {"locked": locked, "native": locked and ws is self.native, "tabs": sorted(self.lock),
                    "unverified": False}
        if method == TICKET:
            if not (self.adopts and locked and ws is self.native):
                raise RuntimeError("cdp_method_blocked" if not self.adopts else "payment_surface_unavailable")
            self.ticket = secrets.token_hex(32)
            return {"ticket": self.ticket}
        if method == ADOPT:
            if not (self.adopts and locked and self.native is None and self.ticket
                    and params.get("ticket") == self.ticket):
                raise RuntimeError("cdp_method_blocked" if not self.adopts else "payment_surface_unavailable")
            self.native, self.ticket = ws, None
            return self.adopt_reply or {"adopted": True, "locked": True, "tabs": sorted(self.lock)}
        if locked and ws is not self.native and method != "Target.getTargets":
            raise RuntimeError("payment_surface_locked")  # the splice's filter on every connection but the native
        if method == "Target.attachToTarget":
            tab = params["targetId"]
            return {"sessionId": "S-page" if tab == "TAB" else "S-other"}
        if method == "Target.closeTarget" and params.get("targetId") == "TAB":
            self.lock, self.native, self.ticket = set(), None, None
        out = self.page.answer(method, params, sid)
        if method == "Target.getTargets":
            out["targetInfos"] += [{"targetId": tab, "type": "page", "url": url, **({"openerId": opener} if opener else {})}
                                   for tab, (opener, url) in self.tabs.items()]
        if method == "Page.getFrameTree":  # as Chrome answers it: every frame with its origin
            origins = {row[0]: row[3] for row in self.page.tree}
            nodes = [out["frameTree"]]
            for node in nodes:
                node["frame"]["securityOrigin"] = origins[node["frame"]["id"]]
                nodes.extend(node["childFrames"])
        return out


class Flow:
    """One purchase of one task: Sera's fill, the owner's take and return, Sera's payment calls."""

    def __init__(self, env):
        self.ctl, self.monkeypatch = Control(), env.monkeypatch
        env.ctl = self.ctl
        self.task = f"t-return-{next(_ids)}"
        env.tasks.append(self.task)

    def sup(self):
        return vault_tool._managed_supervisor(self.task)

    def fill(self):
        """A card fill the owner approved with the total on its card: the claim, and the total bound to it."""
        sup = self.sup()
        assert surface.claim_payment_surface(sup, INSPECTED) is None
        assert surface.record_approved_total(sup, 2266, "EUR", ("TOP", "L0"), shop.SHOP) is True
        return sup

    def take(self):
        """The owner takes the tab: control fences Sera's lease and closes its sockets; her supervisor's redial is
        refused and it ends."""
        first = bs.SUPERVISOR_REGISTRY.get(self.task)
        self.ctl.lease, self.ctl.native = 0, None
        for ws in self.ctl.sockets:
            if not ws.closed:
                ws.drop()
        wait_until(lambda: not first._thread.is_alive(), what="the fenced supervisor ended")
        assert bs.SUPERVISOR_REGISTRY.get(self.task) is None

    def give_back(self):
        self.ctl.lease = 2  # Return to Sera: a new agent lease on the same slot, the lock kept

    def act(self, ref=None, **kwargs):
        return json.loads(vault_tool._payment_surface_action(self.task, ref, **kwargs))

    def press(self, caption, **kwargs):
        """payment_controls, then payment_click on the top document's control ``caption`` (a refusal of either)."""
        listed = self.act()
        if not listed.get("success"):
            return listed
        with surface._LOCK:
            issued = dict(surface._ISSUED.get(self.sup()) or {})
        key = "|".join(self.ctl.page.tree[0][:2])
        refs = [r for r, spec in issued.items() if spec[0] == key and spec[3] == caption]
        assert len(refs) == 1, (caption, sorted(issued.values()), listed)
        return self.act(refs[0], **kwargs)

    def presses(self):
        return [c for c in self.ctl.calls if c[1] == "Input.dispatchMouseEvent" and c[2].get("type") == "mousePressed"]

    def close(self):
        return json.loads(vault_tool._payment_close_action(self.task))

    def card_fill(self, answer="accept", commit=True, cart=CART, during=None, past=False):
        """Sera fills the card (again) through the real ``fill_payment``: ``(its result, the approval card as the
        owner saw it or None)``. ``answer``: the owner's (accept, decline, cancel: no answer in time); ``during``: runs
        while the card waits; ``commit``: whether the fill itself then succeeds; ``past``: the page has no card form
        (True: past the card step, no card control at all; "cvc": a saved card's CVC-only form in a hosted frame).
        The discovery pass (on by default, as live) ends as the real one does on such a page."""
        sup, cards = self.sup(), []
        inspected = SimpleNamespace(
            **vars(INSPECTED), consumed=False, owner=sup, frame_origin=STRIPE, operation_id="f" * 32,
            expires_at=time.monotonic() + 120, target_origins=(STRIPE,), page_match=("matched", "the merchant page"),
            merchant_match="matched", prompt=lambda: {"merchant_origin": shop.SHOP, "target_origin": STRIPE,
                                                      "fields": ["cc-number"], "checks": []})

        def inspect(*a, unreviewed=False, **k):
            if not sup.snapshot().active:  # as the real inspection: it reads the attached payment tab
                raise bvp.PaymentPolicyError("Supervised browser required")
            if past and unreviewed:
                cvc = (("CVC", "s", "o", "https://pay.example", (SimpleNamespace(token="cc-csc"),)),)
                sp._discovery_documents(list(cvc if past == "cvc" else ()), shop.SHOP, (), 1)
            if past:
                raise bvp.PaymentPolicyError("Missing, ambiguous or split payment documents")
            return inspected

        sup.inspect_payment, sup.payment_is_current = inspect, lambda i: True
        sup.arm_payment_auto, sup.discard_payment = lambda i: False, lambda i: None
        sup.commit_payment = lambda i, f, strict=False: {"success": commit}

        def consent(message, description, **kwargs):
            cards.append(prompt.render_html(prompt.PaymentPromptFacts.from_dict(kwargs["payment"]), budget_utf16=4096))
            if during:
                during(inspected)
            return answer

        self.monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", consent)
        out = json.loads(bvp.fill_payment(BACKEND, META, self.task, cart=cart))
        return out, (cards[0] if cards else None)


@pytest.fixture
def env(monkeypatch):
    env = SimpleNamespace(ctl=None, tasks=[], monkeypatch=monkeypatch)
    real_sleep = asyncio.sleep

    async def connect(url, **kwargs):
        return await env.ctl.connect(url, **kwargs)

    monkeypatch.setattr(websockets, "connect", connect)
    monkeypatch.setattr(asyncio, "sleep", lambda delay, *a: real_sleep(min(delay, 0.02), *a))  # the redial backoff
    monkeypatch.setattr(bs, "PAYMENT_RECOVERY_POLL_S", 0.05)
    monkeypatch.setattr(surface, "_SETTLE_S", 0.02)
    monkeypatch.setattr(surface, "_SETTLE_POLL_S", 0.01)
    monkeypatch.setattr(done, "_WAIT_S", 2.0)
    monkeypatch.setattr(done, "_POLL_S", 0.05)
    monkeypatch.setattr(vault_tool, "_is_managed", lambda: True)
    # the live config (monolith hermes/identity/config.yaml: vault.payment_destination_consent: true)
    monkeypatch.setattr(bvp, "destination_policy", lambda: (True, "gen", False))
    monkeypatch.setattr("tools.browser_use_cli.vault_route",
                        lambda task_id: {"lease": "l", "cdp": LEASE % env.ctl.lease, "token": "t"})
    monkeypatch.setattr("tools.browser_control_route._lease_alive", lambda url, lease: True)
    monkeypatch.setattr("tools.browser_control_route.control_url", lambda: "http://control.invalid")
    monkeypatch.setattr("tools.browser_session_cdp.resolve_cdp_override", lambda url: url)
    monkeypatch.setattr("tools.browser_session_cdp.get_dialog_policy_config",
                        lambda: (bs.DEFAULT_DIALOG_POLICY, bs.DEFAULT_DIALOG_TIMEOUT_S))
    # the fill's other steps (test_browser_payment_surface's ``fill`` fixture): the card read and its guards
    monkeypatch.setattr(vault_tool, "_bot_desktop_browser_session", lambda task_id: False)
    monkeypatch.setattr(prompt, "payment_pay_once_enabled", lambda: True)
    monkeypatch.setattr(bvp, "validate_card", lambda secret: secret)
    monkeypatch.setattr(bvp, "effective_frame_origins", lambda origins: origins)
    monkeypatch.setattr(bvp, "_register_card_redaction", lambda secret, supervisor=None: None)
    monkeypatch.setattr(bvp, "_document_fills", lambda i, s: [{"index": 0, "token": "cc-number", "document": 0}])
    monkeypatch.setattr("tools.browser_control_route.set_capture_guard", lambda lease, token: (True, ""))
    monkeypatch.setattr("tools.browser_payment_privacy.mark_payment_session", lambda *a, **k: None)
    yield env
    for task in env.tasks:
        bs.SUPERVISOR_REGISTRY.stop(task)


def _returned(env, pressed=False, shows=None):
    """Fill, (Sera's own Pay press, after which the checkout still shows its Pay control,) take, (what the page
    shows when the owner is done with it: ``shows``,) Return to Sera."""
    flow = Flow(env)
    flow.fill()
    if pressed:
        assert flow.press("JETZT KAUFEN")["pay_pressed"] is True
    flow.take()
    for key, value in (shows or {}).items():
        flow.ctl.page.page.update(value) if key == "page" else setattr(flow.ctl.page, key, value)
    flow.give_back()
    return flow


def test_after_a_return_only_the_fresh_cards_approval_presses_and_then_what_the_checkout_needs(env):
    flow = _returned(env)
    first = bs.SUPERVISOR_REGISTRY.get(flow.task)
    listed = flow.act()  # Sera's next payment call: her new connection adopted the lock and its record
    assert listed["success"] is True and listed["returned"] is True and "payment_detected" not in listed
    assert "fill the card again" in listed["next"] and listed["presses_in_lock"] == 0
    sup, sockets = flow.sup(), flow.ctl.sockets
    assert flow.ctl.native is sockets[1] and len(sockets) == 2 and sup.snapshot().active is True
    attached = [c for c in flow.ctl.calls if c[0] is sockets[1] and c[1] == "Target.attachToTarget"]
    assert [c[2]["targetId"] for c in attached] == ["TAB"], "the payment tab, and no dialog bridge on a locked slot"
    assert not [c for c in flow.ctl.calls if c[0] is sockets[1] and c[1] == "Fetch.enable"]
    # the approval from before the take does not count, and his word in chat does not either (D-0922 a)
    stale = flow.press("JETZT KAUFEN")
    assert stale["error_type"] == surface.GO_REQUIRED and "No approval from before counts" in stale["error"]
    told = flow.press("JETZT KAUFEN", owner_go=True)
    assert told["error_type"] == surface.GO_REQUIRED and "fill the card again" in told["error"]
    assert "himself" not in told["error"] and flow.presses() == []
    # the fresh card (D-0942 a): what the page showed, and what his tap confirms
    scans = len([c for c in flow.ctl.calls if c[1] == "Runtime.evaluate"])
    out, card = flow.card_fill()
    assert out["success"] is True
    assert APPROVE in card and "<b>No order wording seen on the checked page.</b> This check only advises" in card
    assert "Sera presses through its steps" in card and "already pressed Pay" not in card
    assert "go in chat" not in card and "yourself" not in card
    assert len([c for c in flow.ctl.calls if c[1] == "Runtime.evaluate"]) == scans + 1, "one read of the page"
    assert "next" not in flow.act(), "a fresh approval is bound"
    # D-0961: then Sera presses what the checkout needs: Pay, the shop's own confirm step, Pay once more
    assert flow.press("JETZT KAUFEN")["pay_pressed"] is True
    assert flow.press("BESTÄTIGEN")["success"] is True
    assert flow.press("JETZT KAUFEN")["pay_pressed"] is True and len(flow.presses()) == 3
    listed = flow.act()
    assert listed["returned"] is True and listed["presses_in_lock"] == 3, "the lock stays a returned one"
    assert surface.record_approved_total(first, 2266, "EUR", ("TOP", "L0"), shop.SHOP) is False, "the ended connection"


def test_returned_card_names_earlier_press(env):
    """Opus M1: Sera pressed Pay before the take, and its order went unseen (another tab, an e-mail later, an
    uncertain press). The fresh card says so, and payment_controls counts it; a re-fill and a second return keep
    counting. A second return also voids the fresh approval, and owner_go never counts again in this lock."""
    flow = _returned(env, pressed=True)
    assert flow.act()["presses_in_lock"] == 1
    out, card = flow.card_fill()
    assert out["success"] is True and APPROVE in card and PRESSED % "1 time" in card
    assert flow.press("JETZT KAUFEN")["pay_pressed"] is True
    assert PRESSED % "2 times" in flow.card_fill()[1], "a re-fill in the same lock"
    flow.take()
    flow.ctl.lease = 3  # Return to Sera again, the re-fill's approval unused
    assert flow.act()["presses_in_lock"] == 2 and flow.ctl.native is flow.ctl.sockets[2]
    assert flow.press("JETZT KAUFEN")["error_type"] == surface.GO_REQUIRED and len(flow.presses()) == 2
    assert PRESSED % "2 times" in flow.card_fill()[1] and flow.press("JETZT KAUFEN")["pay_pressed"] is True
    assert surface.claim_payment_surface(flow.sup(), INSPECTED) is None  # a later fill that bound no total
    assert flow.press("JETZT KAUFEN", owner_go=True)["error_type"] == surface.GO_REQUIRED and len(flow.presses()) == 3


def test_press_only_card_offered_with_discovery_on(env):
    """D-0961, with the live config (discovery on: it finds no card control at all on this page): after the return
    the page is past the card step (no card form; the total and Pay on the shop's page). The fill gives the same card
    without the fill: declined, nothing is bound; approved, Pay is pressed. A page with no Pay-like control (the
    thank-you page after the owner paid), one that does not show the total, and a lock that was never returned get no
    such card: the fill's refusal stands."""
    flow = _returned(env, pressed=True)
    out, card = flow.card_fill("decline", past=True)
    assert out["error_type"] == "payment_declined" and PRESS_ONLY in card
    assert flow.press("JETZT KAUFEN")["error_type"] == surface.GO_REQUIRED and len(flow.presses()) == 1
    # a second return while the card waits, adopted before the tap: that tap binds nothing
    out, card = flow.card_fill(past=True, during=lambda _i: (flow.take(), flow.give_back(), flow.act()))
    assert out["error_type"] == "payment_browser_changed" and PRESS_ONLY in card
    assert flow.press("JETZT KAUFEN")["error_type"] == surface.GO_REQUIRED and len(flow.presses()) == 1
    out, card = flow.card_fill(past=True)
    assert out["success"] is True and out["press_only"] is True and "payment_controls" in out["message"]
    assert PRESS_ONLY in card and PRESSED % "1 time" in card and "<b>No order wording seen on the checked page." in card
    assert "One tap covers one purchase at this checkout (Sera presses through its steps)" in card
    assert "Card fields go to" not in card and "Card: " not in card and "fills the card" not in card
    assert "next" not in flow.act(), "the tap bound the total"
    assert flow.press("JETZT KAUFEN")["pay_pressed"] is True and len(flow.presses()) == 2
    for shows in (THANKS, {"pin": NO_TOTAL}, None):
        other = _returned(env, shows=shows) if shows else Flow(env)
        if not shows:  # never returned
            other.fill()
        out, card = other.card_fill(past=True)
        assert card is None and out["error_type"] == "payment_fields_unsupported" and other.presses() == [], out


def test_press_only_card_offered_with_discovery_off(env):
    """The same card with discovery off: the reviewed pass's coverage refusal leads to it."""
    env.monkeypatch.setattr(bvp, "destination_policy", lambda: (False, "gen", False))
    flow = _returned(env)
    out, card = flow.card_fill(past=True)
    assert out["success"] is True and out["press_only"] is True and PRESS_ONLY in card
    assert flow.press("JETZT KAUFEN")["pay_pressed"] is True and len(flow.presses()) == 1


def test_incomplete_card_form_with_discovery_on_keeps_refusal(env):
    """A form discovery finds but cannot fill (a saved card's CVC-only form) is not a page past the card step: no
    press-only card, its refusal stands, nothing is bound or pressed."""
    flow = _returned(env)
    out, card = flow.card_fill(past="cvc")
    assert card is None and out["error_type"] == "payment_fields_unsupported", out
    assert flow.press("JETZT KAUFEN")["error_type"] == surface.GO_REQUIRED and flow.presses() == []


def _reload(flow):
    """The shop's page loads again as a new top document (same frame, new loader)."""
    old = flow.ctl.page.tree
    flow.ctl.page.tree = ((old[0][0], "L9", *old[0][2:]),) + tuple(old[1:])


def test_press_only_after_reload_before_press_gets_new_card(env):
    """Opus r6 L2: the press-only approval binds the top document it was read in. That document reloads before the
    first press: the press is refused and Sera is sent to fill again, which gives a fresh press-only card; after its
    tap she presses. Once a press of an approval was admitted, no further press-only card replaces it."""
    flow = _returned(env, pressed=True)
    assert flow.card_fill(past=True)[0]["press_only"] is True
    _reload(flow)
    first = flow.press("JETZT KAUFEN")
    assert first["error_type"] == surface.TOTAL_CHANGED and "payment_close" in first["error"], first
    assert len(flow.presses()) == 1
    out, card = flow.card_fill(past=True)
    assert out["press_only"] is True and PRESS_ONLY in card and PRESSED % "1 time" in card, out
    assert flow.press("JETZT KAUFEN")["pay_pressed"] is True and len(flow.presses()) == 2
    out, card = flow.card_fill(past=True)
    assert card is None and out["error_type"] == "payment_fields_unsupported", out


_HANDS_OFF = re.compile(r"tell (him|the owner)|himself|yourself|by hand|go in chat|unless danil|ask the owner",
                        re.IGNORECASE)
PIN = {"state": "matched", "labelled": [2266], "stated": 1, "unclear": False}  # the page shows the stated total
NO_TOTAL = {"state": "not_found", "labelled": [], "stated": 0, "unclear": False}
SHIPPING = {"page": {"title": "Versand", "text": "Lieferadresse"}, "rows": [_row("Weiter zur Zahlung")], "pin": NO_TOTAL}
THANKS = {"page": shop.THANKS, "rows": [_row("Weiter einkaufen", role="link")], "tree": shop.SAME_DOC}  # no card frame


def _reopen(flow):
    """After payment_close Sera's turn ends (its supervisor with it) and she opens the checkout again: the card form."""
    bs.SUPERVISOR_REGISTRY.stop(flow.task)
    page = flow.ctl.page
    page.tab_open, page.tree, page.rows, page.pin = True, shop.CHECKOUT, list(shop.SUNDAY_ROWS), dict(PIN)
    page.page.update(shop.CART)


def _walk(flow, out, card, past=False, order=False, pressed=False):
    """Sera carries out the step each answer names. Within three calls (``out`` is the first one's answer) she holds
    a fresh approval card that names the return and her Pay press, or payment_done handed the order over; no answer
    comes twice and none hands a step to the owner."""
    seen = []
    for call in range(3):
        if out.get("order_evidence"):
            return "payment_done"
        if card is not None:
            assert "came back to Sera" in card and (APPROVE in card or PRESS_ONLY in card), card
            assert (PRESSED % "1 time" in card) is pressed and ("already pressed" in card) is pressed, card
            return "card"
        text = out.get("error") or out["next"]
        assert text not in seen and not _HANDS_OFF.search(text) and call < 2, (call, seen, text)
        seen.append(text)
        if "Retry this call" in text:
            out = flow.act()
        elif order and "payment_done" in text:
            out = json.loads(vault_tool._payment_done_action(flow.task, ""))
        elif "payment_close" in text:
            assert flow.close()["lock_released"] is True, text
            _reopen(flow)
            out, card = flow.card_fill("decline")  # the walk ends at the card: his answer is his
        else:
            assert "fill the card again" in text, (call, text)
            out, card = flow.card_fill("decline", past=past)


@pytest.mark.parametrize("state", ["card form", "Pay page", "thank-you page", "shipping step", "total changed after a press",
                                   "discovery off", "CVC-only form", "second return while a card waits",
                                   "second return while the card is read", "lock met before the take back"])
def test_every_page_a_returned_payment_can_be_on_leads_to_a_fresh_card_or_payment_done(env, monkeypatch, state):
    """D-0961 (owner 2026-11-25: "i don't want to do amy manual work at all"), approach r8: whatever the payment tab
    shows after the return, and whenever a second return or a late adoption cuts in, the answers lead Sera to a
    fresh approval card or to payment_done (``_walk``); the way out of a page that takes neither a fill nor a
    press-only card is the base one (payment_close, the checkout again, fill), and its card still warns."""
    if state == "discovery off":
        monkeypatch.setattr(bvp, "destination_policy", lambda: (False, "gen", False))
    shows = {"thank-you page": THANKS, "shipping step": SHIPPING, "discovery off": SHIPPING}.get(state)
    flow = _returned(env, pressed=state in ("Pay page", "shipping step"), shows=shows)
    again = lambda _i: (flow.take(), flow.give_back(), flow.act())
    past = {"card form": False, "CVC-only form": "cvc", "total changed after a press": True}.get(state, True)
    pressed = state in ("Pay page", "shipping step", "total changed after a press")
    if state == "total changed after a press":  # her approved press went out; the shop then shows another page
        assert flow.card_fill(past=True)[0]["press_only"] is True
        assert flow.press("JETZT KAUFEN")["pay_pressed"] is True
        flow.ctl.page.pin = NO_TOTAL
        out, card = flow.press("JETZT KAUFEN"), None
        assert out["error_type"] == surface.TOTAL_CHANGED and len(flow.presses()) == 1, out
    elif state == "second return while a card waits":
        out, card = flow.card_fill(during=again)[0], None
        assert out["error_type"] == "payment_browser_changed", out
        past = False
    elif state == "second return while the card is read":  # after the claim, under the lock
        monkeypatch.setattr(bvp, "validate_card", lambda secret: (again(None), secret)[1])
        out, card = flow.card_fill()[0], None
        assert out["error_type"] == "payment_browser_changed" and out["returned"] is True, out
        monkeypatch.setattr(bvp, "validate_card", lambda secret: secret)
        past = False
    elif state == "lock met before the take back":  # a supervisor that dialled before the record was parked
        with surface._LOCK:
            parked = ret._PARKED.pop(flow.task)
        assert flow.act()["error_type"] == surface.LOCK_FOREIGN, "nothing parked yet: a foreign lock, as before"
        with surface._LOCK:
            ret._PARKED[flow.task] = parked
        out, card = surface.native_lost_refusal(bs.SUPERVISOR_REGISTRY.get(flow.task)), None
        assert out["error_type"] == surface.LOCK_FOREIGN, out
        past = False
    else:
        out, card = flow.card_fill("decline", past=past)
    assert _walk(flow, out, card, past, order=state == "thank-you page", pressed=pressed) == (
        "payment_done" if state == "thank-you page" else "card")
    if state == "thank-you page":
        _reopen(flow)  # the purchase is finished: the task's next one is no returned payment
        assert "came back" not in flow.card_fill()[1]


def test_close_then_refill_after_return_keeps_warning_lines(env):
    """Approach r8 Medium: Sera pressed Pay once, the owner took the tab and returned it on a page that takes no
    fill. payment_close and the checkout again: the next card still asks him to confirm he has not already paid and
    names her press (D-0942 a), his word in chat still does not count, and the count goes on; only payment_done's
    confirmation ends that, and a lock that never came back starts as before."""
    flow = _returned(env, pressed=True, shows=SHIPPING)
    assert flow.card_fill(past=True)[1] is None
    assert flow.close()["lock_released"] is True
    _reopen(flow)
    out, card = flow.card_fill(cart=None)  # a fill-only card: no total bound
    assert out["success"] is True and "came back to Sera" in card and PRESSED % "1 time" in card, card
    assert flow.press("JETZT KAUFEN", owner_go=True)["error_type"] == surface.GO_REQUIRED and len(flow.presses()) == 1
    out, card = flow.card_fill()
    assert out["success"] is True and APPROVE in card and PRESSED % "1 time" in card, card
    listed = flow.act()
    assert listed["returned"] is True and listed["presses_in_lock"] == 0, "this lock's own presses"
    assert flow.press("JETZT KAUFEN")["pay_pressed"] is True and len(flow.presses()) == 2
    assert flow.close()["lock_released"] is True  # once more, now with two presses
    _reopen(flow)
    flow.ctl.page.after = dict(PAID)
    assert PRESSED % "2 times" in flow.card_fill()[1] and flow.press("JETZT KAUFEN")["payment_detected"] is True
    assert json.loads(vault_tool._payment_done_action(flow.task, ""))["success"] is True
    _reopen(flow)
    out, card = flow.card_fill()
    assert out["success"] is True and "came back" not in card and "already pressed" not in card
    other = Flow(env)  # a payment that never came back: payment_close and a re-fill give the card as before
    other.fill()
    assert other.press("JETZT KAUFEN")["pay_pressed"] is True and other.close()["lock_released"] is True
    _reopen(other)
    assert "came back" not in other.card_fill()[1]


@pytest.mark.parametrize("shows, tabs, line", [
    # the owner paid: thank-you and order number inline, next to the still enabled Pay button
    ({"page": {"text": shop.CART["text"] + " Vielen Dank für Ihre Bestellung! Bestellnummer 10023"}}, {},
     "Order wording seen on the checked page."),
    # the owner paid: the confirmation is in a tab he opened himself, or a noopener tab; the checkout looks untouched
    ({}, {"DANKE": (None, shop.SHOP + "/danke")}, "No order wording seen on the checked page."),
    # nobody paid: the checkout's own service text reads as order wording
    ({"page": {"text": shop.CART["text"] + " Once your order has been placed you will receive an email."}}, {},
     "Order wording seen on the checked page."),
])
def test_what_the_page_shows_after_a_return_only_changes_the_cards_line(env, shows, tabs, line):
    """D-0942 a: inline confirmation next to an enabled Pay, a manual or noopener confirmation tab and ordinary
    checkout service wording neither permit nor refuse a press: without the owner's tap nothing is pressed, with it
    Pay is pressed."""
    flow = _returned(env, shows=shows)
    flow.ctl.tabs.update(tabs)
    assert flow.press("JETZT KAUFEN")["error_type"] == surface.GO_REQUIRED
    out, card = flow.card_fill("decline")
    assert out["error_type"] == "payment_declined" and f"<b>{line}</b>" in card and APPROVE in card
    assert flow.press("JETZT KAUFEN")["error_type"] == surface.GO_REQUIRED and flow.presses() == []
    out, card = flow.card_fill()
    assert out["success"] is True and f"<b>{line}</b>" in card
    assert flow.press("JETZT KAUFEN")["pay_pressed"] is True and len(flow.presses()) == 1
    assert {c[2]["targetId"] for c in flow.ctl.calls if c[1] == "Target.attachToTarget"} == {"TAB"}


@pytest.mark.parametrize("how", ["decline", "cancel", "unshown", "expired", "fill_failed", "fill_only", "card_from_before"])
def test_after_a_return_a_card_that_was_not_approved_shown_or_filled_presses_nothing(env, how):
    """Declined, not answered in time, not shown, answered after its window, a fill that failed after the tap, a
    fill-only card (no stated total), and a card shown before the take and approved after the return: nothing is
    bound, so Pay is refused, owner_go too."""
    flow = _returned(env, pressed=True) if how != "card_from_before" else Flow(env)
    if how == "card_from_before":
        flow.fill()

    def boom(inspected):
        raise RuntimeError("no card could be sent")

    def late(inspected):
        inspected.expires_at = time.monotonic() - 1

    def taken(inspected):
        flow.take()
        flow.give_back()

    out, card = flow.card_fill(
        answer=how if how in ("decline", "cancel") else "accept", commit=how != "fill_failed",
        cart=None if how == "fill_only" else CART,
        during={"unshown": boom, "expired": late, "card_from_before": taken}.get(how))
    assert out.get("error_type") == {
        "decline": "payment_declined", "cancel": "payment_consent_missing", "unshown": "payment_consent_missing",
        "expired": "payment_consent_expired", "card_from_before": "payment_browser_changed"}.get(how), out
    if how == "fill_only":
        assert "Pay needs a new card that covers it" in card and APPROVE not in card and "go in chat" not in card
    for owner_go in (False, True):
        refused = flow.press("JETZT KAUFEN", owner_go=owner_go)
        assert refused["error_type"] == surface.GO_REQUIRED and "fill the card again" in refused["error"]
    assert len(flow.presses()) == (0 if how == "card_from_before" else 1), "only Sera's press before the take"


@pytest.mark.parametrize("returned", [True, False])
def test_an_order_detected_in_a_lock_is_still_known_after_a_re_fill(env, returned):
    """Sera's approved Pay press shows the thank-you: detected. The page then goes back to the checkout and the card
    is filled again in the same lock (a new approval): Pay is still refused as already made."""
    flow = _returned(env) if returned else Flow(env)
    assert flow.card_fill()[0]["success"] is True
    flow.ctl.page.after = dict(PAID)
    assert flow.press("JETZT KAUFEN")["payment_detected"] is True
    flow.ctl.page.page.update(shop.CART)
    flow.ctl.page.rows = list(shop.SUNDAY_ROWS)
    out, card = flow.card_fill()
    assert out["success"] is True and (APPROVE in card) is returned
    assert flow.press("JETZT KAUFEN")["error_type"] == detect.SPENT and len(flow.presses()) == 1
    assert detect.detected(flow.sup())["evidence"] == "confirmation_text"


def test_after_a_return_a_shops_confirm_dialog_is_accepted_only_after_the_fresh_approval(env):
    """A shop's own "Place order?" confirm waits on the payment tab after the return. Accepting it could place the
    order without the owner's tap: refused (Sera dismisses it and fills the card again, nothing is left to him);
    dismissing works. After his tap on the fresh card a confirm is accepted as on a lock never returned."""
    flow = _returned(env)
    assert flow.act()["returned"] is True
    sup, ws = flow.sup(), flow.ctl.native

    def confirm():
        event = {"method": "Page.javascriptDialogOpening", "sessionId": "S-page",
                 "params": {"type": "confirm", "message": "Bestellung absenden?", "url": shop.SHOP + "/checkout/",
                            "frameId": "TOP", "defaultPrompt": ""}}
        ws.loop.call_soon_threadsafe(ws.queue.put_nowait, json.dumps(event))
        wait_until(lambda: sup.snapshot().pending_dialogs, what="the confirm is held")
        return sup.snapshot().pending_dialogs[0].id

    def answer(dialog_id, accept):
        return json.loads(vault_tool._payment_dialog_action(flow.task, dialog_id, accept))

    def answered():
        return [c[2] for c in flow.ctl.calls if c[1] == "Page.handleJavaScriptDialog"]

    dialog_id = confirm()
    out = answer(dialog_id, True)
    assert out["error_type"] == "payment_dialog_returned" and "fill the card again" in out["error"]
    assert "himself" not in out["error"] and answered() == []
    assert answer(dialog_id, False)["answered"] == "dismiss" and answered() == [{"accept": False}]
    assert flow.card_fill("decline")[0]["error_type"] == "payment_declined"
    assert answer(confirm(), True)["error_type"] == "payment_dialog_returned", "a declined card binds nothing"
    assert flow.card_fill()[0]["success"] is True
    assert answer(sup.snapshot().pending_dialogs[0].id, True)["answered"] == "accept"
    assert answered()[-1] == {"accept": True} and flow.presses() == []


def test_without_the_adoption_in_control_the_returned_payment_is_lost_not_foreign(env):
    """An older control (no ticket, no adoption): the record still moves to the new supervisor, so Sera is told to
    have the owner check the order, and nothing is pressed; once the lock ends the supervisor attaches as usual."""
    flow = Flow(env)
    flow.ctl.adopts = False
    flow.fill()
    flow.take()
    flow.give_back()
    out = flow.press("JETZT KAUFEN", owner_go=True)
    assert out["error_type"] == surface.NATIVE_LOST and "check the order" in out["error"]
    assert flow.presses() == [] and flow.sup().snapshot().active is False
    sent = [c[1] for c in flow.ctl.calls if c[0] is flow.ctl.sockets[1]]
    assert "Target.attachToTarget" not in sent and ADOPT not in sent, "no ticket: nothing to ask control for"
    flow.ctl.page.tab_open, flow.ctl.lock = False, set()  # the payment tab closed
    wait_until(lambda: flow.sup().snapshot().active, what="attach after the unlock")


@pytest.mark.parametrize("reply", [{"adopted": True, "locked": True, "tabs": ["OTHER"]},
                                   {"adopted": False, "locked": True, "tabs": ["TAB"]}])
def test_an_adoption_control_does_not_confirm_exactly_leaves_the_payment_lost_for_good(env, reply):
    flow = Flow(env)
    flow.ctl.adopt_reply = reply
    flow.fill()
    flow.take()
    flow.give_back()
    assert flow.act()["error_type"] == surface.NATIVE_LOST
    bs.SUPERVISOR_REGISTRY.stop(flow.task)  # her supervisor is replaced once more: the lost record stays lost
    flow.ctl.adopt_reply = None
    assert flow.press("JETZT KAUFEN", owner_go=True)["error_type"] == surface.NATIVE_LOST
    assert [c[1] for c in flow.ctl.calls].count(ADOPT) == 1 and flow.presses() == []


def _spans(monkeypatch, ctl_of):
    """Which calls (indexes in the control's log) each adoption, each read for the card and each click sent."""
    spans = {"take_back": [], "_wording": [], "_click": []}
    for module, name in ((ret, "take_back"), (ret, "_wording"), (surface, "_click")):
        def spy(*args, _real=getattr(module, name), _name=name):
            async def run():
                start = len(ctl_of().calls)
                try:
                    return await _real(*args)
                finally:
                    spans[_name].append(range(start, len(ctl_of().calls)))
            return run()
        monkeypatch.setattr(module, name, spy)
    return spans


def _strand(flow):
    """The task's registered supervisor dialled before the record was parked (a lost start race, a late park): it
    found no record and serves the lock as another connection's."""
    with surface._LOCK:
        parked = ret._PARKED.pop(flow.task)
    assert flow.act()["error_type"] == surface.LOCK_FOREIGN
    with surface._LOCK:
        ret._PARKED[flow.task] = parked


@pytest.mark.parametrize("stranded", [False, True])
def test_any_failed_slow_or_unanswered_call_after_a_return_never_hides_a_press(env, monkeypatch, stranded):
    """Whole input: fill, Sera's Pay press (the checkout stays), take, return, the fresh card, the owner's tap, Pay.
    ``stranded``: the adoption is made from that payment action (the card's fill), on the supervisor that replaces
    a registered one which dialled before the record was parked.
    Then each call of the adoption, of the card's read of the page and of the approved click, one at a time, fails,
    answers late or never answers. A fault in the adoption: the payment is lost, no card, nothing pressed. A fault in
    the read: the card says it could not check (the read is not repeated), still names Sera's earlier press, and the
    owner's tap decides. A fault in the click's dispatch: answered as on a lock never returned (uncertain), and the
    press is counted in the lock, so a later card names it."""
    monkeypatch.setattr(ret, "_CALL_TIMEOUT_S", 0.2)
    monkeypatch.setattr(bs, "PAGE_ATTACH_TIMEOUT_S", 0.2)
    monkeypatch.setattr(ret, "_OBSERVE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(surface, "_ACTION_TIMEOUT_S", 1.2)
    monkeypatch.setattr(surface, "_PRESS_MARGIN_S", 0.4)
    monkeypatch.setattr(detect, "_BEFORE_SCAN_S", 0.2)
    spans = _spans(monkeypatch, lambda: env.ctl)

    def run(fault=None):
        for found in spans.values():
            found.clear()
        flow = _returned(env, pressed=True)
        if stranded:
            _strand(flow)
        flow.ctl.fault = fault
        out, card = flow.card_fill()
        return flow, out, card, flow.press("JETZT KAUFEN")

    flow, out, card, first = run()
    assert first["pay_pressed"] is True and len(flow.presses()) == 2 and PRESSED % "1 time" in card
    adoption, read, click = spans["take_back"][-1], spans["_wording"][0], spans["_click"][1]
    assert len(spans["_wording"]) == 1 and [flow.ctl.calls[at][1] for at in adoption] == [
        ADOPT, "Target.attachToTarget", "Page.enable", "Runtime.enable", "Target.setAutoAttach", TICKET]
    assert [flow.ctl.calls[at][1] for at in read] == ["Page.getFrameTree", "Page.createIsolatedWorld", "Runtime.evaluate"]
    dispatch = [at for at in click if flow.ctl.calls[at][1] == "Input.dispatchMouseEvent"][-2:]
    assert [flow.ctl.calls[at][2]["type"] for at in dispatch] == ["mousePressed", "mouseReleased"]
    faults = [(at, kind) for at in (*adoption, *(() if stranded else (*read, *dispatch)))
              for kind in ("fail", "slow", "never")]
    faults += [(at, "fail") for at in click if at not in dispatch and not stranded]
    for at, kind in faults:
        flow, out, card, first = run((at, kind))
        where = f"call {at} ({flow.ctl.calls[at][1]}) {kind}"
        assert flow.ctl.fired, where
        assert len(flow.presses()) <= 2, where  # Sera's press before the take, and the one after the fresh tap
        if at in adoption:
            assert card is None and out["success"] is False and len(flow.presses()) == 1, where
            assert first["error_type"] == surface.NATIVE_LOST and "check the order" in first["error"], where
        elif at in read:
            assert "<b>Hermes could not check the page for order wording.</b>" in card and APPROVE in card, where
            assert PRESSED % "1 time" in card and len(spans["_wording"]) == 1, where
            assert out["success"] is True and first["pay_pressed"] is True, where
        elif at in dispatch:  # booked at its dispatch: counted, whatever its outcome
            if kind != "slow":  # a late answer may still arrive inside the action's time
                assert first["error_type"] == "payment_click_uncertain" and "Do NOT click again" in first["error"], where
            assert first.get("pay_pressed") is True or first["error_type"] == "payment_click_uncertain", where
            assert flow.act()["presses_in_lock"] == 2, where


def test_replacement_during_403_cleanup_adopts_parked_record(env, monkeypatch):
    """Codex M2: the owner takes the tab; Sera's fenced supervisor meets the 403 and leaves the registry, and is held
    right there (by the test) while Return to Sera and her next call start the replacement. The record was parked
    before the supervisor left, so the replacement takes it back."""
    flow = Flow(env)
    first = flow.fill()
    left, go_on = threading.Event(), threading.Event()
    real_pop = bs.SUPERVISOR_REGISTRY._pop

    def pop(task_id):
        out = real_pop(task_id)
        if threading.current_thread() is first._thread:
            left.set()
            go_on.wait(10)
        return out

    monkeypatch.setattr(bs.SUPERVISOR_REGISTRY, "_pop", pop)
    flow.ctl.lease, flow.ctl.native = 0, None
    for ws in flow.ctl.sockets:
        ws.drop()
    try:
        assert left.wait(10) and bs.SUPERVISOR_REGISTRY.get(flow.task) is None and first._thread.is_alive()
        flow.give_back()
        listed = flow.act()
        assert listed["success"] is True and listed["returned"] is True, listed
        assert flow.sup() is not first and flow.ctl.native is flow.ctl.sockets[1]
    finally:
        go_on.set()
    wait_until(lambda: not first._thread.is_alive(), what="the fenced supervisor ended")
    assert flow.task not in ret._PARKED and flow.act()["returned"] is True


def test_a_stop_that_outlives_its_join_still_parks_the_record(env, monkeypatch):
    """Opus L1: the supervisor is replaced and its thread outlives stop()'s join (held in its exit by the test): the
    record is parked when stop() returns, so the next supervisor takes it back."""
    flow = Flow(env)
    first, go_on = flow.fill(), threading.Event()
    real_active = first._set_active
    monkeypatch.setattr(first, "_set_active", lambda value: (
        threading.current_thread() is not first._thread or go_on.wait(10), real_active(value)))
    flow.ctl.lease, flow.ctl.native = 2, None  # the connection is replaced; the old lease's sockets are gone
    try:
        bs.SUPERVISOR_REGISTRY._pop(flow.task)
        first.stop(timeout=0.2)
        assert first._thread.is_alive() and flow.task in ret._PARKED
        assert flow.act()["returned"] is True and flow.sup() is not first
    finally:
        go_on.set()


def test_concurrent_get_or_start_loser_park_is_adopted_on_next_payment_action(env, monkeypatch):
    """Round-5 open question 3: two starts for the task at once after the return. The one that dials first takes the
    record back; the other registers first, finds no record and serves the lock as another connection's; the first is
    then stopped as the loser and parks the record. Sera's next payment action adopts it."""
    flow = Flow(env)
    flow.fill()
    flow.take()
    flow.give_back()
    real_start, started = bs.CDPSupervisor.start, []

    def start(self, *args, **kwargs):
        real_start(self, *args, **kwargs)
        started.append(self)
        if len(started) == 1:  # the adopter; before it registers, a concurrent call starts and registers another
            assert self in surface._HELD and flow.task not in ret._PARKED
            bs.SUPERVISOR_REGISTRY.get_or_start(self.task_id, self.cdp_url)

    monkeypatch.setattr(bs.CDPSupervisor, "start", start)
    raced = flow.act()
    loser, winner = started
    assert raced["error_type"] == surface.LOCK_FOREIGN and bs.SUPERVISOR_REGISTRY.get(flow.task) is winner
    assert flow.task in ret._PARKED and not loser._thread.is_alive()
    listed = flow.act()
    assert listed["success"] is True and listed["returned"] is True, listed
    assert flow.sup() not in (loser, winner) and flow.ctl.native is flow.ctl.sockets[-1]
    assert flow.card_fill()[0]["success"] is True and flow.press("JETZT KAUFEN")["pay_pressed"] is True


def test_park_after_registry_stop_is_adopted_on_next_payment_action(env, monkeypatch):
    """Round-5 open question 3: the connection is replaced (a registry stop) while Sera's next call starts the
    replacement. It dials before stop() has parked the record, finds none and serves the lock as another
    connection's. Once the record is parked, her next payment action adopts it."""
    flow = Flow(env)
    first, parking, go_on = flow.fill(), threading.Event(), threading.Event()
    real_park = first._park_payment
    monkeypatch.setattr(first, "_park_payment", lambda: (parking.set(), go_on.wait(10), real_park()))
    flow.ctl.lease = 2  # the connection is replaced: the old lease's dials are refused from now
    stopper = threading.Thread(target=bs.SUPERVISOR_REGISTRY.stop, args=(flow.task,))
    stopper.start()
    try:
        assert parking.wait(10) and flow.task not in ret._PARKED
        assert flow.act()["error_type"] == surface.LOCK_FOREIGN
    finally:
        go_on.set()
        stopper.join(10)
    assert flow.task in ret._PARKED
    listed = flow.act()
    assert listed["success"] is True and listed["returned"] is True, listed
    assert flow.ctl.native is flow.ctl.sockets[-1] and flow.task not in ret._PARKED


def _card(returned, cart=True, receiving=(prompt.ReceivingOrigin(STRIPE, "reviewed", ("cc-number",)),), **kwargs):
    facts = prompt.PaymentPromptFacts(
        ref="f" * 32, card_label="Card", merchant_origin=shop.SHOP, expires_in_s=300, returned=returned,
        receiving=receiving,
        cart=prompt.StatedCart((prompt.CartItem("16 Pakete", 1),), "22.66", "EUR") if cart else None,
        page_match=prompt.PageMatch("matched" if cart else "not_checked"), **kwargs)
    return prompt.render_html(prompt.PaymentPromptFacts.from_dict(facts.to_dict()), budget_utf16=4096), facts


def test_the_approval_card_after_a_return(monkeypatch):
    monkeypatch.setattr(prompt, "payment_pay_once_enabled", lambda: True)
    for state, line in prompt.RETURNED_LINES.items():
        card, facts = _card(state)
        assert APPROVE in card and f"<b>{line}</b> This check only advises" in card and "pays once" not in card
        assert prompt.scope_line(facts) == APPROVE and APPROVE in prompt.render_plain(facts)
        assert "Sera presses through its steps" in card and "already pressed" not in card
    assert PRESSED % "3 times" in _card("seen", pay_presses=3)[0]
    assert "<b>Hermes does not know whether Sera already pressed Pay in this payment" in _card("seen", pay_presses=None)[0]
    card, facts = _card("")  # no return: the card as it was
    assert "pays once for 22.66 EUR" in card and "already paid" not in card and "came back" not in card
    payload = facts.to_dict()
    payload.pop("returned"), payload.pop("pay_presses")  # a payload from before these fields
    assert prompt.PaymentPromptFacts.from_dict(payload) == facts
    assert prompt.approved_toast(_card("seen", cart=False)[1]) == ("Approved: Sera fills the card. Pay needs a new "
                                                                    "card that covers it.")
    for fill_only in (_card("seen", cart=False)[0], _card("not_seen", pay_once_on=False)[0]):
        assert "Pay needs a new card that covers it" in fill_only and "go in chat" not in fill_only
        assert "already paid this purchase" not in fill_only and "yourself" not in fill_only
    for bad in ({"returned": "paid"}, {"returned": "seen", "pay_presses": -1}, {"returned": "seen", "pay_presses": True},
                {"returned": "seen", "press_only": True}, {"returned": "", "press_only": True, "receiving": ()},
                {"returned": "seen", "press_only": True, "receiving": (), "cart": False}):
        with pytest.raises(prompt.FactsInvalid):
            _card(**bad)
    card, facts = _card("not_seen", pay_presses=1, press_only=True, receiving=())  # D-0961: past the card step
    assert PRESS_ONLY in card and "<b>No order wording seen" in card and PRESSED % "1 time" in card
    assert "One tap covers one purchase" in card and "Card fields go to" not in card and "Card: " not in card
    assert prompt.render_plain(facts).startswith("No card field is filled.") and prompt.summary(facts).startswith("Press Pay on")
    assert prompt.approve_label(facts) == prompt.APPROVE_LABEL_PRESS and prompt.pays_once(facts)
