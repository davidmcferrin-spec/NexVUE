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
        self.assertIn("revoke the share before deleting", api)

    def test_users_ui_edit_delete(self) -> None:
        html = (ROOT / "users.html").read_text(encoding="utf-8")
        self.assertIn("openShareEditor", html)
        self.assertIn('authApi("share_update"', html)
        self.assertIn('authApi("share_delete"', html)

    def test_share_ui_delete(self) -> None:
        js = (ROOT / "nexvue-share-ui.js").read_text(encoding="utf-8")
        self.assertIn('share_delete', js)
        self.assertIn("Permanently delete share", js)


if __name__ == "__main__":
    unittest.main()
