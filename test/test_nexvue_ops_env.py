#!/usr/bin/env python3
"""
Unit tests for nexvue-ops-env-update.py — parse + line-based patch.

Run: python3 test/test_nexvue_ops_env.py
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SPEC_PATH = Path(__file__).resolve().parent.parent / "nexvue-ops-env-update.py"
spec = importlib.util.spec_from_file_location("nexvue_ops_env_update", SPEC_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules["nexvue_ops_env_update"] = mod
spec.loader.exec_module(mod)


SAMPLE = """# header
DEVICE_NUMBER=0
CHANNEL_PATH=ch0

#MAX_DEVICES=4
DEINT_FIELDS=top
BITRATE_KBPS=4000
ENABLE_AUDIO=true
#LO_ENABLE=false
LO_PRESET=360p
"""


class TestParse(unittest.TestCase):
    def test_skips_commented_assignments(self):
        keys = mod.parse_env_text(SAMPLE)
        self.assertEqual(keys["DEVICE_NUMBER"], "0")
        self.assertEqual(keys["DEINT_FIELDS"], "top")
        self.assertNotIn("MAX_DEVICES", keys)
        self.assertNotIn("LO_ENABLE", keys)

    def test_strips_inline_comment_on_active_line(self):
        keys = mod.parse_env_text("BITRATE_KBPS=4000   # note\n")
        self.assertEqual(keys["BITRATE_KBPS"], "4000")


class TestApplyPatch(unittest.TestCase):
    def test_rewrites_active_key(self):
        out = mod.apply_patch(SAMPLE, {"BITRATE_KBPS": "2500"})
        self.assertIn("BITRATE_KBPS=2500", out)
        self.assertNotIn("BITRATE_KBPS=4000", out)
        self.assertIn("# header", out)

    def test_uncomments_key(self):
        out = mod.apply_patch(SAMPLE, {"LO_ENABLE": "true"})
        self.assertIn("LO_ENABLE=true", out)
        self.assertNotIn("#LO_ENABLE=", out)

    def test_appends_missing_key(self):
        out = mod.apply_patch(SAMPLE, {"CHANNEL_ALIAS": "Prompter A"})
        self.assertIn("# --- Ops UI ---", out)
        self.assertIn("CHANNEL_ALIAS=Prompter A", out)

    def test_rejects_non_editable(self):
        with self.assertRaises(ValueError):
            mod.apply_patch(SAMPLE, {"DEVICE_NUMBER": "9"})

    def test_rejects_shell_metachar(self):
        with self.assertRaises(ValueError):
            mod.apply_patch(SAMPLE, {"EXTRA_ENC_ARGS": "x;rm"})

    def test_alias_sanitized(self):
        out = mod.apply_patch(SAMPLE, {"CHANNEL_ALIAS": "Cam 1 (Main)"})
        self.assertIn("CHANNEL_ALIAS=Cam 1 (Main)", out)
        with self.assertRaises(ValueError):
            mod.apply_patch(SAMPLE, {"CHANNEL_ALIAS": "bad`tick"})

    def test_roundtrip_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "0.env"
            p.write_text(SAMPLE, encoding="utf-8")
            text = p.read_text(encoding="utf-8")
            new = mod.apply_patch(text, {"MAX_DEVICES": "8", "ENABLE_AUDIO": "false"})
            p.write_text(new, encoding="utf-8")
            keys = mod.parse_env_text(p.read_text(encoding="utf-8"))
            self.assertEqual(keys["MAX_DEVICES"], "8")
            self.assertEqual(keys["ENABLE_AUDIO"], "false")
            self.assertEqual(keys["CHANNEL_PATH"], "ch0")


if __name__ == "__main__":
    unittest.main()
