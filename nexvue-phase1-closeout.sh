#!/usr/bin/env bash
###############################################################################
# nexvue-phase1-closeout.sh — Phase 1 hardware closeout checks
#
# Run on the edge box (not from a Windows checkout). Prints a pass/fail
# summary for the remaining Phase 1 gate items that can be automated:
#   - DeckLink inputs locked / modes (connector direction sanity)
#   - encode / MediaMTX / status / metrics units active
#   - encoder restart count over the last 72h (soak)
#   - caption JSON freshness per channel (when CAPTIONS_ENABLE)
#   - iGPU sample presence in metrics DB (optional)
#
# Usage:
#   sudo ./nexvue-phase1-closeout.sh
#   sudo ./nexvue-phase1-closeout.sh --since 24h   # shorter soak window
#
# Manual items this script cannot finish (fill in README results table):
#   - burnt-in-clock latency photos (59.94p / 29.97p × audio on/off)
#   - BlackmagicDesktopVideoSetup connector Input flips
###############################################################################
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
set -euo pipefail

GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
ok()   { echo "${GREEN}[ OK ]${RESET} $*"; PASS+=("$*"); }
warn() { echo "${YELLOW}[WARN]${RESET} $*"; WARN+=("$*"); }
fail() { echo "${RED}[FAIL]${RESET} $*"; FAIL+=("$*"); }
PASS=(); WARN=(); FAIL=()

SINCE="72h"
while [ $# -gt 0 ]; do
  case "$1" in
    --since=*) SINCE="${1#--since=}" ;;
    --since)
      shift
      SINCE="${1:-72h}"
      ;;
    *)
      echo "usage: $0 [--since 72h]" >&2
      exit 2
      ;;
  esac
  shift
done

echo "=== NexVUE Phase 1 closeout ($(date -Is)) since=${SINCE} ==="

# ---- Units ------------------------------------------------------------------
for u in mediamtx nexvue-status nexvue-metrics; do
  if systemctl is-active --quiet "$u"; then
    ok "unit active: $u"
  else
    fail "unit not active: $u"
  fi
done

ENC_ACTIVE=0
for n in 0 1 2 3 4 5 6 7; do
  if systemctl is-active --quiet "nexvue-encode@${n}"; then
    ENC_ACTIVE=$((ENC_ACTIVE + 1))
    ok "encoder active: nexvue-encode@${n}"
  elif [ -f "/etc/nexvue/channels/${n}.env" ]; then
    warn "env present but encoder inactive: nexvue-encode@${n}"
  fi
done
if [ "$ENC_ACTIVE" -eq 0 ]; then
  fail "no nexvue-encode@N instances active"
fi

# ---- DeckLink status --------------------------------------------------------
if [ -x /usr/local/bin/decklink-status ]; then
  if command -v jq >/dev/null 2>&1; then
    STATUS_JSON="$(/usr/local/bin/decklink-status 2>/dev/null || true)"
    if [ -n "$STATUS_JSON" ]; then
      LOCKED="$(printf '%s' "$STATUS_JSON" | jq -r '[.devices[]? | select(.input_locked==true)] | length')"
      TOTAL="$(printf '%s' "$STATUS_JSON" | jq -r '.devices|length')"
      ok "decklink-status: ${LOCKED}/${TOTAL} inputs locked"
      printf '%s' "$STATUS_JSON" | jq -r '.devices[]? | "  device \(.index): locked=\(.input_locked) mode=\(.input_mode // "-")"'
      # Hint: unlocked rows with no mode often mean Output direction or unpatched.
      UNLOCKED="$(printf '%s' "$STATUS_JSON" | jq -r '[.devices[]? | select(.input_locked!=true)] | length')"
      if [ "${UNLOCKED:-0}" -gt 0 ]; then
        warn "unlocked inputs present — confirm Duo 2 connectors are Input (BlackmagicDesktopVideoSetup) and cables patched"
      fi
    else
      fail "decklink-status returned no JSON"
    fi
  else
    warn "jq missing — install jq to summarize decklink-status"
  fi
else
  warn "decklink-status not installed — player dots stay gray; make DECKLINK_SDK=... && sudo make install"
fi

# ---- Soak: encoder restarts -------------------------------------------------
if command -v journalctl >/dev/null 2>&1; then
  # Count systemd "Started" lines for encode instances in the window.
  RESTARTS="$(journalctl -u 'nexvue-encode@*' --since "-${SINCE}" --no-pager 2>/dev/null \
    | grep -ciE 'Started NexVUE|Started nexvue-encode' || true)"
  RESTARTS="${RESTARTS//$'\r'/}"
  if [ "${RESTARTS:-0}" -eq 0 ]; then
    ok "encode restarts in last ${SINCE}: 0"
  elif [ "${RESTARTS:-0}" -le "$ENC_ACTIVE" ]; then
    # One "Started" per currently-running instance is expected if soak began
    # with a fresh enable — not proof of crash loops.
    warn "encode Started lines in last ${SINCE}: ${RESTARTS} (≈ active count; inspect journal for crash loops)"
  else
    fail "encode Started lines in last ${SINCE}: ${RESTARTS} (want ~0 beyond initial enable)"
  fi
else
  warn "journalctl unavailable"
fi

# ---- Captions freshness -----------------------------------------------------
CAP_DIR="${NEXVUE_CAPTIONS_DIR:-/run/nexvue/captions}"
if [ -d "$CAP_DIR" ]; then
  ANY_CAP=0
  NOW="$(date +%s)"
  for f in "$CAP_DIR"/*.json; do
    [ -f "$f" ] || continue
    ANY_CAP=1
    AGE=$(( NOW - $(stat -c %Y "$f" 2>/dev/null || echo "$NOW") ))
    if [ "$AGE" -le 60 ]; then
      ok "caption state fresh: $(basename "$f") (${AGE}s old)"
    else
      warn "caption state stale: $(basename "$f") (${AGE}s old) — OK if CAPTIONS_ENABLE=false or no CC on feed"
    fi
  done
  if [ "$ANY_CAP" -eq 0 ]; then
    warn "no caption JSON under ${CAP_DIR} — enable CAPTIONS_ENABLE and probe with nexvue-captions-probe.sh"
  fi
else
  warn "caption dir missing: ${CAP_DIR}"
fi

# ---- Metrics DB presence ----------------------------------------------------
METRICS_DB="${NEXVUE_METRICS_DB:-/var/lib/nexvue/metrics.db}"
if [ -f "$METRICS_DB" ]; then
  ok "metrics DB present: ${METRICS_DB}"
else
  warn "metrics DB missing: ${METRICS_DB}"
fi

# ---- Manual gate reminder ---------------------------------------------------
echo
echo "Manual Phase 1 gates (not automatable here):"
echo "  1. Flip remaining Duo 2 connectors Output → Input (BlackmagicDesktopVideoSetup)"
echo "  2. Burnt-in-clock latency: fill results table in README.md"
echo "     (59.94p / 29.97p × ENABLE_AUDIO on/off; target ~200 ms, bug if >300 ms)"
echo "  3. Confirm soak window with all intended channels hot, then re-run this script"

echo
echo "=== Summary: ${#PASS[@]} ok, ${#WARN[@]} warn, ${#FAIL[@]} fail ==="
if [ "${#FAIL[@]}" -gt 0 ]; then
  exit 1
fi
exit 0
