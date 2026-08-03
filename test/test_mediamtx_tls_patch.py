#!/usr/bin/env python3
"""Unit tests for nexvue-mediamtx-tls-patch.py (stdlib unittest)."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "nexvue-mediamtx-tls-patch.py"


def _load():
    spec = importlib.util.spec_from_file_location("mtx_tls", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestMediamtxTlsPatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_sets_paths_and_uncomments(self):
        src = """
api: yes
#apiEncryption: no
#apiServerKey: /old/key.pem
#apiServerCert: /old/cert.pem
webrtcEncryption: no
webrtcServerKey: /wrong/privkey.pem
webrtcServerCert: /wrong/fullchain.pem
paths: {}
"""
        out = self.mod.patch_tls_paths(src)
        self.assertIn("apiEncryption: yes", out)
        self.assertNotIn("#apiEncryption", out)
        self.assertIn("apiServerKey: /etc/nexvue/tls/privkey.pem", out)
        self.assertIn("webrtcServerCert: /etc/nexvue/tls/fullchain.pem", out)
        self.assertTrue(self.mod.tls_paths_ok(out))

    def test_appends_when_missing(self):
        src = "api: yes\npaths: {}\n"
        out = self.mod.patch_tls_paths(src)
        self.assertTrue(self.mod.tls_paths_ok(out))

    def test_idempotent_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "mediamtx.yml"
            p.write_text("api: yes\n", encoding="utf-8")
            # run twice via main helpers
            p.write_text(self.mod.patch_tls_paths(p.read_text(encoding="utf-8")), encoding="utf-8")
            once = p.read_text(encoding="utf-8")
            p.write_text(self.mod.patch_tls_paths(once), encoding="utf-8")
            self.assertEqual(once, p.read_text(encoding="utf-8"))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(verbosity=2) is None else 1)
