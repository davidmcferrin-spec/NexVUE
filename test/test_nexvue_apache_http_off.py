#!/usr/bin/env python3
"""Unit tests for nexvue-apache-http-off.py (stdlib unittest)."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "nexvue-apache-http-off.py"

UBUNTU_PORTS = """\
# If you just change the port or add more ports here, you will likely also
# have to change the VirtualHost statement in
# /etc/apache2/sites-enabled/000-default.conf

Listen 80

<IfModule ssl_module>
	Listen 443
</IfModule>

<IfModule mod_gnutls.c>
	Listen 443
</IfModule>
"""


def _load():
    spec = importlib.util.spec_from_file_location("apache_http_off", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestApacheHttpOff(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_comments_listen_80_keeps_443(self):
        out = self.mod.comment_listen_80(UBUNTU_PORTS)
        self.assertTrue(self.mod.http_listen_is_off(out))
        self.assertIn("Listen 443", out)
        self.assertRegex(out, r"(?m)^# Listen 80")
        self.assertNotRegex(out, r"(?m)^Listen 80\s*$")

    def test_does_not_touch_8080_or_jwks(self):
        src = "Listen 8080\nListen 127.0.0.1:9080\nListen 80\n"
        out = self.mod.comment_listen_80(src)
        self.assertIn("Listen 8080", out)
        self.assertIn("Listen 127.0.0.1:9080", out)
        self.assertTrue(self.mod.http_listen_is_off(out))

    def test_comments_bound_listen_80(self):
        src = "Listen 0.0.0.0:80\nListen *:80\nListen 127.0.0.1:80\nListen [::]:80\n"
        out = self.mod.comment_listen_80(src)
        self.assertTrue(self.mod.http_listen_is_off(out))
        self.assertNotRegex(out, r"(?m)^Listen ")

    def test_idempotent(self):
        once = self.mod.comment_listen_80(UBUNTU_PORTS)
        twice = self.mod.comment_listen_80(once)
        self.assertEqual(once, twice)

    def test_check_and_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ports.conf"
            p.write_text(UBUNTU_PORTS, encoding="utf-8")
            self.assertFalse(self.mod.http_listen_is_off(p.read_text(encoding="utf-8")))
            p.write_text(
                self.mod.comment_listen_80(p.read_text(encoding="utf-8")),
                encoding="utf-8",
            )
            self.assertTrue(self.mod.http_listen_is_off(p.read_text(encoding="utf-8")))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(verbosity=2) is None else 1)
