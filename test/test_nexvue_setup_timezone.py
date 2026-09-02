#!/usr/bin/env python3
"""
Contract checks for setup.sh Eastern timezone + NTP.

setup.sh can't be run here (needs root + timedatectl). Verify the helper
exists, is called from edge and --portal, and --check only verifies.

Run: python3 test/test_nexvue_setup_timezone.py
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / "setup.sh"
EXAMPLE = ROOT / "nexvue-example.env"


class TestSetupTimezoneEastern(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SETUP.read_text(encoding="utf-8")

    def test_helper_defined_before_portal_and_edge_calls(self) -> None:
        def_idx = self.text.index("ensure_timezone_eastern() {")
        portal_call = self.text.index("ensure_timezone_eastern\n")
        check_call = self.text.index("ensure_timezone_eastern --check")
        self.assertLess(def_idx, portal_call)
        self.assertLess(def_idx, check_call)
        self.assertIn("America/New_York", self.text[def_idx:def_idx + 400])
        self.assertIn("set-ntp true", self.text)
        self.assertIn("99-nexvue-timezone.ini", self.text)
        self.assertIn("date.timezone", self.text)
        self.assertIn("systemd-timesyncd", self.text)

    def test_edge_and_portal_install_tzdata(self) -> None:
        self.assertIn("openssh-server ufw tzdata", self.text)
        self.assertIn("php-cli php-sqlite3 ufw tzdata", self.text)

    def test_metrics_tz_appended_when_missing(self) -> None:
        self.assertIn("NEXVUE_METRICS_TZ=America/New_York", self.text)
        example = EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("NEXVUE_METRICS_TZ=America/New_York", example)

    def test_check_only_does_not_set_timezone(self) -> None:
        start = self.text.index("if $check_only; then")
        # First --check branch in the helper must not call set-timezone.
        helper = self.text[
            self.text.index("ensure_timezone_eastern() {") : self.text.index(
                "# SSH first, then HTTPS/WHEP"
            )
        ]
        check_branch = helper.split("if $check_only; then", 1)[1].split("fi", 1)[0]
        self.assertNotIn("set-timezone", check_branch)
        self.assertNotIn("set-ntp", check_branch)
        self.assertIn("want ${want}", check_branch)


if __name__ == "__main__":
    unittest.main()
