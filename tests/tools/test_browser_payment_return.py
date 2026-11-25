"""D-0878 a (owner 2026-11-24): Sera takes a card payment back after the owner took the tab and pressed Return to
Sera. The real CDPSupervisor (its ``_run`` loop, the 403 exit, the registry, the real attach) on a fake
browser-control socket that also serves the payment tab's page, entered through the vault tool
(``_payment_surface_action``). The fake control keeps the contract the monolith side builds: a ticket from the lock's
native connection, an adoption with that ticket on a later connection, every other connection refused while locked.
D-0942 a (owner 2026-11-25): after the return only the owner's tap on a fresh approval card lets Sera press Pay,
once; the card says what one read of the page showed, and that read never decides a press. The fresh card goes
through the real ``fill_payment`` (facts, card text, claim, binding); the inspection and the commit are faked.
No real Chrome; synthetic data."""

from __future__ import annotations

import asyncio
import itertools
import json
import secrets
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
from tools import browser_payment_return as ret
from tools import browser_payment_surface as surface
from tools import browser_supervisor as bs
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
APPROVE = "Approve confirms you have not already paid this purchase. Sera fills the card and presses Pay once for EUR 22.66."
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
        """payment_controls, then payment_click on the shop page's control ``caption`` (a refusal of either)."""
        listed = self.act()
        if not listed.get("success"):
            return listed
        with surface._LOCK:
            issued = dict(surface._ISSUED.get(self.sup()) or {})
        refs = [r for r, spec in issued.items() if spec[0] == "TOP|L0" and spec[3] == caption]
        assert len(refs) == 1, (caption, sorted(issued.values()), listed)
        return self.act(refs[0], **kwargs)

    def presses(self):
        return [c for c in self.ctl.calls if c[1] == "Input.dispatchMouseEvent" and c[2].get("type") == "mousePressed"]

    def card_fill(self, answer="accept", commit=True, cart=CART, during=None):
        """Sera fills the card (again) through the real ``fill_payment``: ``(its result, the approval card as the
        owner saw it or None)``. ``answer``: the owner's (accept, decline, cancel: no answer in time); ``during``: runs
        while the card waits; ``commit``: whether the fill itself then succeeds."""
        sup, cards = self.sup(), []
        inspected = SimpleNamespace(
            **vars(INSPECTED), consumed=False, owner=sup, frame_origin=STRIPE, operation_id="f" * 32,
            expires_at=time.monotonic() + 120, target_origins=(STRIPE,), page_match=("matched", "the merchant page"),
            merchant_match="matched", prompt=lambda: {"merchant_origin": shop.SHOP, "target_origin": STRIPE,
                                                      "fields": ["cc-number"], "checks": []})

        def inspect(*a, **k):
            if not sup.snapshot().active:  # as the real inspection: it reads the attached payment tab
                raise bvp.PaymentPolicyError("Supervised browser required")
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
    monkeypatch.setattr(vault_tool, "_is_managed", lambda: True)
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


def test_after_a_return_only_the_fresh_cards_approval_presses_pay_and_exactly_once(env):
    flow = _returned(env)
    first = bs.SUPERVISOR_REGISTRY.get(flow.task)
    listed = flow.act()  # Sera's next payment call: her new connection adopted the lock and its record
    assert listed["success"] is True and listed["returned"] is True and "payment_detected" not in listed
    assert "fill the card again" in listed["next"] and listed["presses_in_lock"] is None
    sup, sockets = flow.sup(), flow.ctl.sockets
    assert flow.ctl.native is sockets[1] and len(sockets) == 2 and sup.snapshot().active is True
    attached = [c for c in flow.ctl.calls if c[0] is sockets[1] and c[1] == "Target.attachToTarget"]
    assert [c[2]["targetId"] for c in attached] == ["TAB"], "the payment tab, and no dialog bridge on a locked slot"
    assert not [c for c in flow.ctl.calls if c[0] is sockets[1] and c[1] == "Fetch.enable"]
    # the approval from before the take does not count, and his word in chat does not either (D-0922 a)
    stale = flow.press("JETZT KAUFEN")
    assert stale["error_type"] == surface.GO_REQUIRED and "No approval from before counts" in stale["error"]
    told = flow.press("JETZT KAUFEN", owner_go=True)
    assert told["error_type"] == surface.GO_REQUIRED and "press Pay himself" in told["error"]
    assert flow.presses() == []
    # the fresh card (D-0942 a): what the page showed, and what his tap confirms; then one Pay press
    scans = len([c for c in flow.ctl.calls if c[1] == "Runtime.evaluate"])
    out, card = flow.card_fill()
    assert out["success"] is True and "order_wording_seen" not in out
    assert APPROVE in card and "<b>No order wording seen on the checked page.</b> This check only advises" in card
    assert "One tap covers one fill and one Pay press" in card and "go in chat" not in card
    assert len([c for c in flow.ctl.calls if c[1] == "Runtime.evaluate"]) == scans + 1, "one read of the page"
    assert "next" not in flow.act(), "a fresh approval is bound"
    pressed = flow.press("JETZT KAUFEN")
    assert pressed["success"] is True and pressed["pay_pressed"] is True and len(flow.presses()) == 1
    again = flow.press("JETZT KAUFEN")  # the checkout still shows its button: the approval's one press is used
    assert again["error_type"] == ret.PRESS_USED and "new approval card" in again["error"]
    assert flow.press("BESTÄTIGEN")["error_type"] == ret.PRESS_USED and len(flow.presses()) == 1
    assert flow.act()["returned"] is True, "the lock stays a returned one"
    assert surface.record_approved_total(first, 2266, "EUR", ("TOP", "L0"), shop.SHOP) is False, "the ended connection"
    out, card = flow.card_fill()  # another press: another card, another tap
    assert APPROVE in card and flow.press("JETZT KAUFEN")["pay_pressed"] is True and len(flow.presses()) == 2


def test_after_a_return_two_clicks_admitted_under_one_approval_book_one_pay_press(env):
    """Two payment_clicks admitted with the same approval (as two overlapping calls would be): the first Pay-like
    press books at its dispatch, the second is refused there; a bank's step after it is still booked."""
    flow = _returned(env)
    assert flow.card_fill()[0]["success"] is True
    sup = flow.sup()
    with surface._LOCK:
        gen = surface._GENS[sup]
        gate = surface._Gate(gen.approved, gen.admitted, False, surface._EPOCHS.get(sup, 0), gen)
    assert surface._book_press(sup, gate, True, True, []) is None
    assert surface._book_press(sup, gate, True, True, [])["error_type"] == ret.PRESS_USED
    assert surface._book_press(sup, gate, True, False, []) is None, "a press that is not Pay-like"
    assert surface._book_press(sup, gate, True, True, [], bank=True) is None and gen.presses == 3


@pytest.mark.parametrize("shows, tabs, line, seen", [
    # the owner paid: thank-you and order number inline, next to the still enabled Pay button
    ({"page": {"text": shop.CART["text"] + " Vielen Dank für Ihre Bestellung! Bestellnummer 10023"}}, {},
     "Order wording seen on the checked page.", True),
    # the owner paid: the confirmation is in a tab he opened himself, or a noopener tab; the checkout looks untouched
    ({}, {"DANKE": (None, shop.SHOP + "/danke")}, "No order wording seen on the checked page.", False),
    # nobody paid: the checkout's own service text reads as order wording
    ({"page": {"text": shop.CART["text"] + " Once your order has been placed you will receive an email."}}, {},
     "Order wording seen on the checked page.", True),
])
def test_what_the_page_shows_after_a_return_only_changes_the_cards_line(env, shows, tabs, line, seen):
    """D-0942 a: inline confirmation next to an enabled Pay, a manual or noopener confirmation tab and ordinary
    checkout service wording neither permit nor refuse a press: without the owner's tap nothing is pressed, with it
    Pay is pressed once."""
    flow = _returned(env, shows=shows)
    flow.ctl.tabs.update(tabs)
    assert flow.press("JETZT KAUFEN")["error_type"] == surface.GO_REQUIRED
    out, card = flow.card_fill("decline")
    assert out["error_type"] == "payment_declined" and f"<b>{line}</b>" in card and APPROVE in card
    assert ("order_wording_seen" in out) is seen and (not seen or "Say it in chat" in out["order_wording_seen"])
    assert flow.press("JETZT KAUFEN")["error_type"] == surface.GO_REQUIRED and flow.presses() == []
    out, card = flow.card_fill()
    assert out["success"] is True and f"<b>{line}</b>" in card and ("order_wording_seen" in out) is seen
    assert flow.press("JETZT KAUFEN")["pay_pressed"] is True and len(flow.presses()) == 1
    assert flow.press("JETZT KAUFEN")["error_type"] == ret.PRESS_USED and len(flow.presses()) == 1
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
        assert "you press Pay yourself in the tab" in card and APPROVE not in card and "go in chat" not in card
    for owner_go in (False, True):
        refused = flow.press("JETZT KAUFEN", owner_go=owner_go)
        assert refused["error_type"] == surface.GO_REQUIRED and "press Pay himself" in refused["error"]
    assert len(flow.presses()) == (0 if how == "card_from_before" else 1), "only Sera's press before the take"


def test_a_second_return_voids_the_fresh_approval_and_owner_go_never_counts_again_in_this_lock(env):
    flow = _returned(env)
    assert flow.card_fill()[0]["success"] is True
    flow.take()
    flow.ctl.lease = 3  # Return to Sera again
    assert flow.act()["returned"] is True and flow.ctl.native is flow.ctl.sockets[2]
    assert flow.press("JETZT KAUFEN")["error_type"] == surface.GO_REQUIRED and flow.presses() == []
    assert flow.card_fill()[0]["success"] is True and flow.press("JETZT KAUFEN")["pay_pressed"] is True
    assert surface.claim_payment_surface(flow.sup(), INSPECTED) is None  # a later fill that bound no total
    assert flow.press("JETZT KAUFEN", owner_go=True)["error_type"] == surface.GO_REQUIRED and len(flow.presses()) == 1


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


def test_after_a_return_a_shops_confirm_dialog_is_accepted_only_after_the_fresh_approvals_pay_press(env):
    """A shop's own "Place order?" confirm waits on the payment tab after the return. Accepting it could place the
    order without the owner's tap: refused (Sera asks him to answer it), also with a fresh card approved and no Pay
    press yet; dismissing works. The confirm her own approved Pay press raises is accepted as before."""
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
    assert out["error_type"] == "payment_dialog_returned" and "answer it himself" in out["error"]
    assert flow.card_fill()[0]["success"] is True  # a fresh card alone does not do it: only her Pay press after it
    assert answer(dialog_id, True)["error_type"] == "payment_dialog_returned" and answered() == []
    assert answer(dialog_id, False)["answered"] == "dismiss" and answered() == [{"accept": False}]
    assert flow.press("JETZT KAUFEN")["pay_pressed"] is True
    assert answer(confirm(), True)["answered"] == "accept" and answered()[-1] == {"accept": True}


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


def test_any_failed_slow_or_unanswered_call_after_a_return_ends_in_at_most_one_pay_press(env, monkeypatch):
    """Whole input: fill, Sera's Pay press (the checkout stays), take, return, the fresh card, the owner's tap, Pay,
    and Pay once more. With every call answered: one press after the return, the second refused. Then each call of
    the adoption, of the card's read of the page and of the approved click, one at a time, fails, answers late or
    never answers. A fault in the adoption: the payment is lost, no card, nothing pressed. A fault in the read: the
    card says it could not check (the read is not repeated) and the owner's tap still decides. A fault in the click,
    its dispatch included: never a second press after the return, whatever the first one's outcome."""
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
        flow.ctl.fault = fault
        out, card = flow.card_fill()
        return flow, out, card, [flow.press("JETZT KAUFEN") for _ in range(2)]

    flow, out, card, (first, second) = run()
    assert first["pay_pressed"] is True and second["error_type"] == ret.PRESS_USED and len(flow.presses()) == 2
    adoption, read, click = spans["take_back"][-1], spans["_wording"][0], spans["_click"][1]
    assert len(spans["_wording"]) == 1 and [flow.ctl.calls[at][1] for at in adoption] == [
        ADOPT, "Target.attachToTarget", "Page.enable", "Runtime.enable", "Target.setAutoAttach", TICKET]
    assert [flow.ctl.calls[at][1] for at in read] == ["Page.getFrameTree", "Page.createIsolatedWorld", "Runtime.evaluate"]
    dispatch = [at for at in click if flow.ctl.calls[at][1] == "Input.dispatchMouseEvent"][-2:]
    assert [flow.ctl.calls[at][2]["type"] for at in dispatch] == ["mousePressed", "mouseReleased"]
    faults = [(at, kind) for at in (*adoption, *read, *dispatch) for kind in ("fail", "slow", "never")]
    faults += [(at, "fail") for at in click if at not in dispatch]
    for at, kind in faults:
        flow, out, card, (first, second) = run((at, kind))
        where = f"call {at} ({flow.ctl.calls[at][1]}) {kind}"
        assert flow.ctl.fired, where
        assert len(flow.presses()) <= 2, where  # Sera's press before the take, and at most one after the return
        if at in adoption:
            assert card is None and out["success"] is False and len(flow.presses()) == 1, where
            assert first["error_type"] == surface.NATIVE_LOST and "check the order" in first["error"], where
        elif at in read:
            assert "<b>Hermes could not check the page for order wording.</b>" in card and APPROVE in card, where
            assert len(spans["_wording"]) == 1 and out["success"] is True and first["pay_pressed"] is True, where
            assert second["error_type"] == ret.PRESS_USED, where
        elif at in dispatch:  # the press was booked at its dispatch: its outcome is unknown, the approval is used
            assert first["success"] is (kind == "slow") or first["error_type"] == "payment_click_uncertain", where
            assert second["error_type"] == ret.PRESS_USED, where
        elif first.get("pay_pressed"):
            assert second["error_type"] == ret.PRESS_USED, where


def _card(returned, cart=True, **kwargs):
    facts = prompt.PaymentPromptFacts(
        ref="f" * 32, card_label="Card", merchant_origin=shop.SHOP, expires_in_s=300, returned=returned,
        receiving=(prompt.ReceivingOrigin(STRIPE, "reviewed", ("cc-number",)),),
        cart=prompt.StatedCart((prompt.CartItem("16 Pakete", 1),), "22.66", "EUR") if cart else None,
        page_match=prompt.PageMatch("matched" if cart else "not_checked"), **kwargs)
    return prompt.render_html(prompt.PaymentPromptFacts.from_dict(facts.to_dict()), budget_utf16=4096), facts


def test_the_approval_card_after_a_return(monkeypatch):
    monkeypatch.setattr(prompt, "payment_pay_once_enabled", lambda: True)
    for state, line in prompt.RETURNED_LINES.items():
        card, facts = _card(state)
        assert APPROVE in card and f"<b>{line}</b> This check only advises" in card and "pays once" not in card
        assert prompt.scope_line(facts) == APPROVE and APPROVE in prompt.render_plain(facts)
    card, facts = _card("")  # no return: the card as it was
    assert "pays once for 22.66 EUR" in card and "already paid" not in card and "came back" not in card
    payload = facts.to_dict()
    payload.pop("returned")  # a payload from before this field
    assert prompt.PaymentPromptFacts.from_dict(payload) == facts
    for fill_only in (_card("seen", cart=False)[0], _card("not_seen", pay_once_on=False)[0]):
        assert "you press Pay yourself in the tab" in fill_only and "go in chat" not in fill_only
        assert "already paid this purchase" not in fill_only and "Pay once" not in fill_only
    with pytest.raises(prompt.FactsInvalid):
        _card("paid")
