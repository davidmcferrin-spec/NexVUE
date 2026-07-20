#!/usr/bin/env python3
"""
Unit tests for the encoder enable/disable path:
  - nexvue-ops.php helpers unit_enable_allowed() + parse_unit_status()
  - nexvue-ops-enable.sh allowlist and the systemctl commands it issues
    (verified against a stub systemctl on PATH — never the real one)

PHP tests are skipped when the php CLI is unavailable; wrapper tests are
skipped when bash is unavailable (e.g. Windows laptop).

Run: python3 test/test_nexvue_ops_enable.py
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS_PHP = ROOT / "nexvue-ops.php"
ENABLE_SH = ROOT / "nexvue-ops-enable.sh"
PHP = shutil.which("php")
BASH = shutil.which("bash")

STUB_SYSTEMCTL = """#!/usr/bin/env bash
# Records every invocation; the test asserts on the log.
echo "$@" >> "$SYSTEMCTL_LOG"
exit 0
"""


@unittest.skipUnless(PHP and OPS_PHP.is_file(), "php CLI or nexvue-ops.php missing")
class TestPhpHelpers(unittest.TestCase):
    def _php(self, body: str) -> dict:
        ops = OPS_PHP.as_posix()
        code = f"include '{ops}';\n{body}"
        env = os.environ.copy()
        env.pop("NEXVUE_OPS_HTTP", None)  # library mode: no dispatch
        r = subprocess.run(
            [PHP, "-d", "display_errors=stderr", "-r", code],
            capture_output=True, text=True, env=env, timeout=15,
        )
        if r.returncode != 0:
            self.fail(f"php failed ({r.returncode}): {r.stderr or r.stdout}")
        try:
            return json.loads((r.stdout or "").strip())
        except json.JSONDecodeError:
            self.fail(f"expected JSON, got: {r.stdout!r}\nstderr={r.stderr!r}")

    def test_unit_enable_allowed_encoders_only(self) -> None:
        data = self._php("""
echo json_encode([
  'enc0' => unit_enable_allowed('nexvue-encode@0'),
  'enc7' => unit_enable_allowed('nexvue-encode@7'),
  'enc8' => unit_enable_allowed('nexvue-encode@8'),
  'enc9' => unit_enable_allowed('nexvue-encode@9'),
  'enc10' => unit_enable_allowed('nexvue-encode@10'),
  'mediamtx' => unit_enable_allowed('mediamtx'),
  'status' => unit_enable_allowed('nexvue-status'),
  'metrics' => unit_enable_allowed('nexvue-metrics'),
  'trailing' => unit_enable_allowed('nexvue-encode@0 extra'),
]);
""")
        self.assertTrue(data["enc0"])
        self.assertTrue(data["enc7"])
        self.assertTrue(data["enc8"])
        self.assertTrue(data["enc9"])
        self.assertFalse(data["enc10"])
        self.assertFalse(data["mediamtx"])
        self.assertFalse(data["status"])
        self.assertFalse(data["metrics"])
        self.assertFalse(data["trailing"])

    def test_parse_unit_status_two_tokens(self) -> None:
        data = self._php("""
echo json_encode([
  'both' => parse_unit_status("failed disabled\\n"),
  'active' => parse_unit_status("active enabled"),
]);
""")
        self.assertEqual(data["both"], {"state": "failed", "enabled": "disabled"})
        self.assertEqual(data["active"], {"state": "active", "enabled": "enabled"})

    def test_parse_unit_status_legacy_and_empty(self) -> None:
        # Old single-token wrapper output and empty output must not break.
        data = self._php("""
echo json_encode([
  'legacy' => parse_unit_status("inactive\\n"),
  'empty' => parse_unit_status(""),
]);
""")
        self.assertEqual(data["legacy"], {"state": "inactive", "enabled": "unknown"})
        self.assertEqual(data["empty"], {"state": "unknown", "enabled": "unknown"})


@unittest.skipUnless(BASH and ENABLE_SH.is_file(), "bash or nexvue-ops-enable.sh missing")
class TestEnableWrapper(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        td = Path(self._td.name)
        self.log = td / "systemctl.log"
        stub = td / "systemctl"
        stub.write_text(STUB_SYSTEMCTL, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.env = os.environ.copy()
        self.env["PATH"] = f"{td}{os.pathsep}{self.env.get('PATH', '')}"
        self.env["SYSTEMCTL_LOG"] = str(self.log)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [BASH, str(ENABLE_SH), *args],
            capture_output=True, text=True, env=self.env, timeout=15,
        )

    def _log_lines(self) -> list[str]:
        if not self.log.exists():
            return []
        return [l for l in self.log.read_text(encoding="utf-8").splitlines() if l]

    def test_enable_runs_enable_now(self) -> None:
        r = self._run("enable", "nexvue-encode@3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._log_lines(), ["enable --now nexvue-encode@3"])

    def test_disable_runs_disable_now_and_reset_failed(self) -> None:
        r = self._run("disable", "nexvue-encode@4")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            self._log_lines(),
            ["disable --now nexvue-encode@4", "reset-failed nexvue-encode@4"],
        )

    def test_start_runs_start_only(self) -> None:
        r = self._run("start", "nexvue-encode@2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._log_lines(), ["start nexvue-encode@2"])

    def test_stop_runs_stop_and_reset_failed(self) -> None:
        r = self._run("stop", "nexvue-encode@5")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            self._log_lines(),
            ["stop nexvue-encode@5", "reset-failed nexvue-encode@5"],
        )

    def test_rejects_bad_verb(self) -> None:
        for verb in ("restart", "mask", "kill"):
            r = self._run(verb, "nexvue-encode@0")
            self.assertEqual(r.returncode, 2, f"{verb}: {r.stderr}")
            self.assertIn("disallowed verb", r.stderr)
        self.assertEqual(self._log_lines(), [])

    def test_rejects_core_units_and_bad_instances(self) -> None:
        for unit in ("mediamtx", "nexvue-status", "nexvue-metrics",
                     "nexvue-encode@10", "nexvue-encode@", "sshd"):
            r = self._run("disable", unit)
            self.assertEqual(r.returncode, 2, f"{unit}: {r.stderr}")
            self.assertIn("disallowed unit", r.stderr)
        self.assertEqual(self._log_lines(), [])

    def test_rejects_missing_args(self) -> None:
        self.assertEqual(self._run().returncode, 2)
        self.assertEqual(self._run("enable").returncode, 2)
        self.assertEqual(self._log_lines(), [])


if __name__ == "__main__":
    unittest.main()
