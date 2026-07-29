#!/usr/bin/env python3
"""Tests for nexvue-mediamtx-jwt-patch.py."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATCH = ROOT / "nexvue-mediamtx-jwt-patch.py"


def _load():
    spec = importlib.util.spec_from_file_location("jwt_patch", PATCH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(PATCH.is_file(), "patcher missing")
class TestMediaMtxJwtPatch(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_inserts_jwt_block(self) -> None:
        src = "logLevel: info\n\npaths:\n  all:\n"
        out = self.mod.patch(src, "http://127.0.0.1:9080/nexvue-jwks.php", None)
        self.assertIn("authMethod: jwt", out)
        self.assertIn("authJWTJWKS: http://127.0.0.1:9080/nexvue-jwks.php", out)
        self.assertIn("authJWTInHTTPQuery: yes", out)
        self.assertIn("action: api", out)
        self.assertIn("apiAddress: 127.0.0.1:9997", out)
        self.assertTrue(out.index("authMethod") < out.index("paths:"))

    def test_api_address_loopback_overrides_all_interfaces(self) -> None:
        src = "apiAddress: :9997\nauthMethod: jwt\npaths:\n  all:\n"
        out = self.mod.patch(src, "http://127.0.0.1:9080/nexvue-jwks.php", None)
        self.assertIn("apiAddress: 127.0.0.1:9997", out)
        self.assertNotIn("apiAddress: :9997", out)

    def test_updates_existing(self) -> None:
        src = (
            "authMethod: internal\n"
            "authJWTJWKS: http://127.0.0.1/old\n"
            "paths:\n  all:\n"
        )
        out = self.mod.patch(src, "http://127.0.0.1:9080/nexvue-jwks.php", None)
        self.assertIn("authMethod: jwt", out)
        self.assertNotIn("internal", out)
        self.assertIn("9080", out)
        self.assertNotIn("http://127.0.0.1/old", out)

    def test_fingerprint_for_https(self) -> None:
        src = "paths:\n  x:\n"
        out = self.mod.patch(src, "https://127.0.0.1/nexvue-jwks.php", "aabbcc")
        self.assertIn("authJWTJWKSFingerprint: aabbcc", out)

    def test_file_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "mediamtx.yml"
            p.write_text("logLevel: info\npaths:\n  a:\n", encoding="utf-8")
            import subprocess
            import sys

            r = subprocess.run(
                [
                    sys.executable,
                    str(PATCH),
                    str(p),
                    "--jwks",
                    "http://127.0.0.1:9080/nexvue-jwks.php",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            body = p.read_text(encoding="utf-8")
            self.assertIn("authMethod: jwt", body)


if __name__ == "__main__":
    unittest.main()
