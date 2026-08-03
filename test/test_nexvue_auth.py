#!/usr/bin/env python3
"""
Unit tests for nexvue-auth-lib.php — users, shares, JWT, reset, import/export.

Requires `php` on PATH with openssl + sqlite3. Skipped when unavailable.

Run: python3 test/test_nexvue_auth.py
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
LIB = ROOT / "nexvue-auth-lib.php"
PHP = shutil.which("php")


@unittest.skipUnless(PHP and LIB.is_file(), "php CLI or nexvue-auth-lib.php missing")
class TestNexVueAuth(unittest.TestCase):
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

    def test_bootstrap_admin(self) -> None:
        data = self._php(
            "echo json_encode(['user' => auth_user_row_public(auth_user_find_by_username('admin'))]);"
        )
        self.assertEqual(data["user"]["username"], "admin")
        self.assertEqual(data["user"]["role"], "admin")
        self.assertTrue(data["user"]["must_change_password"])

    def test_default_db_path_inside_auth_dir(self) -> None:
        """Default SQLite path must be under auth/ so Apache can create WAL files."""
        lib = LIB.as_posix()
        code = f"""
putenv('NEXVUE_AUTH_DIR={self.auth_dir.as_posix()}');
putenv('NEXVUE_AUTH_DB');
putenv('NEXVUE_STATION_ENV={self.env.as_posix()}');
include '{lib}';
echo json_encode(['path' => auth_db_path(), 'dir' => auth_dir()]);
"""
        r = subprocess.run(
            [PHP, "-d", "display_errors=stderr", "-r", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode != 0:
            self.fail(f"php failed ({r.returncode}): {r.stderr or r.stdout}")
        data = json.loads((r.stdout or "").strip())
        self.assertEqual(data["path"], data["dir"] + "/auth.db")

    def test_password_verify(self) -> None:
        data = self._php(
            """
$ok = auth_user_verify('admin', 'password');
$bad = auth_user_verify('admin', 'wrongpass');
echo json_encode(['ok' => $ok !== null, 'bad' => $bad === null]);
"""
        )
        self.assertTrue(data["ok"])
        self.assertTrue(data["bad"])

    def test_change_password_after_session_cache(self) -> None:
        """Hot-path session cache omits password_hash; forceDb must verify + clear must_change."""
        data = self._php(
            """
if (session_status() !== PHP_SESSION_ACTIVE) {
  session_start();
}
$row = auth_user_find_by_username('admin');
auth_login_user($row);
// Prime request memo + session snapshot (empty hash), as status/me polls do.
$cached = auth_current_user();
$cached_hash = (string)($cached['password_hash'] ?? 'missing');
// change_password path: reload from DB
$db_user = auth_current_user(true);
$verify = password_verify('password', (string)$db_user['password_hash']);
auth_user_update($db_user['id'], [
  'password' => 'newpassword1',
  'must_change_password' => false,
]);
$fresh = auth_current_user(true);
$me = auth_me_payload();
$verify_new = password_verify('newpassword1', (string)$fresh['password_hash']);
echo json_encode([
  'cached_hash_empty' => $cached_hash === '',
  'verify_old' => $verify,
  'must_change_after' => !empty($me['must_change_password']),
  'session_flag' => !empty($_SESSION['must_change_password']),
  'verify_new' => $verify_new,
]);
"""
        )
        self.assertTrue(data["cached_hash_empty"])
        self.assertTrue(data["verify_old"])
        self.assertFalse(data["must_change_after"])
        self.assertFalse(data["session_flag"])
        self.assertTrue(data["verify_new"])

    def test_share_requires_expiry(self) -> None:
        data = self._php(
            """
try {
  auth_parse_expires(null, null);
  echo json_encode(['error' => 'expected throw']);
} catch (InvalidArgumentException $e) {
  echo json_encode(['error' => $e->getMessage()]);
}
"""
        )
        self.assertIn("expiry", data["error"].lower())

    def test_share_create_revoke(self) -> None:
        data = self._php(
            """
$admin = auth_user_find_by_username('admin');
$exp = gmdate('Y-m-d\\TH:i:s\\Z', time() + 3600);
$c = auth_share_create('Bench feed', ['ch0', 'ch2'], $exp, $admin['id']);
$valid = auth_share_is_valid($c['row']);
$found = auth_share_find_by_token($c['token']);
$rev = auth_share_revoke($c['row']['id']);
$after = auth_share_is_valid($rev);
echo json_encode([
  'valid' => $valid,
  'found' => $found !== null,
  'channels' => json_decode($c['row']['channels'], true),
  'status' => auth_share_row_public($rev)['status'],
  'after' => $after,
  'has_token' => strlen($c['token']) >= 32,
]);
"""
        )
        self.assertTrue(data["valid"])
        self.assertTrue(data["found"])
        self.assertEqual(data["channels"], ["ch0", "ch2"])
        self.assertEqual(data["status"], "revoked")
        self.assertFalse(data["after"])
        self.assertTrue(data["has_token"])

    def test_share_token_persisted_same_url(self) -> None:
        data = self._php(
            """
$admin = auth_user_find_by_username('admin');
$exp = gmdate('Y-m-d\\TH:i:s\\Z', time() + 3600);
$c = auth_share_create('Persist', ['ch0'], $exp, $admin['id'], 'multiview');
$row = auth_share_find_by_id($c['row']['id']);
$pub = auth_share_row_public($row);
$list = auth_shares_list($admin['id']);
$found = null;
foreach ($list as $s) {
  if ($s['id'] === $c['row']['id']) { $found = $s; break; }
}
echo json_encode([
  'stored_token' => ($row['token'] ?? '') === $c['token'],
  'page' => $row['page'] ?? null,
  'url' => $pub['url'] ?? null,
  'list_url' => $found['url'] ?? null,
  'same' => ($pub['url'] ?? '') === ($found['url'] ?? '') && str_contains((string)($pub['url'] ?? ''), 'multiview.html?t='),
]);
"""
        )
        self.assertTrue(data["stored_token"])
        self.assertEqual(data["page"], "multiview")
        self.assertTrue(data["same"])
        self.assertIn("multiview.html?t=", data["url"])

    def test_share_update_and_delete(self) -> None:
        data = self._php(
            """
$admin = auth_user_find_by_username('admin');
$exp = gmdate('Y-m-d\\TH:i:s\\Z', time() + 3600);
$c = auth_share_create('Old name', ['ch0'], $exp, $admin['id']);
$id = $c['row']['id'];
$token = $c['token'];
$exp2 = gmdate('Y-m-d\\TH:i:s\\Z', time() + 7200);
$upd = auth_share_update($id, 'New name', ['ch1', 'ch3'], $exp2);
$same_token = auth_share_find_by_token($token) !== null;
$revoked_edit = null;
auth_share_revoke($id);
try {
  auth_share_update($id, 'Nope', ['ch0'], $exp2);
  $revoked_edit = 'ok';
} catch (InvalidArgumentException $e) {
  $revoked_edit = $e->getMessage();
}
auth_share_delete($id);
$gone = auth_share_find_by_id($id);
echo json_encode([
  'name' => $upd['name'],
  'channels' => json_decode($upd['channels'], true),
  'same_token' => $same_token,
  'revoked_edit' => $revoked_edit,
  'gone' => $gone === null,
]);
"""
        )
        self.assertEqual(data["name"], "New name")
        self.assertEqual(data["channels"], ["ch1", "ch3"])
        self.assertTrue(data["same_token"])
        self.assertIn("revoked", data["revoked_edit"].lower())
        self.assertTrue(data["gone"])

    def test_shares_purge_expired_grace(self) -> None:
        data = self._php(
            """
$admin = auth_user_find_by_username('admin');
$old = gmdate('Y-m-d\\TH:i:s\\Z', time() - (8 * 86400));
$recent = gmdate('Y-m-d\\TH:i:s\\Z', time() - (2 * 86400));
$future = gmdate('Y-m-d\\TH:i:s\\Z', time() + 3600);
// Insert expired rows directly (create() rejects past expires_at).
$db = auth_db();
$now = auth_now_iso();
foreach ([['stale', $old], ['fresh-expired', $recent], ['live', $future]] as $pair) {
  [$name, $ex] = $pair;
  $id = auth_uuid();
  $st = $db->prepare(
    'INSERT INTO share_links (id, name, token_hash, token, page, channels, expires_at, revoked_at, created_by, created_at, updated_at, synced_at)
     VALUES (:id, :n, :th, NULL, :pg, :ch, :ex, NULL, :cb, :c, :up, NULL)'
  );
  $st->bindValue(':id', $id, SQLITE3_TEXT);
  $st->bindValue(':n', $name, SQLITE3_TEXT);
  $st->bindValue(':th', auth_hash_token(bin2hex(random_bytes(8))), SQLITE3_TEXT);
  $st->bindValue(':pg', 'player', SQLITE3_TEXT);
  $st->bindValue(':ch', '["ch0"]', SQLITE3_TEXT);
  $st->bindValue(':ex', $ex, SQLITE3_TEXT);
  $st->bindValue(':cb', $admin['id'], SQLITE3_TEXT);
  $st->bindValue(':c', $now, SQLITE3_TEXT);
  $st->bindValue(':up', $now, SQLITE3_TEXT);
  $st->execute();
}
$deleted = auth_shares_purge_expired(7 * 86400);
$names = [];
foreach (auth_shares_list() as $s) {
  $names[] = $s['name'];
}
sort($names);
echo json_encode(['deleted' => $deleted, 'names' => $names]);
"""
        )
        self.assertGreaterEqual(data["deleted"], 1)
        self.assertIn("fresh-expired", data["names"])
        self.assertIn("live", data["names"])
        self.assertNotIn("stale", data["names"])

    def test_share_channels_preserve_order(self) -> None:
        data = self._php(
            """
$ordered = auth_normalize_share_channels(['ch3', 'ch1', 'ch0'], 'player');
echo json_encode(['ordered' => $ordered]);
"""
        )
        self.assertEqual(data["ordered"], ["ch3", "ch1", "ch0"])

    def test_multiview_share_max_four(self) -> None:
        data = self._php(
            """
$ok = auth_normalize_share_channels(['ch0', 'ch1', 'ch2', 'ch3'], 'multiview');
$err = null;
try {
  auth_normalize_share_channels(['ch0', 'ch1', 'ch2', 'ch3', 'ch4'], 'multiview');
  $err = 'expected throw';
} catch (InvalidArgumentException $e) {
  $err = $e->getMessage();
}
$player = auth_normalize_share_channels(['ch0', 'ch1', 'ch2', 'ch3', 'ch4'], 'player');
echo json_encode(['ok' => $ok, 'err' => $err, 'player' => $player]);
"""
        )
        self.assertEqual(data["ok"], ["ch0", "ch1", "ch2", "ch3"])
        self.assertIn("at most 4", data["err"].lower())
        self.assertEqual(data["player"], ["ch0", "ch1", "ch2", "ch3", "ch4"])

    def test_jwt_permissions_paths(self) -> None:
        data = self._php(
            """
$jwt = auth_mint_viewer_jwt('user:admin', ['ch1']);
$parts = explode('.', $jwt);
$payload = json_decode(auth_b64url_decode($parts[1]), true);
$paths = array_column($payload['mediamtx_permissions'], 'path');
sort($paths);
echo json_encode(['paths' => $paths, 'alg' => json_decode(auth_b64url_decode($parts[0]), true)['alg']]);
"""
        )
        self.assertEqual(data["alg"], "RS256")
        self.assertEqual(data["paths"], ["ch1", "ch1lo"])

    def test_reset_single_use(self) -> None:
        data = self._php(
            """
$u = auth_user_find_by_username('admin');
$r = auth_reset_create($u['id']);
auth_reset_consume($r['token'], 'newpassword1');
$again = null;
try {
  auth_reset_consume($r['token'], 'otherpassword');
  $again = 'ok';
} catch (InvalidArgumentException $e) {
  $again = $e->getMessage();
}
$login = auth_user_verify('admin', 'newpassword1');
echo json_encode(['again' => $again, 'login' => $login !== null]);
"""
        )
        self.assertIn("invalid", data["again"].lower())
        self.assertTrue(data["login"])

    def test_users_import_export_idempotent(self) -> None:
        data = self._php(
            """
$exported = auth_users_export(null);
$uid = $exported[0]['id'];
$r1 = auth_users_import($exported);
$r2 = auth_users_import($exported);
$again = auth_users_export(null);
echo json_encode([
  'n' => count($exported),
  'u1' => $r1['upserted'],
  'u2' => $r2['upserted'],
  'same_id' => $again[0]['id'] === $uid,
]);
"""
        )
        self.assertGreaterEqual(data["n"], 1)
        self.assertEqual(data["u1"], data["n"])
        self.assertEqual(data["u2"], data["n"])
        self.assertTrue(data["same_id"])

    def test_publish_jwt_env(self) -> None:
        data = self._php(
            """
$jwt = auth_ensure_publish_jwt_in_env();
$again = auth_ensure_publish_jwt_in_env();
$raw = file_get_contents(getenv('NEXVUE_STATION_ENV'));
echo json_encode([
  'len' => strlen($jwt),
  'same' => $jwt === $again,
  'in_env' => str_contains($raw, 'NEXVUE_PUBLISH_JWT='),
]);
"""
        )
        self.assertGreater(data["len"], 40)
        self.assertTrue(data["same"])
        self.assertTrue(data["in_env"])

    def test_sharer_role_and_user_channel_acl(self) -> None:
        data = self._php(
            """
$u = auth_user_create([
  'username' => 'sharebob',
  'password' => 'password1',
  'role' => 'sharer',
  'channels' => ['ch1', 'ch3'],
]);
$pub = auth_user_row_public($u);
$bases = auth_user_channel_bases($u);
$ok = auth_user_allows_channels($u, ['ch1']);
$deny = auth_user_allows_channels($u, ['ch0', 'ch1']);
$admin = auth_user_find_by_username('admin');
$adminBases = auth_user_channel_bases($admin);
echo json_encode([
  'role' => $pub['role'],
  'channels' => $pub['channels'],
  'bases' => $bases,
  'ok' => $ok,
  'deny' => $deny,
  'admin_all' => count($adminBases) === 8 && $pub['channels'] !== null,
  'admin_null' => auth_user_row_public($admin)['channels'] === null,
]);
"""
        )
        self.assertEqual(data["role"], "sharer")
        self.assertEqual(data["channels"], ["ch1", "ch3"])
        self.assertEqual(data["bases"], ["ch1", "ch3"])
        self.assertTrue(data["ok"])
        self.assertFalse(data["deny"])
        self.assertTrue(data["admin_all"])
        self.assertTrue(data["admin_null"])

    def test_share_list_filter_and_can_manage(self) -> None:
        data = self._php(
            """
$admin = auth_user_find_by_username('admin');
$sharer = auth_user_create([
  'username' => 'sharer1',
  'password' => 'password1',
  'role' => 'sharer',
]);
$exp = gmdate('Y-m-d\\TH:i:s\\Z', time() + 3600);
$a = auth_share_create('Admin share', ['ch0'], $exp, $admin['id']);
$s = auth_share_create('Sharer share', ['ch1'], $exp, $sharer['id']);
$all = auth_shares_list(null);
$mine = auth_shares_list($sharer['id']);
$canOwn = auth_share_can_manage($s['row'], $sharer);
$canOther = auth_share_can_manage($a['row'], $sharer);
$adminAny = auth_share_can_manage($s['row'], $admin);
echo json_encode([
  'all_n' => count($all),
  'mine_n' => count($mine),
  'mine_name' => $mine[0]['name'] ?? null,
  'owner_name' => $mine[0]['created_by_username'] ?? null,
  'can_own' => $canOwn,
  'can_other' => $canOther,
  'admin_any' => $adminAny,
]);
"""
        )
        self.assertGreaterEqual(data["all_n"], 2)
        self.assertEqual(data["mine_n"], 1)
        self.assertEqual(data["mine_name"], "Sharer share")
        self.assertEqual(data["owner_name"], "sharer1")
        self.assertTrue(data["can_own"])
        self.assertFalse(data["can_other"])
        self.assertTrue(data["admin_any"])


    def test_session_cache_snapshot_keeps_channel_acl(self) -> None:
        """Hot-path session row must carry channels or ACL falls back to all."""
        data = self._php(
            """
$u = auth_user_create([
  'username' => 'acluser',
  'password' => 'password1',
  'role' => 'viewer',
  'channels' => ['ch1', 'ch2'],
]);
// Simulate pre-fix hot-path snapshot (no channels key).
$legacy = [
  'id' => $u['id'],
  'username' => $u['username'],
  'role' => $u['role'],
  'password_hash' => '',
  'email' => null,
  'must_change_password' => 0,
  'disabled_at' => null,
  'created_at' => '',
  'updated_at' => '',
  'synced_at' => null,
];
$legacyBases = auth_user_channel_bases($legacy);
// Fixed snapshot includes raw DB channels.
$fixed = $legacy;
$fixed['channels'] = $u['channels'];
$fixedBases = auth_user_channel_bases($fixed);
echo json_encode([
  'legacy_is_all' => count($legacyBases) === 8,
  'fixed' => $fixedBases,
  'raw' => $u['channels'],
]);
"""
        )
        self.assertTrue(data["legacy_is_all"])
        self.assertEqual(data["fixed"], ["ch1", "ch2"])
        self.assertIn("ch1", data["raw"])


if __name__ == "__main__":
    unittest.main()
