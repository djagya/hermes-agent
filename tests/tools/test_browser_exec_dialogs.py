"""Managed browser_exec dialog policy: answer ordinary dialogs, hold consequential ones for the agent."""

import asyncio
from types import SimpleNamespace

import pytest

from tools import browser_exec_dialogs as dialogs
from tools.browser_supervisor import CDPSupervisor


def _d(kind, message="", url="https://example.com/"):
    return SimpleNamespace(type=kind, message=message, url=url, default_prompt="")


def _decide(dialog, *, on_dialog="auto", marked=False, identity="", after_handoff=False):
    return dialogs.make_decider(on_dialog=on_dialog, slot_marked=marked, identity=identity,
                                after_handoff=after_handoff)(dialog)


@pytest.mark.parametrize("kind,expected", [("alert", "accept"), ("confirm", "dismiss"), ("prompt", "dismiss"),
                                           ("beforeunload", "accept")])
def test_auto_policy_on_an_ordinary_page(kind, expected):
    assert _decide(_d(kind, "Show more?")) == expected


@pytest.mark.parametrize("dialog,identity", [
    (_d("confirm", "Buy now for EUR 249?", url="https://shop.example/p/123"), ""),
    (_d("confirm", "Weiter zur Kasse?"), ""),
    (_d("confirm", "Confirmer la commande ?"), ""),
    (_d("confirm", "Continue?"), "shopping"),
    (_d("prompt", "Name?"), "work:default"),
    (_d("prompt", "Name?"), "sensitive"),
])
def test_consequential_confirms_are_held_even_when_the_call_says_accept(dialog, identity):
    assert _decide(dialog, on_dialog="accept", identity=identity) == "hold"


def test_a_widget_iframe_on_a_checkout_tab_is_consequential():
    widget = _d("confirm", "Add insurance?", url="https://widget.example/frame")
    widget.top_url = "https://shop.example/checkout/review"
    assert _decide(widget) == "hold"


def test_on_dialog_accept_never_auto_accepts_a_confirm():
    assert _decide(_d("confirm", "Show more?"), on_dialog="accept") == "hold"
    assert _decide(_d("beforeunload"), on_dialog="accept") == "accept"


def test_beforeunload_leaves_even_on_a_marked_slot_but_not_right_after_a_handoff():
    assert _decide(_d("beforeunload"), marked=True) == "accept"
    assert _decide(_d("beforeunload"), after_handoff=True) == "hold"
    assert _decide(_d("beforeunload"), on_dialog="dismiss") == "dismiss"


def _admit(supervisor, kind, message=""):
    answered = []

    async def respond(dialog, *, accept, prompt_text):
        answered.append((dialog.id, accept))

    supervisor._respond = respond

    async def run():
        supervisor._admit_dialog(type=kind, message=message, default_prompt="", session_id="s", frame_id=None,
                                 url="https://example.com/")
        await asyncio.sleep(0)
        for handle in supervisor._dialog_watchdogs.values():
            handle.cancel()

    asyncio.run(run())
    return answered


def test_supervisor_holds_a_consequential_confirm_and_answers_an_ordinary_one():
    sup = CDPSupervisor("t", "ws://unused")
    sup.dialog_decider = dialogs.make_decider(on_dialog="auto", slot_marked=False, identity="")
    assert _admit(sup, "confirm", "Confirm payment of 20 EUR?") == []
    assert [d.message for d in sup.snapshot().pending_dialogs] == ["Confirm payment of 20 EUR?"]
    assert _admit(sup, "confirm", "Show more results?") == [("d-2", False)]
    assert sup.snapshot().recent_dialogs[-1].action == "dismiss"


def test_a_failing_decider_holds_instead_of_accepting():
    sup = CDPSupervisor("t", "ws://unused", dialog_policy="auto_accept")

    def broken(dialog):
        raise RuntimeError("boom")

    sup.dialog_decider = broken
    assert _admit(sup, "confirm", "ok?") == []
    assert len(sup.snapshot().pending_dialogs) == 1


def _tab_supervisor():
    sup = CDPSupervisor("t", "ws://unused")
    attaches = []

    async def fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
        if method == "Target.attachToTarget":
            attaches.append(params["targetId"])
            await asyncio.sleep(0.01)  # the attach reply arrives after other work ran
            return {"result": {"sessionId": "tab-sid"}}
        return {"result": {}}

    sup._cdp = fake_cdp
    return sup, attaches


def test_a_new_tab_is_watched_once_and_its_frames_stay_out_of_the_main_tree():
    sup, attaches = _tab_supervisor()
    sup._watch_new_tabs = True

    async def run():
        info = {"targetInfo": {"targetId": "TAB2", "type": "page", "url": "https://shop.example/checkout"}}
        await sup._on_target_created(info, None)
        await sup._on_target_created(info, None)  # discovery + arm race on the same tab
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert attaches == ["TAB2"]
    assert sup._dialog_tab_event_ignored("Page.frameNavigated", {}, "tab-sid") is True
    assert sup._dialog_tab_event_ignored("Page.javascriptDialogOpening", {}, "tab-sid") is False
    sup.dialog_decider = dialogs.make_decider(on_dialog="accept", slot_marked=False, identity="")
    assert _admit_on(sup, "confirm", "Continue?", session="tab-sid") == []
    pending = sup.snapshot().pending_dialogs[0]
    assert pending.top_url == "https://shop.example/checkout"


def _admit_on(supervisor, kind, message, *, session):
    answered = []

    async def respond(dialog, *, accept, prompt_text):
        answered.append(accept)

    supervisor._respond = respond

    async def run():
        supervisor._admit_dialog(type=kind, message=message, default_prompt="", session_id=session, frame_id=None)
        await asyncio.sleep(0)
        for handle in supervisor._dialog_watchdogs.values():
            handle.cancel()

    asyncio.run(run())
    return answered


def test_reconnect_rewatches_tabs():
    sup, attaches = _tab_supervisor()

    async def targets(method, params=None, *, session_id=None, timeout=10.0):
        if method == "Target.getTargets":
            return {"result": {"targetInfos": [{"targetId": "TAB3", "type": "page", "url": "https://a.example/"}]}}
        if method == "Target.attachToTarget":
            attaches.append(params["targetId"])
            return {"result": {"sessionId": "s3"}}
        return {"result": {}}

    sup._cdp = targets
    sup._watch_new_tabs = True
    sup._reset_dialog_tabs()
    asyncio.run(sup._rewatch_dialog_tabs())
    assert attaches == ["TAB3"] and sup._dialog_tabs() == {"TAB3": "s3"}
