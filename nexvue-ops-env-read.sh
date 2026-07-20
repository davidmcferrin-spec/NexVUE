#!/usr/bin/env bash
# nexvue-ops-env-read.sh — read one channel env file as JSON.
# Usage: nexvue-ops-env-read.sh <N>   # N = 0..9
set -euo pipefail

N="${1:-}"
[[ "$N" =~ ^[0-9]$ ]] || { echo "channel id must be 0-9" >&2; exit 2; }

exec /usr/bin/python3 /usr/local/bin/nexvue-ops-env-update.py read "$N"
