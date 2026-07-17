#!/usr/bin/env bash
# nexvue-ops-status.sh — allowlisted systemctl status for NexVUE ops UI.
# Usage: nexvue-ops-status.sh <unit>
# Prints: active|inactive|failed|activating|deactivating|unknown
set -euo pipefail

UNIT="${1:-}"
case "$UNIT" in
  mediamtx|nexvue-status|nexvue-metrics|nexvue-encode@[0-7]) ;;
  *) echo "disallowed unit: $UNIT" >&2; exit 2 ;;
esac

# is-active exits non-zero for inactive/failed — do not trip set -e.
set +e
STATE="$(systemctl is-active "$UNIT" 2>/dev/null)"
RC=$?
set -e
if [ -z "$STATE" ]; then
  STATE="unknown"
fi
echo "$STATE"
exit 0
