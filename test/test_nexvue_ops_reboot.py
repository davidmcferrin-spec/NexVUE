#!/usr/bin/env python3
"""
Unit tests for the Services host-reboot path:
  - nexvue-ops-reboot.sh takes no args and invokes `systemctl reboot`
    (verified against a stub systemctl on PATH — never the real one)
  - extra args are rejected and do not invoke systemctl
  - sudoers allowlists the helper with no trailing *
  - reboot_host is admin-only in nexvue-ops.php + Services UI

Wrapper tests are skipped when bash is unavailable (e.g. Windows laptop).

Run: python3 test/test_nexvue_ops_reboot.py
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REBOOT_SH = ROOT / "nexvue-ops-reboot.sh"
SUDOERS = ROOT / "nexvue-ops.sudoers"
OPS_PHP = ROOT / "web-node" / "nexvue-ops.php"
SERVICES = ROOT / "web-node" / "services.html"
SETUP = ROOT / "setup.sh"
_GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")
# Prefer Git Bash on Windows — WSL bash (often first on PATH) does not see
# the same /c/... mount and cannot exec the repo script.
BASH = str(_GIT_BASH) if _GIT_BASH.is_file() else shutil.which("bash")


def bash_path(p: Path) -> str:
    """Git Bash on Windows cannot exec a raw `C:\\...` path."""
    s = str(p.resolve())
    if len(s) >= 2 and s[1] == ":":
        return "/" + s[0].lower() + s[2:].replace("\\", "/")
    return s

STUB_SYSTEMCTL = """#!/usr/bin/env bash
# Records every invocation; the test asserts on the log.
echo "$@" >> "$SYSTEMCTL_LOG"
exit 0
"""


@unittest.skipUnless(BASH and REBOOT_SH.is_file(), "bash or nexvue-ops-reboot.sh missing")
class TestRebootWrapper(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        td = Path(self._td.name)
        self.log = td / "systemctl.log"
        stub = td / "systemctl"
        stub.write_text(STUB_SYSTEMCTL, encoding="utf-8", newline="\n")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.env = os.environ.copy()
        posix_td = bash_path(td)
        self.env["PATH"] = f"{posix_td}:{td}{os.pathsep}{self.env.get('PATH', '')}"
        self.env["SYSTEMCTL_LOG"] = bash_path(self.log)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [BASH, bash_path(REBOOT_SH), *args],
            capture_output=True, text=True, env=self.env, timeout=15,
        )

    def _log_lines(self) -> list[str]:
        if not self.log.exists():
            return []
        return [l for l in self.log.read_text(encoding="utf-8").splitlines() if l]

    def test_no_args_runs_systemctl_reboot(self) -> None:
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._log_lines(), ["reboot"])

    def test_rejects_extra_args_and_does_not_invoke_systemctl(self) -> None:
        for extra in ("now", "-f", "reboot", "nexvue-encode@0"):
            r = self._run(extra)
            self.assertEqual(r.returncode, 2, f"{extra!r}: {r.stderr}")
            self.assertIn("usage: nexvue-ops-reboot.sh", r.stderr)
        self.assertEqual(self._log_lines(), [])


class TestRebootWiring(unittest.TestCase):
    def test_sudoers_allowlists_reboot_without_wildcard(self) -> None:
        text = SUDOERS.read_text(encoding="utf-8")
        self.assertIn(
            "www-data ALL=(root) NOPASSWD: /usr/local/bin/nexvue-ops-reboot.sh",
            text,
        )
        self.assertNotIn("nexvue-ops-reboot.sh *", text)

    def test_ops_php_reboot_host_is_admin_only(self) -> None:
        ops = OPS_PHP.read_text(encoding="utf-8")
        start = ops.index("$adminOnly = [")
        block = ops[start : ops.index("];", start)]
        self.assertIn("'reboot_host'", block)
        self.assertIn("if ($action === 'reboot_host')", ops)
        self.assertIn("/usr/local/bin/nexvue-ops-reboot.sh", ops)
        self.assertIn("reboot_host", ops.split("Actions (GET or POST JSON body):", 1)[1][:800])

    def test_services_ui_two_step_confirm(self) -> None:
        services = SERVICES.read_text(encoding="utf-8")
        self.assertRegex(
            services,
            r'id="btn-reboot-host"[^>]*data-auth-role="admin"',
        )
        self.assertIn('api("reboot_host"', services)
        self.assertIn('Type REBOOT to confirm', services)
        self.assertIn('String(typed).trim() !== "REBOOT"', services)
        self.assertIn("Reboot this Linux box now?", services)

    def test_setup_installs_helper(self) -> None:
        setup = SETUP.read_text(encoding="utf-8")
        self.assertIn("nexvue-ops-reboot.sh", setup)
        self.assertIn(
            'install -m 755 "${REPO_DIR}/nexvue-ops-reboot.sh" /usr/local/bin/nexvue-ops-reboot.sh',
            setup,
        )
        self.assertIn("sudoers allows nexvue-ops-reboot.sh", setup)
        # Full setup (and Update from repo) must rewrite sudoers from the repo
        # file and refuse to finish if reboot is not allowlisted.
        self.assertIn(
            'install -m 440 "${REPO_DIR}/nexvue-ops.sudoers" /etc/sudoers.d/nexvue-ops',
            setup,
        )
        self.assertIn(
            'fail "nexvue-ops.sudoers missing reboot allowlist',
            setup,
        )
        self.assertIn(
            'fail "sudoers drop-in missing nexvue-ops-reboot.sh',
            setup,
        )


if __name__ == "__main__":
    unittest.main()
