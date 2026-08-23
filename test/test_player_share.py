#!/usr/bin/env python3
"""
Smoke checks for Player idle pick overlay + share auto-play.

Run: python3 test/test_player_share.py
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAYER = ROOT / "web-node" / "index.html"


class TestPlayerShare(unittest.TestCase):
    def test_idle_pick_overlay_and_share_autoplay(self) -> None:
        html = PLAYER.read_text(encoding="utf-8")
        self.assertIn('id="pick-overlay"', html)
        self.assertIn("Select a channel", html)
        self.assertIn("Choose a channel in the bar above to start watching", html)
        self.assertIn("autoPlayFromShare", html)
        self.assertIn('authUser.auth !== "share"', html)
        self.assertIn("select a channel", html)
        self.assertIn("player-idle", html)
        self.assertIn("html.theater.player-idle .bar:not(.controls)", html)
        self.assertNotIn('id="state">idle</span>', html)

    def test_share_ui_player_has_no_channel_cap(self) -> None:
        js = (ROOT / "web-node" / "nexvue-share-ui.js").read_text(encoding="utf-8")
        self.assertIn('page === "multiview" ? 4 : 0', js)
        self.assertIn("getDefaultChannels", js)


if __name__ == "__main__":
    unittest.main()
