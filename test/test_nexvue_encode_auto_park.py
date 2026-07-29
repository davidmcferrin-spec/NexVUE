#!/usr/bin/env python3
"""
Unit tests for nexvue-encode-auto-park.sh (check + stoppost).

Uses a stub decklink-status and stub systemctl — never the real ones.
Skipped when bash is unavailable (e.g. Windows laptop without Git Bash).

Run: python3 test/test_nexvue_encode_auto_park.py
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
PARK_SH = ROOT / "nexvue-encode-auto-park.sh"
BASH = shutil.which("bash") or (
    str(Path(r"C:\Program Files\Git\bin\bash.exe"))
    if Path(r"C:\Program Files\Git\bin\bash.exe").is_file()
    else None
)


@unittest.skipUnless(BASH and PARK_SH.is_file(), "bash or auto-park script missing")
class TestEncodeAutoPark(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.state_dir = self.state / "state"
        self.state_dir.mkdir()
        self.bin = self.state / "bin"
        self.bin.mkdir()
        self.systemctl_log = self.state / "systemctl.log"
        self.systemctl_log.write_text("", encoding="utf-8")
        self._write_systemctl_stub()
        self.status_json = {
            "devices": [
                {
                    "index": 4,
                    "name": "DeckLink",
                    "input_locked": False,
                    "busy": False,
                }
            ]
        }
        self._write_status_stub()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_systemctl_stub(self) -> None:
        path = self.bin / "systemctl"
        path.write_text(
            "#!/usr/bin/env bash\n"
            'echo "$@" >> "$SYSTEMCTL_LOG"\n'
            "exit 0\n",
            encoding="utf-8",
            newline="\n",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _write_status_stub(self) -> None:
        path = self.bin / "decklink-status"
        # Read JSON from STATUS_JSON_FILE so tests can mutate without rewrite.
        path.write_text(
            "#!/usr/bin/env bash\n"
            'cat "$STATUS_JSON_FILE"\n',
            encoding="utf-8",
            newline="\n",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        (self.state / "status.json").write_text(
            json.dumps(self.status_json), encoding="utf-8"
        )

    def _set_locked(self, locked: bool, *, busy: bool = False) -> None:
        self.status_json["devices"][0]["input_locked"] = locked
        self.status_json["devices"][0]["busy"] = busy
        (self.state / "status.json").write_text(
            json.dumps(self.status_json), encoding="utf-8"
        )

    def _env(self, **extra: str) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env["AUTO_PARK_STATE_DIR"] = str(self.state_dir)
        env["DECKLINK_STATUS_BIN"] = str(self.bin / "decklink-status")
        env["STATUS_JSON_FILE"] = str(self.state / "status.json")
        env["SYSTEMCTL_LOG"] = str(self.systemctl_log)
        env["AUTO_PARK_UNLOCK_CYCLES"] = "3"
        env.update(extra)
        return env

    def _run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BASH, str(PARK_SH), *args],
            capture_output=True,
            text=True,
            env=env or self._env(),
            timeout=15,
        )

    def test_disabled_when_cycles_zero(self) -> None:
        r = self._run("check", "4", "4", env=self._env(AUTO_PARK_UNLOCK_CYCLES="0"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.state_dir / "4.count").exists())

    def test_srt_skips_park(self) -> None:
        r = self._run("check", "4", "4", env=self._env(INPUT_TYPE="srt"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.state_dir / "4.count").exists())

    def test_locked_clears_streak(self) -> None:
        (self.state_dir / "4.count").write_text("2\n", encoding="utf-8")
        self._set_locked(True)
        r = self._run("check", "4", "4")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.state_dir / "4.count").exists())

    def test_busy_fail_open(self) -> None:
        self._set_locked(False, busy=True)
        r = self._run("check", "4", "4")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.state_dir / "4.count").exists())

    def test_unlocked_increments_then_parks(self) -> None:
        self._set_locked(False)
        r1 = self._run("check", "4", "4")
        self.assertEqual(r1.returncode, 1, r1.stderr + r1.stdout)
        self.assertEqual((self.state_dir / "4.count").read_text(encoding="utf-8").strip(), "1")

        r2 = self._run("check", "4", "4")
        self.assertEqual(r2.returncode, 1, r2.stderr + r2.stdout)
        self.assertEqual((self.state_dir / "4.count").read_text(encoding="utf-8").strip(), "2")

        r3 = self._run("check", "4", "4")
        self.assertEqual(r3.returncode, 75, r3.stderr + r3.stdout)
        self.assertTrue((self.state_dir / "4.request").is_file())

    def test_stoppost_disables_when_request_present(self) -> None:
        (self.state_dir / "4.request").write_text("unlocked\n", encoding="utf-8")
        (self.state_dir / "4.count").write_text("3\n", encoding="utf-8")
        r = self._run("stoppost", "4")
        self.assertEqual(r.returncode, 0, r.stderr)
        log = self.systemctl_log.read_text(encoding="utf-8")
        self.assertIn("disable nexvue-encode@4", log)
        self.assertIn("reset-failed nexvue-encode@4", log)
        self.assertFalse((self.state_dir / "4.request").exists())
        self.assertFalse((self.state_dir / "4.count").exists())

    def test_stoppost_noop_without_request(self) -> None:
        r = self._run("stoppost", "4")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.systemctl_log.read_text(encoding="utf-8"), "")

    def test_rejects_bad_channel(self) -> None:
        r = self._run("check", "9", "4")
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
