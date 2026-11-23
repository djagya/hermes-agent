"""Stored recovery / backup codes (owner decision D-0365, 2026-11-18): ``browser_vault enter_code`` with
``code_source=recovery`` types ONE stored code per human approval.

The backend picks the field, not the agent (J-0141, 2026-11-23): a field is a candidate only when it is not the
login's password, username or one-time-password field and its LABEL says both recovery/backup and code
(:func:`is_recovery_code_label`, six languages). Exactly one candidate is used; the approval shows its exact
label, and after the approval only that field is read, at the same item revision (:func:`bound_codes`). The
value is split into codes here, server-side only. Each code is single-use on the site, so a small ledger
remembers which codes Hermes already typed: only a keyed hash (HMAC-SHA256 with a random per-install key kept
in the same mode-600 file) per canonical item id, never a code. A code is marked used BEFORE it is written, so
a write whose outcome is unknown never offers the same code twice. The read-select-save transaction holds a
file lock next to the ledger, so two processes sharing ``HERMES_HOME`` never pick the same code.
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
from typing import Iterable, Iterator, NamedTuple, Optional, Sequence

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
_KEY_RE = re.compile(r"[0-9a-fA-F]{64}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_lock = threading.Lock()
_MAX_ENTRIES = 2000

# The whole label rule, no deny list: a recovery word AND a code word, both in the label itself (lower case,
# accents dropped, anything else a space; one trailing "(...)" is the site's name and says nothing). EN, DE, FR,
# ES, IT, NL. Stems inside one word, so compounds match ("Wiederherstellungscodes", "herstelcodes"); the
# optional space joins "Notfall-Codes" and "Back up codes" but never two unrelated words ("disco de respaldo").
_RECOVERY_WORD_RE = re.compile(
    r"recover|back ?up|wiederherstell|sicherungs ?code|notfall ?code|ersatz ?code|recuper|secours|sauvegarde|"
    r"respaldo|riserva|emergen|herstel|reserve ?code")
_CODE_WORD_RE = re.compile(r"cod(?:e|igo|ic[ei])")
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")
_MAX_LABEL = 80


class RecoveryField(NamedTuple):
    """One recovery-code field of an item as an approval binds it: the backend's field id, its exact label and
    the item's revision at that read. Never a value."""
    id: str
    label: str
    revision: str


class RecoveryLedgerUnreadable(Exception):
    """The spent-code ledger exists but cannot be parsed: refusing beats re-offering a spent code."""


def is_recovery_code_label(label: str) -> bool:
    """True when the field LABEL says recovery/backup codes ("Recovery codes (PyPI)", "Notfallcodes",
    "Códigos de respaldo"). "Code (TOTP)", "Security code" and "Codice fiscale" say no recovery word, so they
    never are. The approval shows the label as it is, so it must be one short printable line."""
    label = str(label or "")
    if len(label) > _MAX_LABEL or not label.isprintable():
        return False
    folded = unicodedata.normalize("NFKD", _TRAILING_PAREN_RE.sub("", label)).encode("ascii", "ignore").decode("ascii")
    words = re.sub(r"[^a-z0-9]+", " ", folded.lower())
    return bool(_RECOVERY_WORD_RE.search(words) and _CODE_WORD_RE.search(words))


def choose_field(field: str, candidates: Sequence[RecoveryField]) -> Optional[RecoveryField]:
    """The one field to read: the item's only candidate, or among several the one whose label equals the agent's
    ``field`` exactly (case-insensitive). ``None``: no candidate, or several and no single exact match."""
    if len(candidates) > 1:
        want = str(field or "").strip().casefold()
        candidates = [c for c in candidates if c.label.strip().casefold() == want]
    return candidates[0] if len(candidates) == 1 else None


def bound_codes(chosen: RecoveryField, fields: Iterable[tuple[RecoveryField, str]]) -> Optional[list[str]]:
    """After the approval: the codes of exactly the approved field, from the item's revealed candidates. ``None``
    when the item no longer has that field id under that label at that revision (edited since the prompt, or no
    revision to compare): nothing else is ever read instead."""
    from agent.vault_store import revision_verdict

    values = [value for found, value in fields
              if (found.id, found.label) == (chosen.id, chosen.label)
              and revision_verdict(chosen.revision, found.revision) == "match"]
    return parse_codes(values) if len(values) == 1 else None


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
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        raise RecoveryLedgerUnreadable() from None
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise RecoveryLedgerUnreadable() from None
    key = data.get("key") if isinstance(data, dict) else None
    used = data.get("used") if isinstance(data, dict) else None
    if (not isinstance(key, str) or not _KEY_RE.fullmatch(key) or not isinstance(used, list)
            or not all(isinstance(entry, str) and _DIGEST_RE.fullmatch(entry) for entry in used)):
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
