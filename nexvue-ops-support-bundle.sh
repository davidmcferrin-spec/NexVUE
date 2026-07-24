#!/usr/bin/env bash
# Allowlisted support-bundle builder for NexVUE Services UI.
# Usage: nexvue-ops-support-bundle.sh <hours> [requestor_ip]
#
# hours: 1|6|12|24|48|72
# Prints the absolute path to the zip on stdout (single line).
set -euo pipefail

HOURS="${1:-}"
REQUESTOR_IP="${2:-}"
PY="${NEXVUE_SUPPORT_BUNDLE:-/usr/local/bin/nexvue-support-bundle.py}"

case "$HOURS" in
  1|6|12|24|48|72) ;;
  *)
    echo "hours must be one of: 1 6 12 24 48 72" >&2
    exit 2
    ;;
esac

if [[ -n "$REQUESTOR_IP" ]]; then
  if [[ "$REQUESTOR_IP" =~ [\$\`\;\|\&\<\>\ \'\"\\] ]]; then
    echo "disallowed characters in requestor_ip" >&2
    exit 2
  fi
  if [[ ${#REQUESTOR_IP} -gt 64 ]]; then
    echo "requestor_ip too long" >&2
    exit 2
  fi
fi

if [[ ! -f "$PY" ]]; then
  echo "missing collector: $PY" >&2
  exit 2
fi

DATA="${NEXVUE_DATA:-/var/lib/nexvue}"
mkdir -p "$DATA/support"
chmod 750 "$DATA/support" 2>/dev/null || true

export NEXVUE_DATA="$DATA"
export NEXVUE_ETC="${NEXVUE_ETC:-/etc/nexvue}"
export NEXVUE_RUN_DIR="${NEXVUE_RUN_DIR:-/run/nexvue}"
export NEXVUE_METRICS_DB="${NEXVUE_METRICS_DB:-$DATA/metrics.db}"

ARGS=(--hours "$HOURS")
if [[ -n "$REQUESTOR_IP" ]]; then
  ARGS+=(--requestor-ip "$REQUESTOR_IP")
fi

exec /usr/bin/python3 "$PY" "${ARGS[@]}"
