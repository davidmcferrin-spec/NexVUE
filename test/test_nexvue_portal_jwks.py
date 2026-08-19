#!/usr/bin/env python3
"""
Unit tests for auth_merged_jwks() / portal-jwks-cache handling in
nexvue-auth-lib.php (Phase 4 — cloud portal adoption).

Requires `php` on PATH with openssl + sqlite3. Skipped when unavailable.

Run: python3 test/test_nexvue_portal_jwks.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "web-node" / "nexvue-auth-lib.php"
PHP = shutil.which("php")


@unittest.skipUnless(PHP and LIB.is_file(), "php CLI or nexvue-auth-lib.php missing")
class TestPortalMergedJwks(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.auth_dir = Path(self._td.name) / "auth"
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        self.db = Path(self._td.name) / "auth.db"
        self.env = Path(self._td.name) / "nexvue.env"
        self.env.write_text("# test\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    def _php(self, body: str) -> dict:
        lib = LIB.as_posix()
        code = f"""
putenv('NEXVUE_AUTH_DB={self.db.as_posix()}');
putenv('NEXVUE_AUTH_DIR={self.auth_dir.as_posix()}');
putenv('NEXVUE_STATION_ENV={self.env.as_posix()}');
include '{lib}';
auth_migrate();
auth_ensure_keys();
{body}
"""
        env = os.environ.copy()
        env.pop("NEXVUE_AUTH_HTTP", None)
        r = subprocess.run(
            [PHP, "-d", "display_errors=stderr", "-r", code],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        if r.returncode != 0:
            self.fail(f"php failed ({r.returncode}): {r.stderr or r.stdout}")
        out = (r.stdout or "").strip()
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            self.fail(f"expected JSON, got: {out!r}\nstderr={r.stderr!r}")

    def test_local_only_unaffected_when_no_cache(self) -> None:
        data = self._php("echo json_encode(auth_merged_jwks());")
        self.assertEqual(len(data["keys"]), 1)
        self.assertFalse((self.auth_dir / "portal-jwks-cache.json").exists())

    def test_merge_includes_both_kids(self) -> None:
        data = self._php(
            """
$local = auth_ensure_keys();
$portalJwks = ['keys' => [[
    'kty' => 'RSA', 'use' => 'sig', 'alg' => 'RS256',
    'kid' => 'portal-key-1', 'n' => 'AAAA', 'e' => 'AQAB',
]]];
auth_portal_jwks_cache_write($portalJwks);
$merged = auth_merged_jwks();
echo json_encode(['local_kid' => $local['kid'], 'merged' => $merged]);
"""
        )
        kids = {k["kid"] for k in data["merged"]["keys"]}
        self.assertEqual(len(data["merged"]["keys"]), 2)
        self.assertIn(data["local_kid"], kids)
        self.assertIn("portal-key-1", kids)

    def test_corrupt_cache_falls_back_to_local_only(self) -> None:
        (self.auth_dir / "portal-jwks-cache.json").write_text("not json{{{", encoding="utf-8")
        data = self._php("echo json_encode(auth_merged_jwks());")
        self.assertEqual(len(data["keys"]), 1)

    def test_missing_keys_array_ignored(self) -> None:
        (self.auth_dir / "portal-jwks-cache.json").write_text(
            json.dumps({"fetched_at": "2026-01-01T00:00:00Z"}), encoding="utf-8"
        )
        data = self._php("echo json_encode(auth_merged_jwks());")
        self.assertEqual(len(data["keys"]), 1)

    def test_cache_write_rejects_missing_keys(self) -> None:
        code = """
try {
    auth_portal_jwks_cache_write(['not_keys' => []]);
    echo json_encode(['threw' => false]);
} catch (InvalidArgumentException $e) {
    echo json_encode(['threw' => true]);
}
"""
        data = self._php(code)
        self.assertTrue(data["threw"])

    def test_portal_adopted_false_by_default(self) -> None:
        data = self._php("echo json_encode(['adopted' => auth_portal_adopted()]);")
        self.assertFalse(data["adopted"])

    def test_portal_adopted_true_once_url_and_adopted_at_set(self) -> None:
        self.env.write_text(
            "NEXVUE_PORTAL_URL=https://portal.example.com\n"
            "NEXVUE_PORTAL_ADOPTED_AT=2026-08-15T00:00:00Z\n",
            encoding="utf-8",
        )
        data = self._php(
            "echo json_encode(['adopted' => auth_portal_adopted(), 'env' => auth_read_portal_env()]);"
        )
        self.assertTrue(data["adopted"])
        self.assertEqual(data["env"]["url"], "https://portal.example.com")

    def test_jwks_php_serves_merged_when_cache_present(self) -> None:
        jwks_php = (ROOT / "web-node" / "nexvue-jwks.php").as_posix()
        code = f"""
$local = auth_ensure_keys();
auth_portal_jwks_cache_write(['keys' => [[
    'kty' => 'RSA', 'use' => 'sig', 'alg' => 'RS256',
    'kid' => 'portal-key-2', 'n' => 'AAAA', 'e' => 'AQAB',
]]]);
ob_start();
include '{jwks_php}';
$out = ob_get_clean();
echo $out;
"""
        data = self._php(code)
        kids = {k["kid"] for k in data["keys"]}
        self.assertEqual(len(data["keys"]), 2)
        self.assertIn("portal-key-2", kids)


if __name__ == "__main__":
    unittest.main()
