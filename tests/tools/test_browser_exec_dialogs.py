"""Managed browser_exec dialog policy: answer ordinary dialogs, hold consequential ones for the agent."""

import asyncio
from types import SimpleNamespace

import pytest

from tools import browser_exec_dialogs as dialogs
from tools.browser_supervisor import CDPSupervisor


def _d(kind, message="", url="https://example.com/"):
    return SimpleNamespace(type=kind, message=message, url=url, default_prompt="")


@pytest.mark.parametrize("kind,expected", [("alert", "accept"), ("confirm", "dismiss"), ("prompt", "dismiss"),
                                           ("beforeunload", "accept")])
def test_auto_policy_on_an_ordinary_page(kind, expected):
    assert dialogs.make_decider(on_dialog="auto", slot_marked=False, identity="")(_d(kind)) == expected


@pytest.mark.parametrize("dialog,marked,identity", [
    (_d("confirm", "Delete this project?"), False, ""),
    (_d("confirm", "Continue?", url="https://shop.example/checkout/review"), False, ""),
    (_d("beforeunload"), True, ""),               # slot holds a card-fill mark
    (_d("prompt"), False, "sensitive"),
])
def test_consequential_dialogs_are_held_even_when_the_call_says_accept(dialog, marked, identity):
    decide = dialogs.make_decider(on_dialog="accept", slot_marked=marked, identity=identity)
    assert decide(dialog) == "hold"


def test_on_dialog_accept_applies_to_ordinary_pages():
    assert dialogs.make_decider(on_dialog="accept", slot_marked=False, identity="")(_d("confirm", "Leave?")) == "accept"


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
