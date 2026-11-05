#!/usr/bin/env python3
"""Weekly metrics pack: slots, redaction, dry-run, S3 key layout.

Run: python3 test/test_nexvue_weekly_pack.py
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pack = _load("nexvue_weekly_pack", "nexvue-weekly-pack.py")
bundle = _load("nexvue_support_bundle", "nexvue-support-bundle.py")

END = datetime(2026, 11, 9, 4, 0, tzinfo=timezone.utc)  # Sun 23:00 EST
START = END - timedelta(days=7)


def _quiet_runner(argv, timeout=30.0):
    del timeout
    cmd = argv[0]
    if cmd == "systemctl":
        if argv[1] == "is-active":
            return 0, "active\n", ""
        if argv[1] == "is-enabled":
            return 0, "enabled\n", ""
    if cmd == "journalctl":
        return 0, "", ""
    if cmd == "timedatectl":
        return 0, "America/New_York\n", ""
    return 127, "", f"unexpected {' '.join(argv)}"


class EnvGuard(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.copy()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved)


class TestSlots(EnvGuard):
    def test_quad_is_zero_through_seven(self) -> None:
        slots, source, dropped = pack.resolve_encode_slots(
            max_channels=8,
            max_devices=8,
            channel_indices=[0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
            decklink=list(range(8)),
        )
        self.assertEqual(slots, list(range(8)))
        self.assertEqual(source, "max_channels+decklink")
        self.assertEqual(dropped, [])

    def test_channel_files_above_quad_cap_are_not_slots(self) -> None:
        kept, ignored = pack.channel_env_indices(Path("/no/such"))
        self.assertEqual(kept, [])
        with self._tmpdir() as raw:
            channels = Path(raw)
            for idx in (0, 1, 7, 8, 9):
                (channels / f"{idx}.env").write_text("CHANNEL_PATH=ch\n", encoding="utf-8")
            kept, ignored = pack.channel_env_indices(channels)
        self.assertEqual(kept, [0, 1, 7])
        self.assertEqual(ignored, [8, 9])

    def test_duo_max_devices_not_the_whole_quad(self) -> None:
        slots, source, dropped = pack.resolve_encode_slots(
            max_channels=None,
            max_devices=2,
            channel_indices=list(range(8)),
            decklink=None,
        )
        self.assertEqual(slots, [0, 1])
        self.assertEqual(source, "max_devices")
        self.assertEqual(dropped, [])

    def test_decklink_drops_slots_the_card_does_not_have(self) -> None:
        slots, source, dropped = pack.resolve_encode_slots(
            max_channels=8,
            max_devices=8,
            channel_indices=list(range(8)),
            decklink=[0, 1, 2, 3],
        )
        self.assertEqual(slots, [0, 1, 2, 3])
        self.assertEqual(source, "max_channels+decklink")
        self.assertEqual(dropped, [4, 5, 6, 7])

    def test_empty_decklink_does_not_wipe_configured_slots(self) -> None:
        self.assertIsNone(pack.decklink_indices({"devices": [], "error": "no_decklink_api"}))
        slots, source, _dropped = pack.resolve_encode_slots(
            max_channels=8,
            max_devices=None,
            channel_indices=[],
            decklink=None,
        )
        self.assertEqual(slots, list(range(8)))
        self.assertEqual(source, "max_channels")

    def test_channel_env_fallback(self) -> None:
        slots, source, _dropped = pack.resolve_encode_slots(
            max_channels=None,
            max_devices=None,
            channel_indices=[0, 2],
            decklink=None,
        )
        self.assertEqual(slots, [0, 2])
        self.assertEqual(source, "channel_env")

    def _tmpdir(self):
        import tempfile

        return tempfile.TemporaryDirectory()


class TestRedaction(unittest.TestCase):
    def test_matches_support_bundle(self) -> None:
        sample = "publish srt://user:hunter2@10.0.0.1:9000 SRT_PASSPHRASE=hunter2 ok"
        self.assertEqual(pack.redact_text(sample), bundle.redact_text(sample))
        self.assertNotIn("hunter2", pack.redact_text(sample))
        obj = {"SRT_URI": "srt://a:b@host/x", "CHANNEL_ALIAS": "Cam 1", "password": "nope"}
        self.assertEqual(pack.redact_obj(obj), bundle.redact_obj(obj))


class TestIsoWeek(unittest.TestCase):
    def test_sunday_evening_eastern_is_not_the_utc_week(self) -> None:
        local = END.astimezone(pack.ZoneInfo("America/New_York"))
        self.assertEqual(local.isoformat(), "2026-11-08T23:00:00-05:00")
        self.assertEqual(pack.iso_week_key(local), "2026-W45")
        self.assertEqual(pack.iso_week_key(END), "2026-W46")


class TestThin(unittest.TestCase):
    def test_keeps_ends_and_spreads_the_middle(self) -> None:
        rows = [{"ts": i} for i in range(100)]
        kept = pack.thin_samples(rows, 10)
        self.assertEqual(len(kept), 10)
        self.assertEqual(kept[0]["ts"], 0)
        self.assertEqual(kept[-1]["ts"], 99)


class TestBuild(EnvGuard):
    def _stage(self, root: Path, *, max_channels: str = "8") -> Path:
        etc = root / "etc"
        channels = etc / "channels"
        channels.mkdir(parents=True)
        (etc / "nexvue.env").write_text(
            "\n".join(
                [
                    f"MAX_CHANNELS={max_channels}",
                    "MAX_DEVICES=8",
                    "NEXVUE_METRICS_TZ=America/New_York",
                    "NEXVUE_S3_BUCKET=nexvue-metrics",
                    "NEXVUE_S3_REGION=us-east-1",
                    "NEXVUE_SITE_ID=nyc-cof1",
                    "NEXVUE_PORTAL_STATION_ID=sta-should-lose",
                    "NEXVUE_PORTAL_STATION_API_KEY=supersecretkeyvalue",
                    "NEXVUE_PUBLISH_JWT=eyJhbGciOi-not-really",
                    "SRT_PASSPHRASE=hunter2",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        for idx in range(10):
            (channels / f"{idx}.env").write_text(
                f"CHANNEL_PATH=ch{idx}\nSRT_URI=srt://u:p@10.0.0.1:9000\n",
                encoding="utf-8",
            )
        data = root / "data"
        data.mkdir()
        (root / "VERSION").write_text("9.9.9\n", encoding="utf-8")
        db_path = data / "metrics.db"
        inside = int(START.timestamp()) + 120
        outside = int(START.timestamp()) - 120
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE host_samples (
                ts INTEGER PRIMARY KEY,
                cpu_pct REAL,
                mem_used_bytes INTEGER,
                mem_total_bytes INTEGER,
                load1 REAL,
                note TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO host_samples VALUES (?,?,?,?,?,?)",
            (outside, 1, 1, 2, 0.1, "srt://user:hunter2@10.9.9.9/old"),
        )
        conn.execute(
            "INSERT INTO host_samples VALUES (?,?,?,?,?,?)",
            (inside, 10, 100, 200, 0.4, "srt://user:hunter2@10.9.9.9/live"),
        )
        conn.execute(
            "INSERT INTO host_samples VALUES (?,?,?,?,?,?)",
            (inside + 3600, 30, 150, 200, 1.2, "plain"),
        )
        conn.execute(
            """
            CREATE TABLE totals (
                ts INTEGER PRIMARY KEY,
                active_streams INTEGER,
                total_readers INTEGER,
                total_bandwidth_bps REAL
            )
            """
        )
        conn.execute("INSERT INTO totals VALUES (?,?,?,?)", (inside, 2, 3, 1000.0))
        conn.execute(
            """
            CREATE TABLE viewer_sessions (
                session_id TEXT PRIMARY KEY,
                remote_addr TEXT,
                channel TEXT,
                user_agent TEXT,
                first_seen INTEGER,
                last_seen INTEGER,
                bytes_sent INTEGER,
                user TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO viewer_sessions VALUES (?,?,?,?,?,?,?,?)",
            ("s1", "203.0.113.50", "ch0", "secret-agent", inside, inside + 10, 1000, "ada@example.com"),
        )
        conn.execute(
            "INSERT INTO viewer_sessions VALUES (?,?,?,?,?,?,?,?)",
            ("s2", "203.0.113.50", "ch1", "secret-agent", inside, inside + 20, 500, "ada@example.com"),
        )
        conn.commit()
        conn.close()
        os.environ["NEXVUE_ETC"] = str(etc)
        os.environ["NEXVUE_DATA"] = str(data)
        os.environ["NEXVUE_METRICS_DB"] = str(db_path)
        os.environ["NEXVUE_VERSION_FILE"] = str(root / "VERSION")
        os.environ["NEXVUE_METRICS_TZ"] = "America/New_York"
        os.environ["NEXVUE_S3_BUCKET"] = "nexvue-metrics"
        os.environ["NEXVUE_S3_REGION"] = "us-east-1"
        return db_path

    def _decklink(self, indexes=range(8)):
        return {
            "devices": [
                {
                    "index": idx,
                    "name": f"DeckLink Quad ({idx + 1})",
                    "input_locked": True,
                    "input_mode": "1080i59.94",
                }
                for idx in indexes
            ],
            "note": "srt://user:hunter2@10.1.1.1/live",
        }

    def _build(self, root: Path, **kwargs):
        import tempfile

        out = Path(tempfile.mkdtemp(dir=root))
        seen: list[str] = []

        def runner(argv, timeout=30.0):
            del timeout
            if argv[0] == "systemctl":
                seen.append(argv[2])
                return (0, "active\n", "") if argv[1] == "is-active" else (0, "enabled\n", "")
            if argv[0] == "journalctl":
                unit = argv[argv.index("-u") + 1]
                seen.append(unit)
                if unit == "nexvue-encode@1":
                    text = "\n".join(
                        [
                            "Started NexVUE encoder channel 1",
                            "SRT_PASSPHRASE=hunter2",
                            "nexvue-encode@1.service: Main process exited, code=exited, status=1/FAILURE",
                            "nexvue-encode@1.service: Failed with result 'exit-code'.",
                            "nexvue-encode@1.service: Scheduled restart job, restart counter is at 2.",
                            "Stopped NexVUE encoder channel 1",
                        ]
                    )
                    return 0, text + "\n", ""
                return 0, "", ""
            return _quiet_runner(argv)

        result = pack.build_pack(
            now=END,
            days=7,
            out_dir=out,
            hostname=kwargs.get("hostname", "nynycof1nexvue01"),
            runner=runner,
            decklink=kwargs.get("decklink", self._decklink()),
        )
        return result, out, seen

    def test_nyc_quad_pack_omits_phantom_slots_and_secrets(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._stage(root)
            result, out, seen = self._build(root)
            self.assertEqual(result["iso_week"], "2026-W45")
            self.assertEqual(
                result["s3_uri"],
                "s3://nexvue-metrics/nexvue/nynycof1nexvue01/weekly/2026-W45/",
            )
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["pack_format"], 1)
            self.assertEqual(manifest["product_version"], "9.9.9")
            self.assertEqual(manifest["timezone"], "America/New_York")
            self.assertEqual(manifest["iso_week"], "2026-W45")
            self.assertEqual(manifest["window_end_utc"], "2026-11-09T04:00:00Z")
            self.assertEqual(manifest["window_end_local"], "2026-11-08T23:00:00-05:00")
            self.assertEqual(manifest["window_start_utc"], "2026-11-02T04:00:00Z")
            self.assertEqual(manifest["site_id"], "nyc-cof1")
            self.assertEqual(manifest["site_id_source"], "NEXVUE_SITE_ID")
            self.assertEqual(manifest["encode_slots"], list(range(8)))
            self.assertEqual(manifest["hostname"], "nynycof1nexvue01")

            services = json.loads((out / "services.json").read_text(encoding="utf-8"))
            units = services["units"]
            for idx in range(8):
                self.assertEqual(units[f"nexvue-encode@{idx}"]["active"], "active")
                self.assertEqual(units[f"nexvue-encode@{idx}"]["enabled"], "enabled")
            self.assertNotIn("nexvue-encode@8", units)
            self.assertNotIn("nexvue-encode@9", units)
            self.assertIn("mediamtx", units)
            self.assertNotIn("nexvue-encode@8", seen)
            self.assertNotIn("nexvue-encode@9", seen)

            joined = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in out.iterdir() if path.suffix != ".gz")
            with gzip.open(out / "host_samples.jsonl.gz", "rt", encoding="utf-8") as handle:
                sample_text = handle.read()
            blob = joined + "\n" + sample_text
            for secret in (
                "supersecretkeyvalue",
                "eyJhbGciOi-not-really",
                "hunter2",
                "203.0.113.50",
                "ada@example.com",
                "secret-agent",
                "u:p@",
            ):
                self.assertNotIn(secret, blob)
            self.assertIn("srt://***:***@", (out / "decklink-status.json").read_text(encoding="utf-8"))
            self.assertIn("1080i59.94", blob)
            self.assertNotIn("10.9.9.9", sample_text)
            rows = [json.loads(line) for line in sample_text.splitlines() if line]
            self.assertEqual(len(rows), 2)
            self.assertNotIn("note", rows[0])

            sessions = json.loads((out / "viewer_sessions.json").read_text(encoding="utf-8"))
            self.assertEqual(sessions["sessions"], 2)
            self.assertEqual(sessions["bytes_sent"], 1500)
            self.assertEqual(sessions["distinct_remotes"], 1)
            self.assertEqual(sessions["distinct_users"], 1)
            self.assertEqual(len(sessions["by_channel"]), 2)

            restarts = json.loads((out / "restarts.json").read_text(encoding="utf-8"))
            one = restarts["units"]["nexvue-encode@1"]
            self.assertEqual(one["started"], 1)
            self.assertEqual(one["stopped"], 1)
            self.assertEqual(one["failed"], 1)
            self.assertEqual(one["main_exited"], 1)
            self.assertEqual(one["scheduled_restart"], 1)
            self.assertIn("ignored channel env 8.env, 9.env", " ".join(manifest["notes"]))

            summary = json.loads((out / "host_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(sum(bucket["n"] for bucket in summary["buckets"]), 2)
            self.assertTrue((out / "totals.json").is_file())

    def test_short_card_is_not_reported_as_a_quad(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._stage(root)
            _result, out, seen = self._build(root, decklink=self._decklink(range(4)))
            services = json.loads((out / "services.json").read_text(encoding="utf-8"))
            self.assertEqual(services["encode_slots"], [0, 1, 2, 3])
            self.assertNotIn("nexvue-encode@4", services["units"])
            self.assertNotIn("nexvue-encode@4", seen)
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(any("DeckLink did not report" in note for note in manifest["notes"]))

    def test_uppercase_hostname_is_folded_into_the_key(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._stage(root)
            result, out, _seen = self._build(root, hostname="NYNYCOF1Nexvue01")
            self.assertEqual(result["s3_hostname"], "nynycof1nexvue01")
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["hostname"], "NYNYCOF1Nexvue01")

    def test_thin_keeps_hourly_totals(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = self._stage(root)
            conn = sqlite3.connect(db_path)
            base = int(START.timestamp()) + 10
            conn.executemany(
                "INSERT INTO host_samples VALUES (?,?,?,?,?,?)",
                [(base + i, 1, 1, 2, 0.1, "x") for i in range(30)],
            )
            conn.commit()
            conn.close()
            with patch.object(pack, "MAX_HOST_SAMPLE_ROWS", 5):
                _result, out, _seen = self._build(root)
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            samples = next(item for item in manifest["files"] if item["name"] == "host_samples.jsonl.gz")
            self.assertEqual(samples["rows"], 5)
            self.assertGreater(samples["raw_rows"], 5)
            summary = json.loads((out / "host_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(sum(bucket["n"] for bucket in summary["buckets"]), samples["raw_rows"])


class TestUpload(unittest.TestCase):
    def test_aws_argv_has_no_keys_and_manifest_is_last(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, timeout=30.0):
            del timeout
            calls.append(list(argv))
            return 0, "", ""

        with patch.object(pack.shutil, "which", return_value="/usr/bin/aws"):
            tool = pack.upload_pack(
                Path("/var/lib/nexvue/weekly/2026-W45"),
                ["manifest.json", "host_samples.jsonl.gz"],
                bucket="nexvue-metrics",
                prefix="nexvue",
                host="nynycof1nexvue01",
                week="2026-W45",
                region="us-east-1",
                runner=runner,
            )
        self.assertEqual(tool, "aws")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][3].endswith("host_samples.jsonl.gz"))
        self.assertTrue(calls[1][3].endswith("manifest.json"))
        flat = " ".join(" ".join(call) for call in calls)
        self.assertIn(
            "s3://nexvue-metrics/nexvue/nynycof1nexvue01/weekly/2026-W45/manifest.json",
            flat,
        )
        self.assertIn("--region us-east-1", flat)
        self.assertNotIn("AWS_ACCESS_KEY", flat)
        self.assertNotIn("secret", flat.lower())

    def test_boto3_when_aws_cli_is_absent(self) -> None:
        called: dict[str, str] = {}

        def fake_boto3(*args, **kwargs):
            called["used"] = "yes"
            del args, kwargs

        with patch.object(pack.shutil, "which", return_value=None):
            with patch.object(pack, "upload_with_boto3", fake_boto3):
                tool = pack.upload_pack(
                    Path("/tmp"),
                    ["manifest.json"],
                    bucket="b",
                    prefix="nexvue",
                    host="h",
                    week="2026-W45",
                    region="",
                )
        self.assertEqual(tool, "boto3")
        self.assertEqual(called["used"], "yes")

    def test_missing_aws_and_boto3(self) -> None:
        try:
            import boto3  # noqa: F401
        except ImportError:
            boto3 = None
        if boto3 is not None:
            self.skipTest("boto3 is installed")
        with self.assertRaises(pack.UploadError) as caught:
            pack.upload_with_boto3(
                Path("/tmp"),
                ["manifest.json"],
                bucket="b",
                prefix="nexvue",
                host="h",
                week="2026-W45",
                region="",
            )
        self.assertIn("boto3", str(caught.exception))


class TestCli(EnvGuard):
    def _bin(self, root: Path) -> Path:
        bindir = root / "bin"
        bindir.mkdir()
        (bindir / "systemctl").write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$STAT_LOG\"\n"
            "case \"$1\" in\n"
            "  is-active) echo active ;;\n"
            "  is-enabled) echo enabled ;;\n"
            "  *) echo unknown ;;\n"
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (bindir / "journalctl").write_text(
            "#!/bin/sh\n"
            "prev=\n"
            "unit=\n"
            "for arg in \"$@\"; do\n"
            "  if [ \"$prev\" = \"-u\" ]; then unit=$arg; fi\n"
            "  prev=$arg\n"
            "done\n"
            "printf '%s\\n' \"$unit\" >> \"$JOURNAL_LOG\"\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (bindir / "aws").write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$AWS_LOG\"\n"
            "if [ -n \"$AWS_FAIL\" ]; then echo boom >&2; exit 1; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        devices = [
            {
                "index": idx,
                "name": f"DeckLink Quad ({idx + 1})",
                "input_mode": "1080i59.94",
            }
            for idx in range(8)
        ]
        payload = json.dumps({"devices": devices, "note": "srt://user:hunter2@10.1.1.1/live"})
        (bindir / "decklink-status").write_text(
            "#!/bin/sh\ncat <<'END'\n" + payload + "\nEND\n",
            encoding="utf-8",
        )
        for path in bindir.iterdir():
            path.chmod(0o755)
        return bindir

    def _env(self, root: Path, bindir: Path, *, bucket: bool) -> dict[str, str]:
        etc = root / "etc"
        (etc / "channels").mkdir(parents=True)
        lines = [
            "MAX_CHANNELS=8",
            "NEXVUE_METRICS_TZ=America/New_York",
            "NEXVUE_PORTAL_STATION_API_KEY=supersecretkeyvalue",
        ]
        if bucket:
            lines.append("NEXVUE_S3_BUCKET=nexvue-metrics")
            lines.append("NEXVUE_S3_REGION=us-east-1")
        (etc / "nexvue.env").write_text("\n".join(lines) + "\n", encoding="utf-8")
        for idx in range(10):
            (etc / "channels" / f"{idx}.env").write_text("X=1\n", encoding="utf-8")
        data = root / "data"
        data.mkdir()
        db = data / "metrics.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE host_samples (ts INTEGER PRIMARY KEY, cpu_pct REAL, load1 REAL)"
        )
        conn.execute(
            "INSERT INTO host_samples VALUES (?,?,?)",
            (int(START.timestamp()) + 30, 5, 0.2),
        )
        conn.commit()
        conn.close()
        env = os.environ.copy()
        for key in list(env):
            if key.startswith("NEXVUE_") or key.startswith("AWS_"):
                env.pop(key, None)
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
        env["NEXVUE_ETC"] = str(etc)
        env["NEXVUE_DATA"] = str(data)
        env["NEXVUE_METRICS_DB"] = str(db)
        env["NEXVUE_DECKLINK_STATUS"] = str(bindir / "decklink-status")
        env["STAT_LOG"] = str(root / "stat.log")
        env["JOURNAL_LOG"] = str(root / "journal.log")
        env["AWS_LOG"] = str(root / "aws.log")
        return env

    def _run(self, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "nexvue-weekly-pack.py"), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_dry_run_writes_pack_without_s3(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = self._bin(root)
            env = self._env(root, bindir, bucket=True)
            out = root / "out"
            proc = self._run(
                ["--dry-run", "--out-dir", str(out), "--now", "2026-11-09T04:00:00Z"],
                env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), str(out.resolve()))
            self.assertIn("would upload to s3://nexvue-metrics/", proc.stderr)
            self.assertFalse((root / "aws.log").exists())
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["iso_week"], "2026-W45")
            self.assertEqual(manifest["encode_slots"], list(range(8)))
            services = json.loads((out / "services.json").read_text(encoding="utf-8"))
            self.assertNotIn("nexvue-encode@8", services["units"])
            self.assertNotIn("nexvue-encode@9", services["units"])
            journal = (root / "journal.log").read_text(encoding="utf-8")
            self.assertIn("nexvue-encode@0", journal)
            self.assertNotIn("nexvue-encode@8", journal)
            self.assertNotIn("supersecretkeyvalue", proc.stdout + proc.stderr)
            blob = (out / "manifest.json").read_text(encoding="utf-8")
            self.assertNotIn("supersecretkeyvalue", blob)
            self.assertNotIn("hunter2", (out / "decklink-status.json").read_text(encoding="utf-8"))

    def test_upload_uses_aws_cli(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = self._bin(root)
            env = self._env(root, bindir, bucket=True)
            out = root / "out"
            proc = self._run(
                ["--out-dir", str(out), "--now", "2026-11-09T04:00:00Z"],
                env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(proc.stdout.strip().endswith("/weekly/2026-W45/"))
            log = (root / "aws.log").read_text(encoding="utf-8").splitlines()
            self.assertGreaterEqual(len(log), 2)
            self.assertIn("host_samples.jsonl.gz", log[0])
            self.assertIn("manifest.json", log[-1])
            self.assertIn("--region us-east-1", log[-1])
            self.assertNotIn("AWS_ACCESS_KEY", "\n".join(log))

    def test_upload_refuses_without_a_bucket(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = self._bin(root)
            env = self._env(root, bindir, bucket=False)
            proc = self._run(["--out-dir", str(root / "out")], env)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("NEXVUE_S3_BUCKET", proc.stderr)
            self.assertFalse((root / "aws.log").exists())
            self.assertFalse((root / "out" / "manifest.json").exists())

    def test_upload_failure_keeps_the_local_pack(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = self._bin(root)
            env = self._env(root, bindir, bucket=True)
            env["AWS_FAIL"] = "1"
            out = root / "out"
            proc = self._run(["--out-dir", str(out), "--now", "2026-11-09T04:00:00Z"], env)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertTrue((out / "manifest.json").is_file())
            self.assertIn("local pack kept", proc.stderr)


class TestInstallContract(unittest.TestCase):
    def test_units_and_setup_are_opt_in(self) -> None:
        setup = (ROOT / "setup.sh").read_text(encoding="utf-8")
        timer = (ROOT / "nexvue-weekly-pack.timer").read_text(encoding="utf-8")
        service = (ROOT / "nexvue-weekly-pack.service").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        example = (ROOT / "nexvue-example.env").read_text(encoding="utf-8")
        script = (ROOT / "nexvue-weekly-pack.py").read_text(encoding="utf-8")

        self.assertIn("OnCalendar=Sun *-*-* 23:00:00", timer)
        self.assertIn("Timezone=America/New_York", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Type=oneshot", service)
        self.assertIn("/usr/local/bin/nexvue-weekly-pack.py", service)
        self.assertNotIn("AWS_", service)
        self.assertNotIn("AKIA", script)
        self.assertNotIn("AWS_ACCESS_KEY_ID", script)
        self.assertNotIn("aws_secret_access_key", script.lower())

        self.assertEqual(setup.count("systemctl enable --now nexvue-weekly-pack.timer"), 1)
        flag = setup.index("nexvue_env_get NEXVUE_WEEKLY_PACK 0")
        enable = setup.index("systemctl enable --now nexvue-weekly-pack.timer")
        self.assertLess(flag, enable)
        self.assertIn("nexvue-weekly-pack.py", setup)
        self.assertIn("nexvue-weekly-pack.timer", setup)

        self.assertIn("s3:PutObject", readme)
        self.assertIn("NEXVUE_S3_BUCKET", readme)
        self.assertIn("--no-upload", readme)
        self.assertIn("nynycof1nexvue01", readme)
        self.assertIn("#NEXVUE_WEEKLY_PACK=1", example)
        self.assertNotIn("\nNEXVUE_WEEKLY_PACK=1\n", example)


if __name__ == "__main__":
    unittest.main()
