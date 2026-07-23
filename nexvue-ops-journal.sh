#!/usr/bin/env bash
# nexvue-ops-journal.sh — allowlisted journalctl for NexVUE ops UI.
#
# Read:
#   nexvue-ops-journal.sh <unit> [lines] [since]
#     lines  default 100, max 500
#     since  optional journalctl --since value (ISO from UI follow cursor)
#     A per-unit clear watermark (see below) floors --since so cleared
#     history stays hidden in the Services UI.
#
# Clear (selected unit only — not host-wide vacuum):
#   nexvue-ops-journal.sh clear <unit>
#     Records a unix-epoch watermark under journal-cleared/. Subsequent
#     reads for that unit use journalctl --since @EPOCH. systemd cannot
#     purge one unit from the binary journal; this is the allowlisted
#     per-service clear for the ops UI.
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

# Convert ISO / @epoch / bare epoch → unix seconds. Empty/unparseable → empty.
to_epoch() {
  local s="${1:-}"
  [ -z "$s" ] && return 0
  if [[ "$s" =~ ^@[0-9]+$ ]]; then
    printf '%s' "${s:1}"
    return 0
  fi
  if [[ "$s" =~ ^[0-9]+$ ]]; then
    printf '%s' "$s"
    return 0
  fi
  # Normalize short-iso: T→space, -0400→-04:00 (GNU date + journalctl friendly).
  local n="$s"
  n="${n//T/ }"
  if [[ "$n" =~ ^(.*[+-][0-9]{2})([0-9]{2})$ ]]; then
    n="${BASH_REMATCH[1]}:${BASH_REMATCH[2]}"
  fi
  date -d "$n" +%s 2>/dev/null || true
}

read_cleared_epoch() {
  local f raw
  f="$(cleared_path "$1")"
  [ -f "$f" ] || return 0
  raw="$(tr -d '[:space:]' <"$f")"
  to_epoch "$raw"
}

# Later of two epoch strings (empty ignored).
max_epoch() {
  local a="${1:-}" b="${2:-}"
  if [ -z "$a" ]; then printf '%s' "$b"; return; fi
  if [ -z "$b" ]; then printf '%s' "$a"; return; fi
  if [ "$a" -ge "$b" ]; then
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
  # Epoch only — journalctl --since @N always parses; ISO±HHMM often does not.
  TS="$(date +%s)"
  printf '%s\n' "$TS" >"$(cleared_path "$UNIT")"
  echo "cleared $UNIT since @$TS"
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

SINCE_E="$(to_epoch "$SINCE")"
# Relative phrases (e.g. "1 hour ago") are not used by the UI; if to_epoch
# failed but SINCE is set, pass it through only when it looks safe/relative.
CLEARED_E="$(read_cleared_epoch "$UNIT")"
USE_E="$(max_epoch "$SINCE_E" "$CLEARED_E")"

if [ -n "$USE_E" ]; then
  exec journalctl -u "$UNIT" -n "$LINES" --no-pager -o short-iso --since "@${USE_E}"
elif [ -n "$SINCE" ]; then
  # Unparsed follow cursor — last resort (should be rare).
  exec journalctl -u "$UNIT" -n "$LINES" --no-pager -o short-iso --since "$SINCE"
else
  exec journalctl -u "$UNIT" -n "$LINES" --no-pager -o short-iso
fi
