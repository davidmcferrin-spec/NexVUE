#!/usr/bin/env bash
# test/test-ops-journal-clear.sh — per-unit journal clear watermark (no sudo).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="${ROOT}/nexvue-ops-journal.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export NEXVUE_JOURNAL_CLEARED_DIR="$TMP/cleared"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "OK: $*"; }

chmod +x "$SCRIPT" 2>/dev/null || true

# Clear requires allowlisted unit.
if "$SCRIPT" clear bogus-unit 2>/dev/null; then
  fail "clear accepted bogus unit"
fi
pass "clear rejects bogus unit"

out="$("$SCRIPT" clear nexvue-encode@2)"
[[ "$out" == cleared\ nexvue-encode@2\ since\ * ]] || fail "unexpected clear output: $out"
f="$TMP/cleared/nexvue-encode@2"
[ -f "$f" ] || fail "watermark file missing"
ts="$(tr -d '\n' <"$f")"
[[ "$ts" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T ]] || fail "bad timestamp: $ts"
pass "clear writes watermark for encode@2"

"$SCRIPT" clear mediamtx >/dev/null
[ -f "$TMP/cleared/mediamtx" ] || fail "mediamtx watermark missing"
pass "clear writes watermark for mediamtx"

# Host-wide vacuum must be rejected.
if "$SCRIPT" vacuum time 7d 2>/dev/null; then
  fail "vacuum still accepted"
fi
pass "vacuum disabled"

# Disallowed read unit.
if "$SCRIPT" sshd 10 2>/dev/null; then
  fail "read accepted sshd"
fi
pass "read rejects disallowed unit"

echo "All journal-clear tests passed."
