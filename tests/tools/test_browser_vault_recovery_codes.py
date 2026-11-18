"""Stored recovery codes, review E round 2 (2026-11-18): only a recovery-code field is ever read (never the
password, username or TOTP seed), the code's pixels are masked before it lands, handle aliases share one
spent-code record, two processes never pick the same code, a corrupt ledger refuses, codes parse per line.
Synthetic items and pages only."""
import json
import multiprocessing as mp
import queue
from pathlib import Path

import pytest

from agent import vault_recovery_codes as rc
from agent.vault_backends import local as local_mod
from agent.vault_backends.onepassword import OnePasswordLoginBackend
from agent.vault_login_classifier import build_fill_js
from tests.tools import test_browser_vault_credential_fills as cred
from tests.tools.test_browser_vault_signup import _run
from tools import browser_vault_tool as vault

ORIGIN, ctl, enter = cred.ORIGIN, cred.ctl, cred.enter
core, recovery = cred.core, cred.recovery  # the shared fixtures (synthetic backend, page and ledger)

PW = "correct-horse-battery-staple"
SEED = "JBSWY3DPEHPK3PXP"
ITEM = {"id": "i1", "fields": [
    {"id": "username", "type": "STRING", "purpose": "USERNAME", "label": "username", "value": "danil@example.test"},
    {"id": "password", "type": "CONCEALED", "purpose": "PASSWORD", "label": "password", "value": PW},
    {"id": "TOTP_x", "type": "OTP", "label": "one-time password", "value": SEED, "totp": "123456"},
    {"id": "n1", "type": "STRING", "label": "notes", "value": "pin 4711 safe"},
    {"id": "rc", "type": "CONCEALED", "label": "recovery codes", "value": "abcd-1234 efgh-5678"}]}


def _op(monkeypatch, item=ITEM):
    backend = OnePasswordLoginBackend({})
    monkeypatch.setattr(backend, "_resolve_handle", lambda h: ("v1", "i1"))
    monkeypatch.setattr(backend, "_run", lambda *a: json.dumps(item))
    return backend


def _not_codes(call):
    with pytest.raises(Exception) as err:
        call()
    assert type(err.value).__name__ == "RecoveryFieldNotCodes"
    assert PW not in str(err.value) and SEED not in str(err.value)


# ── 1. the named field must be a recovery-code field ───────────────────────────────────────────────────────

@pytest.mark.parametrize("field", ["password", "Password", "one-time password", "one", "username", "notes"])
def test_the_password_totp_seed_or_an_unnamed_field_is_never_typed_as_a_recovery_code(recovery, monkeypatch, field):
    recovery.backend.resolve_recovery_codes = _op(monkeypatch).resolve_recovery_codes
    out = enter(field=field)
    assert out["success"] is False and out["error_type"] == "recovery_field_not_codes", out
    assert not any(PW in c.args[1] or SEED in c.args[1] for c in recovery.write.call_args_list)
    recovery.write.assert_not_called()
    assert PW not in json.dumps(out) and SEED not in json.dumps(out)


@pytest.mark.parametrize("field", ["password", "one-time password", "one", "username", "notes"])
def test_onepassword_never_returns_the_password_username_or_totp_field(monkeypatch, field):
    _not_codes(lambda: _op(monkeypatch).resolve_recovery_codes("op:v1:i1", field))


@pytest.mark.parametrize("label", ["Backup codes", "Wiederherstellungscodes", "Codes de récupération",
                                   "Códigos de respaldo", "Codici di backup", "Herstelcodes", "Codes", "Login codes"])
def test_recovery_wording_in_six_languages_is_read(monkeypatch, label):
    item = {"fields": [{"id": "x", "type": "CONCEALED", "label": label, "value": "1111-aaaa\n2222-bbbb"}]}
    assert _op(monkeypatch, item).resolve_recovery_codes("op:v1:i1", label) == ["1111-aaaa", "2222-bbbb"]


def test_a_password_labelled_field_inside_a_recovery_section_is_not_read(monkeypatch):
    item = {"fields": [{"id": "p2", "type": "CONCEALED", "label": "password", "value": PW,
                        "section": {"label": "Recovery codes"}}]}
    _not_codes(lambda: _op(monkeypatch, item).resolve_recovery_codes("op:v1:i1", "recovery codes"))


@pytest.mark.parametrize("field", ["password", "otp secret", "otp_secret", "username"])
def test_the_local_vault_never_reads_password_seed_or_identifier_keys(monkeypatch, field):
    store = type("S", (), {"resolve_secret": lambda self, h: {
        "username": "danil", "password": PW, "otp_secret": SEED, "recovery_codes": "1111-2222"}})()
    monkeypatch.setattr(local_mod, "_store", lambda: store)
    backend = local_mod.LocalLoginBackend.__new__(local_mod.LocalLoginBackend)
    _not_codes(lambda: backend.resolve_recovery_codes("local:x", field))
    assert backend.resolve_recovery_codes("local:x", "recovery codes") == ["1111-2222"]


def test_the_approval_names_the_field(recovery):
    assert enter(field="recovery codes")["success"] is True
    message = recovery.consent.call_args.args[0]
    assert "'recovery codes'" in message and ORIGIN in message


# ── 2. screenshots never show the code ─────────────────────────────────────────────────────────────────────

CODE = "abcd-1234"
_CSS = ("globalThis.CSS = { supports: (p, v) => p === '-webkit-text-security' && v === 'disc' };"
        "byIdx[2].style = { p: {}, setProperty(k, v) { writes.push('mask'); this.p[k] = v; },"
        " getPropertyValue(k) { return this.p[k] || ''; } };")
_TYPE = ("let t = byIdx[2].type; Object.defineProperty(byIdx[2], 'type', { get() { return t; },"
         " set(v) { writes.push('type:' + v); if (%s) t = v; } });")


@pytest.mark.parametrize("mutate,writes,reason", [
    (_CSS, ["mask", 2], "written"),                       # Chromium: -webkit-text-security before the value
    (_TYPE % "true", ["type:password", 2], "written"),    # no CSS mask: type=password before the value
    (_TYPE % "false", ["type:password"], "mask_failed"),  # neither takes: the code is never written
])
def test_the_writer_masks_the_field_before_the_code_lands(mutate, writes, reason):
    script = build_fill_js([{"index": 2, "token": "one-time-code", "value": CODE, "mask": True}], ORIGIN, "nonce")
    observed = _run(script, [ctl(index=2, name="recovery_code")], mutate, expected=CODE)
    assert observed["writes"] == writes
    assert json.loads(observed["raw"])["reasons"] == [reason]


def test_the_recovery_write_asks_for_the_mask(recovery):
    enter(field="recovery codes")
    payload = recovery.write.call_args.args[1]
    assert '"mask": true' in payload and "-webkit-text-security" in payload


# ── 3. handle aliases ──────────────────────────────────────────────────────────────────────────────────────

def test_handle_aliases_of_one_item_share_the_spent_code_record(recovery):
    recovery.meta.id = "op:v1:i1"  # the backend's canonical id, whichever spelling found it
    for handle, code in (("op:i1", "aaaa-1111"), ("op:v1:i1", "bbbb-2222")):
        out = json.loads(vault.browser_vault_enter_code(handle, code_source="recovery", field="recovery codes"))
        assert out["success"] is True and code in recovery.write.call_args.args[1]


# ── 4. two processes ───────────────────────────────────────────────────────────────────────────────────────

CODES = [f"code-{i:04d}" for i in range(20)]


def _take_worker(ledger: str, start, results) -> None:
    import time

    from agent import vault_recovery_codes as codes

    codes._ledger_path = lambda: Path(ledger)
    read = codes._load
    # Widen the read-to-save window so the race is real; the lock under test must close it.
    codes._load = lambda path: (read(path), time.sleep(0.05))[0]  # health: allow HX013 -- the delay is the race window
    results.put("ready")
    start.wait(20)
    results.put([codes.take_unused("op:v1:i1", CODES)[0] for _ in range(10)])


def test_two_processes_selecting_at_once_never_take_the_same_code(tmp_path):
    ctx = mp.get_context("spawn")
    start, results = ctx.Event(), ctx.Queue()
    ledger = str(tmp_path / "vault" / "recovery_codes_used.json")
    procs = [ctx.Process(target=_take_worker, args=(ledger, start, results)) for _ in range(2)]
    for p in procs:
        p.start()
    try:
        assert [results.get(timeout=60) for _ in procs] == ["ready", "ready"]
        start.set()
        taken = [results.get(timeout=60) for _ in procs]
    except queue.Empty:
        pytest.fail("a ledger worker did not answer")
    finally:
        for p in procs:
            p.join(30)
    picked = taken[0] + taken[1]
    assert None not in picked and sorted(picked) == sorted(CODES)  # 20 distinct codes, none twice
    assert len(json.loads(Path(ledger).read_text())["used"]) == 20
    assert not [p for p in Path(ledger).parent.iterdir() if p.name.endswith(".tmp")]


# ── 5. a corrupt ledger refuses ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["{truncated", "", "[]", '{"used": []}'])
def test_an_unreadable_ledger_refuses_instead_of_forgetting_spent_codes(recovery, tmp_path, text):
    assert enter(field="recovery codes")["success"] is True
    (tmp_path / "used.json").write_text(text)
    out = enter(field="recovery codes")
    assert out["error_type"] == "recovery_ledger_unreadable"
    assert recovery.write.call_count == 1
    assert (tmp_path / "used.json").read_text() == text  # never re-keyed


def test_the_ledger_is_private_and_holds_no_code(recovery, tmp_path):
    enter(field="recovery codes")
    path = tmp_path / "used.json"
    assert path.stat().st_mode & 0o777 == 0o600 and "aaaa" not in path.read_text()


# ── 6. code parsing ────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,codes", [
    ("1234 5678\n8765 4321", ["12345678", "87654321"]),          # Google: digit groups are one code
    ("1234 5678 8765 4321", ["12345678", "87654321"]),           # two Google codes on one line
    ("GitHub recovery codes:\nabcd1-ef234\n5678a-bc901", ["abcd1-ef234", "5678a-bc901"]),  # words dropped
    ("1. 0123456789abcdef\n2. fedcba9876543210", ["0123456789abcdef", "fedcba9876543210"]),
    ("12345678, 87654321; 11112222", ["12345678", "87654321", "11112222"]),
    ("abcd-1234 efgh-5678", ["abcd-1234", "efgh-5678"]),
])
def test_codes_are_parsed_per_line(value, codes):
    assert rc.parse_codes([value]) == codes
