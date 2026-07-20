#!/usr/bin/env bash
###############################################################################
# nexvue-phase1-deploy-verify.sh — post-setup.sh edge checks for Phase 1 deploy
#
# Run on the edge after `sudo ./setup.sh` (and after restarting nexvue-metrics
# if this is the first Temperature-column deploy). Verifies:
#   - metrics SQLite has cpu_temp_c / gpu_temp_c
#   - PHP host API returns temperature fields
#   - Apache docroot has current UI (no obsolete cast.html)
#   - MAX_DEVICES in /etc/nexvue/nexvue.env
#   - encode units use nexvue-encode.sh
#
# Usage:
#   sudo ./nexvue-phase1-deploy-verify.sh
#   sudo nexvue-phase1-deploy-verify.sh   # after setup.sh install
###############################################################################
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
set -euo pipefail

WEBROOT="${NEXVUE_WEBROOT:-/var/www/html}"
METRICS_DB="${NEXVUE_METRICS_DB:-/var/lib/nexvue/metrics.db}"
PASS=0
FAIL=0
WARN=0

ok()   { echo "[ OK ] $*"; PASS=$((PASS + 1)); }
fail() { echo "[FAIL] $*"; FAIL=$((FAIL + 1)); }
warn() { echo "[WARN] $*"; WARN=$((WARN + 1)); }

echo "=== NexVUE Phase 1 deploy verify ($(date -Is)) ==="
echo

# ---- metrics unit + schema --------------------------------------------------
echo "-- nexvue-metrics --"
if systemctl is-active --quiet nexvue-metrics 2>/dev/null; then
  ok "nexvue-metrics is active"
else
  fail "nexvue-metrics is not active — sudo systemctl restart nexvue-metrics"
fi

if [ -f "$METRICS_DB" ]; then
  ok "metrics DB present: $METRICS_DB"
  cols="$(sqlite3 "$METRICS_DB" "PRAGMA table_info(host_samples);" 2>/dev/null || true)"
  if grep -q 'cpu_temp_c' <<<"$cols"; then
    ok "host_samples.cpu_temp_c column exists"
  else
    fail "host_samples.cpu_temp_c missing — restart nexvue-metrics so schema migrates"
  fi
  if grep -q 'gpu_temp_c' <<<"$cols"; then
    ok "host_samples.gpu_temp_c column exists"
  else
    fail "host_samples.gpu_temp_c missing — restart nexvue-metrics so schema migrates"
  fi
  # Latest sample (may be NULL until first post-migration poll).
  row="$(sqlite3 -separator '|' "$METRICS_DB" \
    "SELECT cpu_temp_c, gpu_temp_c FROM host_samples ORDER BY ts DESC LIMIT 1;" 2>/dev/null || true)"
  if [ -n "$row" ]; then
    cpu="${row%%|*}"
    gpu="${row#*|}"
    if [ -n "$cpu" ] && [ "$cpu" != "" ]; then
      ok "latest host sample cpu_temp_c=${cpu}"
    else
      warn "latest cpu_temp_c is NULL — check sysfs hwmon / coretemp after a few polls"
    fi
    if [ -n "$gpu" ] && [ "$gpu" != "" ]; then
      ok "latest host sample gpu_temp_c=${gpu}"
    else
      warn "latest gpu_temp_c is NULL (often OK if iGPU has no hwmon temp)"
    fi
  else
    warn "no host_samples rows yet — wait one poll interval and re-run"
  fi
else
  fail "metrics DB missing at $METRICS_DB"
fi
echo

# ---- PHP API ----------------------------------------------------------------
echo "-- nexvue-metrics.php host series --"
api_url="http://127.0.0.1/nexvue-metrics.php?endpoint=host&range=1h"
if ! command -v curl >/dev/null 2>&1; then
  warn "curl missing — skip PHP API probe"
elif out="$(curl -fsS --max-time 10 "$api_url" 2>/dev/null)"; then
  if grep -q 'cpu_temp_c' <<<"$out"; then
    ok "API host payload includes cpu_temp_c"
  else
    fail "API host payload missing cpu_temp_c — check ${WEBROOT}/nexvue-metrics.php"
  fi
  if grep -q 'gpu_temp_c' <<<"$out"; then
    ok "API host payload includes gpu_temp_c"
  else
    fail "API host payload missing gpu_temp_c"
  fi
else
  fail "curl $api_url failed — is Apache serving ${WEBROOT}?"
fi
echo

# ---- docroot ----------------------------------------------------------------
echo "-- Apache docroot (${WEBROOT}) --"
if [ ! -d "$WEBROOT" ]; then
  fail "WEBROOT missing: $WEBROOT"
else
  for f in index.html multiview.html metrics.html services.html channels.html \
           nexvue-metrics.php nexvue-ops.php nexvue-ui.js nexvue-captions.js; do
    if [ -f "${WEBROOT}/${f}" ]; then
      ok "present ${f}"
    else
      fail "missing ${WEBROOT}/${f} — re-run sudo ./setup.sh"
    fi
  done
  if [ -f "${WEBROOT}/cast.html" ] || [ -f "${WEBROOT}/nexvue-cast.js" ]; then
    warn "obsolete Cast files still in docroot — remove cast.html / nexvue-cast.js"
  else
    ok "no obsolete Cast files in docroot"
  fi
  if grep -q 'TEMP_LIMIT_CPU_C\|Temperature' "${WEBROOT}/metrics.html" 2>/dev/null; then
    ok "metrics.html includes Temperature chart"
  else
    fail "metrics.html lacks Temperature chart — redeploy UI"
  fi
fi
echo

# ---- station config + supervisor -------------------------------------------
echo "-- station config / encode ExecStart --"
if [ -f /etc/nexvue/nexvue.env ]; then
  # shellcheck disable=SC1091
  set +u
  # shellcheck source=/dev/null
  . /etc/nexvue/nexvue.env
  set -u
  md="${MAX_DEVICES:-}"
  if [ "$md" = "8" ]; then
    ok "MAX_DEVICES=8 in /etc/nexvue/nexvue.env (Quad 2)"
  elif [ -n "$md" ]; then
    warn "MAX_DEVICES=${md} in /etc/nexvue/nexvue.env (expected 8 on Quad 2)"
  else
    fail "MAX_DEVICES unset in /etc/nexvue/nexvue.env"
  fi
else
  fail "/etc/nexvue/nexvue.env missing — sudo ./setup.sh"
fi

exec_line="$(systemctl show -p ExecStart --value nexvue-encode@0 2>/dev/null || true)"
if grep -q 'nexvue-encode.sh' <<<"$exec_line"; then
  ok "nexvue-encode@0 ExecStart → nexvue-encode.sh"
else
  fail "nexvue-encode@0 ExecStart is not nexvue-encode.sh — redeploy unit: sudo ./setup.sh && sudo systemctl daemon-reload"
fi
echo

echo "=== summary: ${PASS} ok, ${WARN} warn, ${FAIL} fail ==="
if [ "$FAIL" -gt 0 ]; then
  echo "Deploy verify FAILED. Fix above, then: sudo systemctl restart nexvue-metrics"
  echo "Confirm Temperature chart in the browser Metrics page (CPU °C + 95 °C line)."
  exit 1
fi
echo "Deploy verify passed. Open Metrics → Temperature chart in a browser to confirm UI."
echo "Next: sudo nexvue-phase1-closeout.sh --since 1h   (then 24h / 72h soak)"
exit 0
