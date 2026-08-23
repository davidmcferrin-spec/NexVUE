#!/usr/bin/env bash
# nexvue-encode-auto-park.sh — park / unpark nexvue-encode@N from DeckLink lock.
#
# Verbs:
#   check <channel_id> <device_number>
#     Run from nexvue-encode.py (user nexvue) before the first capture open.
#     Reads AUTO_PARK_UNLOCK_CYCLES (default 5; 0 = disabled).
#     Exit 0  → continue encode (locked, disabled, or fail-open).
#     Exit 1  → unlocked this start; counter bumped; encode should exit so
#               systemd restarts (fast cycle, no gst-launch).
#     Exit 75 → park threshold hit; encode must exit 75 (RestartPreventExitStatus).
#   stoppost <channel_id>
#     Run from ExecStopPost=+ (root). If a park request exists for the slot,
#     systemctl disable + reset-failed that encode@N, and write a durable
#     auto-parked marker so unpark-scan can bring it back when SDI locks.
#   unpark-scan
#     Run from nexvue-encode-auto-unpark.timer (root). For each DeckLink slot
#     in 0..(MAX_CHANNELS-1) that is disabled or inactive: if we last saw
#     unlocked (or the slot was auto-parked) and lock has held for
#     AUTO_UNPARK_LOCK_POLLS ticks (default 2), enable --now or start.
#     Manual Disable/Stop of a still-locked feed is left alone (no unlock→lock
#     edge, no auto-parked marker). SRT and slots at/above MAX_CHANNELS skipped.
#
# Runtime state: /run/nexvue/auto-park/ (wiped on reboot).
# Durable auto-parked markers: /var/lib/nexvue/auto-park/ (survive reboot).
set -euo pipefail

log() { echo "[nexvue-auto-park] $*"; }

STATE_DIR="${AUTO_PARK_STATE_DIR:-/run/nexvue/auto-park}"
DURABLE_DIR="${AUTO_PARK_DURABLE_DIR:-/var/lib/nexvue/auto-park}"
STATION_ENV="${NEXVUE_STATION_ENV:-/etc/nexvue/nexvue.env}"
CHANNELS_DIR="${NEXVUE_CHANNELS_DIR:-/etc/nexvue/channels}"
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
  echo "       $0 unpark-scan" >&2
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

last_path() { echo "${STATE_DIR}/${1}.last"; }
streak_path() { echo "${STATE_DIR}/${1}.lockstreak"; }
durable_parked_path() { echo "${DURABLE_DIR}/${1}.auto-parked"; }
run_parked_path() { echo "${STATE_DIR}/${1}.auto-parked"; }

ensure_durable_dir() {
  mkdir -p "${DURABLE_DIR}"
}

write_auto_parked() {
  ensure_state_dir
  ensure_durable_dir
  printf 'auto-parked\n' >"$(run_parked_path "$1")"
  printf 'auto-parked\n' >"$(durable_parked_path "$1")"
}

has_auto_parked() {
  [ -f "$(run_parked_path "$1")" ] || [ -f "$(durable_parked_path "$1")" ]
}

clear_auto_parked() {
  rm -f "$(run_parked_path "$1")" "$(durable_parked_path "$1")"
}

read_last() {
  local f
  f="$(last_path "$1")"
  if [ -f "$f" ]; then
    tr -d '[:space:]' <"$f" 2>/dev/null || true
  fi
}

write_last() {
  ensure_state_dir
  printf '%s\n' "$2" >"$(last_path "$1")"
}

read_streak() {
  local f n
  f="$(streak_path "$1")"
  if [ -f "$f" ]; then
    n="$(tr -d '[:space:]' <"$f" 2>/dev/null || echo 0)"
    if [[ "$n" =~ ^[0-9]+$ ]]; then
      echo "$n"
      return
    fi
  fi
  echo 0
}

write_streak() {
  ensure_state_dir
  printf '%s\n' "$2" >"$(streak_path "$1")"
}

clear_streak() {
  rm -f "$(streak_path "$1")"
}

# Last uncommented KEY= from an env file (comments/quotes stripped). Empty if missing.
read_env_key() {
  local file="$1" key="$2" line val
  [ -f "$file" ] || return 1
  line="$(grep -E "^[[:space:]]*${key}=" "$file" 2>/dev/null | tail -n 1 || true)"
  [ -n "$line" ] || return 1
  val="${line#*=}"
  val="${val%%#*}"
  val="${val#"${val%%[![:space:]]*}"}"
  val="${val%"${val##*[![:space:]]}"}"
  if [[ "$val" == \"*\" && "$val" == *\" ]]; then
    val="${val:1:${#val}-2}"
  elif [[ "$val" == \'*\' && "$val" == *\' ]]; then
    val="${val:1:${#val}-2}"
  fi
  printf '%s' "$val"
  return 0
}

env_bool_true() {
  local raw
  raw="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  raw="${raw%%#*}"
  raw="${raw#"${raw%%[![:space:]]*}"}"
  raw="${raw%"${raw##*[![:space:]]}"}"
  case "$raw" in
    ""|true|1|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

env_bool_false() {
  local raw
  raw="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  raw="${raw%%#*}"
  raw="${raw#"${raw%%[![:space:]]*}"}"
  raw="${raw%"${raw##*[![:space:]]}"}"
  case "$raw" in
    false|0|no|off) return 0 ;;
    *) return 1 ;;
  esac
}

max_channels_from_env() {
  local n
  n="$(read_env_key "${STATION_ENV}" MAX_CHANNELS || true)"
  if ! [[ "$n" =~ ^[1-8]$ ]]; then
    n="$(read_env_key "${STATION_ENV}" MAX_DEVICES || true)"
  fi
  if ! [[ "$n" =~ ^[1-8]$ ]]; then
    n="${MAX_CHANNELS:-}"
  fi
  if ! [[ "$n" =~ ^[1-8]$ ]]; then
    n=8
  fi
  printf '%s' "$n"
}

unpark_polls_from_env() {
  local raw
  raw="$(read_env_key "${STATION_ENV}" AUTO_UNPARK_LOCK_POLLS || true)"
  if [ -z "$raw" ]; then
    raw="${AUTO_UNPARK_LOCK_POLLS:-2}"
  fi
  raw="${raw%%#*}"
  raw="${raw#"${raw%%[![:space:]]*}"}"
  raw="${raw%"${raw##*[![:space:]]}"}"
  if ! [[ "$raw" =~ ^[0-9]+$ ]]; then
    echo 2
    return
  fi
  echo "$raw"
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

# Load status JSON: test file, then loopback nexvue-status, then decklink-status.
fetch_status_json() {
  if [ -n "${STATUS_JSON_FILE:-}" ] && [ -f "${STATUS_JSON_FILE}" ]; then
    cat "${STATUS_JSON_FILE}"
    return 0
  fi
  local py=""
  if command -v python3 >/dev/null 2>&1; then
    py=python3
  elif command -v python >/dev/null 2>&1; then
    py=python
  else
    return 1
  fi
  if "$py" -c '
import ssl, sys, urllib.request
urls = ("http://127.0.0.1:9998/status", "https://127.0.0.1:9998/status")
ctx = ssl._create_unverified_context()
err = None
for url in urls:
    try:
        kw = {"timeout": 3}
        if url.startswith("https"):
            kw["context"] = ctx
        with urllib.request.urlopen(url, **kw) as resp:
            sys.stdout.buffer.write(resp.read())
            raise SystemExit(0)
    except Exception as exc:
        err = exc
sys.stderr.write(str(err) + "\n")
raise SystemExit(1)
' 2>/dev/null; then
    return 0
  fi
  if [ -n "${STATUS_BIN}" ] && [ -x "${STATUS_BIN}" ]; then
    "${STATUS_BIN}" 2>/dev/null && return 0
  fi
  return 1
}

# stdin JSON → stdout lines "index<TAB>locked|unlocked|busy". Exit 3 if stale.
parse_status_locks() {
  local py=""
  if command -v python3 >/dev/null 2>&1; then
    py=python3
  elif command -v python >/dev/null 2>&1; then
    py=python
  else
    return 1
  fi
  "$py" -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
if data.get("stale") is True:
    raise SystemExit(3)
for d in data.get("devices") or []:
    try:
        idx = int(d.get("index", -1))
    except (TypeError, ValueError):
        continue
    if idx < 0:
        continue
    if d.get("busy") is True:
        st = "busy"
    elif d.get("input_locked") is True:
        st = "locked"
    else:
        st = "unlocked"
    print("%d\t%s" % (idx, st))
'
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
  write_auto_parked "$ch"
  clear_request "$ch"
  clear_count "$ch"
  log "parked ${unit} (auto-unpark when this DeckLink input locks)"
  exit 0
}

cmd_unpark_scan() {
  local station_unpark
  station_unpark="$(read_env_key "${STATION_ENV}" AUTO_UNPARK || true)"
  if [ -z "$station_unpark" ]; then
    station_unpark="${AUTO_UNPARK:-true}"
  fi
  if env_bool_false "$station_unpark"; then
    exit 0
  fi

  local polls
  polls="$(unpark_polls_from_env)"
  if [ "$polls" -eq 0 ]; then
    exit 0
  fi

  local max_ch
  max_ch="$(max_channels_from_env)"

  local json
  if ! json="$(fetch_status_json)"; then
    log "WARN: unpark-scan: no status JSON — skip"
    exit 0
  fi

  local locks
  if ! locks="$(printf '%s' "$json" | parse_status_locks)"; then
    local rc=$?
    if [ "$rc" -eq 3 ]; then
      log "unpark-scan: status stale — skip"
    else
      log "WARN: unpark-scan: status parse failed — skip"
    fi
    exit 0
  fi

  local ch
  for ch in $(seq 0 $((max_ch - 1))); do
    local ch_env="${CHANNELS_DIR}/${ch}.env"
    local itype
    itype="$(read_env_key "$ch_env" INPUT_TYPE || true)"
    itype="$(printf '%s' "${itype:-decklink}" | tr '[:upper:]' '[:lower:]')"
    if [ -z "$itype" ]; then
      itype=decklink
    fi
    if [ "$itype" != "decklink" ]; then
      clear_streak "$ch"
      continue
    fi

    local ch_unpark
    ch_unpark="$(read_env_key "$ch_env" AUTO_UNPARK || true)"
    if env_bool_false "$ch_unpark"; then
      clear_streak "$ch"
      continue
    fi

    local dev
    dev="$(read_env_key "$ch_env" DEVICE_NUMBER || true)"
    if ! [[ "$dev" =~ ^[0-9]+$ ]]; then
      dev="$ch"
    fi

    local state=""
    local line idx st
    while IFS=$'\t' read -r idx st; do
      [ -n "$idx" ] || continue
      if [ "$idx" = "$dev" ]; then
        state="$st"
        break
      fi
    done <<<"$locks"

    if [ -z "$state" ]; then
      state="missing"
    fi

    local unit="nexvue-encode@${ch}"
    local active enabled
    active="$(systemctl is-active "$unit" 2>/dev/null || true)"
    enabled="$(systemctl is-enabled "$unit" 2>/dev/null || true)"

    if [ "$active" = "active" ]; then
      write_last "$ch" "$state"
      clear_streak "$ch"
      continue
    fi

    if [ "$state" = "busy" ] || [ "$state" = "missing" ] || [ "$state" = "error" ]; then
      write_last "$ch" "$state"
      clear_streak "$ch"
      continue
    fi

    if [ "$state" = "unlocked" ]; then
      write_last "$ch" "unlocked"
      clear_streak "$ch"
      continue
    fi

    if [ "$state" != "locked" ]; then
      write_last "$ch" "$state"
      clear_streak "$ch"
      continue
    fi

    local last parked
    last="$(read_last "$ch")"
    parked=0
    if has_auto_parked "$ch"; then
      parked=1
    fi

    if [ "$last" != "unlocked" ] && [ "$parked" -eq 0 ]; then
      write_last "$ch" "locked"
      clear_streak "$ch"
      continue
    fi

    local n
    n="$(read_streak "$ch")"
    n=$((n + 1))
    write_streak "$ch" "$n"
    if [ "$n" -lt "$polls" ]; then
      log "device ${dev} ch${ch}: locked ${n}/${polls} (was ${last:-auto-parked}) — waiting"
      continue
    fi

    if [ "$enabled" = "disabled" ] || [ "$enabled" = "disabled-runtime" ]; then
      log "device ${dev} ch${ch}: SDI locked — enable --now ${unit}"
      if systemctl enable --now "$unit"; then
        clear_auto_parked "$ch"
        clear_streak "$ch"
        write_last "$ch" "locked"
      else
        log "WARN: systemctl enable --now ${unit} failed"
      fi
    else
      log "device ${dev} ch${ch}: SDI locked — start ${unit}"
      systemctl reset-failed "$unit" 2>/dev/null || true
      if systemctl start "$unit"; then
        clear_auto_parked "$ch"
        clear_streak "$ch"
        write_last "$ch" "locked"
      else
        log "WARN: systemctl start ${unit} failed"
      fi
    fi
  done
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
  unpark-scan)
    cmd_unpark_scan
    ;;
  *)
    usage
    ;;
esac
