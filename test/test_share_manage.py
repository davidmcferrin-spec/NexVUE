#!/usr/bin/env python3
"""
Smoke checks for share edit/delete/purge wiring in UI + API surface.

Run: python3 test/test_share_manage.py
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestShareManage(unittest.TestCase):
    def test_auth_lib_has_update_delete_purge(self) -> None:
        lib = (ROOT / "nexvue-auth-lib.php").read_text(encoding="utf-8")
        self.assertIn("function auth_share_update", lib)
        self.assertIn("function auth_share_delete", lib)
        self.assertIn("function auth_shares_purge_expired", lib)
        self.assertIn("NEXVUE_AUTH_SHARE_PURGE_GRACE_S", lib)

    def test_auth_api_actions(self) -> None:
        api = (ROOT / "nexvue-auth.php").read_text(encoding="utf-8")
        self.assertIn("share_update", api)
        self.assertIn("share_delete", api)
        self.assertIn("share_email", api)
        self.assertIn("revoke the share before deleting", api)

    def test_auth_lib_stores_share_token(self) -> None:
        lib = (ROOT / "nexvue-auth-lib.php").read_text(encoding="utf-8")
        self.assertIn("NEXVUE_AUTH_SCHEMA_VERSION = 3", lib)
        self.assertIn("function auth_share_build_url", lib)
        self.assertIn("auth_try_mail_share", lib)
        self.assertIn("token, page, channels", lib)

    def test_users_ui_edit_delete(self) -> None:
        html = (ROOT / "users.html").read_text(encoding="utf-8")
        self.assertIn("openShareEditor", html)
        self.assertIn('authApi("share_update"', html)
        self.assertIn('authApi("share_delete"', html)
        self.assertIn("showCreatedUrl", html)
        self.assertIn("promptEmailShare", html)
        self.assertIn('authApi("share_email"', html)

    def test_share_ui_delete(self) -> None:
        js = (ROOT / "nexvue-share-ui.js").read_text(encoding="utf-8")
        self.assertIn('share_delete', js)
        self.assertIn("Permanently delete share", js)
        self.assertIn("Copied to clipboard", js)
        self.assertIn("promptEmailShare", js)

    def test_share_viewer_sees_time_left(self) -> None:
        js = (ROOT / "nexvue-auth-gate.js").read_text(encoding="utf-8")
        self.assertIn("formatShareRemaining", js)
        self.assertIn("nav-share-left", js)
        self.assertIn("startShareExpiryTicker", js)

    def test_stereo_companion_paths_in_auth(self) -> None:
        lib = (ROOT / "nexvue-auth-lib.php").read_text(encoding="utf-8")
        self.assertIn("function auth_channel_base_from_path", lib)
        self.assertIn("lost|lo|st", lib)
        api = (ROOT / "nexvue-auth.php").read_text(encoding="utf-8")
        self.assertIn("ch[0-7](lost|lo|st)?", api)
        enc = (ROOT / "nexvue-encode.sh").read_text(encoding="utf-8")
        self.assertIn("STEREO_FALLBACK", enc)
        self.assertIn("sinkst", enc)
        self.assertIn("sinklost", enc)


if __name__ == "__main__":
    unittest.main()
