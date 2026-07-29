#!/usr/bin/env bash
###############################################################################
# setup.sh — NexVUE edge node installer
#
# Idempotent: safe to re-run after fixing a failed step or after a reboot.
# Run from the repo root as root:
#
#   sudo ./setup.sh            full install + sanity checks
#   sudo ./setup.sh --check    sanity checks only (e.g. after HWE reboot or
#                              after installing Desktop Video)
#   sudo ./setup.sh --firewall install, then open Phase 1 ufw ports (does not
#                              enable ufw for you — see note at that step)
#
# Python policy: NexVUE uses stdlib only — no pip, ever. Any future Python
# dependency must come from apt (python3-<package>).
#
# What this script canNOT do (license-gated downloads, prompted manually):
#   - Blackmagic Desktop Video driver  (required for capture)
#   - Blackmagic DeckLink SDK          (required to build decklink-status)
###############################################################################
# Must run under bash (uses pipefail, [[ ]], arrays). Re-exec under bash if
# launched via sh/dash so failures are clear, not "Illegal option -o pipefail".
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
set -euo pipefail

# ---- Output helpers -------------------------------------------------------------
GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
ok()   { echo "${GREEN}[ OK ]${RESET} $*"; }
warn() { echo "${YELLOW}[WARN]${RESET} $*"; WARNINGS+=("$*"); }
fail() { echo "${RED}[FAIL]${RESET} $*"; exit 1; }
step() { echo; echo "=== $* ==="; }
WARNINGS=()

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK_ONLY=false
APPLY_FIREWALL=false
for arg in "$@"; do
  case "$arg" in
    --check)    CHECK_ONLY=true ;;
    --firewall) APPLY_FIREWALL=true ;;
  esac
done

[ "$(id -u)" -eq 0 ] || fail "run as root: sudo ./setup.sh"

# ---- Required repo files (verify before touching the system) ---------------------
REQUIRED_FILES=(
  mediamtx.yml mediamtx.service
  nexvue-encode.sh nexvue-supervisor.py nexvue-encode@.service
  nexvue-encode-auto-park.sh
  nexvue-decklink-configure.service
  decklink-configure.cpp
  nexvue-status-server.py nexvue-status.service
  nexvue-metrics-server.py nexvue-metrics.service
  nexvue-metrics.php nexvue-status.php nexvue-mediamtx-api.php
  nexvue-captions.php nexvue-captions.js
  nexvue-qr.js nexvue-ui.js nexvue-vu.js nexvue-logo.php chart.umd.min.js
  metrics.html index.html multiview.html
  nexvue-ops.php services.html channels.html
  nexvue-auth-lib.php nexvue-auth.php nexvue-jwks.php nexvue-auth-bootstrap.php
  nexvue-auth-gate.js nexvue-share-ui.js
  nexvue-mediamtx-jwt-patch.py nexvue-jwks-loopback.conf
  login.html forgot.html reset.html users.html
  nexvue-captions-decode.py nexvue-captions-probe.sh
  nexvue-phase1-closeout.sh
  nexvue-phase1-deploy-verify.sh
  nexvue-encode-storm-diagnose.sh
  nexvue-ops-env-update.py nexvue-ops.sudoers
  nexvue-ops-status.sh nexvue-ops-journal.sh
  nexvue-ops-env-read.sh nexvue-ops-env-write.sh nexvue-ops-restart.sh
  nexvue-ops-enable.sh nexvue-ops-audio-probe.sh
  nexvue-ops-support-bundle.sh nexvue-support-bundle.py
  nexvue-ops-update.sh
  nexvue-version.php
  VERSION
  channels-example.env
  nexvue-example.env
)
if ! $CHECK_ONLY; then
  for f in "${REQUIRED_FILES[@]}"; do
    [ -f "${REPO_DIR}/${f}" ] || fail "missing ${f} — run from the repo root"
  done
fi

###############################################################################
# Install phases (skipped with --check)
###############################################################################
if ! $CHECK_ONLY; then

step "1/5 APT packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# HWE kernel: required for Arrow Lake (Core Ultra 200S) iGPU; harmless on
# older platforms. If this pulls a new kernel, a reboot is required before
# vah264enc will work — the summary at the end will say so.
KERNEL_BEFORE="$(uname -r)"
apt-get install -y -qq \
  linux-generic-hwe-24.04 \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
  intel-media-va-driver-non-free vainfo intel-gpu-tools \
  build-essential curl ca-certificates jq openssl \
  apache2 libapache2-mod-php php-cli php-sqlite3 \
  python3-gi python3-gst-1.0 gir1.2-glib-2.0 gir1.2-gstreamer-1.0 \
  gir1.2-gst-plugins-base-1.0
ok "apt packages installed (python: stdlib + apt-only python3-gi/python3-gst-1.0 for the Phase 1.5 supervisor — never pip; Apache + php-cli/sqlite3 for login/auth + metrics.php)"

# PHP under Apache (login / ops / metrics). Idempotent: enable whatever
# versioned mod_php apt just installed, then reload if Apache is running.
if command -v a2enmod >/dev/null 2>&1; then
  php_mod_enabled=false
  for load in /etc/apache2/mods-available/php*.load; do
    [ -f "$load" ] || continue
    m="$(basename "$load" .load)"
    if a2enmod "$m" >/dev/null 2>&1; then
      ok "Apache module enabled: $m"
      php_mod_enabled=true
      break
    fi
  done
  $php_mod_enabled || warn "no php*.load under /etc/apache2/mods-available — login JSON APIs will 500 until libapache2-mod-php is enabled"
  if systemctl is-active --quiet apache2 2>/dev/null; then
    systemctl reload apache2 >/dev/null 2>&1 \
      && ok "apache2 reloaded" \
      || warn "apache2 reload failed — restart manually after setup if login fails"
  else
    systemctl enable --now apache2 >/dev/null 2>&1 \
      && ok "apache2 enabled and started" \
      || warn "could not start apache2 — start it before using /login.html"
  fi
else
  warn "a2enmod missing — install apache2 + libapache2-mod-php for the web UI"
fi

# Allow the metrics collector (user nexvue) to read iGPU PMU without root.
# AmbientCapabilities on the unit also requests CAP_PERFMON; setcap covers
# the case where the binary is invoked directly during bring-up checks.
if command -v setcap >/dev/null 2>&1 && [ -x /usr/bin/intel_gpu_top ]; then
  setcap cap_perfmon,cap_sys_admin+ep /usr/bin/intel_gpu_top 2>/dev/null \
    || setcap cap_sys_admin+ep /usr/bin/intel_gpu_top 2>/dev/null \
    || warn "setcap on intel_gpu_top failed — iGPU metrics may need CAP_PERFMON on nexvue-metrics.service"
else
  warn "intel_gpu_top missing or setcap unavailable — iGPU Metrics charts stay empty until intel-gpu-tools is installed"
fi

step "2/5 MediaMTX"
if command -v mediamtx >/dev/null || [ -x /usr/local/bin/mediamtx ]; then
  ok "mediamtx already installed: $(/usr/local/bin/mediamtx --version 2>/dev/null || echo present)"
else
  TAG="$(curl -fsSL https://api.github.com/repos/bluenviron/mediamtx/releases/latest | jq -r .tag_name)"
  [ -n "${TAG}" ] && [ "${TAG}" != "null" ] || fail "could not resolve latest MediaMTX release tag"
  URL="https://github.com/bluenviron/mediamtx/releases/download/${TAG}/mediamtx_${TAG}_linux_amd64.tar.gz"
  TMP="$(mktemp -d)"
  curl -fsSL "${URL}" -o "${TMP}/mediamtx.tar.gz"
  tar -C "${TMP}" -xzf "${TMP}/mediamtx.tar.gz" mediamtx
  install -m 755 "${TMP}/mediamtx" /usr/local/bin/mediamtx
  rm -rf "${TMP}"
  ok "mediamtx ${TAG} installed"
fi

step "3/5 Service user & directories"
if ! id nexvue >/dev/null 2>&1; then
  useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin nexvue
  ok "created system user 'nexvue'"
else
  ok "user 'nexvue' exists"
fi
install -d -m 755 /etc/nexvue/channels
# Player aliases reads these as www-data (no sudo) — keep world-readable.
chmod 755 /etc/nexvue /etc/nexvue/channels 2>/dev/null || true
chmod 644 /etc/nexvue/channels/*.env 2>/dev/null || true
chmod 644 /etc/nexvue/nexvue.env 2>/dev/null || true
ok "/etc/nexvue/channels ready (644 .env for www-data alias reads)"

# Station-wide /etc/nexvue/nexvue.env (MAX_DEVICES etc.). Never overwrite a live
# file. When absent, migrate a consistent legacy MAX_DEVICES from channel envs,
# or install the example default (8). Conflicting legacy values → warn and
# leave absent so legacy channel copies keep working until the operator picks.
if [ -f /etc/nexvue/nexvue.env ]; then
  ok "/etc/nexvue/nexvue.env exists — left untouched"
else
  migrate_val=""
  migrate_conflict=false
  for f in /etc/nexvue/channels/*.env; do
    [ -f "$f" ] || continue
    v="$(grep -E '^[[:space:]]*MAX_DEVICES=' "$f" 2>/dev/null | tail -1 \
      | cut -d= -f2- || true)"
    v="${v%%#*}"
    v="${v//\"/}"
    v="${v//\'/}"
    v="${v// /}"
    v="${v//$'\t'/}"
    [ -n "$v" ] || continue
    if ! [[ "$v" =~ ^[1-8]$ ]]; then
      warn "legacy MAX_DEVICES='${v}' in ${f} is invalid — ignoring for migration"
      continue
    fi
    if [ -z "$migrate_val" ]; then
      migrate_val="$v"
    elif [ "$migrate_val" != "$v" ]; then
      migrate_conflict=true
    fi
  done
  if $migrate_conflict; then
    warn "conflicting legacy MAX_DEVICES across channel envs — not creating /etc/nexvue/nexvue.env; set MAX_DEVICES=N there manually (1–8)"
  else
    if [ -z "$migrate_val" ]; then
      install -m 644 "${REPO_DIR}/nexvue-example.env" /etc/nexvue/nexvue.env
      ok "installed /etc/nexvue/nexvue.env (MAX_DEVICES=8 default)"
    else
      {
        echo "# Migrated from channel .env by setup.sh ($(date -Is))"
        echo "MAX_DEVICES=${migrate_val}"
      } > /etc/nexvue/nexvue.env
      chmod 644 /etc/nexvue/nexvue.env
      ok "migrated MAX_DEVICES=${migrate_val} into /etc/nexvue/nexvue.env"
    fi
  fi
fi

step "4/5 NexVUE files"
# Config: never clobber a live config — install only if absent.
if [ -f /etc/nexvue/mediamtx.yml ]; then
  ok "/etc/nexvue/mediamtx.yml exists — left untouched (diff against repo manually)"
else
  install -m 644 "${REPO_DIR}/mediamtx.yml" /etc/nexvue/mediamtx.yml
  ok "installed mediamtx.yml"
fi
install -m 755 "${REPO_DIR}/nexvue-encode.sh" /usr/local/bin/nexvue-encode.sh
install -m 755 "${REPO_DIR}/nexvue-encode-auto-park.sh" /usr/local/bin/nexvue-encode-auto-park.sh
install -m 755 "${REPO_DIR}/nexvue-supervisor.py" /usr/local/bin/nexvue-supervisor.py
install -m 755 "${REPO_DIR}/nexvue-status-server.py" /usr/local/bin/nexvue-status-server.py
install -m 755 "${REPO_DIR}/nexvue-metrics-server.py" /usr/local/bin/nexvue-metrics-server.py
install -m 755 "${REPO_DIR}/nexvue-captions-decode.py" /usr/local/bin/nexvue-captions-decode.py
install -m 755 "${REPO_DIR}/nexvue-captions-probe.sh" /usr/local/bin/nexvue-captions-probe.sh
install -m 755 "${REPO_DIR}/nexvue-phase1-closeout.sh" /usr/local/bin/nexvue-phase1-closeout.sh
install -m 755 "${REPO_DIR}/nexvue-phase1-deploy-verify.sh" /usr/local/bin/nexvue-phase1-deploy-verify.sh
install -m 755 "${REPO_DIR}/nexvue-encode-storm-diagnose.sh" /usr/local/bin/nexvue-encode-storm-diagnose.sh
install -m 755 "${REPO_DIR}/nexvue-ops-env-update.py" /usr/local/bin/nexvue-ops-env-update.py
install -m 755 "${REPO_DIR}/nexvue-ops-status.sh" /usr/local/bin/nexvue-ops-status.sh
install -m 755 "${REPO_DIR}/nexvue-ops-journal.sh" /usr/local/bin/nexvue-ops-journal.sh
install -m 755 "${REPO_DIR}/nexvue-ops-env-read.sh" /usr/local/bin/nexvue-ops-env-read.sh
install -m 755 "${REPO_DIR}/nexvue-ops-env-write.sh" /usr/local/bin/nexvue-ops-env-write.sh
install -m 755 "${REPO_DIR}/nexvue-ops-restart.sh" /usr/local/bin/nexvue-ops-restart.sh
install -m 755 "${REPO_DIR}/nexvue-ops-enable.sh" /usr/local/bin/nexvue-ops-enable.sh
install -m 755 "${REPO_DIR}/nexvue-ops-audio-probe.sh" /usr/local/bin/nexvue-ops-audio-probe.sh
install -m 755 "${REPO_DIR}/nexvue-ops-support-bundle.sh" /usr/local/bin/nexvue-ops-support-bundle.sh
install -m 755 "${REPO_DIR}/nexvue-support-bundle.py" /usr/local/bin/nexvue-support-bundle.py
install -m 755 "${REPO_DIR}/nexvue-ops-update.sh" /usr/local/bin/nexvue-ops-update.sh
# Support-bundle zip staging (www-data must read finished zips).
install -d -m 750 -o root -g www-data /var/lib/nexvue/support 2>/dev/null \
  || install -d -m 750 /var/lib/nexvue/support
chgrp www-data /var/lib/nexvue/support 2>/dev/null || true
chmod 750 /var/lib/nexvue/support 2>/dev/null || true
ok "support bundle dir: /var/lib/nexvue/support"
# Remember clone path for Services → Update from repo.
printf '%s\n' "${REPO_DIR}" > /etc/nexvue/repo.path
chmod 644 /etc/nexvue/repo.path
ok "repo.path → ${REPO_DIR}"
# Version stamp (nav badge + support).
install -d -m 755 /usr/local/share/nexvue
install -m 644 "${REPO_DIR}/VERSION" /usr/local/share/nexvue/VERSION
VER_STR="$(tr -d '[:space:]' < "${REPO_DIR}/VERSION" 2>/dev/null || echo 0.0.0)"
GIT_SHA=""
GIT_FULL=""
GIT_BR=""
if [ -d "${REPO_DIR}/.git" ]; then
  GIT_SHA="$(git -C "${REPO_DIR}" rev-parse --short=12 HEAD 2>/dev/null || true)"
  GIT_FULL="$(git -C "${REPO_DIR}" rev-parse HEAD 2>/dev/null || true)"
  GIT_BR="$(git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
fi
TS_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat > /var/lib/nexvue/version.json <<EOF
{
  "version": "${VER_STR}",
  "git_sha": "${GIT_SHA}",
  "git_full": "${GIT_FULL}",
  "git_branch": "${GIT_BR}",
  "repo": "${REPO_DIR}",
  "updated_at": "${TS_UTC}"
}
EOF
chmod 644 /var/lib/nexvue/version.json
ok "version ${VER_STR}${GIT_SHA:+ (${GIT_SHA})}"
# Caption JSON state (encode writes; Apache/www-data reads via nexvue-captions.php).
install -d -m 755 -o nexvue -g nexvue /run/nexvue/captions 2>/dev/null \
  || mkdir -p /run/nexvue/captions
chown nexvue:nexvue /run/nexvue/captions 2>/dev/null || true
chmod 755 /run/nexvue/captions 2>/dev/null || true
install -m 644 "${REPO_DIR}/mediamtx.service" \
               "${REPO_DIR}/nexvue-encode@.service" \
               "${REPO_DIR}/nexvue-decklink-configure.service" \
               "${REPO_DIR}/nexvue-status.service" \
               "${REPO_DIR}/nexvue-metrics.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable nexvue-decklink-configure.service >/dev/null 2>&1 \
  && ok "enabled nexvue-decklink-configure.service (half-duplex BNCs before encode)" \
  || warn "could not enable nexvue-decklink-configure.service"
# Pick up status bind default (127.0.0.1) + unit file changes.
if systemctl is-active --quiet nexvue-status 2>/dev/null; then
  systemctl try-restart nexvue-status >/dev/null 2>&1 \
    && ok "nexvue-status restarted (loopback :9998)" \
    || warn "nexvue-status restart failed — sudo systemctl restart nexvue-status"
fi
ok "scripts + units installed, systemd reloaded"

# Ops UI sudoers — validate before installing (a bad drop-in breaks sudo).
if command -v visudo >/dev/null; then
  TMP_SUDOERS="$(mktemp)"
  # visudo -cf needs the final path form; stage then install.
  install -m 440 "${REPO_DIR}/nexvue-ops.sudoers" "${TMP_SUDOERS}"
  if visudo -cf "${TMP_SUDOERS}" >/dev/null 2>&1; then
    install -m 440 "${REPO_DIR}/nexvue-ops.sudoers" /etc/sudoers.d/nexvue-ops
    ok "sudoers drop-in installed: /etc/sudoers.d/nexvue-ops"
  else
    warn "nexvue-ops.sudoers failed visudo -cf — NOT installed; Services/Settings pages will not work until fixed"
  fi
  rm -f "${TMP_SUDOERS}"
else
  warn "visudo not found — copy nexvue-ops.sudoers to /etc/sudoers.d/nexvue-ops manually (mode 0440)"
fi

# Station branding logo storage (www-data writes via nexvue-ops.php logo_*).
install -d -m 750 -o www-data -g www-data /var/lib/nexvue/branding 2>/dev/null \
  || install -d -m 750 /var/lib/nexvue/branding
if id www-data >/dev/null 2>&1; then
  chown www-data:www-data /var/lib/nexvue/branding 2>/dev/null || true
  chmod 750 /var/lib/nexvue/branding 2>/dev/null || true
fi

# Local auth store (www-data RW) + keypair / JWKS / bootstrap admin + publish JWT.
# DB path is /var/lib/nexvue/auth/auth.db (inside the www-data dir) so Apache can
# create SQLite WAL sidecars. Legacy /var/lib/nexvue/auth.db is migrated here.
install -d -m 755 /usr/local/share/nexvue
install -d -m 755 /var/lib/nexvue
install -d -m 750 -o www-data -g www-data /var/lib/nexvue/auth 2>/dev/null \
  || install -d -m 750 /var/lib/nexvue/auth
if id www-data >/dev/null 2>&1; then
  chown www-data:www-data /var/lib/nexvue/auth 2>/dev/null || true
  chmod 750 /var/lib/nexvue/auth 2>/dev/null || true
fi
# Migrate legacy DB sitting next to metrics.db (parent dir is often nexvue:nexvue
# 0755 — www-data cannot create auth.db-wal there → "auth store unavailable").
if [ -f /var/lib/nexvue/auth.db ] && [ ! -f /var/lib/nexvue/auth/auth.db ]; then
  mv /var/lib/nexvue/auth.db /var/lib/nexvue/auth/auth.db
  for side in /var/lib/nexvue/auth.db-*; do
    [ -e "$side" ] || continue
    mv "$side" "/var/lib/nexvue/auth/$(basename "$side")"
  done
  ok "migrated auth.db → /var/lib/nexvue/auth/auth.db"
fi
install -m 644 "${REPO_DIR}/nexvue-auth-lib.php" /usr/local/share/nexvue/nexvue-auth-lib.php
install -m 755 "${REPO_DIR}/nexvue-auth-bootstrap.php" /usr/local/bin/nexvue-auth-bootstrap.php
if command -v php >/dev/null 2>&1; then
  if php "${REPO_DIR}/nexvue-auth-bootstrap.php"; then
    ok "auth bootstrap (auth.db + JWKS + seed admin + NEXVUE_PUBLISH_JWT)"
  else
    warn "auth bootstrap failed — run: sudo php ${REPO_DIR}/nexvue-auth-bootstrap.php"
  fi
  if id www-data >/dev/null 2>&1; then
    chown -R www-data:www-data /var/lib/nexvue/auth 2>/dev/null || true
    chmod 750 /var/lib/nexvue/auth 2>/dev/null || true
    chmod 640 /var/lib/nexvue/auth/private.pem 2>/dev/null || true
    chmod 644 /var/lib/nexvue/auth/public.pem /var/lib/nexvue/auth/jwks.json \
      /var/lib/nexvue/auth/kid /var/lib/nexvue/auth/auth.db 2>/dev/null || true
    # WAL sidecars if present from bootstrap.
    chmod 644 /var/lib/nexvue/auth/auth.db-* 2>/dev/null || true
    # Smoke: Apache user must open/migrate/write the store (login uses this path).
    AUTH_SMOKE_PHP='
      require "'"${REPO_DIR}"'/nexvue-auth-lib.php";
      auth_migrate();
      auth_ensure_keys();
      $n = (int)auth_db()->querySingle("SELECT COUNT(*) FROM users");
      if ($n < 1) { fwrite(STDERR, "no users\n"); exit(1); }
      echo "users=$n\n";
    '
    auth_smoke_ok=false
    if command -v runuser >/dev/null 2>&1; then
      runuser -u www-data -- php -r "${AUTH_SMOKE_PHP}" >/dev/null 2>&1 && auth_smoke_ok=true
    fi
    if ! $auth_smoke_ok; then
      sudo -u www-data php -r "${AUTH_SMOKE_PHP}" >/dev/null 2>&1 && auth_smoke_ok=true
    fi
    if $auth_smoke_ok; then
      ok "auth store writable by www-data (/var/lib/nexvue/auth)"
    else
      warn "auth store NOT usable as www-data — login will show 'auth store unavailable'; fix ownership on /var/lib/nexvue/auth and re-run setup"
    fi
  fi
else
  warn "php missing — cannot bootstrap auth.db / JWKS"
fi

# Per-unit Services journal clear watermarks (root writes via sudo journal wrapper).
install -d -m 755 /var/lib/nexvue/journal-cleared

# Apache docroot: player, multiviewer, metrics, ops pages + PHP.
# Override with NEXVUE_WEBROOT if the site isn't under /var/www/html.
WEBROOT="${NEXVUE_WEBROOT:-/var/www/html}"
if [ -d "${WEBROOT}" ]; then
  install -m 644 "${REPO_DIR}/index.html" \
                 "${REPO_DIR}/multiview.html" \
                 "${REPO_DIR}/metrics.html" \
                 "${REPO_DIR}/nexvue-metrics.php" \
                 "${REPO_DIR}/nexvue-status.php" \
                 "${REPO_DIR}/nexvue-mediamtx-api.php" \
                 "${REPO_DIR}/nexvue-captions.php" \
                 "${REPO_DIR}/nexvue-captions.js" \
                 "${REPO_DIR}/nexvue-qr.js" \
                 "${REPO_DIR}/nexvue-ui.js" \
                 "${REPO_DIR}/nexvue-vu.js" \
                 "${REPO_DIR}/nexvue-logo.php" \
                 "${REPO_DIR}/nexvue-version.php" \
                 "${REPO_DIR}/VERSION" \
                 "${REPO_DIR}/chart.umd.min.js" \
                 "${REPO_DIR}/services.html" \
                 "${REPO_DIR}/channels.html" \
                 "${REPO_DIR}/nexvue-ops.php" \
                 "${REPO_DIR}/nexvue-auth-lib.php" \
                 "${REPO_DIR}/nexvue-auth.php" \
                 "${REPO_DIR}/nexvue-jwks.php" \
                 "${REPO_DIR}/nexvue-auth-gate.js" \
                 "${REPO_DIR}/nexvue-share-ui.js" \
                 "${REPO_DIR}/login.html" \
                 "${REPO_DIR}/forgot.html" \
                 "${REPO_DIR}/reset.html" \
                 "${REPO_DIR}/users.html" \
                 "${WEBROOT}/"
  ok "web UI installed to ${WEBROOT} (player / multiview / metrics / services / channels / auth / captions / branding)"
else
  warn "Apache docroot ${WEBROOT} missing — after Apache is up: sudo cp index.html multiview.html metrics.html … login.html users.html nexvue-auth*.php ${WEBROOT}/"
fi

install -m 644 "${REPO_DIR}/nexvue-mediamtx-jwt-patch.py" /usr/local/share/nexvue/nexvue-mediamtx-jwt-patch.py
install -m 644 "${REPO_DIR}/nexvue-jwks-loopback.conf" /usr/local/share/nexvue/nexvue-jwks-loopback.conf

# MediaMTX JWT needs a reachable JWKS. HTTPS-only Apache often 301s :80 → :443,
# which breaks MediaMTX's JWKS fetch and kills both publish and WHEP. Prefer a
# localhost-only :9080 vhost; fall back to https://127.0.0.1 + fingerprint.
JWKS_URL=""
JWKS_FP=""
if [ -d /etc/apache2/conf-available ]; then
  install -m 644 "${REPO_DIR}/nexvue-jwks-loopback.conf" \
    /etc/apache2/conf-available/nexvue-jwks-loopback.conf
  if command -v a2enconf >/dev/null 2>&1; then
    a2enconf nexvue-jwks-loopback >/dev/null 2>&1 || true
  fi
  if systemctl is-active --quiet apache2 2>/dev/null; then
    systemctl reload apache2 >/dev/null 2>&1 \
      || systemctl restart apache2 >/dev/null 2>&1 \
      || warn "apache2 reload failed after enabling JWKS loopback :9080"
  fi
fi
if curl -fsS --max-time 3 "http://127.0.0.1:9080/nexvue-jwks.php" 2>/dev/null | grep -q '"keys"'; then
  JWKS_URL="http://127.0.0.1:9080/nexvue-jwks.php"
  ok "JWKS reachable at ${JWKS_URL}"
elif curl -fsS --max-time 3 "http://127.0.0.1/nexvue-jwks.php" 2>/dev/null | grep -q '"keys"'; then
  JWKS_URL="http://127.0.0.1/nexvue-jwks.php"
  ok "JWKS reachable at ${JWKS_URL}"
elif curl -fskS --max-time 3 "https://127.0.0.1/nexvue-jwks.php" 2>/dev/null | grep -q '"keys"'; then
  JWKS_URL="https://127.0.0.1/nexvue-jwks.php"
  JWKS_FP="$(echo | openssl s_client -connect 127.0.0.1:443 -servername 127.0.0.1 2>/dev/null \
    | openssl x509 -fingerprint -sha256 -noout 2>/dev/null \
    | cut -d= -f2 | tr -d ':' | tr '[:upper:]' '[:lower:]')"
  if [ -n "${JWKS_FP}" ]; then
    ok "JWKS reachable via HTTPS loopback (fingerprint ${JWKS_FP:0:16}…)"
  else
    warn "JWKS HTTPS works but fingerprint could not be derived — MediaMTX may still reject TLS"
  fi
else
  warn "JWKS not reachable on loopback — MediaMTX JWT auth will fail (no publish / no WHEP) until Apache serves nexvue-jwks.php"
fi

if [ -n "${JWKS_URL}" ] && [ -f /etc/nexvue/mediamtx.yml ]; then
  PATCH_ARGS=(--jwks "${JWKS_URL}")
  [ -n "${JWKS_FP}" ] && PATCH_ARGS+=(--fingerprint "${JWKS_FP}")
  if python3 "${REPO_DIR}/nexvue-mediamtx-jwt-patch.py" /etc/nexvue/mediamtx.yml "${PATCH_ARGS[@]}"; then
    ok "mediamtx.yml JWT auth → ${JWKS_URL} (apiAddress=127.0.0.1:9997)"
    if systemctl is-active --quiet mediamtx 2>/dev/null; then
      systemctl restart mediamtx >/dev/null 2>&1 \
        && ok "mediamtx restarted (JWT/JWKS)" \
        || warn "mediamtx restart failed — restart manually after JWT patch"
      # Encoders must re-open RTSP with ?jwt= from NEXVUE_PUBLISH_JWT.
      restarted=0
      for id in 0 1 2 3 4 5 6 7; do
        if systemctl is-enabled --quiet "nexvue-encode@${id}" 2>/dev/null; then
          systemctl try-restart "nexvue-encode@${id}" >/dev/null 2>&1 && restarted=$((restarted + 1)) || true
        fi
      done
      if [ "${restarted}" -gt 0 ]; then
        ok "restarted ${restarted} enabled encoder(s) so publish JWT takes effect"
      else
        warn "no enabled nexvue-encode@N — start encoders after NEXVUE_PUBLISH_JWT is set"
      fi
    fi
  else
    warn "mediamtx.yml JWT patch failed — merge authMethod/authJWTJWKS from repo mediamtx.yml manually"
  fi
fi

step "5/5 DeckLink helpers (status + audio probe + configure)"
if [ -f "${REPO_DIR}/decklink-status.cpp" ] && ls /opt/decklink-sdk/Linux/include/DeckLinkAPI.h >/dev/null 2>&1; then
  ( cd "${REPO_DIR}" && make DECKLINK_SDK=/opt/decklink-sdk all && make install )
  ok "decklink-status + decklink-audio-probe + decklink-configure built from SDK at /opt/decklink-sdk"
elif [ -x /usr/local/bin/decklink-status ] && [ -x /usr/local/bin/decklink-audio-probe ] \
     && [ -x /usr/local/bin/decklink-configure ]; then
  ok "decklink helpers already installed"
elif [ -x /usr/local/bin/decklink-status ]; then
  warn "decklink helpers incomplete — rebuild: make && sudo make install"
else
  warn "DeckLink helpers not built — download the SDK, then: make DECKLINK_SDK=/path && sudo make install (status dots + Settings Detect audio + BNC mapping need them)"
fi
if [ -x /usr/local/bin/decklink-configure ]; then
  # Apply half-duplex now (encoders may be stopped during setup; safe no-op if already set).
  if /usr/local/bin/decklink-configure --apply-inputs >/tmp/nexvue-decklink-configure.json 2>/tmp/nexvue-decklink-configure.err; then
    ok "decklink-configure --apply-inputs (BNC half-duplex for capture)"
  else
    rc=$?
    if [ "$rc" -eq 1 ]; then
      warn "decklink-configure: no DeckLink API (Desktop Video not installed?)"
    else
      warn "decklink-configure --apply-inputs failed (rc=$rc) — stop encoders and re-run: sudo decklink-configure --apply-inputs"
    fi
  fi
fi

fi # !CHECK_ONLY

###############################################################################
# Sanity checks (always run)
###############################################################################
step "Sanity checks"

# Kernel / HWE
if $CHECK_ONLY; then :; else
  LATEST_INSTALLED="$(ls /boot/vmlinuz-* 2>/dev/null | sort -V | tail -1 | sed 's|/boot/vmlinuz-||')"
  if [ -n "${LATEST_INSTALLED}" ] && [ "${LATEST_INSTALLED}" != "${KERNEL_BEFORE}" ]; then
    warn "kernel ${LATEST_INSTALLED} installed but ${KERNEL_BEFORE} is running — REBOOT, then re-run: sudo ./setup.sh --check"
  fi
fi

# Quick Sync / VA-API
if vainfo 2>/dev/null | grep -qiE "H264.*(EncSlice|Enc)"; then
  ok "VA-API H.264 encode entrypoints present ($(vainfo 2>/dev/null | grep -m1 -oE 'iHD driver [^ ]+' || echo iHD))"
else
  warn "no H.264 encode entrypoints in vainfo — headless iGPU disabled in BIOS, pre-reboot HWE state, or (Arrow Lake) media driver too old: use Intel's apt repo"
fi

# GStreamer elements (encode path + Phase 1.5 slate supervisor)
for el in decklinkvideosrc vah264enc x264enc watchdog deinterlace opusenc \
          rtspclientsink ccextractor ccconverter \
          input-selector videotestsrc audiotestsrc textoverlay valve identity; do
  if gst-inspect-1.0 "$el" >/dev/null 2>&1; then
    ok "gstreamer element: $el"
  else
    case "$el" in
      decklinkvideosrc) warn "missing $el — install Blackmagic Desktop Video (deb) and reboot" ;;
      vah264enc)        warn "missing $el — VA driver issue (see vainfo above); x264enc fallback works for 1-2 channels only" ;;
      ccextractor|ccconverter) warn "missing $el — caption side channel needs gstreamer1.0-plugins-bad" ;;
      input-selector|videotestsrc|audiotestsrc|textoverlay|valve|identity)
        warn "missing $el — Phase 1.5 supervisor needs gstreamer1.0-plugins-base / good" ;;
      *)                warn "missing $el — check gstreamer package install" ;;
    esac
  fi
done
[ -x /usr/local/bin/nexvue-captions-decode.py ] \
  && ok "nexvue-captions-decode.py present" \
  || warn "nexvue-captions-decode.py missing — CC side channel will stay off"

[ -x /usr/local/bin/nexvue-encode.sh ] \
  && ok "nexvue-encode.sh present" \
  || warn "nexvue-encode.sh missing — nexvue-encode@N will not start"
[ -x /usr/local/bin/nexvue-encode-auto-park.sh ] \
  && ok "nexvue-encode-auto-park.sh present" \
  || warn "nexvue-encode-auto-park.sh missing — empty-port auto-park disabled"
# Supervisor is installed but not used by ExecStart (Phase 1.5 rolled back).
[ -x /usr/local/bin/nexvue-supervisor.py ] \
  && ok "nexvue-supervisor.py present (deferred — not ExecStart)" \
  || true

# MediaMTX + units
[ -x /usr/local/bin/mediamtx ] && ok "mediamtx binary present" || warn "mediamtx binary missing"
for u in mediamtx.service nexvue-encode@.service nexvue-decklink-configure.service \
         nexvue-status.service nexvue-metrics.service; do
  [ -f "/etc/systemd/system/$u" ] && ok "unit installed: $u" || warn "unit missing: $u"
done

# Metrics PHP reader (SQLite) + web UI / auth
if command -v php >/dev/null 2>&1 && php -m 2>/dev/null | grep -qi sqlite3; then
  ok "php sqlite3 extension present (auth + metrics.php)"
else
  warn "php sqlite3 missing — re-run setup.sh (installs php-cli php-sqlite3 libapache2-mod-php)"
fi
if command -v php >/dev/null 2>&1 && php -m 2>/dev/null | grep -qi openssl; then
  ok "php openssl extension present (JWT / auth keys)"
else
  warn "php openssl missing — login JWT minting will fail until php openssl is installed"
fi
if systemctl is-active --quiet apache2 2>/dev/null; then
  ok "apache2 is active"
else
  warn "apache2 not running — sudo systemctl enable --now apache2"
fi
WEBROOT="${NEXVUE_WEBROOT:-/var/www/html}"
if [ -d "${WEBROOT}" ]; then
  for f in index.html multiview.html metrics.html nexvue-metrics.php nexvue-status.php nexvue-mediamtx-api.php nexvue-captions.php nexvue-captions.js nexvue-qr.js nexvue-ui.js nexvue-vu.js nexvue-share-ui.js nexvue-logo.php chart.umd.min.js services.html channels.html nexvue-ops.php nexvue-auth.php nexvue-auth-lib.php nexvue-jwks.php nexvue-auth-gate.js login.html forgot.html reset.html users.html; do
    [ -f "${WEBROOT}/$f" ] && ok "web UI: ${WEBROOT}/$f" || warn "web UI missing: ${WEBROOT}/$f"
  done
  if [ -d /var/lib/nexvue/branding ]; then
    ok "branding dir: /var/lib/nexvue/branding"
  else
    warn "branding dir missing — Settings logo upload needs /var/lib/nexvue/branding (www-data writable)"
  fi
  if [ -d /var/lib/nexvue/auth ] && { [ -f /var/lib/nexvue/auth/auth.db ] || [ -f /var/lib/nexvue/auth.db ]; }; then
    if [ -f /var/lib/nexvue/auth/auth.db ]; then
      ok "auth store: /var/lib/nexvue/auth/auth.db"
    else
      warn "auth store still at legacy /var/lib/nexvue/auth.db — re-run setup.sh to migrate into /var/lib/nexvue/auth/"
    fi
    if [ -f /var/lib/nexvue/auth/jwks.json ] && [ -f /var/lib/nexvue/auth/private.pem ]; then
      ok "auth JWKS + private key present"
    else
      warn "auth keys missing under /var/lib/nexvue/auth — sudo php /usr/local/bin/nexvue-auth-bootstrap.php"
    fi
  else
    warn "auth store missing — run: sudo php /usr/local/bin/nexvue-auth-bootstrap.php"
  fi
else
  warn "Apache docroot ${WEBROOT} not present — web UI not deployed yet"
fi

if [ -f /etc/nexvue/mediamtx.yml ] && grep -qE '^\s*authMethod:\s*jwt\b' /etc/nexvue/mediamtx.yml; then
  ok "mediamtx.yml authMethod=jwt"
else
  warn "mediamtx.yml still open/internal — copy JWT auth block from repo mediamtx.yml (setup leaves existing yml untouched)"
fi
if [ -f /etc/nexvue/mediamtx.yml ] && grep -qE '^\s*apiAddress:\s*127\.0\.0\.1:9997\b' /etc/nexvue/mediamtx.yml; then
  ok "mediamtx.yml apiAddress=127.0.0.1:9997 (loopback)"
else
  warn "mediamtx.yml apiAddress not loopback — re-run setup.sh (JWT patch sets 127.0.0.1:9997) or edit manually"
fi
if [ -f /etc/nexvue/nexvue.env ] && grep -qE '^\s*NEXVUE_PUBLISH_JWT=' /etc/nexvue/nexvue.env; then
  ok "NEXVUE_PUBLISH_JWT present in nexvue.env"
else
  warn "NEXVUE_PUBLISH_JWT missing — encoders cannot publish under JWT auth until bootstrap runs"
fi

# Ops wrappers + sudoers
for w in nexvue-ops-status.sh nexvue-ops-journal.sh nexvue-ops-env-read.sh \
         nexvue-ops-env-write.sh nexvue-ops-restart.sh nexvue-ops-enable.sh \
         nexvue-ops-audio-probe.sh nexvue-ops-support-bundle.sh \
         nexvue-support-bundle.py nexvue-ops-update.sh \
         nexvue-ops-env-update.py nexvue-phase1-closeout.sh \
         nexvue-phase1-deploy-verify.sh nexvue-encode-storm-diagnose.sh \
         nexvue-encode-auto-park.sh; do
  [ -x "/usr/local/bin/$w" ] || [ -f "/usr/local/bin/$w" ] \
    && ok "ops helper: $w" || warn "ops helper missing: /usr/local/bin/$w"
done
if [ -d /var/lib/nexvue/support ]; then
  ok "support bundle dir: /var/lib/nexvue/support"
else
  warn "support bundle dir missing — Services Download zip needs /var/lib/nexvue/support"
fi
if [ -f /etc/nexvue/repo.path ]; then
  ok "repo.path: $(tr -d '\r\n' </etc/nexvue/repo.path)"
else
  warn "repo.path missing — Services Update needs /etc/nexvue/repo.path (re-run setup.sh from the clone)"
fi
if [ -f /usr/local/share/nexvue/VERSION ]; then
  ok "VERSION: $(tr -d '[:space:]' </usr/local/share/nexvue/VERSION)"
else
  warn "VERSION stamp missing under /usr/local/share/nexvue"
fi
if [ -f /etc/sudoers.d/nexvue-ops ]; then
  ok "sudoers drop-in: /etc/sudoers.d/nexvue-ops"
  if grep -q 'nexvue-ops-update\.sh' /etc/sudoers.d/nexvue-ops; then
    ok "sudoers allows nexvue-ops-update.sh"
  else
    warn "sudoers missing nexvue-ops-update.sh — Services Update will fail until sudoers is refreshed from repo"
  fi
else
  warn "sudoers drop-in missing — Services/Settings need /etc/sudoers.d/nexvue-ops"
fi
if [ -x /usr/local/bin/nexvue-ops-update.sh ] && [ -f /etc/nexvue/repo.path ]; then
  if id www-data >/dev/null 2>&1 \
    && sudo -n -u www-data sudo -n /usr/local/bin/nexvue-ops-update.sh status >/dev/null 2>&1; then
    ok "update helper runnable as www-data (Services → Update)"
  else
    # Direct root smoke (sudoers path may still be wrong for www-data).
    if /usr/local/bin/nexvue-ops-update.sh status >/dev/null 2>&1; then
      warn "nexvue-ops-update.sh runs as root but not via www-data sudo — reinstall /etc/sudoers.d/nexvue-ops from repo"
    else
      warn "nexvue-ops-update.sh status failed — check /etc/nexvue/repo.path and git remote"
    fi
  fi
fi

# DeckLink helpers
[ -x /usr/local/bin/decklink-status ] && ok "decklink-status helper present" \
  || warn "decklink-status helper not installed (optional; see step 5)"
[ -x /usr/local/bin/decklink-audio-probe ] && ok "decklink-audio-probe helper present" \
  || warn "decklink-audio-probe not installed (Settings → Detect audio; see step 5)"
[ -x /usr/local/bin/decklink-configure ] && ok "decklink-configure helper present" \
  || warn "decklink-configure not installed (Duo/Quad BNC mapping; see step 5)"

###############################################################################
# Optional: viewer firewall rules (only with --firewall — never silent)
###############################################################################
if $APPLY_FIREWALL; then
  step "Firewall (ufw) — viewer ports (API/status are loopback-only)"
  if ! command -v ufw >/dev/null; then
    warn "ufw not installed — skipping (apt install ufw to use --firewall)"
  else
    # NOTE: does not enable ufw for you — enabling can drop your SSH session if
    # 22 isn't already allowed. Opens viewer ports only; you enable ufw.
    # :9997 / :9998 bind to 127.0.0.1 — do not open them.
    ufw allow 80/tcp comment 'NexVUE player (Apache)' >/dev/null
    ufw allow 443/tcp comment 'NexVUE player (Apache TLS)' >/dev/null
    ufw allow 8889/tcp comment 'NexVUE WHEP signaling' >/dev/null
    ufw allow 8189 comment 'NexVUE WebRTC media (UDP+TCP)' >/dev/null
    # Metrics has NO port at all — the collector doesn't listen on anything;
    # PHP reads its SQLite file directly and Apache serves the result on 443.
    ok "NexVUE viewer ports opened (80,443,8889,8189/udp+tcp)"
    if ufw status 2>/dev/null | grep -qE '\b9997\b|\b9998\b'; then
      warn "ufw still allows 9997/9998 — remove those rules (API/status are loopback-only now)"
    fi
    if ! ufw status | grep -q "Status: active"; then
      warn "ufw is NOT active — rules staged but not enforced. Ensure 22/ssh is allowed, then: sudo ufw enable"
    fi
    warn "8554 (RTSP) left closed on purpose — loopback ingest only"
  fi
fi

###############################################################################
# Summary
###############################################################################
echo
if [ "${#WARNINGS[@]}" -eq 0 ]; then
  ok "all checks passed"
else
  echo "${YELLOW}${#WARNINGS[@]} item(s) need attention:${RESET}"
  for w in "${WARNINGS[@]}"; do echo "  - $w"; done
fi

cat <<'NEXT'

Next steps:
  1. If a reboot was flagged above: reboot, then  sudo ./setup.sh --check
  2. Install Blackmagic Desktop Video if flagged, reboot, re-check
  3. Configure channels:
       sudo cp channels-example.env /etc/nexvue/channels/0.env
       sudo nano /etc/nexvue/channels/0.env
  4. Start services:
       sudo systemctl enable --now nexvue-decklink-configure   # Duo/Quad BNCs → half-duplex
       sudo systemctl enable --now mediamtx nexvue-status nexvue-metrics nexvue-encode@0
  5. Firewall (if ufw is in use): open ports with
       sudo ./setup.sh --firewall     (then: sudo ufw enable, once 22/ssh is allowed)
     or apply the rules manually — see the Firewall section in README.md
  6. Remove any Apache Basic Auth / .htaccess AuthType — app login replaces it.
     Confirm JWKS (MediaMTX): curl -fsS http://127.0.0.1:9080/nexvue-jwks.php | head
  7. Login:  https://<edge-ip>/login.html
     Default bootstrap: admin / password  (forced change on first login)
     Users + share links: /users.html (admin). Player/Multiview/Metrics/Services/Settings
     require a session (or a share link ?t=… on Player).
     If login shows "auth store unavailable": sudo ./setup.sh  (repairs
     /var/lib/nexvue/auth ownership + migrates legacy auth.db).
     If Player connects but never gets video after JWT auth: re-run setup.sh
     (JWKS loopback :9080 + publish JWT + encoder restart).
NEXT
