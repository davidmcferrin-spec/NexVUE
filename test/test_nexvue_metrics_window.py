#!/usr/bin/env python3
"""
Endpoint tests for nexvue-metrics.php window clamp, series bucketing,
input-edge collapse, and host-uptime metadata.

Requires `php` on PATH with the sqlite3 extension. Skipped automatically
when php/sqlite is unavailable (e.g. Windows laptop without PHP).

Run: python3 test/test_nexvue_metrics_window.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
METRICS_PHP = ROOT / "web-node" / "nexvue-metrics.php"
PHP = shutil.which("php")


def _php_has_sqlite() -> bool:
    if not PHP:
        return False
    r = subprocess.run(
        [PHP, "-r", "exit(extension_loaded('sqlite3') ? 0 : 1);"],
        capture_output=True,
        timeout=10,
    )
    return r.returncode == 0


@unittest.skipUnless(
    PHP and METRICS_PHP.is_file() and _php_has_sqlite(),
    "php CLI with sqlite3, or nexvue-metrics.php, missing",
)
class TestMetricsWindow(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.db = Path(self._td.name) / "metrics.db"
        self.code_file = Path(self._td.name) / "http_code.txt"
        self.now = 1_700_000_000
        self.data_start = self.now - 2 * 86400
        self.data_end = self.now - 60
        self._seed()

    def tearDown(self) -> None:
        self._td.cleanup()

    def _seed(self) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute(
            """
            CREATE TABLE totals (
                ts INTEGER NOT NULL PRIMARY KEY,
                active_streams INTEGER,
                total_readers INTEGER,
                total_bandwidth_bps REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE host_samples (
                ts INTEGER NOT NULL PRIMARY KEY,
                cpu_pct REAL,
                mem_used_bytes INTEGER,
                mem_total_bytes INTEGER,
                load1 REAL,
                gpu_video_pct REAL,
                gpu_render_pct REAL,
                gpu_video_enhance_pct REAL,
                gpu_freq_mhz REAL,
                cpu_temp_c REAL,
                gpu_temp_c REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE input_status (
                ts INTEGER NOT NULL,
                device_index INTEGER NOT NULL,
                card_name TEXT,
                input_locked INTEGER,
                input_mode TEXT,
                reference_locked INTEGER,
                reference_mode TEXT
            )
            """
        )
        # Two days of 15s totals/host samples — enough that a 7d request
        # both clamps and (over a long requested span) buckets.
        rows = []
        host_rows = []
        ts = self.data_start
        while ts <= self.data_end:
            rows.append((ts, 2, 3, 5_000_000.0))
            host_rows.append(
                (ts, 10.0, 1_000, 8_000, 0.5, 20.0, None, None, 300.0, 45.0, 40.0)
            )
            ts += 15
        conn.executemany("INSERT INTO totals VALUES (?,?,?,?)", rows)
        conn.executemany(
            "INSERT INTO host_samples VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            host_rows,
        )

        # Device 0: 200 identical locked samples, then unlock, then lock.
        # Collapse must keep first / last-of-run / change edges, not 200+.
        base = self.data_start + 3600
        locked = [
            (base + i * 15, 0, "Quad", 1, "1080i59.94", 1, "1080i59.94")
            for i in range(200)
        ]
        unlocked = (base + 200 * 15, 0, "Quad", 0, "", 0, "")
        relock = (base + 201 * 15, 0, "Quad", 1, "1080i59.94", 1, "1080i59.94")
        conn.executemany(
            "INSERT INTO input_status VALUES (?,?,?,?,?,?,?)",
            locked + [unlocked, relock],
        )
        conn.commit()
        conn.close()
        self.sample_count = len(rows)

    def _get(self, params: dict) -> tuple[int, dict]:
        q = dict(params)
        get_php = ",\n".join(
            f"{json.dumps(k)} => {json.dumps(v)}" for k, v in q.items()
        )
        php_path = METRICS_PHP.as_posix()
        db_path = self.db.as_posix()
        code_path = self.code_file.as_posix()
        if self.code_file.exists():
            self.code_file.unlink()

        runner = Path(self._td.name) / "run_metrics.php"
        runner.write_text(
            f"""<?php
declare(strict_types=1);
$_GET = [{get_php}];
putenv('NEXVUE_METRICS_DB={db_path}');
putenv('NEXVUE_AUTH_BYPASS=1');
putenv('NEXVUE_METRICS_TZ=UTC');
$codeFile = '{code_path}';
register_shutdown_function(static function () use ($codeFile): void {{
    $code = http_response_code();
    if ($code === false || $code === 0) {{
        $code = 200;
    }}
    file_put_contents($codeFile, (string)$code);
}});
require '{php_path}';
""",
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["NEXVUE_METRICS_DB"] = str(self.db)
        env["NEXVUE_METRICS_TZ"] = "UTC"
        r = subprocess.run(
            [PHP, "-d", "display_errors=stderr", str(runner)],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        raw = (r.stdout or "").strip()
        if raw.startswith("Content-Type:"):
            parts = raw.split("\n\n", 1)
            raw = parts[1].strip() if len(parts) > 1 else raw

        http_code = 200
        if self.code_file.is_file():
            http_code = int(self.code_file.read_text(encoding="utf-8").strip() or "200")
        elif r.returncode != 0:
            self.fail(f"php failed ({r.returncode}): {r.stderr or r.stdout}")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self.fail(f"expected JSON, got: {raw!r}\nstderr={r.stderr!r}")
        return http_code, data

    def test_7d_clamps_without_500(self) -> None:
        code, data = self._get({
            "view": "totals",
            "from": str(self.now - 7 * 86400),
            "to": str(self.now),
        })
        self.assertEqual(code, 200, data.get("error"))
        self.assertTrue(data["truncated_past"])
        self.assertEqual(data["requested_from"], self.now - 7 * 86400)
        self.assertEqual(data["from"], self.data_start)
        self.assertEqual(data["to"], self.data_end)
        self.assertEqual(data["data_start"], self.data_start)
        self.assertEqual(data["data_end"], self.data_end)
        self.assertGreater(len(data["totals"]), 0)
        self.assertLessEqual(len(data["totals"]), 1500)
        self.assertIn("host_uptime_s", data)
        self.assertIn("host_boot_ts", data)

    def test_future_custom_to_flags_truncated_future(self) -> None:
        code, data = self._get({
            "view": "totals",
            "from": str(self.data_start),
            "to": str(self.data_end + 3600),
        })
        self.assertEqual(code, 200, data.get("error"))
        self.assertFalse(data["truncated_past"])
        self.assertTrue(data["truncated_future"])
        self.assertEqual(data["to"], self.data_end)

    def test_no_overlap_still_200_empty(self) -> None:
        # Entirely before stored samples.
        code, data = self._get({
            "view": "totals",
            "from": str(self.data_start - 10 * 86400),
            "to": str(self.data_start - 9 * 86400),
        })
        self.assertEqual(code, 200, data.get("error"))
        self.assertTrue(data["truncated_past"])
        self.assertEqual(data["totals"], [])

    def test_short_window_not_bucketed(self) -> None:
        code, data = self._get({
            "view": "totals",
            "from": str(self.data_end - 900),
            "to": str(self.data_end),
        })
        self.assertEqual(code, 200, data.get("error"))
        self.assertEqual(data.get("stride_s"), 0)
        self.assertGreater(len(data["totals"]), 10)
        self.assertLess(len(data["totals"]), 80)

    def test_inputs_collapse_same_state_run(self) -> None:
        code, data = self._get({
            "view": "inputs",
            "from": str(self.data_start),
            "to": str(self.data_end),
        })
        self.assertEqual(code, 200, data.get("error"))
        rows = [r for r in data["inputs"] if int(r["device_index"]) == 0]
        self.assertGreaterEqual(len(rows), 3)
        self.assertLess(len(rows), 20)
        locks = [int(r["input_locked"]) for r in rows]
        self.assertIn(0, locks)
        self.assertIn(1, locks)

    def test_host_series_and_uptime_keys(self) -> None:
        code, data = self._get({
            "view": "host",
            "from": str(self.now - 7 * 86400),
            "to": str(self.now),
        })
        self.assertEqual(code, 200, data.get("error"))
        self.assertTrue(data["truncated_past"])
        self.assertGreater(len(data["host"]), 0)
        self.assertLessEqual(len(data["host"]), 1500)
        up = data["host_uptime_s"]
        self.assertTrue(up is None or isinstance(up, (int, float)))


class TestMetricsWindowEmptyDb(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.db = Path(self._td.name) / "metrics.db"
        self.code_file = Path(self._td.name) / "http_code.txt"
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE totals (ts INTEGER, active_streams INTEGER, "
            "total_readers INTEGER, total_bandwidth_bps REAL)"
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self._td.cleanup()

    @unittest.skipUnless(
        PHP and METRICS_PHP.is_file() and _php_has_sqlite(),
        "php CLI with sqlite3, or nexvue-metrics.php, missing",
    )
    def test_empty_db_200(self) -> None:
        now = int(time.time())
        q = {
            "view": "totals",
            "from": str(now - 86400),
            "to": str(now),
        }
        get_php = ",\n".join(
            f"{json.dumps(k)} => {json.dumps(v)}" for k, v in q.items()
        )
        runner = Path(self._td.name) / "run.php"
        runner.write_text(
            f"""<?php
declare(strict_types=1);
$_GET = [{get_php}];
putenv('NEXVUE_METRICS_DB={self.db.as_posix()}');
putenv('NEXVUE_AUTH_BYPASS=1');
$codeFile = '{self.code_file.as_posix()}';
register_shutdown_function(static function () use ($codeFile): void {{
    file_put_contents($codeFile, (string)(http_response_code() ?: 200));
}});
require '{METRICS_PHP.as_posix()}';
""",
            encoding="utf-8",
        )
        r = subprocess.run(
            [PHP, "-d", "display_errors=stderr", str(runner)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        raw = (r.stdout or "").strip()
        if raw.startswith("Content-Type:"):
            raw = raw.split("\n\n", 1)[-1].strip()
        code = 200
        if self.code_file.is_file():
            code = int(self.code_file.read_text(encoding="utf-8").strip() or "200")
        data = json.loads(raw)
        self.assertEqual(code, 200, data)
        self.assertEqual(data["totals"], [])
        self.assertFalse(data["truncated_past"])
        self.assertFalse(data["truncated_future"])


if __name__ == "__main__":
    unittest.main()
