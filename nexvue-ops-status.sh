#!/usr/bin/env bash
# nexvue-ops-status.sh — allowlisted systemctl status for NexVUE ops UI.
# Usage: nexvue-ops-status.sh <unit>
# Prints two space-separated tokens:
#   <active-state> <enabled-state>
# active-state:  active|inactive|failed|activating|deactivating|unknown
# enabled-state: enabled|disabled|static|masked|unknown (is-enabled vocabulary)
set -euo pipefail

UNIT="${1:-}"
case "$UNIT" in
  mediamtx|nexvue-status|nexvue-metrics|nexvue-encode@[0-9]) ;;
  *) echo "disallowed unit: $UNIT" >&2; exit 2 ;;
esac

# is-active / is-enabled exit non-zero for inactive/failed/disabled — do not
# trip set -e.
set +e
STATE="$(systemctl is-active "$UNIT" 2>/dev/null)"
ENABLED="$(systemctl is-enabled "$UNIT" 2>/dev/null)"
set -e
if [ -z "$STATE" ]; then
  STATE="unknown"
fi
if [ -z "$ENABLED" ]; then
  ENABLED="unknown"
fi
echo "$STATE $ENABLED"
exit 0
