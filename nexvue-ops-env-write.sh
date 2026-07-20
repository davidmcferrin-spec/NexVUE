#!/usr/bin/env bash
# nexvue-ops-env-write.sh — write a JSON patch to one channel env file.
# Usage: nexvue-ops-env-write.sh <N>   # JSON patch on stdin
set -euo pipefail

N="${1:-}"
[[ "$N" =~ ^[0-9]$ ]] || { echo "channel id must be 0-9" >&2; exit 2; }

exec /usr/bin/python3 /usr/local/bin/nexvue-ops-env-update.py write "$N"
