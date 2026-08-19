#!/usr/bin/env python3
"""nexvue-status default bind is loopback (DMZ-safe)."""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "nexvue-status-server.py").read_text(encoding="utf-8")


def main() -> int:
    if 'os.environ.get("NEXVUE_STATUS_BIND", "127.0.0.1")' not in SRC:
        raise AssertionError("NEXVUE_STATUS_BIND default 127.0.0.1 missing")
    if re.search(r'^LISTEN_ADDR\s*=\s*\(\s*"0\.0\.0\.0"', SRC, re.M):
        raise AssertionError("LISTEN_ADDR must not hardcode 0.0.0.0")
    if "nexvue-mediamtx-api.php" not in (ROOT / "web-node" / "index.html").read_text(encoding="utf-8"):
        raise AssertionError("index.html must poll nexvue-mediamtx-api.php")
    if "nexvue-mediamtx-api.php" not in (ROOT / "web-node" / "multiview.html").read_text(encoding="utf-8"):
        raise AssertionError("multiview.html must poll nexvue-mediamtx-api.php")
    api_php = (ROOT / "web-node" / "nexvue-mediamtx-api.php").read_text(encoding="utf-8")
    if "/v3/paths/list" not in api_php:
        raise AssertionError("nexvue-mediamtx-api.php must proxy /v3/paths/list")
    if "auth_require_any" not in api_php:
        raise AssertionError("nexvue-mediamtx-api.php must require auth")
    yml = (ROOT / "mediamtx.yml").read_text(encoding="utf-8")
    if not re.search(r"(?m)^apiAddress:\s*127\.0\.0\.1:9997\s*$", yml):
        raise AssertionError("mediamtx.yml apiAddress must be 127.0.0.1:9997")
    print("ok: status bind default loopback + mediamtx-api proxy + apiAddress")
    return 0


if __name__ == "__main__":
    sys.exit(main())
