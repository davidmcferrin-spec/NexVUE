#!/usr/bin/env python3
"""
nexvue-ops-env-update.py — line-based updater for /etc/nexvue/channels/<N>.env

Preserves comments and layout. Used by nexvue-ops-env-read.sh /
nexvue-ops-env-write.sh (via sudo). Also importable by unit tests.

CLI:
  nexvue-ops-env-update.py read  <N>          -> JSON {keys, raw_exists}
  nexvue-ops-env-update.py write <N>          <- JSON patch on stdin
  nexvue-ops-env-update.py parse-file <path>  -> JSON keys (test helper)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CHANNELS_DIR = Path("/etc/nexvue/channels")

# Keys the ops UI may change. DEVICE_NUMBER / CHANNEL_PATH / RTSP_URL are
# identity/derived and stay read-only in the API.
EDITABLE_KEYS = frozenset({
    "CHANNEL_ALIAS",
    "DEINT_FIELDS",
    "BITRATE_KBPS",
    "GOP_FRAMES",
    "ENABLE_AUDIO",
    "AUDIO_FRAME_MS",
    "AUDIO_BITRATE_BPS",
    "AUDIO_CHANNELS",
    "DECKLINK_BUFFER_FRAMES",
    "VIDEO_ENCODER",
    "EXTRA_ENC_ARGS",
    "LO_ENABLE",
    "LO_PRESET",
    "LO_WIDTH",
    "LO_HEIGHT",
    "LO_BITRATE_KBPS",
    "LO_FPS",
    "LO_TARGET_USAGE",
    "LO_QUEUE_BUFFERS",
    "LO_GOP_FRAMES",
    "SIGNAL_LOSS_DEBOUNCE_S",
    "SIGNAL_ACQUIRE_DEBOUNCE_S",
    "DECKLINK_RETRY_S",
})

READONLY_KEYS = frozenset({"DEVICE_NUMBER", "CHANNEL_PATH", "RTSP_URL"})

# Match optional leading #, optional spaces, KEY=, then value (no stripping of
# trailing inline comments on ACTIVE lines — we rewrite the whole assignment).
ASSIGN_RE = re.compile(
    r"^(\s*)(#?)(\s*)([A-Za-z_][A-Za-z0-9_]*)=(.*)$"
)

ALIAS_SAFE_RE = re.compile(r"^[\w .:/()+#@&'*-]{0,64}$")

# Values matching this need no quoting when written. Anything else (spaces,
# parens, '#', ...) is double-quoted: the encoder unit SOURCES the env file
# through bash, so an unquoted `CHANNEL_ALIAS=TVU 35` runs `35` as a command
# with CHANNEL_ALIAS=TVU in its environment — the alias is silently lost.
UNQUOTED_SAFE_RE = re.compile(r"^[A-Za-z0-9_./:+=,-]+$")


def format_assignment_value(value: str) -> str:
    """Quote a value for a bash-sourced env file when required.

    Double quotes are safe because sanitize_value rejects every character
    that bash treats specially inside them ($ ` \\ " and, for non-alias
    keys, ' as well).
    """
    if value == "" or UNQUOTED_SAFE_RE.match(value):
        return value
    return f'"{value}"'


def channel_path(n: int) -> Path:
    if not isinstance(n, int) or n < 0 or n > 7:
        raise ValueError("channel id must be integer 0-7")
    return CHANNELS_DIR / f"{n}.env"


def parse_env_text(text: str) -> dict[str, str]:
    """Return last active (uncommented) assignment per key."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = ASSIGN_RE.match(line)
        if not m:
            continue
        _indent, hashmark, _sp, key, value = m.groups()
        if hashmark:
            continue
        value = value.strip()
        # Quoted value (as written by format_assignment_value, or by hand):
        # take the quoted content verbatim — a '#' inside is NOT a comment.
        if len(value) >= 2 and value[0] in "\"'" and value[0] == value[-1]:
            value = value[1:-1]
        elif " #" in value:
            # Strip a trailing inline comment only when preceded by whitespace
            # (shell-sourced files on live boxes sometimes have them).
            value = value.split(" #", 1)[0].strip()
        out[key] = value
    return out


def sanitize_value(key: str, value: str) -> str:
    value = value.strip()
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError(f"{key}: value must be a single line")
    if key == "CHANNEL_ALIAS":
        if not ALIAS_SAFE_RE.match(value):
            raise ValueError(
                "CHANNEL_ALIAS: use letters, numbers, spaces, and .:/()+#@&'*- only (max 64)"
            )
        return value
    # Everything else: alphanumeric-ish ops values; reject shell metacharacters
    # (quotes included, so format_assignment_value's double-quoting is safe).
    # Also reject '!' — EXTRA_ENC_ARGS is interpolated UNQUOTED into the
    # gst-launch-1.0 pipeline in nexvue-encode.sh, so a literal '!' splices
    # a new element/branch after the encoder (filesink, etc.).
    if re.search(r"[`$;&|<>!\\\"']", value):
        raise ValueError(f"{key}: disallowed characters in value")
    return value


def apply_patch(text: str, patch: dict[str, str]) -> str:
    """
    Update KEY=value lines in place. If a key exists only as a commented
    assignment, uncomment and set it. Missing keys are appended under an
    Ops UI marker block.
    """
    if not patch:
        return text

    cleaned: dict[str, str] = {}
    for key, raw in patch.items():
        if key not in EDITABLE_KEYS:
            raise ValueError(f"key not editable: {key}")
        cleaned[key] = sanitize_value(key, str(raw))

    lines = text.splitlines(keepends=True)
    pending = dict(cleaned)
    updated_active: set[str] = set()

    def line_ending(line: str) -> str:
        if line.endswith("\r\n"):
            return "\r\n"
        if line.endswith("\n"):
            return "\n"
        return "\n"

    # First pass: rewrite active (uncommented) assignments.
    new_lines: list[str] = []
    commented_slots: dict[str, int] = {}
    for line in lines:
        m = ASSIGN_RE.match(line.rstrip("\r\n"))
        if not m:
            new_lines.append(line)
            continue
        indent, hashmark, _sp, key, _old = m.groups()
        ending = line_ending(line)
        if key in pending and not hashmark and key not in updated_active:
            val = format_assignment_value(pending.pop(key))
            updated_active.add(key)
            new_lines.append(f"{indent}{key}={val}{ending}")
            continue
        if key in pending and hashmark and key not in commented_slots:
            commented_slots[key] = len(new_lines)
        new_lines.append(line)

    # Second pass: uncomment first commented assignment for remaining keys.
    for key, idx in list(commented_slots.items()):
        if key not in pending:
            continue
        line = new_lines[idx]
        m = ASSIGN_RE.match(line.rstrip("\r\n"))
        if not m:
            continue
        indent, _hashmark, _sp, k, _old = m.groups()
        ending = line_ending(line)
        val = format_assignment_value(pending.pop(key))
        new_lines[idx] = f"{indent}{k}={val}{ending}"

    # Append anything still missing.
    if pending:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] = new_lines[-1] + "\n"
        if not any("# --- Ops UI ---" in ln for ln in new_lines):
            if new_lines and new_lines[-1].strip():
                new_lines.append("\n")
            new_lines.append("# --- Ops UI ---\n")
        for key, val in pending.items():
            new_lines.append(f"{key}={format_assignment_value(val)}\n")

    return "".join(new_lines)


def cmd_read(n: int) -> int:
    path = channel_path(n)
    if not path.is_file():
        print(json.dumps({"ok": False, "error": f"missing {path}"}))
        return 1
    text = path.read_text(encoding="utf-8", errors="replace")
    keys = parse_env_text(text)
    print(json.dumps({"ok": True, "id": n, "path": str(path), "keys": keys}))
    return 0


def cmd_write(n: int) -> int:
    path = channel_path(n)
    if not path.is_file():
        print(json.dumps({"ok": False, "error": f"missing {path}"}), file=sys.stderr)
        return 1
    try:
        patch = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"invalid JSON: {exc}"}), file=sys.stderr)
        return 1
    if not isinstance(patch, dict):
        print(json.dumps({"ok": False, "error": "patch must be a JSON object"}), file=sys.stderr)
        return 1
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        new_text = apply_patch(text, patch)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    path.write_text(new_text, encoding="utf-8")
    keys = parse_env_text(new_text)
    print(json.dumps({"ok": True, "id": n, "keys": keys}))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: nexvue-ops-env-update.py read|write|parse-file ...", file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "parse-file":
        if len(argv) != 3:
            return 2
        text = Path(argv[2]).read_text(encoding="utf-8", errors="replace")
        print(json.dumps(parse_env_text(text)))
        return 0
    if cmd in ("read", "write"):
        if len(argv) != 3:
            return 2
        try:
            n = int(argv[2])
        except ValueError:
            print(json.dumps({"ok": False, "error": "N must be 0-7"}), file=sys.stderr)
            return 1
        return cmd_read(n) if cmd == "read" else cmd_write(n)
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
