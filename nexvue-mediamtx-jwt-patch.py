#!/usr/bin/env python3
"""
nexvue-mediamtx-jwt-patch.py — set MediaMTX JWT auth keys in mediamtx.yml.

Stdlib only. Idempotent: updates existing keys or inserts a JWT block before
the first top-level "paths:" (or appends). Does not rewrite unrelated config.

Usage:
  python3 nexvue-mediamtx-jwt-patch.py /etc/nexvue/mediamtx.yml \\
      --jwks http://127.0.0.1:9080/nexvue-jwks.php
  python3 nexvue-mediamtx-jwt-patch.py /etc/nexvue/mediamtx.yml \\
      --jwks https://127.0.0.1/nexvue-jwks.php --fingerprint AABBCC...
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


JWT_KEYS = (
    "authMethod",
    "authJWTJWKS",
    "authJWTJWKSFingerprint",
    "authJWTClaimKey",
    "authJWTInHTTPQuery",
)


def upsert_scalar(text: str, key: str, value: str | None) -> str:
    """Set key: value at top level, or remove the line if value is None."""
    pattern = re.compile(
        rf"(?m)^([ \t]*){re.escape(key)}:[ \t]*.*$"
    )
    if value is None:
        return pattern.sub("", text)

    line = f"{key}: {value}"
    if pattern.search(text):
        return pattern.sub(line, text, count=1)

    # Insert before paths: or authJWTExclude or at end.
    for anchor in (r"(?m)^(paths:)", r"(?m)^(authJWTExclude:)", r"(?m)^(pathDefaults:)"):
        m = re.search(anchor, text)
        if m:
            return text[: m.start()] + line + "\n" + text[m.start() :]
    return text.rstrip() + "\n" + line + "\n"


def ensure_api_exclude(text: str) -> str:
    """Ensure authJWTExclude contains action: api (Control API LAN-open)."""
    if re.search(r"(?m)^authJWTExclude:", text):
        # Already present — if it mentions api, leave alone; else append entry.
        block_m = re.search(
            r"(?ms)^(authJWTExclude:\s*\n(?:[ \t]+-[ \t]*action:.*\n(?:[ \t]+path:.*\n)?)*)",
            text,
        )
        if block_m and re.search(r"action:\s*api\b", block_m.group(1)):
            return text
        # Simple append under existing list if we can find the header.
        return re.sub(
            r"(?m)^(authJWTExclude:\s*)$",
            r"\1\n  - action: api",
            text,
            count=1,
        )
    insert = "authJWTExclude:\n  - action: api\n"
    m = re.search(r"(?m)^paths:", text)
    if m:
        return text[: m.start()] + insert + text[m.start() :]
    return text.rstrip() + "\n" + insert


def patch(text: str, jwks: str, fingerprint: str | None) -> str:
    text = upsert_scalar(text, "authMethod", "jwt")
    text = upsert_scalar(text, "authJWTJWKS", jwks)
    text = upsert_scalar(text, "authJWTClaimKey", "mediamtx_permissions")
    text = upsert_scalar(text, "authJWTInHTTPQuery", "yes")
    if fingerprint:
        text = upsert_scalar(text, "authJWTJWKSFingerprint", fingerprint)
    else:
        # Clear stale fingerprint when switching to plain HTTP JWKS.
        if jwks.startswith("http://"):
            text = upsert_scalar(text, "authJWTJWKSFingerprint", None)
    text = ensure_api_exclude(text)
    # Collapse accidental blank runs from deletions.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("yml", type=Path)
    ap.add_argument("--jwks", required=True, help="authJWTJWKS URL")
    ap.add_argument("--fingerprint", default="", help="SHA-256 hex for HTTPS JWKS")
    args = ap.parse_args()
    if not args.yml.is_file():
        print(f"missing {args.yml}", file=sys.stderr)
        return 1
    raw = args.yml.read_text(encoding="utf-8")
    fp = args.fingerprint.strip() or None
    new = patch(raw, args.jwks.strip(), fp)
    if new != raw:
        args.yml.write_text(new, encoding="utf-8")
        print("updated", args.yml)
    else:
        print("unchanged", args.yml)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
