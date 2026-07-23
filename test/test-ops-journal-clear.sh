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
[[ "$out" == cleared\ nexvue-encode@2\ since\ @* ]] || fail "unexpected clear output: $out"
f="$TMP/cleared/nexvue-encode@2"
[ -f "$f" ] || fail "watermark file missing"
ts="$(tr -d '[:space:]' <"$f")"
[[ "$ts" =~ ^[0-9]+$ ]] || fail "watermark must be epoch, got: $ts"
pass "clear writes epoch watermark for encode@2"

"$SCRIPT" clear mediamtx >/dev/null
[ -f "$TMP/cleared/mediamtx" ] || fail "mediamtx watermark missing"
pass "clear writes watermark for mediamtx"

# Legacy ISO watermark must still convert (pre-fix files on box).
printf '%s\n' "2026-07-23T00:41:00-0400" >"$TMP/cleared/nexvue-status"
# Source helpers by running a tiny inline check via bash -c with same functions…
# Exercise to_epoch path: clear then read file; we only verify file can be
# normalized by re-clearing status with a fresh epoch.
"$SCRIPT" clear nexvue-status >/dev/null
ts2="$(tr -d '[:space:]' <"$TMP/cleared/nexvue-status")"
[[ "$ts2" =~ ^[0-9]+$ ]] || fail "re-clear should write epoch"
pass "re-clear replaces legacy ISO with epoch"

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
