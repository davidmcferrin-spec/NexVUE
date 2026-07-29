#!/usr/bin/env bash
# nexvue-encode-auto-park.sh — park nexvue-encode@N after consecutive unlocked
# DeckLink starts (empty / unpatched BNC), so Restart=always does not storm.
#
# Verbs:
#   check <channel_id> <device_number>
#     Run from nexvue-encode.sh (user nexvue) before gst-launch.
#     Reads AUTO_PARK_UNLOCK_CYCLES (default 5; 0 = disabled).
#     Exit 0  → continue encode (locked, disabled, or fail-open).
#     Exit 1  → unlocked this start; counter bumped; encode should exit so
#               systemd restarts (fast cycle, no gst-launch).
#     Exit 75 → park threshold hit; encode must exit 75 (RestartPreventExitStatus).
#   stoppost <channel_id>
#     Run from ExecStopPost=+ (root). If a park request exists for the slot,
#     systemctl disable + reset-failed that encode@N.
#
# State lives under /run/nexvue/auto-park/ (RuntimeDirectory; wiped on reboot —
# a reboot with empty ports still enabled re-counts then parks again).
set -euo pipefail

log() { echo "[nexvue-auto-park] $*"; }

STATE_DIR="${AUTO_PARK_STATE_DIR:-/run/nexvue/auto-park}"
STATUS_BIN="${DECKLINK_STATUS_BIN:-}"
if [ -z "${STATUS_BIN}" ]; then
  if [ -x /usr/local/bin/decklink-status ]; then
    STATUS_BIN=/usr/local/bin/decklink-status
  else
    STATUS_BIN="$(command -v decklink-status 2>/dev/null || true)"
  fi
fi

EXIT_PARK=75

usage() {
  echo "usage: $0 check <channel_id> <device_number>" >&2
  echo "       $0 stoppost <channel_id>" >&2
  exit 2
}

channel_ok() {
  [[ "${1:-}" =~ ^[0-7]$ ]]
}

cycles_from_env() {
  local raw="${AUTO_PARK_UNLOCK_CYCLES:-5}"
  raw="${raw%%#*}"
  raw="${raw#"${raw%%[![:space:]]*}"}"
  raw="${raw%"${raw##*[![:space:]]}"}"
  if [ -z "$raw" ]; then
    echo 5
    return
  fi
  if ! [[ "$raw" =~ ^[0-9]+$ ]]; then
    log "WARN: AUTO_PARK_UNLOCK_CYCLES='${raw}' invalid — treating as 0 (off)"
    echo 0
    return
  fi
  echo "$raw"
}

ensure_state_dir() {
  mkdir -p "${STATE_DIR}"
}

count_path() { echo "${STATE_DIR}/${1}.count"; }
request_path() { echo "${STATE_DIR}/${1}.request"; }

read_count() {
  local f
  f="$(count_path "$1")"
  if [ -f "$f" ]; then
    local n
    n="$(tr -d '[:space:]' <"$f" 2>/dev/null || echo 0)"
    if [[ "$n" =~ ^[0-9]+$ ]]; then
      echo "$n"
      return
    fi
  fi
  echo 0
}

write_count() {
  ensure_state_dir
  printf '%s\n' "$2" >"$(count_path "$1")"
}

clear_count() {
  rm -f "$(count_path "$1")"
}

write_request() {
  ensure_state_dir
  printf 'unlocked device=%s cycles=%s\n' "${2:-}" "${3:-}" >"$(request_path "$1")"
}

clear_request() {
  rm -f "$(request_path "$1")"
}

# Probe DEVICE_NUMBER via decklink-status JSON. Prints: locked|unlocked|busy|missing|error
probe_lock() {
  local dev="$1"
  if [ -z "${STATUS_BIN}" ] || [ ! -x "${STATUS_BIN}" ]; then
    echo "error"
    return
  fi
  local json
  if ! json="$("${STATUS_BIN}" 2>/dev/null)"; then
    echo "error"
    return
  fi
  # Prefer python3 (stdlib) over jq — always present on NexVUE edges.
  # Fall back to `python` for Windows/Git Bash test hosts.
  local py=""
  if command -v python3 >/dev/null 2>&1; then
    py=python3
  elif command -v python >/dev/null 2>&1; then
    py=python
  else
    echo "error"
    return
  fi
  printf '%s' "$json" | "$py" -c '
import json, sys
dev = int(sys.argv[1])
try:
    data = json.load(sys.stdin)
except Exception:
    print("error")
    raise SystemExit(0)
for d in data.get("devices") or []:
    if int(d.get("index", -1)) == dev:
        if d.get("busy") is True:
            print("busy")
        elif d.get("input_locked") is True:
            print("locked")
        else:
            print("unlocked")
        raise SystemExit(0)
print("missing")
' "$dev" 2>/dev/null || echo "error"
}

cmd_check() {
  local ch="${1:-}"
  local dev="${2:-}"
  if ! channel_ok "$ch"; then
    log "ERROR: channel_id must be 0-7, got '${ch}'"
    exit 2
  fi
  if ! [[ "${dev}" =~ ^[0-9]+$ ]]; then
    log "ERROR: device_number must be an integer, got '${dev}'"
    exit 2
  fi

  # SRT (and any non-decklink) inputs have no DeckLink lock — never auto-park.
  local itype
  itype="$(printf '%s' "${INPUT_TYPE:-decklink}" | tr '[:upper:]' '[:lower:]')"
  itype="${itype%%#*}"
  itype="${itype#"${itype%%[![:space:]]*}"}"
  itype="${itype%"${itype##*[![:space:]]}"}"
  if [ -z "$itype" ]; then
    itype=decklink
  fi
  if [ "$itype" != "decklink" ]; then
    clear_count "$ch"
    clear_request "$ch"
    exit 0
  fi

  local cycles
  cycles="$(cycles_from_env)"
  if [ "$cycles" -eq 0 ]; then
    clear_count "$ch"
    clear_request "$ch"
    exit 0
  fi

  local state
  state="$(probe_lock "$dev")"
  case "$state" in
    locked)
      clear_count "$ch"
      clear_request "$ch"
      log "device ${dev}: locked — unlock streak cleared"
      exit 0
      ;;
    busy)
      # Another holder (or ambiguous busy flag) — do not count toward park.
      log "device ${dev}: busy — skip unlock count (fail open)"
      exit 0
      ;;
    missing|error)
      log "WARN: device ${dev}: probe ${state} — skip unlock count (fail open)"
      exit 0
      ;;
    unlocked)
      ;;
    *)
      log "WARN: device ${dev}: unexpected probe '${state}' — skip (fail open)"
      exit 0
      ;;
  esac

  local n
  n="$(read_count "$ch")"
  n=$((n + 1))
  write_count "$ch" "$n"
  if [ "$n" -ge "$cycles" ]; then
    write_request "$ch" "$dev" "$n"
    log "device ${dev}: unlocked ${n}/${cycles} — requesting auto-park (exit ${EXIT_PARK})"
    exit "${EXIT_PARK}"
  fi
  log "device ${dev}: unlocked ${n}/${cycles} — deferring encode (restart cycle)"
  exit 1
}

cmd_stoppost() {
  local ch="${1:-}"
  if ! channel_ok "$ch"; then
    log "ERROR: channel_id must be 0-7, got '${ch}'"
    exit 2
  fi
  local req
  req="$(request_path "$ch")"
  if [ ! -f "$req" ]; then
    exit 0
  fi
  local unit="nexvue-encode@${ch}"
  log "park request present for ${unit} — disable + reset-failed"
  # Already stopping / stopped; disable without --now (boot config only).
  systemctl disable "$unit" || log "WARN: systemctl disable ${unit} failed"
  systemctl reset-failed "$unit" 2>/dev/null || true
  clear_request "$ch"
  clear_count "$ch"
  log "parked ${unit} (re-enable from Services when the BNC is patched)"
  exit 0
}

VERB="${1:-}"
case "$VERB" in
  check)
    shift
    cmd_check "$@"
    ;;
  stoppost)
    shift
    cmd_stoppost "$@"
    ;;
  *)
    usage
    ;;
esac
