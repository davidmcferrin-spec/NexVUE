#!/usr/bin/env python3
"""
Endpoint tests for nexvue-client-events.php (opt-in session reports).

Requires `php` on PATH with the sqlite3 extension. Skipped automatically
when php/sqlite is unavailable.

Run: python3 test/test_nexvue_client_events.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PHP_FILE = ROOT / "web-node" / "nexvue-client-events.php"
PHP = shutil.which("php")
SID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


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
    PHP and PHP_FILE.is_file() and _php_has_sqlite(),
    "php CLI with sqlite3, or nexvue-client-events.php, missing",
)
class TestClientEvents(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.db = Path(self._td.name) / "client-events.db"
        self.code_file = Path(self._td.name) / "http_code.txt"

    def tearDown(self) -> None:
        self._td.cleanup()

    def _run(self, method: str, query: dict | None = None, body: dict | None = None) -> tuple[int, dict]:
        q = dict(query or {})
        get_php = ",\n".join(
            f"{json.dumps(k)} => {json.dumps(v)}" for k, v in q.items()
        )
        body_json = json.dumps(body) if body is not None else ""
        runner = Path(self._td.name) / "run_ce.php"
        extra_putenv = ""
        if body is not None:
            extra_putenv = f"putenv('NEXVUE_CLIENT_EVENTS_BODY=' . {json.dumps(body_json)});"
        runner.write_text(
            f"""<?php
declare(strict_types=1);
$_GET = [{get_php}];
$_SERVER['REQUEST_METHOD'] = {json.dumps(method)};
putenv('NEXVUE_CLIENT_EVENTS_DB={self.db.as_posix()}');
putenv('NEXVUE_AUTH_BYPASS=1');
{extra_putenv}
$codeFile = '{self.code_file.as_posix()}';
register_shutdown_function(static function () use ($codeFile): void {{
    $code = http_response_code();
    if ($code === false || $code === 0) {{
        $code = 200;
    }}
    file_put_contents($codeFile, (string)$code);
}});
require '{PHP_FILE.as_posix()}';
""",
            encoding="utf-8",
        )

        if self.code_file.exists():
            self.code_file.unlink()
        env = os.environ.copy()
        env["NEXVUE_CLIENT_EVENTS_DB"] = str(self.db)
        env["NEXVUE_AUTH_BYPASS"] = "1"
        r = subprocess.run(
            [PHP, "-d", "display_errors=stderr", str(runner)],
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )
        raw = (r.stdout or "").strip()
        if raw.startswith("Content-Type:"):
            raw = raw.split("\n\n", 1)[-1].strip()
        code = 200
        if self.code_file.is_file():
            code = int(self.code_file.read_text(encoding="utf-8").strip() or "200")
        elif r.returncode != 0:
            self.fail(f"php failed ({r.returncode}): {r.stderr or r.stdout}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self.fail(f"expected JSON, got: {raw!r}\nstderr={r.stderr!r}")
        return code, data

    def test_post_event_then_list_and_get(self) -> None:
        code, data = self._run("POST", body={
            "session_id": SID,
            "page": "player",
            "channel": "ch0",
            "kind": "event",
            "event": "start",
            "detail": "ch0",
            "client_label": "Chrome/120 · Windows",
        })
        self.assertEqual(code, 200, data)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("stored"), "event")

        code, data = self._run("POST", body={
            "session_id": SID,
            "page": "player",
            "channel": "ch0",
            "kind": "snapshot",
            "snapshot": {
                "inbound_bps": 4_200_000,
                "loss_pct": 0.4,
                "rtt_ms": 12.5,
                "rendition": "hi",
                "width": 1920,
                "height": 1080,
            },
        })
        self.assertEqual(code, 200, data)
        self.assertEqual(data.get("stored"), "snapshot")

        now = int(time.time())
        code, data = self._run("GET", query={
            "action": "list",
            "from": str(now - 3600),
            "to": str(now + 10),
        })
        self.assertEqual(code, 200, data)
        self.assertEqual(len(data["reports"]), 1)
        self.assertEqual(data["reports"][0]["session_id"], SID)
        self.assertEqual(data["reports"][0]["snapshot_count"], 1)
        self.assertEqual(data["reports"][0]["event_count"], 1)

        code, data = self._run("GET", query={"action": "get", "session_id": SID})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["events"][0]["kind"], "start")
        self.assertEqual(data["snapshots"][0]["rendition"], "hi")
        self.assertAlmostEqual(float(data["snapshots"][0]["inbound_bps"]), 4_200_000, places=0)

    def test_rejects_bad_session_id(self) -> None:
        code, data = self._run("POST", body={
            "session_id": "not-a-id",
            "kind": "event",
            "event": "start",
        })
        self.assertEqual(code, 400)
        self.assertIn("session_id", data.get("error", ""))

    def test_rate_limits_snapshots(self) -> None:
        body = {
            "session_id": SID,
            "page": "player",
            "channel": "ch1",
            "kind": "snapshot",
            "snapshot": {"inbound_bps": 1},
        }
        code, data = self._run("POST", body=body)
        self.assertEqual(code, 200, data)
        self.assertEqual(data.get("stored"), "snapshot")
        code, data = self._run("POST", body=body)
        self.assertEqual(code, 200, data)
        self.assertEqual(data.get("skipped"), "rate")

    def test_client_fallback_key(self) -> None:
        sid = "c" + ("ab" * 16)
        code, data = self._run("POST", body={
            "session_id": sid,
            "page": "multiview",
            "kind": "event",
            "event": "start",
        })
        self.assertEqual(code, 200, data)
        self.assertEqual(data.get("stored"), "event")

    def test_unknown_event_rejected(self) -> None:
        code, data = self._run("POST", body={
            "session_id": SID,
            "kind": "event",
            "event": "explode",
        })
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
