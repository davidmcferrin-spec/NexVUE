#!/usr/bin/env bash
# nexvue-ops-portal-write.sh — write NEXVUE_PORTAL_* fields to station env.
# Usage: nexvue-ops-portal-write.sh   # JSON patch on stdin
set -euo pipefail

exec /usr/bin/python3 /usr/local/bin/nexvue-ops-portal-write.py
