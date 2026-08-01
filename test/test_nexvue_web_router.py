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
ROUTER = ROOT / "nexvue-web-router.php"
PUBLIC_INDEX = ROOT / "public" / "index.php"
LIB = ROOT / "nexvue-auth-lib.php"
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
        player = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="/player"', player)
        self.assertIn('href="/settings"', player)
        self.assertIn('src="/assets/nexvue-ui.js"', player)
        self.assertIn("/api/ops", player)
        self.assertIn("/api/status", player)

    def test_js_api_base(self) -> None:
        gate = (ROOT / "nexvue-auth-gate.js").read_text(encoding="utf-8")
        self.assertIn('AUTH_URL = "/api/auth"', gate)
        self.assertIn('"/login"', gate)
        ui = (ROOT / "nexvue-ui.js").read_text(encoding="utf-8")
        self.assertIn('LOGO_SRC = "/api/logo"', ui)


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


if __name__ == "__main__":
    unittest.main()
