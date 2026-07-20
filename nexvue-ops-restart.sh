#!/usr/bin/env bash
# nexvue-ops-restart.sh — allowlisted systemctl restart for NexVUE ops UI.
# Usage: nexvue-ops-restart.sh <unit> [<unit> ...]
# Units: mediamtx | nexvue-status | nexvue-metrics | nexvue-encode@[0-9]
set -euo pipefail

[ "$#" -ge 1 ] || { echo "usage: nexvue-ops-restart.sh <unit>..." >&2; exit 2; }

for UNIT in "$@"; do
  case "$UNIT" in
    mediamtx|nexvue-status|nexvue-metrics|nexvue-encode@[0-9]) ;;
    *) echo "disallowed unit: $UNIT" >&2; exit 2 ;;
  esac
done

exec systemctl restart "$@"
