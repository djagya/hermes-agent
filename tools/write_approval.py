#!/usr/bin/env python3
"""Write-approval gate + pending store for memory and skill writes.

Background
----------
The agent writes to two persistent stores that survive across sessions:

  * **memory** — MEMORY.md / USER.md, small (~200 char) declarative entries
  * **skills** — SKILL.md + supporting files, potentially huge (10-100 KB)

Both stores are written from two origins:

  * **foreground** — a normal agent turn (user is present / chatting)
  * **background_review** — the self-improvement review fork that runs after a
    turn and autonomously decides what to save (the source of the
    "wrong assumptions" users complained about)

This module lets the user gate those writes per-subsystem with a boolean
``write_approval``:

  * ``false`` (default) — write freely (the pre-gate behaviour)
  * ``true``            — require approval: do not commit the write; either
    prompt inline (memory, interactive CLI only) or **stage** it to a pending
    store and surface it for the user to approve or reject out-of-band

The size asymmetry between memory and skills is real and unavoidable: a memory
entry can be reviewed inline in a chat bubble; a 100 KB SKILL.md cannot. So
the gate stages BOTH to disk, but review affordances differ by subsystem
(see ``hermes_cli`` slash handlers): memory shows full content, skills show
metadata + a one-line gist + a ``diff`` escape hatch (CLI/dashboard/file).

Staging is mandatory for background-origin writes (a daemon thread cannot
block on an interactive prompt) and for gateway sessions (no inline prompt
channel — review happens via ``/memory pending``). Foreground CLI memory
writes prompt inline via the dangerous-command approval callback; skill
writes always stage (too big to eyeball mid-loop).

Pending records live under ``<HERMES_HOME>/pending/{memory,skills}/<id>.json``
so they survive process restarts and can be reviewed from CLI, gateway, or the
web dashboard.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None

try:  # Windows
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX
    _msvcrt = None

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# Subsystem identifiers
MEMORY = "memory"
SKILLS = "skills"
_SUBSYSTEMS = (MEMORY, SKILLS)

# Config key (per subsystem). A single boolean: the approval gate is OFF by
# default (writes flow freely, the pre-gate behaviour), and ON means stage /
# prompt every write for the user's approval. There is intentionally no third
# "block all writes" state — to disable a subsystem entirely use its own
# enable flag (e.g. ``memory.memory_enabled: false``).
CONFIG_KEY = "write_approval"
PENDING_SCHEMA_VERSION = 2


class PendingWriteError(RuntimeError):
    """A gated write could not be durably staged or verified."""


_pending_apply_capability: ContextVar[dict[str, Any] | None] = ContextVar(
    "pending_apply_capability", default=None
)


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

def write_approval_enabled(subsystem: str) -> bool:
    """Return whether the approval gate is enabled for ``subsystem``.

    An explicit missing key still defaults to ``False`` for backwards
    compatibility. Invalid subsystem names, unreadable config, and malformed
    values fail closed so a write is staged instead of silently bypassing the
    approval boundary.
    """
    if subsystem not in _SUBSYSTEMS:
        return True
    try:
        from hermes_cli.config import load_config, cfg_get
        cfg = load_config()
        raw = cfg_get(cfg, subsystem, CONFIG_KEY, default=False)
    except Exception:
        logger.exception("Cannot resolve %s.write_approval; failing closed", subsystem)
        return True
    return _normalize_enabled(raw)


def _normalize_enabled(value: Any) -> bool:
    """Coerce a config value to bool; malformed values fail closed."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"on", "true", "yes", "1", "approve", "enabled"}:
            return True
        if normalized in {"off", "false", "no", "0", "disabled"}:
            return False
    return True


# ---------------------------------------------------------------------------
# Pending store (file-backed)
# ---------------------------------------------------------------------------

def _pending_dir(subsystem: str) -> Path:
    return get_hermes_home() / "pending" / subsystem


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _chmod_fd_private(fd: int) -> None:
    if hasattr(os, "fchmod"):
        os.fchmod(fd, 0o600)


def _lock_fd(fd: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        return
    if _msvcrt is not None:  # pragma: no cover - Windows
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
            os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        getattr(_msvcrt, "locking")(fd, getattr(_msvcrt, "LK_LOCK"), 1)
        return
    raise PendingWriteError("no supported cross-process pending-store lock")


def _unlock_fd(fd: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
    elif _msvcrt is not None:  # pragma: no cover - Windows
        os.lseek(fd, 0, os.SEEK_SET)
        getattr(_msvcrt, "locking")(fd, getattr(_msvcrt, "LK_UNLCK"), 1)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        if os.name == "nt":  # Windows does not expose POSIX directory handles.
            return
        raise
    try:
        try:
            os.fsync(fd)
        except OSError:
            if os.name != "nt":
                raise
    finally:
        os.close(fd)


@contextmanager
def _pending_lock(subsystem: str):
    d = _pending_dir(subsystem)
    _ensure_private_dir(d.parent)
    _ensure_private_dir(d)
    lock_path = d / ".pending.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        _chmod_fd_private(fd)
        _lock_fd(fd)
        locked = True
        yield d
    finally:
        if locked:
            _unlock_fd(fd)
        os.close(fd)


def _canonical_payload(payload: Dict[str, Any]) -> bytes:
    """Stable bytes for semantic dedupe, including provenance/audit context."""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _payload_sha256(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_payload(payload)).hexdigest()


def consume_pending_apply_capability(subsystem: str, payload: Dict[str, Any]) -> bool:
    """Consume the one-shot capability installed by ``apply_pending_record``."""
    capability = _pending_apply_capability.get()
    if not isinstance(capability, dict) or capability.get("consumed"):
        return False
    if capability.get("subsystem") != subsystem:
        return False
    if capability.get("payload_sha256") != _payload_sha256(payload):
        return False
    capability["consumed"] = True
    return True


def _load_pending_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _validated_pending_record(
    record: Optional[Dict[str, Any]],
    subsystem: str,
    pending_id: str,
    *,
    require_current_schema: bool = False,
) -> Dict[str, Any]:
    """Validate identity and payload binding for one pending record."""
    if not isinstance(record, dict):
        raise PendingWriteError("pending record is not an object")
    if record.get("id") != pending_id or record.get("subsystem") != subsystem:
        raise PendingWriteError("pending record identity mismatch")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise PendingWriteError("pending record payload is not an object")
    version = record.get("schema_version", 1)
    if version not in {1, PENDING_SCHEMA_VERSION}:
        raise PendingWriteError(f"unsupported pending record schema: {version!r}")
    if require_current_schema and version != PENDING_SCHEMA_VERSION:
        raise PendingWriteError("pending record must be restaged under the current schema")
    digest = record.get("payload_sha256")
    actual = _payload_sha256(payload)
    if version == PENDING_SCHEMA_VERSION:
        if not isinstance(digest, str) or digest != actual:
            raise PendingWriteError("pending record payload hash mismatch")
    elif digest and digest != actual:
        raise PendingWriteError("legacy pending record payload hash mismatch")
    state = record.get("state", "pending")
    if state not in {"pending", "applying"}:
        raise PendingWriteError(f"unsupported pending record state: {state!r}")
    if state == "applying":
        started = record.get("applying_started_at")
        if isinstance(started, bool) or not isinstance(started, (int, float)):
            raise PendingWriteError("applying pending record lacks transition evidence")
    return record


def _atomic_write_pending_record(
    directory: Path, path: Path, record: Dict[str, Any]
) -> Dict[str, Any]:
    """Durably replace one pending record and verify its exact read-back."""
    data = (
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    tmp = directory / f".{path.stem}.{uuid.uuid4().hex}.tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        _chmod_fd_private(fd)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        _fsync_dir(directory)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    if path.read_bytes() != data:
        raise PendingWriteError(f"pending record read-back mismatch: {path}")
    return _validated_pending_record(
        _load_pending_file(path),
        str(record["subsystem"]),
        str(record["id"]),
        require_current_schema=True,
    )


def stage_write(subsystem: str, payload: Dict[str, Any],
                *, summary: str, origin: str) -> Dict[str, Any]:
    """Durably persist or deduplicate one pending write.

    The call succeeds only after an atomic write and exact read-back. A disk or
    verification failure raises ``PendingWriteError``; callers must never claim
    that a proposal was staged without this receipt.
    """
    if subsystem not in _SUBSYSTEMS:
        raise PendingWriteError(f"unknown pending subsystem: {subsystem}")
    if not isinstance(payload, dict):
        raise PendingWriteError("pending payload must be an object")

    payload_digest = _payload_sha256(payload)
    try:
        with _pending_lock(subsystem) as d:
            for existing_path in sorted(d.glob("*.json")):
                existing = _load_pending_file(existing_path)
                try:
                    existing = _validated_pending_record(
                        existing,
                        subsystem,
                        existing_path.stem,
                        require_current_schema=True,
                    )
                except PendingWriteError:
                    logger.warning(
                        "Ignoring invalid pending record during dedupe: %s", existing_path
                    )
                    continue
                if existing.get("payload_sha256") == payload_digest:
                    os.chmod(existing_path, 0o600)
                    applying = existing.get("state", "pending") == "applying"
                    return {
                        **existing,
                        "deduplicated": True,
                        "persisted": True,
                        "recovery_required": applying,
                    }

            # Allocate under the subsystem lock and never replace an existing
            # record. Full UUID entropy also makes accidental collisions
            # negligible without relying on probability for correctness.
            while True:
                pid = uuid.uuid4().hex
                path = d / f"{pid}.json"
                if not path.exists():
                    break
            record = {
                "schema_version": PENDING_SCHEMA_VERSION,
                "id": pid,
                "subsystem": subsystem,
                "action": payload.get("action", ""),
                "summary": (summary or "").strip(),
                "origin": origin or "foreground",
                "created_at": time.time(),
                "state": "pending",
                "payload": payload,
                "payload_sha256": payload_digest,
                "persisted": True,
            }
            tmp = d / f".{pid}.{uuid.uuid4().hex}.tmp"
            data = (
                json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                _chmod_fd_private(fd)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, path)
                os.chmod(path, 0o600)
                _fsync_dir(d)
            except BaseException:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
                raise

            if path.read_bytes() != data:
                path.unlink(missing_ok=True)
                _fsync_dir(d)
                raise PendingWriteError(f"pending write read-back mismatch: {path}")
            try:
                loaded = _validated_pending_record(
                    _load_pending_file(path),
                    subsystem,
                    pid,
                    require_current_schema=True,
                )
            except PendingWriteError:
                path.unlink(missing_ok=True)
                _fsync_dir(d)
                raise PendingWriteError(f"pending write verification failed: {path}")
            # Mirror the dedupe-path receipt shape so every caller sees the
            # same staging flags without key-existence checks.
            return {**loaded, "deduplicated": False, "recovery_required": False}
    except PendingWriteError:
        raise
    except Exception as exc:
        logger.error("Failed to stage pending %s write: %s", subsystem, exc, exc_info=True)
        raise PendingWriteError(
            f"failed to durably stage pending {subsystem} write: {exc}"
        ) from exc


def list_pending(subsystem: str) -> List[Dict[str, Any]]:
    """Return all verified pending records for ``subsystem``, oldest first."""
    if subsystem not in _SUBSYSTEMS:
        return []
    d = _pending_dir(subsystem)
    if not d.exists():
        return []
    records: List[Dict[str, Any]] = []
    try:
        _ensure_private_dir(d.parent)
        _ensure_private_dir(d)
    except Exception:
        logger.exception("Cannot secure pending directory: %s", d)
        return []
    for p in d.glob("*.json"):
        try:
            os.chmod(p, 0o600)
            record = _validated_pending_record(
                _load_pending_file(p), subsystem, p.stem
            )
            visible = dict(record)
            visible["legacy_schema"] = (
                record.get("schema_version", 1) != PENDING_SCHEMA_VERSION
            )
            records.append(visible)
        except Exception:
            logger.warning("Skipping unreadable or invalid pending record: %s", p)
    records.sort(key=lambda r: r.get("created_at", 0))
    return records


def _valid_pending_id(value: str) -> bool:
    return bool(value) and len(value) <= 64 and all(
        char.isalnum() or char in {"-", "_"} for char in value
    )


def get_pending(subsystem: str, pending_id: str) -> Optional[Dict[str, Any]]:
    """Return one verified pending record by id, or ``None``."""
    if subsystem not in _SUBSYSTEMS or not _valid_pending_id(pending_id):
        return None
    path = _pending_dir(subsystem) / f"{pending_id}.json"
    if not path.exists():
        return None
    try:
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o600)
        record = _validated_pending_record(
            _load_pending_file(path), subsystem, pending_id
        )
        visible = dict(record)
        visible["legacy_schema"] = (
            record.get("schema_version", 1) != PENDING_SCHEMA_VERSION
        )
        return visible
    except Exception:
        return None


def apply_pending_record(subsystem: str, pending_id: str, callback):
    """Durably claim, apply, and consume one live pending record.

    The subsystem lock is held only for state transitions, never while target
    code runs. ``applying`` is fsynced before the callback, so concurrent apply
    attempts fail closed and a crash leaves durable quarantine evidence. The
    callback may then acquire the target's own lock without creating a reverse
    ``pending -> target`` lock order. We re-lock and verify the same claim before
    restoring or consuming it.
    """
    if subsystem not in _SUBSYSTEMS or not _valid_pending_id(pending_id):
        return False, "invalid pending record identity"

    def _verify_live_claim(directory: Path) -> tuple[Path, Dict[str, Any]]:
        path = directory / f"{pending_id}.json"
        if not path.is_file():
            raise PendingWriteError(
                "pending applying record disappeared; target state requires reconciliation"
            )
        current = _validated_pending_record(
            _load_pending_file(path),
            subsystem,
            pending_id,
            require_current_schema=True,
        )
        if current.get("state") != "applying":
            raise PendingWriteError(
                "pending apply claim changed state; target state requires reconciliation"
            )
        if (
            current.get("payload_sha256") != applying_record.get("payload_sha256")
            or current.get("applying_started_at")
            != applying_record.get("applying_started_at")
        ):
            raise PendingWriteError(
                "pending apply claim changed after transition; target state requires reconciliation"
            )
        return path, current

    try:
        # Phase 1: claim under the pending-store lock, then release it before
        # invoking any target code.
        with _pending_lock(subsystem) as d:
            path = d / f"{pending_id}.json"
            if not path.is_file():
                return False, "pending record disappeared before apply"
            record = _validated_pending_record(
                _load_pending_file(path),
                subsystem,
                pending_id,
                require_current_schema=True,
            )
            if record.get("state", "pending") != "pending":
                return False, (
                    "pending record is quarantined in applying state; reconcile "
                    "the target before retrying or discarding it"
                )

            pending_record = dict(record)
            pending_record["state"] = "pending"
            pending_record.pop("applying_started_at", None)
            applying_record = dict(pending_record)
            applying_record["state"] = "applying"
            applying_record["applying_started_at"] = time.time()
            _atomic_write_pending_record(d, path, applying_record)

        # Phase 2: run outside the pending lock. The one-shot capability binds
        # the exact payload and records whether mutation-capable code was
        # entered; only pre-consumption failures are safe to restore to pending.
        capability = {
            "subsystem": subsystem,
            "pending_id": pending_id,
            "payload_sha256": record["payload_sha256"],
            "consumed": False,
        }
        token = _pending_apply_capability.set(capability)
        callback_error: BaseException | None = None
        try:
            ok, message = callback(applying_record)
        except BaseException as exc:
            callback_error = exc
            ok, message = False, str(exc)
        finally:
            _pending_apply_capability.reset(token)

        if callback_error is not None:
            if capability["consumed"]:
                raise PendingWriteError(
                    "target apply failed after consuming its one-shot capability; "
                    "the approval record remains quarantined in applying state"
                ) from callback_error
            with _pending_lock(subsystem) as d:
                path, _ = _verify_live_claim(d)
                _atomic_write_pending_record(d, path, pending_record)
            raise callback_error

        if not ok:
            if capability["consumed"]:
                return False, (
                    "target apply reported failure after consuming its one-shot "
                    "capability; record remains quarantined in applying state: "
                    + str(message)
                )
            with _pending_lock(subsystem) as d:
                path, _ = _verify_live_claim(d)
                _atomic_write_pending_record(d, path, pending_record)
            return False, message

        if not capability["consumed"]:
            with _pending_lock(subsystem) as d:
                path, _ = _verify_live_claim(d)
                _atomic_write_pending_record(d, path, pending_record)
            return False, "apply callback returned success without consuming its capability"

        # Phase 3: target success is acknowledged only by durably consuming the
        # exact applying claim. Failure here deliberately leaves quarantine.
        try:
            with _pending_lock(subsystem) as d:
                path, _ = _verify_live_claim(d)
                path.unlink()
                _fsync_dir(d)
        except BaseException as exc:
            raise PendingWriteError(
                "approved target may already be mutated; approval-record "
                "consumption is not durably confirmed. Any surviving or "
                "reappearing record remains quarantined in applying state "
                f"and must not be replayed automatically: {exc}"
            ) from exc
        return True, message
    except Exception as exc:
        logger.error(
            "Failed to apply pending %s/%s: %s", subsystem, pending_id, exc,
            exc_info=True,
        )
        return False, str(exc)


def discard_pending(subsystem: str, pending_id: str) -> bool:
    """Delete a pending record, but never discard an uncertain apply marker."""
    if subsystem not in _SUBSYSTEMS or not _valid_pending_id(pending_id):
        return False
    try:
        with _pending_lock(subsystem) as d:
            path = d / f"{pending_id}.json"
            if path.exists():
                record = _validated_pending_record(
                    _load_pending_file(path), subsystem, pending_id
                )
                if record.get("state", "pending") != "pending":
                    logger.warning(
                        "Refusing to discard quarantined pending record: %s/%s",
                        subsystem,
                        pending_id,
                    )
                    return False
                path.unlink()
                _fsync_dir(d)
                return True
    except Exception as exc:  # pragma: no cover
        logger.error("Failed to discard pending %s/%s: %s", subsystem, pending_id, exc)
    return False


def discard_pending_if_matches(
    subsystem: str, pending_id: str, payload_sha256: str
) -> tuple[bool, str]:
    """Discard one exact still-pending record under a single locked re-read.

    This is for transaction compensation: it must never erase an applying
    quarantine, a replacement record, or evidence whose payload is unknown.
    """
    if subsystem not in _SUBSYSTEMS or not _valid_pending_id(pending_id):
        return False, "invalid_identity"
    try:
        with _pending_lock(subsystem) as d:
            path = d / f"{pending_id}.json"
            if not path.is_file():
                return False, "missing"
            record = _validated_pending_record(
                _load_pending_file(path),
                subsystem,
                pending_id,
                require_current_schema=True,
            )
            if record.get("state", "pending") != "pending":
                return False, "applying"
            if record.get("payload_sha256") != payload_sha256:
                return False, "payload_mismatch"
            path.unlink()
            _fsync_dir(d)
            return True, "discarded"
    except Exception as exc:
        logger.error(
            "Failed exact discard of pending %s/%s: %s",
            subsystem,
            pending_id,
            exc,
            exc_info=True,
        )
        return False, "verification_failed"


def resolve_applying(
    subsystem: str, pending_id: str, resolution: str
) -> tuple[bool, str]:
    """Resolve one quarantined apply after explicit operator reconciliation.

    ``not-applied`` restores replayability. ``applied`` removes the active
    record only after a private, fsynced tombstone preserves the exact evidence.
    The function never infers target state itself.
    """
    if subsystem not in _SUBSYSTEMS or not _valid_pending_id(pending_id):
        return False, "invalid pending record identity"
    if resolution not in {"applied", "not-applied"}:
        return False, "resolution must be 'applied' or 'not-applied'"
    try:
        with _pending_lock(subsystem) as d:
            path = d / f"{pending_id}.json"
            if not path.is_file():
                return False, "pending record not found"
            record = _validated_pending_record(
                _load_pending_file(path),
                subsystem,
                pending_id,
                require_current_schema=True,
            )
            if record.get("state") != "applying":
                return False, "pending record is not quarantined in applying state"

            if resolution == "not-applied":
                restored = dict(record)
                restored["state"] = "pending"
                restored.pop("applying_started_at", None)
                _atomic_write_pending_record(d, path, restored)
                return True, "quarantine resolved as not applied; record restored to pending"

            archive_dir = d / "resolved"
            _ensure_private_dir(archive_dir)
            archive_path = archive_dir / f"{pending_id}.json"
            tombstone = {
                "resolution_schema_version": 1,
                "id": pending_id,
                "subsystem": subsystem,
                "resolution": "applied",
                "resolved_at": time.time(),
                "record": record,
            }
            data = (
                json.dumps(tombstone, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8")
            if archive_path.exists():
                try:
                    archived = json.loads(archive_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    raise PendingWriteError(
                        "existing quarantine tombstone is unreadable"
                    ) from exc
                if (
                    archived.get("id") != pending_id
                    or archived.get("subsystem") != subsystem
                    or archived.get("resolution") != "applied"
                    or archived.get("record", {}).get("payload_sha256")
                    != record.get("payload_sha256")
                ):
                    raise PendingWriteError(
                        "existing quarantine tombstone does not match active evidence"
                    )
            else:
                tmp = archive_dir / f".{pending_id}.{uuid.uuid4().hex}.tmp"
                fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    _chmod_fd_private(fd)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp, archive_path)
                    os.chmod(archive_path, 0o600)
                    _fsync_dir(archive_dir)
                except BaseException:
                    tmp.unlink(missing_ok=True)
                    raise
                if archive_path.read_bytes() != data:
                    raise PendingWriteError(
                        "quarantine tombstone read-back mismatch"
                    )
            path.unlink()
            _fsync_dir(d)
            return True, "quarantine resolved as applied; evidence archived"
    except Exception as exc:
        logger.error(
            "Failed to resolve pending %s/%s: %s",
            subsystem,
            pending_id,
            exc,
            exc_info=True,
        )
        return False, str(exc)


def pending_count(subsystem: str) -> int:
    """Cheap count of pending records (for notification badges)."""
    d = _pending_dir(subsystem)
    if not d.exists():
        return 0
    try:
        return sum(1 for _ in d.glob("*.json"))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Write origin
# ---------------------------------------------------------------------------

def current_origin() -> str:
    """Return the active write origin: ``foreground`` or ``background_review``.

    Reuses the skill-provenance ContextVar, which the background review fork
    already sets (see ``agent.background_review`` /
    ``AIAgent._spawn_background_review``). Foreground agent turns leave it at
    the default ``foreground``.
    """
    try:
        from tools.skill_provenance import get_current_write_origin
        return get_current_write_origin()
    except Exception:
        return "foreground"


def is_background() -> bool:
    return current_origin() == "background_review"


# ---------------------------------------------------------------------------
# Gate decision
# ---------------------------------------------------------------------------

class GateDecision:
    """Result of evaluating the write gate for a single write attempt.

    Exactly one of the boolean flags is True:
      * ``allow``  — proceed with the real write (gate off, or an inline
        approval was granted).
      * ``blocked`` — refuse the write (the user denied an inline approval
        prompt). ``message`` explains why; surface it to the agent.
      * ``stage``  — do not write; the caller should stage the payload via
        ``stage_write`` (gate on, and no inline prompt is available — gateway,
        background review, script, or any skill write). ``message`` is the
        user-facing "staged for approval" note.
    """

    __slots__ = ("allow", "blocked", "stage", "message")

    def __init__(self, *, allow=False, blocked=False, stage=False, message=""):
        self.allow = allow
        self.blocked = blocked
        self.stage = stage
        self.message = message


def evaluate_gate(subsystem: str, *, inline_summary: str = "",
                  inline_detail: str = "") -> GateDecision:
    """Decide what to do with a pending write for ``subsystem``.

    Args:
        subsystem: ``memory`` or ``skills``.
        inline_summary: short description used as the inline approval prompt
            header (memory foreground path only).
        inline_detail: full content shown in the inline prompt (memory entries
            are small; skills never take the inline path).

    Decision matrix:
        gate off (default)                    → allow (writes flow freely)
        gate on, memory + interactive CLI     → inline approve/deny prompt
        gate on, memory + gateway/script/bg   → stage
        gate on, skills (any origin)          → stage (too big to review inline)

    Note: there is no config-driven "blocked" outcome — the gate only ever
    delays a write for approval, never silently refuses it. ``blocked`` is
    still produced when the user *actively denies* an inline prompt.
    """
    if not write_approval_enabled(subsystem):
        return GateDecision(allow=True)

    background = is_background()

    # Skills always stage — a SKILL.md is too large to review inline, and a
    # background skill write happens in a daemon thread with no user present.
    if subsystem == SKILLS or background:
        where = "/skills pending" if subsystem == SKILLS else "/memory pending"
        return GateDecision(
            stage=True,
            message=(
                f"Staged for approval ({subsystem}.write_approval is on). "
                f"Not yet saved — review with {where}."
            ),
        )

    # Memory + foreground: if an interactive approval channel exists (a CLI
    # approval callback registered on this thread), prompt inline — entries
    # are small enough to show in full. Otherwise (gateway, script, batch,
    # no listener) stage instead of forcing a blind deny.
    if _interactive_approval_available():
        granted = _prompt_inline_memory_approval(inline_summary, inline_detail)
        if granted is True:
            return GateDecision(allow=True)
        if granted is False:
            return GateDecision(
                blocked=True,
                message="Memory write denied by user. The change was not saved.",
            )
        # granted is None → prompt failed; fall through to staging.

    return GateDecision(
        stage=True,
        message=(
            "Staged for approval (memory.write_approval is on). "
            "Not yet saved — review with /memory pending."
        ),
    )


def _interactive_approval_available() -> bool:
    """True when a foreground memory write can be approved inline.

    Inline prompting requires a per-thread approval callback registered by the
    interactive CLI (``tools.terminal_tool.set_approval_callback``). Every
    other surface stages instead:

    * **Gateway/API sessions** — the dangerous-command ``/approve`` round-trip
      lives in the pending-approval queue (``submit_pending`` +
      ``_await_gateway_decision``), which ``prompt_dangerous_approval`` never
      reaches; trying to prompt from a gateway session would hit the
      ``input()`` fallback and silently deny. Staging gives the user a real
      review affordance (``/memory pending``) instead.
    * Scripts, cron, and background threads — no user present.
    """
    try:
        from tools.terminal_tool import _get_approval_callback
        return _get_approval_callback() is not None
    except Exception:
        return False


def _prompt_inline_memory_approval(summary: str, detail: str) -> Optional[bool]:
    """Prompt the user inline to approve a memory write.

    Returns True (approved), False (denied), or None (no interactive prompt
    available / prompt failed → caller should stage instead).

    Reuses the per-thread CLI approval callback registered for dangerous
    commands (``tools.terminal_tool.set_approval_callback``). The callback is
    invoked directly — NOT via ``prompt_dangerous_approval`` — because that
    wrapper falls back to ``input()`` (deadlock-prone under prompt_toolkit,
    see #15216) and converts callback errors into a silent deny; here a
    failed prompt must stage the write instead.
    """
    try:
        from tools.terminal_tool import _get_approval_callback
    except Exception:
        return None

    callback = _get_approval_callback()
    if callback is None:
        # No interactive channel on this thread — stage rather than risk the
        # input() fallback (deadlock under prompt_toolkit, EOF-deny in tests).
        return None

    header = summary.strip() or "Save to memory?"
    body = detail.strip()
    description = f"Save to memory: {header}"
    command = body if body else header
    # Invoke the callback directly instead of via prompt_dangerous_approval:
    # that wrapper swallows callback exceptions into "deny", which would
    # silently refuse the write. Direct invocation lets a crashed prompt fall
    # back to staging (the gate only ever delays a write, never drops it).
    try:
        choice = callback(command, description, allow_permanent=False)
    except Exception as e:
        logger.error("Inline memory approval prompt failed: %s", e)
        return None

    if choice in {"once", "session"}:
        return True
    if choice == "deny":
        return False
    # Any other outcome (e.g. timeout that returns "deny" already handled) →
    # treat unknown as no-decision so we stage rather than silently drop.
    return None


# ---------------------------------------------------------------------------
# Skill-specific helpers (gist + diff for the review affordances)
# ---------------------------------------------------------------------------

def skill_gist(action: str, name: str, *, content: str = "",
               file_path: str = "", old_string: str = "",
               new_string: str = "") -> str:
    """Build a one-line human gist for a pending skill write.

    Heuristic, no model call — the gist surfaces enough to decide approve/reject
    in a chat bubble, while the full diff stays behind /skills diff (CLI/
    dashboard/file). For create/edit it pulls the frontmatter ``description:``;
    for patch/write_file it describes the size of the change.
    """
    if action in {"create", "edit"} and content:
        desc = _frontmatter_description(content)
        size = f"{len(content) // 1024 + 1} KB" if len(content) >= 1024 else f"{len(content)} chars"
        verb = "create" if action == "create" else "rewrite"
        if desc:
            return f"{verb} '{name}' — {desc} ({size})"
        return f"{verb} '{name}' ({size})"
    if action == "patch":
        target = file_path or "SKILL.md"
        removed = old_string.count("\n") + 1 if old_string else 0
        added = new_string.count("\n") + 1 if new_string else 0
        return f"patch '{name}' {target} (+{added}/-{removed} lines)"
    if action == "write_file":
        return f"write {file_path} in '{name}'"
    if action == "remove_file":
        return f"remove {file_path} from '{name}'"
    if action == "delete":
        return f"delete skill '{name}'"
    return f"{action} '{name}'"


def _frontmatter_description(content: str) -> str:
    """Extract the ``description:`` value from SKILL.md YAML frontmatter."""
    import re
    m = re.search(r"^description:\s*(.+)$", content, re.MULTILINE)
    if not m:
        return ""
    desc = m.group(1).strip().strip("'\"")
    return desc[:140]


def skill_pending_diff(record: Dict[str, Any]) -> str:
    """Build a full unified diff (or full content) for a staged skill write.

    Used by /skills diff <id> on a surface that can render it (CLI pager, web
    dashboard, or by opening the pending JSON file). For create this is the new
    file content; for edit/patch it is a unified diff against the current
    on-disk skill.
    """
    import difflib
    payload = record.get("payload", {})
    action = payload.get("action", "")
    name = payload.get("name", "")

    if action == "create":
        return (payload.get("content") or "")

    # Resolve current on-disk content for diffable actions.
    try:
        from tools.skill_manager_tool import _find_skill
    except Exception:
        _find_skill = None  # type: ignore

    current = ""
    target_label = "SKILL.md"
    if _find_skill is not None:
        found = _find_skill(name)
        if found:
            base = found["path"]
            if action == "edit":
                p = base / "SKILL.md"
            elif action in {"patch", "write_file"}:
                rel = payload.get("file_path") or "SKILL.md"
                p = base / rel
                target_label = rel
            else:
                p = base / "SKILL.md"
            try:
                if p.exists():
                    current = p.read_text(encoding="utf-8")
            except Exception:
                current = ""

    if action == "edit":
        new = payload.get("content") or ""
    elif action == "patch":
        old_s = payload.get("old_string") or ""
        new_s = payload.get("new_string") or ""
        new = current.replace(old_s, new_s) if current else f"(patch {old_s!r} → {new_s!r})"
    elif action == "write_file":
        new = payload.get("file_content") or ""
    elif action == "remove_file":
        return f"remove file: {payload.get('file_path')} from skill '{name}'"
    elif action == "delete":
        return f"delete skill '{name}'"
    else:
        return f"({action} on '{name}')"

    diff = difflib.unified_diff(
        current.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{target_label}",
        tofile=f"b/{target_label}",
    )
    text = "".join(diff)
    return text or "(no textual change)"
