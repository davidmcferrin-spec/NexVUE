#!/usr/bin/env python3
"""
Unit tests for public-reachability write (nexvue-ops-network-write.py)
and PHP helpers in nexvue-ops.php.

Run: python3 test/test_nexvue_ops_network.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WRITE_PY = ROOT / "nexvue-ops-network-write.py"
ICE_PY = ROOT / "nexvue-mediamtx-ice-patch.py"
OPS_PHP = ROOT / "web-node" / "nexvue-ops.php"
SETUP = ROOT / "setup.sh"
SUDOERS = ROOT / "nexvue-ops.sudoers"
CHANNELS = ROOT / "web-node" / "channels.html"
PHP = shutil.which("php")


def _load_write():
    spec = importlib.util.spec_from_file_location("nexvue_ops_network_write", WRITE_PY)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestSanitize(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_write()

    def test_hostname_fqdn(self) -> None:
        self.assertEqual(self.mod.sanitize_hostname("NexVUE.Example.COM"), "nexvue.example.com")

    def test_hostname_blank(self) -> None:
        self.assertEqual(self.mod.sanitize_hostname("  "), "")

    def test_hostname_rejects_url(self) -> None:
        with self.assertRaises(ValueError):
            self.mod.sanitize_hostname("https://nexvue.example.com")

    def test_hostname_rejects_ip(self) -> None:
        with self.assertRaises(ValueError):
            self.mod.sanitize_hostname("203.0.113.40")

    def test_hostname_rejects_spaces(self) -> None:
        with self.assertRaises(ValueError):
            self.mod.sanitize_hostname("not a host")

    def test_ip_ok(self) -> None:
        self.assertEqual(self.mod.sanitize_ip(" 203.0.113.40 "), "203.0.113.40")

    def test_ip_blank(self) -> None:
        self.assertEqual(self.mod.sanitize_ip(""), "")

    def test_ip_rejects_loopback(self) -> None:
        with self.assertRaises(ValueError):
            self.mod.sanitize_ip("127.0.0.1")

    def test_ip_rejects_link_local(self) -> None:
        with self.assertRaises(ValueError):
            self.mod.sanitize_ip("169.254.1.1")

    def test_ip_rejects_multicast(self) -> None:
        with self.assertRaises(ValueError):
            self.mod.sanitize_ip("224.0.0.1")

    def test_ip_allows_rfc1918(self) -> None:
        self.assertEqual(self.mod.sanitize_ip("10.98.41.152"), "10.98.41.152")


class TestApply(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_write()

    def test_writes_env_and_yml(self) -> None:
        env = "MAX_DEVICES=8\nMAX_CHANNELS=8\n"
        yml = "webrtcIPsFromInterfaces: yes\nwebrtcAdditionalHosts: [old.example.com]\n"
        new_env, new_yml = self.mod.apply("nexvue.example.com", "203.0.113.40", env, yml)
        self.assertIn("MAX_DEVICES=8\n", new_env)
        self.assertIn("NEXVUE_PUBLIC_HOSTNAME=nexvue.example.com\n", new_env)
        self.assertIn("NEXVUE_PUBLIC_IP=203.0.113.40\n", new_env)
        self.assertIn("# --- Public reachability", new_env)
        self.assertIn("webrtcAdditionalHosts: [nexvue.example.com, 203.0.113.40]", new_yml)
        self.assertNotIn("old.example.com", new_yml)

    def test_updates_existing_env_keys(self) -> None:
        env = "NEXVUE_PUBLIC_HOSTNAME=old.example.com\nNEXVUE_PUBLIC_IP=198.51.100.1\n"
        yml = "webrtcIPsFromInterfaces: yes\n"
        new_env, new_yml = self.mod.apply("", "", env, yml)
        self.assertIn("NEXVUE_PUBLIC_HOSTNAME=\n", new_env)
        self.assertIn("NEXVUE_PUBLIC_IP=\n", new_env)
        self.assertNotIn("old.example.com", new_env)
        self.assertIn("webrtcAdditionalHosts: []", new_yml)

    def test_cli_writes_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / "nexvue.env"
            yml_path = Path(td) / "mediamtx.yml"
            env_path.write_text("MAX_DEVICES=8\n", encoding="utf-8")
            yml_path.write_text("webrtcIPsFromInterfaces: yes\n", encoding="utf-8")
            env = os.environ.copy()
            env["NEXVUE_STATION_ENV"] = str(env_path)
            env["NEXVUE_MEDIAMTX_YML"] = str(yml_path)
            r = subprocess.run(
                [sys.executable, str(WRITE_PY)],
                input=json.dumps({"hostname": "edge.example.com", "ip": "192.0.2.8"}),
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertTrue(out["ok"])
            self.assertEqual(out["hostname"], "edge.example.com")
            self.assertIn("NEXVUE_PUBLIC_HOSTNAME=edge.example.com", env_path.read_text(encoding="utf-8"))
            self.assertIn(
                "webrtcAdditionalHosts: [edge.example.com, 192.0.2.8]",
                yml_path.read_text(encoding="utf-8"),
            )


@unittest.skipUnless(PHP and OPS_PHP.is_file(), "php CLI or nexvue-ops.php missing")
class TestOpsNetworkPhp(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.env = Path(self._td.name) / "nexvue.env"
        self.yml = Path(self._td.name) / "mediamtx.yml"

    def tearDown(self) -> None:
        self._td.cleanup()

    def _php(self, body: str, env_text: str = "", yml_text: str = "") -> dict:
        self.env.write_text(env_text, encoding="utf-8")
        self.yml.write_text(yml_text, encoding="utf-8")
        ops = OPS_PHP.as_posix().replace("\\", "/")
        env_p = self.env.as_posix()
        yml_p = self.yml.as_posix()
        code = f"""
putenv('NEXVUE_PUBLIC_HOSTNAME');
putenv('NEXVUE_PUBLIC_IP');
putenv('NEXVUE_STATION_ENV={env_p}');
putenv('NEXVUE_MEDIAMTX_YML={yml_p}');
include '{ops}';
{body}
"""
        env = os.environ.copy()
        env.pop("NEXVUE_OPS_HTTP", None)
        env.pop("NEXVUE_PUBLIC_HOSTNAME", None)
        env.pop("NEXVUE_PUBLIC_IP", None)
        r = subprocess.run(
            [PHP, "-d", "display_errors=stderr", "-r", code],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        if r.returncode != 0:
            self.fail(f"php failed ({r.returncode}): {r.stderr or r.stdout}")
        out = (r.stdout or "").strip()
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            self.fail(f"expected JSON, got: {out!r}\nstderr={r.stderr!r}")

    def test_sanitize_hostname(self) -> None:
        data = self._php("echo json_encode(['v' => network_sanitize_hostname('Edge.Example.COM')]);")
        self.assertEqual(data["v"], "edge.example.com")

    def test_sanitize_hostname_rejects_ip(self) -> None:
        data = self._php("""
try { network_sanitize_hostname('203.0.113.1'); echo json_encode(['ok'=>true]); }
catch (Throwable $e) { echo json_encode(['error'=>$e->getMessage()]); }
""")
        self.assertIn("Public IP", data.get("error", ""))

    def test_sanitize_ip_rejects_loopback(self) -> None:
        data = self._php("""
try { network_sanitize_ip('127.0.0.1'); echo json_encode(['ok'=>true]); }
catch (Throwable $e) { echo json_encode(['error'=>$e->getMessage()]); }
""")
        self.assertIn("Loopback", data.get("error", ""))

    def test_read_prefers_env(self) -> None:
        data = self._php(
            "echo json_encode(network_read_settings());",
            env_text="NEXVUE_PUBLIC_HOSTNAME=from.env.example\nNEXVUE_PUBLIC_IP=192.0.2.4\n",
            yml_text="webrtcAdditionalHosts: [yml.example.com, 198.51.100.2]\n",
        )
        self.assertEqual(data["hostname"], "from.env.example")
        self.assertEqual(data["ip"], "192.0.2.4")

    def test_read_falls_back_to_yml(self) -> None:
        data = self._php(
            "echo json_encode(network_read_settings());",
            env_text="MAX_DEVICES=8\n",
            yml_text="webrtcAdditionalHosts: [yml.example.com, 198.51.100.2]\n",
        )
        self.assertEqual(data["hostname"], "yml.example.com")
        self.assertEqual(data["ip"], "198.51.100.2")

    def test_parse_block_list(self) -> None:
        data = self._php(
            "echo json_encode(network_parse_additional_hosts(file_get_contents(network_mediamtx_yml_path())));",
            yml_text="webrtcAdditionalHosts:\n  - block.example.com\n  - 203.0.113.9\n",
        )
        self.assertEqual(data["hostname"], "block.example.com")
        self.assertEqual(data["ip"], "203.0.113.9")


class TestWiring(unittest.TestCase):
    def test_setup_lists_helpers(self) -> None:
        text = SETUP.read_text(encoding="utf-8")
        self.assertIn("nexvue-ops-network-write.sh", text)
        self.assertIn("nexvue-ops-network-write.py", text)
        self.assertIn("nexvue-mediamtx-ice-patch.py", text)

    def test_sudoers_allowlists_write(self) -> None:
        text = SUDOERS.read_text(encoding="utf-8")
        self.assertIn("nexvue-ops-network-write.sh", text)

    def test_ops_php_admin_only(self) -> None:
        text = OPS_PHP.read_text(encoding="utf-8")
        self.assertIn("'network_get', 'network_put'", text)
        self.assertIn("function network_read_settings()", text)

    def test_settings_panel_admin_gated(self) -> None:
        text = CHANNELS.read_text(encoding="utf-8")
        self.assertIn('id="network-panel"', text)
        self.assertIn('me.role === "admin"', text)
        self.assertIn("network_put", text)
        self.assertTrue(ICE_PY.is_file())


if __name__ == "__main__":
    unittest.main()
