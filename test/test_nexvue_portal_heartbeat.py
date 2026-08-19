#!/usr/bin/env python3
"""
Unit tests for nexvue-portal-heartbeat.php (Phase 4 — cloud portal
adoption): channel enumeration, no-op-when-unadopted, and graceful
success/failure handling of the outbound sync call so the systemd timer
never enters a failed-restart loop.

Requires `php` on PATH with openssl + sqlite3. Skipped when unavailable.

Run: python3 test/test_nexvue_portal_heartbeat.py
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "nexvue-portal-heartbeat.php"
LIB = ROOT / "web-node" / "nexvue-auth-lib.php"
PHP = shutil.which("php")


class _StubHandler(http.server.BaseHTTPRequestHandler):
    response_status = 200
    response_body: dict = {"ok": True}
    received: list = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            _StubHandler.received.append(json.loads(raw))
        except json.JSONDecodeError:
            _StubHandler.received.append({"_raw": raw.decode("utf-8", "replace")})
        body = json.dumps(_StubHandler.response_body).encode("utf-8")
        self.send_response(_StubHandler.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence default stderr logging
        pass


@unittest.skipUnless(PHP and SCRIPT.is_file() and LIB.is_file(), "php CLI or heartbeat script missing")
class TestPortalHeartbeat(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.auth_dir = Path(self._td.name) / "auth"
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        self.db = Path(self._td.name) / "auth.db"
        self.env_file = Path(self._td.name) / "nexvue.env"
        self.channels_dir = Path(self._td.name) / "channels"
        self.channels_dir.mkdir(parents=True, exist_ok=True)
        self.heartbeat_status = self.auth_dir / "portal-heartbeat-status.json"

        _StubHandler.response_status = 200
        _StubHandler.response_body = {"ok": True}
        _StubHandler.received = []
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
        self.server_port = self.server.server_address[1]
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server_thread.join(timeout=5)
        self.server.server_close()
        self._td.cleanup()

    def _write_channel(self, n: int, *, alias: str = "", lo_enable: str = "false") -> None:
        (self.channels_dir / f"{n}.env").write_text(
            f'CHANNEL_PATH=ch{n}\nCHANNEL_ALIAS="{alias}"\nLO_ENABLE={lo_enable}\n',
            encoding="utf-8",
        )

    def _run(self, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["NEXVUE_AUTH_DB"] = str(self.db)
        env["NEXVUE_AUTH_DIR"] = str(self.auth_dir)
        env["NEXVUE_STATION_ENV"] = str(self.env_file)
        env["NEXVUE_CHANNELS_DIR"] = str(self.channels_dir)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [PHP, "-d", "display_errors=stderr", str(SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def _php_include_only(self, body: str, extra_env: dict | None = None) -> dict:
        env = os.environ.copy()
        env["NEXVUE_AUTH_DB"] = str(self.db)
        env["NEXVUE_AUTH_DIR"] = str(self.auth_dir)
        env["NEXVUE_STATION_ENV"] = str(self.env_file)
        env["NEXVUE_CHANNELS_DIR"] = str(self.channels_dir)
        env["NEXVUE_PORTAL_HEARTBEAT_INCLUDE_ONLY"] = "1"
        if extra_env:
            env.update(extra_env)
        code = f"include '{SCRIPT.as_posix()}';\n{body}"
        r = subprocess.run(
            [PHP, "-d", "display_errors=stderr", "-r", code],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        if r.returncode != 0:
            self.fail(f"php failed ({r.returncode}): {r.stderr or r.stdout}")
        return json.loads((r.stdout or "").strip())

    def test_collect_channels_parses_alias_and_lo(self) -> None:
        self._write_channel(0, alias="Program", lo_enable="true")
        self._write_channel(1, alias="", lo_enable="false")
        data = self._php_include_only("echo json_encode(heartbeat_collect_channels());")
        self.assertEqual(len(data), 2)
        ch0 = next(c for c in data if c["channel_base"] == "ch0")
        self.assertEqual(ch0["alias"], "Program")
        self.assertTrue(ch0["lo_enabled"])
        ch1 = next(c for c in data if c["channel_base"] == "ch1")
        self.assertFalse(ch1["lo_enabled"])

    def test_collect_channels_skips_missing_slots(self) -> None:
        self._write_channel(3)
        data = self._php_include_only("echo json_encode(heartbeat_collect_channels());")
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["channel_base"], "ch3")

    def test_noop_when_unadopted(self) -> None:
        self.env_file.write_text("# not adopted\n", encoding="utf-8")
        r = self._run()
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.heartbeat_status.exists())
        self.assertEqual(_StubHandler.received, [])

    def test_success_writes_ok_status_and_sends_channels(self) -> None:
        self.env_file.write_text(
            f"NEXVUE_PORTAL_URL=http://127.0.0.1:{self.server_port}\n"
            "NEXVUE_PORTAL_ADOPTED_AT=2026-08-15T00:00:00Z\n"
            "NEXVUE_PORTAL_STATION_API_KEY=testkey12345678\n",
            encoding="utf-8",
        )
        self._write_channel(0, alias="Program")
        r = self._run()
        self.assertEqual(r.returncode, 0)
        status = json.loads(self.heartbeat_status.read_text(encoding="utf-8"))
        self.assertTrue(status["ok"])
        self.assertEqual(len(_StubHandler.received), 1)
        self.assertEqual(_StubHandler.received[0]["channels"][0]["channel_base"], "ch0")

    def test_success_refreshes_jwks_cache(self) -> None:
        _StubHandler.response_body = {
            "ok": True,
            "portal_jwks": {"keys": [{
                "kty": "RSA", "use": "sig", "alg": "RS256",
                "kid": "portal-heartbeat-key", "n": "AAAA", "e": "AQAB",
            }]},
        }
        self.env_file.write_text(
            f"NEXVUE_PORTAL_URL=http://127.0.0.1:{self.server_port}\n"
            "NEXVUE_PORTAL_ADOPTED_AT=2026-08-15T00:00:00Z\n"
            "NEXVUE_PORTAL_STATION_API_KEY=testkey12345678\n",
            encoding="utf-8",
        )
        r = self._run()
        self.assertEqual(r.returncode, 0)
        cache = json.loads((self.auth_dir / "portal-jwks-cache.json").read_text(encoding="utf-8"))
        self.assertEqual(cache["keys"][0]["kid"], "portal-heartbeat-key")

    def test_non_2xx_writes_ok_false_and_exits_zero(self) -> None:
        _StubHandler.response_status = 500
        self.env_file.write_text(
            f"NEXVUE_PORTAL_URL=http://127.0.0.1:{self.server_port}\n"
            "NEXVUE_PORTAL_ADOPTED_AT=2026-08-15T00:00:00Z\n"
            "NEXVUE_PORTAL_STATION_API_KEY=testkey12345678\n",
            encoding="utf-8",
        )
        r = self._run()
        self.assertEqual(r.returncode, 0, "must never fail the systemd unit on a portal error")
        status = json.loads(self.heartbeat_status.read_text(encoding="utf-8"))
        self.assertFalse(status["ok"])

    def test_unreachable_portal_writes_ok_false_and_exits_zero(self) -> None:
        # A closed local port (server never bound here) — genuinely unreachable
        # without tearing down the shared stub server used by other tests.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            dead_port = s.getsockname()[1]
        self.env_file.write_text(
            f"NEXVUE_PORTAL_URL=http://127.0.0.1:{dead_port}\n"
            "NEXVUE_PORTAL_ADOPTED_AT=2026-08-15T00:00:00Z\n"
            "NEXVUE_PORTAL_STATION_API_KEY=testkey12345678\n",
            encoding="utf-8",
        )
        r = self._run()
        self.assertEqual(r.returncode, 0, "must never fail the systemd unit when the portal is down")
        status = json.loads(self.heartbeat_status.read_text(encoding="utf-8"))
        self.assertFalse(status["ok"])


if __name__ == "__main__":
    unittest.main()
