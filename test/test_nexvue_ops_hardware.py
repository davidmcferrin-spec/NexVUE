#!/usr/bin/env python3
"""
Unit tests for Settings → Card / encode slots
(nexvue-ops-hardware-write.py + PHP helpers in nexvue-ops.php / auth-lib).

Run: python3 test/test_nexvue_ops_hardware.py
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
WRITE_PY = ROOT / "nexvue-ops-hardware-write.py"
OPS_PHP = ROOT / "web-node" / "nexvue-ops.php"
AUTH_PHP = ROOT / "web-node" / "nexvue-auth-lib.php"
SETUP = ROOT / "setup.sh"
SUDOERS = ROOT / "nexvue-ops.sudoers"
CHANNELS_HTML = ROOT / "web-node" / "channels.html"
EXAMPLE = ROOT / "channels-example.env"
PHP = shutil.which("php")


def _load_write():
    spec = importlib.util.spec_from_file_location("nexvue_ops_hardware_write", WRITE_PY)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestSanitize(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_write()

    def test_slots_ok(self) -> None:
        self.assertEqual(self.mod.sanitize_slots(8), 8)
        self.assertEqual(self.mod.sanitize_slots("4"), 4)
        self.assertEqual(self.mod.sanitize_slots(2), 2)

    def test_slots_rejects_bool_and_range(self) -> None:
        with self.assertRaises(ValueError):
            self.mod.sanitize_slots(True)
        with self.assertRaises(ValueError):
            self.mod.sanitize_slots(0)
        with self.assertRaises(ValueError):
            self.mod.sanitize_slots(9)
        with self.assertRaises(ValueError):
            self.mod.sanitize_slots("nope")


class TestEnvPatch(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_write()

    def test_updates_existing_keys(self) -> None:
        env = "MAX_DEVICES=8\nMAX_CHANNELS=8\nNEXVUE_PUBLIC_IP=\n"
        out = self.mod.apply_env_patch(env, 4)
        self.assertIn("MAX_DEVICES=4\n", out)
        self.assertIn("MAX_CHANNELS=4\n", out)
        self.assertIn("NEXVUE_PUBLIC_IP=\n", out)
        self.assertNotIn("MAX_DEVICES=8", out)

    def test_appends_when_missing(self) -> None:
        out = self.mod.apply_env_patch("NEXVUE_API_KEY=x\n", 8)
        self.assertIn("# --- Card / encode slots", out)
        self.assertIn("MAX_DEVICES=8\n", out)
        self.assertIn("MAX_CHANNELS=8\n", out)

    def test_strip_channel_max_devices(self) -> None:
        text = "DEVICE_NUMBER=1\nMAX_DEVICES=8\n#MAX_DEVICES=4\nCHANNEL_PATH=ch1\n"
        out, n = self.mod.strip_channel_max_devices(text)
        self.assertEqual(n, 1)
        self.assertNotIn("MAX_DEVICES=8", out)
        self.assertIn("#MAX_DEVICES=4", out)
        self.assertIn("DEVICE_NUMBER=1", out)

    def test_seed_rewrites_identity(self) -> None:
        tmpl = EXAMPLE.read_text(encoding="utf-8")
        out = self.mod.seed_channel_env(3, tmpl)
        self.assertRegex(out, r"(?m)^DEVICE_NUMBER=3\s*$")
        self.assertRegex(out, r"(?m)^CHANNEL_PATH=ch3\s*$")
        self.assertNotRegex(out, r"(?m)^MAX_DEVICES=")


class TestCliApply(unittest.TestCase):
    def test_cli_writes_env_and_seeds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env_path = root / "nexvue.env"
            ch_dir = root / "channels"
            ch_dir.mkdir()
            (ch_dir / "0.env").write_text(
                "DEVICE_NUMBER=0\nCHANNEL_PATH=ch0\nMAX_DEVICES=8\n",
                encoding="utf-8",
            )
            env_path.write_text("MAX_DEVICES=8\nMAX_CHANNELS=8\n", encoding="utf-8")
            env = os.environ.copy()
            env["NEXVUE_STATION_ENV"] = str(env_path)
            env["NEXVUE_CHANNELS_DIR"] = str(ch_dir)
            env["NEXVUE_CHANNELS_EXAMPLE"] = str(EXAMPLE)
            env["NEXVUE_HARDWARE_SKIP_SYSTEMCTL"] = "1"
            r = subprocess.run(
                [os.environ.get("PYTHON", None) or __import__("sys").executable, str(WRITE_PY)],
                input='{"slots": 4}',
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            data = json.loads(r.stdout)
            self.assertTrue(data["ok"])
            self.assertEqual(data["slots"], 4)
            self.assertEqual(data["seeded"], [1, 2, 3])
            self.assertGreaterEqual(data["stripped"], 1)
            text = env_path.read_text(encoding="utf-8")
            self.assertIn("MAX_DEVICES=4\n", text)
            self.assertIn("MAX_CHANNELS=4\n", text)
            self.assertTrue((ch_dir / "3.env").is_file())
            self.assertNotIn("MAX_DEVICES=", (ch_dir / "0.env").read_text(encoding="utf-8"))
            self.assertIn("CHANNEL_PATH=ch3", (ch_dir / "3.env").read_text(encoding="utf-8"))
            self.assertFalse((ch_dir / "4.env").is_file())


@unittest.skipUnless(PHP, "php not on PATH")
class TestPhpHelpers(unittest.TestCase):
    def _php(self, body: str, env_text: str = "MAX_DEVICES=8\nMAX_CHANNELS=8\n") -> dict:
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / "nexvue.env"
            env_path.write_text(env_text, encoding="utf-8")
            ops = OPS_PHP.as_posix().replace("\\", "/")
            auth = AUTH_PHP.as_posix().replace("\\", "/")
            env_p = env_path.as_posix()
            code = f"""
putenv('MAX_CHANNELS');
putenv('MAX_DEVICES');
putenv('NEXVUE_STATION_ENV={env_p}');
include '{auth}';
include '{ops}';
{body}
"""
            env = os.environ.copy()
            env.pop("NEXVUE_OPS_HTTP", None)
            env.pop("MAX_CHANNELS", None)
            env.pop("MAX_DEVICES", None)
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

    def test_auth_max_channels_from_env(self) -> None:
        data = self._php(
            "echo json_encode(['n' => auth_max_channels(), 'id' => auth_max_channel_id(), 'all' => auth_all_channel_bases()]);",
            env_text="MAX_CHANNELS=4\nMAX_DEVICES=4\n",
        )
        self.assertEqual(data["n"], 4)
        self.assertEqual(data["id"], 3)
        self.assertEqual(data["all"], ["ch0", "ch1", "ch2", "ch3"])

    def test_auth_max_channels_defaults_eight(self) -> None:
        data = self._php(
            "echo json_encode(['n' => auth_max_channels()]);",
            env_text="# empty\n",
        )
        self.assertEqual(data["n"], 8)

    def test_auth_max_falls_back_to_devices(self) -> None:
        data = self._php(
            "echo json_encode(['n' => auth_max_channels()]);",
            env_text="MAX_DEVICES=2\n",
        )
        self.assertEqual(data["n"], 2)

    def test_channel_id_ok_respects_live_max(self) -> None:
        data = self._php(
            "echo json_encode(["
            "channel_id_ok(3),"
            "channel_id_ok(4),"
            "channel_id_ok(7)"
            "]);",
            env_text="MAX_CHANNELS=4\n",
        )
        self.assertEqual(data, [True, False, False])

    def test_hardware_read_settings_shape(self) -> None:
        data = self._php(
            "echo json_encode(hardware_read_settings());",
            env_text="MAX_CHANNELS=8\n",
        )
        self.assertEqual(data["slots"], 8)
        self.assertEqual(data["presets"], [2, 4, 8])
        self.assertIn("detected_devices", data)


class TestWiring(unittest.TestCase):
    def test_setup_lists_helpers(self) -> None:
        text = SETUP.read_text(encoding="utf-8")
        self.assertIn("nexvue-ops-hardware-write.sh", text)
        self.assertIn("nexvue-ops-hardware-write.py", text)
        self.assertIn("channels-example.env", text)

    def test_sudoers_allowlists_write(self) -> None:
        text = SUDOERS.read_text(encoding="utf-8")
        self.assertIn("nexvue-ops-hardware-write.sh", text)

    def test_ops_php_admin_only(self) -> None:
        text = OPS_PHP.read_text(encoding="utf-8")
        self.assertIn("'hardware_get', 'hardware_put'", text)
        self.assertIn("function hardware_read_settings()", text)

    def test_settings_panel_admin_gated(self) -> None:
        text = CHANNELS_HTML.read_text(encoding="utf-8")
        self.assertIn('id="hardware-panel"', text)
        self.assertIn("hardware_put", text)
        self.assertIn("hardware_get", text)
        self.assertIn("Card / encode slots", text)


if __name__ == "__main__":
    unittest.main()
