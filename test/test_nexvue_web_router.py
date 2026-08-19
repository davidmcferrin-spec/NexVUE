#!/usr/bin/env python3
"""
Smoke + PHP unit tests for the path front door (nexvue-web-router.php).

Run: python3 test/test_nexvue_web_router.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTER = ROOT / "web-node" / "nexvue-web-router.php"
PUBLIC_INDEX = ROOT / "web-node" / "public" / "index.php"
LIB = ROOT / "web-node" / "nexvue-auth-lib.php"
PHP = shutil.which("php")


class TestWebRouterFiles(unittest.TestCase):
    def test_front_door_files_exist(self) -> None:
        self.assertTrue(ROUTER.is_file())
        self.assertTrue(PUBLIC_INDEX.is_file())
        conf = (ROOT / "nexvue-web-apache.conf").read_text(encoding="utf-8")
        self.assertIn("@@APP_ROOT@@", conf)
        self.assertIn("RewriteEngine On", conf)
        self.assertIn("RewriteRule ^ index.php", conf)
        self.assertIn("FallbackResource", conf)
        setup = (ROOT / "setup.sh").read_text(encoding="utf-8")
        self.assertIn("a2enmod rewrite", setup)
        self.assertIn("front-door smoke", setup)

    def test_html_uses_path_nav_and_assets(self) -> None:
        player = (ROOT / "web-node" / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="/player"', player)
        self.assertIn('href="/settings"', player)
        self.assertIn('src="/assets/nexvue-ui.js"', player)
        self.assertIn("/api/ops", player)
        self.assertIn("/api/status", player)

    def test_js_api_base(self) -> None:
        gate = (ROOT / "web-node" / "nexvue-auth-gate.js").read_text(encoding="utf-8")
        self.assertIn('AUTH_URL = "/api/auth"', gate)
        self.assertIn('"/login"', gate)
        ui = (ROOT / "web-node" / "nexvue-ui.js").read_text(encoding="utf-8")
        self.assertIn('LOGO_SRC = "/api/logo"', ui)

    def test_whep_always_https(self) -> None:
        # MediaMTX :8889 is TLS-only. Inheriting http: from a leftover :80
        # page produced ERR_CONNECTION_RESET (Chrome reports it as CORS).
        gate = (ROOT / "web-node" / "nexvue-auth-gate.js").read_text(encoding="utf-8")
        self.assertIn('return "https://" + edgeHost() + ":" + WHEP_PORT', gate)
        self.assertIn("whepUrl: whepUrl", gate)
        player = (ROOT / "web-node" / "index.html").read_text(encoding="utf-8")
        self.assertIn("NexVueAuth.whepUrl(path, jwt)", player)
        self.assertNotIn("location.protocol", player)
        multi = (ROOT / "web-node" / "multiview.html").read_text(encoding="utf-8")
        self.assertIn("NexVueAuth.whepUrl(path, jwt)", multi)
        self.assertNotIn("location.protocol", multi)

    def test_login_page_has_non_blocking_portal_nudge(self) -> None:
        # Phase 4 — local sign-in must never be hidden/blocked by the nudge;
        # it only becomes visible via JS after a successful portal_status
        # check (adopted + reachable), never rendered "on" by default.
        login = (ROOT / "web-node" / "login.html").read_text(encoding="utf-8")
        self.assertIn('id="portal-nudge"', login)
        self.assertNotIn('id="portal-nudge show"', login)
        self.assertIn("portal_status", login)
        self.assertIn('id="login-form"', login)
        self.assertNotIn('id="login-box" class="hide"', login)


@unittest.skipUnless(PHP and ROUTER.is_file() and LIB.is_file(), "php CLI missing")
class TestWebRouterPhpMaps(unittest.TestCase):
    def test_maps(self) -> None:
        code = f"""
require '{ROUTER.as_posix()}';
require '{LIB.as_posix()}';
echo json_encode([
  'legacy_player' => nexvue_web_legacy_page_redirect('/index.html'),
  'legacy_settings' => nexvue_web_legacy_page_redirect('/channels.html'),
  'api_ops' => nexvue_web_legacy_api_path('/nexvue-ops.php'),
  'pages' => array_keys(nexvue_web_pages()),
  'apis' => array_keys(nexvue_web_apis()),
  'share_path' => auth_share_page_path('multiview'),
  'share_url' => auth_share_build_url('abc', 'player', 'https', 'edge.example'),
]);
"""
        r = subprocess.run([PHP, "-r", code], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["legacy_player"], "/player")
        self.assertEqual(data["legacy_settings"], "/settings")
        self.assertEqual(data["api_ops"], "/api/ops")
        self.assertIn("/login", data["pages"])
        self.assertIn("/api/ops", data["apis"])
        self.assertEqual(data["share_path"], "multiview")
        self.assertEqual(data["share_url"], "https://edge.example/player?t=abc")

    def test_https_redirect_target(self) -> None:
        code = f"""
require '{ROUTER.as_posix()}';
$_SERVER['HTTPS'] = 'off';
$_SERVER['HTTP_HOST'] = '10.207.40.18';
$_SERVER['REQUEST_URI'] = '/player?t=abc';
$_SERVER['SERVER_PORT'] = '80';
$http = nexvue_web_https_redirect_target();
$_SERVER['HTTPS'] = 'on';
$_SERVER['SERVER_PORT'] = '443';
$https = nexvue_web_https_redirect_target();
$_SERVER['HTTPS'] = 'off';
$_SERVER['HTTP_HOST'] = '127.0.0.1:9080';
$_SERVER['SERVER_PORT'] = '9080';
$_SERVER['REQUEST_URI'] = '/nexvue-jwks.php';
$loop = nexvue_web_https_redirect_target();
echo json_encode(['http' => $http, 'https' => $https, 'loopback' => $loop]);
"""
        r = subprocess.run([PHP, "-r", code], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["http"], "https://10.207.40.18/player?t=abc")
        self.assertIsNone(data["https"])
        self.assertIsNone(data["loopback"])


if __name__ == "__main__":
    unittest.main()
