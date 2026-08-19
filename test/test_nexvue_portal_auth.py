#!/usr/bin/env python3
"""
Unit tests for web-portal/nexvue-portal-auth-lib.php — schema migration,
org-scoping, enrollment tokens, catalog ACL, viewer JWT shape (Phase 4 —
cloud portal).

Requires `php` on PATH with openssl + sqlite3. Skipped when unavailable.

Run: python3 test/test_nexvue_portal_auth.py
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "web-portal" / "nexvue-portal-auth-lib.php"
PHP = shutil.which("php")


def _b64url_decode(s: str) -> bytes:
    pad = 4 - (len(s) % 4)
    if pad < 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


@unittest.skipUnless(PHP and LIB.is_file(), "php CLI or nexvue-portal-auth-lib.php missing")
class TestPortalAuthLib(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.portal_dir = Path(self._td.name) / "portal"
        self.portal_dir.mkdir(parents=True, exist_ok=True)
        self.db = Path(self._td.name) / "portal.db"

    def tearDown(self) -> None:
        self._td.cleanup()

    def _php(self, body: str) -> dict:
        lib = LIB.as_posix()
        code = f"""
putenv('NEXVUE_PORTAL_DB={self.db.as_posix()}');
putenv('NEXVUE_PORTAL_DIR={self.portal_dir.as_posix()}');
include '{lib}';
portal_migrate();
{body}
"""
        env = os.environ.copy()
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

    # ---- migration / seed --------------------------------------------------

    def test_seeds_default_org_and_admin(self) -> None:
        data = self._php(
            """
$admin = portal_user_find_by_username('admin');
$org = portal_org_find_by_id($admin['org_id']);
echo json_encode(['user' => portal_user_row_public($admin), 'org' => $org]);
"""
        )
        self.assertEqual(data["user"]["username"], "admin")
        self.assertEqual(data["user"]["role"], "org_admin")
        self.assertTrue(data["user"]["must_change_password"])
        self.assertEqual(data["org"]["name"], "Default Org")

    def test_migrate_is_idempotent(self) -> None:
        data = self._php(
            """
portal_migrate();
portal_migrate();
$count = (int)portal_db()->querySingle('SELECT COUNT(*) FROM orgs');
echo json_encode(['org_count' => $count]);
"""
        )
        self.assertEqual(data["org_count"], 1)

    # ---- org-scoped users ---------------------------------------------------

    def test_user_create_requires_valid_org(self) -> None:
        code = """
try {
    portal_user_create(['org_id' => 'not-a-real-org', 'username' => 'x', 'password' => 'password123', 'role' => 'org_viewer']);
    echo json_encode(['threw' => false]);
} catch (InvalidArgumentException $e) {
    echo json_encode(['threw' => true]);
}
"""
        data = self._php(code)
        self.assertTrue(data["threw"])

    def test_user_update_rejects_cross_org_actor(self) -> None:
        data = self._php(
            """
$org2 = portal_org_create('Second Org');
$u1 = portal_user_create(['org_id' => portal_user_find_by_username('admin')['org_id'], 'username' => 'viewer1', 'password' => 'password123', 'role' => 'org_viewer']);
$threw = false;
try {
    portal_user_update($u1['id'], ['role' => 'org_admin'], $org2['id']);
} catch (InvalidArgumentException $e) {
    $threw = true;
}
echo json_encode(['threw' => $threw]);
"""
        )
        self.assertTrue(data["threw"])

    def test_user_verify_rejects_disabled(self) -> None:
        data = self._php(
            """
$org = portal_user_find_by_username('admin')['org_id'];
$u = portal_user_create(['org_id' => $org, 'username' => 'toverify', 'password' => 'password123', 'role' => 'org_viewer']);
portal_user_update($u['id'], ['disabled' => true]);
$ok = portal_user_verify('toverify', 'password123');
echo json_encode(['verified' => $ok !== null]);
"""
        )
        self.assertFalse(data["verified"])

    # ---- enrollment tokens ---------------------------------------------------

    def test_enroll_token_create_and_consume_creates_station(self) -> None:
        data = self._php(
            """
$org = portal_user_find_by_username('admin')['org_id'];
$admin = portal_user_find_by_username('admin');
$et = portal_enroll_token_create($org, 'Studio A', $admin['id']);
$result = portal_enroll_token_consume($et['token'], 'https://edge-a.example.com', '2.5.0');
echo json_encode([
    'station_name' => $result['station']['name'],
    'edge_url' => $result['station']['edge_base_url'],
    'has_api_key' => strlen($result['api_key']) > 0,
]);
"""
        )
        self.assertEqual(data["station_name"], "Studio A")
        self.assertEqual(data["edge_url"], "https://edge-a.example.com")
        self.assertTrue(data["has_api_key"])

    def test_enroll_token_double_use_is_idempotent_same_station(self) -> None:
        data = self._php(
            """
$org = portal_user_find_by_username('admin')['org_id'];
$admin = portal_user_find_by_username('admin');
$et = portal_enroll_token_create($org, 'Studio B', $admin['id']);
$r1 = portal_enroll_token_consume($et['token'], 'https://edge-b.example.com', '2.5.0');
$r2 = portal_enroll_token_consume($et['token'], 'https://edge-b.example.com', '2.5.1');
echo json_encode([
    'same_station' => $r1['station']['id'] === $r2['station']['id'],
    'different_keys' => $r1['api_key'] !== $r2['api_key'],
]);
"""
        )
        self.assertTrue(data["same_station"])
        self.assertTrue(data["different_keys"])

    def test_enroll_token_rejects_unknown_token(self) -> None:
        code = """
$threw = false;
try {
    portal_enroll_token_consume('not-a-real-token', 'https://edge.example.com', '1.0.0');
} catch (InvalidArgumentException $e) {
    $threw = true;
}
echo json_encode(['threw' => $threw]);
"""
        data = self._php(code)
        self.assertTrue(data["threw"])

    def test_enroll_token_rejects_revoked(self) -> None:
        data = self._php(
            """
$org = portal_user_find_by_username('admin')['org_id'];
$admin = portal_user_find_by_username('admin');
$et = portal_enroll_token_create($org, 'Studio C', $admin['id']);
portal_enroll_token_revoke($et['row']['id'], $org);
$threw = false;
try {
    portal_enroll_token_consume($et['token'], 'https://edge-c.example.com', '1.0.0');
} catch (InvalidArgumentException $e) {
    $threw = true;
}
echo json_encode(['threw' => $threw]);
"""
        )
        self.assertTrue(data["threw"])

    def test_station_findable_by_api_key(self) -> None:
        data = self._php(
            """
$org = portal_user_find_by_username('admin')['org_id'];
$admin = portal_user_find_by_username('admin');
$et = portal_enroll_token_create($org, 'Studio D', $admin['id']);
$result = portal_enroll_token_consume($et['token'], 'https://edge-d.example.com', '1.0.0');
$found = portal_station_find_by_api_key($result['api_key']);
echo json_encode(['found' => $found !== null, 'matches' => $found['id'] === $result['station']['id']]);
"""
        )
        self.assertTrue(data["found"])
        self.assertTrue(data["matches"])

    # ---- catalog ACL -----------------------------------------------------

    def test_admin_and_operator_see_all_org_channels_without_grants(self) -> None:
        data = self._php(
            """
$org = portal_user_find_by_username('admin')['org_id'];
$adminUser = portal_user_find_by_username('admin');
$et = portal_enroll_token_create($org, 'Studio E', $adminUser['id']);
$result = portal_enroll_token_consume($et['token'], 'https://edge-e.example.com', '1.0.0');
portal_station_channels_upsert($result['station']['id'], [
    ['channel_base' => 'ch0', 'alias' => 'Program', 'lo_enabled' => true],
    ['channel_base' => 'ch1', 'alias' => 'Preview', 'lo_enabled' => false],
]);
$catalog = portal_catalog_list_for_user($adminUser);
echo json_encode(['stations' => count($catalog), 'channels' => count($catalog[0]['channels'] ?? [])]);
"""
        )
        self.assertEqual(data["stations"], 1)
        self.assertEqual(data["channels"], 2)

    def test_viewer_sees_nothing_without_explicit_grant(self) -> None:
        data = self._php(
            """
$org = portal_user_find_by_username('admin')['org_id'];
$adminUser = portal_user_find_by_username('admin');
$et = portal_enroll_token_create($org, 'Studio F', $adminUser['id']);
$result = portal_enroll_token_consume($et['token'], 'https://edge-f.example.com', '1.0.0');
portal_station_channels_upsert($result['station']['id'], [['channel_base' => 'ch0']]);
$viewer = portal_user_create(['org_id' => $org, 'username' => 'viewer_nogrant', 'password' => 'password123', 'role' => 'org_viewer']);
$catalog = portal_catalog_list_for_user($viewer);
echo json_encode(['stations' => count($catalog)]);
"""
        )
        self.assertEqual(data["stations"], 0)

    def test_viewer_sees_only_granted_channel(self) -> None:
        data = self._php(
            """
$org = portal_user_find_by_username('admin')['org_id'];
$adminUser = portal_user_find_by_username('admin');
$et = portal_enroll_token_create($org, 'Studio G', $adminUser['id']);
$result = portal_enroll_token_consume($et['token'], 'https://edge-g.example.com', '1.0.0');
$stationId = $result['station']['id'];
portal_station_channels_upsert($stationId, [
    ['channel_base' => 'ch0', 'alias' => 'Program'],
    ['channel_base' => 'ch1', 'alias' => 'Preview'],
]);
$viewer = portal_user_create(['org_id' => $org, 'username' => 'viewer_grant', 'password' => 'password123', 'role' => 'org_viewer']);
portal_catalog_acl_put($org, $viewer['id'], $stationId, ['ch0']);
$catalog = portal_catalog_list_for_user($viewer);
echo json_encode([
    'stations' => count($catalog),
    'channels' => array_column($catalog[0]['channels'] ?? [], 'channel_base'),
    'allows_ch0' => portal_user_allows_channel($viewer, $stationId, 'ch0'),
    'allows_ch1' => portal_user_allows_channel($viewer, $stationId, 'ch1'),
]);
"""
        )
        self.assertEqual(data["stations"], 1)
        self.assertEqual(data["channels"], ["ch0"])
        self.assertTrue(data["allows_ch0"])
        self.assertFalse(data["allows_ch1"])

    def test_catalog_acl_never_crosses_org(self) -> None:
        code = """
$org1 = portal_user_find_by_username('admin')['org_id'];
$org2 = portal_org_create('Other Org');
$adminUser = portal_user_find_by_username('admin');
$et = portal_enroll_token_create($org1, 'Studio H', $adminUser['id']);
$result = portal_enroll_token_consume($et['token'], 'https://edge-h.example.com', '1.0.0');
$otherOrgViewer = portal_user_create(['org_id' => $org2['id'], 'username' => 'other_org_viewer', 'password' => 'password123', 'role' => 'org_viewer']);
$threw = false;
try {
    portal_catalog_acl_put($org2['id'], $otherOrgViewer['id'], $result['station']['id'], null);
} catch (InvalidArgumentException $e) {
    $threw = true;
}
echo json_encode(['threw' => $threw]);
"""
        data = self._php(code)
        self.assertTrue(data["threw"])

    def test_channel_upsert_deactivates_missing_channels_not_deletes(self) -> None:
        data = self._php(
            """
$org = portal_user_find_by_username('admin')['org_id'];
$adminUser = portal_user_find_by_username('admin');
$et = portal_enroll_token_create($org, 'Studio I', $adminUser['id']);
$result = portal_enroll_token_consume($et['token'], 'https://edge-i.example.com', '1.0.0');
$stationId = $result['station']['id'];
portal_station_channels_upsert($stationId, [['channel_base' => 'ch0'], ['channel_base' => 'ch1']]);
portal_station_channels_upsert($stationId, [['channel_base' => 'ch0']]); // ch1 dropped from heartbeat
$active = portal_station_channels_list($stationId, true);
$all = portal_station_channels_list($stationId, false);
echo json_encode(['active_count' => count($active), 'all_count' => count($all)]);
"""
        )
        self.assertEqual(data["active_count"], 1)
        self.assertEqual(data["all_count"], 2)

    # ---- viewer JWT claim shape (hard MediaMTX contract) ---------------------

    def test_viewer_jwt_claim_shape(self) -> None:
        data = self._php(
            """
$keys = portal_ensure_keys();
$jwt = portal_mint_viewer_jwt('portal-user:abc', 'ch2');
[$h, $p, $s] = explode('.', $jwt);
echo json_encode(['header' => $h, 'payload' => $p, 'kid' => $keys['kid']]);
"""
        )
        header = json.loads(_b64url_decode(data["header"]))
        payload = json.loads(_b64url_decode(data["payload"]))
        self.assertEqual(header["alg"], "RS256")
        self.assertEqual(header["kid"], data["kid"])
        self.assertEqual(
            payload["mediamtx_permissions"],
            [{"action": "read", "path": "ch2"}, {"action": "read", "path": "ch2lo"}],
        )
        self.assertIn("exp", payload)
        self.assertIn("iat", payload)

    def test_viewer_jwt_rejects_invalid_channel(self) -> None:
        code = """
$threw = false;
try {
    portal_mint_viewer_jwt('sub', 'ch99');
} catch (InvalidArgumentException $e) {
    $threw = true;
}
echo json_encode(['threw' => $threw]);
"""
        data = self._php(code)
        self.assertTrue(data["threw"])

    def test_portal_jwks_shape(self) -> None:
        data = self._php("echo json_encode(portal_ensure_keys()['jwks']);")
        self.assertEqual(len(data["keys"]), 1)
        self.assertEqual(data["keys"][0]["kty"], "RSA")
        self.assertEqual(data["keys"][0]["alg"], "RS256")


if __name__ == "__main__":
    unittest.main()
