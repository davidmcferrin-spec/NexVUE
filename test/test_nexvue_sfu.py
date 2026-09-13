#!/usr/bin/env python3
"""
Unit tests for Cloudflare Stream hybrid helpers (auth.db + WHIP publisher
config). TURN stays independent.

Requires `php` on PATH with openssl + sqlite3 for the PHP cases. File-level
and publisher-config tests always run.

Run: python3 test/test_nexvue_sfu.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "web-node" / "nexvue-auth-lib.php"
PUBLISH_PY = ROOT / "nexvue-sfu-publish.py"
PHP = shutil.which("php")

LIVE_INPUT = {
    "success": True,
    "result": {
        "uid": "uid-ch0-test",
        "webRTC": {"url": "https://customer.example.com/uid-ch0-test/webRTC/publish"},
        "webRTCPlayback": {"url": "https://customer.example.com/uid-ch0-test/webRTC/play"},
    },
}


class TestSfuUiWiring(unittest.TestCase):
    def test_settings_panel_and_ops_actions(self) -> None:
        html = (ROOT / "web-node" / "channels.html").read_text(encoding="utf-8")
        self.assertIn('id="sfu-panel"', html)
        self.assertIn("Cloudflare Stream", html)
        self.assertIn('id="turn-panel"', html)
        self.assertIn('api("sfu_put"', html)
        ops = (ROOT / "web-node" / "nexvue-ops.php").read_text(encoding="utf-8")
        self.assertIn("'sfu_get', 'sfu_put', 'sfu_test'", ops)
        lib = LIB.read_text(encoding="utf-8")
        self.assertIn("NEXVUE_AUTH_SCHEMA_VERSION = 6", lib)
        self.assertIn("function auth_sfu_put", lib)
        self.assertIn("function auth_sfu_use_for_session", lib)
        self.assertNotIn("NEXVUE_SFU_ENABLE", lib)
        auth = (ROOT / "web-node" / "nexvue-auth.php").read_text(encoding="utf-8")
        self.assertIn("'egress' => $egress", auth)
        self.assertIn("sfu_whep", auth)
        gate = (ROOT / "web-node" / "nexvue-auth-gate.js").read_text(encoding="utf-8")
        self.assertIn("whepExchange", gate)
        self.assertIn('sess.egress === "sfu"', gate)
        player = (ROOT / "web-node" / "index.html").read_text(encoding="utf-8")
        self.assertIn("whepExchange", player)
        mv = (ROOT / "web-node" / "multiview.html").read_text(encoding="utf-8")
        self.assertIn("whepExchange", mv)
        hb = (ROOT / "nexvue-portal-heartbeat.php").read_text(encoding="utf-8")
        self.assertIn("auth_sfu_heartbeat_payload", hb)
        portal_api = (ROOT / "web-portal" / "nexvue-portal-api.php").read_text(encoding="utf-8")
        self.assertIn("portal_station_sfu_store", portal_api)
        self.assertIn("sfu_whep", portal_api)
        watch = (ROOT / "web-portal" / "watch.html").read_text(encoding="utf-8")
        self.assertIn('jwtResp.egress === "sfu"', watch)
        setup = (ROOT / "setup.sh").read_text(encoding="utf-8")
        self.assertIn("nexvue-sfu-publish.py", setup)
        self.assertIn("nexvue-sfu-publish.service", setup)
        self.assertTrue((ROOT / "nexvue-sfu-publish.service").is_file())

    def test_public_helpers_do_not_name_publish_urls(self) -> None:
        lib = LIB.read_text(encoding="utf-8")
        self.assertIn("function auth_sfu_public", lib)
        self.assertIn("function auth_sfu_play_map", lib)
        auth = (ROOT / "web-node" / "nexvue-auth.php").read_text(encoding="utf-8")
        self.assertNotIn("publish_url", auth)
        ops = (ROOT / "web-node" / "nexvue-ops.php").read_text(encoding="utf-8")
        self.assertNotIn("publish_url", ops)


def _load_publisher():
    spec = importlib.util.spec_from_file_location("nexvue_sfu_publish", PUBLISH_PY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSfuPublishConfig(unittest.TestCase):
    def test_missing_file_is_off(self) -> None:
        mod = _load_publisher()
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "nope.json"
            cfg = mod.load_publish_config(missing)
        self.assertEqual(cfg["mode"], "off")
        self.assertEqual(cfg["paths"], {})

    def test_loads_hybrid_paths_and_jwt(self) -> None:
        mod = _load_publisher()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sfu-publish.json"
            p.write_text(
                json.dumps({
                    "mode": "hybrid",
                    "rtsp_jwt": "abc.def.ghi",
                    "paths": {
                        "ch0": {
                            "publish_url": "https://customer.example.com/pub",
                            "rtsp_url": "rtsp://127.0.0.1:8554/ch0",
                            "play_url": "https://should-not-load.example/play",
                        },
                        "bad": {"publish_url": "", "rtsp_url": "rtsp://x"},
                    },
                }),
                encoding="utf-8",
            )
            cfg = mod.load_publish_config(p)
        self.assertEqual(cfg["mode"], "hybrid")
        self.assertEqual(cfg["rtsp_jwt"], "abc.def.ghi")
        self.assertEqual(set(cfg["paths"]), {"ch0"})
        self.assertNotIn("play_url", cfg["paths"]["ch0"])

    def test_rtsp_url_with_jwt(self) -> None:
        mod = _load_publisher()
        url = mod.rtsp_url_with_jwt("rtsp://127.0.0.1:8554/ch0", "a/b+c=")
        self.assertTrue(url.startswith("rtsp://127.0.0.1:8554/ch0?jwt="))
        self.assertNotIn("a/b+c=", url)
        same = mod.rtsp_url_with_jwt("rtsp://127.0.0.1:8554/ch0?jwt=already", "other")
        self.assertEqual(same, "rtsp://127.0.0.1:8554/ch0?jwt=already")


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
class TestNexVueSfu(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.auth_dir = Path(self._td.name) / "auth"
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        self.db = Path(self._td.name) / "auth.db"
        self.env = Path(self._td.name) / "nexvue.env"
        self.env.write_text("# test — Stream must not write here\n", encoding="utf-8")
        self.pub_file = Path(self._td.name) / "sfu-publish.json"
        self.stub = Path(self._td.name) / "cf.json"
        self.stub.write_text(json.dumps(LIVE_INPUT), encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    def _php(self, body: str, extra_env: dict | None = None) -> dict:
        lib = LIB.as_posix()
        code = f"""
putenv('NEXVUE_AUTH_DB={self.db.as_posix()}');
putenv('NEXVUE_AUTH_DIR={self.auth_dir.as_posix()}');
putenv('NEXVUE_STATION_ENV={self.env.as_posix()}');
putenv('NEXVUE_SFU_PUBLISH_FILE={self.pub_file.as_posix()}');
include '{lib}';
auth_migrate();
{body}
"""
        env = os.environ.copy()
        env["NEXVUE_AUTH_DB"] = str(self.db)
        env["NEXVUE_AUTH_DIR"] = str(self.auth_dir)
        env["NEXVUE_STATION_ENV"] = str(self.env)
        env["NEXVUE_SFU_PUBLISH_FILE"] = str(self.pub_file)
        if extra_env:
            env.update(extra_env)
        r = subprocess.run(
            [PHP, "-d", "display_errors=0", "-r", code],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
        out = (r.stdout or "").strip()
        if r.returncode != 0:
            self.fail(f"php failed: {r.stderr!r} stdout={out!r}")
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            self.fail(f"expected JSON, got: {out!r}\nstderr={r.stderr!r}")

    def test_schema_creates_sfu_tables(self) -> None:
        data = self._php(
            "echo json_encode(['ver' => (int)auth_db()->querySingle('PRAGMA user_version'), "
            "'pub' => auth_sfu_public()]);"
        )
        self.assertGreaterEqual(data["ver"], 5)
        self.assertEqual(data["pub"]["mode"], "off")
        self.assertFalse(data["pub"]["has_token"])
        self.assertNotIn("api_token", data["pub"])
        self.assertNotIn("publish_url", data["pub"])

    def test_put_rejects_hybrid_without_token(self) -> None:
        data = self._php(
            """
try {
  auth_sfu_put(['mode' => 'hybrid', 'account_id' => '0123456789abcdef0123456789abcdef', 'api_token' => '']);
  echo json_encode(['ok' => true]);
} catch (InvalidArgumentException $e) {
  echo json_encode(['ok' => false, 'error' => $e->getMessage()]);
}
"""
        )
        self.assertFalse(data["ok"])
        self.assertIn("token", data["error"].lower())

    def test_put_provisions_and_hides_secrets(self) -> None:
        data = self._php(
            """
$pub = auth_sfu_put([
  'mode' => 'hybrid',
  'account_id' => '0123456789abcdef0123456789abcdef',
  'api_token' => 'secret-stream-token-ok',
]);
$hb = auth_sfu_heartbeat_payload();
$useShare = auth_sfu_use_for_session(['auth' => 'share'], 'ch0');
$useUser = auth_sfu_use_for_session(['auth' => 'session', 'role' => 'admin'], 'ch0');
$env = file_get_contents(getenv('NEXVUE_STATION_ENV'));
$file = file_get_contents(getenv('NEXVUE_SFU_PUBLISH_FILE'));
echo json_encode([
  'pub' => $pub,
  'hb' => $hb,
  'use_share' => $useShare,
  'use_user' => $useUser,
  'env' => $env,
  'file' => $file,
]);
""",
            extra_env={"NEXVUE_SFU_HTTP_STUB": str(self.stub)},
        )
        self.assertEqual(data["pub"]["mode"], "hybrid")
        self.assertTrue(data["pub"]["has_token"])
        self.assertGreater(data["pub"]["input_count"], 0)
        self.assertNotIn("secret-stream-token-ok", json.dumps(data["pub"]))
        self.assertEqual(data["hb"]["mode"], "hybrid")
        self.assertIn("ch0", data["hb"]["play"])
        self.assertTrue(data["hb"]["play"]["ch0"].endswith("/webRTC/play"))
        self.assertNotIn("publish", json.dumps(data["hb"]))
        self.assertTrue(data["use_share"])
        self.assertFalse(data["use_user"])
        self.assertNotIn("secret-stream-token-ok", data["env"])
        file_obj = json.loads(data["file"])
        self.assertEqual(file_obj["mode"], "hybrid")
        self.assertIn("ch0", file_obj["paths"])
        self.assertIn("publish_url", file_obj["paths"]["ch0"])
        self.assertNotIn("play_url", file_obj["paths"]["ch0"])
        dumped = json.dumps(file_obj)
        self.assertNotIn("secret-stream-token-ok", dumped)

    def test_sfu_mode_uses_stream_for_everyone(self) -> None:
        data = self._php(
            """
auth_sfu_put([
  'mode' => 'sfu',
  'account_id' => '0123456789abcdef0123456789abcdef',
  'api_token' => 'secret-stream-token-ok',
]);
echo json_encode([
  'share' => auth_sfu_use_for_session(['auth' => 'share'], 'ch0'),
  'user' => auth_sfu_use_for_session(['auth' => 'session'], 'ch0'),
]);
""",
            extra_env={"NEXVUE_SFU_HTTP_STUB": str(self.stub)},
        )
        self.assertTrue(data["share"])
        self.assertTrue(data["user"])

    def test_parse_live_input(self) -> None:
        data = self._php(
            "echo json_encode(auth_sfu_parse_live_input((string)file_get_contents(getenv('NEXVUE_SFU_HTTP_STUB'))));",
            extra_env={"NEXVUE_SFU_HTTP_STUB": str(self.stub)},
        )
        self.assertEqual(data["uid"], "uid-ch0-test")
        self.assertTrue(data["publish_url"].endswith("/webRTC/publish"))
        self.assertTrue(data["play_url"].endswith("/webRTC/play"))

    def test_whep_exchange_rejects_bad_sdp(self) -> None:
        data = self._php(
            """
try {
  auth_sfu_whep_exchange('https://example.com/play', 'not-sdp');
  echo json_encode(['ok' => true]);
} catch (InvalidArgumentException $e) {
  echo json_encode(['ok' => false, 'error' => $e->getMessage()]);
}
"""
        )
        self.assertFalse(data["ok"])


if __name__ == "__main__":
    unittest.main()
