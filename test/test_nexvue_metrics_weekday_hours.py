#!/usr/bin/env python3
"""
Endpoint tests for nexvue-metrics.php view=weekday_hours.

Verifies dense Mon–Sun × 24 grid and equal-date averaging (missing telemetry
excluded). Requires `php` on PATH with the sqlite3 extension. Skipped
automatically when php/sqlite is unavailable.

Run: python3 test/test_nexvue_metrics_weekday_hours.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
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


def _utc(*parts: int) -> int:
    """Unix seconds for a UTC datetime (Y, M, D, H[, M, S])."""
    y, m, d, h = parts[:4]
    mi = parts[4] if len(parts) > 4 else 0
    s = parts[5] if len(parts) > 5 else 0
    return int(datetime(y, m, d, h, mi, s, tzinfo=timezone.utc).timestamp())


@unittest.skipUnless(
    PHP and METRICS_PHP.is_file() and _php_has_sqlite(),
    "php CLI with sqlite3, or nexvue-metrics.php, missing",
)
class TestMetricsWeekdayHours(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.db = Path(self._td.name) / "metrics.db"
        self.code_file = Path(self._td.name) / "http_code.txt"
        # Fixed UTC week: 2024-01-01 Mon … 2024-01-07 Sun, plus next Mon.
        self.from_ts = _utc(2024, 1, 1, 0)
        self.to_ts = _utc(2024, 1, 8, 23, 59, 59)
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
        # One sample at 14:00 UTC for each ISO weekday in the first week.
        # Bandwidth = weekday * 1000 so weekend cells are distinguishable.
        rows = []
        for day, dow in (
            (1, 1),  # Mon
            (2, 2),  # Tue
            (3, 3),  # Wed
            (4, 4),  # Thu
            (5, 5),  # Fri
            (6, 6),  # Sat
            (7, 7),  # Sun
        ):
            rows.append((_utc(2024, 1, day, 14), 1, dow, float(dow * 1000)))

        # Equal-date weighting on Monday hour 10:
        #   2024-01-01: samples 10 + 20 → day mean 15
        #   2024-01-08: sample 100 → day mean 100
        # equal-date avg = (15+100)/2 = 57.5
        # sample-weighted would be (10+20+100)/3 ≈ 43.333
        rows.extend([
            (_utc(2024, 1, 1, 10, 0), 1, 1, 10.0),
            (_utc(2024, 1, 1, 10, 15), 1, 2, 20.0),
            (_utc(2024, 1, 8, 10, 0), 1, 5, 100.0),
        ])

        conn.executemany(
            "INSERT INTO totals VALUES (?,?,?,?)",
            rows,
        )
        conn.commit()
        conn.close()

    def _get(self, params: dict | None = None) -> tuple[int, dict]:
        q = dict(params or {})
        q.setdefault("view", "weekday_hours")
        q.setdefault("from", str(self.from_ts))
        q.setdefault("to", str(self.to_ts))

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
            timeout=20,
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

    def _cell(self, data: dict, weekday: int, hour: int) -> dict:
        for c in data["weekday_hours"]:
            if c["weekday"] == weekday and c["hour"] == hour:
                return c
        self.fail(f"missing cell weekday={weekday} hour={hour}")

    def test_dense_seven_by_twenty_four_grid(self) -> None:
        code, data = self._get()
        self.assertEqual(code, 200)
        self.assertEqual(data["timezone"], "UTC")
        cells = data["weekday_hours"]
        self.assertEqual(len(cells), 168)
        expected = [(dow, hour) for dow in range(1, 8) for hour in range(24)]
        actual = [(c["weekday"], c["hour"]) for c in cells]
        self.assertEqual(actual, expected)
        for c in cells:
            self.assertIn("date_count", c)
            self.assertIn("sample_count", c)

    def test_weekend_cells_included(self) -> None:
        code, data = self._get()
        self.assertEqual(code, 200)
        sat = self._cell(data, 6, 14)
        sun = self._cell(data, 7, 14)
        self.assertEqual(sat["date_count"], 1)
        self.assertEqual(sun["date_count"], 1)
        self.assertEqual(sat["avg_bandwidth_bps"], 6000.0)
        self.assertEqual(sun["avg_bandwidth_bps"], 7000.0)
        self.assertEqual(sat["avg_readers"], 6.0)
        self.assertEqual(sun["avg_readers"], 7.0)

    def test_equal_date_weighting(self) -> None:
        code, data = self._get()
        self.assertEqual(code, 200)
        mon10 = self._cell(data, 1, 10)
        # (15 + 100) / 2 — not sample-weighted 43.333…
        self.assertAlmostEqual(mon10["avg_bandwidth_bps"], 57.5, places=5)
        self.assertEqual(mon10["date_count"], 2)
        self.assertEqual(mon10["sample_count"], 3)
        self.assertEqual(mon10["peak_bandwidth_bps"], 100.0)
        # Readers: day1 mean (1+2)/2=1.5, day2 mean 5 → (1.5+5)/2 = 3.25
        self.assertAlmostEqual(mon10["avg_readers"], 3.25, places=5)
        self.assertEqual(mon10["peak_readers"], 5)

    def test_empty_cells_zeroed(self) -> None:
        code, data = self._get()
        self.assertEqual(code, 200)
        empty = self._cell(data, 3, 3)  # Wed 03:00 — no samples
        self.assertEqual(empty["date_count"], 0)
        self.assertEqual(empty["sample_count"], 0)
        self.assertEqual(empty["avg_bandwidth_bps"], 0.0)
        self.assertEqual(empty["avg_readers"], 0.0)
        self.assertEqual(empty["peak_bandwidth_bps"], 0.0)
        self.assertEqual(empty["peak_readers"], 0)

    def test_single_weekday_hour_from_one_date(self) -> None:
        code, data = self._get()
        self.assertEqual(code, 200)
        tue = self._cell(data, 2, 14)
        self.assertEqual(tue["date_count"], 1)
        self.assertEqual(tue["sample_count"], 1)
        self.assertEqual(tue["avg_bandwidth_bps"], 2000.0)
        self.assertEqual(tue["peak_bandwidth_bps"], 2000.0)


if __name__ == "__main__":
    unittest.main()
