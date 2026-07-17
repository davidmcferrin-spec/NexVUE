#!/usr/bin/env python3
"""Sanity: nexvue-status STALE_AFTER_S must exceed helper timeout."""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "nexvue-status-server.py").read_text(encoding="utf-8")


def literal(name: str) -> float:
    m = re.search(rf"^{name}\s*=\s*([0-9.]+)\s*$", SRC, re.M)
    if not m:
        raise AssertionError(f"literal {name} not found")
    return float(m.group(1))


def main() -> int:
    poll = literal("POLL_INTERVAL_S")
    helper = literal("HELPER_TIMEOUT_S")
    m = re.search(r"^STALE_AFTER_S\s*=\s*(.+)$", SRC, re.M)
    if not m:
        raise AssertionError("STALE_AFTER_S not found")
    expr = m.group(1).strip()
    if re.fullmatch(r"[0-9.]+", expr):
        stale = float(expr)
    else:
        # Expect HELPER_TIMEOUT_S + POLL_INTERVAL_S (order may vary)
        parts = [p.strip() for p in expr.split("+")]
        stale = 0.0
        for p in parts:
            if p == "HELPER_TIMEOUT_S":
                stale += helper
            elif p == "POLL_INTERVAL_S":
                stale += poll
            elif re.fullmatch(r"[0-9.]+", p):
                stale += float(p)
            else:
                raise AssertionError(f"cannot evaluate STALE_AFTER_S = {expr}")
    assert stale > helper, f"STALE_AFTER_S ({stale}) must be > HELPER_TIMEOUT_S ({helper})"
    assert stale >= helper + poll, (
        f"STALE_AFTER_S ({stale}) should be >= HELPER_TIMEOUT_S+POLL_INTERVAL_S "
        f"({helper + poll})"
    )
    print(f"ok: STALE_AFTER_S={stale} HELPER_TIMEOUT_S={helper} POLL_INTERVAL_S={poll}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
