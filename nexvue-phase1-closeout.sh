#!/usr/bin/env bash
###############################################################################
# nexvue-phase1-closeout.sh — Phase 1 hardware closeout checks
#
# Run on the edge box (not from a Windows checkout). Prints a pass/fail
# summary for the remaining Phase 1 gate items that can be automated:
#   - DeckLink inputs locked / modes (connector direction sanity)
#   - encode / MediaMTX / status / metrics units active
#   - per-instance encoder Started counts over the soak window
#   - caption JSON freshness per channel (when CAPTIONS_ENABLE)
#   - iGPU sample presence in metrics DB (optional)
#
# Usage:
#   sudo ./nexvue-phase1-closeout.sh
#   sudo ./nexvue-phase1-closeout.sh --since 24h   # shorter soak window
#
# Only enable nexvue-encode@N for patched Input connectors. Empty Quad ports
# left enabled restart-loop (RestartSec=3) until Phase 1.5 slate supervisor.
#
# Manual items this script cannot finish:
#   - confirm Quad 2 connectors are Input (BlackmagicDesktopVideoSetup)
#   - glass-to-glass latency photos (deferred on remote datacenter; see README)
#   - disable empty-channel units (script prints the command when needed)
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

# Fresh enable + one heal is normal; above this on a locked input = storm.
STARTED_OK_MAX=2

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

read_device_number() {
  local n=$1 f="/etc/nexvue/channels/${n}.env" v=""
  if [ -f "$f" ]; then
    v="$(grep -E '^[[:space:]]*DEVICE_NUMBER=' "$f" 2>/dev/null | tail -1 \
      | cut -d= -f2- || true)"
    v="${v%%#*}"
    v="${v//\"/}"
    v="${v//\'/}"
    v="${v// /}"
    v="${v//$'\t'/}"
  fi
  if [[ "${v}" =~ ^[0-9]+$ ]]; then
    printf '%s' "$v"
  else
    printf '%s' "$n"
  fi
}

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
ACTIVE_NS=()
for n in 0 1 2 3 4 5 6 7; do
  if systemctl is-active --quiet "nexvue-encode@${n}"; then
    ENC_ACTIVE=$((ENC_ACTIVE + 1))
    ACTIVE_NS+=("$n")
    ok "encoder active: nexvue-encode@${n}"
  elif [ -f "/etc/nexvue/channels/${n}.env" ]; then
    warn "env present but encoder inactive: nexvue-encode@${n}"
  fi
done
if [ "$ENC_ACTIVE" -eq 0 ]; then
  fail "no nexvue-encode@N instances active"
fi

# ---- DeckLink status --------------------------------------------------------
STATUS_JSON=""
declare -A LOCKED_BY_IDX=()
if [ -x /usr/local/bin/decklink-status ]; then
  if command -v jq >/dev/null 2>&1; then
    STATUS_JSON="$(/usr/local/bin/decklink-status 2>/dev/null || true)"
    if [ -n "$STATUS_JSON" ]; then
      LOCKED="$(printf '%s' "$STATUS_JSON" | jq -r '[.devices[]? | select(.input_locked==true)] | length')"
      TOTAL="$(printf '%s' "$STATUS_JSON" | jq -r '.devices|length')"
      ok "decklink-status: ${LOCKED}/${TOTAL} inputs locked"
      printf '%s' "$STATUS_JSON" | jq -r '.devices[]? | "  device \(.index): locked=\(.input_locked) mode=\(.input_mode // "-")"'
      while IFS=$'\t' read -r idx locked; do
        [ -n "$idx" ] || continue
        LOCKED_BY_IDX["$idx"]="$locked"
      done < <(printf '%s' "$STATUS_JSON" | jq -r '.devices[]? | "\(.index)\t\(.input_locked)"')
      UNLOCKED="$(printf '%s' "$STATUS_JSON" | jq -r '[.devices[]? | select(.input_locked!=true)] | length')"
      if [ "${UNLOCKED:-0}" -gt 0 ]; then
        warn "unlocked inputs present — empty BNC or not Input; Phase 1.5 supervisor serves NO SIGNAL slate when encode@N is left enabled"
      fi
    else
      fail "decklink-status returned no JSON"
    fi
  else
    warn "jq missing — install jq to summarize decklink-status and correlate soak restarts"
  fi
else
  warn "decklink-status not installed — player dots stay gray; make DECKLINK_SDK=... && sudo make install"
fi

# ---- Soak: per-instance encoder Started counts ------------------------------
# Phase 1.5: an unlocked active encoder is healthy if it is NOT restarting
# (supervisor holds slate). Fail only when Started storms regardless of lock.
DISABLE_CANDIDATES=()
if command -v journalctl >/dev/null 2>&1; then
  if [ "${#ACTIVE_NS[@]}" -eq 0 ]; then
    warn "no active encoders — skip soak Started counts"
  else
    echo
    echo "Encode soak (Started lines in last ${SINCE}):"
    printf '  %-18s %-8s %-8s %-8s %s\n' "unit" "device" "locked" "Started" "verdict"
    for n in "${ACTIVE_NS[@]}"; do
      unit="nexvue-encode@${n}"
      dev="$(read_device_number "$n")"
      started="$(journalctl -u "$unit" --since "-${SINCE}" --no-pager 2>/dev/null \
        | grep -ciE 'Started NexVUE|Started nexvue-encode' || true)"
      started="${started//$'\r'/}"
      started="${started:-0}"

      locked_raw="${LOCKED_BY_IDX[$dev]:-unknown}"
      case "$locked_raw" in
        true)  locked="true" ;;
        false) locked="false" ;;
        *)     locked="unknown" ;;
      esac

      if [ "$started" -le "$STARTED_OK_MAX" ]; then
        if [ "$locked" = "false" ]; then
          verdict="ok (slate / unlocked)"
        else
          verdict="ok"
        fi
        printf '  %-18s %-8s %-8s %-8s %s\n' "$unit" "$dev" "$locked" "$started" "$verdict"
        ok "${unit}: Started=${started} (device ${dev}, locked=${locked})"
      elif [ "$locked" = "true" ]; then
        verdict="FAIL storm on locked input"
        printf '  %-18s %-8s %-8s %-8s %s\n' "$unit" "$dev" "$locked" "$started" "$verdict"
        fail "${unit}: Started=${started} on locked device ${dev} (want ≤${STARTED_OK_MAX}; inspect journal)"
      else
        # Unlocked + storming: still a problem (supervisor should hold slate
        # without systemd cycling). Fail so the soak catches it.
        verdict="FAIL storm while unlocked/slate"
        printf '  %-18s %-8s %-8s %-8s %s\n' "$unit" "$dev" "$locked" "$started" "$verdict"
        fail "${unit}: Started=${started} while unlocked (supervisor should slate without restarting)"
      fi
    done
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
echo "Manual Phase 1 / 1.5 gates (not automatable here):"
echo "  1. Quad 2: intended connectors Input; MAX_DEVICES in /etc/nexvue/nexvue.env"
echo "  2. Supervisor: unlocked encode@N should publish NO SIGNAL slate without restarting"
echo "  3. Latency: RTT-based ~200 ms estimate is accepted for remote datacenter;"
echo "     glass-to-glass photo deferred (see README) — not a Phase 1 blocker"
echo "  4. Confirm soak window, then re-run this script"

echo
echo "=== Summary: ${#PASS[@]} ok, ${#WARN[@]} warn, ${#FAIL[@]} fail ==="
if [ "${#FAIL[@]}" -gt 0 ]; then
  exit 1
fi
exit 0
