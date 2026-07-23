#!/usr/bin/env bash
# nexvue-ops-audio-probe.sh — allowlisted DeckLink embedded-audio energy probe.
# Usage: nexvue-ops-audio-probe.sh <device_number> [duration_ms]
#
# device_number: 0-15 (matches DEVICE_NUMBER / decklinkvideosrc device-number)
# duration_ms:   200-3000 (optional; default inside the binary)
#
# The DeckLink sub-device must be free. nexvue-ops.php stops nexvue-encode@N
# around this call when probing a live channel.
set -euo pipefail

DEV="${1:-}"
DUR="${2:-}"

[[ "$DEV" =~ ^[0-9]+$ ]] || { echo "device must be an integer 0-15" >&2; exit 2; }
if [ "$DEV" -gt 15 ]; then
  echo "device must be 0-15" >&2
  exit 2
fi

PROBE="/usr/local/bin/decklink-audio-probe"
if [ ! -x "$PROBE" ]; then
  echo "{\"ok\":false,\"device\":${DEV},\"busy\":false,\"error\":\"probe_not_installed\",\"channels\":[]}"
  exit 1
fi

if [ -n "$DUR" ]; then
  [[ "$DUR" =~ ^[0-9]+$ ]] || { echo "duration_ms must be an integer" >&2; exit 2; }
  exec "$PROBE" "$DEV" "$DUR"
else
  exec "$PROBE" "$DEV"
fi
