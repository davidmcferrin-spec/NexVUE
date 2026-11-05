#!/usr/bin/env python3
"""
Build a weekly NexVUE metrics pack and optionally upload it to S3.

Not the support-bundle zip. The pack is a manifest, a week of host
samples, systemd state, a DeckLink snapshot, and restart counts.

Encode slots follow the card that is actually configured. A DeckLink
Quad has inputs 0-7. The support bundle still probes encode@0..9,
because those template instances can exist; this pack does not report
encode@8 or encode@9 as disabled inputs.

stdlib only. Upload uses the aws CLI when it is on PATH, otherwise
boto3 if that module is already importable. Credentials are the
default AWS chain (instance role). This file never reads or writes
access keys.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Quad 2 is the largest card NexVUE configures (Settings slots 1-8).
# Indices 8 and 9 show up in the support-bundle unit sweep only.
SLOT_CEILING = 8
PACK_FORMAT = 1
DEFAULT_DAYS = 7
MAX_DAYS = 30
# ~2 samples/minute across 7 days. Denser history stays in the hourly summary.
MAX_HOST_SAMPLE_ROWS = 20160
LOCAL_KEEP_SECONDS = 28 * 24 * 3600

CORE_UNITS = ("mediamtx", "nexvue-status", "nexvue-metrics")

# Columns the metrics collector actually writes. Extra DB columns are ignored.
HOST_SAMPLE_FIELDS = (
    "ts",
    "cpu_pct",
    "mem_used_bytes",
    "mem_total_bytes",
    "load1",
    "gpu_video_pct",
    "gpu_render_pct",
    "gpu_video_enhance_pct",
    "gpu_freq_mhz",
    "cpu_temp_c",
    "gpu_temp_c",
)
TOTAL_FIELDS = (
    "ts",
    "active_streams",
    "total_readers",
    "total_bandwidth_bps",
)

# Station env keys this job is willing to read. Everything else in
# nexvue.env (API keys, publish JWT, SRT passphrases) stays on the box.
ENV_KEYS = frozenset(
    {
        "NEXVUE_S3_BUCKET",
        "NEXVUE_S3_PREFIX",
        "NEXVUE_S3_REGION",
        "NEXVUE_SITE_ID",
        "NEXVUE_PORTAL_STATION_ID",
        "NEXVUE_METRICS_TZ",
        "NEXVUE_WEEKLY_PACK_TZ",
        "MAX_CHANNELS",
        "MAX_DEVICES",
    }
)

# systemd's own start/stop lines. Counts only — the journal text is not kept.
JOURNAL_GREP = (
    r"^(Started |Stopped )|Failed with result|Main process exited|Scheduled restart job"
)

_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION = re.compile(r"^[a-z0-9-]{1,32}$")
_PREFIX_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$")
_WEEK = re.compile(r"^\d{4}-W\d{2}$")

# Same substitutions as nexvue-support-bundle.py.
_RE_URL_CREDS = re.compile(
    r"(?i)\b((?:rtsp|rtsps|srt|http|https|ftp)://)([^/\s:@]+):([^/\s@]+)@"
)
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

Runner = Callable[..., tuple[int, str, str]]


class UploadError(RuntimeError):
    pass


class _Probe:
    """Sentinel: ask decklink-status. Tests pass a payload instead."""


PROBE = _Probe()


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def die(msg: str, code: int = 2) -> None:
    log(msg)
    raise SystemExit(code)


def data_dir() -> Path:
    return Path(os.environ.get("NEXVUE_DATA", "/var/lib/nexvue"))


def etc_dir() -> Path:
    return Path(os.environ.get("NEXVUE_ETC", "/etc/nexvue"))


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
        for key, value in obj.items():
            low = str(key).lower()
            if low in _SECRET_KEYS or low.endswith("_password") or low.endswith("_passphrase"):
                out[key] = "***" if value not in (None, "") else value
            elif isinstance(value, str) and (
                "://" in value or low.endswith("_url") or low.endswith("_uri") or "key" in low
            ):
                out[key] = redact_text(value)
            else:
                out[key] = redact_obj(value)
        return out
    if isinstance(obj, list):
        return [redact_obj(item) for item in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def json_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    return value


def run_cmd(argv: list[str], *, timeout: float = 30.0) -> tuple[int, str, str]:
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
        return 124, "", f"timeout after {timeout}s: {argv[0]}"


def parse_env_value(raw: str) -> str:
    val = raw.strip()
    if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
        return val[1:-1]
    if "#" in val:
        val = val.split("#", 1)[0].strip()
    return val


def read_station_env(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return found
    for line in text.splitlines():
        match = _ASSIGN.match(line)
        if not match:
            continue
        key, raw = match.group(1), match.group(2)
        if key not in ENV_KEYS:
            continue
        found[key] = parse_env_value(raw)
    return found


def station_settings() -> dict[str, str]:
    """File values, then a non-empty process environment (tests and one-shots)."""
    found = read_station_env(etc_dir() / "nexvue.env")
    for key in ENV_KEYS:
        override = os.environ.get(key, "").strip()
        if override:
            found[key] = override
    return found


def slot_count(raw: str | None) -> int | None:
    if raw is None:
        return None
    text = raw.strip()
    if not re.fullmatch(r"[1-8]", text):
        return None
    return int(text)


def channel_env_indices(channels_dir: Path) -> tuple[list[int], list[int]]:
    """In-range channel env stems, and stems at or above the Quad cap."""
    kept: list[int] = []
    ignored: list[int] = []
    if not channels_dir.is_dir():
        return kept, ignored
    for path in sorted(channels_dir.glob("*.env")):
        if not re.fullmatch(r"\d+", path.stem):
            continue
        idx = int(path.stem)
        if idx < SLOT_CEILING:
            kept.append(idx)
        else:
            ignored.append(idx)
    return sorted(set(kept)), sorted(set(ignored))


def decklink_indices(payload: Any) -> list[int] | None:
    """Device indexes from a helper snapshot.

    None when the helper is missing, errored, or listed no devices.
    An empty list must not wipe a configured Quad down to zero slots.
    """
    if not isinstance(payload, dict):
        return None
    devices = payload.get("devices")
    if not isinstance(devices, list) or not devices:
        return None
    found: list[int] = []
    for device in devices:
        if not isinstance(device, dict) or "index" not in device:
            continue
        try:
            idx = int(device["index"])
        except (TypeError, ValueError):
            continue
        if 0 <= idx < SLOT_CEILING:
            found.append(idx)
    if not found:
        return None
    return sorted(set(found))


def resolve_encode_slots(
    *,
    max_channels: int | None,
    max_devices: int | None,
    channel_indices: Sequence[int],
    decklink: Sequence[int] | None,
) -> tuple[list[int], str, list[int]]:
    """Configured encode indexes, where that answer came from, and drops.

    MAX_CHANNELS (else MAX_DEVICES) is the Settings slot count: those
    units are the ones hardware setup enables. Channel env files are
    the fallback when that key is absent. A DeckLink device list, when
    the helper actually returned devices, drops indexes the card does
    not have so a 4-input Duo is not described as an 8-input Quad.
    """
    count = max_channels if max_channels is not None else max_devices
    if count is not None:
        configured = list(range(count))
        source = "max_channels" if max_channels is not None else "max_devices"
    elif channel_indices:
        configured = sorted({i for i in channel_indices if 0 <= i < SLOT_CEILING})
        source = "channel_env"
    elif decklink:
        return list(decklink), "decklink", []
    else:
        return [], "none", []

    if not decklink:
        return configured, source, []
    physical = set(decklink)
    kept = [i for i in configured if i in physical]
    dropped = [i for i in configured if i not in physical]
    return kept, source + "+decklink", dropped


def encode_units(slots: Sequence[int]) -> list[str]:
    return [*CORE_UNITS, *[f"nexvue-encode@{idx}" for idx in slots]]


def clean_hostname(host: str) -> str:
    label = (host or "").strip().lower()
    if not _HOST.fullmatch(label) or ".." in label:
        die(f"hostname is not safe as an S3 key: {host!r}")
    return label


def clean_bucket(raw: str) -> str:
    bucket = raw.strip()
    if not _BUCKET.fullmatch(bucket) or ".." in bucket:
        die(f"NEXVUE_S3_BUCKET is not a bucket name: {raw!r}")
    return bucket


def clean_region(raw: str) -> str:
    region = raw.strip()
    if not region:
        return ""
    if not _REGION.fullmatch(region):
        die(f"NEXVUE_S3_REGION is not a region name: {raw!r}")
    return region


def clean_prefix(raw: str | None) -> str:
    prefix = (raw or "").strip().strip("/")
    if not prefix:
        prefix = "nexvue"
    parts = prefix.split("/")
    for part in parts:
        if part in ("", ".", "..") or not _PREFIX_PART.fullmatch(part):
            die(f"NEXVUE_S3_PREFIX is not a safe S3 prefix: {raw!r}")
    return "/".join(parts)


def iso_week_key(moment: datetime) -> str:
    year, week, _day = moment.isocalendar()
    return f"{year}-W{week:02d}"


def s3_prefix_uri(bucket: str, prefix: str, host: str, week: str) -> str:
    return f"s3://{bucket}/{prefix}/{host}/weekly/{week}/"


def s3_object_uri(bucket: str, prefix: str, host: str, week: str, name: str) -> str:
    return f"{s3_prefix_uri(bucket, prefix, host, week)}{name}"


def product_version(data: Path) -> tuple[str, str | None]:
    version = "unknown"
    override = os.environ.get("NEXVUE_VERSION_FILE", "").strip()
    candidates = [Path(override)] if override else []
    candidates.append(Path("/usr/local/share/nexvue/VERSION"))
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        line = text.strip().splitlines()
        if line and line[0].strip():
            version = line[0].strip()
            break
    git_sha = None
    for path in (data / "version.json", Path("/var/lib/nexvue/version.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            sha = payload.get("git_sha")
            if isinstance(sha, str) and sha.strip():
                git_sha = sha.strip()
            if version == "unknown":
                raw = payload.get("version")
                if isinstance(raw, str) and raw.strip():
                    version = raw.strip()
            break
    return version, git_sha


def resolve_timezone(settings: dict[str, str], runner: Runner) -> tuple[str, Any]:
    name = ""
    for key in ("NEXVUE_WEEKLY_PACK_TZ", "NEXVUE_METRICS_TZ"):
        raw = settings.get(key, "").strip()
        if raw:
            name = raw
            break
    if not name:
        code, out, _err = runner(
            ["timedatectl", "show", "-p", "Timezone", "--value"],
            timeout=5.0,
        )
        if code == 0 and out.strip():
            name = out.strip()
    if not name:
        name = "America/New_York"
    try:
        return name, ZoneInfo(name)
    except ZoneInfoNotFoundError:
        try:
            return "America/New_York", ZoneInfo("America/New_York")
        except ZoneInfoNotFoundError:
            return "UTC", timezone.utc


def site_identity(settings: dict[str, str]) -> tuple[str | None, str | None]:
    for key in ("NEXVUE_SITE_ID", "NEXVUE_PORTAL_STATION_ID"):
        value = settings.get(key, "").strip()
        if value:
            return value, key
    return None, None


def fmt_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_local(moment: datetime, tz: Any) -> str:
    return moment.astimezone(tz).isoformat(timespec="seconds")


def systemctl_value(runner: Runner, verb: str, unit: str) -> str:
    _code, out, err = runner(["systemctl", verb, unit], timeout=5.0)
    text = (out or "").strip()
    if text:
        return text.splitlines()[0].strip()
    err_text = (err or "").strip()
    if err_text:
        return "unknown"
    return "unknown"


def collect_services(units: Sequence[str], runner: Runner) -> dict[str, Any]:
    states: dict[str, Any] = {}
    for unit in units:
        states[unit] = {
            "active": systemctl_value(runner, "is-active", unit),
            "enabled": systemctl_value(runner, "is-enabled", unit),
        }
    return states


def count_restart_lines(text: str) -> dict[str, int]:
    counts = {
        "started": 0,
        "stopped": 0,
        "failed": 0,
        "main_exited": 0,
        "scheduled_restart": 0,
    }
    for line in text.splitlines():
        row = line.strip()
        if row.startswith("Started "):
            counts["started"] += 1
        elif row.startswith("Stopped "):
            counts["stopped"] += 1
        if "Failed with result" in row:
            counts["failed"] += 1
        if "Main process exited" in row:
            counts["main_exited"] += 1
        if "Scheduled restart job" in row:
            counts["scheduled_restart"] += 1
    return counts


def collect_restarts(
    units: Sequence[str],
    start_ts: int,
    end_ts: int,
    runner: Runner,
) -> tuple[dict[str, Any], list[str]]:
    notes: list[str] = []
    per_unit: dict[str, Any] = {}
    grep_missing = False
    for unit in units:
        code, out, err = runner(
            [
                "journalctl",
                "-u",
                unit,
                "--since",
                f"@{start_ts}",
                "--until",
                f"@{end_ts}",
                "-o",
                "cat",
                "--no-pager",
                "--quiet",
                "-g",
                JOURNAL_GREP,
            ],
            timeout=15.0,
        )
        err_low = (err or "").lower()
        if code != 0 and (
            "unrecognized option" in err_low or "invalid option" in err_low
        ):
            grep_missing = True
            per_unit[unit] = {"skipped": True}
            continue
        if code not in (0, 1) and not out:
            per_unit[unit] = {"skipped": True}
            if code == 127:
                notes.append("journalctl not installed; restart counts skipped")
                break
            notes.append(f"{unit}: journalctl exit {code}")
            continue
        # A week of matched lines is small. Cap so a broken grep cannot
        # pin a huge journal in memory; counts then cover the prefix only.
        if len(out) > 2_000_000:
            out = out[:2_000_000]
            notes.append(f"{unit}: restart scan truncated")
        per_unit[unit] = count_restart_lines(out)
    if grep_missing:
        notes.append("journalctl has no -g; restart counts skipped")
    totals = {
        "started": 0,
        "stopped": 0,
        "failed": 0,
        "main_exited": 0,
        "scheduled_restart": 0,
    }
    for unit, row in per_unit.items():
        if not unit.startswith("nexvue-encode@"):
            continue
        for key in totals:
            value = row.get(key)
            if isinstance(value, int):
                totals[key] += value
    return {"units": per_unit, "encode_totals": totals}, notes


def decklink_helper() -> Path | None:
    override = os.environ.get("NEXVUE_DECKLINK_STATUS", "").strip()
    if override:
        path = Path(override)
        return path if path.is_file() else None
    found = shutil.which("decklink-status")
    if found:
        return Path(found)
    default = Path("/usr/local/bin/decklink-status")
    return default if default.is_file() else None


def probe_decklink(runner: Runner) -> tuple[Any, str | None]:
    path = decklink_helper()
    if path is None:
        return None, "decklink-status not installed"
    code, out, _err = runner([str(path)], timeout=20.0)
    body = (out or "").strip()
    if not body:
        return None, f"decklink-status produced no output (exit {code})"
    try:
        return json.loads(body), None
    except json.JSONDecodeError:
        return body, None


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    if not _IDENT.fullmatch(table):
        return []
    cols: list[str] = []
    for row in conn.execute(f"PRAGMA table_info({table})"):
        name = str(row[1])
        if _IDENT.fullmatch(name):
            cols.append(name)
    return cols


def load_series(
    conn: sqlite3.Connection,
    table: str,
    fields: Sequence[str],
    start_ts: int,
    end_ts: int,
) -> list[dict[str, Any]]:
    present = set(table_columns(conn, table))
    cols = [name for name in fields if name in present]
    if "ts" not in cols:
        raise sqlite3.Error(f"{table} has no ts column")
    sql = (
        f"SELECT {', '.join(cols)} FROM {table} "
        "WHERE ts >= ? AND ts <= ? ORDER BY ts ASC"
    )
    cur = conn.execute(sql, (start_ts, end_ts))
    names = [item[0] for item in cur.description] if cur.description else []
    rows = []
    for record in cur.fetchall():
        rows.append({key: json_value(value) for key, value in zip(names, record)})
    return rows


def thin_samples(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    count = len(rows)
    if limit < 1 or count <= limit:
        return rows
    if limit == 1:
        return [rows[-1]]
    step = (count - 1) / (limit - 1)
    picked: list[dict[str, Any]] = []
    seen: set[int] = set()
    for i in range(limit):
        idx = int(round(i * step))
        if idx >= count:
            idx = count - 1
        if idx in seen:
            continue
        seen.add(idx)
        picked.append(rows[idx])
    return picked


def _trim_num(value: float) -> float | int:
    if value.is_integer():
        return int(value)
    return round(value, 3)


def numeric_stats(values: Sequence[Any]) -> dict[str, float | int] | None:
    nums: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        nums.append(float(value))
    if not nums:
        return None
    return {
        "min": _trim_num(min(nums)),
        "max": _trim_num(max(nums)),
        "avg": round(sum(nums) / len(nums), 3),
    }


def summarize_series(
    rows: Sequence[dict[str, Any]],
    fields: Sequence[str],
    tz: Any,
) -> dict[str, Any]:
    buckets: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        ts = row.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            continue
        key = int(ts) // 3600 * 3600
        buckets.setdefault(key, []).append(row)
    out: list[dict[str, Any]] = []
    for key in sorted(buckets):
        group = buckets[key]
        item: dict[str, Any] = {
            "ts": key,
            "local": datetime.fromtimestamp(key, tz).isoformat(timespec="seconds"),
            "n": len(group),
        }
        for field in fields:
            if field == "ts":
                continue
            stats = numeric_stats([row.get(field) for row in group])
            if stats is not None:
                item[field] = stats
        out.append(item)
    return {"bucket_seconds": 3600, "buckets": out}


def session_summary(
    conn: sqlite3.Connection,
    start_ts: int,
    end_ts: int,
) -> dict[str, Any] | None:
    names = table_names(conn)
    if "viewer_sessions" not in names:
        return None
    cols = set(table_columns(conn, "viewer_sessions"))
    if "last_seen" not in cols:
        return None
    select = ["COUNT(*) AS sessions"]
    if "bytes_sent" in cols:
        select.append("COALESCE(SUM(bytes_sent), 0) AS bytes_sent")
    if "remote_addr" in cols:
        select.append("COUNT(DISTINCT remote_addr) AS distinct_remotes")
    if "user" in cols:
        select.append(
            "COUNT(DISTINCT CASE WHEN user IS NOT NULL AND user != '' "
            "THEN user END) AS distinct_users"
        )
    if "first_seen" in cols:
        select.append("MIN(first_seen) AS earliest_first_seen")
    select.append("MAX(last_seen) AS latest_last_seen")
    if "first_seen" in cols:
        where = "last_seen >= ? AND (first_seen IS NULL OR first_seen <= ?)"
        params: tuple[int, ...] = (start_ts, end_ts)
    else:
        where = "last_seen >= ? AND last_seen <= ?"
        params = (start_ts, end_ts)
    group = "channel" if "channel" in cols else None
    sql = f"SELECT {', '.join(select)}"
    if group:
        sql += ", channel"
    sql += f" FROM viewer_sessions WHERE {where}"
    if group:
        sql += " GROUP BY channel ORDER BY channel"
    by_channel: list[dict[str, Any]] = []
    totals = {
        "sessions": 0,
        "bytes_sent": 0,
        "distinct_remotes": 0,
        "distinct_users": 0,
    }
    saw_bytes = "bytes_sent" in cols
    saw_remotes = "remote_addr" in cols
    saw_users = "user" in cols
    earliest = None
    latest = None
    for row in conn.execute(sql, params):
        record = dict(row)
        sessions = int(record.get("sessions") or 0)
        totals["sessions"] += sessions
        if saw_bytes:
            totals["bytes_sent"] += int(record.get("bytes_sent") or 0)
        first = record.get("earliest_first_seen")
        last = record.get("latest_last_seen")
        if isinstance(first, int) and (earliest is None or first < earliest):
            earliest = first
        if isinstance(last, int) and (latest is None or last > latest):
            latest = last
        if group:
            item: dict[str, Any] = {
                "channel": record.get("channel"),
                "sessions": sessions,
            }
            if saw_bytes:
                item["bytes_sent"] = int(record.get("bytes_sent") or 0)
            if saw_remotes:
                item["distinct_remotes"] = int(record.get("distinct_remotes") or 0)
            by_channel.append(item)
    # Distinct viewers are counted once for the week. Summing the
    # per-channel distincts would double-count someone who watched two paths.
    if saw_remotes or saw_users:
        bits = []
        if saw_remotes:
            bits.append("COUNT(DISTINCT remote_addr) AS distinct_remotes")
        if saw_users:
            bits.append(
                "COUNT(DISTINCT CASE WHEN user IS NOT NULL AND user != '' "
                "THEN user END) AS distinct_users"
            )
        overall = conn.execute(
            f"SELECT {', '.join(bits)} FROM viewer_sessions WHERE {where}",
            params,
        ).fetchone()
        if overall is not None:
            overall_row = dict(overall)
            if saw_remotes:
                totals["distinct_remotes"] = int(overall_row.get("distinct_remotes") or 0)
            if saw_users:
                totals["distinct_users"] = int(overall_row.get("distinct_users") or 0)
    summary: dict[str, Any] = {"sessions": totals["sessions"]}
    if saw_bytes:
        summary["bytes_sent"] = totals["bytes_sent"]
    if saw_remotes:
        summary["distinct_remotes"] = totals["distinct_remotes"]
    if saw_users:
        summary["distinct_users"] = totals["distinct_users"]
    if earliest is not None:
        summary["earliest_first_seen"] = earliest
    if latest is not None:
        summary["latest_last_seen"] = latest
    if by_channel:
        summary["by_channel"] = by_channel
    return summary


def sqlite_ro_uri(path: Path) -> str:
    return "file:" + quote(path.resolve().as_posix(), safe="/") + "?mode=ro"


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(redact_obj(payload), indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(path, 0o640)
    except OSError:
        pass


def write_text(path: Path, body: str) -> None:
    path.write_text(redact_text(body), encoding="utf-8")
    try:
        os.chmod(path, 0o640)
    except OSError:
        pass


def write_jsonl_gz(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    # GzipFile does not close a file object it was handed.
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as handle:
            for row in rows:
                line = json.dumps(row, separators=(",", ":"), default=str) + "\n"
                handle.write(line.encode("utf-8"))
    try:
        os.chmod(path, 0o640)
    except OSError:
        pass


def prune_old_packs(root: Path, keep: Path) -> None:
    if not root.is_dir():
        return
    cutoff = time.time() - LOCAL_KEEP_SECONDS
    for child in root.iterdir():
        if not child.is_dir() or not _WEEK.fullmatch(child.name):
            continue
        try:
            if child.resolve() == keep.resolve():
                continue
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                log(f"pruned old pack {child.name}")
        except OSError:
            continue


def clear_pack_names(directory: Path) -> None:
    for name in (
        "manifest.json",
        "host_samples.jsonl.gz",
        "host_summary.json",
        "services.json",
        "restarts.json",
        "viewer_sessions.json",
        "totals.json",
        "decklink-status.json",
        "decklink-status.txt",
    ):
        try:
            (directory / name).unlink()
        except OSError:
            pass


def file_entry(path: Path, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": path.name, "bytes": path.stat().st_size}
    entry.update(extra)
    return entry


def build_pack(
    *,
    now: datetime | None = None,
    days: int = DEFAULT_DAYS,
    out_dir: Path | None = None,
    hostname: str | None = None,
    runner: Runner = run_cmd,
    decklink: Any = PROBE,
) -> dict[str, Any]:
    if not 1 <= int(days) <= MAX_DAYS:
        die(f"days must be 1-{MAX_DAYS}")
    days = int(days)

    settings = station_settings()
    tz_name, tz = resolve_timezone(settings, runner)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    end = moment.astimezone(timezone.utc)
    start = end - timedelta(days=days)
    # Sunday 23:00 America/New_York is already Monday in UTC. The folder
    # is the ISO week of the local window end, which is the week that
    # contains that Sunday.
    local_end = end.astimezone(tz)
    week = iso_week_key(local_end)
    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())

    raw_host = (hostname or socket.gethostname() or "nexvue").strip() or "nexvue"
    s3_host = clean_hostname(raw_host)
    version, git_sha = product_version(data_dir())
    site_id, site_source = site_identity(settings)

    bucket_raw = settings.get("NEXVUE_S3_BUCKET", "").strip()
    prefix = clean_prefix(settings.get("NEXVUE_S3_PREFIX"))
    region = clean_region(settings.get("NEXVUE_S3_REGION", ""))
    bucket = clean_bucket(bucket_raw) if bucket_raw else ""
    destination = (
        s3_prefix_uri(bucket, prefix, s3_host, week) if bucket else None
    )

    if decklink is PROBE:
        decklink_payload, decklink_note = probe_decklink(runner)
    else:
        decklink_payload, decklink_note = decklink, None
    physical = decklink_indices(decklink_payload)
    channels_dir = etc_dir() / "channels"
    file_slots, ignored_files = channel_env_indices(channels_dir)
    slots, slot_source, dropped = resolve_encode_slots(
        max_channels=slot_count(settings.get("MAX_CHANNELS")),
        max_devices=slot_count(settings.get("MAX_DEVICES")),
        channel_indices=file_slots,
        decklink=physical,
    )
    units = encode_units(slots)

    notes: list[str] = []
    if tz_name == "UTC" and settings.get("NEXVUE_METRICS_TZ", "").strip() not in ("", "UTC"):
        notes.append("timezone data missing; stamps are UTC")
    if decklink_note:
        notes.append(decklink_note)
    if ignored_files:
        names = ", ".join(f"{idx}.env" for idx in ignored_files)
        notes.append(
            f"ignored channel env {names}; a Quad's inputs are 0-{SLOT_CEILING - 1}"
        )
    if dropped:
        listed = ", ".join(f"nexvue-encode@{idx}" for idx in dropped)
        notes.append(f"omitted {listed}; DeckLink did not report those inputs")
    if not slots:
        notes.append("no configured encode slots (no MAX_CHANNELS, channel env, or DeckLink devices)")

    log(f"encode slots: {slots or 'none'} ({slot_source})")
    services = collect_services(units, runner)
    restarts, restart_notes = collect_restarts(units, start_ts, end_ts, runner)
    notes.extend(restart_notes)

    host_rows: list[dict[str, Any]] = []
    host_fields: list[str] = []
    total_rows: list[dict[str, Any]] = []
    totals_present = False
    sessions: dict[str, Any] | None = None
    db_path = metrics_db_path()
    if db_path.is_file():
        try:
            conn = sqlite3.connect(sqlite_ro_uri(db_path), uri=True, timeout=10)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            notes.append(f"metrics db: {exc}")
            conn = None
        if conn is not None:
            try:
                names = table_names(conn)
                if "host_samples" in names:
                    host_fields = [
                        name
                        for name in HOST_SAMPLE_FIELDS
                        if name in set(table_columns(conn, "host_samples"))
                    ]
                    try:
                        host_rows = load_series(
                            conn, "host_samples", HOST_SAMPLE_FIELDS, start_ts, end_ts
                        )
                    except sqlite3.Error as exc:
                        notes.append(f"host_samples: {exc}")
                else:
                    notes.append("host_samples table not in metrics.db")
                if "totals" in names and "ts" in set(table_columns(conn, "totals")):
                    try:
                        total_rows = load_series(
                            conn, "totals", TOTAL_FIELDS, start_ts, end_ts
                        )
                        totals_present = True
                    except sqlite3.Error as exc:
                        notes.append(f"totals: {exc}")
                if "viewer_sessions" in names:
                    try:
                        sessions = session_summary(conn, start_ts, end_ts)
                    except sqlite3.Error as exc:
                        notes.append(f"viewer_sessions: {exc}")
                        sessions = None
            finally:
                conn.close()
    else:
        notes.append(f"metrics db missing: {db_path}")

    kept_rows = thin_samples(host_rows, MAX_HOST_SAMPLE_ROWS)
    if len(kept_rows) < len(host_rows):
        notes.append(
            f"host_samples thinned {len(host_rows)} to {len(kept_rows)}; "
            "hourly summary still uses every sample"
        )
    host_summary = summarize_series(host_rows, host_fields or HOST_SAMPLE_FIELDS, tz)
    totals_summary = summarize_series(total_rows, TOTAL_FIELDS, tz)

    if out_dir is None:
        directory = data_dir() / "weekly" / week
        prune_old_packs(directory.parent, directory)
    else:
        directory = out_dir
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o750)
    except OSError:
        pass
    clear_pack_names(directory)

    written: list[dict[str, Any]] = []

    samples_path = directory / "host_samples.jsonl.gz"
    write_jsonl_gz(samples_path, kept_rows)
    written.append(
        file_entry(
            samples_path,
            rows=len(kept_rows),
            raw_rows=len(host_rows),
        )
    )
    summary_path = directory / "host_summary.json"
    write_json(summary_path, host_summary)
    written.append(
        file_entry(summary_path, buckets=len(host_summary["buckets"]))
    )

    services_path = directory / "services.json"
    write_json(
        services_path,
        {
            "slot_ceiling": SLOT_CEILING,
            "encode_slots": slots,
            "encode_slots_source": slot_source,
            "units": services,
        },
    )
    written.append(file_entry(services_path))

    restarts_path = directory / "restarts.json"
    write_json(
        restarts_path,
        {
            "window_start_utc": fmt_utc(start),
            "window_end_utc": fmt_utc(end),
            **restarts,
        },
    )
    written.append(file_entry(restarts_path))

    if isinstance(decklink_payload, (dict, list)):
        deck_path = directory / "decklink-status.json"
        write_json(deck_path, decklink_payload)
        written.append(file_entry(deck_path))
    elif isinstance(decklink_payload, str) and decklink_payload.strip():
        deck_path = directory / "decklink-status.txt"
        write_text(deck_path, decklink_payload)
        written.append(file_entry(deck_path))

    if sessions is not None:
        sessions_path = directory / "viewer_sessions.json"
        write_json(sessions_path, sessions)
        written.append(file_entry(sessions_path))

    if totals_present:
        totals_path = directory / "totals.json"
        write_json(totals_path, totals_summary)
        written.append(file_entry(totals_path, raw_rows=len(total_rows)))

    manifest: dict[str, Any] = {
        "product": "NexVUE",
        "pack_format": PACK_FORMAT,
        "hostname": raw_host,
        "s3_hostname": s3_host,
        "timezone": tz_name,
        "product_version": version,
        "window_days": days,
        "window_start_utc": fmt_utc(start),
        "window_end_utc": fmt_utc(end),
        "window_start_local": fmt_local(start, tz),
        "window_end_local": fmt_local(end, tz),
        "iso_week": week,
        "site_id": site_id,
        "site_id_source": site_source,
        "encode_slots": slots,
        "encode_slots_source": slot_source,
        "slot_ceiling": SLOT_CEILING,
        "s3_uri": destination,
        "s3_region": region or None,
        "metrics_db": str(db_path),
        "files": written,
        "notes": notes,
        "redactions": [
            "URL userinfo (user:pass@) replaced",
            "password, passphrase, token, and api key values replaced",
            "viewer sessions are counts only (no addresses, user names, or user agents)",
            "restart journal text is not included",
            "nexvue.env is not copied",
        ],
    }
    if git_sha:
        manifest["product_git_sha"] = git_sha
    manifest_path = directory / "manifest.json"
    write_json(manifest_path, manifest)

    log(f"pack {directory}")
    names = [entry["name"] for entry in written]
    names.append("manifest.json")
    return {
        "directory": directory,
        "files": names,
        "s3_uri": destination,
        "s3_bucket": bucket,
        "s3_prefix": prefix,
        "s3_region": region,
        "s3_hostname": s3_host,
        "iso_week": week,
        "hostname": raw_host,
    }


def upload_with_aws(
    directory: Path,
    names: Sequence[str],
    *,
    bucket: str,
    prefix: str,
    host: str,
    week: str,
    region: str,
    runner: Runner = run_cmd,
) -> None:
    aws = shutil.which("aws")
    if not aws:
        raise UploadError("aws CLI not found")
    for name in names:
        uri = s3_object_uri(bucket, prefix, host, week, name)
        argv = [aws, "s3", "cp", str(directory / name), uri]
        if region:
            argv.extend(["--region", region])
        code, out, err = runner(argv, timeout=120.0)
        if code != 0:
            detail = redact_text((err or out or f"exit {code}").strip())
            raise UploadError(f"aws s3 cp {name} failed: {detail.splitlines()[0][:300]}")
        log(f"uploaded {uri}")


def upload_with_boto3(
    directory: Path,
    names: Sequence[str],
    *,
    bucket: str,
    prefix: str,
    host: str,
    week: str,
    region: str,
) -> None:
    try:
        import boto3
    except ImportError as exc:
        raise UploadError(
            "aws CLI not found and boto3 is not installed"
        ) from exc
    kwargs: dict[str, Any] = {}
    if region:
        kwargs["region_name"] = region
    # Default session: instance role / ambient chain. No keys are passed.
    client = boto3.client("s3", **kwargs)
    for name in names:
        key = f"{prefix}/{host}/weekly/{week}/{name}"
        client.upload_file(str(directory / name), bucket, key)
        log(f"uploaded s3://{bucket}/{key}")


def upload_pack(
    directory: Path,
    names: Sequence[str],
    *,
    bucket: str,
    prefix: str,
    host: str,
    week: str,
    region: str,
    runner: Runner = run_cmd,
) -> str:
    ordered = [name for name in names if name != "manifest.json"]
    if "manifest.json" in names:
        # Manifest last, so a failed sample upload does not leave a
        # manifest that points at objects that never arrived.
        ordered.append("manifest.json")
    if shutil.which("aws"):
        upload_with_aws(
            directory,
            ordered,
            bucket=bucket,
            prefix=prefix,
            host=host,
            week=week,
            region=region,
            runner=runner,
        )
        return "aws"
    upload_with_boto3(
        directory,
        ordered,
        bucket=bucket,
        prefix=prefix,
        host=host,
        week=week,
        region=region,
    )
    return "boto3"


def parse_now(text: str) -> datetime:
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        die(f"--now is not an ISO timestamp: {text}")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a NexVUE weekly metrics pack and upload it to S3"
    )
    parser.add_argument(
        "--no-upload",
        "--dry-run",
        action="store_true",
        dest="no_upload",
        help="Write the pack locally and do not call S3",
    )
    parser.add_argument(
        "--out-dir",
        default="",
        help="Directory for this pack (default: $NEXVUE_DATA/weekly/YYYY-Www)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"Trailing window in days (default {DEFAULT_DAYS})",
    )
    parser.add_argument(
        "--now",
        default="",
        help="Window end as an ISO timestamp (tests). Default is the current time.",
    )
    args = parser.parse_args(argv)

    settings = station_settings()
    bucket_raw = settings.get("NEXVUE_S3_BUCKET", "").strip()
    if not args.no_upload and not bucket_raw:
        die("NEXVUE_S3_BUCKET is unset. Set it in nexvue.env, or pass --no-upload.")
    if bucket_raw:
        clean_bucket(bucket_raw)
    clean_prefix(settings.get("NEXVUE_S3_PREFIX"))
    clean_region(settings.get("NEXVUE_S3_REGION", ""))

    pack = build_pack(
        now=parse_now(args.now) if args.now else None,
        days=args.days,
        out_dir=Path(args.out_dir) if args.out_dir else None,
    )
    if args.no_upload:
        if pack["s3_uri"]:
            log(f"dry-run: would upload to {pack['s3_uri']}")
        else:
            log("dry-run: NEXVUE_S3_BUCKET unset; pack was not uploaded")
        print(str(Path(pack["directory"]).resolve()))
        return 0
    try:
        upload_pack(
            Path(pack["directory"]),
            pack["files"],
            bucket=pack["s3_bucket"],
            prefix=pack["s3_prefix"],
            host=pack["s3_hostname"],
            week=pack["iso_week"],
            region=pack["s3_region"],
        )
    except UploadError as exc:
        log(f"upload failed: {exc}")
        log(f"local pack kept at {pack['directory']}")
        return 1
    print(pack["s3_uri"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"weekly pack failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
