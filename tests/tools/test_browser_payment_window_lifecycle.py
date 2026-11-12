"""Payment window lifecycle (dlz, 2026-11-12 redaction overreach): the transitions that left a profile-wide
window open with no payment in progress. A window ends only on control's confirmed unlock, fenced by the
generation observed first, or is taken back when its fill never wrote. Fake supervisors, fake control views;
synthetic data only."""

import json
from types import SimpleNamespace

import pytest

from agent import redact
from tools import browser_payment_card_values as card_values
from tools import browser_payment_privacy as privacy
from tools import browser_payment_surface as surface
from tools import browser_use_cli
from tools import browser_vault_payment as bvp
from tests.tools.test_browser_payment_surface import FakeSup
from tests.tools.test_browser_vault_payment_split import CARD as SPLIT_CARD, PAYMENT_PSP_ORIGINS, _payone_inspection

CARD = {"card_number": "4000056655665556", "cvc": "737", "cardholder_name": "Ada Testperson",
        "exp_month": "03", "exp_year": "2031"}
LOCKED = {"locked": True, "native": True, "tabs": ["TAB"], "unverified": False}
UNLOCKED = {"locked": False, "native": False, "tabs": [], "unverified": False}
# an unrelated mail preview: the owner's name as sender, a count, a status id and a wallet address
PUBLIC = ("From: Ada Testperson | Example Research\nPublic count: 731\n"
          "https://x.com/example/status/" + "7" * 19 + "\nProposer: 0x" + "a" * 10 + "731" + "b" * 27)
URL = "ws://control/slot/shopping"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(surface, "_managed", lambda: False)
    redact.clear_vault_redaction_values()
    yield
    redact.clear_vault_redaction_values()


def _routes(monkeypatch, **routes):
    monkeypatch.setattr(browser_use_cli, "vault_route", lambda task_id: routes.get(task_id, {}))


def _sup(task_id, status=LOCKED, cdp_url=URL):
    sup = FakeSup()
    sup.task_id, sup.status, sup.cdp_url = task_id, dict(status), cdp_url
    return sup


def _paying(task_id, card=CARD):
    """A supervisor that claimed (this process saw it locked) and filled ``card``."""
    sup = _sup(task_id)
    with surface._LOCK:
        surface._HELD.add(sup)
    bvp._register_card_redaction(card, sup)
    return sup


def _model(text=PUBLIC):
    return redact.redact_sensitive_text(text, force=True)


def _browser(text=PUBLIC):
    return redact.redact_sensitive_text(text, force=True, vault="browser")


def test_a_replacement_supervisor_reads_the_unlock_the_paying_one_never_saw(monkeypatch):
    # 11-12: the fill stopped, the payment tab was closed in Chrome, and the paying supervisor never read a
    # status again. Its replacement on the slot read "unlocked" but had never seen the lock, so nothing was
    # released and every output of the profile stayed masked until the next fill.
    route = {"slot_id": "shopping", "supervisor_url": URL}
    _routes(monkeypatch, t1=route, t2=route)
    _paying("t1")
    assert _model() != PUBLIC
    later = _sup("t2", UNLOCKED)
    assert surface.lock_status(later) == "unlocked"
    assert _model() == PUBLIC  # matrix 1: name, count, status id and wallet intact after the confirmed unlock
    assert "Ada Testperson" not in _browser() and "737" not in _browser("cvc 737")  # matrix 5


@pytest.mark.parametrize("route, status", [
    ({"slot_id": "shopping", "supervisor_url": "ws://control/slot/research"}, UNLOCKED),  # its socket is elsewhere
    ({"slot_id": "shopping", "supervisor_url": ""}, UNLOCKED),  # the attach to this route failed
    ({"slot_id": "shopping", "supervisor_url": URL}, {"locked": False}),  # malformed status: unknown
    ({"slot_id": "shopping", "supervisor_url": URL}, RuntimeError("no answer")),
    ({"slot_id": "shopping", "supervisor_url": URL}, LOCKED),
])
def test_an_unlock_that_does_not_prove_this_slot_releases_nothing(monkeypatch, route, status):
    # matrix 4: a supervisor that never saw the lock releases only on a clean unlock from the socket its
    # route names; another browser's status, a failed attach, a malformed or missing answer keep the window
    _routes(monkeypatch, t1={"slot_id": "shopping", "supervisor_url": URL}, t2=route)
    _paying("t1")
    other = _sup("t2")
    other.status = status if isinstance(status, Exception) else dict(status)
    surface.lock_status(other)
    assert _model() != PUBLIC


def test_a_replacements_stale_unlock_never_releases_a_window_claimed_meanwhile(monkeypatch):
    # matrix 3: the never-held path is fenced like the held one: a fill that claimed and opened while the
    # status read was on the wire keeps its window
    route = {"slot_id": "shopping", "supervisor_url": URL}
    _routes(monkeypatch, t1=route, t2=route)
    reader = _sup("t2", UNLOCKED)
    real = reader.on

    def straddled(method, params, session):
        if method == "Hermes.paymentSurfaceStatus":
            _paying("t1")
        return real(method, params, session)

    reader.on = straddled
    assert surface.lock_status(reader) == "unlocked"
    assert _model() != PUBLIC


def _view(slot_id, **surface_fields):
    return {"slot_id": slot_id, "capture_blocked": True, "human_clear": None,
            "payment_surface": {"locked": False, "tabs": [], "unverified": False, "native_connected": False,
                                "claimed_at": None, **surface_fields}}


def test_controls_view_releases_a_slot_no_supervisor_reads_any_more(monkeypatch):
    # the paying task ended and nothing drives that slot again: the next browser_exec of the profile (any slot)
    # asks control for the slots whose window is still open
    import tools.browser_control_route as control

    _routes(monkeypatch, t1={"slot_id": "shopping", "supervisor_url": URL})
    _paying("t1")
    monkeypatch.setattr(privacy, "_managed", lambda: True)
    asked = []
    monkeypatch.setattr(control, "capture_guard_view", lambda slot_id: asked.append(slot_id) or _view(slot_id))
    privacy.reconcile_payment_windows()
    assert asked == ["shopping"] and _model() == PUBLIC and "Ada Testperson" not in _browser()
    privacy.reconcile_payment_windows()
    assert asked == ["shopping"]  # nothing open: control is not asked


@pytest.mark.parametrize("view", [
    _view("shopping", locked=True, tabs=["TAB"]),
    _view("shopping", unverified=True),
    _view("research"),  # another slot's answer
    {"slot_id": "shopping", "capture_blocked": True},  # a control without payment_surface
    {"error": "unavailable"},
    {**_view("shopping"), "error": "stale"},
    None,
])
def test_controls_view_keeps_the_window_unless_it_proves_the_unlock(monkeypatch, view):
    import tools.browser_control_route as control

    _routes(monkeypatch, t1={"slot_id": "shopping", "supervisor_url": URL})
    _paying("t1")
    monkeypatch.setattr(privacy, "_managed", lambda: True)
    monkeypatch.setattr(control, "capture_guard_view", lambda slot_id: view)
    privacy.reconcile_payment_windows()
    assert _model() != PUBLIC


def test_controls_view_read_before_a_claim_never_releases_that_claims_window(monkeypatch):
    # matrix 3: the view was read, then a claim and a fill opened the window, then the stale answer arrived
    import tools.browser_control_route as control

    _routes(monkeypatch, t1={"slot_id": "shopping", "supervisor_url": URL})
    first = _paying("t1")
    monkeypatch.setattr(privacy, "_managed", lambda: True)

    def view(slot_id):
        card_values.note_claim(first)
        bvp._register_card_redaction(CARD, first)
        return _view(slot_id)

    monkeypatch.setattr(control, "capture_guard_view", view)
    privacy.reconcile_payment_windows()
    assert _model() != PUBLIC


def test_a_recovery_close_of_the_payment_tab_releases_without_another_call(monkeypatch):
    # the model's last call of the turn closed the payment tab: the window stayed open until some later
    # browser call read the status
    _routes(monkeypatch, t1={"slot_id": "shopping", "supervisor_url": URL})
    sup = _paying("t1")
    real = sup.on

    def on(method, params, session):
        if method == "Target.closeTarget":
            sup.status = dict(UNLOCKED)
            return {"success": True}
        return real(method, params, session)

    sup.on = on
    assert surface.recovery_call(sup, "Target.closeTarget", {"targetId": "PAGE"})["success"] is True
    assert _model() == PUBLIC and "Ada Testperson" not in _browser()


def _fill(monkeypatch, years):
    """``fill_payment`` on a four-frame checkout whose expiry year select offers ``years``."""
    inspection, committed = _payone_inspection(years), []

    class Supervisor:
        task_id = "task"

        def inspect_payment(self, *a, **k):
            return inspection

        def payment_is_current(self, i):
            return True

        def commit_payment(self, i, fills, strict=False):
            committed.extend(fills)
            i.consumed = True
            return {"success": True, "filled_fields": len(fills)}

        def discard_payment(self, i):
            i.consumed = True

    sup = Supervisor()
    monkeypatch.setattr("tools.browser_vault_tool._ensure_supervisor", lambda task_id: sup)
    monkeypatch.setattr("tools.browser_vault_tool._bot_desktop_browser_session", lambda task_id: False)
    monkeypatch.setattr("tools.browser_vault_tool._is_managed", lambda: False)
    monkeypatch.setattr("tools.browser_payment_privacy.mark_payment_session", lambda task_id, surface=None: None)
    monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", lambda *a, **k: "accept")
    meta = SimpleNamespace(id="h", kind="payment", label="Card", allowed_origins=(), origin=None,
                           payment_scope="confirmed_page", payment_frame_origins=PAYMENT_PSP_ORIGINS)
    backend = SimpleNamespace(name="local", resolve_secret=lambda handle: dict(SPLIT_CARD), get_meta=lambda handle: meta)
    return json.loads(bvp.fill_payment(backend, meta, "task")), committed


SPLIT_PUBLIC = "From: Ada Lovelace | Example Research, 731 items"


def test_a_fill_refused_before_its_first_write_leaves_no_window(monkeypatch):
    # the card was read, then the expiry select had no matching option: nothing was written, yet the window
    # masked the holder's name and every 3- and 16-digit run in every output of the profile
    _routes(monkeypatch, task={"slot_id": "shopping", "supervisor_url": URL})
    out, committed = _fill(monkeypatch, ["2034", "2036"])
    assert out["error_type"] == "payment_expiry_option_unmatched" and not committed
    assert _model(SPLIT_PUBLIC) == SPLIT_PUBLIC and _browser(SPLIT_PUBLIC) == SPLIT_PUBLIC


def test_a_fill_that_reached_the_commit_keeps_its_window(monkeypatch):
    # matrix 2: once the commit ran, card values may be in the page: only control's unlock ends the window
    _routes(monkeypatch, task={"slot_id": "shopping", "supervisor_url": URL})
    out, committed = _fill(monkeypatch, ["34", "35", "36"])
    assert out["success"] and committed
    assert "Ada Lovelace" not in _model(SPLIT_PUBLIC) and "731" not in _model(SPLIT_PUBLIC)


def test_an_unwritten_fill_takes_back_only_its_own_open(monkeypatch):
    # an earlier payment on the slot wrote its card: a later fill that never wrote leaves that window as it was
    # (matrix 2), and a claim since the open keeps even its own (matrix 3)
    _routes(monkeypatch, task={"slot_id": "shopping", "supervisor_url": URL}, t1={"slot_id": "shopping"})
    earlier = _paying("t1", {**SPLIT_CARD, "cvc": "1234"})
    out, _committed = _fill(monkeypatch, ["2034", "2036"])
    assert out["error_type"] == "payment_expiry_option_unmatched"
    assert "Ada Lovelace" not in _model(SPLIT_PUBLIC) and "9876" not in _model("pin 9876")
    assert _model("731 items") == "731 items"  # the unwritten card's 3-digit CVC class went with its open
    generation = bvp._register_card_redaction(CARD, earlier)
    card_values.note_claim(earlier)  # another claim on the slot since that open
    assert card_values.withdraw_payment_window(earlier, generation) is False
    assert "737" not in _model("cvc 737")


def test_a_withdrawn_open_keeps_a_release_confirmed_meanwhile(monkeypatch):
    # an earlier window, reopened by a fill that never wrote, was released by control's unlock in between: the
    # take-back must not turn that release back into a profile-wide mask
    _routes(monkeypatch, t1={"slot_id": "shopping", "supervisor_url": URL})
    sup = _paying("t1")
    generation = bvp._register_card_redaction(CARD, sup)
    sup.status = dict(UNLOCKED)
    assert surface.lock_status(sup) == "unlocked" and _model() == PUBLIC
    assert card_values.withdraw_payment_window(sup, generation) is True
    assert _model() == PUBLIC and "Ada Testperson" not in _browser()


def test_a_released_window_masks_its_own_slots_browser_results_only(monkeypatch):
    # after the confirmed unlock the payment page may still show card text, so its slot's browser results stay
    # masked until the mark clears; a result control's lease attributes to another slot cannot carry that page
    _routes(monkeypatch, t1={"slot_id": "shopping", "supervisor_url": URL})
    sup = _paying("t1")

    def browser(slot):
        return redact.redact_sensitive_text(PUBLIC, force=True, vault="browser", vault_slot=slot)

    assert browser("research") != PUBLIC  # matrix 2, 6: while the lock holds, every output of the profile
    sup.status = dict(UNLOCKED)
    assert surface.lock_status(sup) == "unlocked"
    assert browser("research") == PUBLIC
    assert browser("shopping") != PUBLIC and browser("") != PUBLIC  # matrix 4, 5: its own slot, and an unknown one
    assert redact.redact_sensitive_text(PUBLIC, force=True, vault="log", vault_slot="research") != PUBLIC


def test_a_released_window_without_a_slot_masks_every_slots_browser_results(monkeypatch):
    # matrix 4: a supervisor with no slot route could be on any browser, so its released window is not scoped
    _routes(monkeypatch)
    sup = _paying("t1")
    sup.status = dict(UNLOCKED)
    assert surface.lock_status(sup) == "unlocked" and _model() == PUBLIC
    assert redact.redact_sensitive_text(PUBLIC, force=True, vault="browser", vault_slot="research") != PUBLIC
