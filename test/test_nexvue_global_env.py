#!/usr/bin/env python3
"""
Tests for station-wide /etc/nexvue/nexvue.env ownership of MAX_DEVICES:
  - nexvue-encode@.service sources channel env then global (precedence)
  - legacy migration decision matches setup.sh (keep in sync with that block)

Run: python3 test/test_nexvue_global_env.py
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def migrate_max_devices(legacy_values: list[str]) -> tuple[str, str | None]:
    """Mirror setup.sh migration when /etc/nexvue/nexvue.env is absent.

    Returns (action, value):
      ("default", None)   — no legacy values → install example (MAX_DEVICES=8)
      ("migrate", "N")    — one consistent valid 1–8 value
      ("conflict", None)  — conflicting valid values → leave global absent
    Invalid entries are ignored (same as setup.sh).
    """
    migrate_val: str | None = None
    for raw in legacy_values:
        v = raw.split("#", 1)[0]
        v = v.replace('"', "").replace("'", "").replace(" ", "").replace("\t", "")
        if not v:
            continue
        if not re.fullmatch(r"[1-8]", v):
            continue
        if migrate_val is None:
            migrate_val = v
        elif migrate_val != v:
            return ("conflict", None)
    if migrate_val is None:
        return ("default", None)
    return ("migrate", migrate_val)


class TestEncodeServicePrecedence(unittest.TestCase):
    def test_channel_env_sourced_before_global(self) -> None:
        text = (ROOT / "nexvue-encode@.service").read_text(encoding="utf-8")
        self.assertIn("nexvue-supervisor.py", text)
        # Channel file, then global — global wins for MAX_DEVICES.
        m = re.search(
            r"\. \"\$f\".*?\. \"\$g\".*?exec /usr/local/bin/nexvue-supervisor\.py",
            text,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "ExecStart must source channel ($f) then global ($g) before exec")
        self.assertIn('/etc/nexvue/channels/%i.env', text)
        self.assertIn("/etc/nexvue/nexvue.env", text)


class TestLegacyMaxDevicesMigration(unittest.TestCase):
    def test_no_legacy_installs_default(self) -> None:
        self.assertEqual(migrate_max_devices([]), ("default", None))
        self.assertEqual(migrate_max_devices(["", "bogus", "9"]), ("default", None))

    def test_single_consistent_value_migrates(self) -> None:
        self.assertEqual(migrate_max_devices(["4"]), ("migrate", "4"))
        self.assertEqual(migrate_max_devices(["8", "8", " 8 "]), ("migrate", "8"))
        self.assertEqual(migrate_max_devices(['4 # note', "4"]), ("migrate", "4"))

    def test_conflicting_values_leave_absent(self) -> None:
        self.assertEqual(migrate_max_devices(["4", "8"]), ("conflict", None))

    def test_example_env_defaults_to_eight(self) -> None:
        text = (ROOT / "nexvue-example.env").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^MAX_DEVICES=8\s*$")


if __name__ == "__main__":
    unittest.main()
