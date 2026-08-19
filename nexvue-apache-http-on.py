#!/usr/bin/env python3
"""
nexvue-apache-http-on.py — ensure Listen 80 is uncommented in Apache
ports.conf.

UI is HTTPS-only, but Apache still listens on :80 specifically to 301
redirect stray HTTP hits to the same URL under https:// (nexvue-web-router.php
/ nexvue-portal-web-router.php's *_https_redirect_target() does the actual
redirect once Apache hands it the request — this script only makes sure
Apache is listening at all). Stdlib only. Idempotent. Does not touch
Listen 443, Listen 8080, or Listen 127.0.0.1:9080 (JWKS loopback, which
must stay plain HTTP forever — see mediamtx.yml's authJWTJWKS comment).

Historical note: earlier NexVUE releases shipped nexvue-apache-http-off.py,
which closed :80 entirely. That produced dead-end WHEP failures for anyone
who reached the UI via a stale http:// bookmark or typed URL — the browser
inherited http: for the WHEP fetch too, and MediaMTX's :8889 listener is
TLS-only, so the connection just reset with no explanation. This script
(and the two *_https_redirect_target() functions) exist to catch that
class of mistake at the front door instead.

Usage:
  python3 nexvue-apache-http-on.py /etc/apache2/ports.conf
  python3 nexvue-apache-http-on.py /path/to/ports.conf --check
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# A previously-commented "# Listen 80  # NexVUE: HTTP closed..." line (or a
# hand-commented one) — bare 80 or addr:80, not 8080 / 8000 / 9080.
COMMENTED_LISTEN_80 = re.compile(
    r"^(?P<indent>\s*)#\s*Listen\s+"
    r"(?P<target>(?:\*|0\.0\.0\.0|127\.0\.0\.1|\[::\]):80|80)"
    r"(?:\s*#.*)?\s*$"
)
LISTEN_80 = re.compile(
    r"^\s*Listen\s+"
    r"(?:"
    r"(?:\*|0\.0\.0\.0|127\.0\.0\.1|\[::\]):80"
    r"|80"
    r")"
    r"(?:\s*(?:#.*)?)?$"
)


def uncomment_listen_80(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    already_active = any(
        LISTEN_80.match(line) for line in lines if not line.lstrip().startswith("#")
    )
    for line in lines:
        m = COMMENTED_LISTEN_80.match(line)
        if m and not already_active:
            out.append(f"{m.group('indent')}Listen {m.group('target')}")
        else:
            out.append(line)
    ended_nl = text.endswith("\n") if text else True
    joined = "\n".join(out)
    if ended_nl:
        joined += "\n"
    # Nothing to uncomment and Listen 80 was never present at all (unusual
    # ports.conf) — append a plain Listen 80 rather than silently no-op.
    if not http_listen_is_on(joined):
        if joined and not joined.endswith("\n"):
            joined += "\n"
        joined += "Listen 80\n"
    return joined


def http_listen_is_on(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if LISTEN_80.match(line):
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("conf", type=Path, help="path to ports.conf")
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit 0 if Listen 80 is active; do not write",
    )
    args = ap.parse_args()
    if not args.conf.is_file():
        print(f"error: missing {args.conf}", file=sys.stderr)
        return 2
    text = args.conf.read_text(encoding="utf-8")
    if args.check:
        return 0 if http_listen_is_on(text) else 1
    new = uncomment_listen_80(text)
    if new != text:
        args.conf.write_text(new, encoding="utf-8")
        print("patched")
    else:
        print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
