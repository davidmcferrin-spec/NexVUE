#!/usr/bin/env python3
"""
nexvue-ops-network-write.py — persist public hostname / IP and patch MediaMTX.

Writes NEXVUE_PUBLIC_HOSTNAME and NEXVUE_PUBLIC_IP to /etc/nexvue/nexvue.env
and sets webrtcAdditionalHosts in mediamtx.yml. Restart is the caller's job
(nexvue-ops.php runs nexvue-ops-restart.sh mediamtx after a successful write).

JSON on stdin:
  {"hostname": "nexvue.example.com", "ip": "203.0.113.40"}
Either field may be "" (LAN-only).

Also importable by unit tests.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

STATION_ENV = Path(os.environ.get("NEXVUE_STATION_ENV", "/etc/nexvue/nexvue.env"))
MEDIAMTX_YML = Path(os.environ.get("NEXVUE_MEDIAMTX_YML", "/etc/nexvue/mediamtx.yml"))

HOSTNAME_KEY = "NEXVUE_PUBLIC_HOSTNAME"
IP_KEY = "NEXVUE_PUBLIC_IP"

ASSIGN_RE = re.compile(r"^(\s*)(#?)(\s*)([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
UNQUOTED_SAFE_RE = re.compile(r"^[A-Za-z0-9_./:+=,-]+$")
HOSTNAME_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)


def _load_ice_patch():
    here = Path(__file__).resolve().parent
    candidates = [
        here / "nexvue-mediamtx-ice-patch.py",
        Path("/usr/local/share/nexvue/nexvue-mediamtx-ice-patch.py"),
        Path("/usr/local/bin/nexvue-mediamtx-ice-patch.py"),
    ]
    for path in candidates:
        if path.is_file():
            spec = importlib.util.spec_from_file_location("nexvue_mediamtx_ice_patch", path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError("nexvue-mediamtx-ice-patch.py not found")


def format_assignment_value(value: str) -> str:
    if value == "" or UNQUOTED_SAFE_RE.match(value):
        return value
    return f'"{value}"'


def sanitize_hostname(raw: str) -> str:
    value = raw.strip().lower()
    if value == "":
        return ""
    if "://" in value or "/" in value or ":" in value:
        raise ValueError("Enter a hostname like nexvue.example.com")
    if _looks_ipv4(value):
        raise ValueError("Put IP addresses in Public IP, not hostname")
    if len(value) > 253 or not HOSTNAME_RE.fullmatch(value):
        raise ValueError("Enter a hostname like nexvue.example.com")
    return value


def sanitize_ip(raw: str) -> str:
    value = raw.strip()
    if value == "":
        return ""
    parts = value.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        raise ValueError("Enter an IPv4 address like 203.0.113.40")
    try:
        nums = [int(p) for p in parts]
    except ValueError as exc:
        raise ValueError("Enter an IPv4 address like 203.0.113.40") from exc
    if any(n < 0 or n > 255 for n in nums) or any(len(p) > 1 and p.startswith("0") for p in parts):
        raise ValueError("Enter an IPv4 address like 203.0.113.40")
    if value in ("0.0.0.0", "255.255.255.255"):
        raise ValueError("Enter a reachable IPv4 address")
    if nums[0] == 127:
        raise ValueError("Loopback addresses cannot be used as a public IP")
    if nums[0] == 169 and nums[1] == 254:
        raise ValueError("Link-local addresses cannot be used as a public IP")
    if nums[0] >= 224:
        raise ValueError("Multicast addresses cannot be used as a public IP")
    return value


def _looks_ipv4(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def apply_env_patch(text: str, hostname: str, ip: str) -> str:
    """Update or append the two public-reachability keys."""
    pending = {
        HOSTNAME_KEY: hostname,
        IP_KEY: ip,
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
        if not any("# --- Public reachability" in ln for ln in new_lines):
            if new_lines and new_lines[-1].strip():
                new_lines.append("\n")
            new_lines.append("# --- Public reachability (written by Settings) ---\n")
        # Stable order: hostname then IP.
        for key in (HOSTNAME_KEY, IP_KEY):
            if key in pending:
                new_lines.append(f"{key}={format_assignment_value(pending[key])}\n")
    return "".join(new_lines)


def apply(hostname: str, ip: str, env_text: str, yml_text: str) -> tuple[str, str]:
    hn = sanitize_hostname(hostname)
    addr = sanitize_ip(ip)
    ice = _load_ice_patch()
    new_env = apply_env_patch(env_text, hn, addr)
    new_yml = ice.patch(yml_text, hn, addr)
    new_yml = re.sub(r"\n{3,}", "\n\n", new_yml)
    return new_env, new_yml


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o644)
    except OSError:
        pass


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
    hostname = str(patch.get("hostname", ""))
    ip = str(patch.get("ip", ""))
    env_text = STATION_ENV.read_text(encoding="utf-8", errors="replace") if STATION_ENV.is_file() else ""
    if not MEDIAMTX_YML.is_file():
        print(json.dumps({"ok": False, "error": "MediaMTX config is missing"}), file=sys.stderr)
        return 1
    yml_text = MEDIAMTX_YML.read_text(encoding="utf-8", errors="replace")
    try:
        new_env, new_yml = apply(hostname, ip, env_text, yml_text)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    _atomic_write(STATION_ENV, new_env)
    _atomic_write(MEDIAMTX_YML, new_yml)
    hn = sanitize_hostname(hostname)
    addr = sanitize_ip(ip)
    print(json.dumps({"ok": True, "hostname": hn, "ip": addr}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
