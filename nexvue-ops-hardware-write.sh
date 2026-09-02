#!/usr/bin/env bash
# nexvue-ops-hardware-write.sh — write MAX_DEVICES/MAX_CHANNELS + apply encode slots.
# Usage: nexvue-ops-hardware-write.sh   # JSON {"slots":N} on stdin
set -euo pipefail

exec /usr/bin/python3 /usr/local/bin/nexvue-ops-hardware-write.py
