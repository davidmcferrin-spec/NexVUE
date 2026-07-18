#!/usr/bin/env python3
"""
Endpoint tests for nexvue-metrics.php view=viewers column filters.

Requires `php` on PATH with the sqlite3 extension. Skipped automatically when
php/sqlite is unavailable (e.g. Windows laptop without PHP).

Run: python3 test/test_nexvue_metrics_viewers.py
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
METRICS_PHP = ROOT / "nexvue-metrics.php"
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
class TestMetricsViewersFilters(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.db = Path(self._td.name) / "metrics.db"
        self.code_file = Path(self._td.name) / "http_code.txt"
        self.now = int(time.time())
        self._seed()

    def tearDown(self) -> None:
        self._td.cleanup()

    def _seed(self) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute(
            """
            CREATE TABLE viewer_sessions (
                session_id TEXT PRIMARY KEY,
                remote_addr TEXT,
                channel TEXT,
                user TEXT,
                user_agent TEXT,
                first_seen REAL,
                last_seen REAL,
                bytes_sent INTEGER
            )
            """
        )
        rows = [
            (
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "203.0.113.10:54321",
                "ch0",
                "",
                "Mozilla/5.0 Chrome/120",
                self.now - 700,  # duration 695s (>=10m)
                self.now - 5,
                50_000_000,
            ),
            (
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "198.51.100.20:4000",
                "ch1",
                "alice",
                "NexVUE Player",
                self.now - 7200,
                self.now - 120,
                1_500_000_000,
            ),
            (
                "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "203.0.113.99:9",
                "ch0lo",
                "",
                "curl/8.0",
                self.now - 30,
                self.now - 10,
                500_000,
            ),
            (
                "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "10.0.0.5:1111",
                "ch2",
                "",
                "Mozilla/5.0 Firefox/115",
                self.now - 3600,
                self.now - 300,
                2_000_000,
            ),
        ]
        conn.executemany(
            "INSERT INTO viewer_sessions VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        conn.close()

    def _get(self, params: dict) -> tuple[int, dict]:
        """Invoke nexvue-metrics.php with $_GET; return (http_code, json)."""
        q = dict(params)
        q.setdefault("view", "viewers")
        q.setdefault("from", str(self.now - 86400))
        q.setdefault("to", str(self.now))

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
        r = subprocess.run(
            [PHP, "-d", "display_errors=stderr", str(runner)],
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )
        raw = (r.stdout or "").strip()
        # Strip accidental header lines if any CLI SAPI emits them.
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

    def _sids(self, data: dict) -> set[str]:
        return {s["session_id"] for s in data["sessions"]}

    def test_no_filters_returns_all(self) -> None:
        code, data = self._get({})
        self.assertEqual(code, 200)
        self.assertEqual(data["session_total"], 4)
        self.assertEqual(data["session_count"], 4)
        self.assertEqual(len(data["sessions"]), 4)
        self.assertEqual(data["filters"], {})

    def test_status_live(self) -> None:
        code, data = self._get({"filter_status": "live"})
        self.assertEqual(code, 200)
        self.assertEqual(self._sids(data), {
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "cccccccc-cccc-cccc-cccc-cccccccccccc",
        })
        self.assertEqual(data["session_total"], 4)
        self.assertEqual(data["session_count"], 2)

    def test_status_ended_regex(self) -> None:
        code, data = self._get({"filter_status": "/^end/i"})
        self.assertEqual(code, 200)
        self.assertEqual(self._sids(data), {
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "dddddddd-dddd-dddd-dddd-dddddddddddd",
        })

    def test_invalid_regex(self) -> None:
        code, data = self._get({"filter_ip": "/(/"})
        self.assertEqual(code, 400)
        self.assertIn("regex", data["error"].lower())

    def test_ip_substring(self) -> None:
        code, data = self._get({"filter_ip": "203.0.113"})
        self.assertEqual(code, 200)
        self.assertEqual(self._sids(data), {
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "cccccccc-cccc-cccc-cccc-cccccccccccc",
        })

    def test_channel_regex_and_exact_selector(self) -> None:
        code, data = self._get({"channel": "ch0", "filter_channel": "/^ch0$/"})
        self.assertEqual(code, 200)
        self.assertEqual(data["channel_filter"], "ch0")
        self.assertEqual(self._sids(data), {
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        })

    def test_channel_text_matches_lo(self) -> None:
        code, data = self._get({"filter_channel": "ch0"})
        self.assertEqual(code, 200)
        self.assertEqual(self._sids(data), {
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "cccccccc-cccc-cccc-cccc-cccccccccccc",
        })

    def test_duration_gte_10m(self) -> None:
        code, data = self._get({"filter_duration": ">=10m"})
        self.assertEqual(code, 200)
        self.assertEqual(self._sids(data), {
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "dddddddd-dddd-dddd-dddd-dddddddddddd",
        })

    def test_duration_lt_1m(self) -> None:
        code, data = self._get({"filter_duration": "<1m"})
        self.assertEqual(code, 200)
        self.assertEqual(self._sids(data), {
            "cccccccc-cccc-cccc-cccc-cccccccccccc",
        })

    def test_duration_text_display(self) -> None:
        code, data = self._get({"filter_duration": "20s"})
        self.assertEqual(code, 200)
        self.assertEqual(self._sids(data), {
            "cccccccc-cccc-cccc-cccc-cccccccccccc",
        })

    def test_data_gt_500mb(self) -> None:
        code, data = self._get({"filter_data": ">500MB"})
        self.assertEqual(code, 200)
        self.assertEqual(self._sids(data), {
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        })

    def test_data_lte_1mb(self) -> None:
        code, data = self._get({"filter_data": "<=1MB"})
        self.assertEqual(code, 200)
        self.assertEqual(self._sids(data), {
            "cccccccc-cccc-cccc-cccc-cccccccccccc",
        })

    def test_client_regex(self) -> None:
        code, data = self._get({"filter_client": "/chrome/i"})
        self.assertEqual(code, 200)
        self.assertEqual(self._sids(data), {
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        })

    def test_combined_filters(self) -> None:
        code, data = self._get({
            "filter_status": "ended",
            "filter_channel": "ch",
            "filter_duration": ">=30m",
            "filter_client": "Firefox",
        })
        self.assertEqual(code, 200)
        self.assertEqual(self._sids(data), {
            "dddddddd-dddd-dddd-dddd-dddddddddddd",
        })
        self.assertEqual(data["filters"]["status"], "ended")
        self.assertEqual(data["filters"]["duration"], ">=30m")

    def test_bad_duration_unit(self) -> None:
        code, data = self._get({"filter_duration": ">5days"})
        self.assertEqual(code, 400)
        self.assertIn("unit", data["error"].lower())

    def test_overlong_filter(self) -> None:
        code, data = self._get({"filter_ip": "x" * 200})
        self.assertEqual(code, 400)
        self.assertIn("at most", data["error"].lower())


if __name__ == "__main__":
    unittest.main()
