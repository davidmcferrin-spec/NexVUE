#!/usr/bin/env bash
# nexvue-ops-enable.sh — allowlisted systemctl enable/disable/start/stop for
# the NexVUE ops UI.
# Usage: nexvue-ops-enable.sh <enable|disable|start|stop> <unit>
#
# Deliberately restricted to nexvue-encode@0-7: the Phase 1 LAN-trust ops page
# must not be able to disable or stop mediamtx / nexvue-status /
# nexvue-metrics (that would take down every channel or the shared status
# daemon). Restart of those core units stays available via
# nexvue-ops-restart.sh.
#
# enable/disable use --now (boot config + immediate effect); start/stop are
# runtime-only and leave the boot config alone. disable and stop also run
# reset-failed so a previously restart-looping encoder does not keep showing
# a stale "failed" state after it has been parked.
set -euo pipefail

VERB="${1:-}"
UNIT="${2:-}"

case "$VERB" in
  enable|disable|start|stop) ;;
  *) echo "disallowed verb: $VERB" >&2; exit 2 ;;
esac
case "$UNIT" in
  nexvue-encode@[0-7]) ;;
  *) echo "disallowed unit: $UNIT" >&2; exit 2 ;;
esac

case "$VERB" in
  enable)
    systemctl enable --now "$UNIT"
    ;;
  disable)
    systemctl disable --now "$UNIT"
    systemctl reset-failed "$UNIT" 2>/dev/null || true
    ;;
  start)
    systemctl start "$UNIT"
    ;;
  stop)
    systemctl stop "$UNIT"
    systemctl reset-failed "$UNIT" 2>/dev/null || true
    ;;
esac
