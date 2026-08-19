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


if __name__ == "__main__":
    unittest.main()
