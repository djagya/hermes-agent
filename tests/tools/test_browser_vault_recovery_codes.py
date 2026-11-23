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


@pytest.mark.parametrize("label", ["Recovery email", "Recovery e-mail address", "Backup phone", "Recovery phone number",
                                   "Password recovery question", "Recovery answer", "Backup email", "Recovery password",
                                   "Recovery PIN", "Notfall Telefon", "Recovery codes email", "Recovery hint",
                                   "Recovery codes phone", "Backup codes SMS", "Recovery codes question",
                                   "Recovery codes answer", "Recovery codes hint", "Recovery codes address",
                                   "Wiederherstellungscodes Handy", "Recovery codes mobile", "Recovery codes username"])
def test_a_recovery_label_that_names_another_secret_is_not_read_as_codes(monkeypatch, label):
    item = {"fields": [{"id": "x", "type": "STRING", "label": label, "value": "1111-aaaa\n2222-bbbb"}]}
    _not_codes(lambda: _op(monkeypatch, item).resolve_recovery_codes("op:v1:i1", label.split()[0]))
    _not_codes(lambda: _op(monkeypatch, item).resolve_recovery_codes("op:v1:i1", label))


@pytest.mark.parametrize("label,section,kind", [
    ("Herstel telefoonnummer", "", "STRING"), ("Herstel antwoord", "", "STRING"), ("Herstelvraag", "", "STRING"),
    ("Mobilnummer Wiederherstellung", "", "STRING"), ("Rufnummer Wiederherstellung", "", "STRING"),
    ("Antwort Wiederherstellung", "", "STRING"),
    ("Réponse de récupération", "", "STRING"), ("Indice de récupération", "", "STRING"),
    ("Portable de secours", "", "STRING"), ("Respuesta de recuperación", "", "STRING"),
    ("Móvil de recuperación", "", "STRING"), ("Risposta di recupero", "", "STRING"),
    ("Cellulare di recupero", "", "STRING"),
    ("Recovery number", "", "STRING"), ("Recovery cell", "", "STRING"), ("Recovery contact", "", "STRING"),
    ("Recovery hint", "", "STRING"), ("Recovery question", "", "STRING"),
    ("Backup authenticator key", "", "STRING"), ("Backup authenticator key", "", "CONCEALED"),
    ("Recovery 2FA key", "", "STRING"), ("Recovery 2FA key", "", "CONCEALED"),
    ("Number", "Recovery", "STRING"), ("Cell", "Backup", "STRING"), ("1", "Recovery", "STRING"),
])
def test_a_label_without_a_code_word_is_never_read_even_with_recovery_wording(monkeypatch, label, section, kind):
    field = section or label.split()[0]
    entry = {"id": "x", "type": kind, "label": label, "value": "1111-aaaa\n2222-bbbb"}
    if section:
        entry["section"] = {"label": section}
    _not_codes(lambda: _op(monkeypatch, {"fields": [entry]}).resolve_recovery_codes("op:v1:i1", field))


def test_a_phone_field_before_the_real_codes_is_skipped(monkeypatch):
    item = {"fields": [
        {"id": "a", "type": "STRING", "label": "Recovery number", "value": "+49 151 23456789"},
        {"id": "b", "type": "CONCEALED", "label": "Recovery codes", "value": "abcd-1234\nefgh-5678"}]}
    assert _op(monkeypatch, item).resolve_recovery_codes("op:v1:i1", "recovery") == ["abcd-1234", "efgh-5678"]


@pytest.mark.parametrize("label", [
    "Recovery codes (PyPI)", "Notfallcodes", "Sicherungscodes", "Reservecodes",
    "Recovery codes (Postbank)", "Recovery codes (LastPass)", "Recovery codes (T-Mobile)", "Recovery codes (iPhone)",
    "Backup codes (1Password)", "Backup codes (Zipcar)", "Recovery codes (Compass)", "Backup-Einmalcodes",
    "One-time recovery codes"])
def test_a_code_label_that_names_the_site_or_a_compound_is_read(monkeypatch, label):
    item = {"fields": [{"id": "x", "type": "CONCEALED", "label": label, "value": "1111-aaaa\n2222-bbbb"}]}
    for field in (label, label.split()[0]):
        assert _op(monkeypatch, item).resolve_recovery_codes("op:v1:i1", field) == ["1111-aaaa", "2222-bbbb"]


def test_a_recovery_section_named_after_an_account_does_not_hide_the_real_codes(monkeypatch):
    item = {"fields": [{"id": "x", "type": "CONCEALED", "label": "Backup codes", "value": "1111-aaaa",
                        "section": {"label": "Email account"}}]}
    assert _op(monkeypatch, item).resolve_recovery_codes("op:v1:i1", "backup codes") == ["1111-aaaa"]


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

_KEY = "ab" * 32


@pytest.mark.parametrize("raw", [
    b"{truncated", b"", b"[]", b'{"used": []}',
    b"\xff\xfe\x00bad",                                              # not UTF-8 at all
    json.dumps({"key": "zz" * 32, "used": []}).encode(),              # 64 characters but not hex
    json.dumps({"key": _KEY, "used": [["x"]]}).encode(),              # an entry that is not a digest string
    json.dumps({"key": _KEY, "used": [1]}).encode(),
    json.dumps({"key": _KEY, "used": ["A" * 64]}).encode(),           # a digest must be 64 lower-case hex
    json.dumps({"key": _KEY, "used": ["junk"]}).encode(),
    b"[" * 200000,                                                   # json.loads raises RecursionError
])
def test_an_unreadable_ledger_refuses_instead_of_forgetting_spent_codes(recovery, tmp_path, raw):
    assert enter(field="recovery codes")["success"] is True
    (tmp_path / "used.json").write_bytes(raw)
    out = enter(field="recovery codes")
    assert out["error_type"] == "recovery_ledger_unreadable"
    assert recovery.write.call_count == 1
    assert (tmp_path / "used.json").read_bytes() == raw  # never re-keyed


def test_an_upper_cased_spent_digest_refuses_instead_of_offering_the_code_again(tmp_path, monkeypatch):
    path = tmp_path / "vault" / "used.json"
    monkeypatch.setattr(rc, "_ledger_path", lambda: path)
    assert rc.take_unused("i1", ["abcd-1234", "efgh-5678"]) == ("abcd-1234", 1)
    data = json.loads(path.read_text())
    data["used"] = [d.upper() for d in data["used"]]
    path.write_text(json.dumps(data))
    with pytest.raises(rc.RecoveryLedgerUnreadable):
        rc.take_unused("i1", ["abcd-1234", "efgh-5678"])


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
