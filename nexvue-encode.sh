#!/usr/bin/env bash
###############################################################################
# nexvue-encode.sh — ExecStart wrapper for nexvue-encode@N
#
# systemd sources channel + station env, then execs this script. Real encode
# is nexvue-encode.py: persistent MediaMTX publish + disposable DeckLink
# capture (no gst-launch, no input-selector / slate).
#
# Assembly tests set NEXVUE_ENCODE_PRINT_PIPELINE=1 to print both pipeline
# descriptions and exit without GI.
###############################################################################
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
set -euo pipefail

log() { echo "[nexvue-encode] $*"; }

HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -f "${HERE}/nexvue-encode.py" ]; then
  ENCODE_PY="${HERE}/nexvue-encode.py"
elif [ -f /usr/local/bin/nexvue-encode.py ]; then
  ENCODE_PY=/usr/local/bin/nexvue-encode.py
else
  log "ERROR: nexvue-encode.py not found (looked in ${HERE} and /usr/local/bin)"
  exit 69
fi

if [ "${NEXVUE_ENCODE_PRINT_PIPELINE:-}" = "1" ] || [ "${NEXVUE_ENCODE_PRINT_PIPELINE:-}" = "true" ]; then
  exec python3 "${ENCODE_PY}" --print-pipeline "$@"
fi
exec python3 "${ENCODE_PY}" "$@"
