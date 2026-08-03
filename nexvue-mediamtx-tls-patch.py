#!/usr/bin/env python3
"""
nexvue-mediamtx-tls-patch.py — point MediaMTX WHEP/API TLS at /etc/nexvue/tls.

Stdlib only. Idempotent: sets (or uncomments) encryption flags and cert paths.
Does not rewrite unrelated config.

Usage:
  python3 nexvue-mediamtx-tls-patch.py /etc/nexvue/mediamtx.yml
  python3 nexvue-mediamtx-tls-patch.py /path/to.yml --check
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TLS_PAIRS = (
    ("apiEncryption", "yes"),
    ("apiServerKey", "/etc/nexvue/tls/privkey.pem"),
    ("apiServerCert", "/etc/nexvue/tls/fullchain.pem"),
    ("webrtcEncryption", "yes"),
    ("webrtcServerKey", "/etc/nexvue/tls/privkey.pem"),
    ("webrtcServerCert", "/etc/nexvue/tls/fullchain.pem"),
)


def patch_tls_paths(text: str) -> str:
    for key, val in TLS_PAIRS:
        pat = re.compile(rf"^(\s*)#?\s*{re.escape(key)}\s*:.*$", re.M)
        if pat.search(text):
            text = pat.sub(rf"\1{key}: {val}", text, count=1)
        else:
            text = text.rstrip() + f"\n{key}: {val}\n"
    return text


def tls_paths_ok(text: str) -> bool:
    need = {
        "webrtcServerKey": "/etc/nexvue/tls/privkey.pem",
        "webrtcServerCert": "/etc/nexvue/tls/fullchain.pem",
        "apiServerKey": "/etc/nexvue/tls/privkey.pem",
        "apiServerCert": "/etc/nexvue/tls/fullchain.pem",
    }
    for key, val in need.items():
        if not re.search(
            rf"(?m)^\s*{re.escape(key)}\s*:\s*{re.escape(val)}\s*$",
            text,
        ):
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("yml", type=Path, help="path to mediamtx.yml")
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit 0 if TLS paths already correct; do not write",
    )
    args = ap.parse_args()
    if not args.yml.is_file():
        print(f"error: missing {args.yml}", file=sys.stderr)
        return 2
    text = args.yml.read_text(encoding="utf-8")
    if args.check:
        return 0 if tls_paths_ok(text) else 1
    new = patch_tls_paths(text)
    if new != text:
        args.yml.write_text(new, encoding="utf-8")
        print("patched")
    else:
        print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
