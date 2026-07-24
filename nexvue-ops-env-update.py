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
from typing import Optional

CHANNELS_DIR = Path("/etc/nexvue/channels")
STATION_ENV = Path("/etc/nexvue/nexvue.env")
MAX_CHANNEL_ID = 9  # slots 0..9 (MAX_CHANNELS=10)

# Keys the ops UI may change. DEVICE_NUMBER / CHANNEL_PATH / RTSP_URL are
# identity/derived and stay read-only in the API.
EDITABLE_KEYS = frozenset({
    "CHANNEL_ALIAS",
    "INPUT_TYPE",
    "SRT_URI",
    "SRT_LATENCY_MS",
    "DEINT_FIELDS",
    "BITRATE_KBPS",
    "GOP_FRAMES",
    "ENABLE_AUDIO",
    "AUDIO_FRAME_MS",
    "AUDIO_BITRATE_BPS",
    "AUDIO_CHANNELS",
    "AUDIO_LAYOUT",
    "AUDIO_EMBEDS",
    "DECKLINK_BUFFER_FRAMES",
    "DECKLINK_DROP_NO_SIGNAL_FRAMES",
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
SRT_URI_SAFE_RE = re.compile(r"^srt://[A-Za-z0-9._~:/?#[\]@!$&'()*+,;=%-]+$", re.IGNORECASE)

# Curated LO framerates only — free-form "29.97" / "15" breaks GStreamer caps.
LO_FPS_ALLOWED = frozenset({"", "60000/1001", "30000/1001", "15000/1001"})
LO_PRESET_ALLOWED = frozenset({"720p", "540p", "480p", "360p", "240p", "180p"})
LO_TARGET_USAGE_ALLOWED = frozenset({"1", "2", "3", "4", "5", "6", "7"})
AUDIO_FRAME_MS_ALLOWED = frozenset({"2", "5", "10", "20", "40", "60"})
AUDIO_CHANNELS_ALLOWED = frozenset({"2", "4", "6", "8"})
AUDIO_LAYOUT_ALLOWED = frozenset({"stereo", "51", "stereo_sap", "51_sap"})
# 1-based SDI embeds enabled for browser VU / listen (metadata; encode is always 8ch).
AUDIO_EMBEDS_DEFAULT = "1,2,3,4,5,6,7,8"
DEINT_ALLOWED = frozenset({"all", "top"})
VIDEO_ENCODER_ALLOWED = frozenset({"vah264enc", "x264enc"})
BOOL_ALLOWED = frozenset({"true", "false"})

# Values matching this need no quoting when written. Anything else (spaces,
# parens, '#', ...) is double-quoted: the encoder unit SOURCES the env file
# through bash, so an unquoted `CHANNEL_ALIAS=TVU 35` runs `35` as a command
# with CHANNEL_ALIAS=TVU in its environment — the alias is silently lost.
UNQUOTED_SAFE_RE = re.compile(r"^[A-Za-z0-9_./:+=,-]+$")


def _require_int(
    key: str,
    value: str,
    *,
    lo: Optional[int] = None,
    hi: Optional[int] = None,
    allow_empty: bool = True,
) -> str:
    if value == "":
        if allow_empty:
            return value
        raise ValueError(f"{key}: required")
    if not re.fullmatch(r"-?[0-9]+", value):
        raise ValueError(f"{key}: must be an integer")
    n = int(value)
    if lo is not None and n < lo:
        raise ValueError(f"{key}: must be >= {lo}")
    if hi is not None and n > hi:
        raise ValueError(f"{key}: must be <= {hi}")
    return value


def _require_even_positive(key: str, value: str) -> str:
    if value == "":
        return value
    _require_int(key, value, lo=2, allow_empty=False)
    if int(value) % 2:
        raise ValueError(f"{key}: must be an even positive integer (H.264 raster)")
    return value


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
    if not isinstance(n, int) or n < 0 or n > MAX_CHANNEL_ID:
        raise ValueError(f"channel id must be integer 0-{MAX_CHANNEL_ID}")
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
    if key == "INPUT_TYPE":
        if value == "":
            return value
        low = value.lower()
        if low not in ("decklink", "srt"):
            raise ValueError("INPUT_TYPE must be decklink or srt")
        return low
    if key == "SRT_URI":
        if value == "":
            return value
        if not SRT_URI_SAFE_RE.match(value):
            raise ValueError(
                "SRT_URI must be an srt:// URI (caller or listener); "
                "disallowed shell metacharacters"
            )
        return value
    if key == "SRT_LATENCY_MS":
        return _require_int(key, value, lo=0, hi=10000)
    if key == "DEINT_FIELDS":
        if value == "":
            return value
        if value not in DEINT_ALLOWED:
            raise ValueError("DEINT_FIELDS must be all or top")
        return value
    if key == "BITRATE_KBPS":
        return _require_int(key, value, lo=100, hi=50000)
    if key == "GOP_FRAMES":
        return _require_int(key, value, lo=1, hi=300)
    if key in ("ENABLE_AUDIO", "LO_ENABLE"):
        if value == "":
            return value
        low = value.lower()
        if low not in BOOL_ALLOWED:
            raise ValueError(f"{key} must be true or false")
        return low
    if key == "AUDIO_FRAME_MS":
        if value == "":
            return value
        if value not in AUDIO_FRAME_MS_ALLOWED:
            raise ValueError("AUDIO_FRAME_MS must be one of 2,5,10,20,40,60")
        return value
    if key == "AUDIO_BITRATE_BPS":
        return _require_int(key, value, lo=8000, hi=512000)
    if key == "AUDIO_CHANNELS":
        if value == "":
            return value
        if value not in AUDIO_CHANNELS_ALLOWED:
            raise ValueError("AUDIO_CHANNELS must be 2, 4, 6, or 8")
        return value
    if key == "AUDIO_LAYOUT":
        if value == "":
            return value
        low = value.lower().replace("-", "_")
        aliases = {
            "5.1": "51",
            "5_1": "51",
            "surround": "51",
            "sap": "stereo_sap",
            "5.1_sap": "51_sap",
            "5_1_sap": "51_sap",
            "surround_sap": "51_sap",
        }
        low = aliases.get(low, low)
        if low not in AUDIO_LAYOUT_ALLOWED:
            raise ValueError("AUDIO_LAYOUT must be stereo, 51, stereo_sap, or 51_sap")
        return low
    if key == "AUDIO_EMBEDS":
        # Blank = all eight (browser default). Accept "1,2,7,8" or "1-8".
        if value == "":
            return value
        raw = value.lower().replace(" ", "")
        if raw in ("1-8", "all", "*"):
            return AUDIO_EMBEDS_DEFAULT
        parts = [p for p in raw.split(",") if p]
        if not parts:
            raise ValueError("AUDIO_EMBEDS: list at least one embed 1-8")
        seen: set[int] = set()
        ordered: list[int] = []
        for p in parts:
            if not re.fullmatch(r"[1-8]", p):
                raise ValueError("AUDIO_EMBEDS: each entry must be an integer 1-8")
            n = int(p)
            if n not in seen:
                seen.add(n)
                ordered.append(n)
        ordered.sort()
        return ",".join(str(n) for n in ordered)
    if key == "DECKLINK_BUFFER_FRAMES":
        return _require_int(key, value, lo=1, hi=16)
    if key == "DECKLINK_DROP_NO_SIGNAL_FRAMES":
        if value == "":
            return value
        if value not in ("true", "false"):
            raise ValueError("DECKLINK_DROP_NO_SIGNAL_FRAMES must be true or false")
        return value
    if key == "VIDEO_ENCODER":
        if value == "":
            return value
        if value not in VIDEO_ENCODER_ALLOWED:
            raise ValueError("VIDEO_ENCODER must be vah264enc or x264enc")
        return value
    if key == "LO_PRESET":
        if value == "":
            return value
        if value not in LO_PRESET_ALLOWED:
            raise ValueError(
                "LO_PRESET must be one of " + ", ".join(sorted(LO_PRESET_ALLOWED))
            )
        return value
    if key in ("LO_WIDTH", "LO_HEIGHT"):
        # Settings UI uses "auto" for follow-preset (stored as blank).
        if value == "" or value.lower() == "auto":
            return ""
        return _require_even_positive(key, value)
    if key == "LO_BITRATE_KBPS":
        if value == "" or value.lower() == "auto":
            return ""
        return _require_int(key, value, lo=100, hi=20000)
    if key == "LO_FPS":
        # Normalize legacy bare rates that used to become framerate=(int)N and
        # fail videoscale→vah264enc (encoder restart storm).
        aliases = {
            "60": "60000/1001",
            "59.94": "60000/1001",
            "30": "30000/1001",
            "29.97": "30000/1001",
            "15": "15000/1001",
            "14.99": "15000/1001",
        }
        value = aliases.get(value, value)
        if value not in LO_FPS_ALLOWED:
            raise ValueError(
                "LO_FPS must be 60000/1001, 30000/1001, or 15000/1001"
            )
        return value
    if key == "LO_TARGET_USAGE":
        if value == "":
            return value
        if value not in LO_TARGET_USAGE_ALLOWED:
            raise ValueError("LO_TARGET_USAGE must be an integer 1-7")
        return value
    if key == "LO_QUEUE_BUFFERS":
        return _require_int(key, value, lo=1, hi=64)
    if key == "LO_GOP_FRAMES":
        return _require_int(key, value, lo=1, hi=300)
    if key in ("SIGNAL_LOSS_DEBOUNCE_S", "SIGNAL_ACQUIRE_DEBOUNCE_S", "DECKLINK_RETRY_S"):
        if value == "":
            return value
        try:
            f = float(value)
        except ValueError as exc:
            raise ValueError(f"{key}: must be a number") from exc
        if f < 0:
            raise ValueError(f"{key}: must be >= 0")
        if key == "DECKLINK_RETRY_S" and f <= 0:
            raise ValueError(f"{key}: must be > 0")
        return value
    if key == "EXTRA_ENC_ARGS":
        # Settings UI uses "none" for no extra properties (stored as blank).
        if value == "" or value.lower() == "none":
            return ""
        if re.search(r"[`$;&|<>!\\\"']", value):
            raise ValueError(f"{key}: disallowed characters in value")
        return value
    # Everything else: alphanumeric-ish; reject shell metacharacters.
    # Also reject '!' — EXTRA_ENC_ARGS is interpolated UNQUOTED into the
    # gst-launch pipeline in nexvue-encode.sh (handled above).
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
            print(json.dumps({"ok": False, "error": f"N must be 0-{MAX_CHANNEL_ID}"}), file=sys.stderr)
            return 1
        return cmd_read(n) if cmd == "read" else cmd_write(n)
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
