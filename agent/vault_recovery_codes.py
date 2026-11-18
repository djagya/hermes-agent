"""Stored recovery / backup codes (owner decision D-0365, 2026-11-18): ``browser_vault enter_code`` with
``code_source=recovery`` types ONE stored code per human approval.

A login item keeps its codes in a field the agent names (``recovery codes``); the value is split into codes
here, server-side only. Only a field whose label or section says recovery / backup / codes (six languages) is
read, and never the item's password, username or one-time-password field (review E, 11-18: ``field=password``
typed the password into a code box). Each code is single-use on the site, so a small ledger remembers which
codes Hermes already typed: only a keyed hash (HMAC-SHA256 with a random per-install key kept in the same
mode-600 file) per canonical item id, never a code. A code is marked used BEFORE it is written, so a write
whose outcome is unknown never offers the same code twice. The read-select-save transaction holds a file lock
next to the ledger, so two processes sharing ``HERMES_HOME`` never pick the same code.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import unicodedata
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Iterable, Iterator, Optional

msvcrt = None
try:
    import fcntl
except ImportError:  # pragma: no cover - platform-specific fallback (same shape as agent/vault_store.py)
    fcntl = None
    with suppress(ImportError):
        import msvcrt

_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{3,63}$")
_CHUNK_SPLIT_RE = re.compile(r"[,;]+")
_DIGIT_GROUP_RE = re.compile(r"^\d{2,5}$")
_lock = threading.Lock()
_MAX_ENTRIES = 2000

# Recovery / backup wording on the field's label or section (normalised: lower case, accents dropped, words
# split on anything else). EN, DE, FR, ES, IT, NL. Stems, so compounds ("Wiederherstellungscodes",
# "herstelcodes", "backupcodes") match.
_RECOVERY_WORDS_RE = re.compile(
    r"recover|backup|back up|wiederherstell|sicherungscode|notfallcode|recuper|secours|sauvegarde|respaldo|"
    r"herstel|reservecode")
_CODE_WORD_RE = re.compile(r"cod(?:e|es|igo|igos|ice|ici)\b")
# "code" alone is not enough when the text names something else that is secret or not a recovery code.
_NOT_CODES_RE = re.compile(
    r"pass|kennwort|wachtwoord|contrasena|mot de passe|\bpin\b|\botp\b|totp|one time|einmal|seed|secret|"
    r"user ?name|e ?mail|zip|post|bar ?code|promo|gift|coupon|voucher|discount")
# A field label that names the account's own secret or identifier is never read as codes, whatever the section.
_SECRET_LABEL_RE = re.compile(
    r"^(?:password|passwort|kennwort|wachtwoord|contrasena|mot de passe|passcode|pin|username|user name|"
    r"e ?mail|login|one time password|otp|totp|authenticator|secret|seed)(?: (?!cod)\w+)?$")


class RecoveryFieldNotCodes(Exception):
    """The named field exists but is not a recovery-code field (the password, an identifier, a TOTP seed, or a
    label without recovery/backup/code wording). Carries no value."""


class RecoveryLedgerUnreadable(Exception):
    """The spent-code ledger exists but cannot be parsed: refusing beats re-offering a spent code."""


def normalize_label(text: str) -> str:
    folded = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip()


def has_code_wording(text: str) -> bool:
    """True when ``text`` (a field label plus its section) says recovery / backup codes."""
    words = normalize_label(text)
    if _RECOVERY_WORDS_RE.search(words):
        return True
    return bool(_CODE_WORD_RE.search(words)) and not _NOT_CODES_RE.search(words)


def names_account_secret(text: str) -> bool:
    """True when ``text`` names the login's password, identifier or one-time-password field."""
    words = normalize_label(text)
    return bool(_SECRET_LABEL_RE.match(words)) and not _RECOVERY_WORDS_RE.search(words)


def _line_codes(line: str) -> list[str]:
    out: list[str] = []
    for chunk in _CHUNK_SPLIT_RE.split(line):
        tokens = chunk.split()
        if len(tokens) > 1 and all(_DIGIT_GROUP_RE.match(t) for t in tokens):
            # Digit groups ("1234 5678", Google): one code per pair; an odd run is one code.
            step = 2 if len(tokens) % 2 == 0 else len(tokens)
            tokens = ["".join(tokens[i:i + step]) for i in range(0, len(tokens), step)]
        out.extend(tokens)
    return out


def parse_codes(values: Iterable[str]) -> list[str]:
    """Codes in stored order from one or more field values, read line by line: commas and semicolons separate
    codes, whitespace separates codes too except between short digit groups ("1234 5678" is one code). Keeps
    4-64 character tokens of letters, digits and inner hyphens that contain a digit (list numbering like ``1.``
    and words like "GitHub" are dropped)."""
    out: list[str] = []
    for value in values:
        for line in str(value or "").splitlines():
            for token in _line_codes(line):
                if _CODE_RE.match(token) and any(ch.isdigit() for ch in token) and token not in out:
                    out.append(token)
    return out


def field_matches(field: str, label: str, section: str = "") -> bool:
    """The agent-named field: its label equals ``field`` or starts with it ("recovery codes" matches
    "Recovery codes (PyPI)"), or the field sits in a section of that name."""
    want = normalize_label(field)
    if not want:
        return False
    have = normalize_label(label)
    return have == want or have.startswith(want + " ") or normalize_label(section) == want


def pick_code_values(field: str, entries: Iterable[tuple[str, str, str, bool]]) -> list[str]:
    """Codes from the item's fields ``(label, section, value, protected)`` that match ``field``. ``protected``
    is the backend's own mark for the password / username / one-time-password field. ``[]`` when nothing
    matches; :class:`RecoveryFieldNotCodes` when fields match but none of them may be read as codes."""
    matched = [(label, section, value, protected) for label, section, value, protected in entries
               if field_matches(field, label, section)]
    usable = [value for label, section, value, protected in matched
              if not protected and not names_account_secret(label) and has_code_wording(label + " " + section)]
    if matched and not usable:
        raise RecoveryFieldNotCodes()
    return parse_codes(usable)


def _ledger_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "vault" / "recovery_codes_used.json"


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Threads AND processes: an exclusive lock on ``<ledger>.lock`` for the whole read-select-save."""
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + ".lock")
        if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
            lock_path.write_text(" ", encoding="utf-8")  # msvcrt needs a non-empty byte range to lock
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX)
            elif msvcrt:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            yield
        finally:
            with suppress(OSError):
                if fcntl:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                elif msvcrt:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            os.close(fd)


def _load(path: Path) -> Optional[dict]:
    """The ledger, or None when it does not exist yet. An existing ledger that does not parse (or lacks its
    key) raises :class:`RecoveryLedgerUnreadable`: a fresh key would forget every spent code."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        raise RecoveryLedgerUnreadable() from None
    try:
        data = json.loads(text)
    except ValueError:
        raise RecoveryLedgerUnreadable() from None
    key = data.get("key") if isinstance(data, dict) else None
    if not isinstance(key, str) or len(key) != 64 or not isinstance(data.get("used"), list):
        raise RecoveryLedgerUnreadable()
    return data


def _save(path: Path, data: dict) -> None:
    """Unique temp file in the ledger's directory, fsync, atomic replace (mode 600)."""
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def _digest(key: str, item_id: str, code: str) -> str:
    return hmac.new(bytes.fromhex(key), f"{item_id}\0{code}".encode(), hashlib.sha256).hexdigest()


def take_unused(item_id: str, codes: list[str]) -> tuple[Optional[str], int]:
    """``(code, left_after)``: the first stored code Hermes has not typed for the item ``item_id`` (the
    backend's canonical id, ``meta.id``, so handle aliases share one record), marked used now, and how many
    unused codes remain after it. ``(None, 0)`` when every code was used. Raises
    :class:`RecoveryLedgerUnreadable` when the ledger exists but is corrupt."""
    path = _ledger_path()
    with _locked(path):
        data = _load(path) or {"key": secrets.token_hex(32), "used": []}
        key = data["key"]
        used = list(data["used"])
        seen = set(used)
        fresh = [c for c in codes if _digest(key, item_id, c) not in seen]
        if not fresh:
            return None, 0
        used.append(_digest(key, item_id, fresh[0]))
        data["used"] = used[-_MAX_ENTRIES:]
        _save(path, data)
        return fresh[0], len(fresh) - 1
