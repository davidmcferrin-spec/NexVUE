#!/usr/bin/env python3
"""
nexvue-mediamtx-ice-patch.py — set MediaMTX webrtcAdditionalHosts.

Stdlib only. Idempotent: replaces webrtcAdditionalHosts (flow or block
list) and removes the deprecated webrtcICEHostNAT1To1IPs alias so the two
cannot drift. Does not rewrite unrelated config.

Usage:
  python3 nexvue-mediamtx-ice-patch.py /etc/nexvue/mediamtx.yml
  python3 nexvue-mediamtx-ice-patch.py /etc/nexvue/mediamtx.yml \\
      --hostname nexvue.example.com --ip 203.0.113.40
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HOSTS_KEY = "webrtcAdditionalHosts"
LEGACY_KEY = "webrtcICEHostNAT1To1IPs"

# Top-level YAML key, flow list or block list of "- item" lines.
_KEY_BLOCK_RE = re.compile(
    r"(?m)^(?P<key>webrtcAdditionalHosts|webrtcICEHostNAT1To1IPs):"
    r"(?:[ \t]*\[.*?\][ \t]*)?(?:\n[ \t]+-[ \t]*.+)*[ \t]*\n?"
)


def _split_tokens(inner: str) -> list[str]:
    tokens: list[str] = []
    for raw in re.split(r"\s*,\s*|\s+", inner.strip()):
        tok = raw.strip().strip("\"'")
        if tok:
            tokens.append(tok)
    return tokens


def parse_additional_hosts(text: str) -> list[str]:
    """Return host/IP tokens from the first active webrtcAdditionalHosts."""
    m = re.search(
        r"(?m)^webrtcAdditionalHosts:[ \t]*\[(.*?)\][ \t]*$",
        text,
    )
    if m:
        return _split_tokens(m.group(1))
    m = re.search(
        r"(?ms)^webrtcAdditionalHosts:[ \t]*\n((?:[ \t]+-[ \t]*.+\n?)*)",
        text,
    )
    if m:
        found: list[str] = []
        for line in m.group(1).splitlines():
            lm = re.match(r"^[ \t]+-[ \t]*(.+)$", line)
            if lm:
                tok = lm.group(1).strip().strip("\"'")
                if tok:
                    found.append(tok)
        return found
    return []


def classify_hosts(tokens: list[str]) -> dict[str, str]:
    """First IPv4 → ip; first non-IP → hostname. Extra tokens ignored."""
    hostname = ""
    ip = ""
    for tok in tokens:
        if _looks_ipv4(tok):
            if not ip:
                ip = tok
        elif not hostname:
            hostname = tok.lower()
    return {"hostname": hostname, "ip": ip}


def _looks_ipv4(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def format_hosts_line(hostname: str, ip: str) -> str:
    hosts: list[str] = []
    hn = hostname.strip()
    addr = ip.strip()
    if hn:
        hosts.append(hn)
    if addr:
        hosts.append(addr)
    if not hosts:
        return f"{HOSTS_KEY}: []"
    parts: list[str] = []
    for h in hosts:
        if re.fullmatch(r"[A-Za-z0-9._-]+", h):
            parts.append(h)
        else:
            parts.append(json.dumps(h))
    return f"{HOSTS_KEY}: [{', '.join(parts)}]"


def _strip_ice_keys(text: str) -> str:
    return _KEY_BLOCK_RE.sub("", text)


def patch(text: str, hostname: str = "", ip: str = "") -> str:
    """Set webrtcAdditionalHosts from hostname + IP; drop the legacy key."""
    line = format_hosts_line(hostname, ip)
    stripped = _strip_ice_keys(text)
    insert = line + "\n"
    m = re.search(r"(?m)^webrtcIPsFromInterfaces:[ \t]*.*$", stripped)
    if m:
        end = m.end()
        # Keep a single newline after the interfaces line.
        if end < len(stripped) and stripped[end] == "\n":
            end += 1
        return stripped[:end] + insert + stripped[end:]
    for anchor in (r"(?m)^# --- Authentication", r"(?m)^authMethod:", r"(?m)^paths:"):
        am = re.search(anchor, stripped)
        if am:
            return stripped[: am.start()] + insert + stripped[am.start() :]
    if stripped and not stripped.endswith("\n"):
        stripped += "\n"
    return stripped + insert


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("yml", type=Path)
    ap.add_argument("--hostname", default="", help="Public DNS name (optional)")
    ap.add_argument("--ip", default="", help="Public / NAT IPv4 (optional)")
    args = ap.parse_args()
    if not args.yml.is_file():
        print(f"missing {args.yml}", file=sys.stderr)
        return 1
    raw = args.yml.read_text(encoding="utf-8")
    new = patch(raw, args.hostname.strip(), args.ip.strip())
    new = re.sub(r"\n{3,}", "\n\n", new)
    if new != raw:
        args.yml.write_text(new, encoding="utf-8")
        print("updated", args.yml)
    else:
        print("unchanged", args.yml)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
