#!/usr/bin/env python3
"""
Smoke + PHP unit tests for the portal path front door
(nexvue-portal-web-router.php).

Run: python3 test/test_nexvue_portal_web_router.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTER = ROOT / "web-portal" / "nexvue-portal-web-router.php"
PUBLIC_INDEX = ROOT / "web-portal" / "public" / "index.php"
LIB = ROOT / "web-portal" / "nexvue-portal-auth-lib.php"
PHP = shutil.which("php")


class TestPortalWebRouterFiles(unittest.TestCase):
    def test_front_door_files_exist(self) -> None:
        self.assertTrue(ROUTER.is_file())
        self.assertTrue(PUBLIC_INDEX.is_file())
        conf = (ROOT / "web-portal" / "nexvue-portal-web-apache.conf").read_text(encoding="utf-8")
        self.assertIn("@@APP_ROOT@@", conf)
        self.assertIn("RewriteEngine On", conf)
        self.assertIn("RewriteRule ^ index.php", conf)

    def test_pages_directory_has_expected_files(self) -> None:
        for name in ("login.html", "catalog.html", "watch.html", "stations.html", "users.html"):
            self.assertTrue((ROOT / "web-portal" / name).is_file(), name)


@unittest.skipUnless(PHP and ROUTER.is_file() and LIB.is_file(), "php CLI missing")
class TestPortalWebRouterPhpMaps(unittest.TestCase):
    def test_maps(self) -> None:
        code = f"""
require '{ROUTER.as_posix()}';
$pages = nexvue_portal_web_pages();
echo json_encode(['pages' => array_keys($pages)]);
"""
        r = subprocess.run(
            [PHP, "-d", "display_errors=stderr", "-r", code],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.returncode != 0:
            self.fail(f"php failed: {r.stderr or r.stdout}")
        data = json.loads((r.stdout or "").strip())
        for path in ("/", "/login", "/catalog", "/watch", "/stations", "/users"):
            self.assertIn(path, data["pages"])

    def test_login_is_public_others_require_role(self) -> None:
        code = f"""
require '{ROUTER.as_posix()}';
$pages = nexvue_portal_web_pages();
echo json_encode([
    'login_public' => $pages['/login']['public'],
    'catalog_public' => $pages['/catalog']['public'],
    'stations_roles' => $pages['/stations']['roles'],
    'users_roles' => $pages['/users']['roles'],
]);
"""
        r = subprocess.run(
            [PHP, "-d", "display_errors=stderr", "-r", code],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.returncode != 0:
            self.fail(f"php failed: {r.stderr or r.stdout}")
        data = json.loads((r.stdout or "").strip())
        self.assertTrue(data["login_public"])
        self.assertFalse(data["catalog_public"])
        self.assertEqual(data["stations_roles"], ["org_admin"])
        self.assertEqual(data["users_roles"], ["org_admin"])

    def test_https_redirect_target(self) -> None:
        # Apache keeps :80 open on a portal box too, purely to redirect —
        # same rationale as the edge (a stray http:// bookmark should
        # bounce cleanly, not dead-end against a TLS-only endpoint).
        code = f"""
require '{ROUTER.as_posix()}';
$_SERVER['HTTPS'] = 'off';
$_SERVER['HTTP_HOST'] = 'portal.example.com';
$_SERVER['REQUEST_URI'] = '/catalog';
$_SERVER['SERVER_PORT'] = '80';
$http = nexvue_portal_web_https_redirect_target();
$_SERVER['HTTPS'] = 'on';
$_SERVER['SERVER_PORT'] = '443';
$https = nexvue_portal_web_https_redirect_target();
echo json_encode(['http' => $http, 'https' => $https]);
"""
        r = subprocess.run([PHP, "-r", code], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["http"], "https://portal.example.com/catalog")
        self.assertIsNone(data["https"])

    def test_dispatch_redirects_before_any_routing(self) -> None:
        text = ROUTER.read_text(encoding="utf-8")
        dispatch_start = text.index("function nexvue_portal_web_dispatch(): void {")
        redirect_idx = text.index("nexvue_portal_web_https_redirect_target()", dispatch_start)
        pages_idx = text.index("nexvue_portal_web_pages()", dispatch_start)
        self.assertLess(redirect_idx, pages_idx, "HTTPS redirect must be checked before page routing")


if __name__ == "__main__":
    unittest.main()
