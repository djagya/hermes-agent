"""Stored recovery codes (review E round 2, 2026-11-18; label redesign J-0141, 2026-11-23): the backend picks the
item's one field whose label says recovery/backup codes, the approval names that exact label, and only that
field is read after it, at the same item revision (never the password, username, TOTP seed or any other field).
The code's pixels are masked before it lands, handle aliases share one spent-code record, two processes never
pick the same code, a corrupt ledger refuses, codes parse per line. Synthetic items and pages only."""
import json
import multiprocessing as mp
import queue
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import vault_recovery_codes as rc
from agent.vault_backends import local as local_mod
from agent.vault_backends import onepassword as op_mod
from agent.vault_login_classifier import build_fill_js
from tests.tools import test_browser_vault_credential_fills as cred
from tests.tools.test_browser_vault_signup import _run
from tools import browser_vault_tool as vault

ORIGIN, ctl, enter = cred.ORIGIN, cred.ctl, cred.enter
core, recovery = cred.core, cred.recovery  # the shared fixtures (synthetic backend, page and ledger)

PW = "correct-horse-battery-staple"
SEED = "JBSWY3DPEHPK3PXP"
OTHER = "qqqq-7777"  # the value of a field that is not the recovery codes; parses as a code, so a read types it
STORED = "wxyz-1111\nwxyz-2222"


def item(*fields, version=7):
    """A 1Password Login item as ``op item get --format json`` prints it: the login's own fields, then
    ``fields`` as ``(label, value)`` or ``(label, value, section)``."""
    own = [
        {"id": "username", "type": "STRING", "purpose": "USERNAME", "label": "username", "value": "danil@example.test"},
        {"id": "password", "type": "CONCEALED", "purpose": "PASSWORD", "label": "password", "value": PW},
        {"id": "TOTP_x", "type": "OTP", "label": "one-time password", "value": SEED, "totp": "123456"},
        {"id": "notesPlain", "type": "STRING", "purpose": "NOTES", "label": "notesPlain", "value": "pin 4711 safe"}]
    extra = [{"id": f"f{i}", "type": "CONCEALED", "label": f[0], "value": f[1],
              **({"section": {"id": "s1", "label": f[2]}} if len(f) > 2 else {})} for i, f in enumerate(fields)]
    return {"id": "i1", "version": version, "updated_at": "2026-11-23T10:00:00Z", "fields": own + extra}


class FakeOp:
    """The ``op`` CLI under the real 1Password backend: prints ``item`` (``after_approval`` for the revealing
    read, when set) with every value, as op may, and records each read. ``metadata_failure`` /
    ``reveal_failure``: an exception ``run_cli`` raises, ``str`` stderr of a failed op, or ``bytes`` printed as is."""

    def __init__(self):
        self.item, self.after_approval, self.calls = item(), None, []
        self.metadata_failure = self.reveal_failure = None

    def __call__(self, argv, **_kw):
        reveal = "--reveal" in argv
        self.calls.append("reveal" if reveal else "metadata")
        failure = self.reveal_failure if reveal else self.metadata_failure
        if isinstance(failure, Exception):
            raise failure
        if isinstance(failure, str):
            return SimpleNamespace(returncode=1, stdout="", stderr=failure)
        if isinstance(failure, bytes):
            return SimpleNamespace(returncode=0, stdout=failure.decode(), stderr="")
        shown = self.after_approval if reveal and self.after_approval is not None else self.item
        return SimpleNamespace(returncode=0, stdout=json.dumps(shown), stderr="")


@pytest.fixture
def op(recovery, monkeypatch):
    """The real 1Password backend behind ``enter_code code_source=recovery``, over :class:`FakeOp`."""
    backend = op_mod.OnePasswordLoginBackend({})
    backend._service_token = "synthetic-service-token"
    monkeypatch.setattr(backend, "_op", lambda: Path("op"))
    monkeypatch.setattr(backend, "_resolve_handle", lambda handle: ("v1", "i1"))
    fake = FakeOp()
    monkeypatch.setattr(op_mod, "run_cli", fake)
    monkeypatch.setattr(vault, "_saved_login", lambda handle: (backend, recovery.meta))
    return fake


def typed(recovery) -> str:
    """Everything the page writer was handed."""
    return "".join(call.args[1] for call in recovery.write.call_args_list)


# ── 1. which field is read: the backend picks, the approval names it, the read is bound to it ──────────────────

# Labels that must never be read as recovery codes, as (label, section). Rows 1-13 are the final reviews' and the
# approach verdict's cases (J-0141): each was typed by the old "code word minus a deny list" rule.
NOT_RECOVERY_CODES = [
    ("Code (TOTP)", ""), ("Code (PIN)", ""), ("Pincode", ""), ("Security code", ""), ("Code word", ""),
    ("Codice fiscale", ""), ("Code confidentiel", ""), ("Access code", ""), ("Sort code", ""), ("CVV code", ""),
    ("Codes (2FA seed)", ""), ("One-time code", ""), ("Code", "TOTP"),
    ("Code", "PIN"), ("Code", "Password"), ("Code 3", "Recovery codes"), ("password", "Recovery codes"),
    ("Code (OTP)", ""), ("Codes (password)", ""), ("Code (backup phone)", ""), ("Codes (recovery)", ""),
    ("Codes", ""), ("Login codes", ""), ("Einmalcode", ""), ("Passcode", ""), ("PIN code", ""), ("2FA code", ""),
    ("Authenticator code", ""), ("Verification code", ""), ("Master code", ""), ("Door code", ""), ("PUK code", ""),
    ("Sicherheitscode", ""), ("Zugangscode", ""), ("Kartencode", ""), ("Code de sécurité", ""), ("Code d'accès", ""),
    ("Código de seguridad", ""), ("Código de acceso", ""), ("Codice di sicurezza", ""), ("Codice di accesso", ""),
    ("Beveiligingscode", ""), ("Toegangscode", ""), ("Telefoon code", ""),
    # recovery wording without a code word
    ("Recovery email", ""), ("Backup phone", ""), ("Recovery phone number", ""), ("Recovery answer", ""),
    ("Recovery password", ""), ("Recovery PIN", ""), ("Recovery key", ""), ("Recovery number", ""),
    ("Backup authenticator key", ""), ("Recovery 2FA key", ""), ("Notfall Telefon", ""),
    ("Herstel telefoonnummer", ""), ("Mobilnummer Wiederherstellung", ""), ("Réponse de récupération", ""),
    ("Portable de secours", ""), ("Respuesta de recuperación", ""), ("Cellulare di recupero", ""),
    ("Number", "Recovery"), ("1", "Backup"),
    # a stem spelled across two unrelated words is not that word
    ("Contraseña del disco de respaldo", ""), ("Clave del disco de recuperación", ""), ("More cover codec", ""),
    # a label the approval could not show as it is
    ("Recovery codes\nPassword", ""), ("Recovery codes " + "x" * 80, ""),
]


@pytest.mark.parametrize("label,section", NOT_RECOVERY_CODES)
def test_a_field_not_labelled_recovery_codes_is_never_read_or_typed(op, recovery, label, section):
    op.item = item((label, OTHER, section)) if section else item((label, OTHER))
    for field in ("", label, section, "code", "codes", "recovery", "backup", "recovery codes", "one", "1", "c"):
        out = enter(field=field)
        assert out["success"] is False, (field, out)
        assert OTHER not in json.dumps(out)
    recovery.consent.assert_not_called()   # nobody is even asked
    recovery.write.assert_not_called()
    assert "reveal" not in op.calls


RECOVERY_CODES = [
    ("Backup codes", ""), ("Wiederherstellungscodes", ""), ("Codes de récupération", ""), ("Códigos de respaldo", ""),
    ("Codici di backup", ""), ("Herstelcodes", ""),                                              # six languages
    ("Recovery codes (PyPI)", ""), ("Notfallcodes", ""), ("Backup codes (1Password)", ""),
    ("recovery codes", ""), ("Recovery codes (Postbank)", ""), ("Recovery codes (T-Mobile)", ""),
    ("Sicherungscodes", ""), ("Reservecodes", ""), ("Notfall-Codes", ""), ("Back up codes", ""), ("Ersatzcodes", ""),
    ("Backup-Einmalcodes", ""), ("One-time recovery codes", ""), ("Emergency codes", ""), ("Codes de secours", ""),
    ("Códigos de recuperación", ""), ("Codici di recupero", ""), ("Posteo recovery codes", ""),
    ("Backup codes", "Email account"), ("Recovery codes", "TOTP"),                               # sections play no part
]


@pytest.mark.parametrize("label,section", RECOVERY_CODES)
def test_the_one_recovery_code_field_is_picked_named_in_the_approval_and_typed(op, recovery, label, section):
    codes = (label, STORED, section) if section else (label, STORED)
    op.item = item(("Recovery number", "+49 151 23456789"), ("Code (TOTP)", SEED), codes, ("Security code", OTHER))
    out = enter()
    assert out["success"] is True and out["codes_left"] == 1, out
    assert recovery.consent.call_args.args[0] == f"Type one code from '{label}' of 'Synthetic' on {ORIGIN}?"
    assert "wxyz-1111" in typed(recovery)
    assert not any(v in typed(recovery) + json.dumps(out) for v in (OTHER, SEED, PW, "23456789", "wxyz-2222"))
    assert op.calls == ["metadata", "reveal"]


@pytest.mark.parametrize("field", ["password", "one-time password", "username", "notesPlain", "code", "Code (TOTP)"])
def test_a_field_name_from_the_agent_never_reaches_another_field(op, recovery, field):
    op.item = item(("Code (TOTP)", SEED), ("Recovery codes", STORED))
    assert enter(field=field)["success"] is True
    assert "'Recovery codes'" in recovery.consent.call_args.args[0]  # what is read is what the user was shown
    assert "wxyz-1111" in typed(recovery) and SEED not in typed(recovery) and PW not in typed(recovery)


@pytest.mark.parametrize("mark", [{"purpose": "PASSWORD"}, {"purpose": "USERNAME"}, {"type": "OTP"}, {"id": "password"}])
def test_the_logins_own_password_username_or_totp_field_is_never_codes_whatever_its_label(op, recovery, mark):
    op.item = item()
    op.item["fields"] = [{"id": "x1", "type": "CONCEALED", "label": "Recovery codes", "value": OTHER, **mark}]
    out = enter()
    assert out["error_type"] == "recovery_field_not_codes" and "\"Recovery codes\"" in out["next"], out
    recovery.consent.assert_not_called()
    recovery.write.assert_not_called()


def test_several_recovery_code_fields_refuse_until_one_is_named_exactly(op, recovery):
    op.item = item(("Recovery codes", OTHER), ("Backup codes (old)", STORED))
    for field in ("", "backup", "Backup codes", "recovery", "codes"):  # no prefix, no guess
        out = enter(field=field)
        assert out["error_type"] == "several_recovery_fields", (field, out)
        assert "'Recovery codes'" in out["error"] and "'Backup codes (old)'" in out["error"]
    recovery.consent.assert_not_called()
    assert "reveal" not in op.calls
    assert enter(field="backup CODES (old)")["success"] is True
    assert "'Backup codes (old)'" in recovery.consent.call_args.args[0]
    assert "wxyz-1111" in typed(recovery) and OTHER not in typed(recovery)


def test_two_fields_with_the_same_label_refuse(op, recovery):
    op.item = item(("Recovery codes", OTHER), ("Recovery codes", STORED, "Old"))
    assert enter(field="Recovery codes")["error_type"] == "several_recovery_fields"
    recovery.consent.assert_not_called()


def _renamed(label):
    def edit(shown):
        shown["fields"][-1]["label"] = label
    return edit


def _moved_to_a_new_field(shown):
    shown["fields"][-1].update(label="Security code")
    shown["fields"].append({"id": "f9", "type": "CONCEALED", "label": "Recovery codes", "value": OTHER})


@pytest.mark.parametrize("edit", [
    lambda shown: shown.update(version=8),                        # any edit at all bumps the item version
    _renamed("Code (TOTP)"),                                      # the approved field id now says something else
    _renamed("Backup codes"),                                     # ... even another recovery-code label
    _moved_to_a_new_field,                                        # the approved label now sits on another field id
    lambda shown: shown["fields"].pop(),                          # the field is gone
    lambda shown: shown["fields"].append(dict(shown["fields"][-1])),  # two fields answer to the approved id
    lambda shown: shown.pop("version"),                           # no version to compare with the prompt's
])
def test_an_item_edited_between_the_approval_and_the_read_types_nothing(op, recovery, edit):
    op.item = item(("Recovery codes", OTHER))
    op.after_approval = item(("Recovery codes", OTHER))
    edit(op.after_approval)
    out = enter(field="recovery codes")
    assert out["error_type"] == "recovery_item_changed", out
    assert recovery.consent.call_count == 1 and op.calls == ["metadata", "reveal"]
    recovery.write.assert_not_called()
    assert OTHER not in json.dumps(out)


def test_an_item_without_any_revision_is_never_read(op, recovery):
    op.item = item(("Recovery codes", OTHER))
    del op.item["version"], op.item["updated_at"]
    assert enter()["error_type"] == "recovery_item_changed"
    recovery.write.assert_not_called()


@pytest.mark.parametrize("failure", [
    RuntimeError("op timed out"),                       # run_cli gave up on a slow op
    "[ERROR] dial tcp 1.2.3.4:443: i/o timeout",        # op's own network failure
    "[ERROR] Too many requests. rate limit exceeded",
    "[ERROR] something op never said before",
    b"<html>proxy error</html>",                        # exit 0 without an item
    b"[]",
])
def test_a_failed_slow_or_timed_out_metadata_read_refuses_and_reveals_nothing(op, recovery, failure):
    op.item = item(("Recovery codes", OTHER))
    op.metadata_failure = failure
    out = json.loads(vault.typed_page_op(lambda: json.dumps(enter(field="recovery codes"))))
    assert out["success"] is False, out
    assert op.calls == ["metadata"]  # no fallback to a read the approval does not bind
    recovery.consent.assert_not_called()
    recovery.write.assert_not_called()


def test_a_failed_read_after_the_approval_types_nothing(op, recovery):
    op.item = item(("Recovery codes", OTHER))
    op.reveal_failure = RuntimeError("op timed out")
    out = json.loads(vault.typed_page_op(lambda: json.dumps(enter(field="recovery codes"))))
    assert out["success"] is False and out["error_type"] == "vault_network", out
    recovery.write.assert_not_called()


def test_the_read_before_the_approval_keeps_no_value_and_a_decline_reveals_nothing(op, recovery):
    op.item = item(("Recovery codes", OTHER))  # FakeOp prints the value even without --reveal
    recovery.consent.side_effect = None
    recovery.consent.return_value = "decline"
    backend = vault._saved_login("op:v1:i1")[0]
    assert backend.recovery_fields("op:v1:i1") == [(rc.RecoveryField("f0", "Recovery codes",
                                                    "op.version=7;op.updated_at=2026-11-23T10:00:00Z"), "")]
    out = enter()
    assert out["error_type"] == "recovery_code_declined" and "reveal" not in op.calls
    assert OTHER not in json.dumps(out)


@pytest.fixture
def local(recovery, monkeypatch):
    """The local vault backend behind the tool, over a stub store holding one record."""
    record = {"secret": {"username": "danil", "password": PW, "otp_secret": SEED, "security_code": OTHER,
                         "code": OTHER, "recovery_codes": STORED}, "revision": "local=r1"}
    store = SimpleNamespace(resolve_secret=lambda handle: dict(record["secret"]),
                            resolve_secret_with_revision=lambda handle: (dict(record["secret"]), record["revision"]))
    monkeypatch.setattr(local_mod, "_store", lambda: store)
    backend = local_mod.LocalLoginBackend.__new__(local_mod.LocalLoginBackend)
    monkeypatch.setattr(vault, "_saved_login", lambda handle: (backend, recovery.meta))
    return record


@pytest.mark.parametrize("field", ["", "password", "otp_secret", "otp secret", "username", "code", "security_code"])
def test_the_local_vault_reads_only_its_recovery_codes_key(local, recovery, field):
    assert enter(field=field)["success"] is True
    assert "'recovery_codes'" in recovery.consent.call_args.args[0]
    assert "wxyz-1111" in typed(recovery)
    assert not any(v in typed(recovery) for v in (PW, SEED, OTHER))


def test_the_local_vault_without_a_recovery_codes_key_or_edited_after_the_approval_refuses(local, recovery):
    recovery.consent.side_effect = lambda *a, **kw: local.update(revision="local=r2") or "accept"
    assert enter()["error_type"] == "recovery_item_changed"
    del local["secret"]["recovery_codes"]
    assert enter()["error_type"] == "recovery_field_not_codes"
    recovery.write.assert_not_called()


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
    assert path.stat().st_mode & 0o777 == 0o600 and "aaaa-1111" not in path.read_text()


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
