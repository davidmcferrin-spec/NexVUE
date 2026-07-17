#!/usr/bin/env bash
###############################################################################
# nexvue-captions-probe.sh — one-shot VANC caption type probe for a DeckLink
# input. Confirms whether the feed carries CEA-608 (including 608-in-708 CDP)
# before relying on the side-channel decoder (CC1).
#
# Usage (on the edge node; stops any encoder that holds the same device):
#   sudo systemctl stop nexvue-encode@0
#   sudo -u nexvue ./nexvue-captions-probe.sh 0
#   sudo systemctl start nexvue-encode@0
#
# Interpreting output:
#   closedcaption/x-cea-608  → CC1 path will work
#   closedcaption/x-cea-708  → usually still has 608 compatibility bytes;
#                              ccconverter extracts them. If probe shows only
#                              708 and the live decoder never emits text,
#                              the feed is 708-only (out of scope for v1).
###############################################################################
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
set -euo pipefail

DEVICE="${1:-0}"
SECONDS_RUN="${2:-8}"

if ! [[ "${DEVICE}" =~ ^[0-9]+$ ]]; then
  echo "usage: $0 <device-number> [seconds]" >&2
  exit 64
fi
command -v gst-launch-1.0 >/dev/null || { echo "gst-launch-1.0 missing" >&2; exit 69; }
gst-inspect-1.0 decklinkvideosrc >/dev/null 2>&1 \
  || { echo "decklinkvideosrc missing" >&2; exit 69; }
gst-inspect-1.0 ccextractor >/dev/null 2>&1 \
  || { echo "ccextractor missing (gstreamer1.0-plugins-bad)" >&2; exit 69; }

echo "[probe] device=${DEVICE} for ${SECONDS_RUN}s — Caps / type lines below indicate caption presence"
# GST_DEBUG on closedcaption + ccextractor prints pad caps when captions appear.
timeout --signal=INT "${SECONDS_RUN}" \
  env GST_DEBUG=ccextractor:4,closedcaption:3 \
  gst-launch-1.0 -q \
    decklinkvideosrc device-number="${DEVICE}" mode=auto output-cc=true \
    ! queue max-size-buffers=2 leaky=downstream \
    ! ccextractor name=cc \
    cc. ! fakesink sync=false \
    cc.caption ! queue ! tee name=t \
    t. ! queue ! fakesink dump=true sync=false \
    t. ! queue ! ccconverter ! fakesink sync=false \
  2>&1 | tee /dev/stderr | grep -Ei 'cea-608|cea-708|closedcaption|caps|cc_data|cdp|s334' \
  || true

echo "[probe] done — if nothing matched, the input likely has no embedded captions (or the device is busy)"
