#!/usr/bin/env python3
"""VERSION file + version stamp shape.

Run: python3 test/test_nexvue_version.py
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([.-][A-Za-z0-9.-]+)?$")


class TestVersion(unittest.TestCase):
    def test_version_file(self):
        raw = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(raw, VERSION_RE)

    def test_version_php_present(self):
        self.assertTrue((ROOT / "nexvue-version.php").is_file())
        self.assertTrue((ROOT / "nexvue-ops-update.sh").is_file())

    def test_stamp_roundtrip_shape(self):
        ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        stamp = {
            "version": ver,
            "git_sha": "abc123def456",
            "git_full": "abc123def4567890",
            "git_branch": "main",
            "repo": "/opt/NexVUE",
            "updated_at": "2026-07-24T00:00:00Z",
        }
        raw = json.dumps(stamp)
        data = json.loads(raw)
        self.assertEqual(data["version"], ver)
        self.assertRegex(data["version"], VERSION_RE)


if __name__ == "__main__":
    unittest.main()
