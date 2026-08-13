#!/usr/bin/env python3
"""
nexvue-apache-http-off.py — comment Listen 80 in Apache ports.conf.

Stdlib only. Idempotent. Does not touch Listen 443, Listen 8080, or
Listen 127.0.0.1:9080 (JWKS loopback).

Usage:
  python3 nexvue-apache-http-off.py /etc/apache2/ports.conf
  python3 nexvue-apache-http-off.py /path/to/ports.conf --check
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Bare 80 or addr:80 — not 8080 / 8000 / 9080.
LISTEN_80 = re.compile(
    r"^\s*Listen\s+"
    r"(?:"
    r"(?:\*|0\.0\.0\.0|127\.0\.0\.1|\[::\]):80"
    r"|80"
    r")"
    r"(?:\s*(?:#.*)?)?$"
)

COMMENT_NOTE = "  # NexVUE: HTTP closed; UI is HTTPS :443 only"


def comment_listen_80(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#"):
            out.append(line)
            continue
        if LISTEN_80.match(line):
            indent = line[: len(line) - len(stripped)]
            out.append(f"{indent}# {stripped}{COMMENT_NOTE}")
        else:
            out.append(line)
    ended_nl = text.endswith("\n") if text else True
    joined = "\n".join(out)
    if ended_nl:
        joined += "\n"
    return joined


def http_listen_is_off(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if LISTEN_80.match(line):
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("conf", type=Path, help="path to ports.conf")
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit 0 if no uncommented Listen 80; do not write",
    )
    args = ap.parse_args()
    if not args.conf.is_file():
        print(f"error: missing {args.conf}", file=sys.stderr)
        return 2
    text = args.conf.read_text(encoding="utf-8")
    if args.check:
        return 0 if http_listen_is_off(text) else 1
    new = comment_listen_80(text)
    if new != text:
        args.conf.write_text(new, encoding="utf-8")
        print("patched")
    else:
        print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
