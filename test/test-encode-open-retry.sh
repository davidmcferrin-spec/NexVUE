#!/usr/bin/env bash
# Capture-retry / open-gate policy lives in nexvue-encode.py (no gst-launch).
# This runner keeps the historical test path; the cases are in test_nexvue_encode.py.
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
python3 test/test_nexvue_encode.py TestCapturePolicy
echo "OK: capture-retry / open-gate policy tests passed"
