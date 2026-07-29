#!/usr/bin/env bash
# Exercises nexvue-encode.sh DeckLink open supervisor: quick gst failure retries
# then succeeds. Uses a counting gst-launch stub on PATH.
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

STUB_DIR="$(mktemp -d)"
cleanup() { rm -rf "$STUB_DIR"; }
trap cleanup EXIT

# Fail twice with not-negotiated, then succeed (print pipeline like assembly stub).
cat >"${STUB_DIR}/gst-launch-1.0" <<'EOF'
#!/usr/bin/env bash
n=0
if [ -f "${GST_LAUNCH_COUNT_FILE}" ]; then
  n="$(cat "${GST_LAUNCH_COUNT_FILE}")"
fi
n=$((n + 1))
printf '%s\n' "$n" >"${GST_LAUNCH_COUNT_FILE}"
if [ "$n" -lt 3 ]; then
  echo "ERROR: from element decklinkvideosrc0: Internal data stream error." >&2
  echo "streaming stopped, reason not-negotiated (-4)" >&2
  exit 1
fi
printf '%s\n' "$*"
exit 0
EOF
chmod +x "${STUB_DIR}/gst-launch-1.0"

# Minimal gst-inspect stub (always available).
cat >"${STUB_DIR}/gst-inspect-1.0" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "${STUB_DIR}/gst-inspect-1.0"

export PATH="${STUB_DIR}:${ROOT}/test/stubbin:${PATH}"
export GST_LAUNCH_COUNT_FILE="${STUB_DIR}/count"
export DECKLINK_OPEN_DELAY_S=0
export DECKLINK_START_STAGGER_S=0
export DECKLINK_OPEN_ATTEMPTS=5
export DECKLINK_OPEN_BACKOFF_S=0
export DECKLINK_HANG_KILL_S=1
export DECKLINK_OPEN_GATE_S=10
export CAPTIONS_ENABLE=false
export LO_ENABLE=false

out="$(DEVICE_NUMBER=0 CHANNEL_PATH=ch0 bash ./nexvue-encode.sh 2>&1)" || {
  echo "FAIL: encode exited non-zero"
  echo "$out"
  exit 1
}
grep -q "gst open attempt 1/5" <<<"$out" || { echo "FAIL: missing attempt 1"; echo "$out"; exit 1; }
grep -q "gst open attempt 3/5" <<<"$out" || { echo "FAIL: missing attempt 3 (retries)"; echo "$out"; exit 1; }
n="$(cat "${GST_LAUNCH_COUNT_FILE}")"
[ "$n" -eq 3 ] || { echo "FAIL: expected 3 gst-launch calls, got $n"; exit 1; }
grep -q "device-number=0" <<<"$out" || { echo "FAIL: pipeline not printed on success"; exit 1; }

echo "OK: open-retry recovered after 2 not-negotiated failures"
