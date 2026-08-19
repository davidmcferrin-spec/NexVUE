#!/usr/bin/env python3
"""Unit tests for nexvue-apache-http-on.py (stdlib unittest)."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "nexvue-apache-http-on.py"

UBUNTU_PORTS_ON = """\
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

# What a box previously set up by the old (retired) nexvue-apache-http-off.py
# would have on disk today — this is the realistic upgrade case.
PREVIOUSLY_CLOSED = """\
Listen 80  # NexVUE: HTTP closed; UI is HTTPS :443 only

<IfModule ssl_module>
	Listen 443
</IfModule>
""".replace("Listen 80  # NexVUE", "# Listen 80  # NexVUE")


def _load():
    spec = importlib.util.spec_from_file_location("apache_http_on", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestApacheHttpOn(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_already_on_is_a_noop(self):
        out = self.mod.uncomment_listen_80(UBUNTU_PORTS_ON)
        self.assertEqual(out, UBUNTU_PORTS_ON)
        self.assertTrue(self.mod.http_listen_is_on(out))

    def test_uncomments_previously_closed_box(self):
        self.assertFalse(self.mod.http_listen_is_on(PREVIOUSLY_CLOSED))
        out = self.mod.uncomment_listen_80(PREVIOUSLY_CLOSED)
        self.assertTrue(self.mod.http_listen_is_on(out))
        self.assertRegex(out, r"(?m)^Listen 80\s*$")
        self.assertIn("Listen 443", out)

    def test_does_not_touch_8080_or_jwks(self):
        src = "Listen 8080\nListen 127.0.0.1:9080\n# Listen 80  # NexVUE: HTTP closed\n"
        out = self.mod.uncomment_listen_80(src)
        self.assertIn("Listen 8080", out)
        self.assertIn("Listen 127.0.0.1:9080", out)
        self.assertTrue(self.mod.http_listen_is_on(out))
        # Loopback JWKS listener must never be treated as satisfying "on".
        self.assertFalse(self.mod.http_listen_is_on("Listen 127.0.0.1:9080\n"))

    def test_uncomments_bound_variants(self):
        src = "# Listen 0.0.0.0:80\n# Listen *:80\n# Listen 127.0.0.1:80\n# Listen [::]:80\n"
        out = self.mod.uncomment_listen_80(src)
        self.assertTrue(self.mod.http_listen_is_on(out))

    def test_idempotent(self):
        once = self.mod.uncomment_listen_80(PREVIOUSLY_CLOSED)
        twice = self.mod.uncomment_listen_80(once)
        self.assertEqual(once, twice)

    def test_appends_listen_80_if_entirely_absent(self):
        out = self.mod.uncomment_listen_80("Listen 443\n")
        self.assertTrue(self.mod.http_listen_is_on(out))
        self.assertIn("Listen 443", out)

    def test_check_and_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ports.conf"
            p.write_text(PREVIOUSLY_CLOSED, encoding="utf-8")
            self.assertFalse(self.mod.http_listen_is_on(p.read_text(encoding="utf-8")))
            p.write_text(
                self.mod.uncomment_listen_80(p.read_text(encoding="utf-8")),
                encoding="utf-8",
            )
            self.assertTrue(self.mod.http_listen_is_on(p.read_text(encoding="utf-8")))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(verbosity=2) is None else 1)
