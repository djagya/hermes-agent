"""Schema-version holdback vs upstream expectations under the fork FTS fence.

The fork fences live FTS ``'rebuild'`` (``_rebuild_fts_indexes`` is a no-op and
``_migrate_trigram_cron_exclusion`` returns False to hold ``schema_version``
below 30 — see ``tests/sera_toolbox/test_fts_fork_fence_source.py`` and
``tests/state/test_fts_fork_fence.py``). Upstream tests written when the
trigram migration could complete therefore fail here for exactly one reason:
their final ``schema_version == SCHEMA_VERSION`` assertion, or a follow-on
assertion that depends on the migration having run.

These tests are the fork's reconciliation: each names the upstream test it
mirrors, reproduces the upstream scenario, and asserts the FORK contract —
the structural repair still happens (column re-added, sessions usable) while
``schema_version`` legitimately stays held back for retry on a runtime where
the fence is lifted. That is the property production relies on: an upgrade
path that is deferred, never destructive, and never silently claimed current.
"""
from __future__ import annotations

import sqlite3

from hermes_state import SessionDB
from hermes_state_common import SCHEMA_VERSION


def _demote_to_v25_with_column_dropped(path) -> None:
    """Reproduce the upstream scenario: a legacy v25 db missing the column."""
    SessionDB(db_path=path).close()
    conn = sqlite3.connect(path)
    try:
        conn.execute("ALTER TABLE sessions DROP COLUMN git_metadata_generation")
        conn.execute("UPDATE schema_version SET version = 25")
        conn.commit()
    finally:
        conn.close()


def _read_version(path) -> int:
    verify = sqlite3.connect(path)
    try:
        return verify.execute("SELECT version FROM schema_version").fetchone()[0]
    finally:
        verify.close()


def test_legacy_sessions_table_recovers_column_while_version_holds(tmp_path):
    """Upstream mirror: tests/state/test_session_git_metadata_generation.py::
    test_legacy_sessions_table_reconciles_generation_column.

    The reconciler must re-add ``git_metadata_generation`` on reopen; the
    version stamp may (and on this fork does) stay held back by the FTS fence.
    Sessions must remain fully usable at the held-back version."""
    path = tmp_path / "state.db"
    _demote_to_v25_with_column_dropped(path)

    reopened = SessionDB(db_path=path)
    try:
        verify = sqlite3.connect(path)
        try:
            columns = {
                row[1]
                for row in verify.execute("PRAGMA table_info('sessions')")
            }
        finally:
            verify.close()
        assert "git_metadata_generation" in columns
        # Fork contract: the trigram migration is fenced, so the version stamp
        # is deferred — but never regressed and never silently claimed current.
        assert _read_version(path) < SCHEMA_VERSION
        reopened.create_session("session", "desktop", cwd="/repo")
        assert reopened.update_session_cwd("session", "/repo") == 1
    finally:
        reopened.close()


def test_version_advance_resumes_when_fence_lifts(tmp_path, monkeypatch):
    """The version stamp is gated on the trigram migration result: with the
    fence lifted (migration returns True) a demoted db advances to
    SCHEMA_VERSION on reopen. Pins the gate so the fork holdback cannot
    silently become a permanent regression."""
    import hermes_state_schema as schema_mod

    path = tmp_path / "state.db"
    _demote_to_v25_with_column_dropped(path)
    monkeypatch.setattr(
        schema_mod.SessionSchemaMixin,
        "_migrate_trigram_cron_exclusion",
        lambda self, cursor: True,
    )
    reopened = SessionDB(db_path=path)
    try:
        assert _read_version(path) == SCHEMA_VERSION
    finally:
        reopened.close()
