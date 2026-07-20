#!/usr/bin/env bash
###############################################################################
# nexvue-encode-storm-diagnose.sh — classify encode Restart storms on the edge
#
# Run on the edge box when nexvue-phase1-closeout.sh reports high Started
# counts. Prints ExecStart/GI status, Started counts for 1h vs 72h, and a
# filtered journal tail per active encoder.
#
# Usage:
#   sudo ./nexvue-encode-storm-diagnose.sh
#   sudo nexvue-encode-storm-diagnose.sh          # after setup.sh install
###############################################################################
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
set -euo pipefail

echo "=== NexVUE encode storm diagnose ($(date -Is)) ==="
echo

echo "-- ExecStart / binary --"
systemctl show -p FragmentPath -p ExecStart nexvue-encode@0 2>/dev/null || true
for f in /usr/local/bin/nexvue-supervisor.py /usr/local/bin/nexvue-encode.sh; do
  if [ -x "$f" ] || [ -f "$f" ]; then
    ls -l "$f"
  else
    echo "MISSING: $f"
  fi
done
echo

echo "-- PyGObject / GStreamer --"
if python3 -c 'import gi; gi.require_version("Gst","1.0"); from gi.repository import Gst; print("GI OK", Gst.version_string())' 2>/dev/null; then
  :
else
  echo "GI FAIL — supervisor will exit 69; apt install python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0"
fi
echo

echo "-- Started counts (historical vs recent) --"
printf '%-18s %8s %8s %s\n' "unit" "72h" "1h" "hint"
for n in 0 1 2 3 4 5 6 7 8 9; do
  unit="nexvue-encode@${n}"
  systemctl is-active --quiet "$unit" 2>/dev/null || continue
  c72="$(journalctl -u "$unit" --since -72h --no-pager 2>/dev/null \
    | grep -ciE 'Started NexVUE|Started nexvue-encode' || true)"
  c1="$(journalctl -u "$unit" --since -1h --no-pager 2>/dev/null \
    | grep -ciE 'Started NexVUE|Started nexvue-encode' || true)"
  c72="${c72:-0}"; c1="${c1:-0}"
  hint="ok"
  if [ "$c1" -gt 2 ]; then
    hint="LIVE storm (last hour)"
  elif [ "$c72" -gt 2 ]; then
    hint="historical only (quiet last hour)"
  fi
  printf '%-18s %8s %8s %s\n' "$unit" "$c72" "$c1" "$hint"
done
echo

echo "-- Journal tails (errors / supervisor, last 2h) --"
for n in 0 1 2 3 4 5 6 7 8 9; do
  unit="nexvue-encode@${n}"
  systemctl is-active --quiet "$unit" 2>/dev/null || continue
  echo "======== ${unit} ========"
  journalctl -u "$unit" --since -2h --no-pager 2>/dev/null \
    | grep -Ei 'nexvue-supervisor|ERROR|Traceback|fatal|signal|DeckLink|RTSP|MediaMTX|GI|import|caption|EPIPE|Failed|Started|watchdog' \
    | tail -40 || echo "(no matching lines)"
  echo
done

echo "Classify:"
echo "  GI/import / exit 69     → sudo ./setup.sh ; restart encode@*"
echo "  caption / filesink / EPIPE → fixed in supervisor (caption errors non-fatal); redeploy"
echo "  watchdog under 15s debounce → WATCHDOG_MS now defaults 0; redeploy"
echo "  72h high / 1h low       → historical pollution; re-soak with --since 1h then 24h"
echo "  RTSP / MediaMTX         → check mediamtx journal; publish URL"
