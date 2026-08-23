#!/usr/bin/env python3
"""Tests for nexvue-mediamtx-ice-patch.py (webrtcAdditionalHosts)."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATCH = ROOT / "nexvue-mediamtx-ice-patch.py"


def _load():
    spec = importlib.util.spec_from_file_location("ice_patch", PATCH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(PATCH.is_file(), "ice patcher missing")
class TestMediaMtxIcePatch(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_empty_list(self) -> None:
        src = "webrtcIPsFromInterfaces: yes\nauthMethod: jwt\n"
        out = self.mod.patch(src, "", "")
        self.assertIn("webrtcAdditionalHosts: []", out)
        self.assertTrue(out.index("webrtcIPsFromInterfaces") < out.index("webrtcAdditionalHosts"))
        self.assertTrue(out.index("webrtcAdditionalHosts") < out.index("authMethod"))

    def test_hostname_and_ip(self) -> None:
        src = "webrtcIPsFromInterfaces: yes\nwebrtcAdditionalHosts: []\n"
        out = self.mod.patch(src, "nexvue.example.com", "203.0.113.40")
        self.assertIn("webrtcAdditionalHosts: [nexvue.example.com, 203.0.113.40]", out)
        self.assertEqual(out.count("webrtcAdditionalHosts:"), 1)

    def test_replaces_flow_and_drops_legacy_key(self) -> None:
        src = (
            "webrtcIPsFromInterfaces: yes\n"
            "webrtcAdditionalHosts: [old.example.com]\n"
            "webrtcICEHostNAT1To1IPs: [198.51.100.9]\n"
            "authMethod: jwt\n"
        )
        out = self.mod.patch(src, "new.example.com", "203.0.113.10")
        self.assertIn("webrtcAdditionalHosts: [new.example.com, 203.0.113.10]", out)
        self.assertNotIn("old.example.com", out)
        self.assertNotIn("webrtcICEHostNAT1To1IPs", out)
        self.assertNotIn("198.51.100.9", out)

    def test_replaces_block_list(self) -> None:
        src = (
            "webrtcIPsFromInterfaces: yes\n"
            "webrtcAdditionalHosts:\n"
            "  - old.example.com\n"
            "  - 198.51.100.1\n"
            "authMethod: jwt\n"
        )
        out = self.mod.patch(src, "host.example.com", "")
        self.assertIn("webrtcAdditionalHosts: [host.example.com]", out)
        self.assertNotIn("old.example.com", out)
        self.assertNotIn("- 198.51.100.1", out)

    def test_parse_flow_and_block(self) -> None:
        flow = "webrtcAdditionalHosts: [nexvue.example.com, 203.0.113.40]\n"
        self.assertEqual(
            self.mod.parse_additional_hosts(flow),
            ["nexvue.example.com", "203.0.113.40"],
        )
        block = "webrtcAdditionalHosts:\n  - a.example.com\n  - 192.0.2.1\n"
        self.assertEqual(
            self.mod.parse_additional_hosts(block),
            ["a.example.com", "192.0.2.1"],
        )

    def test_classify_hosts(self) -> None:
        self.assertEqual(
            self.mod.classify_hosts(["nexvue.example.com", "203.0.113.40"]),
            {"hostname": "nexvue.example.com", "ip": "203.0.113.40"},
        )
        self.assertEqual(
            self.mod.classify_hosts(["10.1.2.3", "edge.local"]),
            {"hostname": "edge.local", "ip": "10.1.2.3"},
        )

    def test_repo_yml_roundtrip(self) -> None:
        yml = ROOT / "mediamtx.yml"
        raw = yml.read_text(encoding="utf-8")
        out = self.mod.patch(raw, "nexvue.example.com", "203.0.113.40")
        self.assertIn("webrtcAdditionalHosts: [nexvue.example.com, 203.0.113.40]", out)
        self.assertNotRegex(out, r"(?m)^webrtcICEHostNAT1To1IPs:")
        self.assertIn("authMethod: jwt", out)
        self.assertIn("webrtcIPsFromInterfaces: yes", out)
        empty = self.mod.patch(raw, "", "")
        self.assertIn("webrtcAdditionalHosts: []", empty)

    def test_cli_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "mediamtx.yml"
            p.write_text("webrtcIPsFromInterfaces: yes\npaths:\n  a:\n", encoding="utf-8")
            r = subprocess.run(
                [
                    sys.executable,
                    str(PATCH),
                    str(p),
                    "--hostname",
                    "edge.example.com",
                    "--ip",
                    "198.51.100.20",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            body = p.read_text(encoding="utf-8")
            self.assertIn("webrtcAdditionalHosts: [edge.example.com, 198.51.100.20]", body)


if __name__ == "__main__":
    unittest.main()
