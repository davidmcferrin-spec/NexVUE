#!/usr/bin/env python3
"""
NexAPP catalog roles, group→station ACL, heartbeat user bundle, and
portal JWT SSO on the edge (never overwrites local admin).

Run: python3 test/test_nexvue_portal_nexapp.py
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
PORTAL_LIB = ROOT / "web-portal" / "nexvue-portal-auth-lib.php"
NEXAPP_LIB = ROOT / "web-portal" / "nexvue-portal-nexapp.php"
EDGE_LIB = ROOT / "web-node" / "nexvue-auth-lib.php"
PHP = shutil.which("php")


def _ensure_openssl_conf(env: dict[str, str]) -> None:
    """WinGet PHP looks for openssl.cnf under Program Files; extras/ssl has a copy."""
    if env.get("OPENSSL_CONF"):
        return
    if not PHP:
        return
    cand = Path(PHP).resolve().parent / "extras" / "ssl" / "openssl.cnf"
    if cand.is_file():
        env["OPENSSL_CONF"] = str(cand)


@unittest.skipUnless(PHP and PORTAL_LIB.is_file() and EDGE_LIB.is_file(), "php or libs missing")
class TestNexappPortalAndEdgeSso(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.portal_dir = Path(self._td.name) / "portal"
        self.portal_dir.mkdir()
        self.portal_db = Path(self._td.name) / "portal.db"
        self.edge_dir = Path(self._td.name) / "edge-auth"
        self.edge_dir.mkdir()
        self.edge_db = Path(self._td.name) / "edge.db"
        self.directory = Path(self._td.name) / "directory.json"
        self.directory.write_text(
            json.dumps(
                [
                    {
                        "id": "group-news",
                        "name": "News",
                        "catalog_role": "user",
                        "members": [
                            {
                                "sub": "11111111-1111-4111-8111-111111111111",
                                "email": "reporter@nexstar.tv",
                                "name": "Reporter",
                            }
                        ],
                    }
                ]
            ),
            encoding="utf-8",
        )
        self.access_stub = Path(self._td.name) / "access.json"
        self.access_stub.write_text(
            json.dumps(
                {
                    "ok": True,
                    "status": 200,
                    "sub": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "email": "admin.user@nexstar.tv",
                    "name": "Catalog Admin",
                    "role": "admin",
                    "source": "stub",
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._td.cleanup()

    def _php(self, body: str, extra_env: dict[str, str] | None = None) -> dict:
        env = os.environ.copy()
        env["NEXVUE_PORTAL_DB"] = str(self.portal_db)
        env["NEXVUE_PORTAL_DIR"] = str(self.portal_dir)
        env["NEXVUE_AUTH_DB"] = str(self.edge_db)
        env["NEXVUE_AUTH_DIR"] = str(self.edge_dir)
        env["NEXVUE_PORTAL_NEXAPP_DIRECTORY"] = str(self.directory)
        env["NEXVUE_PORTAL_NEXAPP_ACCESS_STUB"] = str(self.access_stub)
        env["NEXVUE_PORTAL_TEST_AUTH"] = "1"
        if extra_env:
            env.update(extra_env)
        _ensure_openssl_conf(env)
        code = f"""
include '{PORTAL_LIB.as_posix()}';
include '{NEXAPP_LIB.as_posix()}';
include '{EDGE_LIB.as_posix()}';
portal_migrate();
auth_migrate();
{body}
"""
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

    def test_catalog_role_never_promotes_unknown_to_admin(self) -> None:
        data = self._php(
            """
echo json_encode([
    'admin' => portal_nexapp_normalize_catalog_role('admin'),
    'user' => portal_nexapp_normalize_catalog_role('user'),
    'blank' => portal_nexapp_normalize_catalog_role(''),
    'weird' => portal_nexapp_normalize_catalog_role('superuser'),
]);
"""
        )
        self.assertEqual(data["admin"], "admin")
        self.assertEqual(data["user"], "user")
        self.assertEqual(data["blank"], "user")
        self.assertEqual(data["weird"], "user")

    def test_nexapp_upsert_maps_catalog_admin_to_org_admin(self) -> None:
        data = self._php(
            """
$access = portal_nexapp_check_access();
$row = portal_nexapp_upsert_user($access);
echo json_encode(portal_user_row_public($row));
"""
        )
        self.assertEqual(data["role"], "org_admin")
        self.assertEqual(data["catalog_role"], "admin")
        self.assertTrue(str(data["identity_key"]).startswith("nexapp:"))

    def test_heartbeat_users_from_group_acl_never_admin(self) -> None:
        data = self._php(
            """
$org = portal_default_org();
$admin = portal_user_find_by_username('admin');
$et = portal_enroll_token_create($org['id'], 'Studio', $admin['id']);
$result = portal_enroll_token_consume($et['token'], 'https://edge.example.com', '2.30.0');
$sid = $result['station']['id'];
portal_station_channels_upsert($sid, [['channel_base' => 'ch0', 'alias' => 'A', 'active' => true]]);
portal_group_acl_put($org['id'], 'group-news', 'News', $sid, ['ch0'], 'operator');
$users = portal_heartbeat_users_for_station($sid);
echo json_encode(['n' => count($users), 'user' => $users[0] ?? null, 'roles_ok' => true]);
"""
        )
        self.assertEqual(data["n"], 1)
        self.assertEqual(data["user"]["role"], "operator")
        self.assertNotEqual(data["user"]["role"], "admin")
        self.assertEqual(data["user"]["email"], "reporter@nexstar.tv")
        self.assertEqual(data["user"]["channels"], ["ch0"])

    def test_directory_unavailable_returns_null_not_empty_wipe(self) -> None:
        data = self._php(
            """
echo json_encode(['dir' => portal_nexapp_directory()]);
""",
            extra_env={"NEXVUE_PORTAL_NEXAPP_DIRECTORY": "/no/such/file.json"},
        )
        # Missing stub and no hub bootstrap → null (omit users_sync).
        self.assertIsNone(data["dir"])

    def test_edge_sync_skips_local_admin_and_disables_removed_nexapp_users(self) -> None:
        data = self._php(
            """
$admin = auth_user_find_by_username('admin');
$bundle = [[
    'id' => '11111111-1111-4111-8111-111111111111',
    'username' => 'reporter@nexstar.tv',
    'email' => 'reporter@nexstar.tv',
    'identity_key' => 'nexapp:11111111-1111-4111-8111-111111111111',
    'role' => 'viewer',
    'channels' => ['ch0'],
]];
$first = auth_apply_portal_user_sync($bundle);
$viewer = auth_user_find_by_identity_key('nexapp:11111111-1111-4111-8111-111111111111');
$second = auth_apply_portal_user_sync([]);
$viewer2 = auth_user_find_by_identity_key('nexapp:11111111-1111-4111-8111-111111111111');
$admin2 = auth_user_find_by_username('admin');
echo json_encode([
    'first' => $first,
    'viewer_role' => $viewer['role'] ?? null,
    'viewer_disabled' => !empty($viewer2['disabled_at']),
    'admin_still' => $admin2['role'] ?? null,
    'admin_disabled' => !empty($admin2['disabled_at']),
    'second_disabled' => $second['disabled'],
]);
"""
        )
        self.assertEqual(data["first"]["upserted"], 1)
        self.assertEqual(data["viewer_role"], "viewer")
        self.assertTrue(data["viewer_disabled"])
        self.assertEqual(data["admin_still"], "admin")
        self.assertFalse(data["admin_disabled"])
        self.assertGreaterEqual(data["second_disabled"], 1)

    def test_jwt_verify_fails_closed_without_key(self) -> None:
        data = self._php(
            """
$threw = false;
try {
    portal_nexapp_verify_jwt('not.a.jwt');
} catch (Throwable $e) {
    $threw = true;
}
echo json_encode(['threw' => $threw]);
""",
            extra_env={"NEXAPP_PUBLIC_KEY_PATH": str(Path(self._td.name) / "missing.pem")},
        )
        self.assertTrue(data["threw"])

    def test_not_assigned_is_forbidden_not_admin(self) -> None:
        stub = Path(self._td.name) / "access-403.json"
        stub.write_text(
            json.dumps(
                {
                    "ok": False,
                    "status": 403,
                    "error": "not_assigned",
                    "role": "user",
                }
            ),
            encoding="utf-8",
        )
        data = self._php(
            """
$msg = null;
try {
    portal_try_nexapp_user();
} catch (RuntimeException $e) {
    $msg = $e->getMessage();
}
echo json_encode(['msg' => $msg]);
""",
            extra_env={"NEXVUE_PORTAL_NEXAPP_ACCESS_STUB": str(stub)},
        )
        self.assertEqual(data["msg"], "forbidden")

    def test_production_migrate_seeds_org_not_admin(self) -> None:
        data = self._php(
            """
$admin = portal_user_find_by_username('admin');
$orgs = (int)portal_db()->querySingle('SELECT COUNT(*) FROM orgs');
echo json_encode(['admin' => $admin, 'orgs' => $orgs]);
""",
            extra_env={"NEXVUE_PORTAL_TEST_AUTH": "0"},
        )
        self.assertIsNone(data["admin"])
        self.assertEqual(data["orgs"], 1)

    def test_portal_sso_jwt_roundtrip(self) -> None:
        data = self._php(
            """
$org = portal_default_org();
$admin = portal_user_find_by_username('admin');
$et = portal_enroll_token_create($org['id'], 'Studio', $admin['id']);
$result = portal_enroll_token_consume($et['token'], 'https://edge.example.com', '2.30.0');
$sid = $result['station']['id'];
$jwt = portal_mint_sso_jwt($admin, $sid);
$keys = portal_ensure_keys();
auth_portal_jwks_cache_write($keys['jwks']);
$claims = auth_portal_jwt_verify($jwt);
$row = auth_login_portal_sso($claims);
echo json_encode([
    'typ' => $claims['typ'] ?? null,
    'iss_ok' => ($claims['iss'] ?? '') === 'nexvue-portal',
    'role' => $claims['role'] ?? null,
    'logged_role' => $row['role'] ?? null,
]);
"""
        )
        self.assertEqual(data["typ"], "nexvue-portal-sso")
        self.assertTrue(data["iss_ok"])
        self.assertEqual(data["role"], "operator")
        self.assertIn(data["logged_role"], ("admin", "operator"))


if __name__ == "__main__":
    unittest.main()
