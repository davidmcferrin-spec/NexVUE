#!/usr/bin/env bash
# nexvue-ops-journal.sh — allowlisted journalctl for NexVUE ops UI.
#
# Read:
#   nexvue-ops-journal.sh <unit> [lines] [since]
#     lines  default 100, max 500
#     since  optional journalctl --since value (e.g. ISO timestamp)
#     A per-unit clear watermark (see below) floors --since so cleared
#     history stays hidden in the Services UI.
#
# Clear (selected unit only — not host-wide vacuum):
#   nexvue-ops-journal.sh clear <unit>
#     Records a watermark at "now" under journal-cleared/. Subsequent
#     reads for that unit only return lines at/after the watermark.
#     systemd cannot purge one unit from the binary journal; this is the
#     allowlisted per-service clear for the ops UI.
#
# Optional test override: NEXVUE_JOURNAL_CLEARED_DIR
set -euo pipefail

CLEARED_DIR="${NEXVUE_JOURNAL_CLEARED_DIR:-/var/lib/nexvue/journal-cleared}"

unit_allowed() {
  case "$1" in
    mediamtx|nexvue-status|nexvue-metrics|nexvue-encode@[0-9]) return 0 ;;
    *) return 1 ;;
  esac
}

# Safe filename for unit (encode@N → encode@N; no path separators).
cleared_path() {
  local u="$1"
  printf '%s/%s' "$CLEARED_DIR" "${u//\//_}"
}

read_cleared_since() {
  local f
  f="$(cleared_path "$1")"
  if [ -f "$f" ]; then
    tr -d '\n' <"$f"
  fi
}

# Pick the later of two journalctl --since values (ISO or relative).
# Empty args are ignored. Falls back to the non-empty one; if both set,
# compare via date -d (GNU date on Ubuntu).
effective_since() {
  local a="${1:-}" b="${2:-}"
  if [ -z "$a" ]; then printf '%s' "$b"; return; fi
  if [ -z "$b" ]; then printf '%s' "$a"; return; fi
  local ea eb
  ea=$(date -d "$a" +%s 2>/dev/null || echo 0)
  eb=$(date -d "$b" +%s 2>/dev/null || echo 0)
  if [ "$ea" -ge "$eb" ]; then
    printf '%s' "$a"
  else
    printf '%s' "$b"
  fi
}

if [ "${1:-}" = "clear" ]; then
  UNIT="${2:-}"
  if ! unit_allowed "$UNIT"; then
    echo "disallowed unit: $UNIT" >&2
    exit 2
  fi
  install -d -m 755 "$CLEARED_DIR"
  # short-iso-compatible stamp (matches journal -o short-iso).
  TS="$(date +%Y-%m-%dT%H:%M:%S%z)"
  printf '%s\n' "$TS" >"$(cleared_path "$UNIT")"
  echo "cleared $UNIT since $TS"
  exit 0
fi

# Host-wide vacuum removed — use clear <unit> for per-service wipe.
if [ "${1:-}" = "vacuum" ]; then
  echo "vacuum is disabled; use: $0 clear <unit>" >&2
  exit 2
fi

UNIT="${1:-}"
LINES="${2:-100}"
SINCE="${3:-}"

if ! unit_allowed "$UNIT"; then
  echo "disallowed unit: $UNIT" >&2
  exit 2
fi

# Digits only for line count.
[[ "$LINES" =~ ^[0-9]+$ ]] || { echo "lines must be an integer" >&2; exit 2; }
if [ "$LINES" -gt 500 ]; then LINES=500; fi
if [ "$LINES" -lt 1 ]; then LINES=1; fi

# Reject shell metacharacters in --since (ISO / relative english only).
if [ -n "$SINCE" ]; then
  if [[ "$SINCE" =~ [\$\`\;\|\&\<\>] ]]; then
    echo "disallowed characters in since" >&2
    exit 2
  fi
fi

CLEARED="$(read_cleared_since "$UNIT")"
USE_SINCE="$(effective_since "$SINCE" "$CLEARED")"

if [ -n "$USE_SINCE" ]; then
  exec journalctl -u "$UNIT" -n "$LINES" --no-pager -o short-iso --since "$USE_SINCE"
else
  exec journalctl -u "$UNIT" -n "$LINES" --no-pager -o short-iso
fi
