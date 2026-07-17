#!/usr/bin/env python3
"""
Unit tests for nexvue-metrics-server.py — DB schema, retention pruning, and
bandwidth-delta math. Pure Python/SQLite; no network or live services needed.

Run: python3 test/test_nexvue_metrics.py
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Import the script as a module directly from its file path — it's a
# standalone deployed script, not a pip package, so no installed import path.
SPEC_PATH = Path(__file__).resolve().parent.parent / "nexvue-metrics-server.py"
spec = importlib.util.spec_from_file_location("nexvue_metrics_server", SPEC_PATH)
nms = importlib.util.module_from_spec(spec)
sys.modules["nexvue_metrics_server"] = nms
spec.loader.exec_module(nms)


class TestBandwidthMath(unittest.TestCase):
    def test_normal_delta(self):
        # 1,000,000 bytes over 10s = 800,000 bits/sec
        bps = nms._compute_bandwidth_bps(0, 0, 10, 1_000_000)
        self.assertAlmostEqual(bps, 800_000.0)

    def test_zero_interval_returns_zero(self):
        self.assertEqual(nms._compute_bandwidth_bps(10, 100, 10, 200), 0.0)

    def test_negative_interval_returns_zero(self):
        self.assertEqual(nms._compute_bandwidth_bps(10, 100, 5, 200), 0.0)

    def test_counter_reset_returns_zero(self):
        # e.g. a stream restarted and MediaMTX's cumulative counter reset
        self.assertEqual(nms._compute_bandwidth_bps(0, 5000, 10, 100), 0.0)

    def test_flat_counter_returns_zero(self):
        # no bytes moved in the interval — a legitimate zero, not an error
        self.assertEqual(nms._compute_bandwidth_bps(0, 1000, 10, 1000), 0.0)


class TestSchemaAndRetention(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test-metrics.db")
        nms.DB_PATH = self.db_path
        nms.init_db()

    def test_tables_created(self):
        with sqlite3.connect(self.db_path) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"samples", "totals", "input_status"} <= tables)

    def test_init_db_is_idempotent(self):
        nms.init_db()  # must not raise on repeated calls
        nms.init_db()

    def test_prune_removes_old_rows_keeps_recent(self):
        now = int(time.time())
        old_ts = now - int(nms.RETENTION_DAYS * 86400) - 3600  # older than retention
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO samples (ts, channel, bandwidth_bps, readers, ready) "
                "VALUES (?, 'ch0', 1000, 1, 1)", (old_ts,)
            )
            conn.execute(
                "INSERT INTO samples (ts, channel, bandwidth_bps, readers, ready) "
                "VALUES (?, 'ch0', 1000, 1, 1)", (now,)
            )
            conn.execute(
                "INSERT INTO input_status (ts, device_index, card_name, input_locked, "
                "input_mode, reference_locked, reference_mode) "
                "VALUES (?, 0, 'test', 1, '1080i59.94', 0, 'unknown')", (old_ts,)
            )
            conn.commit()

        nms.prune_old_samples()

        with sqlite3.connect(self.db_path) as conn:
            sample_rows = list(conn.execute("SELECT ts FROM samples"))
            status_rows = list(conn.execute("SELECT ts FROM input_status"))
        self.assertEqual(len(sample_rows), 1, "old sample should be pruned, recent one kept")
        self.assertEqual(sample_rows[0][0], now)
        self.assertEqual(len(status_rows), 0, "old input_status row should be pruned")

    def test_totals_table_supports_since_filtering(self):
        # query_history() itself moved to PHP (nexvue-metrics.php) since PHP
        # now reads this DB directly — this test just confirms the schema
        # still supports the same since-filter query PHP performs.
        now = int(time.time())
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO totals (ts, active_streams, total_readers, total_bandwidth_bps) "
                "VALUES (?, 2, 3, 5000000)", (now - 100,)
            )
            conn.execute(
                "INSERT INTO totals (ts, active_streams, total_readers, total_bandwidth_bps) "
                "VALUES (?, 2, 3, 5000000)", (now,)
            )
            conn.commit()
            rows = list(conn.execute("SELECT ts FROM totals WHERE ts >= ?", (now - 10,)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], now)

    def test_user_column_exists_after_init(self):
        # Added via the safe ALTER TABLE migration in init_db() — confirm it
        # exists and is queryable, not just that init_db() didn't crash.
        with sqlite3.connect(self.db_path) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(viewer_sessions)")]
        self.assertIn("user", cols)

    def test_init_db_migration_idempotent_on_existing_user_column(self):
        # Simulates re-running init_db() against a database that ALREADY has
        # the `user` column (e.g. daemon restart after the migration already
        # applied once) — the ALTER TABLE must not raise on the second run.
        nms.init_db()
        nms.init_db()
        with sqlite3.connect(self.db_path) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(viewer_sessions)")]
        self.assertEqual(cols.count("user"), 1, "column should not be duplicated")


class TestViewerSessions(unittest.TestCase):
    """Covers the upsert logic that makes viewer_sessions a per-session
    lifecycle table rather than one row per poll — first_seen must survive
    repeated polls of the same still-connected session, only last_seen and
    bytes_sent should move."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test-metrics.db")
        nms.DB_PATH = self.db_path
        nms.init_db()

    def _upsert(self, session_id, remote_addr, channel, ua, ts, bytes_sent):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO viewer_sessions "
                "(session_id, remote_addr, channel, user_agent, first_seen, last_seen, bytes_sent) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "last_seen=excluded.last_seen, bytes_sent=excluded.bytes_sent",
                (session_id, remote_addr, channel, ua, ts, ts, bytes_sent),
            )
            conn.commit()

    def test_first_seen_preserved_across_repeated_polls(self):
        self._upsert("sess-1", "10.0.0.5:51234", "ch0", "Chrome/1.0", 1000, 5000)
        self._upsert("sess-1", "10.0.0.5:51234", "ch0", "Chrome/1.0", 1015, 12000)
        self._upsert("sess-1", "10.0.0.5:51234", "ch0", "Chrome/1.0", 1030, 19000)

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT first_seen, last_seen, bytes_sent FROM viewer_sessions WHERE session_id='sess-1'"
            ).fetchone()
        self.assertEqual(row[0], 1000, "first_seen must stay at the ORIGINAL poll time")
        self.assertEqual(row[1], 1030, "last_seen must advance to the MOST RECENT poll time")
        self.assertEqual(row[2], 19000, "bytes_sent must reflect the latest counter value")

    def test_distinct_sessions_do_not_collide(self):
        self._upsert("sess-a", "10.0.0.5:1", "ch0", "ua", 1000, 100)
        self._upsert("sess-b", "10.0.0.6:2", "ch1", "ua", 1000, 200)
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM viewer_sessions").fetchone()[0]
        self.assertEqual(count, 2)

    def test_user_field_captured_and_updated_on_upsert(self):
        # Matches the real upsert in poll_once(), which includes `user` (from
        # MediaMTX's WebRTCSession.user — blank until Phase 2 auth exists,
        # but the column and upsert path need to work correctly regardless).
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO viewer_sessions "
                "(session_id, remote_addr, channel, user_agent, first_seen, last_seen, bytes_sent, user) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "last_seen=excluded.last_seen, bytes_sent=excluded.bytes_sent, user=excluded.user",
                ("sess-auth", "10.0.0.9:1", "ch0", "ua", 1000, 1000, 100, ""),
            )
            # Simulate a later poll where the viewer has since authenticated —
            # user should update just like last_seen/bytes_sent do.
            conn.execute(
                "INSERT INTO viewer_sessions "
                "(session_id, remote_addr, channel, user_agent, first_seen, last_seen, bytes_sent, user) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "last_seen=excluded.last_seen, bytes_sent=excluded.bytes_sent, user=excluded.user",
                ("sess-auth", "10.0.0.9:1", "ch0", "ua", 1010, 1010, 200, "jsmith"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT first_seen, user FROM viewer_sessions WHERE session_id='sess-auth'"
            ).fetchone()
        self.assertEqual(row[0], 1000, "first_seen still preserved with user column present")
        self.assertEqual(row[1], "jsmith", "user should update like any other mutable field")

    def test_upserted_timestamps_support_the_active_session_query(self):
        # duration_s / active flag computation now lives in nexvue-metrics.php
        # (tested there directly against a real SQLite file) since PHP reads
        # this DB now, not Python. This test just confirms the underlying
        # first_seen/last_seen data the PHP query depends on is correct.
        now = int(time.time())
        self._upsert("sess-old", "10.0.0.5:1", "ch0", "ua", now - 300, 1000)  # never polled again, now stale
        self._upsert("sess-recent", "10.0.0.6:2", "ch1", "ua", now - 60, 4000)
        self._upsert("sess-recent", "10.0.0.6:2", "ch1", "ua", now - 1, 5000)   # polled again just now

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = {r["session_id"]: dict(r) for r in conn.execute(
                "SELECT session_id, first_seen, last_seen FROM viewer_sessions"
            )}
        self.assertEqual(rows["sess-recent"]["last_seen"] - rows["sess-recent"]["first_seen"], 59)
        self.assertEqual(rows["sess-old"]["first_seen"], rows["sess-old"]["last_seen"], "never re-polled")

    def test_pruning_removes_stale_viewer_sessions(self):
        now = int(time.time())
        old_ts = now - int(nms.RETENTION_DAYS * 86400) - 3600
        self._upsert("sess-ancient", "10.0.0.5:1", "ch0", "ua", old_ts, 100)
        self._upsert("sess-current", "10.0.0.6:2", "ch1", "ua", now, 200)

        nms.prune_old_samples()

        with sqlite3.connect(self.db_path) as conn:
            remaining = [r[0] for r in conn.execute("SELECT session_id FROM viewer_sessions")]
        self.assertEqual(remaining, ["sess-current"])


if __name__ == "__main__":
    unittest.main()
