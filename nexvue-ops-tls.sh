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
LOCK="${STATE_DIR}/issue.lock"
UNIT="${NEXVUE_TLS_UNIT:-nexvue-tls-issue.service}"
SKIP_APACHE="${NEXVUE_TLS_SKIP_APACHE:-0}"
SKIP_MEDIAMTX="${NEXVUE_TLS_SKIP_MEDIAMTX:-0}"
SYNC="${NEXVUE_TLS_SYNC:-0}"

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

tls_py() {
  "$PYTHON" "$HELPER" "$@"
}

busy_now() {
  if [[ -f "$LOCK" ]]; then
    return 0
  fi
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet "$UNIT" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
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

cmd_status() {
  [[ -f "$HELPER" ]] || fail_json "nexvue-tls.py not installed — re-run sudo ./setup.sh"
  local raw timer="false" next=""
  raw="$(tls_py status)" || fail_json "tls status failed"
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-enabled --quiet nexvue-tls-renew.timer 2>/dev/null; then
      timer="true"
    fi
    next="$(systemctl show -p NextElapseUSecRealtime --value nexvue-tls-renew.timer 2>/dev/null || true)"
  fi
  if busy_now; then
    "$PYTHON" -c '
import json, sys
d = json.loads(sys.argv[1])
d["busy"] = True
d["timer_enabled"] = sys.argv[2] == "true"
d["timer_next"] = sys.argv[3]
print(json.dumps(d, separators=(",", ":")))
' "$raw" "$timer" "${next}"
  else
    "$PYTHON" -c '
import json, sys
d = json.loads(sys.argv[1])
d["timer_enabled"] = sys.argv[2] == "true"
d["timer_next"] = sys.argv[3]
print(json.dumps(d, separators=(",", ":")))
' "$raw" "$timer" "${next}"
  fi
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
  local body cfg_out err
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
  if busy_now; then
    fail_json "a certificate request is already running"
  fi
  start_issue_unit
}

cmd_renew() {
  if busy_now; then
    echo '{"ok":true,"skipped":true,"reason":"already running"}'
    return 0
  fi
  local due
  due="$(tls_py should-renew)" || fail_json "renew check failed"
  if ! "$PYTHON" -c 'import json,sys; d=json.loads(sys.argv[1]); raise SystemExit(0 if d.get("renew") else 1)' "$due"; then
    echo "$due" | "$PYTHON" -c 'import json,sys; d=json.loads(sys.stdin.read()); print(json.dumps({"ok":True,"skipped":True,"reason":d.get("reason","")}))'
    return 0
  fi
  start_issue_unit
}

cmd_upload() {
  if busy_now; then
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
  mkdir -p "$STATE_DIR" "$LEGO_PATH"
  if [[ -f "$LOCK" ]]; then
    fail_json "a certificate request is already running"
  fi
  echo "$$" > "$LOCK"
  local apache_was=0
  local finished=0
  "$PYTHON" -c '
import json, time, os, sys
p = os.environ.get("NEXVUE_TLS_STATE_DIR", "/var/lib/nexvue/tls") + "/job.json"
d = {"busy": True, "action": "issue", "ok": None, "error": "", "started_at": int(time.time()), "finished_at": 0}
open(p, "w", encoding="utf-8").write(json.dumps(d) + "\n")
'

  finish_job() {
    local ok="$1" err="$2"
    "$PYTHON" -c '
import json, time, os, sys
p = os.environ.get("NEXVUE_TLS_STATE_DIR", "/var/lib/nexvue/tls") + "/job.json"
ok = sys.argv[1] == "1"
err = sys.argv[2]
d = {"busy": False, "action": "issue", "ok": ok, "error": err, "started_at": 0, "finished_at": int(time.time())}
try:
    cur = json.loads(open(p, encoding="utf-8").read())
    if isinstance(cur, dict) and cur.get("started_at"):
        d["started_at"] = cur["started_at"]
except Exception:
    pass
open(p, "w", encoding="utf-8").write(json.dumps(d) + "\n")
' "$ok" "$err"
  }

  cleanup() {
    local ec=$?
    if [[ "$apache_was" -eq 1 && "$SKIP_APACHE" != "1" ]]; then
      systemctl start apache2 >/dev/null 2>&1 || true
    fi
    if [[ "$finished" -eq 0 ]]; then
      if [[ "$ec" -eq 0 ]]; then
        finish_job 1 ""
      else
        finish_job 0 "certificate request failed"
      fi
    fi
    rm -f "$LOCK"
  }
  trap cleanup EXIT

  local domain email
  domain="$(read_env_key NEXVUE_PUBLIC_HOSTNAME)"
  if [[ -z "$domain" ]]; then
    domain="$(read_env_key NEXVUE_TLS_DOMAIN)"
  fi
  email="$(read_env_key NEXVUE_TLS_EMAIL)"
  [[ -n "$domain" ]] || fail_json "Set a Public hostname (Settings → Public reachability)"
  [[ -n "$email" ]] || fail_json "Set an email for Let's Encrypt notices"
  [[ -x "$LEGO" || -f "$LEGO" ]] || fail_json "lego is not installed — re-run sudo ./setup.sh"

  if [[ "$SKIP_APACHE" != "1" ]] && command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet apache2 2>/dev/null; then
      apache_was=1
      systemctl stop apache2 || fail_json "could not stop Apache to free port 443"
      wait_port_free
    fi
  fi

  local log
  log="$(mktemp)"
  set +e
  "$LEGO" run \
    --email "$email" \
    --domains "$domain" \
    --tls \
    --path "$LEGO_PATH" \
    --accept-tos \
    --deploy-hook "$DEPLOY_HOOK" \
    >"$log" 2>&1
  local lec=$?
  set -e
  local tail
  tail="$(tr -d '\r' <"$log" | tail -n 8 | tr '\n' ' ')"
  rm -f "$log"
  if [[ "$lec" -ne 0 ]]; then
    finished=1
    finish_job 0 "${tail:-lego failed}"
    fail_json "${tail:-lego failed}"
  fi

  if [[ "$SKIP_APACHE" != "1" && "$apache_was" -eq 1 ]]; then
    systemctl start apache2 || fail_json "lego succeeded but Apache failed to start"
    apache_was=0
  fi
  if [[ "$SKIP_MEDIAMTX" != "1" ]] && command -v systemctl >/dev/null 2>&1; then
    systemctl restart mediamtx >/dev/null 2>&1 || true
  fi
  finished=1
  finish_job 1 ""
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
