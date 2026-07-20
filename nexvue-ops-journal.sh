#!/usr/bin/env bash
# nexvue-ops-journal.sh — allowlisted journalctl for NexVUE ops UI.
# Usage: nexvue-ops-journal.sh <unit> [lines] [since]
#   lines  default 100, max 500
#   since  optional journalctl --since value (e.g. ISO timestamp)
set -euo pipefail

UNIT="${1:-}"
LINES="${2:-100}"
SINCE="${3:-}"

case "$UNIT" in
  mediamtx|nexvue-status|nexvue-metrics|nexvue-encode@[0-9]) ;;
  *) echo "disallowed unit: $UNIT" >&2; exit 2 ;;
esac

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
  exec journalctl -u "$UNIT" -n "$LINES" --no-pager -o short-iso --since "$SINCE"
else
  exec journalctl -u "$UNIT" -n "$LINES" --no-pager -o short-iso
fi
