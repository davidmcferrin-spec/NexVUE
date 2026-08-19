#!/usr/bin/env python3
"""
Unit tests for nexvue-ops-portal-write.py — station env NEXVUE_PORTAL_*
patch/validate logic (Phase 4 — cloud portal adoption).

Run: python3 test/test_nexvue_ops_portal_write.py
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SPEC_PATH = Path(__file__).resolve().parent.parent / "nexvue-ops-portal-write.py"
spec = importlib.util.spec_from_file_location("nexvue_ops_portal_write", SPEC_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules["nexvue_ops_portal_write"] = mod
spec.loader.exec_module(mod)


class TestSanitize(unittest.TestCase):
    def test_url_requires_https(self):
        with self.assertRaises(ValueError):
            mod.sanitize("url", "http://portal.example.com")
        self.assertEqual(mod.sanitize("url", "https://portal.example.com"), "https://portal.example.com")

    def test_url_trailing_slash_stripped(self):
        self.assertEqual(mod.sanitize("url", "https://portal.example.com/"), "https://portal.example.com")

    def test_url_rejects_garbage(self):
        with self.assertRaises(ValueError):
            mod.sanitize("url", "not a url; rm -rf /")

    def test_station_id_alnum_only(self):
        self.assertEqual(mod.sanitize("station_id", "sta-01_ABC"), "sta-01_ABC")
        with self.assertRaises(ValueError):
            mod.sanitize("station_id", "has spaces")

    def test_station_api_key_shape(self):
        self.assertEqual(mod.sanitize("station_api_key", "a" * 32), "a" * 32)
        with self.assertRaises(ValueError):
            mod.sanitize("station_api_key", "short")
        with self.assertRaises(ValueError):
            mod.sanitize("station_api_key", "has spaces in it 1234567890")

    def test_adopted_at_iso_only(self):
        self.assertEqual(mod.sanitize("adopted_at", "2026-08-15T12:00:00Z"), "2026-08-15T12:00:00Z")
        with self.assertRaises(ValueError):
            mod.sanitize("adopted_at", "not-a-date")

    def test_heartbeat_interval_bounds(self):
        self.assertEqual(mod.sanitize("heartbeat_interval_s", "300"), "300")
        with self.assertRaises(ValueError):
            mod.sanitize("heartbeat_interval_s", "10")
        with self.assertRaises(ValueError):
            mod.sanitize("heartbeat_interval_s", "999999")

    def test_rejects_multiline(self):
        with self.assertRaises(ValueError):
            mod.sanitize("station_id", "abc\ninjected")

    def test_unknown_field_rejected(self):
        with self.assertRaises(ValueError):
            mod.apply_patch("", {"not_a_real_field": "x"})


class TestApplyPatch(unittest.TestCase):
    def test_appends_when_missing(self):
        out = mod.apply_patch("MAX_DEVICES=8\n", {"url": "https://portal.example.com"})
        self.assertIn("MAX_DEVICES=8\n", out)
        self.assertIn("NEXVUE_PORTAL_URL=https://portal.example.com\n", out)
        self.assertIn("# --- Portal adoption", out)

    def test_updates_existing_active_assignment(self):
        text = "MAX_DEVICES=8\nNEXVUE_PORTAL_URL=https://old.example.com\n"
        out = mod.apply_patch(text, {"url": "https://new.example.com"})
        self.assertIn("NEXVUE_PORTAL_URL=https://new.example.com\n", out)
        self.assertNotIn("old.example.com", out)

    def test_multiple_keys_in_one_patch(self):
        out = mod.apply_patch(
            "",
            {
                "url": "https://portal.example.com",
                "station_id": "sta-1",
                "station_api_key": "k" * 20,
                "adopted_at": "2026-08-15T00:00:00Z",
                "heartbeat_interval_s": "300",
            },
        )
        for needle in (
            "NEXVUE_PORTAL_URL=https://portal.example.com",
            "NEXVUE_PORTAL_STATION_ID=sta-1",
            "NEXVUE_PORTAL_STATION_API_KEY=" + "k" * 20,
            "NEXVUE_PORTAL_ADOPTED_AT=2026-08-15T00:00:00Z",
            "NEXVUE_PORTAL_HEARTBEAT_INTERVAL_S=300",
        ):
            self.assertIn(needle, out)

    def test_preserves_unrelated_lines(self):
        text = "# comment\nMAX_DEVICES=8\nMAX_CHANNELS=8\n"
        out = mod.apply_patch(text, {"station_id": "sta-1"})
        self.assertIn("# comment\n", out)
        self.assertIn("MAX_DEVICES=8\n", out)
        self.assertIn("MAX_CHANNELS=8\n", out)

    def test_empty_patch_is_noop(self):
        text = "MAX_DEVICES=8\n"
        self.assertEqual(mod.apply_patch(text, {}), text)


class TestQuoting(unittest.TestCase):
    def test_unsafe_value_gets_quoted(self):
        # station_api_key's own sanitizer forbids spaces, but format_assignment_value
        # is exercised directly here to confirm the bash-sourcing safety net exists
        # independent of any one field's sanitizer (defense in depth, same bug
        # class as CHANNEL_ALIAS="TVU 35" in nexvue-ops-env-update.py).
        self.assertEqual(mod.format_assignment_value("has space"), '"has space"')

    def test_safe_value_unquoted(self):
        self.assertEqual(mod.format_assignment_value("https://a.b:443/x"), "https://a.b:443/x")


if __name__ == "__main__":
    unittest.main()
