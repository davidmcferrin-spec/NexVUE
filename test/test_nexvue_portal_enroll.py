#!/usr/bin/env python3
"""
Unit tests for the portal_status / portal_enroll actions in nexvue-auth.php
(Phase 4 — cloud portal adoption).

Spins up `php -S` serving nexvue-auth.php as the front controller — the
built-in dev server SAPI is "cli-server", not "cli", so the file's normal
HTTP dispatch runs (the "return early" guard only fires under PHP_SAPI ===
'cli'). Covers portal_status (public) and portal_enroll's validation +
auth gating (missing/malformed fields, non-admin, non-https portal_url,
unreachable portal). The full success path — a real outbound HTTPS call to
a portal, persisting NEXVUE_PORTAL_* via the root-owned sudo wrapper — is
deliberately NOT unit-tested here: it needs real TLS and real sudo, and is
covered by the plan's manual end-to-end verification step instead
(nexvue-ops-portal-write.py's own logic is unit-tested separately in
test_nexvue_ops_portal_write.py).

Requires `php` on PATH with openssl + sqlite3. Skipped when unavailable.

Run: python3 test/test_nexvue_portal_enroll.py
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUTH_PHP = ROOT / "web-node" / "nexvue-auth.php"
PHP = shutil.which("php")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(PHP and AUTH_PHP.is_file(), "php CLI or nexvue-auth.php missing")
class TestPortalEnrollAndStatus(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.auth_dir = Path(self._td.name) / "auth"
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        self.db = Path(self._td.name) / "auth.db"
        self.env_file = Path(self._td.name) / "nexvue.env"
        self.env_file.write_text("# test\n", encoding="utf-8")
        self.heartbeat_status = Path(self._td.name) / "heartbeat-status.json"
        self.proc: subprocess.Popen | None = None
        self.port = _free_port()

    def tearDown(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._td.cleanup()

    def _start_server(self, *, bypass: bool = False) -> None:
        env = os.environ.copy()
        env["NEXVUE_AUTH_HTTP"] = "1"
        env["NEXVUE_AUTH_DB"] = str(self.db)
        env["NEXVUE_AUTH_DIR"] = str(self.auth_dir)
        env["NEXVUE_STATION_ENV"] = str(self.env_file)
        env["NEXVUE_PORTAL_HEARTBEAT_STATUS"] = str(self.heartbeat_status)
        if bypass:
            env["NEXVUE_AUTH_BYPASS"] = "1"
        else:
            env.pop("NEXVUE_AUTH_BYPASS", None)
        self.proc = subprocess.Popen(
            [PHP, "-d", "display_errors=0", "-S", f"127.0.0.1:{self.port}", str(AUTH_PHP)],
            cwd=str(AUTH_PHP.parent),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1)
                conn.request("GET", "/?action=api_ping")
                conn.getresponse().read()
                conn.close()
                return
            except (ConnectionRefusedError, OSError) as exc:
                last_exc = exc
                time.sleep(0.1)
        self.fail(f"php -S never came up: {last_exc}")

    def _post(self, action: str, body: dict) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "POST", f"/?action={action}", json.dumps(body), {"Content-Type": "application/json"}
        )
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        try:
            return resp.status, json.loads(raw)
        except json.JSONDecodeError:
            self.fail(f"expected JSON, got: {raw!r}")

    # ---- portal_status (public) -------------------------------------------

    def test_portal_status_unadopted_by_default(self) -> None:
        self._start_server()
        status, data = self._post("portal_status", {})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertFalse(data["adopted"])
        self.assertFalse(data["portal_reachable"])
        self.assertIsNone(data["portal_url"])

    def test_portal_status_adopted_reads_heartbeat_file(self) -> None:
        self.env_file.write_text(
            "NEXVUE_PORTAL_URL=https://portal.example.com\n"
            "NEXVUE_PORTAL_ADOPTED_AT=2026-08-15T00:00:00Z\n",
            encoding="utf-8",
        )
        self.heartbeat_status.write_text(
            json.dumps({"ok": True, "at": "2026-08-15T00:05:00Z"}), encoding="utf-8"
        )
        self._start_server()
        status, data = self._post("portal_status", {})
        self.assertEqual(status, 200)
        self.assertTrue(data["adopted"])
        self.assertTrue(data["portal_reachable"])
        self.assertEqual(data["portal_url"], "https://portal.example.com")

    # ---- portal_enroll (admin-gated) ---------------------------------------

    def test_portal_enroll_requires_admin_session(self) -> None:
        self._start_server(bypass=False)
        status, data = self._post(
            "portal_enroll",
            {
                "portal_url": "https://portal.example.com",
                "enrollment_token": "t",
                "edge_base_url": "https://edge.example.com",
            },
        )
        self.assertEqual(status, 401)
        self.assertFalse(data["ok"])

    def test_portal_enroll_rejects_non_https_portal_url(self) -> None:
        self._start_server(bypass=True)
        status, data = self._post(
            "portal_enroll",
            {
                "portal_url": "http://portal.example.com",
                "enrollment_token": "t",
                "edge_base_url": "https://edge.example.com",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("https://", data["error"])

    def test_portal_enroll_missing_token_rejected(self) -> None:
        self._start_server(bypass=True)
        status, data = self._post(
            "portal_enroll",
            {
                "portal_url": "https://portal.example.com",
                "enrollment_token": "",
                "edge_base_url": "https://edge.example.com",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("enrollment_token", data["error"])

    def test_portal_enroll_rejects_non_https_edge_base_url(self) -> None:
        self._start_server(bypass=True)
        status, data = self._post(
            "portal_enroll",
            {
                "portal_url": "https://portal.example.com",
                "enrollment_token": "t",
                "edge_base_url": "http://edge.example.com",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("edge_base_url", data["error"])

    def test_portal_enroll_unreachable_portal_reports_502_no_partial_state(self) -> None:
        self._start_server(bypass=True)
        # A closed local port: TLS/connect fails fast, no external network needed.
        dead_port = _free_port()
        status, data = self._post(
            "portal_enroll",
            {
                "portal_url": f"https://127.0.0.1:{dead_port}",
                "enrollment_token": "t",
                "edge_base_url": "https://edge.example.com",
            },
        )
        self.assertEqual(status, 502)
        self.assertFalse(data["ok"])
        # Confirms failure happened before any local persistence was attempted.
        self.assertNotIn("NEXVUE_PORTAL_URL", self.env_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
