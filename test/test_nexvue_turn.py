#!/usr/bin/env python3
"""
Unit tests for Cloudflare TURN helpers in nexvue-auth-lib.php.

Requires `php` on PATH with openssl + sqlite3. Skipped when unavailable.

Run: python3 test/test_nexvue_turn.py
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

CF_LIST = {
    "iceServers": [
        {
            "urls": [
                "stun:stun.cloudflare.com:3478",
                "stun:stun.cloudflare.com:53",
            ]
        },
        {
            "urls": [
                "turn:turn.cloudflare.com:3478?transport=udp",
                "turn:turn.cloudflare.com:53?transport=udp",
                "turns:turn.cloudflare.com:443?transport=tcp",
            ],
            "username": "user-a",
            "credential": "cred-a",
        },
    ]
}

CF_OBJECT = {
    "iceServers": {
        "urls": [
            "stun:stun.cloudflare.com:3478",
            "turn:turn.cloudflare.com:3478?transport=udp",
            "turn:turn.cloudflare.com:53?transport=udp",
        ],
        "username": "user-b",
        "credential": "cred-b",
    }
}


class TestTurnUiWiring(unittest.TestCase):
    """File-level checks that do not need a working PHP sqlite extension."""

    def test_settings_panel_and_ops_actions(self) -> None:
        html = (ROOT / "web-node" / "channels.html").read_text(encoding="utf-8")
        self.assertIn('id="turn-panel"', html)
        self.assertIn("Cloudflare TURN", html)
        self.assertIn('api("turn_put"', html)
        self.assertIn('api("turn_test"', html)
        ops = (ROOT / "web-node" / "nexvue-ops.php").read_text(encoding="utf-8")
        self.assertIn("'turn_get', 'turn_put', 'turn_test'", ops)
        lib = LIB.read_text(encoding="utf-8")
        self.assertIn("function auth_turn_put", lib)
        self.assertIn("function auth_turn_ice_servers_for_viewer", lib)
        self.assertNotIn("NEXVUE_TURN_ENABLE", lib)
        auth = (ROOT / "web-node" / "nexvue-auth.php").read_text(encoding="utf-8")
        self.assertIn("'ice_servers' => $turn['ice_servers']", auth)
        gate = (ROOT / "web-node" / "nexvue-auth-gate.js").read_text(encoding="utf-8")
        self.assertIn("waitIceGathering", gate)
        self.assertIn("iceServersFrom", gate)
        portal_api = (ROOT / "web-portal" / "nexvue-portal-api.php").read_text(encoding="utf-8")
        self.assertIn("portal_station_ice_servers_for_viewer", portal_api)
        watch = (ROOT / "web-portal" / "watch.html").read_text(encoding="utf-8")
        self.assertIn("iceServersFrom", watch)
        hb = (ROOT / "nexvue-portal-heartbeat.php").read_text(encoding="utf-8")
        self.assertIn("auth_turn_ice_servers_for_viewer", hb)


def _php_sqlite_ok() -> bool:
    if not PHP:
        return False
    r = subprocess.run(
        [PHP, "-d", "display_errors=0", "-r", "echo class_exists('SQLite3') ? 'yes' : 'no';"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return (r.stdout or "").strip().endswith("yes")


@unittest.skipUnless(
    PHP and LIB.is_file() and _php_sqlite_ok(),
    "php CLI with SQLite3 or nexvue-auth-lib.php missing",
)
class TestNexVueTurn(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.auth_dir = Path(self._td.name) / "auth"
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        self.db = Path(self._td.name) / "auth.db"
        self.env = Path(self._td.name) / "nexvue.env"
        self.env.write_text("# test — secrets must not land here\n", encoding="utf-8")
        self.stub = Path(self._td.name) / "cf.json"
        self.stub.write_text(json.dumps(CF_LIST), encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    def _php(self, body: str, extra_env: dict | None = None) -> dict:
        lib = LIB.as_posix()
        code = f"""
putenv('NEXVUE_AUTH_DB={self.db.as_posix()}');
putenv('NEXVUE_AUTH_DIR={self.auth_dir.as_posix()}');
putenv('NEXVUE_STATION_ENV={self.env.as_posix()}');
include '{lib}';
auth_migrate();
{body}
"""
        env = os.environ.copy()
        env.pop("NEXVUE_AUTH_HTTP", None)
        env.pop("NEXVUE_TURN_HTTP_FAIL", None)
        env["NEXVUE_TURN_HTTP_STUB"] = str(self.stub)
        if extra_env:
            env.update(extra_env)
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

    def test_schema_creates_singleton_row(self) -> None:
        data = self._php(
            "echo json_encode(['ver' => (int)auth_db()->querySingle('PRAGMA user_version'), "
            "'pub' => auth_turn_public()]);"
        )
        self.assertGreaterEqual(data["ver"], 4)
        self.assertFalse(data["pub"]["enabled"])
        self.assertEqual(data["pub"]["key_id"], "")
        self.assertFalse(data["pub"]["has_token"])
        self.assertNotIn("api_token", data["pub"])

    def test_put_rejects_enable_without_token(self) -> None:
        data = self._php(
            """
try {
  auth_turn_put(['enabled' => true, 'key_id' => 'abcd1234-key', 'api_token' => '']);
  echo json_encode(['ok' => true]);
} catch (InvalidArgumentException $e) {
  echo json_encode(['ok' => false, 'error' => $e->getMessage()]);
}
"""
        )
        self.assertFalse(data["ok"])
        self.assertIn("token", data["error"].lower())

    def test_put_get_never_echoes_token_and_skips_env(self) -> None:
        data = self._php(
            """
$pub = auth_turn_put([
  'enabled' => true,
  'key_id' => 'turnkey12-aaaa-bbbb-cccc-dddddddddddd',
  'api_token' => 'cf-token-secret-value-ok',
]);
$row = auth_turn_row();
echo json_encode(['pub' => $pub, 'stored' => $row['api_token'], 'keys' => array_keys($pub)]);
"""
        )
        self.assertTrue(data["pub"]["enabled"])
        self.assertEqual(data["pub"]["key_id"], "turnkey12-aaaa-bbbb-cccc-dddddddddddd")
        self.assertTrue(data["pub"]["has_token"])
        self.assertTrue(data["pub"]["token_hint"].endswith("e-ok") or "e-ok" in data["pub"]["token_hint"])
        self.assertNotIn("api_token", data["keys"])
        self.assertEqual(data["stored"], "cf-token-secret-value-ok")
        env_text = self.env.read_text(encoding="utf-8")
        self.assertNotIn("TURN", env_text)
        self.assertNotIn("cf-token", env_text)

    def test_blank_token_keeps_existing(self) -> None:
        data = self._php(
            """
auth_turn_put([
  'enabled' => true,
  'key_id' => 'turnkey12-keep-token-000000000000',
  'api_token' => 'original-secret-token',
]);
$pub = auth_turn_put([
  'enabled' => false,
  'key_id' => 'turnkey12-keep-token-000000000000',
  'api_token' => '',
]);
$row = auth_turn_row();
echo json_encode(['enabled' => $pub['enabled'], 'token' => $row['api_token']]);
"""
        )
        self.assertFalse(data["enabled"])
        self.assertEqual(data["token"], "original-secret-token")

    def test_filter_drops_port_53(self) -> None:
        data = self._php(
            "echo json_encode(auth_turn_parse_cf_response(file_get_contents(getenv('NEXVUE_TURN_HTTP_STUB'))));"
        )
        urls = []
        for srv in data:
            urls.extend(srv["urls"])
        self.assertTrue(any(":3478" in u for u in urls))
        self.assertTrue(any(":443" in u for u in urls))
        self.assertFalse(any(":53" in u for u in urls))

    def test_parse_object_shape(self) -> None:
        obj_path = Path(self._td.name) / "obj.json"
        obj_path.write_text(json.dumps(CF_OBJECT), encoding="utf-8")
        data = self._php(
            f"echo json_encode(auth_turn_parse_cf_response(file_get_contents('{obj_path.as_posix()}')));"
        )
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["username"], "user-b")
        self.assertFalse(any(":53" in u for u in data[0]["urls"]))

    def test_mint_uses_stub_and_cache(self) -> None:
        data = self._php(
            """
auth_turn_put([
  'enabled' => true,
  'key_id' => 'turnkey12-mint-0000000000000000',
  'api_token' => 'cf-token-secret-value-ok',
]);
$a = auth_turn_mint(true);
$b = auth_turn_mint(false);
$view = auth_turn_ice_servers_for_viewer();
echo json_encode([
  'count' => count($a['ice_servers']),
  'expires' => $a['expires_at'],
  'cached_same' => $a['ice_servers'] === $b['ice_servers'],
  'view_on' => $view['enabled'],
  'view_count' => count($view['ice_servers']),
]);
"""
        )
        self.assertGreaterEqual(data["count"], 1)
        self.assertTrue(data["expires"])
        self.assertTrue(data["cached_same"])
        self.assertTrue(data["view_on"])
        self.assertEqual(data["view_count"], data["count"])

    def test_viewer_empty_when_disabled(self) -> None:
        data = self._php(
            """
auth_turn_put([
  'enabled' => false,
  'key_id' => 'turnkey12-off-00000000000000000',
  'api_token' => 'cf-token-secret-value-ok',
]);
$view = auth_turn_ice_servers_for_viewer();
echo json_encode($view);
"""
        )
        self.assertFalse(data["enabled"])
        self.assertEqual(data["ice_servers"], [])

    def test_mint_fail_does_not_break_viewer(self) -> None:
        data = self._php(
            """
auth_turn_put([
  'enabled' => true,
  'key_id' => 'turnkey12-fail-0000000000000000',
  'api_token' => 'cf-token-secret-value-ok',
]);
$view = auth_turn_ice_servers_for_viewer();
echo json_encode($view);
""",
            extra_env={"NEXVUE_TURN_HTTP_FAIL": "1", "NEXVUE_TURN_HTTP_STUB": ""},
        )
        self.assertTrue(data["enabled"])
        self.assertEqual(data["ice_servers"], [])


if __name__ == "__main__":
    unittest.main()
