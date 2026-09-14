"""Fork FTS fence: no live 'rebuild', display indexes still created."""
from __future__ import annotations

import inspect

from gateway.session_transcript import SessionTranscriptMixin
from hermes_startup_watchdog import _MAX_LEASE_S
from hermes_state import SessionDB
from hermes_state_common import FTS_STALE_KEY
from hermes_state_schema import SessionSchemaMixin

DISPLAY_INDEXES = {
    "idx_messages_display_page",
    "idx_messages_display_backfill",
    "idx_messages_display_identity",
}


def test_rebuild_fts_returns_zero_without_rebuild_sql(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    seen: list[str] = []
    real = db._conn.execute

    def spy(sql, *args, **kwargs):
        seen.append(str(sql))
        return real(sql, *args, **kwargs)

    db._conn.execute = spy
    assert db.rebuild_fts() == 0
    assert not any("VALUES('rebuild')" in sql for sql in seen)
    db.close()


def test_rebuild_fts_once_returns_false():
    class Stub(SessionTranscriptMixin):
        def __init__(self):
            self._fts_rebuild_attempted = False
            self._db = object()

    stub = Stub()
    assert stub._rebuild_fts_once() is False
    assert stub._fts_rebuild_attempted is True
    assert stub._rebuild_fts_once() is False


def test_retry_deferred_fts_recovery_always_false(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db._fts_stale = True
    assert db.retry_deferred_fts_recovery() is False
    db.close()


def test_display_indexes_exist_when_fenced(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    names = {
        row[0]
        for row in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'idx_messages_display%'"
        )
    }
    assert DISPLAY_INDEXES <= names
    db.close()


def test_leftover_stale_stays_detached(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db._conn.execute(
        "INSERT OR REPLACE INTO state_meta (key, value) VALUES (?, ?)",
        (FTS_STALE_KEY, "1"),
    )
    db._conn.commit()
    db.close()
    again = SessionDB(db_path=path)
    assert again._fts_stale is True
    assert again.retry_deferred_fts_recovery() is False
    again.close()


def test_deferred_index_lease_covers_create_index():
    assert _MAX_LEASE_S >= 3600.0
    source = inspect.getsource(SessionSchemaMixin._init_schema)
    assert 'report_startup_progress(3600.0, phase="state_db_init_schema")' in source
    assert "DEFERRED_INDEX_SQL" in source
    before, after = source.split("DEFERRED_INDEX_SQL", 1)
    assert "report_startup_progress(3600.0" in before
