#!/usr/bin/env python3
"""
nexvue-ops-hardware-write.py — persist card / encode slot count.

Writes MAX_DEVICES and MAX_CHANNELS (same value) to /etc/nexvue/nexvue.env,
seeds missing /etc/nexvue/channels/<N>.env, strips leftover per-channel
MAX_DEVICES copies, and enable --now / disable --now nexvue-encode@N to
match. Restart is not needed for already-running slots that stay in range.

JSON on stdin:
  {"slots": 8}

slots must be an integer 1–8 (Duo=2, Duo 2=4, Quad 2=8).

Also importable by unit tests. Set NEXVUE_HARDWARE_SKIP_SYSTEMCTL=1 to skip
systemctl (tests / boxes without systemd).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

STATION_ENV = Path(os.environ.get("NEXVUE_STATION_ENV", "/etc/nexvue/nexvue.env"))
CHANNELS_DIR = Path(os.environ.get("NEXVUE_CHANNELS_DIR", "/etc/nexvue/channels"))
SLOT_CEILING = 8  # Quad 2 hard cap (indices 0..7)
SLOT_FLOOR = 1
DEVICES_KEY = "MAX_DEVICES"
CHANNELS_KEY = "MAX_CHANNELS"

ASSIGN_RE = re.compile(r"^(\s*)(#?)(\s*)([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
UNQUOTED_SAFE_RE = re.compile(r"^[A-Za-z0-9_./:+=,-]+$")

MINIMAL_CHANNEL_ENV = "DEVICE_NUMBER=0\nCHANNEL_PATH=ch0\n"


def sanitize_slots(raw) -> int:
    if isinstance(raw, bool):
        raise ValueError("Encode slots must be 1–8 (Duo=2, Duo 2=4, Quad 2=8)")
    if isinstance(raw, float) and raw != int(raw):
        raise ValueError("Encode slots must be 1–8 (Duo=2, Duo 2=4, Quad 2=8)")
    try:
        n = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Encode slots must be 1–8 (Duo=2, Duo 2=4, Quad 2=8)") from exc
    if n < SLOT_FLOOR or n > SLOT_CEILING:
        raise ValueError("Encode slots must be 1–8 (Duo=2, Duo 2=4, Quad 2=8)")
    return n


def format_assignment_value(value: str) -> str:
    if value == "" or UNQUOTED_SAFE_RE.match(value):
        return value
    return f'"{value}"'


def apply_env_patch(text: str, slots: int) -> str:
    """Set MAX_DEVICES and MAX_CHANNELS to the same slot count."""
    pending = {
        DEVICES_KEY: str(slots),
        CHANNELS_KEY: str(slots),
    }
    lines = text.splitlines(keepends=True)
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
        if not any("# --- Card / encode slots" in ln for ln in new_lines):
            if new_lines and new_lines[-1].strip():
                new_lines.append("\n")
            new_lines.append("# --- Card / encode slots (written by Settings) ---\n")
        for key in (DEVICES_KEY, CHANNELS_KEY):
            if key in pending:
                new_lines.append(f"{key}={format_assignment_value(pending[key])}\n")
    return "".join(new_lines)


def strip_channel_max_devices(text: str) -> tuple[str, int]:
    """Remove active MAX_DEVICES= lines from a channel .env. Comments stay."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    removed = 0
    for line in lines:
        m = ASSIGN_RE.match(line.rstrip("\r\n"))
        if m:
            _indent, hashmark, _sp, key, _old = m.groups()
            if key == DEVICES_KEY and not hashmark:
                removed += 1
                continue
        out.append(line)
    return "".join(out), removed


def _channel_example_text() -> str:
    env = os.environ.get("NEXVUE_CHANNELS_EXAMPLE", "").strip()
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.append(Path("/usr/local/share/nexvue/channels-example.env"))
    zero = CHANNELS_DIR / "0.env"
    if zero.is_file():
        candidates.append(zero)
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    return MINIMAL_CHANNEL_ENV


def seed_channel_env(slot_id: int, template: str) -> str:
    text, _removed = strip_channel_max_devices(template)
    pending = {
        "DEVICE_NUMBER": str(slot_id),
        "CHANNEL_PATH": f"ch{slot_id}",
    }
    lines = text.splitlines(keepends=True)
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
        for key in ("DEVICE_NUMBER", "CHANNEL_PATH"):
            if key in pending:
                new_lines.append(f"{key}={format_assignment_value(pending[key])}\n")
    return "".join(new_lines)


def _atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(mode)
    except OSError:
        pass


def apply_units(slots: int) -> dict:
    """enable --now @0..slots-1; disable --now @slots..7."""
    if os.environ.get("NEXVUE_HARDWARE_SKIP_SYSTEMCTL", "").strip() == "1":
        return {"enabled": [], "disabled": [], "errors": [], "skipped": True}
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return {"enabled": [], "disabled": [], "errors": [], "skipped": True}

    enabled: list[int] = []
    disabled: list[int] = []
    errors: list[str] = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, capture_output=True, text=True, timeout=30)

    for i in range(slots):
        unit = f"nexvue-encode@{i}"
        r = run([systemctl, "enable", "--now", unit])
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip() or f"{unit} enable failed"
            errors.append(err)
        else:
            enabled.append(i)
    for i in range(slots, SLOT_CEILING):
        unit = f"nexvue-encode@{i}"
        r = run([systemctl, "disable", "--now", unit])
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            # Already disabled / missing unit is fine.
            if err and "not been found" not in err.lower() and "not-found" not in err.lower():
                errors.append(err)
                continue
        run([systemctl, "reset-failed", unit])
        disabled.append(i)
    return {"enabled": enabled, "disabled": disabled, "errors": errors, "skipped": False}


def apply_channel_files(slots: int) -> tuple[list[int], int]:
    """Seed 0..slots-1 if missing; strip MAX_DEVICES from every existing 0..7."""
    n = sanitize_slots(slots)
    CHANNELS_DIR.mkdir(parents=True, exist_ok=True)
    template = _channel_example_text()
    seeded: list[int] = []
    stripped = 0
    for i in range(n):
        path = CHANNELS_DIR / f"{i}.env"
        if not path.is_file():
            _atomic_write(path, seed_channel_env(i, template))
            seeded.append(i)
    for i in range(SLOT_CEILING):
        path = CHANNELS_DIR / f"{i}.env"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        new_text, n_rm = strip_channel_max_devices(text)
        if n_rm:
            _atomic_write(path, new_text)
            stripped += n_rm
    return seeded, stripped


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
    try:
        slots = sanitize_slots(patch.get("slots", patch.get("max_channels")))
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1

    env_text = STATION_ENV.read_text(encoding="utf-8", errors="replace") if STATION_ENV.is_file() else ""
    new_env = apply_env_patch(env_text, slots)
    _atomic_write(STATION_ENV, new_env)
    seeded, stripped = apply_channel_files(slots)
    units = apply_units(slots)
    print(json.dumps({
        "ok": True,
        "slots": slots,
        "max_devices": slots,
        "max_channels": slots,
        "max_channel_id": slots - 1,
        "seeded": seeded,
        "stripped": stripped,
        "enabled": units["enabled"],
        "disabled": units["disabled"],
        "unit_errors": units["errors"],
        "units_skipped": units["skipped"],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
