#!/usr/bin/env python3
"""
Unit tests for kick registry helpers in nexvue-ops.php.

Requires `php` on PATH (stdlib PHP only — no extensions beyond json).
Skipped automatically when php is unavailable (e.g. Windows laptop without PHP).

Run: python3 test/test_nexvue_kick_registry.py
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
OPS_PHP = ROOT / "nexvue-ops.php"
PHP = shutil.which("php")
UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"


@unittest.skipUnless(PHP and OPS_PHP.is_file(), "php CLI or nexvue-ops.php missing")
class TestKickRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.reg = Path(self._td.name) / "kicked.json"
        self.reg.write_text("[]", encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    def _php(self, body: str) -> dict:
        """Include ops.php in library mode and run $body; expect JSON on stdout."""
        ops = OPS_PHP.as_posix()
        reg = self.reg.as_posix()
        # NEXVUE_OPS_HTTP unset → nexvue-ops.php returns after defining helpers.
        code = f"""
putenv('NEXVUE_KICK_REGISTRY={reg}');
include '{ops}';
{body}
"""
        env = os.environ.copy()
        env.pop("NEXVUE_OPS_HTTP", None)
        r = subprocess.run(
            [PHP, "-d", "display_errors=stderr", "-r", code],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        if r.returncode != 0:
            self.fail(f"php failed ({r.returncode}): {r.stderr or r.stdout}")
        out = (r.stdout or "").strip()
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            self.fail(f"expected JSON, got: {out!r}\nstderr={r.stderr!r}")

    def test_add_and_check_by_session(self) -> None:
        data = self._php(f"""
kick_registry_add('{UUID_A}', '203.0.113.10', 'maintenance');
$r = kick_registry_check('{UUID_A}', '203.0.113.99');
echo json_encode($r);
""")
        self.assertTrue(data["kicked"])
        self.assertIn("maintenance", data["reason"])

    def test_check_miss(self) -> None:
        data = self._php(f"""
kick_registry_add('{UUID_A}', '203.0.113.10', 'x');
$r = kick_registry_check('{UUID_B}', '203.0.113.10');
echo json_encode($r);
""")
        self.assertFalse(data["kicked"])

    def test_ip_never_matches(self) -> None:
        # Shared NAT: same IP must not suppress a different session.
        data = self._php(f"""
kick_registry_add('{UUID_A}', '203.0.113.10', 'by-ip');
$wrong = kick_registry_check('{UUID_B}', '203.0.113.10');
$noSid = kick_registry_check(null, '203.0.113.10');
$right = kick_registry_check('{UUID_A}', '203.0.113.99');
echo json_encode(['wrong' => $wrong, 'noSid' => $noSid, 'right' => $right]);
""")
        self.assertFalse(data["wrong"]["kicked"])
        self.assertFalse(data["noSid"]["kicked"])
        self.assertTrue(data["right"]["kicked"])
        self.assertIn("by-ip", data["right"]["reason"])

    def test_reason_escaped_and_capped(self) -> None:
        data = self._php(f"""
$reason = kick_normalize_reason('<script>alert(1)</script>');
kick_registry_add('{UUID_A}', '10.0.0.1', $reason);
$r = kick_registry_check('{UUID_A}', null);
echo json_encode(['norm' => $reason, 'out' => $r]);
""")
        self.assertNotIn("<script>", data["out"]["reason"])
        self.assertIn("&lt;script&gt;", data["out"]["reason"])

        data = self._php("""
$long = str_repeat('a', 500);
$n = kick_normalize_reason($long);
echo json_encode(['len' => strlen($n)]);
""")
        self.assertEqual(data["len"], 200)

    def test_strip_ip_port(self) -> None:
        data = self._php("""
echo json_encode([
  'v4' => kick_strip_ip_port('203.0.113.7:54321'),
  'v6' => kick_strip_ip_port('[2001:db8::1]:9999'),
  'bare' => kick_strip_ip_port('10.1.2.3'),
]);
""")
        self.assertEqual(data["v4"], "203.0.113.7")
        self.assertEqual(data["v6"], "2001:db8::1")
        self.assertEqual(data["bare"], "10.1.2.3")

    def test_prune_expired(self) -> None:
        old_ts = int(time.time()) - 700  # past 600s TTL
        self.reg.write_text(
            json.dumps([
                {
                    "session_id": UUID_A,
                    "ip": "10.0.0.1",
                    "reason": "stale",
                    "ts": old_ts,
                }
            ]),
            encoding="utf-8",
        )
        data = self._php(f"""
$r = kick_registry_check('{UUID_A}', null);
$raw = file_get_contents(kick_registry_path());
echo json_encode(['check' => $r, 'file' => json_decode($raw, true)]);
""")
        self.assertFalse(data["check"]["kicked"])
        self.assertEqual(data["file"], [])

    def test_uuid_helper(self) -> None:
        data = self._php(f"""
echo json_encode([
  'ok' => kick_is_uuid('{UUID_A}'),
  'bad' => kick_is_uuid('not-a-uuid'),
]);
""")
        self.assertTrue(data["ok"])
        self.assertFalse(data["bad"])

    def test_remove(self) -> None:
        data = self._php(f"""
kick_registry_add('{UUID_A}', '10.0.0.1', 'temp');
kick_registry_remove('{UUID_A}');
$r = kick_registry_check('{UUID_A}', null);
echo json_encode($r);
""")
        self.assertFalse(data["kicked"])


if __name__ == "__main__":
    unittest.main()
