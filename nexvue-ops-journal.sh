#!/usr/bin/env bash
# nexvue-ops-journal.sh — allowlisted journalctl for NexVUE ops UI.
#
# Read:
#   nexvue-ops-journal.sh <unit> [lines] [since]
#     lines  default 100, max 500
#     since  optional journalctl --since value (e.g. ISO timestamp)
#
# Vacuum (host journal — not per-unit; systemd vacuum is system-wide):
#   nexvue-ops-journal.sh vacuum time <1d|3d|7d|14d|30d>
#   nexvue-ops-journal.sh vacuum size <50M|100M|200M|500M|1G>
# Rotates first so the active file can be archived, then vacuums.
set -euo pipefail

if [ "${1:-}" = "vacuum" ]; then
  MODE="${2:-}"
  VALUE="${3:-}"
  case "$MODE" in
    time)
      case "$VALUE" in
        1d|3d|7d|14d|30d) ;;
        *) echo "vacuum time must be one of: 1d 3d 7d 14d 30d" >&2; exit 2 ;;
      esac
      journalctl --rotate
      exec journalctl --vacuum-time="$VALUE"
      ;;
    size)
      case "$VALUE" in
        50M|100M|200M|500M|1G) ;;
        *) echo "vacuum size must be one of: 50M 100M 200M 500M 1G" >&2; exit 2 ;;
      esac
      journalctl --rotate
      exec journalctl --vacuum-size="$VALUE"
      ;;
    *)
      echo "usage: $0 vacuum time|size <value>" >&2
      exit 2
      ;;
  esac
fi

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
