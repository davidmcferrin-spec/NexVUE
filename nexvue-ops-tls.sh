#!/usr/bin/env bash
# nexvue-ops-tls.sh — allowlisted TLS status / Let's Encrypt / PEM upload.
#
# Usage:
#   nexvue-ops-tls.sh status
#   nexvue-ops-tls.sh issue    JSON stdin {email,accept_tos} (domain optional/legacy)
#   nexvue-ops-tls.sh renew    timer: no-op unless a Lego cert is due
#   nexvue-ops-tls.sh upload   JSON stdin {cert,key}
#   nexvue-ops-tls.sh run      oneshot body (stop Apache, lego --tls, start Apache)
#
# TLS-ALPN-01 on :443 only — port 80 is never used. Apache is stopped only
# for the challenge window; an EXIT trap always starts it again.
# Do not add apache2 to nexvue-ops-restart.sh; this wrapper is the only
# allowlisted path that may stop/start it.
set -euo pipefail

ETC="${NEXVUE_ETC:-/etc/nexvue}"
DATA="${NEXVUE_DATA:-/var/lib/nexvue}"
ENV_FILE="${NEXVUE_STATION_ENV:-${ETC}/nexvue.env}"
HELPER="${NEXVUE_TLS_PY:-/usr/local/bin/nexvue-tls.py}"
DEPLOY_HOOK="${NEXVUE_TLS_DEPLOY:-/usr/local/bin/nexvue-tls-deploy.sh}"
PYTHON="${NEXVUE_PYTHON:-/usr/bin/python3}"
LEGO="${NEXVUE_LEGO:-/usr/local/bin/lego}"
LEGO_PATH="${NEXVUE_LEGO_PATH:-${DATA}/lego}"
STATE_DIR="${NEXVUE_TLS_STATE_DIR:-${DATA}/tls}"
export NEXVUE_TLS_STATE_DIR="$STATE_DIR"
LOCK="${STATE_DIR}/issue.lock"
LEGO_LOG="${STATE_DIR}/lego.log"
LEGO_LOG_KEEP=5
UNIT="${NEXVUE_TLS_UNIT:-nexvue-tls-issue.service}"
export NEXVUE_TLS_UNIT="$UNIT"
SKIP_APACHE="${NEXVUE_TLS_SKIP_APACHE:-0}"
SKIP_MEDIAMTX="${NEXVUE_TLS_SKIP_MEDIAMTX:-0}"
SYNC="${NEXVUE_TLS_SYNC:-0}"
# Apache stop/start inside the run. Cleanup uses a shorter cap so it
# finishes before systemd's TimeoutStopSec sends SIGKILL.
APACHE_STOP_TIMEOUT="${NEXVUE_TLS_APACHE_STOP_TIMEOUT:-40}"
APACHE_START_TIMEOUT="${NEXVUE_TLS_APACHE_START_TIMEOUT:-40}"
APACHE_CLEANUP_TIMEOUT="${NEXVUE_TLS_APACHE_CLEANUP_TIMEOUT:-20}"

# The EXIT trap runs after cmd_run returns, when that function's locals
# are already gone. Under set -u that killed cleanup on the success path
# (apache_was unbound) and left issue.lock behind. Keep the trap's state
# global. child_pid is the foreground wait target so TERM can interrupt it.
apache_was=0
job_finished=0
lock_owned=0
lego_started=0
fail_reason=""
child_pid=0

json_escape() {
  local s="${1:-}"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\r'/}"
  s="${s//$'\t'/\\t}"
  printf '%s' "$s"
}

fail_json() {
  echo "{\"ok\":false,\"error\":\"$(json_escape "$1")\"}"
  exit 1
}

run_fail() {
  fail_reason="$(trim_reason "$1")"
  fail_json "$fail_reason"
}

trim_reason() {
  local s="${1:-}"
  s="${s//$'\r'/}"
  s="${s//$'\n'/ }"
  if [[ ${#s} -gt 400 ]]; then
    s="${s:0:400}"
  fi
  printf '%s' "$s"
}

tls_py() {
  "$PYTHON" "$HELPER" "$@"
}

json_get() {
  "$PYTHON" -c '
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    print("0")
    raise SystemExit(0)
v = d.get(sys.argv[2])
if isinstance(v, bool):
    print("1" if v else "0")
elif isinstance(v, int):
    print(v)
elif v is None or v == "":
    print("0")
else:
    print(v)
' "$1" "$2"
}

# bash does not run a trap while an external command is in the foreground,
# so a lego that ignores SIGTERM never reaches cleanup and systemd's
# SIGKILL leaves the lock. wait is interruptible.
launch() {
  local log="$1"
  shift
  if [[ -n "$log" ]]; then
    "$@" >"$log" 2>&1 &
  else
    "$@" &
  fi
  child_pid=$!
  local rc=0
  wait "$child_pid" || rc=$?
  child_pid=0
  return "$rc"
}

systemctl_bounded() {
  local secs="$1"
  shift
  local rc=0
  if command -v timeout >/dev/null 2>&1; then
    launch /dev/null timeout --signal=TERM "$secs" systemctl "$@" || rc=$?
  else
    launch /dev/null systemctl "$@" || rc=$?
  fi
  return "$rc"
}

unit_state() {
  local st=""
  if command -v systemctl >/dev/null 2>&1; then
    st="$(systemctl show -p ActiveState --value "$UNIT" 2>/dev/null || true)"
  fi
  st="${st//$'\r'/}"
  st="${st//$'\n'/}"
  printf '%s' "$st"
}

unit_exec_pid() {
  local pid=""
  if command -v systemctl >/dev/null 2>&1; then
    pid="$(systemctl show -p ExecMainPID --value "$UNIT" 2>/dev/null || true)"
    pid="${pid//$'\r'/}"
    pid="${pid//$'\n'/}"
  fi
  if [[ "$pid" =~ ^[0-9]+$ ]]; then
    printf '%s' "$pid"
  else
    printf '0'
  fi
}

# Drop a dead lock and a stale busy flag. recover=1 also signals a worker
# that is still alive past the budget, and stops a unit systemd has not
# reaped. cmd_run passes an empty unit snapshot: this process is the unit,
# and systemctl stop here would kill us.
reconcile_stale() {
  local recover="$1"
  local st="$2"
  local epid="$3"
  local out="" kill_pid=0 stop_unit=0
  out="$(tls_py reconcile --unit-state "$st" --exec-pid "$epid" 2>/dev/null)" || return 0
  [[ -n "$out" ]] || return 0
  [[ "$recover" == "1" ]] || return 0
  kill_pid="$(json_get "$out" kill_pid)"
  stop_unit="$(json_get "$out" stop_unit)"
  if [[ "${kill_pid:-0}" -gt 1 && "$kill_pid" != "$$" ]]; then
    # The worker's own EXIT trap starts Apache (capped at 20s) and removes
    # the lock. SIGKILL before that returns the stuck-button failure.
    kill -TERM "$kill_pid" 2>/dev/null || true
    local i=0
    while [[ "$i" -lt 60 ]]; do
      pid_is_alive "$kill_pid" || break
      sleep 0.5
      i=$((i + 1))
    done
    if pid_is_alive "$kill_pid"; then
      kill -KILL "$kill_pid" 2>/dev/null || true
    fi
  fi
  if [[ "$stop_unit" == "1" ]] && command -v systemctl >/dev/null 2>&1; then
    systemctl_bounded 15 stop "$UNIT" >/dev/null 2>&1 || true
    systemctl reset-failed "$UNIT" >/dev/null 2>&1 || true
  fi
  if [[ "${kill_pid:-0}" -gt 1 || "$stop_unit" == "1" ]]; then
    tls_py reconcile --unit-state "$st" --exec-pid "$epid" >/dev/null 2>&1 || true
  fi
}

request_in_progress() {
  local st="$1"
  local epid="$2"
  local out=""
  out="$(tls_py assess --unit-state "$st" --exec-pid "$epid" 2>/dev/null)" || return 1
  [[ -n "$out" ]] || return 1
  [[ "$(json_get "$out" busy)" == "1" || "$(json_get "$out" live_pid)" == "1" ]]
}

finish_job() {
  local ok="$1" err="$2"
  "$PYTHON" -c '
import json, time, os, sys
p = os.environ["NEXVUE_TLS_STATE_DIR"] + "/job.json"
ok = sys.argv[1] == "1"
err = sys.argv[2][:500]
d = {"busy": False, "action": "issue", "ok": ok, "error": err, "started_at": 0, "finished_at": int(time.time())}
try:
    cur = json.loads(open(p, encoding="utf-8").read())
    if isinstance(cur, dict):
        if cur.get("started_at"):
            d["started_at"] = int(cur["started_at"])
        if cur.get("action"):
            d["action"] = cur["action"]
except Exception:
    pass
os.makedirs(os.path.dirname(p), exist_ok=True)
open(p, "w", encoding="utf-8").write(json.dumps(d) + "\n")
' "$ok" "$err"
}

begin_job() {
  "$PYTHON" -c '
import json, time, os
p = os.environ["NEXVUE_TLS_STATE_DIR"] + "/job.json"
os.makedirs(os.path.dirname(p), exist_ok=True)
d = {"busy": True, "action": "issue", "ok": None, "error": "", "started_at": int(time.time()), "finished_at": 0}
open(p, "w", encoding="utf-8").write(json.dumps(d) + "\n")
'
}

lego_failure_reason() {
  [[ -s "${LEGO_LOG:-}" ]] || return 0
  local line=""
  line="$(grep -E -i 'error|fatal' "$LEGO_LOG" 2>/dev/null | tail -n 1 || true)"
  line="${line#"${line%%[![:space:]]*}"}"
  trim_reason "$line"
}

rotate_lego_log() {
  local i
  rm -f "${LEGO_LOG}.${LEGO_LOG_KEEP}"
  i=$((LEGO_LOG_KEEP - 1))
  while [[ "$i" -ge 1 ]]; do
    if [[ -f "${LEGO_LOG}.${i}" ]]; then
      mv -f "${LEGO_LOG}.${i}" "${LEGO_LOG}.$((i + 1))"
    fi
    i=$((i - 1))
  done
  if [[ -f "$LEGO_LOG" ]]; then
    mv -f "$LEGO_LOG" "${LEGO_LOG}.1"
  fi
}

# kill -0 is true for a zombie. The parent may not have reaped it yet;
# a zombie is not a running request and must not keep the wait loop open.
pid_is_alive() {
  local pid="${1:-0}" stat state
  [[ "$pid" -gt 1 ]] || return 1
  [[ -r "/proc/${pid}/stat" ]] || return 1
  stat="$(cat "/proc/${pid}/stat" 2>/dev/null || true)"
  state="${stat##*) }"
  state="${state%% *}"
  [[ -n "$state" && "$state" != "Z" && "$state" != "X" ]]
}

kill_child() {
  local pid="${child_pid:-0}"
  pid_is_alive "$pid" || { child_pid=0; return 0; }
  kill -TERM "$pid" 2>/dev/null || true
  local i
  for i in 1 2 3 4 5; do
    pid_is_alive "$pid" || break
    sleep 0.2
  done
  if pid_is_alive "$pid"; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
  child_pid=0
}

# Installed before the lock is taken. Always clears our lock and the busy
# flag, including when systemd hits TimeoutStartSec (SIGTERM) or the
# success path has already returned from cmd_run.
cleanup() {
  local ec=$?
  trap '' EXIT TERM INT
  set +e
  set +u
  kill_child
  if [[ "${apache_was:-0}" -eq 1 && "${SKIP_APACHE:-0}" != "1" ]]; then
    systemctl_bounded "${APACHE_CLEANUP_TIMEOUT:-20}" start apache2 >/dev/null 2>&1 || true
  fi
  if [[ "${lego_started:-0}" -eq 1 && -f "${LEGO_LOG:-}" ]]; then
    cat "$LEGO_LOG" >&2 || true
  fi
  if [[ "${lock_owned:-0}" -eq 1 && "${job_finished:-0}" -eq 0 ]]; then
    local why="${fail_reason:-}"
    if [[ -z "$why" && "${lego_started:-0}" -eq 1 ]]; then
      why="$(lego_failure_reason || true)"
    fi
    if [[ "$ec" -eq 0 ]]; then
      finish_job 1 "" || true
    else
      if [[ -z "$why" ]]; then
        case "$ec" in
          143|130|124|137) why="certificate request timed out" ;;
          *) why="certificate request failed" ;;
        esac
      fi
      finish_job 0 "$why" || true
    fi
  fi
  if [[ "${lock_owned:-0}" -eq 1 && -f "${LOCK:-}" ]]; then
    local owner=""
    owner="$(tr -cd '0-9' <"$LOCK" 2>/dev/null || true)"
    if [[ -z "$owner" || "$owner" == "$$" ]]; then
      rm -f "$LOCK"
    fi
  fi
  exit "$ec"
}

on_term() {
  exit "$1"
}

read_env_key() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  local line val
  line="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -n 1 || true)"
  [[ -n "$line" ]] || return 0
  val="${line#*=}"
  val="${val%$'\r'}"
  if [[ ${#val} -ge 2 && ( "${val:0:1}" == '"' || "${val:0:1}" == "'" ) && "${val:0:1}" == "${val: -1}" ]]; then
    val="${val:1:${#val}-2}"
  fi
  printf '%s' "$val"
}

overlay_status() {
  local raw="$1" busy="$2" timer="$3" next="$4"
  "$PYTHON" -c '
import json, sys
d = json.loads(sys.argv[1])
busy = sys.argv[2] == "1"
d["busy"] = busy
job = d.get("job")
if isinstance(job, dict):
    job["busy"] = busy
    d["job"] = job
d["timer_enabled"] = sys.argv[3] == "true"
d["timer_next"] = sys.argv[4]
print(json.dumps(d, separators=(",", ":")))
' "$raw" "$busy" "$timer" "$next"
}

cmd_status() {
  [[ -f "$HELPER" ]] || fail_json "nexvue-tls.py not installed — re-run sudo ./setup.sh"
  local raw timer="false" next="" st pid busy=0
  st="$(unit_state)"
  pid="$(unit_exec_pid)"
  reconcile_stale 0 "$st" "$pid"
  raw="$(tls_py status --unit-state "$st" --exec-pid "$pid")" || fail_json "tls status failed"
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-enabled --quiet nexvue-tls-renew.timer 2>/dev/null; then
      timer="true"
    fi
    next="$(systemctl show -p NextElapseUSecRealtime --value nexvue-tls-renew.timer 2>/dev/null || true)"
  fi
  if request_in_progress "$st" "$pid"; then
    busy=1
  fi
  overlay_status "$raw" "$busy" "$timer" "$next"
}

require_tos() {
  local raw="$1"
  "$PYTHON" -c '
import json, sys
body = json.loads(sys.argv[1] or "{}")
if not isinstance(body, dict):
    raise SystemExit("body must be a JSON object")
if body.get("accept_tos") is not True:
    raise SystemExit("Agree to the Let'\''s Encrypt Subscriber Agreement to issue")
' "$raw" 2>/dev/null || fail_json "Agree to the Let's Encrypt Subscriber Agreement to issue"
}

start_issue_unit() {
  if [[ "$SYNC" == "1" ]]; then
    cmd_run
    return
  fi
  if ! command -v systemctl >/dev/null 2>&1; then
    cmd_run
    return
  fi
  if ! systemctl start --no-block "$UNIT"; then
    fail_json "could not start ${UNIT}"
  fi
  echo '{"ok":true,"started":true,"busy":true,"challenge":"tls-alpn-01"}'
}

cmd_issue() {
  local body cfg_out err st pid
  body="$(cat)"
  require_tos "$body"
  if ! cfg_out="$(printf '%s' "$body" | tls_py config)"; then
    err="$(printf '%s' "$cfg_out" | "$PYTHON" -c 'import json,sys
try:
    d=json.loads(sys.stdin.read()); print(d.get("error","invalid domain or email"))
except Exception:
    print("invalid domain or email")')"
    fail_json "$err"
  fi
  st="$(unit_state)"
  pid="$(unit_exec_pid)"
  reconcile_stale 1 "$st" "$pid"
  if request_in_progress "$(unit_state)" "$(unit_exec_pid)"; then
    fail_json "a certificate request is already running"
  fi
  start_issue_unit
}

cmd_renew() {
  local st pid due
  st="$(unit_state)"
  pid="$(unit_exec_pid)"
  reconcile_stale 1 "$st" "$pid"
  if request_in_progress "$(unit_state)" "$(unit_exec_pid)"; then
    echo '{"ok":true,"skipped":true,"reason":"already running"}'
    return 0
  fi
  due="$(tls_py should-renew)" || fail_json "renew check failed"
  if ! "$PYTHON" -c 'import json,sys; d=json.loads(sys.argv[1]); raise SystemExit(0 if d.get("renew") else 1)' "$due"; then
    echo "$due" | "$PYTHON" -c 'import json,sys; d=json.loads(sys.stdin.read()); print(json.dumps({"ok":True,"skipped":True,"reason":d.get("reason","")}))'
    return 0
  fi
  start_issue_unit
}

cmd_upload() {
  local st pid
  st="$(unit_state)"
  pid="$(unit_exec_pid)"
  reconcile_stale 0 "$st" "$pid"
  if request_in_progress "$st" "$pid"; then
    fail_json "a certificate request is already running"
  fi
  local out
  if ! out="$(tls_py install)"; then
    fail_json "$(printf '%s' "$out" | "$PYTHON" -c 'import json,sys
try:
    d=json.loads(sys.stdin.read())
    print(d.get("error","upload failed"))
except Exception:
    print("upload failed")')"
  fi
  if [[ "$SKIP_APACHE" != "1" ]] && command -v apache2ctl >/dev/null 2>&1; then
    if ! apache2ctl configtest >/dev/null 2>&1; then
      local bak="${STATE_DIR}/backup"
      if [[ -f "${bak}/fullchain.pem" && -f "${bak}/privkey.pem" ]]; then
        cp -f "${bak}/fullchain.pem" "${NEXVUE_TLS_DIR:-/etc/nexvue/tls}/fullchain.pem" 2>/dev/null || true
        cp -f "${bak}/privkey.pem" "${NEXVUE_TLS_DIR:-/etc/nexvue/tls}/privkey.pem" 2>/dev/null || true
      fi
      fail_json "Apache rejected the new certificate — previous pair restored"
    fi
    if systemctl is-active --quiet apache2 2>/dev/null; then
      systemctl reload apache2 || systemctl restart apache2 || fail_json "Apache reload failed after certificate install"
    fi
  fi
  if [[ "$SKIP_MEDIAMTX" != "1" ]] && command -v systemctl >/dev/null 2>&1; then
    systemctl restart mediamtx >/dev/null 2>&1 || true
  fi
  echo "$out"
}

wait_port_free() {
  local i
  for i in $(seq 1 30); do
    if command -v ss >/dev/null 2>&1; then
      if ! ss -lnt 2>/dev/null | grep -qE ':443\s'; then
        return 0
      fi
    else
      return 0
    fi
    sleep 0.4
  done
  return 0
}

cmd_run() {
  apache_was=0
  job_finished=0
  lock_owned=0
  lego_started=0
  fail_reason=""
  child_pid=0
  trap cleanup EXIT
  trap 'on_term 143' TERM
  trap 'on_term 130' INT

  mkdir -p "$STATE_DIR" "$LEGO_PATH" || fail_json "could not create ${STATE_DIR}"

  local domain email
  domain="$(read_env_key NEXVUE_PUBLIC_HOSTNAME)"
  if [[ -z "$domain" ]]; then
    domain="$(read_env_key NEXVUE_TLS_DOMAIN)"
  fi
  email="$(read_env_key NEXVUE_TLS_EMAIL)"
  [[ -n "$domain" ]] || fail_json "Set a Public hostname (Settings → Public reachability)"
  [[ -n "$email" ]] || fail_json "Set an email for Let's Encrypt notices"
  [[ -x "$LEGO" || -f "$LEGO" ]] || fail_json "lego is not installed — re-run sudo ./setup.sh"

  # Empty unit snapshot: we are the oneshot. Stopping $UNIT would kill us.
  # issue/renew already stopped a stuck unit before starting this one.
  reconcile_stale 1 "" "0"
  if request_in_progress "" "0"; then
    fail_json "a certificate request is already running"
  fi

  echo "$$" >"$LOCK"
  lock_owned=1
  begin_job

  if [[ "$SKIP_APACHE" != "1" ]] && command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet apache2 2>/dev/null; then
      apache_was=1
      if ! systemctl_bounded "$APACHE_STOP_TIMEOUT" stop apache2; then
        run_fail "could not stop Apache to free port 443"
      fi
      wait_port_free
    fi
  fi

  rotate_lego_log
  : >"$LEGO_LOG"
  chmod 600 "$LEGO_LOG" 2>/dev/null || true
  lego_started=1
  local lec=0
  launch "$LEGO_LOG" "$LEGO" run \
    --email "$email" \
    --domains "$domain" \
    --tls \
    --path "$LEGO_PATH" \
    --accept-tos \
    --deploy-hook "$DEPLOY_HOOK" || lec=$?
  if [[ "$lec" -ne 0 ]]; then
    local why
    why="$(lego_failure_reason || true)"
    run_fail "${why:-lego failed}"
  fi

  if [[ "$SKIP_APACHE" != "1" && "$apache_was" -eq 1 ]]; then
    if ! systemctl_bounded "$APACHE_START_TIMEOUT" start apache2; then
      run_fail "lego succeeded but Apache failed to start"
    fi
    apache_was=0
  fi
  if [[ "$SKIP_MEDIAMTX" != "1" ]] && command -v systemctl >/dev/null 2>&1; then
    systemctl_bounded 20 restart mediamtx >/dev/null 2>&1 || true
  fi
  finish_job 1 ""
  job_finished=1
  echo '{"ok":true,"issued":true,"challenge":"tls-alpn-01"}'
}

CMD="${1:-}"
case "$CMD" in
  status) cmd_status ;;
  issue)  cmd_issue ;;
  renew)  cmd_renew ;;
  upload) cmd_upload ;;
  run)    cmd_run ;;
  *)      fail_json "usage: nexvue-ops-tls.sh status|issue|renew|upload|run" ;;
esac
