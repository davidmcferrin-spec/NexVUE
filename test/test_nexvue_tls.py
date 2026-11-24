#!/usr/bin/env python3
"""
Unit tests for nexvue-tls.py and Settings Certificates wiring.

Run: python3 test/test_nexvue_tls.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TLS_PY = ROOT / "nexvue-tls.py"
TLS_SH = ROOT / "nexvue-ops-tls.sh"
DEPLOY_SH = ROOT / "nexvue-tls-deploy.sh"
SETUP = ROOT / "setup.sh"
SUDOERS = ROOT / "nexvue-ops.sudoers"
OPS_PHP = ROOT / "web-node" / "nexvue-ops.php"
CHANNELS = ROOT / "web-node" / "channels.html"
OPENSSL = shutil.which("openssl")


def _load():
    spec = importlib.util.spec_from_file_location("nexvue_tls", TLS_PY)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _run_py(args: list[str], env: dict[str, str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TLS_PY), *args],
        input=stdin,
        text=True,
        capture_output=True,
        env={**os.environ, **env},
        check=False,
    )


def _make_pair(tmpdir: Path, days: int = 30, cn: str = "nexvue.example.com") -> tuple[bytes, bytes]:
    key = tmpdir / "key.pem"
    cert = tmpdir / "cert.pem"
    subprocess.run(
        [
            OPENSSL,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-days",
            str(days),
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            f"/CN={cn}/O=NexVUE/OU=test",
        ],
        check=True,
        capture_output=True,
    )
    return cert.read_bytes(), key.read_bytes()


class TestSanitize(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_domain_ok(self) -> None:
        self.assertEqual(self.mod.sanitize_domain("NexVUE.Example.COM"), "nexvue.example.com")

    def test_domain_rejects_ip(self) -> None:
        with self.assertRaises(ValueError):
            self.mod.sanitize_domain("203.0.113.40")

    def test_domain_rejects_single_label(self) -> None:
        with self.assertRaises(ValueError):
            self.mod.sanitize_domain("nexvue")

    def test_domain_blank_ok(self) -> None:
        self.assertEqual(self.mod.sanitize_domain("  "), "")

    def test_email_ok(self) -> None:
        self.assertEqual(self.mod.sanitize_email("Ops@Example.com"), "Ops@Example.com")

    def test_email_rejects_shell(self) -> None:
        with self.assertRaises(ValueError):
            self.mod.sanitize_email('a@b.com;rm -rf /')


class TestEnvPatch(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_appends_keys(self) -> None:
        out = self.mod.apply_env_patch("MAX_CHANNELS=8\n", "ops@example.com", "edge.example.com")
        self.assertIn("NEXVUE_TLS_EMAIL=ops@example.com\n", out)
        self.assertIn("NEXVUE_TLS_DOMAIN=edge.example.com\n", out)
        self.assertIn("# --- Certificates", out)

    def test_replaces_existing(self) -> None:
        src = "NEXVUE_TLS_EMAIL=old@x.com\nNEXVUE_TLS_DOMAIN=old.example.com\n"
        out = self.mod.apply_env_patch(src, "new@x.com", "new.example.com")
        self.assertIn("NEXVUE_TLS_EMAIL=new@x.com\n", out)
        self.assertNotIn("old@x.com", out)


class TestPemDecode(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_plain_pem(self) -> None:
        pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
        out = self.mod.decode_pem_field(pem, "certificate")
        self.assertIn(b"-----BEGIN CERTIFICATE-----", out)

    def test_base64_pem(self) -> None:
        import base64

        pem = b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
        out = self.mod.decode_pem_field(base64.b64encode(pem).decode("ascii"), "certificate")
        self.assertEqual(out, pem)

    def test_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            self.mod.decode_pem_field("   ", "certificate")


@unittest.skipUnless(OPENSSL, "openssl not on PATH")
class TestCertPair(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _env(self) -> dict[str, str]:
        tls = self.td / "tls"
        state = self.td / "state"
        envf = self.td / "nexvue.env"
        tls.mkdir()
        state.mkdir()
        envf.write_text("", encoding="utf-8")
        return {
            "NEXVUE_TLS_DIR": str(tls),
            "NEXVUE_TLS_STATE_DIR": str(state),
            "NEXVUE_STATION_ENV": str(envf),
            "NEXVUE_LEGO": str(self.td / "missing-lego"),
        }

    def test_validate_and_install(self) -> None:
        cert, key = _make_pair(self.td)
        info = self.mod.validate_pair(cert, key)
        self.assertTrue(info["self_signed"])
        self.assertGreaterEqual(info["days_left"], 0)
        env = self._env()
        os.environ.update(env)
        try:
            installed = self.mod.install_pair(cert, key, "upload")
            self.assertTrue(installed["installed"])
            self.assertEqual((self.td / "tls" / "fullchain.pem").read_bytes().strip(), cert.strip())
            self.assertEqual((self.td / "state" / "source").read_text(encoding="utf-8").strip(), "upload")
        finally:
            for k in env:
                os.environ.pop(k, None)

    def test_mismatch_rejected(self) -> None:
        cert_a, _key_a = _make_pair(self.td, cn="a.example.com")
        other = self.td / "other"
        other.mkdir()
        _c, key_b = _make_pair(other, cn="b.example.com")
        with self.assertRaises(ValueError):
            self.mod.validate_pair(cert_a, key_b)

    def test_should_renew_skips_self_signed(self) -> None:
        cert, key = _make_pair(self.td, days=5)
        env = self._env()
        os.environ.update(env)
        try:
            self.mod.install_pair(cert, key, "upload")
            (self.td / "nexvue.env").write_text(
                "NEXVUE_TLS_EMAIL=ops@example.com\nNEXVUE_TLS_DOMAIN=nexvue.example.com\n",
                encoding="utf-8",
            )
            renew, reason = self.mod.should_renew()
            self.assertFalse(renew)
            self.assertIn("not from Let's Encrypt", reason)
        finally:
            for k in env:
                os.environ.pop(k, None)


class TestCliConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _env(self) -> dict[str, str]:
        tls = self.td / "tls"
        state = self.td / "state"
        envf = self.td / "nexvue.env"
        tls.mkdir()
        state.mkdir()
        envf.write_text("", encoding="utf-8")
        return {
            "NEXVUE_TLS_DIR": str(tls),
            "NEXVUE_TLS_STATE_DIR": str(state),
            "NEXVUE_STATION_ENV": str(envf),
            "NEXVUE_LEGO": str(self.td / "missing-lego"),
        }

    def test_cli_config_and_status(self) -> None:
        env = self._env()
        (self.td / "nexvue.env").write_text(
            "NEXVUE_PUBLIC_HOSTNAME=edge.example.com\n",
            encoding="utf-8",
        )
        r = _run_py(
            ["config"],
            env,
            json.dumps({"email": "ops@example.com"}),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertTrue(data["ok"])
        text = (self.td / "nexvue.env").read_text(encoding="utf-8")
        self.assertIn("NEXVUE_TLS_EMAIL=ops@example.com", text)
        self.assertIn("NEXVUE_TLS_DOMAIN=edge.example.com", text)
        st = _run_py(["status"], env)
        self.assertEqual(st.returncode, 0, st.stderr)
        status = json.loads(st.stdout)
        self.assertEqual(status["email"], "ops@example.com")
        self.assertEqual(status["resolved_domain"], "edge.example.com")
        self.assertEqual(status["public_hostname"], "edge.example.com")
        self.assertEqual(status["challenge"], "tls-alpn-01")
        self.assertFalse(status["present"])

    def test_cli_config_uses_public_hostname_not_body_domain(self) -> None:
        env = self._env()
        (self.td / "nexvue.env").write_text(
            "NEXVUE_PUBLIC_HOSTNAME=nexvue.example.com\nNEXVUE_TLS_DOMAIN=old.example.com\n",
            encoding="utf-8",
        )
        r = _run_py(
            ["config"],
            env,
            json.dumps({"email": "ops@example.com", "domain": "ignored.example.com"}),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (self.td / "nexvue.env").read_text(encoding="utf-8")
        self.assertIn("NEXVUE_TLS_DOMAIN=nexvue.example.com", text)
        self.assertNotIn("ignored.example.com", text)
        self.assertNotIn("old.example.com", text)

    def test_cli_config_fails_without_hostname(self) -> None:
        env = self._env()
        r = _run_py(
            ["config"],
            env,
            json.dumps({"email": "ops@example.com"}),
        )
        self.assertNotEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertFalse(data.get("ok", True))
        self.assertIn("Public hostname", data.get("error", ""))

    def test_cli_config_legacy_body_domain(self) -> None:
        env = self._env()
        r = _run_py(
            ["config"],
            env,
            json.dumps({"email": "ops@example.com", "domain": "legacy.example.com"}),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (self.td / "nexvue.env").read_text(encoding="utf-8")
        self.assertIn("NEXVUE_TLS_DOMAIN=legacy.example.com", text)


class TestShouldRenewLogic(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_not_due_when_far_out(self) -> None:
        info = {"present": True, "source": "lego", "days_left": 80}
        cfg = {"email": "a@b.example", "domain": "edge.example.com", "public_hostname": ""}
        renew, reason = self.mod.should_renew(info, cfg)
        self.assertFalse(renew)
        self.assertIn("not due", reason)

    def test_due_when_close(self) -> None:
        info = {"present": True, "source": "lego", "days_left": 10}
        cfg = {"email": "a@b.example", "domain": "edge.example.com", "public_hostname": ""}
        renew, _reason = self.mod.should_renew(info, cfg)
        self.assertTrue(renew)

    def test_falls_back_to_public_hostname(self) -> None:
        info = {"present": True, "source": "lego", "days_left": 5}
        cfg = {"email": "a@b.example", "domain": "", "public_hostname": "nexvue.example.com"}
        renew, _reason = self.mod.should_renew(info, cfg)
        self.assertTrue(renew)

    def test_falls_back_to_legacy_tls_domain(self) -> None:
        info = {"present": True, "source": "lego", "days_left": 5}
        cfg = {"email": "a@b.example", "domain": "legacy.example.com", "public_hostname": ""}
        renew, _reason = self.mod.should_renew(info, cfg)
        self.assertTrue(renew)

    def test_public_hostname_wins_over_stale_tls_domain(self) -> None:
        self.assertEqual(
            self.mod.resolve_domain({
                "email": "a@b.example",
                "domain": "old.example.com",
                "public_hostname": "nexvue.example.com",
            }),
            "nexvue.example.com",
        )


class TestShellRenewSkip(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_renew_skips_without_config(self) -> None:
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not on PATH")
        env = {
            **os.environ,
            "NEXVUE_TLS_PY": str(TLS_PY),
            "NEXVUE_PYTHON": sys.executable,
            "NEXVUE_TLS_STATE_DIR": str(self.td / "state"),
            "NEXVUE_STATION_ENV": str(self.td / "nexvue.env"),
            "NEXVUE_TLS_DIR": str(self.td / "tls"),
            "NEXVUE_TLS_SKIP_APACHE": "1",
            "NEXVUE_TLS_SKIP_MEDIAMTX": "1",
            "NEXVUE_TLS_SYNC": "1",
        }
        (self.td / "tls").mkdir()
        (self.td / "state").mkdir()
        (self.td / "nexvue.env").write_text("", encoding="utf-8")
        r = subprocess.run(
            [bash, str(TLS_SH), "renew"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        data = json.loads(r.stdout)
        self.assertTrue(data.get("skipped"))


class TestWiring(unittest.TestCase):
    def test_files_exist(self) -> None:
        for p in (
            TLS_PY,
            TLS_SH,
            DEPLOY_SH,
            ROOT / "nexvue-tls-issue.service",
            ROOT / "nexvue-tls-renew.service",
            ROOT / "nexvue-tls-renew.timer",
        ):
            self.assertTrue(p.is_file(), p)

    def test_setup_lists_helpers(self) -> None:
        text = SETUP.read_text(encoding="utf-8")
        self.assertIn("nexvue-ops-tls.sh", text)
        self.assertIn("nexvue-tls.py", text)
        self.assertIn("ensure_lego", text)
        self.assertIn('lego_v${ver}_linux_${arch}.tar.gz', text)
        self.assertIn("LEGO_VERSION=5.3.1", text)
        self.assertIn("nexvue-tls-renew.timer", text)
        self.assertNotIn("http-01", text.lower())

    def test_sudoers_allowlists_tls(self) -> None:
        text = SUDOERS.read_text(encoding="utf-8")
        self.assertIn("nexvue-ops-tls.sh", text)

    def test_ops_php_admin_only_mutations(self) -> None:
        text = OPS_PHP.read_text(encoding="utf-8")
        start = text.index("$adminOnly = [")
        block = text[start:text.index("];", start)]
        self.assertIn("'tls_status', 'tls_issue', 'tls_upload'", block)
        self.assertIn("if ($action === 'tls_status')", text)

    def test_settings_panel(self) -> None:
        text = CHANNELS.read_text(encoding="utf-8")
        self.assertIn('id="tls-panel"', text)
        self.assertIn("tls_issue", text)
        self.assertIn("TLS-ALPN-01", text)
        self.assertIn("Let's Encrypt Subscriber Agreement", text)
        self.assertNotIn('id="tls-domain"', text)
        self.assertIn('id="tls-hostname"', text)
        self.assertIn("Set a Public hostname under Public reachability first", text)
        self.assertIn("admin-only, same gate as Public reachability", text)

    def test_wrapper_mentions_443_only(self) -> None:
        text = TLS_SH.read_text(encoding="utf-8")
        self.assertIn("TLS-ALPN-01", text)
        self.assertIn("port 80 is never used", text)
        self.assertIn("stop apache2", text)
        self.assertIn("TimeoutStartSec=10min", (ROOT / "nexvue-tls-issue.service").read_text(encoding="utf-8"))
        self.assertIn("TimeoutStopSec=45", (ROOT / "nexvue-tls-issue.service").read_text(encoding="utf-8"))
        self.assertNotIn("TimeoutStartSec=180", (ROOT / "nexvue-tls-issue.service").read_text(encoding="utf-8"))
        self.assertIn("Last request failed:", CHANNELS.read_text(encoding="utf-8"))

    def test_deploy_hook_reads_lego_v5_env(self) -> None:
        text = TLS_PY.read_text(encoding="utf-8")
        self.assertIn("LEGO_HOOK_CERT_PATH", text)
        self.assertIn("LEGO_HOOK_CERT_KEY_PATH", text)
        hook = DEPLOY_SH.read_text(encoding="utf-8")
        self.assertIn("LEGO_HOOK_CERT_", hook)


class TestLegoDeployPaths(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self._saved = {
            k: os.environ.get(k)
            for k in (
                "LEGO_HOOK_CERT_PATH",
                "LEGO_HOOK_CERT_KEY_PATH",
                "LEGO_CERT_PATH",
                "LEGO_CERT_KEY_PATH",
                "NEXVUE_LEGO_PATH",
                "NEXVUE_STATION_ENV",
            )
        }

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _clear_hook_env(self) -> None:
        for k in (
            "LEGO_HOOK_CERT_PATH",
            "LEGO_HOOK_CERT_KEY_PATH",
            "LEGO_CERT_PATH",
            "LEGO_CERT_KEY_PATH",
        ):
            os.environ.pop(k, None)

    def test_prefers_v5_hook_vars(self) -> None:
        self._clear_hook_env()
        os.environ["LEGO_HOOK_CERT_PATH"] = "/v5/cert.crt"
        os.environ["LEGO_HOOK_CERT_KEY_PATH"] = "/v5/cert.key"
        os.environ["LEGO_CERT_PATH"] = "/v4/cert.crt"
        os.environ["LEGO_CERT_KEY_PATH"] = "/v4/cert.key"
        cert, key = self.mod.lego_deploy_paths()
        self.assertEqual(cert, Path("/v5/cert.crt"))
        self.assertEqual(key, Path("/v5/cert.key"))

    def test_falls_back_to_v4_vars(self) -> None:
        self._clear_hook_env()
        os.environ["LEGO_CERT_PATH"] = "/v4/cert.crt"
        os.environ["LEGO_CERT_KEY_PATH"] = "/v4/cert.key"
        cert, key = self.mod.lego_deploy_paths()
        self.assertEqual(cert, Path("/v4/cert.crt"))
        self.assertEqual(key, Path("/v4/cert.key"))

    def test_falls_back_to_lego_store(self) -> None:
        self._clear_hook_env()
        store = self.td / "lego" / "certificates"
        store.mkdir(parents=True)
        crt = store / "nexvue.example.com.crt"
        keyp = store / "nexvue.example.com.key"
        crt.write_text("crt", encoding="utf-8")
        keyp.write_text("key", encoding="utf-8")
        envf = self.td / "nexvue.env"
        envf.write_text("NEXVUE_PUBLIC_HOSTNAME=nexvue.example.com\n", encoding="utf-8")
        os.environ["NEXVUE_LEGO_PATH"] = str(self.td / "lego")
        os.environ["NEXVUE_STATION_ENV"] = str(envf)
        cert, key = self.mod.lego_deploy_paths()
        self.assertEqual(cert, crt)
        self.assertEqual(key, keyp)

    def test_missing_paths_raise(self) -> None:
        self._clear_hook_env()
        os.environ["NEXVUE_LEGO_PATH"] = str(self.td / "empty-lego")
        os.environ["NEXVUE_STATION_ENV"] = str(self.td / "missing.env")
        with self.assertRaises(ValueError):
            self.mod.lego_deploy_paths()


def _dead_pid() -> int:
    pid = (1 << 22) - 1
    while pid > 2:
        if not Path(f"/proc/{pid}").exists():
            return pid
        pid -= 1
    return (1 << 22) - 1


def _spawn_worker() -> subprocess.Popen:
    """A process whose cmdline matches the issue script, so stale-pid checks own it."""
    return subprocess.Popen(
        ["bash", "-c", "exec -a nexvue-ops-tls sleep 120"],
        start_new_session=True,
    )


def _stop_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=2)


class TestIssueBusyState(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.state = self.td / "state"
        self.state.mkdir()
        self._saved = {
            key: os.environ.get(key)
            for key in ("NEXVUE_TLS_STATE_DIR", "NEXVUE_TLS_JOB_MAX_AGE")
        }
        os.environ["NEXVUE_TLS_STATE_DIR"] = str(self.state)
        os.environ["NEXVUE_TLS_JOB_MAX_AGE"] = "660"

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _job(self, **fields: object) -> dict:
        job = {
            "busy": False,
            "action": "issue",
            "ok": None,
            "error": "",
            "started_at": 0,
            "finished_at": 0,
        }
        job.update(fields)
        (self.state / "job.json").write_text(json.dumps(job) + "\n", encoding="utf-8")
        return job

    def test_dead_pid_is_not_busy_and_records_why(self) -> None:
        now = 1_700_000_000
        (self.state / "issue.lock").write_text(f"{_dead_pid()}\n", encoding="utf-8")
        job = self._job(busy=True, started_at=now - 5)
        state = self.mod.apply_reconcile(job, "", 0, now=now)
        self.assertFalse(state["busy"])
        self.assertIn("interrupted", state["reason"])
        self.assertFalse((self.state / "issue.lock").exists())
        saved = json.loads((self.state / "job.json").read_text(encoding="utf-8"))
        self.assertFalse(saved["busy"])
        self.assertIs(saved["ok"], False)
        self.assertIn("interrupted", saved["error"])

    def test_finished_job_keeps_success_when_lock_is_dead(self) -> None:
        now = 1_700_000_000
        (self.state / "issue.lock").write_text(f"{_dead_pid()}\n", encoding="utf-8")
        job = self._job(busy=False, ok=True, error="", started_at=now - 20, finished_at=now - 10)
        state = self.mod.apply_reconcile(job, "", 0, now=now)
        self.assertFalse(state["busy"])
        self.assertFalse((self.state / "issue.lock").exists())
        saved = json.loads((self.state / "job.json").read_text(encoding="utf-8"))
        self.assertIs(saved["ok"], True)
        self.assertEqual(saved["error"], "")

    def test_young_worker_stays_busy(self) -> None:
        proc = _spawn_worker()
        try:
            now = time.time()
            (self.state / "issue.lock").write_text(f"{proc.pid}\n", encoding="utf-8")
            job = self._job(busy=True, started_at=int(now))
            state = self.mod.issue_busy_state(job, "", 0, now=now)
            self.assertTrue(state["busy"])
            self.assertTrue(state["live_pid"])
            self.assertEqual(state["kill_pid"], 0)
            self.assertIsNone(proc.poll())
        finally:
            _stop_proc(proc)

    def test_expired_worker_is_not_busy_and_not_killed_here(self) -> None:
        proc = _spawn_worker()
        try:
            now = time.time()
            os.environ["NEXVUE_TLS_JOB_MAX_AGE"] = "30"
            (self.state / "issue.lock").write_text(f"{proc.pid}\n", encoding="utf-8")
            job = self._job(busy=True, started_at=int(now) - 120)
            state = self.mod.apply_reconcile(job, "activating", proc.pid, now=now)
            self.assertFalse(state["busy"])
            self.assertIn("timed out", state["reason"])
            self.assertEqual(state["kill_pid"], proc.pid)
            self.assertTrue(state["stop_unit"])
            self.assertTrue((self.state / "issue.lock").exists())
            self.assertIsNone(proc.poll())
        finally:
            _stop_proc(proc)

    def test_activating_without_a_lock_is_busy(self) -> None:
        job = self._job(busy=False, started_at=0)
        state = self.mod.issue_busy_state(job, "activating", 0, now=time.time())
        self.assertTrue(state["busy"])

    def test_activating_with_a_dead_main_pid_is_not_busy(self) -> None:
        now = time.time()
        job = self._job(busy=True, started_at=int(now))
        state = self.mod.issue_busy_state(job, "activating", _dead_pid(), now=now)
        self.assertFalse(state["busy"])
        self.assertTrue(state["stop_unit"])

    def test_lego_log_hint_is_the_failure_reason(self) -> None:
        now = time.time()
        (self.state / "lego.log").write_text(
            "starting tls-alpn\nerror: dns lookup failed\n",
            encoding="utf-8",
        )
        (self.state / "issue.lock").write_text(f"{_dead_pid()}\n", encoding="utf-8")
        job = self._job(busy=True, started_at=int(now) - 2)
        state = self.mod.apply_reconcile(job, "", 0, now=now)
        self.assertIn("dns lookup failed", state["reason"])
        saved = json.loads((self.state / "job.json").read_text(encoding="utf-8"))
        self.assertIn("dns lookup failed", saved["error"])


class TestTlsIssueLock(unittest.TestCase):
    def setUp(self) -> None:
        self.bash = shutil.which("bash")
        if not self.bash:
            self.skipTest("bash not on PATH")
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.state = self.td / "state"
        self.state.mkdir()
        (self.td / "tls").mkdir()
        (self.td / "lego-path").mkdir()
        self.env_file = self.td / "nexvue.env"
        self.env_file.write_text(
            "NEXVUE_PUBLIC_HOSTNAME=nexvue.example.com\nNEXVUE_TLS_EMAIL=ops@example.com\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _env(self, **extra: str) -> dict[str, str]:
        env = {
            **os.environ,
            "NEXVUE_TLS_PY": str(TLS_PY),
            "NEXVUE_PYTHON": sys.executable,
            "NEXVUE_TLS_STATE_DIR": str(self.state),
            "NEXVUE_STATION_ENV": str(self.env_file),
            "NEXVUE_TLS_DIR": str(self.td / "tls"),
            "NEXVUE_DATA": str(self.td / "data"),
            "NEXVUE_ETC": str(self.td / "etc"),
            "NEXVUE_LEGO_PATH": str(self.td / "lego-path"),
            "NEXVUE_TLS_SKIP_APACHE": "1",
            "NEXVUE_TLS_SKIP_MEDIAMTX": "1",
            "NEXVUE_TLS_SYNC": "1",
            "NEXVUE_TLS_UNIT": "nexvue-tls-issue-not-installed.service",
            "NEXVUE_TLS_JOB_MAX_AGE": "660",
        }
        env.update(extra)
        return env

    def _run(
        self,
        args: list[str],
        env: dict[str, str] | None = None,
        stdin: str = "",
        timeout: float = 30,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.bash, str(TLS_SH), *args],
            input=stdin,
            text=True,
            capture_output=True,
            env=env or self._env(),
            check=False,
            timeout=timeout,
        )

    def _job(self, **fields: object) -> None:
        job = {
            "busy": False,
            "action": "issue",
            "ok": None,
            "error": "",
            "started_at": 0,
            "finished_at": 0,
        }
        job.update(fields)
        (self.state / "job.json").write_text(json.dumps(job) + "\n", encoding="utf-8")

    def _read_job(self) -> dict:
        return json.loads((self.state / "job.json").read_text(encoding="utf-8"))

    def _fake_lego(self, body: str) -> Path:
        path = self.td / "fake-lego"
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_success_clears_lock_and_does_not_trip_set_u(self) -> None:
        lego = self._fake_lego("#!/bin/sh\necho issued-by-fake-lego\nexit 0\n")
        result = self._run(["run"], env=self._env(NEXVUE_LEGO=str(lego)))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn("unbound variable", result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["issued"])
        self.assertFalse((self.state / "issue.lock").is_file())
        job = self._read_job()
        self.assertFalse(job["busy"])
        self.assertIs(job["ok"], True)
        log = (self.state / "lego.log").read_text(encoding="utf-8")
        self.assertIn("issued-by-fake-lego", log)
        self.assertIn("issued-by-fake-lego", result.stderr)

    def test_failure_keeps_the_log_and_puts_the_reason_on_the_job(self) -> None:
        lego = self._fake_lego(
            "#!/bin/sh\n"
            "echo starting tls-alpn\n"
            "echo 'error: challenge failed: connection refused'\n"
            "exit 1\n"
        )
        result = self._run(["run"], env=self._env(NEXVUE_LEGO=str(lego)))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("unbound variable", result.stderr)
        data = json.loads(result.stdout)
        self.assertIn("connection refused", data["error"])
        self.assertFalse((self.state / "issue.lock").is_file())
        job = self._read_job()
        self.assertFalse(job["busy"])
        self.assertIs(job["ok"], False)
        self.assertIn("connection refused", job["error"])
        log = (self.state / "lego.log").read_text(encoding="utf-8")
        self.assertIn("starting tls-alpn", log)
        self.assertIn("connection refused", log)

    def test_lego_log_rotates(self) -> None:
        first = self._fake_lego("#!/bin/sh\necho first-run-marker\nexit 1\n")
        self._run(["run"], env=self._env(NEXVUE_LEGO=str(first)))
        second = self._fake_lego("#!/bin/sh\necho second-run-marker\nexit 1\n")
        self._run(["run"], env=self._env(NEXVUE_LEGO=str(second)))
        self.assertIn("second-run-marker", (self.state / "lego.log").read_text(encoding="utf-8"))
        self.assertIn("first-run-marker", (self.state / "lego.log.1").read_text(encoding="utf-8"))

    def test_missing_hostname_does_not_leave_a_lock(self) -> None:
        self.env_file.write_text("", encoding="utf-8")
        lego = self._fake_lego("#!/bin/sh\nexit 0\n")
        result = self._run(["run"], env=self._env(NEXVUE_LEGO=str(lego)))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Public hostname", result.stdout)
        self.assertNotIn("unbound variable", result.stderr)
        self.assertFalse((self.state / "issue.lock").is_file())

    def test_sigterm_clears_the_lock(self) -> None:
        lego = self._fake_lego("#!/bin/sh\necho lego waiting\nexec sleep 120\n")
        proc = subprocess.Popen(
            [self.bash, str(TLS_SH), "run"],
            env=self._env(NEXVUE_LEGO=str(lego)),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        lock = self.state / "issue.lock"
        try:
            for _ in range(100):
                if lock.is_file() or proc.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertTrue(lock.is_file(), "lock was not created")
            os.killpg(proc.pid, signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=10)
        finally:
            _stop_proc(proc)
        self.assertNotIn("unbound variable", stderr)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(lock.is_file())
        job = self._read_job()
        self.assertFalse(job["busy"])
        self.assertTrue(job["error"])
        self.assertNotIn("unbound variable", stdout)

    def test_status_clears_a_dead_lock(self) -> None:
        self._job(busy=True, started_at=int(time.time()))
        (self.state / "issue.lock").write_text(f"{_dead_pid()}\n", encoding="utf-8")
        result = self._run(["status"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        data = json.loads(result.stdout)
        self.assertFalse(data["busy"])
        self.assertFalse(data["job"]["busy"])
        self.assertIn("interrupted", data["job"]["error"])
        self.assertFalse((self.state / "issue.lock").is_file())

    def test_status_keeps_a_young_live_worker(self) -> None:
        proc = _spawn_worker()
        try:
            self._job(busy=True, started_at=int(time.time()))
            (self.state / "issue.lock").write_text(f"{proc.pid}\n", encoding="utf-8")
            result = self._run(["status"])
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads(result.stdout)
            self.assertTrue(data["busy"])
            self.assertTrue((self.state / "issue.lock").is_file())
            self.assertIsNone(proc.poll())
        finally:
            _stop_proc(proc)

    def test_renew_recovers_a_dead_lock_instead_of_skipping(self) -> None:
        self.env_file.write_text("", encoding="utf-8")
        self._job(busy=True, started_at=int(time.time()))
        (self.state / "issue.lock").write_text(f"{_dead_pid()}\n", encoding="utf-8")
        result = self._run(["renew"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        data = json.loads(result.stdout)
        self.assertTrue(data["skipped"])
        self.assertNotEqual(data.get("reason"), "already running")
        self.assertFalse((self.state / "issue.lock").is_file())
        self.assertFalse(self._read_job()["busy"])

    def test_renew_leaves_a_young_worker_alone(self) -> None:
        self.env_file.write_text("", encoding="utf-8")
        proc = _spawn_worker()
        try:
            self._job(busy=True, started_at=int(time.time()))
            (self.state / "issue.lock").write_text(f"{proc.pid}\n", encoding="utf-8")
            result = self._run(["renew"])
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads(result.stdout)
            self.assertTrue(data["skipped"])
            self.assertEqual(data.get("reason"), "already running")
            self.assertTrue((self.state / "issue.lock").is_file())
            self.assertIsNone(proc.poll())
        finally:
            _stop_proc(proc)

    def test_renew_stops_an_expired_worker(self) -> None:
        self.env_file.write_text("", encoding="utf-8")
        proc = _spawn_worker()
        try:
            self._job(busy=True, started_at=int(time.time()) - 120)
            (self.state / "issue.lock").write_text(f"{proc.pid}\n", encoding="utf-8")
            result = self._run(["renew"], env=self._env(NEXVUE_TLS_JOB_MAX_AGE="30"))
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads(result.stdout)
            self.assertTrue(data["skipped"])
            self.assertNotEqual(data.get("reason"), "already running")
            self.assertFalse((self.state / "issue.lock").is_file())
            proc.wait(timeout=3)
        finally:
            _stop_proc(proc)

    def test_issue_runs_when_the_lock_is_already_dead(self) -> None:
        lego = self._fake_lego("#!/bin/sh\necho ok-from-lego\nexit 0\n")
        self._job(busy=True, started_at=int(time.time()))
        (self.state / "issue.lock").write_text(f"{_dead_pid()}\n", encoding="utf-8")
        result = self._run(
            ["issue"],
            env=self._env(NEXVUE_LEGO=str(lego)),
            stdin=json.dumps({"email": "ops@example.com", "accept_tos": True}),
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn("unbound variable", result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data.get("issued"))
        self.assertFalse((self.state / "issue.lock").is_file())
        self.assertFalse(self._read_job()["busy"])

    def test_apache_stop_and_start_are_visible_to_cleanup(self) -> None:
        log = self.td / "systemctl.log"
        bindir = self.td / "bin"
        bindir.mkdir()
        fake = bindir / "systemctl"
        fake.write_text(
            "#!/bin/sh\n"
            f"echo \"$*\" >> \"{log}\"\n"
            "if [ \"$1\" = is-active ] && [ \"$3\" = apache2 ]; then\n"
            "  exit 0\n"
            "fi\n"
            "if [ \"$1\" = show ]; then\n"
            "  echo inactive\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        lego = self._fake_lego("#!/bin/sh\necho issued-by-fake-lego\nexit 0\n")
        env = self._env(
            NEXVUE_LEGO=str(lego),
            NEXVUE_TLS_SKIP_APACHE="0",
            PATH=str(bindir) + os.pathsep + os.environ.get("PATH", ""),
        )
        result = self._run(["run"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn("unbound variable", result.stderr)
        text = log.read_text(encoding="utf-8")
        self.assertIn("stop apache2", text)
        self.assertIn("start apache2", text)
        self.assertFalse((self.state / "issue.lock").is_file())


if __name__ == "__main__":
    unittest.main()

