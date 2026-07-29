#!/usr/bin/env python3
"""
Smoke checks for Multiview share auto-tune + frameless fullscreen + 4-ch cap.

Run: python3 test/test_multiview_share.py
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestMultiviewShare(unittest.TestCase):
    def test_multiview_auto_tune_and_frameless(self) -> None:
        html = (ROOT / "multiview.html").read_text(encoding="utf-8")
        self.assertIn("autoTuneFromShare", html)
        self.assertIn('authUser.auth !== "share"', html)
        self.assertIn("body:fullscreen .pane-bar", html)
        self.assertIn("Near-frameless wall mode", html)

    def test_share_ui_multiview_cap(self) -> None:
        js = (ROOT / "nexvue-share-ui.js").read_text(encoding="utf-8")
        self.assertIn('page === "multiview" ? 4 : 0', js)
        self.assertIn("Channels (up to 4)", js)
        self.assertIn("getDefaultChannels", js)

    def test_auth_lib_multiview_max_constant(self) -> None:
        lib = (ROOT / "nexvue-auth-lib.php").read_text(encoding="utf-8")
        self.assertIn("NEXVUE_AUTH_MULTIVIEW_SHARE_MAX", lib)
        self.assertIn("auth_normalize_share_channels", lib)


if __name__ == "__main__":
    unittest.main()
