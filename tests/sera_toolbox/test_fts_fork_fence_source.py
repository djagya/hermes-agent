"""Source-level FTS fence + 3600s display-index lease. No docker."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(*parts: str) -> str:
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


class FtsForkFenceSource(unittest.TestCase):
    def test_rebuild_fts_never_issues_live_rebuild(self) -> None:
        text = _read("hermes_state_search.py")
        start = text.find("def rebuild_fts")
        self.assertGreater(start, 0)
        end = text.find("\ndef ", start + 1)
        body = text[start:end]
        self.assertNotIn("VALUES('rebuild')", body)
        self.assertIn("return 0", body)

    def test_rebuild_fts_once_returns_false(self) -> None:
        text = _read("gateway/session_transcript.py")
        start = text.find("def _rebuild_fts_once")
        self.assertGreater(start, 0)
        end = text.find("\n    def ", start + 1)
        body = text[start:end]
        self.assertIn("return False", body)
        self.assertNotIn("VALUES('rebuild')", body)

    def test_retry_deferred_fts_recovery_returns_false_before_recover(self) -> None:
        text = _read("hermes_state_schema.py")
        self.assertIn('logger.warning("Skipped deferred FTS recovery (fork FTS fence).")', text)
        start = text.find("def retry_deferred_fts_recovery")
        next_def = text.find("\n    def ", start + 1)
        body = text[start:next_def]
        self.assertIn("return False", body)
        self.assertNotIn("BEGIN IMMEDIATE", body)

    def test_trigram_hold_returns_before_drop(self) -> None:
        text = _read("hermes_state_schema.py")
        self.assertIn("Holding trigram cron-exclusion migration (fork FTS fence); no DROP VIEW.", text)
        start = text.find("def _migrate_trigram_cron_exclusion")
        next_def = text.find("\n    def ", start + 1)
        body = text[start:next_def]
        self.assertIn("return False", body)
        self.assertNotIn("cursor.execute", body)

    def test_deferred_index_lease_is_3600(self) -> None:
        watchdog = _read("hermes_startup_watchdog.py")
        self.assertIn("_MAX_LEASE_S = 3600.0", watchdog)
        schema = _read("hermes_state_schema.py")
        start = schema.find("def _init_schema")
        self.assertGreater(start, 0)
        body = schema[start : schema.find("\n    def ", start + 1)]
        self.assertIn("DEFERRED_INDEX_SQL", body)
        self.assertIn('report_startup_progress(3600.0, phase="state_db_init_schema")', body)
        before, _after = body.split("DEFERRED_INDEX_SQL", 1)
        self.assertIn("report_startup_progress(3600.0", before)


if __name__ == "__main__":
    unittest.main()
