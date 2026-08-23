#!/usr/bin/env bash
# nexvue-ops-network-write.sh — write public hostname/IP + patch MediaMTX ICE hosts.
# Usage: nexvue-ops-network-write.sh   # JSON {"hostname","ip"} on stdin
set -euo pipefail

exec /usr/bin/python3 /usr/local/bin/nexvue-ops-network-write.py
