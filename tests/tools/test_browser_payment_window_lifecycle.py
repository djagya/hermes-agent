"""Payment window lifecycle (dlz, 2026-11-12 redaction overreach): the transitions that left a profile-wide
window open with no payment in progress. A window ends only on control's confirmed unlock, fenced by the
generation observed first, or is taken back when its fill never wrote. Fake supervisors, fake control views;
synthetic data only."""

import json
from types import SimpleNamespace

import pytest

from agent import redact
from agent.redact_vault import CARD_PLACEHOLDER
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


def test_a_recovery_close_of_the_payment_tab_is_released_by_the_next_status_read(monkeypatch):
    # the model's last call of the turn closed the payment tab. The close itself reads no status (control's
    # watcher still says locked right after closeTarget, and a read would only hold the claim lock longer);
    # the window is released by the next status read, which sees the unlock.
    _routes(monkeypatch, t1={"slot_id": "shopping", "supervisor_url": URL})
    sup = _paying("t1")
    reads = []
    real_status = surface.lock_status
    monkeypatch.setattr(surface, "lock_status", lambda s: reads.append(s) or real_status(s))
    real = sup.on

    def on(method, params, session):
        if method == "Target.closeTarget":
            return {"success": True}
        return real(method, params, session)

    sup.on = on
    assert surface.recovery_call(sup, "Target.closeTarget", {"targetId": "PAGE"})["success"] is True
    assert reads == [] and _model() != PUBLIC  # no read under the claim lock: the window is still open
    sup.status = dict(UNLOCKED)  # control's watcher caught up
    assert surface.lock_status(sup) == "unlocked"
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
    assert "731" not in _model(SPLIT_PUBLIC) and "Ada Lovelace" not in _browser(SPLIT_PUBLIC)


def test_an_unwritten_fill_takes_back_only_its_own_open(monkeypatch):
    # an earlier payment on the slot wrote its card: a later fill that never wrote leaves that window as it was
    # (matrix 2), and a claim since the open keeps even its own (matrix 3)
    _routes(monkeypatch, task={"slot_id": "shopping", "supervisor_url": URL}, t1={"slot_id": "shopping"})
    earlier = _paying("t1", {**SPLIT_CARD, "cvc": "1234"})
    out, _committed = _fill(monkeypatch, ["2034", "2036"])
    assert out["error_type"] == "payment_expiry_option_unmatched"
    assert "Ada Lovelace" not in _browser(SPLIT_PUBLIC) and "9876" not in _model("pin 9876")
    assert _model("731 items") == "731 items"  # the unwritten card's 3-digit CVC class went with its open
    opened = bvp._register_card_redaction(CARD, earlier)
    card_values.note_claim(earlier)  # another claim on the slot since that open
    assert card_values.withdraw_payment_window(*opened) is False
    assert "737" not in _model("cvc 737")


def test_a_withdrawn_open_keeps_a_release_confirmed_meanwhile(monkeypatch):
    # an earlier window, reopened by a fill that never wrote, was released by control's unlock in between: the
    # take-back must not turn that release back into a profile-wide mask
    _routes(monkeypatch, t1={"slot_id": "shopping", "supervisor_url": URL})
    sup = _paying("t1")
    opened = bvp._register_card_redaction(CARD, sup)
    sup.status = dict(UNLOCKED)
    assert surface.lock_status(sup) == "unlocked" and _model() == PUBLIC
    assert card_values.withdraw_payment_window(*opened) is True
    assert _model() == PUBLIC and "Ada Testperson" not in _browser()


def test_a_released_window_masks_every_slots_browser_results(monkeypatch):
    # Codex r1 P1-2: a browser_exec workspace is per task, across slots, so a lease's slot is not provenance:
    # after the unlock, card text saved under the payment slot can be printed from any other slot
    _routes(monkeypatch, t1={"slot_id": "shopping", "supervisor_url": URL})
    sup = _paying("t1")
    sup.status = dict(UNLOCKED)
    assert surface.lock_status(sup) == "unlocked" and _model() == PUBLIC
    assert "Ada Testperson" not in _browser() and "737" not in _browser("cvc 737")
    assert redact.redact_sensitive_text("cvc 737", force=True, vault="log") != "cvc 737"


def test_a_route_moved_after_the_open_never_withdraws_the_other_slots_committed_window(monkeypatch):
    # Opus/Codex r1: t2 committed a card on "research"; t1 opened "shopping", its route then moved to
    # "research" and the commit fence refused. Both windows had the same generation (then 2: claim 1, open 2): withdrawing by
    # the route's slot at exit took back research's committed window.
    routes = {"t1": {"slot_id": "shopping", "supervisor_url": URL},
              "t2": {"slot_id": "research", "supervisor_url": "ws://control/slot/research"}}
    monkeypatch.setattr(browser_use_cli, "vault_route", lambda task_id: routes.get(task_id, {}))
    payer = _sup("t2", cdp_url="ws://control/slot/research")
    card_values.note_claim(payer)
    with bvp._UnwrittenWindow(CARD, payer) as committed:
        committed_open = committed._opened
        committed.committing()
    filler = _sup("t1")
    card_values.note_claim(filler)
    with bvp._UnwrittenWindow({**CARD, "cvc": "1234"}, filler):
        gens = redact.card_window_generations()
        assert set(gens) == {"shopping", "research"} and gens["shopping"] != gens["research"]
        routes["t1"] = routes["t2"]  # a newer exec moved t1 to research, then the fence refused
    assert redact.card_window_keys() == ["research"]  # shopping's unwritten open went, research's stayed
    assert "737" not in _model("cvc 737") and "Ada Testperson" not in _browser()
    assert card_values.withdraw_payment_window("research", committed_open[1]) is False  # sealed: no withdraw matches it


def test_an_orphaned_supervisor_reading_its_own_unlock_leaves_the_new_routes_window(monkeypatch):
    # Opus r1 P3-1: the supervisor saw the lock on shopping; its task's route then moved to research, where
    # another fill is live. Its own socket reads shopping unlocked: that says nothing about research.
    routes = {"t1": {"slot_id": "shopping", "supervisor_url": URL}}
    monkeypatch.setattr(browser_use_cli, "vault_route", lambda task_id: routes.get(task_id, {}))
    orphan = _sup("t1")
    with surface._LOCK:
        surface._KNOWN_LOCKED.add(orphan)
    routes["t1"] = routes["t2"] = {"slot_id": "research", "supervisor_url": "ws://control/slot/research"}
    _paying("t2")
    orphan.status = dict(UNLOCKED)
    assert surface.lock_status(orphan) == "unlocked"
    assert redact.card_window_keys(unreleased=True) == ["research"] and _model() != PUBLIC


def test_the_holder_name_is_masked_only_in_browser_and_log_output(monkeypatch):
    # owner decision B, 2026-11-12: while a window is open the holder (usually the owner) stays readable in
    # model and authored output; the card's digit classes still mask there, and browser and log mask both
    _routes(monkeypatch, t1={"slot_id": "shopping", "supervisor_url": URL})
    _paying("t1")
    text = "From: Ada Testperson, ada testperson; cvc 737, card 4111 1111 1111 1111"
    model = _model(text)
    assert "Ada Testperson" in model and "ada testperson" in model
    assert "737" not in model and "1111" not in model
    authored = redact.redact_json_text(json.dumps({"to": "Ada Testperson"}), audience="authored")
    assert json.loads(authored) == {"to": "Ada Testperson"}
    for vault in ("browser", "log"):
        out = redact.redact_sensitive_text(text, force=True, vault=vault)
        assert "Testperson" not in out and "testperson" not in out and CARD_PLACEHOLDER in out
        assert "737" not in out and "1111" not in out


R_URL = "ws://control/slot/research"
CARD2 = {"card_number": "5555555555554444", "cvc": "321", "cardholder_name": "Bea Other",
         "exp_month": "04", "exp_year": "2032"}


def test_a_withdrawn_open_whose_route_moved_leaves_no_association_for_a_later_unlock_read(monkeypatch):
    # Opus/Codex r2: the fill held supervisor A (socket on shopping) while its task's route moved to research
    # during the card lookup. The open is keyed on research and recorded in A's own windows; the commit fence
    # refuses and the withdraw takes the window back but, before r3, left that association. After another
    # task committed on research, A's own unlock read on shopping then released research's live window.
    routes = {"t1": {"slot_id": "shopping", "supervisor_url": URL}}
    monkeypatch.setattr(browser_use_cli, "vault_route", lambda task_id: routes.get(task_id, {}))
    a = _sup("t1")
    with surface._LOCK:
        surface._HELD.add(a)
    card_values.note_claim(a)
    routes["t1"] = {"slot_id": "research"}  # moved during the card lookup: no supervisor_url
    with bvp._UnwrittenWindow(CARD, a):
        pass  # the commit fence refuses: withdraw
    assert set(card_values._WINDOWS.get(a, ())) == set()
    assert redact.card_window_keys() == []
    routes["t2"] = {"slot_id": "research", "supervisor_url": R_URL}
    b = _sup("t2", cdp_url=R_URL)
    with surface._LOCK:
        surface._HELD.add(b)
    card_values.note_claim(b)
    with bvp._UnwrittenWindow(CARD2, b) as w:
        w.committing()
    before = _model("pan 5555 5555 5555 4444 cvc 321")
    assert before != "pan 5555 5555 5555 4444 cvc 321"
    a.status = dict(UNLOCKED)
    surface.lock_status(a)
    assert _model("pan 5555 5555 5555 4444 cvc 321") == before  # research stays masked for the model
    assert redact.card_window_keys(unreleased=True) == ["research"]


def test_a_withdraw_keeps_the_association_an_earlier_committed_open_made(monkeypatch):
    _routes(monkeypatch, t1={"slot_id": "shopping", "supervisor_url": URL})
    sup = _paying("t1")
    assert set(card_values._WINDOWS[sup]) == {"shopping"}
    with bvp._UnwrittenWindow(CARD2, sup):
        pass  # a second fill on the same slot never wrote
    assert set(card_values._WINDOWS[sup]) == {"shopping"}
    assert _model("cvc 737") != "cvc 737"


def test_a_managed_open_for_an_unbound_supervisor_is_refused_and_writes_nothing(monkeypatch):
    monkeypatch.setattr(surface, "_managed", lambda: True)
    _routes(monkeypatch, t1={"slot_id": "research"})  # attach failed: no supervisor_url
    sup = _sup("t1")
    with pytest.raises(card_values.PaymentWindowRefused):
        bvp._register_card_redaction(CARD, sup)
    assert redact.card_window_keys() == [] and not card_values._WINDOWS.get(sup)
    assert _model("cvc 737") == "cvc 737"


def test_a_late_withdraw_of_a_reused_generation_never_deletes_a_newer_window():
    # Codex r2 P2-1: F1's open stays unwritten while a mark clear ends the window and 300 other keys evict the
    # counter; the next claim and open on the same key restart at the same generation. F1's late withdraw
    # used to match and delete F2's window.
    redact.bump_card_window("A")
    first = redact.register_card_window("A", ("4" * 16, "123"), ("Ann A",))
    redact.release_card_window("A")
    for i in range(300):
        redact.bump_card_window(f"k{i}")
    redact.bump_card_window("A")
    second = redact.register_card_window("A", ("5" * 16, "321"), ("Bea B",))
    assert first != second
    assert redact.withdraw_card_window("A", first) is False
    assert redact.card_window_keys() == ["A"]
    assert "321" not in _model("cvc 321")


def test_a_seal_fails_closed_when_its_window_is_gone_and_the_fill_writes_nothing(monkeypatch):
    _routes(monkeypatch, t1={"slot_id": "shopping", "supervisor_url": URL})
    sup = _sup("t1")
    committed = []
    with pytest.raises(redact.CardWindowGone):
        with bvp._UnwrittenWindow(CARD, sup) as w:
            redact.clear_vault_redaction_values()  # a mark clear ended the window meanwhile
            w.committing()
            committed.append("write")
    assert committed == []


def test_a_route_moving_between_the_bound_check_and_the_key_never_keys_the_open_on_another_slot(monkeypatch):
    # Codex r3 P2 / Opus r3 P3-1: the bound check and the key used to read the route twice; a move in between
    # keyed the open on the new slot. One snapshot per open: the second read (which would move it) never happens.
    monkeypatch.setattr(surface, "_managed", lambda: True)
    routes = {"t1": {"slot_id": "shopping", "supervisor_url": URL}}
    reads = {"t1": 0}

    def vault_route(task_id):
        if task_id == "t1":
            reads["t1"] += 1
            if reads["t1"] == 2:  # the route moves right after the first read
                routes["t1"] = {"slot_id": "research"}
        return dict(routes.get(task_id, {}))

    monkeypatch.setattr(browser_use_cli, "vault_route", vault_route)
    a = _sup("t1")
    with surface._LOCK:
        surface._HELD.add(a)
    card_values.note_claim(a)
    reads["t1"] = 0
    routes["t2"] = {"slot_id": "research", "supervisor_url": R_URL}
    b = _sup("t2", cdp_url=R_URL)
    with bvp._UnwrittenWindow(CARD, a) as w:
        assert w._opened[0] == "shopping" and reads["t1"] == 1
        with surface._LOCK:
            surface._HELD.add(b)
        card_values.note_claim(b)  # t2 claims research while t1's fill is still unwritten
    with bvp._UnwrittenWindow(CARD2, b) as w2:
        w2.committing()
    before = _model("pan 5555 5555 5555 4444 cvc 321")
    assert before != "pan 5555 5555 5555 4444 cvc 321"
    a.status = dict(UNLOCKED)
    surface.lock_status(a)
    assert _model("pan 5555 5555 5555 4444 cvc 321") == before  # research is not released by A


def test_an_aborted_opens_failed_withdraw_still_retires_its_association(monkeypatch):
    # Codex r3 P2 variant: A's open was keyed on research (an unmanaged route that moved before the open); B
    # claims, opens and seals research before A aborts, so A's withdraw is refused. A must still stop owning
    # research, or its later unlock read on its own browser narrows B's live window.
    routes = {"t1": {"slot_id": "research"}, "t2": {"slot_id": "research", "supervisor_url": R_URL}}
    monkeypatch.setattr(browser_use_cli, "vault_route", lambda task_id: dict(routes.get(task_id, {})))
    a, b = _sup("t1"), _sup("t2", cdp_url=R_URL)
    with surface._LOCK:
        surface._HELD.update((a, b))
    card_values.note_claim(a)
    with bvp._UnwrittenWindow(CARD, a):
        assert set(card_values._WINDOWS[a]) == {"research"}
        card_values.note_claim(b)
        with bvp._UnwrittenWindow(CARD2, b) as w2:
            w2.committing()
    assert set(card_values._WINDOWS.get(a, ())) == set()  # retired though the withdraw was refused
    before = _model("pan 5555 5555 5555 4444 cvc 321")
    assert before != "pan 5555 5555 5555 4444 cvc 321"
    a.status = dict(UNLOCKED)
    surface.lock_status(a)
    assert _model("pan 5555 5555 5555 4444 cvc 321") == before
    assert redact.card_window_keys(unreleased=True) == ["research"]


def test_a_stale_release_never_matches_a_generation_a_dropped_counter_could_repeat():
    # Opus r3 P3-2: identity fixed the withdraw, but a release fenced by an observed generation compared
    # values that restart when a window-less key's counter is dropped. Generations come from one process-wide
    # counter now, so the observed value never equals a later one.
    redact.bump_card_window("A")
    redact.register_card_window("A", ("4" * 16, "123"), ("Ann A",))
    observed = redact.card_window_generations()["A"]
    redact.release_card_window("A")
    for i in range(300):
        redact.bump_card_window(f"k{i}")
    redact.bump_card_window("A")
    redact.register_card_window("A", ("5" * 16, "321"), ("Bea B",))
    assert redact.card_window_generations()["A"] != observed
    assert redact.release_card_window("A", observed, keep_for_browser=True) is False
    assert _model("cvc 321") != "cvc 321"
    assert redact.card_window_keys(unreleased=True) == ["A"]


def test_a_seal_refuses_a_window_a_confirmed_release_already_narrowed():
    # Opus r3 optional: the window still exists but a release narrowed it to browser output; a commit under it
    # would write the card with the model no longer masked. The seal fails closed.
    opened = redact.register_card_window("S", ("4" * 16, "737"), ("Ann A",))
    assert redact.release_card_window("S", redact.card_window_generations()["S"], keep_for_browser=True)
    with pytest.raises(redact.CardWindowGone):
        redact.seal_card_window("S", opened)


def test_an_observe_whose_route_moves_between_the_bound_check_and_the_key_never_observes_another_slot(monkeypatch):
    # Codex r4 P2: observe_windows checked slot_bound on one route read and took the key from a second one; a
    # route that moved to research in between made A observe research's generation, and A's own unlock then
    # released research while research stayed locked with its committed card. One snapshot per observe.
    raw = "pan 5555 5555 5555 4444 cvc 321"
    routes = {"t1": {"slot_id": "shopping", "supervisor_url": URL},
              "t2": {"slot_id": "research", "supervisor_url": R_URL}}
    moving = {"on": False, "reads": 0}

    def vault_route(task_id):
        if task_id == "t1" and moving["on"]:
            moving["reads"] += 1
            if moving["reads"] >= 2:  # every read after the first sees the route on research, still "bound"
                return {"slot_id": "research", "supervisor_url": URL}
        return dict(routes.get(task_id, {}))

    monkeypatch.setattr(browser_use_cli, "vault_route", vault_route)
    a = _sup("t1")
    b = _sup("t2", cdp_url=R_URL)
    with surface._LOCK:
        surface._HELD.add(b)
    card_values.note_claim(b)
    with bvp._UnwrittenWindow(CARD2, b) as w:
        w.committing()
    before = _model(raw)
    assert before != raw
    a.status = dict(UNLOCKED)
    moving["on"] = True
    surface.lock_status(a)
    assert redact.card_window_keys(unreleased=True) == ["research"] and _model(raw) == before
