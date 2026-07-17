#!/usr/bin/env python3
"""
Unit tests for nexvue-metrics-server.py — DB schema, retention pruning, and
bandwidth-delta math. Pure Python/SQLite; no network or live services needed.

Run: python3 test/test_nexvue_metrics.py
"""
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

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


class TestHostSampling(unittest.TestCase):
    """CPU/mem/load parsers — fixture strings only, no live /proc required."""

    STAT_IDLE = (
        "cpu  100 0 50 850 0 0 0 0 0 0\n"
        "cpu0 50 0 25 425 0 0 0 0 0 0\n"
    )
    STAT_BUSY = (
        "cpu  200 0 100 900 0 0 0 0 0 0\n"
        "cpu0 100 0 50 450 0 0 0 0 0 0\n"
    )
    MEMINFO = (
        "MemTotal:       1000000 kB\n"
        "MemFree:         200000 kB\n"
        "MemAvailable:    400000 kB\n"
        "Buffers:          10000 kB\n"
    )

    def test_parse_proc_stat_cpu(self):
        idle, total = nms._parse_proc_stat_cpu(self.STAT_IDLE)
        self.assertEqual(idle, 850)
        self.assertEqual(total, 100 + 0 + 50 + 850)

    def test_compute_cpu_pct(self):
        i0, t0 = nms._parse_proc_stat_cpu(self.STAT_IDLE)
        i1, t1 = nms._parse_proc_stat_cpu(self.STAT_BUSY)
        pct = nms._compute_cpu_pct(i0, t0, i1, t1)
        # delta total = 200, delta idle = 50 → busy 150 → 75%
        self.assertAlmostEqual(pct, 75.0)

    def test_compute_cpu_pct_zero_delta(self):
        self.assertEqual(nms._compute_cpu_pct(10, 100, 10, 100), 0.0)

    def test_parse_meminfo(self):
        used, total = nms._parse_meminfo(self.MEMINFO)
        self.assertEqual(total, 1000000 * 1024)
        self.assertEqual(used, (1000000 - 400000) * 1024)

    def test_parse_loadavg(self):
        self.assertAlmostEqual(nms._parse_loadavg("1.25 0.80 0.50 1/200 1234\n"), 1.25)


class TestIntelGpuTopParse(unittest.TestCase):
    SAMPLE = """
{
  "period": {"duration": 100.0, "unit": "ms"},
  "frequency": {"requested": 1300.0, "actual": 1200.0},
  "engines": {
    "Render/3D/0": {"busy": 5.5, "sema": 0.0, "wait": 0.0},
    "Blitter/0": {"busy": 0.0, "sema": 0.0, "wait": 0.0},
    "Video/0": {"busy": 42.0, "sema": 0.0, "wait": 0.0},
    "Video/1": {"busy": 18.0, "sema": 0.0, "wait": 0.0},
    "VideoEnhance/0": {"busy": 7.25, "sema": 0.0, "wait": 0.0}
  }
}
"""

    def test_parse_engines_and_freq(self):
        got = nms._parse_intel_gpu_top_json(self.SAMPLE)
        self.assertAlmostEqual(got["gpu_video_pct"], 42.0)  # max of Video/0, Video/1
        self.assertAlmostEqual(got["gpu_render_pct"], 5.5)
        self.assertAlmostEqual(got["gpu_video_enhance_pct"], 7.25)
        self.assertAlmostEqual(got["gpu_freq_mhz"], 1200.0)

    def test_parse_ndjson_first_object(self):
        nd = self.SAMPLE.strip() + "\n" + self.SAMPLE.strip()
        got = nms._parse_intel_gpu_top_json(nd)
        self.assertAlmostEqual(got["gpu_video_pct"], 42.0)

    def test_parse_array_wrapper(self):
        wrapped = "[" + self.SAMPLE.strip() + "]"
        got = nms._parse_intel_gpu_top_json(wrapped)
        self.assertAlmostEqual(got["gpu_render_pct"], 5.5)

    def test_classify_engine(self):
        self.assertEqual(nms._classify_gpu_engine("Video/0"), "video")
        self.assertEqual(nms._classify_gpu_engine("VideoEnhance/0"), "enhance")
        self.assertEqual(nms._classify_gpu_engine("Render/3D/0"), "render")
        self.assertIsNone(nms._classify_gpu_engine("Blitter/0"))


class TestConsumeStreamObjects(unittest.TestCase):
    """Incremental parser for the never-ending intel_gpu_top -J stream."""

    OBJ = '{"engines": {"Video/0": {"busy": 10.0}}}'

    def test_bare_concatenated_objects(self):
        objs, rest = nms._consume_stream_objects(self.OBJ + "\n" + self.OBJ + "\n")
        self.assertEqual(len(objs), 2)
        self.assertEqual(rest, "")

    def test_array_wrapped_stream_never_closes(self):
        # Newer intel-gpu-tools emit "[\n{...},\n{...}" and never close the array.
        objs, rest = nms._consume_stream_objects("[\n" + self.OBJ + ",\n" + self.OBJ)
        self.assertEqual(len(objs), 2)
        self.assertEqual(rest, "")

    def test_partial_tail_is_preserved_for_next_read(self):
        partial = '{"engines": {"Video/0": {"bu'
        objs, rest = nms._consume_stream_objects(self.OBJ + "\n" + partial)
        self.assertEqual(len(objs), 1)
        self.assertEqual(rest, partial)
        # ...and completing the tail on the next chunk yields the object.
        objs2, rest2 = nms._consume_stream_objects(rest + 'sy": 5.0}}}')
        self.assertEqual(len(objs2), 1)
        self.assertEqual(rest2, "")

    def test_empty_and_whitespace_only(self):
        self.assertEqual(nms._consume_stream_objects(""), ([], ""))
        self.assertEqual(nms._consume_stream_objects("[\n \n"), ([], ""))


class _FakePopen:
    """Stand-in for the intel_gpu_top child: canned stdout, empty stderr."""

    def __init__(self, stdout_bytes: bytes, returncode=None):
        import io
        self.stdout = io.BytesIO(stdout_bytes)
        self.stderr = io.BytesIO(b"")
        self.returncode = returncode

    def poll(self):
        return self.returncode


class TestGpuStream(unittest.TestCase):
    """Persistent-stream sampling — the fix for intel_gpu_top's pipe
    block-buffering, which made the old kill-after-timeout one-shot come
    back with empty stdout (and empty iGPU charts) on real hardware."""

    def setUp(self):
        self._prev_warn = nms._gpu_warn_last
        nms._gpu_warn_last = 0.0

    def tearDown(self):
        nms._gpu_warn_last = self._prev_warn

    def _wait_for_sample(self, stream, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with stream._lock:
                if stream._latest is not None:
                    return
            time.sleep(0.01)

    def test_latest_returns_newest_parsed_sample(self):
        fake = _FakePopen(TestIntelGpuTopParse.SAMPLE.strip().encode())
        stream = nms._GpuStream()
        with mock.patch.object(subprocess, "Popen", return_value=fake):
            got = stream.latest()  # spawns child + reader threads
            self._wait_for_sample(stream)
            got = stream.latest()
        self.assertAlmostEqual(got["gpu_video_pct"], 42.0)
        self.assertAlmostEqual(got["gpu_render_pct"], 5.5)
        self.assertAlmostEqual(got["gpu_freq_mhz"], 1200.0)

    def test_stale_sample_returns_none(self):
        stream = nms._GpuStream()
        sample = {"gpu_video_pct": 1.0, "gpu_render_pct": None,
                  "gpu_video_enhance_pct": None, "gpu_freq_mhz": None}
        with stream._lock:
            stream._latest = (time.monotonic() - nms._GPU_STALE_AFTER_S - 1, sample)
        with mock.patch.object(stream, "_ensure_running"):
            self.assertIsNone(stream.latest())

    def test_metricless_objects_never_clobber_a_real_sample(self):
        # A header/period-only record after a real sample must not wipe it.
        real = TestIntelGpuTopParse.SAMPLE.strip()
        header = '{"period": {"duration": 100.0, "unit": "ms"}}'
        fake = _FakePopen((real + "\n" + header + "\n").encode())
        stream = nms._GpuStream()
        with mock.patch.object(subprocess, "Popen", return_value=fake):
            stream.latest()
            self._wait_for_sample(stream)
            got = stream.latest()
        self.assertAlmostEqual(got["gpu_video_pct"], 42.0)

    def test_missing_binary_returns_none_and_backs_off(self):
        stream = nms._GpuStream()
        with mock.patch.object(subprocess, "Popen",
                               side_effect=FileNotFoundError) as popen:
            self.assertIsNone(stream.latest())
            self.assertIsNone(stream.latest())  # inside backoff window
        self.assertEqual(popen.call_count, 1, "restart backoff must apply")

    def test_sample_gpu_empty_when_no_stream_data(self):
        with mock.patch.object(nms._gpu_stream, "latest", return_value=None):
            got = nms.sample_gpu()
        self.assertIsNone(got["gpu_video_pct"])
        self.assertIsNone(got["gpu_render_pct"])
        self.assertIsNone(got["gpu_video_enhance_pct"])
        self.assertIsNone(got["gpu_freq_mhz"])

    def test_sample_gpu_passes_through_stream_sample(self):
        sample = {"gpu_video_pct": 33.0, "gpu_render_pct": 2.0,
                  "gpu_video_enhance_pct": 0.0, "gpu_freq_mhz": 900.0}
        with mock.patch.object(nms._gpu_stream, "latest", return_value=sample):
            self.assertEqual(nms.sample_gpu(), sample)


class TestSchemaAndRetention(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test-metrics.db")
        nms.DB_PATH = self.db_path
        nms.init_db()

    def test_host_samples_table_exists(self):
        with sqlite3.connect(self.db_path) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(host_samples)")]
        self.assertEqual(
            cols,
            [
                "ts", "cpu_pct", "mem_used_bytes", "mem_total_bytes", "load1",
                "gpu_video_pct", "gpu_render_pct", "gpu_video_enhance_pct", "gpu_freq_mhz",
            ],
        )

    def test_pruning_removes_old_host_samples(self):
        now = int(time.time())
        old_ts = now - int(nms.RETENTION_DAYS * 86400) - 3600
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO host_samples (ts, cpu_pct, mem_used_bytes, mem_total_bytes, load1) "
                "VALUES (?,?,?,?,?)",
                (old_ts, 10.0, 100, 1000, 0.5),
            )
            conn.execute(
                "INSERT INTO host_samples (ts, cpu_pct, mem_used_bytes, mem_total_bytes, load1) "
                "VALUES (?,?,?,?,?)",
                (now, 20.0, 200, 1000, 1.0),
            )
            conn.commit()
        nms.prune_old_samples()
        with sqlite3.connect(self.db_path) as conn:
            rows = list(conn.execute("SELECT ts FROM host_samples"))
        self.assertEqual(rows, [(now,)])

    def test_tables_created(self):
        with sqlite3.connect(self.db_path) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"samples", "totals", "input_status", "host_samples"} <= tables)

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


class TestStatusUrlCandidates(unittest.TestCase):
    """HTTP-then-HTTPS discovery for the optional-TLS status daemon."""

    def setUp(self):
        self._prev_status_env = nms._STATUS_URL_ENV

    def tearDown(self):
        nms._STATUS_URL_ENV = self._prev_status_env

    def test_unset_tries_http_then_https(self):
        nms._STATUS_URL_ENV = ""
        self.assertEqual(
            nms._status_base_urls(),
            ["http://127.0.0.1:9998", "https://127.0.0.1:9998"],
        )

    def test_env_override_is_single_url(self):
        nms._STATUS_URL_ENV = "https://127.0.0.1:9998/"
        self.assertEqual(nms._status_base_urls(), ["https://127.0.0.1:9998"])

    def test_fetch_json_first_falls_through_on_scheme_error(self):
        calls = []

        class FakeResp:
            def __init__(self, body):
                self._body = body.encode()

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(url, timeout=None, context=None):
            calls.append(url)
            if url.startswith("https://"):
                raise urllib.error.URLError("SSL: WRONG_VERSION_NUMBER")
            return FakeResp('{"devices":[],"stale":false}')

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            data = nms._fetch_json_first([
                "https://127.0.0.1:9998/status",
                "http://127.0.0.1:9998/status",
            ])

        self.assertEqual(calls, [
            "https://127.0.0.1:9998/status",
            "http://127.0.0.1:9998/status",
        ])
        self.assertEqual(data, {"devices": [], "stale": False})

    def test_fetch_json_first_raises_last_error(self):
        def always_fail(url, timeout=None, context=None):
            raise urllib.error.URLError(f"fail:{url}")

        with mock.patch.object(urllib.request, "urlopen", side_effect=always_fail):
            with self.assertRaises(urllib.error.URLError) as ctx:
                nms._fetch_json_first([
                    "http://127.0.0.1:9998/status",
                    "https://127.0.0.1:9998/status",
                ])
        self.assertIn("https://127.0.0.1:9998/status", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
