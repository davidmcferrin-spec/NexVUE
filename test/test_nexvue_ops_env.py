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

    def test_unquotes_double_quoted_value(self):
        keys = mod.parse_env_text('CHANNEL_ALIAS="TVU 35"\n')
        self.assertEqual(keys["CHANNEL_ALIAS"], "TVU 35")

    def test_unquotes_single_quoted_value(self):
        keys = mod.parse_env_text("CHANNEL_ALIAS='Cam 1 (Main)'\n")
        self.assertEqual(keys["CHANNEL_ALIAS"], "Cam 1 (Main)")

    def test_hash_inside_quotes_is_not_a_comment(self):
        keys = mod.parse_env_text('CHANNEL_ALIAS="Feed #2 west"\n')
        self.assertEqual(keys["CHANNEL_ALIAS"], "Feed #2 west")


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
        # Values with spaces MUST be quoted: the systemd unit sources the env
        # file through bash, where `CHANNEL_ALIAS=Prompter A` runs `A` as a
        # command and drops the alias (the "TVU 35" field incident).
        out = mod.apply_patch(SAMPLE, {"CHANNEL_ALIAS": "Prompter A"})
        self.assertIn("# --- Ops UI ---", out)
        self.assertIn('CHANNEL_ALIAS="Prompter A"', out)

    def test_rejects_non_editable(self):
        with self.assertRaises(ValueError):
            mod.apply_patch(SAMPLE, {"DEVICE_NUMBER": "9"})

    def test_audio_layout_editable(self):
        out = mod.apply_patch(SAMPLE, {"AUDIO_LAYOUT": "51_sap"})
        self.assertIn("AUDIO_LAYOUT=51_sap", out)
        self.assertEqual(mod.sanitize_value("AUDIO_LAYOUT", "5.1"), "51")
        self.assertEqual(mod.sanitize_value("AUDIO_CHANNELS", "4"), "4")
        with self.assertRaises(ValueError):
            mod.sanitize_value("AUDIO_LAYOUT", "dolby")

    def test_rejects_shell_metachar(self):
        with self.assertRaises(ValueError):
            mod.apply_patch(SAMPLE, {"EXTRA_ENC_ARGS": "x;rm"})

    def test_extra_enc_args_rejects_pipeline_separator(self):
        # Independent of env-file shell-quoting: EXTRA_ENC_ARGS flows
        # UNQUOTED into a live gst-launch-1.0 pipeline description (see
        # nexvue-encode.sh). A literal '!' there splices in a whole new
        # GStreamer element/branch after the encoder — the env-file
        # double-quoting fix does not (and cannot) protect against this,
        # since it's a downstream, separate use of the same value.
        with self.assertRaises(ValueError):
            mod.sanitize_value(
                "EXTRA_ENC_ARGS",
                "cpb-size=2000 ! filesink location=/tmp/evil",
            )
        # Legitimate multi-property tuning must still work.
        self.assertEqual(
            mod.sanitize_value("EXTRA_ENC_ARGS", "cpb-size=2000 vbv-init=0.9"),
            "cpb-size=2000 vbv-init=0.9",
        )

    def test_alias_sanitized(self):
        out = mod.apply_patch(SAMPLE, {"CHANNEL_ALIAS": "Cam 1 (Main)"})
        self.assertIn('CHANNEL_ALIAS="Cam 1 (Main)"', out)
        with self.assertRaises(ValueError):
            mod.apply_patch(SAMPLE, {"CHANNEL_ALIAS": "bad`tick"})

    def test_simple_values_stay_unquoted(self):
        out = mod.apply_patch(SAMPLE, {"BITRATE_KBPS": "2500", "LO_FPS": "30000/1001"})
        self.assertIn("BITRATE_KBPS=2500\n", out)
        self.assertIn("LO_FPS=30000/1001\n", out)

    def test_lo_fps_rejects_freeform(self):
        # Bare integers used to be written and become framerate=(int)60 in Gst.
        self.assertEqual(mod.sanitize_value("LO_FPS", "60"), "60000/1001")
        self.assertEqual(mod.sanitize_value("LO_FPS", "30"), "30000/1001")
        self.assertEqual(mod.sanitize_value("LO_FPS", "29.97"), "30000/1001")
        with self.assertRaises(ValueError):
            mod.sanitize_value("LO_FPS", "24")
        self.assertEqual(mod.sanitize_value("LO_FPS", "60000/1001"), "60000/1001")
        self.assertEqual(mod.sanitize_value("LO_FPS", ""), "")

    def test_lo_geometry_must_be_even(self):
        with self.assertRaises(ValueError):
            mod.sanitize_value("LO_WIDTH", "641")
        self.assertEqual(mod.sanitize_value("LO_WIDTH", "640"), "640")
        self.assertEqual(mod.sanitize_value("LO_HEIGHT", ""), "")

    def test_lo_target_usage_range(self):
        with self.assertRaises(ValueError):
            mod.sanitize_value("LO_TARGET_USAGE", "9")
        self.assertEqual(mod.sanitize_value("LO_TARGET_USAGE", "7"), "7")

    def test_rejects_quotes_in_non_alias_values(self):
        with self.assertRaises(ValueError):
            mod.apply_patch(SAMPLE, {"EXTRA_ENC_ARGS": 'ref-frames="2"'})

    def test_quoted_alias_roundtrip_and_bash_source(self):
        out = mod.apply_patch(SAMPLE, {"CHANNEL_ALIAS": "TVU 35"})
        self.assertIn('CHANNEL_ALIAS="TVU 35"', out)
        keys = mod.parse_env_text(out)
        self.assertEqual(keys["CHANNEL_ALIAS"], "TVU 35")
        # The file must actually source cleanly in bash (how the systemd unit
        # consumes it) and yield the alias intact.
        import shutil
        import subprocess
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not available")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "0.env"
            p.write_text(out, encoding="utf-8")
            r = subprocess.run(
                [bash, "-c", 'set -a; . "$1"; printf %s "$CHANNEL_ALIAS"', "_", str(p)],
                capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout, "TVU 35")
            self.assertEqual(r.stderr, "")

    def test_roundtrip_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "0.env"
            p.write_text(SAMPLE, encoding="utf-8")
            text = p.read_text(encoding="utf-8")
            new = mod.apply_patch(text, {
                "ENABLE_AUDIO": "false",
                "SIGNAL_LOSS_DEBOUNCE_S": "15",
            })
            p.write_text(new, encoding="utf-8")
            keys = mod.parse_env_text(p.read_text(encoding="utf-8"))
            self.assertEqual(keys["ENABLE_AUDIO"], "false")
            self.assertEqual(keys["SIGNAL_LOSS_DEBOUNCE_S"], "15")
            self.assertEqual(keys["CHANNEL_PATH"], "ch0")

    def test_max_devices_not_channel_editable(self):
        with self.assertRaises(ValueError):
            mod.apply_patch(SAMPLE, {"MAX_DEVICES": "4"})

    def test_factory_defaults_blank_all_editable_keys(self):
        # Settings "Factory defaults…" sends channel_put with EVERY editable
        # key set to "" — the blank-means-default contract. Every sanitizer
        # must accept empty, identity keys must survive, and the result must
        # still source cleanly in bash with all values empty (so the encoder
        # script's ${VAR:-default} fallbacks take over).
        patch = {k: "" for k in mod.EDITABLE_KEYS}
        out = mod.apply_patch(SAMPLE, patch)
        keys = mod.parse_env_text(out)
        self.assertEqual(keys["DEVICE_NUMBER"], "0")
        self.assertEqual(keys["CHANNEL_PATH"], "ch0")
        for k in mod.EDITABLE_KEYS:
            self.assertEqual(keys.get(k, ""), "", f"{k} not blanked")
        import shutil
        import subprocess
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not available")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "0.env"
            p.write_text(out, encoding="utf-8")
            r = subprocess.run(
                [bash, "-c",
                 'set -a; . "$1"; printf %s "${BITRATE_KBPS:-5000}:${ENABLE_AUDIO:-true}"',
                 "_", str(p)],
                capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout, "5000:true")


if __name__ == "__main__":
    unittest.main()
