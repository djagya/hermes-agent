"""Stored recovery / backup codes (owner decision D-0365, 2026-11-18): ``browser_vault enter_code`` with
``code_source=recovery`` types ONE stored code per human approval.

A login item keeps its codes in a field the agent names (``recovery codes``); the value is split into codes
here, server-side only. Each code is single-use on the site, so a small ledger remembers which codes Hermes
already typed: only a keyed hash (HMAC-SHA256 with a random per-install key kept in the same mode-600 file)
per handle, never a code. A code is marked used BEFORE it is written, so a write whose outcome is unknown
never offers the same code twice.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Iterable, Optional

_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{3,63}$")
_SPLIT_RE = re.compile(r"[\s,;]+")
_lock = threading.Lock()
_MAX_ENTRIES = 2000


def normalize_label(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def parse_codes(values: Iterable[str]) -> list[str]:
    """Codes in stored order from one or more field values: split on whitespace, commas and semicolons; keep
    4-64 character tokens of letters, digits and inner hyphens (list numbering like ``1.`` is dropped)."""
    out: list[str] = []
    for value in values:
        for token in _SPLIT_RE.split(str(value or "")):
            if _CODE_RE.match(token) and token not in out:
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


def _ledger_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "vault" / "recovery_codes_used.json"


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _digest(key: str, handle: str, code: str) -> str:
    return hmac.new(bytes.fromhex(key), f"{handle}\0{code}".encode(), hashlib.sha256).hexdigest()


def take_unused(handle: str, codes: list[str]) -> tuple[Optional[str], int]:
    """``(code, left_after)``: the first stored code Hermes has not typed for ``handle``, marked used now, and
    how many unused codes remain after it. ``(None, 0)`` when every code was used."""
    with _lock:
        path = _ledger_path()
        data = _load(path)
        key = data.get("key") if isinstance(data.get("key"), str) else ""
        if len(key) != 64:
            key = secrets.token_hex(32)
            data = {"key": key, "used": []}
        used = data.get("used") if isinstance(data.get("used"), list) else []
        seen = set(used)
        fresh = [c for c in codes if _digest(key, handle, c) not in seen]
        if not fresh:
            return None, 0
        used.append(_digest(key, handle, fresh[0]))
        data["used"] = used[-_MAX_ENTRIES:]
        _save(path, data)
        return fresh[0], len(fresh) - 1
