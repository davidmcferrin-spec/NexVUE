#!/usr/bin/env python3
"""Unit tests for nexvue-support-bundle.py (redaction + hours gate).

Run: python3 test/test_nexvue_support_bundle.py
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "nexvue-support-bundle.py"
spec = importlib.util.spec_from_file_location("nexvue_support_bundle", SPEC_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules["nexvue_support_bundle"] = mod
spec.loader.exec_module(mod)


class TestRedact(unittest.TestCase):
    def test_url_userinfo(self):
        s = "publish srt://user:secret@10.0.0.1:9000?mode=caller"
        out = mod.redact_text(s)
        self.assertIn("srt://***:***@", out)
        self.assertNotIn("secret", out)

    def test_password_kv(self):
        out = mod.redact_text("SRT_PASSPHRASE=hunter2 EXTRA=ok")
        self.assertIn("SRT_PASSPHRASE=***", out)
        self.assertIn("EXTRA=ok", out)

    def test_redact_obj_uri_key(self):
        obj = {"SRT_URI": "srt://a:b@host/stream", "CHANNEL_ALIAS": "Cam 1"}
        out = mod.redact_obj(obj)
        self.assertEqual(out["CHANNEL_ALIAS"], "Cam 1")
        self.assertIn("***:***@", out["SRT_URI"])


class TestBuildBundle(unittest.TestCase):
    def test_rejects_bad_hours(self):
        with self.assertRaises(SystemExit):
            mod.build_bundle(hours=3, out_dir=Path(tempfile.mkdtemp()))

    def test_builds_zip_offline(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            # Fake etc/run so collectors don't explode on missing trees.
            etc = out / "etc"
            (etc / "channels").mkdir(parents=True)
            (etc / "nexvue.env").write_text("MAX_DEVICES=8\n", encoding="utf-8")
            (etc / "channels" / "0.env").write_text(
                'CHANNEL_PATH=ch0\nSRT_URI=srt://u:p@1.2.3.4:9000\n',
                encoding="utf-8",
            )
            run = out / "run"
            run.mkdir()
            (run / "captions").mkdir()
            (run / "captions" / "ch0.json").write_text(
                '{"text":"hi","clear":false}\n', encoding="utf-8"
            )
            data = out / "data"
            data.mkdir()
            import os

            os.environ["NEXVUE_ETC"] = str(etc)
            os.environ["NEXVUE_RUN_DIR"] = str(run)
            os.environ["NEXVUE_DATA"] = str(data)
            os.environ["NEXVUE_METRICS_DB"] = str(data / "missing.db")
            try:
                zpath = mod.build_bundle(hours=1, requestor_ip="127.0.0.1", out_dir=data / "support")
            finally:
                for k in ("NEXVUE_ETC", "NEXVUE_RUN_DIR", "NEXVUE_DATA", "NEXVUE_METRICS_DB"):
                    os.environ.pop(k, None)
            self.assertTrue(zpath.is_file())
            self.assertTrue(zpath.name.startswith("nexvue-support-"))
            with zipfile.ZipFile(zpath, "r") as zf:
                names = set(zf.namelist())
                self.assertIn("MANIFEST.json", names)
                self.assertIn("REDACTIONS.txt", names)
                self.assertIn("config/channels/0.env", names)
                raw = zf.read("config/channels/0.env").decode("utf-8")
                self.assertIn("srt://***:***@", raw)
                self.assertNotIn("u:p@", raw)


if __name__ == "__main__":
    unittest.main()
