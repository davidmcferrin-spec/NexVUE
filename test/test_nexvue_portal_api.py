#!/usr/bin/env python3
"""
Integration test for web-portal/nexvue-portal-api.php — the full happy path
end to end over real HTTP: login -> enroll_token_create -> enroll_exchange
(edge-initiated) -> station_heartbeat (edge-initiated) -> catalog_list
(role-filtered) -> viewer_jwt (whep_url shape + ACL denial for an
unauthorized org_viewer).

Spins up `php -S` serving nexvue-portal-api.php as the front controller
(SAPI is "cli-server", not "cli", so the file's normal dispatch runs).

Requires `php` on PATH with openssl + sqlite3. Skipped when unavailable.

Run: python3 test/test_nexvue_portal_api.py
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
API_PHP = ROOT / "web-portal" / "nexvue-portal-api.php"
PHP = shutil.which("php")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(PHP and API_PHP.is_file(), "php CLI or nexvue-portal-api.php missing")
class TestPortalApiHappyPath(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.portal_dir = Path(self._td.name) / "portal"
        self.portal_dir.mkdir(parents=True, exist_ok=True)
        self.db = Path(self._td.name) / "portal.db"
        self.port = _free_port()

        env = os.environ.copy()
        env["NEXVUE_PORTAL_HTTP"] = "1"
        env["NEXVUE_PORTAL_DB"] = str(self.db)
        env["NEXVUE_PORTAL_DIR"] = str(self.portal_dir)
        self.proc = subprocess.Popen(
            [PHP, "-d", "display_errors=0", "-S", f"127.0.0.1:{self.port}", str(API_PHP)],
            cwd=str(API_PHP.parent),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1)
                conn.request("GET", "/?action=me")
                conn.getresponse().read()
                conn.close()
                break
            except (ConnectionRefusedError, OSError) as exc:
                last_exc = exc
                time.sleep(0.1)
        else:
            self.fail(f"php -S never came up: {last_exc}")

        self.cookie: str | None = None

    def tearDown(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self._td.cleanup()

    def _request(self, method: str, action: str, body: dict | None = None, *, bearer: str | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        payload = json.dumps(body) if body is not None else ""
        conn.request(method, f"/?action={action}", payload, headers)
        resp = conn.getresponse()
        set_cookie = resp.getheader("Set-Cookie")
        if set_cookie:
            self.cookie = set_cookie.split(";", 1)[0]
        raw = resp.read().decode("utf-8")
        conn.close()
        try:
            return resp.status, json.loads(raw)
        except json.JSONDecodeError:
            self.fail(f"expected JSON, got: {raw!r}")

    def _login_admin(self) -> None:
        status, data = self._request("POST", "login", {"username": "admin", "password": "password"})
        self.assertEqual(status, 200, data)

    def test_full_happy_path(self) -> None:
        # 1. Admin logs in.
        self._login_admin()
        status, me = self._request("GET", "me")
        self.assertEqual(status, 200)
        self.assertEqual(me["user"]["username"], "admin")
        self.assertEqual(me["user"]["role"], "org_admin")

        # 2. Admin creates an enrollment token for a new station.
        status, tok = self._request("POST", "enroll_token_create", {"name": "Studio Alpha"})
        self.assertEqual(status, 200, tok)
        self.assertTrue(tok["token"])

        # 3. Edge exchanges the token (no session — this call is unauthenticated
        #    except for the one-time token itself).
        self.cookie_backup, self.cookie = self.cookie, None
        status, enrolled = self._request(
            "POST",
            "enroll_exchange",
            {
                "enrollment_token": tok["token"],
                "edge_base_url": "https://edge-alpha.example.com",
                "edge_version": "2.5.0",
            },
        )
        self.assertEqual(status, 200, enrolled)
        self.assertTrue(enrolled["station_id"])
        self.assertTrue(enrolled["station_api_key"])
        self.assertEqual(len(enrolled["portal_jwks"]["keys"]), 1)
        station_id = enrolled["station_id"]
        station_key = enrolled["station_api_key"]

        # 4. Edge sends a heartbeat with its channel catalog (Bearer station key,
        #    still no browser session).
        status, hb = self._request(
            "POST",
            "station_heartbeat",
            {
                "edge_version": "2.5.0",
                "channels": [
                    {"channel_base": "ch0", "alias": "Program", "lo_enabled": True},
                    {"channel_base": "ch1", "alias": "Preview", "lo_enabled": False},
                ],
            },
            bearer=station_key,
        )
        self.assertEqual(status, 200, hb)
        self.assertEqual(len(hb["portal_jwks"]["keys"]), 1)

        # Restore the admin session for the remaining catalog/admin calls.
        self.cookie = self.cookie_backup

        # 5. Admin's own catalog_list sees everything (implicit org_admin access).
        status, catalog = self._request("GET", "catalog_list")
        self.assertEqual(status, 200)
        self.assertEqual(len(catalog["stations"]), 1)
        self.assertEqual(len(catalog["stations"][0]["channels"]), 2)

        # 6. Admin mints a viewer JWT for ch0 and gets a well-formed whep_url
        #    pointing directly at the edge (never the portal).
        status, jwt_resp = self._request(
            "POST", "viewer_jwt", {"station_id": station_id, "channel_base": "ch0"}
        )
        self.assertEqual(status, 200, jwt_resp)
        self.assertTrue(jwt_resp["jwt"])
        self.assertEqual(jwt_resp["whep_url"], "https://edge-alpha.example.com:8889/ch0/whep")

        # 7. Admin creates an org_viewer with NO catalog grant yet.
        status, viewer = self._request(
            "POST",
            "user_create",
            {"username": "viewer1", "password": "password123", "role": "org_viewer"},
        )
        self.assertEqual(status, 200, viewer)
        viewer_id = viewer["user"]["id"]

        # 8. That viewer, logged in, sees nothing yet.
        admin_cookie = self.cookie
        self.cookie = None
        status, _ = self._request(
            "POST", "login", {"username": "viewer1", "password": "password123"}
        )
        self.assertEqual(status, 200)
        status, empty_catalog = self._request("GET", "catalog_list")
        self.assertEqual(status, 200)
        self.assertEqual(empty_catalog["stations"], [])

        # And cannot mint a JWT for a channel it doesn't have access to.
        status, denied = self._request(
            "POST", "viewer_jwt", {"station_id": station_id, "channel_base": "ch0"}
        )
        self.assertEqual(status, 403, denied)

        # 9. Admin grants ch0 only; viewer now sees exactly that.
        self.cookie = admin_cookie
        status, acl = self._request(
            "POST",
            "catalog_acl_put",
            {"portal_user_id": viewer_id, "station_id": station_id, "channels": ["ch0"]},
        )
        self.assertEqual(status, 200, acl)

        self.cookie = None
        self._request("POST", "login", {"username": "viewer1", "password": "password123"})
        status, granted_catalog = self._request("GET", "catalog_list")
        self.assertEqual(status, 200)
        self.assertEqual(len(granted_catalog["stations"]), 1)
        self.assertEqual(
            [c["channel_base"] for c in granted_catalog["stations"][0]["channels"]], ["ch0"]
        )
        status, allowed = self._request(
            "POST", "viewer_jwt", {"station_id": station_id, "channel_base": "ch0"}
        )
        self.assertEqual(status, 200, allowed)
        status, still_denied = self._request(
            "POST", "viewer_jwt", {"station_id": station_id, "channel_base": "ch1"}
        )
        self.assertEqual(status, 403, still_denied)

    def test_station_heartbeat_rejects_bad_bearer(self) -> None:
        status, data = self._request("POST", "station_heartbeat", {"channels": []}, bearer="not-a-real-key")
        self.assertEqual(status, 401, data)

    def test_admin_only_actions_reject_viewer_role(self) -> None:
        self._login_admin()
        self._request(
            "POST", "user_create", {"username": "onlyviewer", "password": "password123", "role": "org_viewer"}
        )
        self.cookie = None
        self._request("POST", "login", {"username": "onlyviewer", "password": "password123"})
        status, data = self._request("POST", "enroll_token_create", {"name": "Nope"})
        self.assertEqual(status, 403, data)


if __name__ == "__main__":
    unittest.main()
