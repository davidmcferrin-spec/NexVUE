#!/usr/bin/env python3
"""
nexvue-support-bundle — build a redacted zip for bake-in / remote debug.

Runs as root via nexvue-ops-support-bundle.sh (sudoers allowlist).
Stdout: absolute path to the zip (single line). Progress on stderr.

stdlib only — no pip.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ALLOWED_HOURS = frozenset({1, 6, 12, 24, 48, 72})
UNITS = (
    "mediamtx",
    "nexvue-status",
    "nexvue-metrics",
    *[f"nexvue-encode@{i}" for i in range(10)],
)

MAX_JOURNAL_BYTES_PER_UNIT = 8 * 1024 * 1024  # 8 MiB
MAX_ZIP_BYTES = 100 * 1024 * 1024  # 100 MiB warn threshold
BUNDLE_RETENTION_HOURS = 24
MAX_STATE_FILE_BYTES = 2 * 1024 * 1024

_RE_URL_CREDS = re.compile(
    r"(?i)\b((?:rtsp|rtsps|srt|http|https|ftp)://)([^/\s:@]+):([^/\s@]+)@"
)
# Match bare keys and ENV-style suffixes (SRT_PASSPHRASE=, FOO_TOKEN=).
_RE_KEY_VALUE_SECRET = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:password|passwd|passphrase|secret|api[_-]?key|token|authorization))"
    r"(\s*[=:]\s*)([^\s,;\"']+)"
)
_SECRET_KEYS = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "srt_passphrase",
        "secret",
        "token",
        "authorization",
        "auth",
        "api_key",
        "key_hash",
    }
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def data_dir() -> Path:
    return Path(os.environ.get("NEXVUE_DATA", "/var/lib/nexvue"))


def etc_dir() -> Path:
    return Path(os.environ.get("NEXVUE_ETC", "/etc/nexvue"))


def run_dir() -> Path:
    return Path(os.environ.get("NEXVUE_RUN_DIR", "/run/nexvue"))


def metrics_db_path() -> Path:
    env = os.environ.get("NEXVUE_METRICS_DB")
    if env:
        return Path(env)
    return data_dir() / "metrics.db"


def redact_text(text: str) -> str:
    if not text:
        return text
    out = _RE_URL_CREDS.sub(r"\1***:***@", text)
    out = _RE_KEY_VALUE_SECRET.sub(r"\1\2***", out)
    return out


def redact_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in _SECRET_KEYS or lk.endswith("_password") or lk.endswith("_passphrase"):
                out[k] = "***" if v not in (None, "") else v
            elif isinstance(v, str) and (
                "://" in v or lk.endswith("_url") or lk.endswith("_uri") or "key" in lk
            ):
                out[k] = redact_text(v)
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def run_cmd(
    argv: list[str],
    *,
    timeout: float = 30.0,
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return int(proc.returncode), proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", f"not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s: {' '.join(argv)}"


def write_text(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact_text(body), encoding="utf-8", errors="replace")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(redact_obj(data), indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def which_version(cmd: str, args: list[str]) -> str:
    code, out, err = run_cmd([cmd, *args], timeout=8.0)
    blob = (out or err or "").strip()
    if code == 127:
        return f"{cmd}: not installed\n"
    return (blob or f"{cmd}: exit {code}") + "\n"


def collect_host(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for src, name in (
        (Path("/usr/local/share/nexvue/VERSION"), "nexvue-VERSION.txt"),
        (Path("/var/lib/nexvue/version.json"), "nexvue-version.json"),
        (Path("/etc/nexvue/repo.path"), "nexvue-repo.path.txt"),
    ):
        if src.is_file():
            try:
                write_text(dest / name, src.read_text(encoding="utf-8", errors="replace"))
            except OSError as exc:
                write_text(dest / f"{name}.error.txt", f"{exc}\n")
    write_text(dest / "uname.txt", run_cmd(["uname", "-a"])[1])
    write_text(dest / "uptime.txt", run_cmd(["uptime"])[1])
    write_text(dest / "free.txt", run_cmd(["free", "-h"])[1])
    write_text(dest / "df.txt", run_cmd(["df", "-h"])[1])
    write_text(dest / "ip.txt", run_cmd(["ip", "-br", "a"])[1])
    write_text(
        dest / "lsblk.txt",
        run_cmd(["lsblk", "-o", "NAME,SIZE,TYPE,MOUNTPOINT"])[1],
    )
    write_text(dest / "timedatectl.txt", run_cmd(["timedatectl"])[1])
    write_text(dest / "lscpu.txt", run_cmd(["lscpu"])[1])
    code, out, err = run_cmd(["dmesg", "-T", "--level=err,warn"], timeout=10.0)
    if code == 0 and out:
        lines = out.splitlines()
        write_text(dest / "dmesg-warn-err-tail.txt", "\n".join(lines[-200:]) + "\n")
    elif err:
        write_text(dest / "dmesg.error.txt", err)
    # DeckLink snapshot if helper is installed
    dl = shutil.which("decklink-status") or "/usr/local/bin/decklink-status"
    if Path(dl).is_file():
        c, o, e = run_cmd([dl], timeout=20.0)
        write_text(dest / "decklink-status.txt", o or e or f"exit {c}\n")


def collect_versions(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    write_text(dest / "python3.txt", which_version("python3", ["--version"]))
    write_text(dest / "php.txt", which_version("php", ["-v"]))
    write_text(dest / "apache2ctl.txt", which_version("apache2ctl", ["-v"]))
    write_text(dest / "gst-launch.txt", which_version("gst-launch-1.0", ["--version"]))
    write_text(dest / "gst-inspect-decklink.txt", which_version("gst-inspect-1.0", ["decklinkvideosrc"]))
    write_text(dest / "gst-inspect-vah264enc.txt", which_version("gst-inspect-1.0", ["vah264enc"]))
    mtx = shutil.which("mediamtx") or "/usr/local/bin/mediamtx"
    if Path(mtx).is_file():
        write_text(dest / "mediamtx.txt", which_version(mtx, ["--version"]))
    else:
        write_text(dest / "mediamtx.txt", "mediamtx: not found\n")
    code, out, _ = run_cmd(
        ["dpkg-query", "-W", "-f=${Package}\\t${Version}\\n"],
        timeout=15.0,
    )
    if code == 0 and out:
        keep = []
        for line in out.splitlines():
            low = line.lower()
            if any(
                x in low
                for x in (
                    "gstreamer",
                    "intel-media",
                    "va-driver",
                    "apache2",
                    "php",
                    "libsrt",
                    "desktopvideo",
                    "blackmagic",
                )
            ):
                keep.append(line)
        write_text(dest / "dpkg-relevant.txt", "\n".join(keep) + ("\n" if keep else ""))
    ver_bits: dict[str, Any] = {
        "etc": str(etc_dir()),
        "data": str(data_dir()),
        "run": str(run_dir()),
    }
    for helper in (
        "/usr/local/bin/nexvue-encode.sh",
        "/usr/local/bin/nexvue-support-bundle.py",
        "/usr/local/bin/decklink-status",
        "/usr/local/bin/decklink-audio-probe",
    ):
        p = Path(helper)
        ver_bits[p.name] = "present" if p.is_file() else "missing"
    write_json(dest / "nexvue.json", ver_bits)


def collect_systemd(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for unit in UNITS:
        _c1, a, _ = run_cmd(["systemctl", "is-active", unit], timeout=5.0)
        _c2, e, _ = run_cmd(["systemctl", "is-enabled", unit], timeout=5.0)
        lines.append(f"{unit}\t{(a or 'unknown').strip()}\t{(e or 'unknown').strip()}")
    write_text(dest / "is-active-enabled.txt", "\n".join(lines) + "\n")

    chunks: list[str] = []
    for unit in UNITS:
        c, out, err = run_cmd(
            ["systemctl", "status", unit, "--no-pager", "-l"],
            timeout=10.0,
        )
        chunks.append(f"===== {unit} (exit {c}) =====\n{out or err}\n")
    write_text(dest / "status.txt", "".join(chunks))

    c, out, err = run_cmd(
        ["systemctl", "list-units", "nexvue*", "mediamtx*", "--all", "--no-pager"],
        timeout=10.0,
    )
    write_text(dest / "list-units.txt", out or err)

    unit_files = dest / "unit-files"
    unit_files.mkdir(parents=True, exist_ok=True)
    for unit in UNITS:
        c, out, err = run_cmd(["systemctl", "cat", unit], timeout=8.0)
        safe = unit.replace("@", "_at_")
        write_text(unit_files / f"{safe}.service.txt", out or err or "(empty)\n")


def collect_journals(dest: Path, hours: int) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    since = f"{hours} hours ago"
    for unit in UNITS:
        log(f"journal {unit} --since '{since}'")
        c, out, err = run_cmd(
            [
                "journalctl",
                "-u",
                unit,
                "--since",
                since,
                "--no-pager",
                "-o",
                "short-iso",
            ],
            timeout=90.0,
        )
        body = out if c == 0 else (err or out or f"journalctl exit {c}\n")
        raw = body.encode("utf-8", errors="replace")
        if len(raw) > MAX_JOURNAL_BYTES_PER_UNIT:
            raw = raw[-MAX_JOURNAL_BYTES_PER_UNIT:]
            nl = raw.find(b"\n")
            if nl >= 0:
                raw = raw[nl + 1 :]
            notes.append(
                f"{unit}: truncated to last {MAX_JOURNAL_BYTES_PER_UNIT} bytes"
            )
            body = (
                f"[nexvue-support-bundle: truncated to last "
                f"{MAX_JOURNAL_BYTES_PER_UNIT} bytes]\n"
                + raw.decode("utf-8", errors="replace")
            )
        safe = unit.replace("@", "_at_")
        write_text(dest / f"{safe}.log", body)
        if c != 0:
            notes.append(f"{unit}: journalctl exit {c}")
    return notes


def collect_config(dest: Path, hours: int) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    etc = etc_dir()

    for name in ("nexvue.env", "mediamtx.yml"):
        src = etc / name
        if src.is_file():
            try:
                write_text(dest / name, src.read_text(encoding="utf-8", errors="replace"))
            except OSError as exc:
                notes.append(f"{name}: {exc}")
        else:
            notes.append(f"missing {src}")

    ch_dir = etc / "channels"
    ch_out = dest / "channels"
    ch_out.mkdir(parents=True, exist_ok=True)
    if ch_dir.is_dir():
        for src in sorted(ch_dir.glob("*.env")):
            try:
                write_text(
                    ch_out / src.name,
                    src.read_text(encoding="utf-8", errors="replace"),
                )
            except OSError as exc:
                notes.append(f"channel {src.name}: {exc}")
    else:
        notes.append(f"missing {ch_dir}")

    # Apache / TLS paths (redacted on write)
    for src, name in (
        (Path("/etc/apache2/sites-available/nexvue.conf"), "apache-nexvue.conf"),
        (Path("/etc/apache2/sites-enabled/nexvue.conf"), "apache-nexvue-enabled.conf"),
        (Path("/etc/nexvue/tls/fullchain.pem"), "tls-fullchain.pem.exists.txt"),
    ):
        if src.is_file():
            if name.endswith(".exists.txt"):
                write_text(dest / name, f"present: {src} size={src.stat().st_size}\n")
            else:
                try:
                    write_text(
                        dest / name,
                        src.read_text(encoding="utf-8", errors="replace"),
                    )
                except OSError as exc:
                    notes.append(f"{name}: {exc}")

    # Metrics DB: schema + row counts + recent host samples (not full history dump)
    db = metrics_db_path()
    if db.is_file():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        except sqlite3.Error as exc:
            write_text(dest / "metrics.db.error.txt", f"{exc}\n")
            notes.append(str(exc))
            return notes
        try:
            schema: list[str] = []
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
            ):
                schema.append(row[0] + ";")
            write_text(dest / "metrics-schema.sql", "\n\n".join(schema) + "\n")
            counts: dict[str, Any] = {"path": str(db)}
            for (tbl,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ):
                try:
                    n = conn.execute(f"SELECT COUNT(*) FROM [{tbl}]").fetchone()[0]
                    counts[tbl] = n
                except sqlite3.Error as exc:
                    counts[tbl] = f"error: {exc}"
            write_json(dest / "metrics-table-counts.json", counts)
            since = int(time.time() - float(hours) * 3600.0)
            try:
                cur = conn.execute(
                    """
                    SELECT ts, cpu_pct, mem_used_bytes, mem_total_bytes, load1,
                           gpu_video_pct, gpu_render_pct, cpu_temp_c, gpu_temp_c
                    FROM host_samples
                    WHERE ts >= ?
                    ORDER BY ts ASC
                    LIMIT 20000
                    """,
                    (since,),
                )
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                write_json(dest / "metrics-host-samples.json", rows)
            except sqlite3.Error as exc:
                notes.append(f"host_samples: {exc}")
        finally:
            conn.close()
    else:
        notes.append(f"metrics DB missing: {db}")

    return notes


def collect_state(dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    rd = run_dir()
    if not rd.is_dir():
        write_text(dest / "run-dir.missing.txt", f"missing {rd}\n")
        notes.append(f"run dir missing: {rd}")
        return notes

    for root, dirs, files in os.walk(rd):
        dirs[:] = [d for d in dirs if d not in (".",)]
        rel_root = Path(root).relative_to(rd)
        for name in files:
            src = Path(root) / name
            if src.is_symlink() or not src.is_file():
                continue
            try:
                st = src.stat()
            except OSError:
                continue
            if st.st_size > MAX_STATE_FILE_BYTES:
                notes.append(f"skipped large state file: {src} ({st.st_size} bytes)")
                continue
            if name.endswith(".sock") or src.suffix == ".sock":
                continue
            # Caption raw FIFOs / ccraw are noisy binary — skip
            if name.endswith(".ccraw") or name.endswith(".fifo"):
                notes.append(f"skipped fifo/raw: {src}")
                continue
            rel = rel_root / name if str(rel_root) != "." else Path(name)
            dst = dest / "run-nexvue" / rel
            try:
                raw = src.read_bytes()
                try:
                    text = raw.decode("utf-8")
                    if name.endswith(".json") or text.lstrip().startswith(("{", "[")):
                        try:
                            write_json(dst, json.loads(text))
                        except json.JSONDecodeError:
                            write_text(dst, text)
                    else:
                        write_text(dst, text)
                except UnicodeDecodeError:
                    notes.append(f"skipped binary state: {src}")
            except OSError as exc:
                notes.append(f"{src}: {exc}")

    # Branding presence only (not the image bytes)
    branding = data_dir() / "branding"
    if branding.is_dir():
        listing = []
        for p in sorted(branding.iterdir()):
            try:
                st = p.stat()
                listing.append(
                    {
                        "name": p.name,
                        "size": st.st_size if p.is_file() else None,
                        "is_dir": p.is_dir(),
                    }
                )
            except OSError:
                continue
        write_json(dest / "branding-listing.json", listing)

    return notes


def prune_old_bundles(support_dir: Path) -> None:
    if not support_dir.is_dir():
        return
    cutoff = time.time() - BUNDLE_RETENTION_HOURS * 3600
    for p in support_dir.glob("nexvue-support-*.zip"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                log(f"pruned old bundle {p.name}")
        except OSError:
            pass


def zip_tree(src_dir: Path, zip_path: Path) -> int:
    count = 0
    with zipfile.ZipFile(
        zip_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as zf:
        for root, _dirs, files in os.walk(src_dir):
            for name in files:
                full = Path(root) / name
                arc = full.relative_to(src_dir).as_posix()
                zf.write(full, arcname=arc)
                count += 1
    return count


def build_bundle(
    *,
    hours: int,
    requestor_ip: str = "",
    out_dir: Optional[Path] = None,
) -> Path:
    if hours not in ALLOWED_HOURS:
        raise SystemExit(f"hours must be one of {sorted(ALLOWED_HOURS)}")

    support = out_dir or (data_dir() / "support")
    support.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(support, 0o750)
    except OSError:
        pass
    try:
        import grp

        gid = grp.getgrnam("www-data").gr_gid
        os.chown(support, 0, gid)
    except (OSError, KeyError, ImportError):
        pass
    prune_old_bundles(support)

    host = socket.gethostname() or "host"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base_name = f"nexvue-support-{stamp}-{host}-{hours}h"
    work = support / f".work-{base_name}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    redactions = [
        "URL userinfo (user:pass@) → ***:***@",
        "password/passphrase/token field values → ***",
        "SRT/RTSP URI credentials redacted in channel .env and mediamtx.yml",
        "TLS private keys not included (fullchain presence marker only)",
        "Caption FIFO raw streams and branding image bytes omitted",
        f"journals truncated per unit at {MAX_JOURNAL_BYTES_PER_UNIT} bytes if larger",
        "metrics.db: schema + counts + host_samples window only (not full DB)",
    ]
    notes: list[str] = []

    try:
        log("collecting host…")
        collect_host(work / "host")
        log("collecting versions…")
        collect_versions(work / "versions")
        log("collecting systemd…")
        collect_systemd(work / "systemd")
        log("collecting journals…")
        notes.extend(collect_journals(work / "journal", hours))
        log("collecting config…")
        notes.extend(collect_config(work / "config", hours))
        log("collecting runtime state…")
        notes.extend(collect_state(work / "state"))

        write_text(
            work / "REDACTIONS.txt",
            "\n".join(f"- {x}" for x in redactions) + "\n",
        )
        if notes:
            write_text(work / "NOTES.txt", "\n".join(f"- {x}" for x in notes) + "\n")

        manifest = {
            "product": "NexVUE",
            "bundle_format": 1,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "hostname": host,
            "hours": hours,
            "requestor_ip": requestor_ip or None,
            "etc_dir": str(etc_dir()),
            "data_dir": str(data_dir()),
            "run_dir": str(run_dir()),
            "metrics_db": str(metrics_db_path()),
            "units": list(UNITS),
            "notes": notes,
            "redactions": redactions,
        }
        write_json(work / "MANIFEST.json", manifest)

        zip_path = support / f"{base_name}.zip"
        log(f"zipping → {zip_path}")
        nfiles = zip_tree(work, zip_path)
        try:
            os.chmod(zip_path, 0o640)
        except OSError:
            pass
        try:
            shutil.chown(zip_path, user="root", group="www-data")
        except (OSError, LookupError, AttributeError, ImportError, NotImplementedError):
            try:
                import grp

                gid = grp.getgrnam("www-data").gr_gid
                os.chown(zip_path, 0, gid)
            except (OSError, KeyError, ImportError):
                pass

        size = zip_path.stat().st_size
        log(f"done: {nfiles} files, {size} bytes")
        if size > MAX_ZIP_BYTES:
            log(f"WARNING: zip exceeds soft limit {MAX_ZIP_BYTES} bytes")
        return zip_path
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Build a NexVUE support bundle zip")
    ap.add_argument(
        "--hours",
        type=int,
        required=True,
        help=f"Journal/metrics window; one of {sorted(ALLOWED_HOURS)}",
    )
    ap.add_argument(
        "--requestor-ip",
        default="",
        help="Client IP recorded in MANIFEST (from Services UI)",
    )
    ap.add_argument(
        "--out-dir",
        default="",
        help="Override support output directory",
    )
    args = ap.parse_args(argv)
    out = Path(args.out_dir) if args.out_dir else None
    path = build_bundle(
        hours=int(args.hours),
        requestor_ip=str(args.requestor_ip or "").strip(),
        out_dir=out,
    )
    print(str(path.resolve()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"support bundle failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
