#!/usr/bin/env python3
"""
Sanity checks for setup.sh's --portal branch (Phase 4 — cloud portal).

setup.sh can't be exercised end-to-end off a real Ubuntu box (apt-get,
systemctl, apache2ctl aren't available here), so this deliberately checks
only what's verifiable statically: the flag is wired, install_portal() is
defined before it's called, its required-files list is accurate, and it
never touches any DeckLink/GStreamer/MediaMTX/encoder path.

Run: python3 test/test_nexvue_setup_portal.py
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / "setup.sh"


class TestSetupPortalFlag(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SETUP.read_text(encoding="utf-8")

    def test_portal_flag_parsed(self) -> None:
        self.assertIn("--portal)   PORTAL_MODE=true ;;", self.text)

    def test_install_portal_defined_before_called(self) -> None:
        def_idx = self.text.index("install_portal() {")
        call_idx = self.text.index('install_portal "$@"')
        self.assertLess(def_idx, call_idx, "install_portal must be defined before it is called")

    def test_portal_mode_exits_before_edge_only_setup(self) -> None:
        # REPO_DIR= (arg parsing follows immediately) must appear before the
        # --portal branch, and nothing between them should have already done
        # real work unconditionally — the branch must be the very next
        # decision point after parsing args, for every invocation mode.
        repo_dir_idx = self.text.index('REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\nCHECK_ONLY=false')
        call_idx = self.text.index('install_portal "$@"\n  exit $?')
        self.assertLess(repo_dir_idx, call_idx, "the --portal branch must come right after arg parsing")
        between = self.text[repo_dir_idx:call_idx]
        for marker in ("apt-get install", "systemctl enable", "gstreamer1.0", "mediamtx"):
            self.assertNotIn(marker, between, f"{marker} runs unconditionally before the --portal branch decides")
        # And the edge-only body (real work: apt-get, DeckLink, MediaMTX)
        # only begins well after that exit, confirming --portal truly skips it.
        first_apt = self.text.index("apt-get install", call_idx)
        self.assertGreater(first_apt, call_idx)

    def test_required_files_all_exist(self) -> None:
        m = re.search(r"local portal_files=\(\n(.*?)\n  \)", self.text, re.DOTALL)
        self.assertIsNotNone(m, "portal_files array not found")
        files = [line.strip() for line in m.group(1).splitlines() if line.strip()]
        self.assertGreater(len(files), 5)
        for f in files:
            self.assertTrue((ROOT / f).is_file(), f"listed but missing: {f}")

    def test_portal_install_skips_deckLink_and_media_stack(self) -> None:
        start = self.text.index("install_portal() {")
        end = self.text.index("\nREPO_DIR=", start)
        body = self.text[start:end]
        for forbidden in (
            "decklink-configure", "gstreamer1.0", "mediamtx_", "python3-gi",
            "nexvue-encode@", "nexvue-ops.sudoers", "8889/tcp", "8189",
        ):
            self.assertNotIn(forbidden, body, f"install_portal() must never reference {forbidden}")
        for required in ("apache2", "php-sqlite3", "portal.db", "nexvue-portal-bootstrap.php"):
            self.assertIn(required, body)

    def test_portal_uses_distinct_tls_and_firewall_scope(self) -> None:
        start = self.text.index("install_portal() {")
        end = self.text.index("\nREPO_DIR=", start)
        body = self.text[start:end]
        self.assertIn("/etc/nexvue-portal/tls", body)
        self.assertIn("443/tcp", body)
        self.assertNotIn("/etc/nexvue/tls", body)


if __name__ == "__main__":
    unittest.main()
