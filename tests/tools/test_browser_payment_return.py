"""D-0878 a (owner 2026-11-24): Sera takes a card payment back after the owner took the tab and pressed Return to
Sera. The real CDPSupervisor (its ``_run`` loop, the 403 exit, the registry, the real attach) on a fake
browser-control socket that also serves the payment tab's page, entered through the vault tool
(``_payment_surface_action``). The fake control keeps the contract the monolith side builds: a ticket from the lock's
native connection, an adoption with that ticket on a later connection, every other connection refused while locked.
No real Chrome; synthetic data."""

from __future__ import annotations

import asyncio
import itertools
import json
import secrets
from types import SimpleNamespace

import pytest
import websockets

from tests.tools import test_browser_payment_detect as shop
from tests.tools.test_browser_payment_done import STRIPE, frame_tree
from tests.tools.test_browser_payment_native_recovery import FakeWS
from tests.tools.test_browser_payment_surface_honesty import _row
from tests.waiting import wait_until
from tools import browser_payment_detect as detect
from tools import browser_payment_order_evidence as evidence
from tools import browser_payment_surface as surface
from tools import browser_supervisor as bs
from tools import browser_vault_tool as vault_tool

TICKET, ADOPT = "Hermes.paymentSurfaceTicket", "Hermes.adoptPaymentSurface"  # control-v1 ``cdp_methods``
LEASE = "ws://127.0.0.1:9/devtools/browser/lease-%d"
INSPECTED = SimpleNamespace(page_id="TAB", merchant_origin=shop.SHOP, tree=shop.CHECKOUT, unreviewed_origins=(),
                            documents=(SimpleNamespace(frame_id="CARD", origin=STRIPE),))
PAID_ROWS = [_row("Weiter einkaufen", role="link"), _row("Jetzt kaufen", role="link", i=1)]
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
        self.popups = {}  # target id -> (opener id, url, text): tabs the payment tab's page opened

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
            return {"sessionId": "S-page" if tab == "TAB" else f"S-{tab}" if tab in self.popups else "S-other"}
        popup = self.popups.get((sid or "")[2:])
        if popup and method == "Page.getFrameTree":
            return frame_tree(((sid + "-TOP", sid + "-L", popup[1], shop.SHOP, ""),))
        if popup and method == "Runtime.evaluate" and params.get("expression") == evidence.SCAN_JS:
            return {"result": {"type": "object", "value": evidence.wording("", popup[2])}}
        if popup:
            return {"executionContextId": 9} if method == "Page.createIsolatedWorld" else {}
        if method == "Target.closeTarget" and params.get("targetId") == "TAB":
            self.lock, self.native, self.ticket = set(), None, None
        out = self.page.answer(method, params, sid)
        if method == "Target.getTargets":
            out["targetInfos"] += [{"targetId": tab, "type": "page", "openerId": opener, "url": url}
                                   for tab, (opener, url, _text) in self.popups.items()]
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
        self.ctl = env.ctl = Control()
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


@pytest.fixture
def env(monkeypatch):
    env = SimpleNamespace(ctl=None, tasks=[])
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
    yield env
    for task in env.tasks:
        bs.SUPERVISOR_REGISTRY.stop(task)


def _returned(env, paid=False, pressed=False):
    """Fill, (Sera's own Pay press, after which the checkout still shows its Pay control,) take, (the owner pays
    himself,) Return to Sera."""
    flow = Flow(env)
    flow.fill()
    if pressed:
        flow.ctl.page.node, flow.ctl.page.shown = 7, True
        assert flow.press("JETZT KAUFEN")["pay_pressed"] is True
    flow.take()
    if paid:
        flow.ctl.page.page.update(shop.THANKS)
        flow.ctl.page.rows = list(PAID_ROWS)
    flow.give_back()
    return flow


def test_after_a_return_with_no_order_pay_needs_a_fresh_approval_and_is_pressed_once_after_it(env):
    flow = _returned(env)
    listed = flow.act()  # Sera's next payment call: her new connection adopted the lock and its record
    assert listed["success"] is True and listed["returned"] is True and listed["payment_detected"] is False
    assert "fresh approval" in listed["next"] and listed["presses_in_lock"] is None
    sup, sockets = flow.sup(), flow.ctl.sockets
    assert flow.ctl.native is sockets[1] and len(sockets) == 2 and sup.snapshot().active is True
    attached = [c for c in flow.ctl.calls if c[0] is sockets[1] and c[1] == "Target.attachToTarget"]
    assert [c[2]["targetId"] for c in attached] == ["TAB"], "the payment tab, and no dialog bridge on a locked slot"
    assert not [c for c in flow.ctl.calls if c[0] is sockets[1] and c[1] == "Fetch.enable"]
    # the approval from before the take is refused for the press after the return
    stale = flow.press("JETZT KAUFEN")
    assert stale["error_type"] == surface.GO_REQUIRED and "no approval from before counts" in stale["error"]
    told = flow.press("JETZT KAUFEN", owner_go=True)  # D-0922 a: his word in chat does not count after a return
    assert told["error_type"] == surface.GO_REQUIRED and "press Pay himself" in told["error"]
    assert flow.presses() == []
    # a fresh approval: the owner approves a new card fill (a new approval card), its total bound
    assert surface.claim_payment_surface(sup, INSPECTED) is None
    assert surface.record_approved_total(sup, 2266, "EUR", ("TOP", "L0"), shop.SHOP) is True
    assert flow.act()["payment_detected"] is False, "the re-fill's record is still a returned one: checked again"
    out = flow.press("JETZT KAUFEN")
    assert out["success"] is True and out["pay_pressed"] is True and len(flow.presses()) == 1
    assert "returned" not in flow.act(), "from the press on, the detection around presses reads the order"


def test_after_a_return_owner_go_never_presses_after_a_fill_only_re_fill_or_a_second_return(env):
    """D-0922 a: only a new approval card counts after a return. A re-fill whose card bound no total (the page takes
    no card any more, or the card was fill-only) leaves Pay to the owner, and so does a second take and return."""
    flow = _returned(env, pressed=True)
    assert flow.act()["payment_detected"] is False
    assert surface.claim_payment_surface(flow.sup(), INSPECTED) is None  # a re-fill, no total bound
    for _ in range(2):
        out = flow.press("JETZT KAUFEN", owner_go=True)
        assert out["error_type"] == surface.GO_REQUIRED and "press Pay himself" in out["error"]
        flow.take()
        flow.give_back()
        assert flow.act()["returned"] is True
    assert len(flow.presses()) == 1, "only Sera's press from before the first take"


def test_after_a_return_the_order_the_owner_placed_is_detected_and_pay_is_never_pressed(env):
    flow = _returned(env, paid=True)
    listed = flow.act()
    assert listed["returned"] is True and listed["payment_detected"] is True and "payment_done" in listed["next"]
    out = flow.press("Jetzt kaufen", owner_go=True)  # the owner's word does not count after a return (D-0922 a)
    assert out["error_type"] == surface.GO_REQUIRED and "payment_done" in out["error"]
    sup = flow.sup()
    assert surface.claim_payment_surface(sup, INSPECTED) is None  # nor after a new fill in this lock
    assert surface.record_approved_total(sup, 2266, "EUR", ("TOP", "L0"), shop.SHOP) is True
    assert flow.press("Jetzt kaufen")["error_type"] == detect.SPENT
    assert flow.presses() == []
    assert detect.detected(sup)["evidence"] == "confirmation_text"


def _fresh_approval(flow):
    """The owner approves a new card fill (a new approval card) with its total bound (D-0922 a)."""
    sup = flow.sup()
    assert surface.claim_payment_surface(sup, INSPECTED) is None
    assert surface.record_approved_total(sup, 2266, "EUR", ("TOP", "L0"), shop.SHOP) is True
    return sup


def test_after_a_return_a_confirmation_the_owners_payment_opened_in_a_new_tab_is_detected(env):
    """Codex H2: while the owner had the tab, the checkout opened a payment step tab, and that tab opened the
    confirmation (an opener chain; Sera never pressed, so no tab of hers is recorded). The check reads every shop tab
    the payment tab's chain opened: the order is detected and even a fresh approval presses nothing."""
    flow = _returned(env)
    flow.ctl.popups.update({"STEP": ("TAB", shop.SHOP + "/zahlung", "Hilfe und Kontakt"),
                            "BANK": ("STEP", "https://bank.test/3ds", "Vielen Dank für Ihre Bestellung!"),
                            "DANKE": ("STEP", shop.SHOP + "/danke", "Vielen Dank für Ihre Bestellung! Nr. 10023")})
    listed = flow.act()
    assert listed["payment_detected"] is True and "payment_done" in listed["next"]
    sup = _fresh_approval(flow)
    assert flow.press("JETZT KAUFEN")["error_type"] == detect.SPENT and flow.presses() == []
    assert detect.detected(sup)["where"] == "new_tab" and detect.detected(sup)["tab"] == "DANKE"
    read = {c[2]["targetId"] for c in flow.ctl.calls if c[1] == "Target.attachToTarget"}
    assert "BANK" not in read, "another site's tab is never read"


@pytest.mark.parametrize("where", ["top", "new_tab", "new_document_after_her_press"])
def test_after_a_return_order_wording_hermes_cannot_be_sure_of_presses_nothing(env, where):
    """GLM M1: the owner paid; the confirmation says "Your order has been received. Payment successful." (order
    wording a checkout's own service text can carry too, no thank-you sentence or order number) on the payment tab,
    in a tab his payment opened, or on a new document after a Pay press of Sera's (the owner had the tab: not that
    press's result). Hermes cannot tell, so payment_detected is null and even a fresh approval presses nothing."""
    flow = _returned(env, pressed=where == "new_document_after_her_press")
    text = "Your order has been received. Payment successful."
    if where == "new_tab":
        flow.ctl.popups["DONE"] = ("TAB", shop.SHOP + "/order-received/123/", text)
    else:
        flow.ctl.page.page.update({"title": "Order", "text": text})
        flow.ctl.page.rows = list(PAID_ROWS)
    if where == "new_document_after_her_press":
        flow.ctl.page.tree = (("TOP", "L9", shop.SHOP + "/order-received/123/", shop.SHOP, ""),)
    listed = flow.act()
    assert listed["payment_detected"] is None and "could not check" in listed["next"]
    if where == "new_document_after_her_press":
        return
    _fresh_approval(flow)
    out = flow.press("Jetzt kaufen" if where == "top" else "JETZT KAUFEN")
    assert out["error_type"] == "payment_return_unchecked" and flow.presses() == []


def test_after_a_return_a_checkouts_own_service_text_is_no_order(env):
    """GLM M1's other side: the checkout itself (its purchase button still listed) says what happens once the order
    is placed: no order shows, and a fresh approval presses Pay once."""
    flow = _returned(env)
    flow.ctl.page.page["text"] = shop.CART["text"] + " Once your order has been placed you will receive an email."
    assert flow.act()["payment_detected"] is False
    _fresh_approval(flow)
    assert flow.press("JETZT KAUFEN")["pay_pressed"] is True and len(flow.presses()) == 1


def test_after_a_return_a_pressed_pay_control_that_no_longer_resolves_is_gone(env):
    """GLM M2: Sera pressed Pay on a single-page checkout, the owner finished the payment and the shop removed the
    control: Chrome answers DOM.resolveNode with "No node with given id found", which means gone (not a failed read),
    so the thank-you sentence on the same document is the order."""
    flow = _returned(env, paid=True, pressed=True)
    flow.ctl.page.shown = None
    assert flow.act()["payment_detected"] is True
    _fresh_approval(flow)
    assert flow.press("Jetzt kaufen")["error_type"] == detect.SPENT and len(flow.presses()) == 1


def test_after_a_return_a_shops_confirm_dialog_is_never_accepted_until_a_fresh_approvals_press(env):
    """Codex H1: a shop's own "Place order?" confirm waits on the payment tab after the return. Accepting it could
    place the order without a fresh approval: refused (Sera asks the owner to answer it); dismissing still works."""
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
    _fresh_approval(flow)  # a fresh card alone does not do it either: only her Pay press after it
    assert answer(dialog_id, True)["error_type"] == "payment_dialog_returned" and answered() == []
    assert answer(dialog_id, False)["answered"] == "dismiss" and answered() == [{"accept": False}]


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


def test_a_second_take_and_return_is_taken_back_again(env):
    flow = _returned(env)
    assert flow.act()["returned"] is True
    flow.take()
    flow.ctl.lease = 3
    assert flow.act()["payment_detected"] is False and flow.ctl.native is flow.ctl.sockets[2]


def _spans(monkeypatch, ctl_of):
    """Which calls (indexes in the control's log) each adoption, each order check and each press's look before the
    press sent."""
    from tools import browser_payment_return as ret

    spans = {"take_back": [], "check": [], "before_press": []}
    for module, name in ((ret, "take_back"), (ret, "check"), (detect, "before_press")):
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


RELEASES = {"Runtime.releaseObject", "Target.detachFromTarget"}  # they free a handle after a read that answered


def test_any_failed_slow_or_unanswered_adoption_or_check_call_ends_in_no_press_and_a_message(env, monkeypatch):
    """Whole input: fill, Sera's Pay press (the checkout stays), take (the checkout opens a help tab on the shop and a
    bank tab while the owner has it), return, list, press Pay with the owner's fresh approval (a new card fill). With
    every call answered that second press goes out. Then each call of the adoption and of the press's look before the
    press (the one order check: the top document, the pressed control's state, the opened tab), one at a time,
    fails, answers late or never answers: no press after the return, a refusal that tells Sera what to tell the
    owner, and no fault was a no-op. A failed release of a handle after a read that answered changes nothing. The
    listing's own check reports the same as null."""
    from tools import browser_payment_return as ret

    monkeypatch.setattr(ret, "_CALL_TIMEOUT_S", 0.2)
    monkeypatch.setattr(bs, "PAGE_ATTACH_TIMEOUT_S", 0.2)
    monkeypatch.setattr(detect, "_BEFORE_SCAN_S", 0.2)
    spans = _spans(monkeypatch, lambda: env.ctl)

    def run(fault=None):
        for found in spans.values():
            found.clear()
        flow = _returned(env, pressed=True)
        flow.ctl.popups.update({"HELP": ("TAB", shop.SHOP + "/hilfe", "Hilfe und Kontakt"),
                                "BANK": ("TAB", "https://bank.test/3ds", "Bitte bestätigen Sie die Zahlung")})
        flow.ctl.fault = fault
        listed = flow.act()
        if listed.get("payment_detected") is not False:
            return flow, listed, listed
        sup = flow.sup()  # the owner's fresh approval: a new card fill (D-0922 a)
        assert surface.claim_payment_surface(sup, INSPECTED) is None
        assert surface.record_approved_total(sup, 2266, "EUR", ("TOP", "L0"), shop.SHOP) is True
        out = flow.press("JETZT KAUFEN")
        return flow, listed, out

    flow, listed, out = run()
    assert out["success"] is True and out["pay_pressed"] is True and len(flow.presses()) == 2
    adoption, listing_check, press_look = spans["take_back"][-1], spans["check"][0], spans["before_press"][-1]
    methods = [flow.ctl.calls[at][1] for at in press_look]
    assert len(spans["check"]) == 3 and [flow.ctl.calls[at][1] for at in adoption] == [
        ADOPT, "Target.attachToTarget", "Page.enable", "Runtime.enable", "Target.setAutoAttach", TICKET]
    for at, kind in [(at, kind) for at in (*adoption, *press_look) for kind in ("fail", "slow", "never")]:
        flow, listed, out = run((at, kind))
        where = f"call {at} ({flow.ctl.calls[at][1]}) {kind}"
        assert flow.ctl.fired, where
        if kind == "fail" and flow.ctl.calls[at][1] in RELEASES:
            assert out["pay_pressed"] is True and len(flow.presses()) == 2, where
            continue
        assert len(flow.presses()) == 1, where  # Sera's press before the take, none after
        if at in adoption:
            assert out["error_type"] == surface.NATIVE_LOST and "check the order" in out["error"], where
        else:
            assert out["error_type"] == ret.RETURN_UNCHECKED and "nothing was pressed" in out["error"], where
    assert {"Page.getFrameTree", "Runtime.evaluate", "DOM.resolveNode", "Runtime.callFunctionOn", "Target.getTargets",
            "Target.attachToTarget", *RELEASES} <= set(methods), methods
    flow, listed, out = run((listing_check[0], "fail"))  # Sera is told not to press (a press runs its own check)
    assert flow.ctl.fired and listed["success"] is True and listed["payment_detected"] is None
    assert "could not check" in listed["next"] and "Do not press" in listed["next"] and len(flow.presses()) == 1
