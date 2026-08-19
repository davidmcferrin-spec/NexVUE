#!/usr/bin/env python3
"""
nexvue-ops-portal-write.py — write NEXVUE_PORTAL_* fields to the station env
(/etc/nexvue/nexvue.env).

Run via sudo as root by nexvue-ops-portal-write.sh, invoked from
nexvue-auth.php's `portal_enroll` action (admin session only — the enroll
call itself is outbound from the edge to the portal; this script only
persists the result locally). JSON patch on stdin, one or more of:
  {"url": "...", "station_id": "...", "station_api_key": "...",
   "adopted_at": "...", "heartbeat_interval_s": "..."}
Any subset may be present; each key present is validated and written.

Also importable by unit tests.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

STATION_ENV = Path("/etc/nexvue/nexvue.env")

# JSON key -> (env var name, validator)
FIELD_MAP = {
    "url": "NEXVUE_PORTAL_URL",
    "station_id": "NEXVUE_PORTAL_STATION_ID",
    "station_api_key": "NEXVUE_PORTAL_STATION_API_KEY",
    "adopted_at": "NEXVUE_PORTAL_ADOPTED_AT",
    "heartbeat_interval_s": "NEXVUE_PORTAL_HEARTBEAT_INTERVAL_S",
}

ASSIGN_RE = re.compile(r"^(\s*)(#?)(\s*)([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
# Same "unquoted-safe" charset as nexvue-ops-env-update.py — nexvue.env is
# SOURCED by bash (nexvue-encode@.service reads it as the global file after
# the channel file), so an unquoted value with a space/paren/etc. corrupts
# the next shell token exactly like the CHANNEL_ALIAS bug class.
UNQUOTED_SAFE_RE = re.compile(r"^[A-Za-z0-9_./:+=,-]+$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def format_assignment_value(value: str) -> str:
    if value == "" or UNQUOTED_SAFE_RE.match(value):
        return value
    return f'"{value}"'


def sanitize(key: str, value: str) -> str:
    value = value.strip()
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError(f"{key}: value must be a single line")
    if key == "url":
        if value == "":
            return value
        if not re.match(r"^https://[A-Za-z0-9.\-]+(:\d{1,5})?(/.*)?$", value):
            raise ValueError("url must be an https:// URL")
        if len(value) > 512:
            raise ValueError("url too long")
        return value.rstrip("/")
    if key == "station_id":
        if value == "":
            return value
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ValueError("station_id must be alphanumeric/-/_ (max 128)")
        return value
    if key == "station_api_key":
        if value == "":
            return value
        if not re.fullmatch(r"[A-Za-z0-9_.-]{8,512}", value):
            raise ValueError("station_api_key: unexpected shape")
        return value
    if key == "adopted_at":
        if value == "":
            return value
        if not ISO_RE.match(value):
            raise ValueError("adopted_at must be an ISO-8601 UTC timestamp (YYYY-MM-DDTHH:MM:SSZ)")
        return value
    if key == "heartbeat_interval_s":
        if value == "":
            return value
        if not re.fullmatch(r"[0-9]+", value):
            raise ValueError("heartbeat_interval_s must be an integer")
        n = int(value)
        if not (60 <= n <= 86400):
            raise ValueError("heartbeat_interval_s must be 60-86400")
        return value
    raise ValueError(f"unknown field: {key}")


def apply_patch(text: str, patch: dict[str, str]) -> str:
    """Update KEY=value lines in place; append any missing keys. Mirrors the
    channel-env updater's approach (nexvue-ops-env-update.py) but scoped to
    the small, fixed NEXVUE_PORTAL_* key set."""
    if not patch:
        return text

    cleaned: dict[str, str] = {}
    for json_key, raw in patch.items():
        env_key = FIELD_MAP.get(json_key)
        if env_key is None:
            raise ValueError(f"unknown field: {json_key}")
        cleaned[env_key] = sanitize(json_key, str(raw))

    lines = text.splitlines(keepends=True)
    pending = dict(cleaned)
    new_lines: list[str] = []

    def line_ending(line: str) -> str:
        return "\r\n" if line.endswith("\r\n") else "\n"

    for line in lines:
        m = ASSIGN_RE.match(line.rstrip("\r\n"))
        if not m:
            new_lines.append(line)
            continue
        indent, hashmark, _sp, key, _old = m.groups()
        ending = line_ending(line)
        if key in pending and not hashmark:
            val = format_assignment_value(pending.pop(key))
            new_lines.append(f"{indent}{key}={val}{ending}")
            continue
        new_lines.append(line)

    if pending:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] = new_lines[-1] + "\n"
        if not any("# --- Portal adoption" in ln for ln in new_lines):
            if new_lines and new_lines[-1].strip():
                new_lines.append("\n")
            new_lines.append("# --- Portal adoption (written by Settings -> Adopt) ---\n")
        for key, val in pending.items():
            new_lines.append(f"{key}={format_assignment_value(val)}\n")

    return "".join(new_lines)


def main(argv: list[str]) -> int:
    del argv
    try:
        patch = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"invalid JSON: {exc}"}), file=sys.stderr)
        return 1
    if not isinstance(patch, dict):
        print(json.dumps({"ok": False, "error": "patch must be a JSON object"}), file=sys.stderr)
        return 1
    text = STATION_ENV.read_text(encoding="utf-8", errors="replace") if STATION_ENV.is_file() else ""
    try:
        new_text = apply_patch(text, patch)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    tmp = STATION_ENV.with_suffix(STATION_ENV.suffix + f".tmp.{__import__('os').getpid()}")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(STATION_ENV)
    try:
        STATION_ENV.chmod(0o644)
    except OSError:
        pass
    print(json.dumps({"ok": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
