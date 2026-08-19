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
#   sudo ./setup.sh --firewall apply ufw rules (SSH/443/8889/8189, no :80) and
#                              enable ufw. Full install already does this;
#                              the flag is a legacy alias and also works with
#                              --check if you only want the firewall step.
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

# Read KEY=value from /etc/nexvue/nexvue.env (strip comments/quotes/whitespace).
nexvue_env_get() {
  local key="$1" def="${2:-}" path="${3:-/etc/nexvue/nexvue.env}"
  local line v
  [ -f "$path" ] || { printf '%s' "$def"; return; }
  line="$(grep -E "^[[:space:]]*${key}=" "$path" 2>/dev/null | tail -1 || true)"
  [ -n "$line" ] || { printf '%s' "$def"; return; }
  v="${line#*=}"
  v="${v%%#*}"
  v="${v//\"/}"
  v="${v//\'/}"
  v="${v// /}"
  v="${v//$'\t'/}"
  printf '%s' "${v:-$def}"
}

# Station encode slot count (0 .. N-1). Prefer MAX_CHANNELS; fall back to MAX_DEVICES; default 8.
nexvue_max_channels() {
  local n
  n="$(nexvue_env_get MAX_CHANNELS "")"
  if ! [[ "$n" =~ ^[1-8]$ ]]; then
    n="$(nexvue_env_get MAX_DEVICES "8")"
  fi
  if ! [[ "$n" =~ ^[1-8]$ ]]; then
    n=8
  fi
  printf '%s' "$n"
}

# Self-signed (or leave existing) certs for Apache HTTPS + MediaMTX WHEP/API.
# Paths are fixed: /etc/nexvue/tls/{fullchain,privkey}.pem
ensure_nexvue_tls() {
  local tls_dir=/etc/nexvue/tls
  local cert="${tls_dir}/fullchain.pem"
  local key="${tls_dir}/privkey.pem"
  local cn host short ip i san_dns san_ip tmpcnf
  install -d -m 755 "${tls_dir}"

  if ! getent group ssl-cert >/dev/null 2>&1; then
    groupadd --system ssl-cert 2>/dev/null \
      || warn "could not create ssl-cert group — TLS key permissions may need a manual fix"
  fi
  if id nexvue >/dev/null 2>&1; then
    usermod -aG ssl-cert nexvue 2>/dev/null || true
  fi
  if id www-data >/dev/null 2>&1; then
    usermod -aG ssl-cert www-data 2>/dev/null || true
  fi

  if [ -f "${cert}" ] && [ -f "${key}" ]; then
    ok "TLS certs present: ${cert} + ${key} (left untouched)"
  else
    if [ -f "${cert}" ] || [ -f "${key}" ]; then
      warn "incomplete TLS pair under ${tls_dir} — regenerating both"
      rm -f "${cert}" "${key}"
    fi
    if ! command -v openssl >/dev/null 2>&1; then
      fail "openssl required to create /etc/nexvue/tls certs"
    fi
    host="$(hostname -f 2>/dev/null || true)"
    short="$(hostname 2>/dev/null || true)"
    cn="${host:-${short:-nexvue}}"
    [[ "$cn" == "(none)" || -z "$cn" ]] && cn="nexvue"
    san_dns="DNS:${cn}"
    if [ -n "${short}" ] && [ "${short}" != "${cn}" ]; then
      san_dns="${san_dns},DNS:${short}"
    fi
    san_dns="${san_dns},DNS:localhost"
    san_ip="IP:127.0.0.1"
    i=2
    for ip in $(hostname -I 2>/dev/null || true); do
      [ -n "$ip" ] || continue
      san_ip="${san_ip},IP:${ip}"
      i=$((i + 1))
      [ "$i" -le 8 ] || break
    done
    tmpcnf="$(mktemp)"
    cat > "${tmpcnf}" <<EOF
[req]
default_bits = 4096
prompt = no
default_md = sha256
distinguished_name = dn
x509_extensions = v3_req

[dn]
CN = ${cn}
O = NexVUE
OU = edge

[v3_req]
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = ${san_dns},${san_ip}
EOF
    openssl req -x509 -newkey rsa:4096 -sha256 -days 825 -nodes \
      -keyout "${key}" -out "${cert}" -config "${tmpcnf}" >/dev/null 2>&1 \
      || { rm -f "${tmpcnf}"; fail "openssl failed creating ${cert}"; }
    rm -f "${tmpcnf}"
    ok "created self-signed TLS certs in ${tls_dir} (CN=${cn}; replace with a real cert when ready)"
  fi

  chown root:ssl-cert "${cert}" "${key}" 2>/dev/null || chown root:root "${cert}" "${key}"
  chmod 644 "${cert}"
  chmod 640 "${key}"
}

# Ubuntu's unit is ssh.service; sshd.service is an alias on some images.
ensure_sshd() {
  if systemctl enable --now ssh >/dev/null 2>&1 \
      || systemctl enable --now sshd >/dev/null 2>&1; then
    if systemctl is-active --quiet ssh 2>/dev/null \
        || systemctl is-active --quiet sshd 2>/dev/null; then
      ok "sshd enabled and running"
      return 0
    fi
  fi
  warn "could not enable ssh/sshd — install openssh-server before enabling ufw"
  return 0
}

# SSH first, then HTTPS/WHEP, then HTTP (redirect-to-HTTPS only — see
# nexvue_web_https_redirect_target() in nexvue-web-router.php; closing :80
# outright used to strand anyone who reached the UI via a stale http://
# bookmark, since the browser inherited http: for the WHEP fetch too and
# MediaMTX's :8889 listener is TLS-only). Enable ufw only if sshd is up.
apply_nexvue_firewall() {
  step "Firewall (ufw) — SSH + HTTPS + WHEP + HTTP (redirect only)"
  if ! command -v ufw >/dev/null; then
    warn "ufw not installed — skipping (apt install ufw)"
    return
  fi
  if ufw allow OpenSSH >/dev/null 2>&1; then
    ok "ufw allow OpenSSH"
  else
    ufw allow 22/tcp comment 'SSH' >/dev/null 2>&1 \
      && ok "ufw allow 22/tcp" \
      || warn "ufw allow SSH failed"
  fi
  ufw allow 443/tcp comment 'NexVUE Apache HTTPS' >/dev/null \
    || warn "ufw allow 443/tcp failed"
  ufw allow 80/tcp comment 'NexVUE Apache HTTP (redirects to HTTPS)' >/dev/null \
    || warn "ufw allow 80/tcp failed"
  ufw allow 8889/tcp comment 'NexVUE WHEP signaling' >/dev/null \
    || warn "ufw allow 8889/tcp failed"
  ufw allow 8189 comment 'NexVUE WebRTC media (UDP+TCP)' >/dev/null \
    || warn "ufw allow 8189 failed"
  ok "ufw viewer ports opened (80,443,8889,8189/udp+tcp)"
  if ufw status 2>/dev/null | grep -qE '\b9997\b|\b9998\b'; then
    warn "ufw still allows 9997/9998 — remove those rules (API/status are loopback-only now)"
  fi
  if systemctl is-active --quiet ssh 2>/dev/null \
      || systemctl is-active --quiet sshd 2>/dev/null; then
    if ufw --force enable >/dev/null 2>&1; then
      ok "ufw enabled"
    else
      warn "ufw --force enable failed"
    fi
  else
    warn "sshd not active — not enabling ufw (would lock you out). Start ssh, then re-run setup.sh"
  fi
  ok "8554 (RTSP) left closed — loopback ingest only"
}

# ---- Cloud portal installer (Phase 4) --------------------------------------------
# A portal deployment is always a separate box from an edge node — never the
# DeckLink/GStreamer/MediaMTX/encoder stack, just Apache + PHP + its own
# SQLite store. Self-contained function so `--portal` can branch out and
# exit before any edge-only logic below runs.
install_portal() {
  local repo_dir webroot public pages assets
  repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  webroot="${NEXVUE_PORTAL_WEBROOT:-/var/www/html}"
  public="${webroot}/public"
  pages="${webroot}/pages"
  assets="${public}/assets"
  local check_only=false
  for arg in "$@"; do
    [ "$arg" = "--check" ] && check_only=true
  done

  [ "$(id -u)" -eq 0 ] || fail "run as root: sudo ./setup.sh --portal"

  step "Cloud portal — required files"
  local portal_files=(
    web-portal/public/index.php
    web-portal/nexvue-portal-web-router.php
    web-portal/nexvue-portal-web-apache.conf
    web-portal/nexvue-portal-ssl-apache.conf
    web-portal/nexvue-portal-auth-lib.php
    web-portal/nexvue-portal-api.php
    web-portal/nexvue-portal-bootstrap.php
    web-portal/nexvue-portal-auth-gate.js
    web-portal/nexvue-portal-whep.js
    web-portal/nexvue-ui.js
    web-portal/login.html
    web-portal/catalog.html
    web-portal/watch.html
    web-portal/stations.html
    web-portal/users.html
  )
  local f
  for f in "${portal_files[@]}"; do
    [ -f "${repo_dir}/${f}" ] || fail "missing ${f} — run from the repo root"
  done
  ok "all web-portal/ files present"
  if $check_only; then
    [ -d "${webroot}" ] || fail "portal webroot ${webroot} missing — run sudo ./setup.sh --portal (no --check) first"
    for f in public/index.php nexvue-portal-web-router.php nexvue-portal-api.php \
             nexvue-portal-auth-lib.php nexvue-portal-bootstrap.php \
             pages/login.html pages/catalog.html pages/watch.html pages/stations.html pages/users.html \
             public/assets/nexvue-ui.js public/assets/nexvue-portal-auth-gate.js public/assets/nexvue-portal-whep.js; do
      [ -f "${webroot}/${f}" ] && ok "portal web UI: ${webroot}/${f}" || warn "portal web UI missing: ${webroot}/${f}"
    done
    if command -v apache2ctl >/dev/null 2>&1 \
        && apache2ctl -S 2>/dev/null | grep -q "DocumentRoot: ${public}"; then
      ok "Apache DocumentRoot is ${public}"
    else
      warn "Apache DocumentRoot may not be ${public} — /login, /api/portal will fail until fixed"
    fi
    [ -f /etc/nexvue-portal/tls/fullchain.pem ] && ok "portal TLS cert present" || warn "portal TLS cert missing"
    [ -f /var/lib/nexvue-portal/portal.db ] && ok "portal.db present" || warn "portal.db missing — run bootstrap"
    if command -v curl >/dev/null 2>&1 && systemctl is-active --quiet apache2 2>/dev/null; then
      _portal_http_loc="$(curl -fsS -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 2 "http://127.0.0.1/login" 2>/dev/null || true)"
      case "${_portal_http_loc}" in
        30*\ https://*) ok "portal HTTP :80 redirects to HTTPS (${_portal_http_loc})" ;;
        *) warn "portal :80 did not redirect to HTTPS (got: ${_portal_http_loc:-no response}) — check ufw allows 80/tcp and 000-default is enabled" ;;
      esac
    fi
    return 0
  fi

  step "Cloud portal — packages (Apache + PHP only; no DeckLink/GStreamer/MediaMTX)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq || warn "apt-get update failed — continuing with cached package lists"
  apt-get install -y -qq apache2 libapache2-mod-php php-cli php-sqlite3 ufw \
    || fail "apt-get install (apache2/php/ufw) failed"
  ok "apache2 + php-cli + php-sqlite3 + ufw installed"

  step "Cloud portal — web app"
  install -d -m 755 "${public}" "${pages}" "${assets}"
  install -m 644 "${repo_dir}/web-portal/public/index.php" "${public}/index.php"
  install -m 644 "${repo_dir}/web-portal/nexvue-portal-web-router.php" "${webroot}/nexvue-portal-web-router.php"
  install -m 644 "${repo_dir}/web-portal/login.html" \
                 "${repo_dir}/web-portal/catalog.html" \
                 "${repo_dir}/web-portal/watch.html" \
                 "${repo_dir}/web-portal/stations.html" \
                 "${repo_dir}/web-portal/users.html" \
                 "${pages}/"
  install -m 644 "${repo_dir}/web-portal/nexvue-ui.js" \
                 "${repo_dir}/web-portal/nexvue-portal-auth-gate.js" \
                 "${repo_dir}/web-portal/nexvue-portal-whep.js" \
                 "${assets}/"
  install -m 644 "${repo_dir}/web-portal/nexvue-portal-auth-lib.php" \
                 "${repo_dir}/web-portal/nexvue-portal-api.php" \
                 "${webroot}/"
  ok "web-portal/ installed under ${webroot} (public/ front door + pages/ + /api/portal)"

  step "Cloud portal — store (portal.db + RSA signing key)"
  install -d -m 750 -o www-data -g www-data /var/lib/nexvue-portal 2>/dev/null \
    || install -d -m 750 /var/lib/nexvue-portal
  chown www-data:www-data /var/lib/nexvue-portal 2>/dev/null || true
  install -d -m 755 /usr/local/share/nexvue-portal
  install -m 644 "${repo_dir}/web-portal/nexvue-portal-auth-lib.php" \
    /usr/local/share/nexvue-portal/nexvue-portal-auth-lib.php
  install -m 755 "${repo_dir}/web-portal/nexvue-portal-bootstrap.php" \
    /usr/local/bin/nexvue-portal-bootstrap.php
  if command -v php >/dev/null 2>&1; then
    if php /usr/local/bin/nexvue-portal-bootstrap.php; then
      ok "portal bootstrap (portal.db + RSA key + seed org/admin)"
    else
      warn "portal bootstrap failed — run: sudo php /usr/local/bin/nexvue-portal-bootstrap.php"
    fi
    if command -v runuser >/dev/null 2>&1; then
      if runuser -u www-data -- php /usr/local/bin/nexvue-portal-bootstrap.php >/dev/null 2>&1; then
        ok "portal store readable/writable as www-data"
      else
        warn "www-data could not read/write the portal store — check /var/lib/nexvue-portal ownership"
      fi
    fi
  else
    warn "php CLI not found — cannot bootstrap portal store"
  fi

  step "Cloud portal — TLS (self-signed if missing)"
  local tls_dir=/etc/nexvue-portal/tls cert key cn host short
  cert="${tls_dir}/fullchain.pem"
  key="${tls_dir}/privkey.pem"
  install -d -m 755 "${tls_dir}"
  if ! getent group ssl-cert >/dev/null 2>&1; then
    groupadd --system ssl-cert 2>/dev/null || true
  fi
  usermod -aG ssl-cert www-data 2>/dev/null || true
  if [ -f "${cert}" ] && [ -f "${key}" ]; then
    ok "portal TLS certs present: ${cert} + ${key} (left untouched)"
  else
    if command -v openssl >/dev/null 2>&1; then
      host="$(hostname -f 2>/dev/null || true)"; short="$(hostname 2>/dev/null || true)"
      cn="${host:-${short:-nexvue-portal}}"
      openssl req -x509 -newkey rsa:4096 -sha256 -days 825 -nodes \
        -keyout "${key}" -out "${cert}" \
        -subj "/CN=${cn}/O=NexVUE Portal" >/dev/null 2>&1 \
        && ok "created self-signed portal TLS certs (CN=${cn}; replace with a real cert when ready)" \
        || warn "openssl failed creating portal TLS certs"
    else
      warn "openssl not found — cannot create portal TLS certs"
    fi
  fi
  chown root:ssl-cert "${cert}" "${key}" 2>/dev/null || true
  chmod 644 "${cert}" 2>/dev/null || true
  chmod 640 "${key}" 2>/dev/null || true

  step "Cloud portal — Apache"
  if [ -d /etc/apache2/conf-available ] && command -v a2enmod >/dev/null 2>&1; then
    a2enmod rewrite >/dev/null 2>&1 && ok "Apache mod_rewrite enabled" \
      || warn "a2enmod rewrite failed — /login, /catalog routing will 404 without it"
    a2enmod ssl >/dev/null 2>&1 && ok "Apache mod_ssl enabled" || warn "a2enmod ssl failed"
    sed "s|@@APP_ROOT@@|${webroot}|g" "${repo_dir}/web-portal/nexvue-portal-web-apache.conf" \
      > /etc/apache2/conf-available/nexvue-portal-web.conf
    chmod 644 /etc/apache2/conf-available/nexvue-portal-web.conf
    a2enconf nexvue-portal-web >/dev/null 2>&1 \
      && ok "Apache conf enabled: nexvue-portal-web" || warn "a2enconf nexvue-portal-web failed"
    install -m 644 "${repo_dir}/web-portal/nexvue-portal-ssl-apache.conf" \
      /etc/apache2/conf-available/nexvue-portal-ssl-certs.conf
    a2enconf nexvue-portal-ssl-certs >/dev/null 2>&1 \
      && ok "Apache conf enabled: nexvue-portal-ssl-certs" || warn "a2enconf nexvue-portal-ssl-certs failed"
    # Fresh/dedicated box assumption: patch Ubuntu's standard default vhosts
    # directly rather than the edge installer's fully general regex patcher —
    # simpler, appropriately scoped for a single-purpose portal box. If your
    # site layout differs, set DocumentRoot to ${public} manually.
    local vhost
    for vhost in /etc/apache2/sites-available/000-default.conf /etc/apache2/sites-available/default-ssl.conf; do
      [ -f "$vhost" ] || continue
      if grep -qE '^\s*DocumentRoot\s+/var/www/html\s*$' "$vhost" 2>/dev/null; then
        sed -i "s|^\(\s*DocumentRoot\s\+\)/var/www/html\s*\$|\1${public}|" "$vhost"
        ok "patched DocumentRoot -> ${public} in $(basename "$vhost")"
      fi
    done
    if ! apache2ctl -S 2>/dev/null | grep -q "DocumentRoot: ${public}"; then
      warn "DocumentRoot may not be ${public} — set it manually in your enabled site if routing 404s"
    fi
    a2ensite default-ssl >/dev/null 2>&1 || true
    systemctl reload apache2 >/dev/null 2>&1 || systemctl restart apache2 >/dev/null 2>&1 \
      || warn "apache2 reload/restart failed — check apache2ctl configtest"
    ok "Apache reloaded"
  else
    warn "Apache conf-available/a2enmod missing — enable rewrite/ssl and set DocumentRoot manually"
  fi
  systemctl enable --now apache2 >/dev/null 2>&1 && ok "apache2 enabled --now" \
    || warn "could not enable --now apache2"

  step "Cloud portal — firewall (SSH + HTTPS + HTTP redirect only; no media ports)"
  if command -v ufw >/dev/null 2>&1; then
    ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null 2>&1 || warn "ufw allow SSH failed"
    ufw allow 443/tcp comment 'NexVUE portal HTTPS' >/dev/null 2>&1 || warn "ufw allow 443/tcp failed"
    # :80 stays open only for the HTTP -> HTTPS redirect
    # (nexvue_portal_web_https_redirect_target()) — same rationale as the
    # edge: a stray http:// bookmark should bounce cleanly, not dead-end.
    ufw allow 80/tcp comment 'NexVUE portal HTTP (redirects to HTTPS)' >/dev/null 2>&1 || warn "ufw allow 80/tcp failed"
    if systemctl is-active --quiet ssh 2>/dev/null || systemctl is-active --quiet sshd 2>/dev/null; then
      ufw --force enable >/dev/null 2>&1 && ok "ufw enabled (OpenSSH + 80/443 only)" || warn "ufw --force enable failed"
    else
      warn "sshd not active — not enabling ufw (would lock you out)"
    fi
  else
    warn "ufw not installed — skipping firewall step"
  fi

  echo
  ok "NexVUE cloud portal installed. Visit https://<this-host>/login (default admin / password — change immediately)."
  ok "Next: on each edge node, Settings -> Adopt this station, using an enrollment code from /stations here."
  return 0
}

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK_ONLY=false
APPLY_FIREWALL=false
PORTAL_MODE=false
for arg in "$@"; do
  case "$arg" in
    --check)    CHECK_ONLY=true ;;
    --firewall) APPLY_FIREWALL=true ;;
    --portal)   PORTAL_MODE=true ;;
  esac
done

# ---- Cloud portal install (separate box from an edge node) -----------------------
# --portal installs ONLY the web-portal/ app + its own SQLite store — none of
# the DeckLink/GStreamer/MediaMTX/encoder machinery below applies to a portal
# box. Branches out and exits before any of that runs.
if $PORTAL_MODE; then
  install_portal "$@"
  exit $?
fi

[ "$(id -u)" -eq 0 ] || fail "run as root: sudo ./setup.sh"

# ---- Required repo files (verify before touching the system) ---------------------
REQUIRED_FILES=(
  mediamtx.yml mediamtx.service
  nexvue-encode.sh nexvue-encode.py nexvue-supervisor.py nexvue-encode@.service
  nexvue-encode-auto-park.sh
  nexvue-decklink-configure.service
  decklink-configure.cpp
  nexvue-status-server.py nexvue-status.service
  nexvue-metrics-server.py nexvue-metrics.service
  web-node/nexvue-metrics.php web-node/nexvue-status.php web-node/nexvue-mediamtx-api.php
  web-node/nexvue-captions.php web-node/nexvue-captions.js
  web-node/nexvue-qr.js web-node/nexvue-ui.js web-node/nexvue-vu.js web-node/nexvue-logo.php web-node/chart.umd.min.js
  web-node/metrics.html web-node/index.html web-node/multiview.html
  web-node/nexvue-ops.php web-node/services.html web-node/channels.html
  web-node/nexvue-auth-lib.php web-node/nexvue-auth.php web-node/nexvue-jwks.php nexvue-auth-bootstrap.php
  web-node/nexvue-auth-gate.js web-node/nexvue-share-ui.js
  web-node/nexvue-web-router.php nexvue-web-apache.conf nexvue-ssl-apache.conf
  web-node/public/index.php
  nexvue-mediamtx-jwt-patch.py nexvue-mediamtx-tls-patch.py
  nexvue-apache-http-on.py
  nexvue-jwks-loopback.conf
  web-node/login.html web-node/forgot.html web-node/reset.html web-node/users.html
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
  web-node/nexvue-version.php
  VERSION
  channels-example.env
  nexvue-example.env
  nexvue-portal-heartbeat.php nexvue-portal-heartbeat.service nexvue-portal-heartbeat.timer
  nexvue-ops-portal-write.sh nexvue-ops-portal-write.py
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
  gstreamer1.0-rtsp \
  intel-media-va-driver-non-free vainfo intel-gpu-tools \
  build-essential curl ca-certificates jq openssl ssl-cert \
  apache2 libapache2-mod-php php-cli php-sqlite3 \
  openssh-server ufw \
  python3-gi python3-gst-1.0 gir1.2-glib-2.0 gir1.2-gstreamer-1.0 \
  gir1.2-gst-plugins-base-1.0
ok "apt packages installed (python: stdlib + apt-only python3-gi/python3-gst-1.0 for nexvue-encode.py — never pip; Apache + php-cli/sqlite3 for login/auth + metrics.php)"

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
  systemctl enable apache2 >/dev/null 2>&1 || true
  if systemctl is-active --quiet apache2 2>/dev/null; then
    systemctl reload apache2 >/dev/null 2>&1 \
      && ok "apache2 reloaded" \
      || warn "apache2 reload failed — restart manually after setup if login fails"
  else
    systemctl enable --now apache2 >/dev/null 2>&1 \
      && ok "apache2 enabled and started" \
      || warn "could not start apache2 — start it before using /login"
  fi
  a2enmod rewrite >/dev/null 2>&1 && ok "Apache module enabled: rewrite" \
    || warn "a2enmod rewrite failed — front-door FallbackResource needs mod_rewrite"
else
  warn "a2enmod missing — install apache2 + libapache2-mod-php for the web UI"
fi
ensure_sshd

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

# Station-wide /etc/nexvue/nexvue.env. Never overwrite existing key values.
# Fresh install → Quad 2 defaults MAX_DEVICES=8 and MAX_CHANNELS=8.
# When absent: migrate a consistent legacy MAX_DEVICES from channel envs, or
# install the example. Conflicting legacy values → warn and leave absent.
# When present but a key is missing: append the Quad 2 default (or match the
# other key) so encode@ enable and Settings slot counts stay aligned.
if [ -f /etc/nexvue/nexvue.env ]; then
  ok "/etc/nexvue/nexvue.env exists — not overwriting values"
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
    warn "conflicting legacy MAX_DEVICES across channel envs — not creating /etc/nexvue/nexvue.env; set MAX_DEVICES=N and MAX_CHANNELS=N there manually (1–8; Quad 2 = 8)"
  else
    if [ -z "$migrate_val" ]; then
      install -m 644 "${REPO_DIR}/nexvue-example.env" /etc/nexvue/nexvue.env
      ok "installed /etc/nexvue/nexvue.env (Quad 2 defaults MAX_DEVICES=8 MAX_CHANNELS=8)"
    else
      {
        echo "# Migrated from channel .env by setup.sh ($(date -Is))"
        echo "# Duo 2 = 4; Quad 2 = 8"
        echo "MAX_DEVICES=${migrate_val}"
        echo "MAX_CHANNELS=${migrate_val}"
      } > /etc/nexvue/nexvue.env
      chmod 644 /etc/nexvue/nexvue.env
      ok "migrated MAX_DEVICES=${migrate_val} MAX_CHANNELS=${migrate_val} into /etc/nexvue/nexvue.env"
    fi
  fi
fi
# Fill missing slot-count keys only (never change an existing value).
if [ -f /etc/nexvue/nexvue.env ]; then
  _md="$(nexvue_env_get MAX_DEVICES "")"
  _mc="$(nexvue_env_get MAX_CHANNELS "")"
  if [ -z "${_md}" ]; then
    _fill="${_mc:-8}"
    [[ "${_fill}" =~ ^[1-8]$ ]] || _fill=8
    printf '\n# Added by setup.sh (was unset; Quad 2 default is 8)\nMAX_DEVICES=%s\n' "${_fill}" \
      >> /etc/nexvue/nexvue.env
    ok "appended MAX_DEVICES=${_fill} to /etc/nexvue/nexvue.env"
  fi
  if [ -z "${_mc}" ]; then
    _fill="$(nexvue_env_get MAX_DEVICES "8")"
    [[ "${_fill}" =~ ^[1-8]$ ]] || _fill=8
    printf '\n# Added by setup.sh (was unset; Quad 2 default is 8)\nMAX_CHANNELS=%s\n' "${_fill}" \
      >> /etc/nexvue/nexvue.env
    ok "appended MAX_CHANNELS=${_fill} to /etc/nexvue/nexvue.env"
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

# TLS for Apache HTTPS + MediaMTX WHEP/API — before any enable --now of mediamtx.
ensure_nexvue_tls
if [ -f /etc/nexvue/mediamtx.yml ]; then
  _mtx_tls="$(python3 "${REPO_DIR}/nexvue-mediamtx-tls-patch.py" /etc/nexvue/mediamtx.yml)"
  if [ "${_mtx_tls}" = "patched" ]; then
    ok "mediamtx.yml TLS paths → /etc/nexvue/tls/{fullchain,privkey}.pem"
  else
    ok "mediamtx.yml already points at /etc/nexvue/tls"
  fi
fi

install -m 755 "${REPO_DIR}/nexvue-encode.sh" /usr/local/bin/nexvue-encode.sh
install -m 755 "${REPO_DIR}/nexvue-encode.py" /usr/local/bin/nexvue-encode.py
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
install -m 755 "${REPO_DIR}/nexvue-ops-portal-write.py" /usr/local/bin/nexvue-ops-portal-write.py
install -m 755 "${REPO_DIR}/nexvue-ops-portal-write.sh" /usr/local/bin/nexvue-ops-portal-write.sh
install -m 755 "${REPO_DIR}/nexvue-portal-heartbeat.php" /usr/local/bin/nexvue-portal-heartbeat.php
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
               "${REPO_DIR}/nexvue-metrics.service" \
               "${REPO_DIR}/nexvue-portal-heartbeat.service" \
               "${REPO_DIR}/nexvue-portal-heartbeat.timer" /etc/systemd/system/
systemctl daemon-reload
ok "scripts + units installed, systemd reloaded"

# Channel .env stubs for every encode slot (never overwrite existing).
MAX_CH="$(nexvue_max_channels)"
for id in $(seq 0 $((MAX_CH - 1))); do
  ch_env="/etc/nexvue/channels/${id}.env"
  if [ -f "$ch_env" ]; then
    continue
  fi
  if [ ! -f "${REPO_DIR}/channels-example.env" ]; then
    warn "channels-example.env missing — cannot seed ${ch_env}"
    continue
  fi
  sed -e "s/^DEVICE_NUMBER=.*/DEVICE_NUMBER=${id}/" \
      -e "s/^CHANNEL_PATH=.*/CHANNEL_PATH=ch${id}/" \
      "${REPO_DIR}/channels-example.env" > "${ch_env}"
  chmod 644 "${ch_env}"
  ok "seeded ${ch_env} (DEVICE_NUMBER=${id}, CHANNEL_PATH=ch${id})"
done
chmod 644 /etc/nexvue/channels/*.env 2>/dev/null || true

# Enable + start shared daemons and encode@0 .. @(MAX_CHANNELS-1).
# Empty DeckLink ports auto-park after AUTO_PARK_UNLOCK_CYCLES; Duo stations
# should set MAX_CHANNELS=4 (or MAX_DEVICES=4) in nexvue.env before setup.
systemctl enable nexvue-decklink-configure.service >/dev/null 2>&1 \
  && ok "enabled nexvue-decklink-configure.service (half-duplex BNCs before encode)" \
  || warn "could not enable nexvue-decklink-configure.service"
# Run configure once now so BNCs are ready before encoders start.
if [ -x /usr/local/bin/decklink-configure ]; then
  systemctl start nexvue-decklink-configure.service >/dev/null 2>&1 \
    && ok "started nexvue-decklink-configure.service" \
    || warn "nexvue-decklink-configure start failed (card absent / SDK helper missing?)"
fi

for u in mediamtx nexvue-status nexvue-metrics; do
  if systemctl enable --now "$u" >/dev/null 2>&1; then
    ok "enabled --now ${u}"
  else
    warn "could not enable --now ${u}"
  fi
done

# Cloud portal heartbeat (Phase 4) — safe to enable unconditionally on every
# station: the service no-ops until an admin adopts it (Settings → Adopt).
if systemctl enable --now nexvue-portal-heartbeat.timer >/dev/null 2>&1; then
  ok "enabled --now nexvue-portal-heartbeat.timer (no-op until station is adopted)"
else
  warn "could not enable --now nexvue-portal-heartbeat.timer"
fi

enc_ok=0
enc_fail=0
for id in $(seq 0 $((MAX_CH - 1))); do
  if systemctl enable --now "nexvue-encode@${id}" >/dev/null 2>&1; then
    enc_ok=$((enc_ok + 1))
  else
    enc_fail=$((enc_fail + 1))
    warn "could not enable --now nexvue-encode@${id}"
  fi
done
if [ "$enc_ok" -gt 0 ]; then
  ok "enabled --now nexvue-encode@0..$((MAX_CH - 1)) (${enc_ok} slot(s); MAX_CHANNELS=${MAX_CH})"
fi
if [ "$enc_fail" -gt 0 ]; then
  warn "${enc_fail} encode slot(s) failed to enable — check journalctl -u 'nexvue-encode@*'"
fi
# Disable encode instances at/above MAX_CHANNELS so Duo (4) does not leave stale @4..@7 enabled.
for id in $(seq "$MAX_CH" 7); do
  if systemctl is-enabled --quiet "nexvue-encode@${id}" 2>/dev/null; then
    systemctl disable --now "nexvue-encode@${id}" >/dev/null 2>&1 \
      && ok "disabled nexvue-encode@${id} (above MAX_CHANNELS=${MAX_CH})" \
      || true
  fi
done

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
install -m 644 "${REPO_DIR}/web-node/nexvue-auth-lib.php" /usr/local/share/nexvue/nexvue-auth-lib.php
install -m 755 "${REPO_DIR}/nexvue-auth-bootstrap.php" /usr/local/bin/nexvue-auth-bootstrap.php
# Smoke must use the installed lib — www-data often cannot read REPO_DIR under /home.
AUTH_LIB_INSTALLED=/usr/local/share/nexvue/nexvue-auth-lib.php
if command -v php >/dev/null 2>&1; then
  if php /usr/local/bin/nexvue-auth-bootstrap.php; then
    ok "auth bootstrap (auth.db + JWKS + seed admin + NEXVUE_PUBLISH_JWT)"
  else
    warn "auth bootstrap failed — run: sudo php /usr/local/bin/nexvue-auth-bootstrap.php"
  fi
  if id www-data >/dev/null 2>&1; then
    # Parent must stay world-traversable (metrics StateDirectory=nexvue is 0755).
    chmod 755 /var/lib/nexvue 2>/dev/null || true
    chown -R www-data:www-data /var/lib/nexvue/auth 2>/dev/null || true
    chmod 750 /var/lib/nexvue/auth 2>/dev/null || true
    chmod 640 /var/lib/nexvue/auth/private.pem 2>/dev/null || true
    chmod 644 /var/lib/nexvue/auth/public.pem /var/lib/nexvue/auth/jwks.json \
      /var/lib/nexvue/auth/kid /var/lib/nexvue/auth/auth.db 2>/dev/null || true
    # WAL sidecars if present from bootstrap.
    chmod 644 /var/lib/nexvue/auth/auth.db-* 2>/dev/null || true
    # Smoke: Apache user must open/migrate/write the store (login uses this path).
    AUTH_SMOKE_PHP='
      require "'"${AUTH_LIB_INSTALLED}"'";
      auth_migrate();
      auth_ensure_keys();
      $n = (int)auth_db()->querySingle("SELECT COUNT(*) FROM users");
      if ($n < 1) { fwrite(STDERR, "no users\n"); exit(1); }
      echo "users=$n\n";
    '
    auth_smoke_ok=false
    AUTH_SMOKE_ERR="$(mktemp)"
    if command -v runuser >/dev/null 2>&1; then
      if runuser -u www-data -- php -r "${AUTH_SMOKE_PHP}" >"${AUTH_SMOKE_ERR}" 2>&1; then
        auth_smoke_ok=true
      fi
    fi
    if ! $auth_smoke_ok; then
      if sudo -u www-data php -r "${AUTH_SMOKE_PHP}" >"${AUTH_SMOKE_ERR}" 2>&1; then
        auth_smoke_ok=true
      fi
    fi
    if $auth_smoke_ok; then
      ok "auth store writable by www-data (/var/lib/nexvue/auth)"
    else
      warn "auth store NOT usable as www-data — login will show 'auth store unavailable'"
      if [ -s "${AUTH_SMOKE_ERR}" ]; then
        warn "auth smoke (www-data): $(tr '\n' ' ' <"${AUTH_SMOKE_ERR}" | head -c 240)"
      fi
      warn "check: ls -la /var/lib/nexvue /var/lib/nexvue/auth; php-sqlite3 + openssl; re-run setup.sh"
    fi
    rm -f "${AUTH_SMOKE_ERR}"
  fi
else
  warn "php missing — cannot bootstrap auth.db / JWKS"
fi

# Per-unit Services journal clear watermarks (root writes via sudo journal wrapper).
install -d -m 755 /var/lib/nexvue/journal-cleared

# App root (pages + PHP APIs). Public front door is ${WEBROOT}/public.
# Override with NEXVUE_WEBROOT if the site isn't under /var/www/html.
WEBROOT="${NEXVUE_WEBROOT:-/var/www/html}"
PUBLIC="${WEBROOT}/public"
PAGES="${WEBROOT}/pages"
ASSETS="${PUBLIC}/assets"
if [ -d "${WEBROOT}" ] || mkdir -p "${WEBROOT}" 2>/dev/null; then
  install -d -m 755 "${PUBLIC}" "${PAGES}" "${ASSETS}"
  # Front controller
  install -m 644 "${REPO_DIR}/web-node/public/index.php" "${PUBLIC}/index.php"
  install -m 644 "${REPO_DIR}/web-node/nexvue-web-router.php" "${WEBROOT}/nexvue-web-router.php"
  # HTML pages (not web-enumerable — DocumentRoot is public/)
  install -m 644 "${REPO_DIR}/web-node/index.html" \
                 "${REPO_DIR}/web-node/multiview.html" \
                 "${REPO_DIR}/web-node/metrics.html" \
                 "${REPO_DIR}/web-node/services.html" \
                 "${REPO_DIR}/web-node/channels.html" \
                 "${REPO_DIR}/web-node/login.html" \
                 "${REPO_DIR}/web-node/forgot.html" \
                 "${REPO_DIR}/web-node/reset.html" \
                 "${REPO_DIR}/web-node/users.html" \
                 "${PAGES}/"
  # Browser assets
  install -m 644 "${REPO_DIR}/web-node/nexvue-captions.js" \
                 "${REPO_DIR}/web-node/nexvue-qr.js" \
                 "${REPO_DIR}/web-node/nexvue-ui.js" \
                 "${REPO_DIR}/web-node/nexvue-vu.js" \
                 "${REPO_DIR}/web-node/nexvue-auth-gate.js" \
                 "${REPO_DIR}/web-node/nexvue-share-ui.js" \
                 "${REPO_DIR}/web-node/chart.umd.min.js" \
                 "${ASSETS}/"
  # PHP APIs + lib (served only via /api/* front door; JWKS vhost can reach them)
  install -m 644 "${REPO_DIR}/web-node/nexvue-metrics.php" \
                 "${REPO_DIR}/web-node/nexvue-status.php" \
                 "${REPO_DIR}/web-node/nexvue-mediamtx-api.php" \
                 "${REPO_DIR}/web-node/nexvue-captions.php" \
                 "${REPO_DIR}/web-node/nexvue-logo.php" \
                 "${REPO_DIR}/web-node/nexvue-version.php" \
                 "${REPO_DIR}/web-node/nexvue-ops.php" \
                 "${REPO_DIR}/web-node/nexvue-auth-lib.php" \
                 "${REPO_DIR}/web-node/nexvue-auth.php" \
                 "${REPO_DIR}/web-node/nexvue-jwks.php" \
                 "${REPO_DIR}/VERSION" \
                 "${WEBROOT}/"
  # Remove legacy flat copies left from pre-2.0 installs (safe allowlist only).
  for legacy in index.html multiview.html metrics.html services.html channels.html \
      login.html forgot.html reset.html users.html \
      nexvue-captions.js nexvue-qr.js nexvue-ui.js nexvue-vu.js \
      nexvue-auth-gate.js nexvue-share-ui.js chart.umd.min.js; do
    rm -f "${WEBROOT}/${legacy}"
  done
  ok "web UI installed under ${WEBROOT} (public/ front door + pages/ + /api handlers)"

  # ---- Apache front door: rewrite + DocumentRoot → public/ --------------------
  if [ -d /etc/apache2/conf-available ] && command -v a2enmod >/dev/null 2>&1; then
    a2enmod rewrite >/dev/null 2>&1 \
      && ok "Apache mod_rewrite enabled (path front door)" \
      || warn "a2enmod rewrite failed — /player /api/* routing will 404 without it"

    sed "s|@@APP_ROOT@@|${WEBROOT}|g" "${REPO_DIR}/nexvue-web-apache.conf" \
      > /etc/apache2/conf-available/nexvue-web.conf
    chmod 644 /etc/apache2/conf-available/nexvue-web.conf
    a2enconf nexvue-web >/dev/null 2>&1 \
      && ok "Apache conf enabled: nexvue-web (RewriteRule → index.php)" \
      || warn "a2enconf nexvue-web failed"

    # HTTPS: mod_ssl + station certs under /etc/nexvue/tls (same as MediaMTX).
    a2enmod ssl >/dev/null 2>&1 \
      && ok "Apache mod_ssl enabled" \
      || warn "a2enmod ssl failed — enable manually for HTTPS"
    install -m 644 "${REPO_DIR}/nexvue-ssl-apache.conf" \
      /etc/apache2/conf-available/nexvue-ssl-certs.conf
    a2enconf nexvue-ssl-certs >/dev/null 2>&1 \
      && ok "Apache conf enabled: nexvue-ssl-certs → /etc/nexvue/tls" \
      || warn "a2enconf nexvue-ssl-certs failed"
    if command -v python3 >/dev/null 2>&1; then
      SSL_PATCH="$(python3 <<'PY'
import pathlib, re
cert = "/etc/nexvue/tls/fullchain.pem"
key = "/etc/nexvue/tls/privkey.pem"
roots = []
for dname in ("sites-available", "sites-enabled"):
    d = pathlib.Path("/etc/apache2") / dname
    if d.is_dir():
        roots.append(d)
seen = set()
n_file = n_key = 0
for d in roots:
    for p in sorted(d.iterdir()):
        try:
            real = p.resolve()
        except OSError:
            continue
        if real in seen or not real.is_file():
            continue
        seen.add(real)
        try:
            text = real.read_text(encoding="utf-8")
        except OSError:
            continue
        orig = text
        text, a = re.subn(
            r"^(\s*SSLCertificateFile\s+)\S+",
            rf"\1{cert}",
            text,
            flags=re.M,
        )
        text, b = re.subn(
            r"^(\s*SSLCertificateKeyFile\s+)\S+",
            rf"\1{key}",
            text,
            flags=re.M,
        )
        if text != orig:
            real.write_text(text, encoding="utf-8")
            n_file += a
            n_key += b
            print(f"patched SSL cert paths in {real}")
print(f"SUMMARY {n_file} {n_key}")
PY
)"
      _ssl_sum="$(echo "${SSL_PATCH}" | grep '^SUMMARY' | awk '{print $2" "$3}')"
      echo "${SSL_PATCH}" | grep -v '^SUMMARY' | while read -r line; do
        [ -n "$line" ] && ok "$line"
      done
      if [ -n "${_ssl_sum}" ] && [ "${_ssl_sum}" != "0 0" ]; then
        ok "Apache SSLCertificate* → /etc/nexvue/tls (${_ssl_sum} replacements)"
      else
        ok "Apache SSLCertificate* already at /etc/nexvue/tls (or no SSL vhost lines yet)"
      fi
    fi
    # Ensure at least one SSL site (Ubuntu default-ssl) when none is enabled.
    if [ -f /etc/apache2/sites-available/default-ssl.conf ]; then
      if ! ls /etc/apache2/sites-enabled/*ssl* >/dev/null 2>&1 \
          && ! grep -RqsE '^\s*SSLEngine\s+on' /etc/apache2/sites-enabled 2>/dev/null; then
        a2ensite default-ssl >/dev/null 2>&1 \
          && ok "enabled Apache site default-ssl (HTTPS :443)" \
          || warn "a2ensite default-ssl failed — enable an SSL vhost manually"
      else
        ok "Apache SSL site already enabled"
      fi
    fi

    # Point vhosts at ${PUBLIC}. Prefer rewriting the real files under
    # sites-available (sites-enabled is usually a symlink).
    if command -v python3 >/dev/null 2>&1; then
      PATCH_OUT="$(python3 - "${WEBROOT}" "${PUBLIC}" <<'PY'
import pathlib, re, sys
app, pub = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
app_s, pub_s = str(app), str(pub)
# Match DocumentRoot /var/www/html  or  DocumentRoot "/var/www/html"
# but not already .../public (unless it equals app somehow).
pat = re.compile(
    r'^(?P<prefix>\s*DocumentRoot\s+)"?(?P<path>'
    + re.escape(app_s)
    + r')/?("?)(?P<suffix>\s*(?:#.*)?)$',
    re.MULTILINE,
)
roots = []
for dname in ("sites-available", "sites-enabled"):
    d = pathlib.Path("/etc/apache2") / dname
    if d.is_dir():
        roots.append(d)
seen = set()
patched = 0
already = 0
for d in roots:
    for p in sorted(d.iterdir()):
        # Resolve symlinks once so we don't double-write.
        try:
            real = p.resolve()
        except OSError:
            continue
        if real in seen or not real.is_file():
            continue
        seen.add(real)
        try:
            text = real.read_text(encoding="utf-8")
        except OSError:
            continue
        # Skip JWKS loopback conf (must stay on app root for nexvue-jwks.php).
        if "jwks" in real.name.lower():
            continue
        if re.search(
            r'^\s*DocumentRoot\s+"?' + re.escape(pub_s) + r'/?("|\s|$)',
            text,
            re.MULTILINE,
        ):
            already += 1
            continue

        def repl(m: re.Match) -> str:
            # Preserve quoting style when quotes were used.
            if '"' in m.group(0):
                return f'{m.group("prefix")}"{pub_s}"{m.group("suffix")}'
            return f'{m.group("prefix")}{pub_s}{m.group("suffix")}'

        new, n = pat.subn(repl, text)
        if n == 0:
            continue
        try:
            real.write_text(new, encoding="utf-8")
            print(f"patched DocumentRoot → {pub_s} in {real}")
            patched += n
        except OSError as e:
            print(f"warn: could not patch {real}: {e}", file=sys.stderr)
print(f"SUMMARY {patched} {already}")
PY
)"
      while IFS= read -r line; do
        case "$line" in
          patched\ *) ok "$line" ;;
          SUMMARY\ *)
            set -- ${line#SUMMARY }
            _patched="${1:-0}"
            _already="${2:-0}"
            if [ "${_patched}" -gt 0 ] 2>/dev/null; then
              ok "DocumentRoot now ${PUBLIC} (${_patched} replacement(s))"
            elif [ "${_already}" -gt 0 ] 2>/dev/null; then
              ok "DocumentRoot already ${PUBLIC}"
            else
              warn "no DocumentRoot matched ${WEBROOT} — set DocumentRoot ${PUBLIC} in your SSL/default site manually"
            fi
            ;;
          warn:*) warn "${line#warn: }" ;;
        esac
      done <<EOF
${PATCH_OUT}
EOF
    else
      warn "python3 missing — cannot auto-patch DocumentRoot; set DocumentRoot ${PUBLIC} manually"
    fi

    # UI is still HTTPS-only for real content, but Apache keeps :80 open
    # specifically to 301 redirect stray HTTP hits to the same URL under
    # https:// (nexvue_web_https_redirect_target() in nexvue-web-router.php
    # does the actual redirect; JWKS loopback on :9080 is a separate vhost
    # and untouched either way). Closing :80 outright used to send anyone
    # who reached the UI via a stale http:// bookmark into a dead end: the
    # browser inherited http: for the WHEP fetch too, and MediaMTX's :8889
    # listener is TLS-only, so it just reset the connection with no
    # explanation. 000-default already has DocumentRoot ${PUBLIC} from the
    # patch above, so the same router handles both ports.
    if [ -e /etc/apache2/sites-available/000-default.conf ]; then
      a2ensite 000-default >/dev/null 2>&1 \
        && ok "Apache site 000-default enabled (HTTP :80 -> HTTPS redirect)" \
        || warn "a2ensite 000-default failed — enable the HTTP vhost manually"
    else
      warn "no /etc/apache2/sites-available/000-default.conf — HTTP :80 will not respond; create a vhost with DocumentRoot ${PUBLIC} manually"
    fi
    if [ -f /etc/apache2/ports.conf ]; then
      _http_on="$(python3 "${REPO_DIR}/nexvue-apache-http-on.py" /etc/apache2/ports.conf)"
      if [ "${_http_on}" = "patched" ]; then
        ok "Apache Listen 80 enabled in ports.conf"
      else
        ok "Apache Listen 80 already on in ports.conf"
      fi
    fi
    systemctl enable apache2 >/dev/null 2>&1 \
      && ok "apache2 enabled on boot" \
      || warn "systemctl enable apache2 failed"

    if command -v apache2ctl >/dev/null 2>&1; then
      if apache2ctl -M 2>/dev/null | grep -q rewrite_module; then
        ok "apache2ctl: rewrite_module loaded"
      else
        warn "rewrite_module not loaded — run: sudo a2enmod rewrite && sudo systemctl reload apache2"
      fi
      if apache2ctl configtest >/dev/null 2>&1; then
        if systemctl is-active --quiet apache2 2>/dev/null; then
          systemctl reload apache2 >/dev/null 2>&1 \
            && ok "apache2 reloaded (front door)" \
            || warn "apache2 reload failed — sudo systemctl restart apache2"
        fi
      else
        warn "apache2ctl configtest failed — not reloading; fix sites and re-run setup.sh"
        apache2ctl configtest 2>&1 | head -n 20 || true
      fi
    fi

    # Smoke: HTTP :80 redirects to HTTPS; the front controller itself is
    # HTTPS-only.
    if systemctl is-active --quiet apache2 2>/dev/null; then
      _http_loc="$(curl -fsS -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 2 "http://127.0.0.1/login" 2>/dev/null || true)"
      case "${_http_loc}" in
        30*\ https://*)
          ok "Apache HTTP :80 redirects to HTTPS (${_http_loc})" ;;
        *)
          warn "Apache :80 did not redirect to HTTPS as expected (got: ${_http_loc:-no response}) — check 000-default is enabled with DocumentRoot ${PUBLIC} and Listen 80 is on" ;;
      esac
      if curl -fskS --max-time 3 "https://127.0.0.1/login" 2>/dev/null | grep -qi 'NexVUE'; then
        ok "front-door smoke OK: https://127.0.0.1/login"
      else
        warn "front-door smoke failed (curl https://127.0.0.1/login) — check DocumentRoot is ${PUBLIC}, mod_ssl, and mod_rewrite"
      fi
      if curl -fskS --max-time 3 "https://127.0.0.1/api/version" 2>/dev/null \
          | grep -q '"version"'; then
        ok "front-door API smoke OK: https://127.0.0.1/api/version"
      else
        warn "front-door API smoke failed (/api/version) — rewrite may not be routing to index.php"
      fi
    fi
  elif [ -d /etc/apache2 ]; then
    warn "Apache present but conf-available/a2enmod missing — enable rewrite and DocumentRoot ${PUBLIC} manually"
  else
    warn "Apache conf-available missing — set DocumentRoot to ${PUBLIC} and enable mod_rewrite manually"
  fi
else
  warn "Apache app root ${WEBROOT} missing — create it and re-run setup.sh"
fi

install -m 644 "${REPO_DIR}/nexvue-mediamtx-jwt-patch.py" /usr/local/share/nexvue/nexvue-mediamtx-jwt-patch.py
install -m 644 "${REPO_DIR}/nexvue-apache-http-on.py" /usr/local/share/nexvue/nexvue-apache-http-on.py
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

apply_nexvue_firewall

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
      rtspclientsink)   warn "missing $el — install gstreamer1.0-rtsp (setup apt step); encode publish will fail" ;;
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
[ -x /usr/local/bin/nexvue-encode.py ] \
  && ok "nexvue-encode.py present (persistent publish + disposable capture)" \
  || warn "nexvue-encode.py missing — nexvue-encode@N will not start"
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
if systemctl is-enabled --quiet apache2 2>/dev/null; then
  ok "apache2 is enabled"
else
  warn "apache2 not enabled on boot — sudo systemctl enable apache2"
fi
if systemctl is-active --quiet ssh 2>/dev/null || systemctl is-active --quiet sshd 2>/dev/null; then
  ok "sshd is active"
else
  warn "sshd not running — sudo systemctl enable --now ssh"
fi
if systemctl is-enabled --quiet ssh 2>/dev/null || systemctl is-enabled --quiet sshd 2>/dev/null; then
  ok "sshd is enabled"
else
  warn "sshd not enabled on boot — sudo systemctl enable ssh"
fi
if [ -f /etc/apache2/ports.conf ]; then
  if python3 "${REPO_DIR}/nexvue-apache-http-on.py" /etc/apache2/ports.conf --check \
      >/dev/null 2>&1; then
    ok "Apache Listen 80 is on"
  else
    warn "Apache Listen 80 is off — HTTP will not redirect to HTTPS; re-run sudo ./setup.sh"
  fi
fi
if [ -e /etc/apache2/sites-enabled/000-default.conf ]; then
  ok "Apache HTTP site 000-default is enabled (redirects to HTTPS)"
elif [ -d /etc/apache2/sites-enabled ]; then
  warn "Apache 000-default not enabled — HTTP :80 will not redirect to HTTPS; re-run sudo ./setup.sh"
fi
if command -v ufw >/dev/null 2>&1; then
  if ufw status 2>/dev/null | grep -q "Status: active"; then
    ok "ufw is active"
    if ufw status 2>/dev/null | grep -qE 'OpenSSH|[[:space:]]22/tcp'; then
      ok "ufw allows SSH"
    else
      warn "ufw active but SSH not allowed — re-run sudo ./setup.sh"
    fi
    if ufw status 2>/dev/null | grep -qE '\b443/tcp\b'; then
      ok "ufw allows 443/tcp"
    else
      warn "ufw active but 443/tcp not allowed — re-run sudo ./setup.sh"
    fi
    if ufw status 2>/dev/null | grep -qE '\b80/tcp\b'; then
      ok "ufw allows 80/tcp (HTTP -> HTTPS redirect only)"
    else
      warn "ufw does not allow 80/tcp — HTTP-to-HTTPS redirect will not be reachable; re-run sudo ./setup.sh"
    fi
  else
    warn "ufw is not active — sudo ./setup.sh allows SSH/443/8889/8189 and enables it"
  fi
else
  warn "ufw not installed — sudo ./setup.sh installs and enables it"
fi
WEBROOT="${NEXVUE_WEBROOT:-/var/www/html}"
PUBLIC="${WEBROOT}/public"
if [ -d "${WEBROOT}" ]; then
  for f in \
      public/index.php nexvue-web-router.php \
      pages/index.html pages/multiview.html pages/metrics.html pages/services.html \
      pages/channels.html pages/login.html pages/users.html \
      public/assets/nexvue-ui.js public/assets/nexvue-auth-gate.js \
      nexvue-auth.php nexvue-ops.php nexvue-jwks.php nexvue-metrics.php VERSION; do
    [ -f "${WEBROOT}/$f" ] && ok "web UI: ${WEBROOT}/$f" || warn "web UI missing: ${WEBROOT}/$f"
  done
  if [ -f /etc/apache2/conf-enabled/nexvue-web.conf ] || [ -L /etc/apache2/conf-enabled/nexvue-web.conf ]; then
    ok "apache front-door conf enabled: nexvue-web"
  elif [ -f /etc/apache2/conf-available/nexvue-web.conf ]; then
    warn "nexvue-web.conf installed but not enabled — sudo a2enconf nexvue-web && sudo systemctl reload apache2"
  else
    warn "nexvue-web.conf missing — UI may still be on a flat docroot"
  fi
  if command -v apache2ctl >/dev/null 2>&1 && apache2ctl -M 2>/dev/null | grep -q rewrite_module; then
    ok "Apache mod_rewrite loaded"
  else
    warn "Apache mod_rewrite not loaded — sudo a2enmod rewrite && sudo systemctl reload apache2"
  fi
  if command -v apache2ctl >/dev/null 2>&1 \
      && apache2ctl -S 2>/dev/null | grep -q "DocumentRoot: ${PUBLIC}"; then
    ok "Apache DocumentRoot is ${PUBLIC}"
  elif grep -RqsE "DocumentRoot[[:space:]]+\"?${PUBLIC}" /etc/apache2/sites-enabled 2>/dev/null; then
    ok "sites-enabled DocumentRoot points at ${PUBLIC}"
  else
    warn "DocumentRoot may not be ${PUBLIC} — path UI (/login, /api/*) will fail until fixed"
  fi
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

if [ -f /etc/nexvue/tls/fullchain.pem ] && [ -f /etc/nexvue/tls/privkey.pem ]; then
  ok "TLS certs: /etc/nexvue/tls/{fullchain,privkey}.pem"
else
  warn "TLS certs missing under /etc/nexvue/tls — re-run sudo ./setup.sh (creates self-signed if absent)"
fi
if [ -f /etc/nexvue/mediamtx.yml ] && grep -qE '^\s*webrtcServerCert:\s*/etc/nexvue/tls/fullchain\.pem\b' /etc/nexvue/mediamtx.yml \
    && grep -qE '^\s*webrtcServerKey:\s*/etc/nexvue/tls/privkey\.pem\b' /etc/nexvue/mediamtx.yml; then
  ok "mediamtx.yml WHEP TLS → /etc/nexvue/tls"
else
  warn "mediamtx.yml WHEP cert paths not /etc/nexvue/tls — re-run setup.sh"
fi
if [ -f /etc/apache2/conf-enabled/nexvue-ssl-certs.conf ] || [ -L /etc/apache2/conf-enabled/nexvue-ssl-certs.conf ]; then
  ok "Apache nexvue-ssl-certs.conf enabled"
elif [ -d /etc/apache2 ]; then
  warn "Apache nexvue-ssl-certs.conf not enabled — re-run setup.sh for HTTPS → /etc/nexvue/tls"
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
# --check --firewall: apply ufw without a full install
###############################################################################
if $CHECK_ONLY && $APPLY_FIREWALL; then
  apply_nexvue_firewall
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
  3. Tune channel .env files under /etc/nexvue/channels/ (seeded for each
     MAX_CHANNELS slot). Duo 2: set MAX_DEVICES=4 and MAX_CHANNELS=4 in
     /etc/nexvue/nexvue.env, then re-run setup.sh (disables encode@4..7).
  4. Services are enabled by setup: mediamtx, nexvue-status, nexvue-metrics,
     nexvue-decklink-configure, and nexvue-encode@0..(MAX_CHANNELS-1).
     Empty SDI ports auto-park; re-enable from Services when patched.
  5. TLS: /etc/nexvue/tls/{fullchain,privkey}.pem (self-signed if setup created
     them). Replace with a real cert when ready; Apache + MediaMTX already
     point there. Trust click-through once on https://<edge>:8889/ if self-signed.
     UI is HTTPS-only (Apache :80 / ufw 80 closed). Use https://<edge-ip>/login
     — plain http:// will not connect.
  6. Firewall: setup allows OpenSSH + 443/8889/8189 and enables ufw (HTTP :80
     is not allowed). Re-apply with sudo ./setup.sh --firewall if needed.
  7. Remove any Apache Basic Auth / .htaccess AuthType — app login replaces it.
     Confirm JWKS (MediaMTX): curl -fsS http://127.0.0.1:9080/nexvue-jwks.php | head
  8. Login:  https://<edge-ip>/login
     Default bootstrap: admin / password  (forced change on first login)
     Path UI: /player /multiview /metrics /settings /services /users
     If login shows "auth store unavailable": sudo ./setup.sh  (repairs
     /var/lib/nexvue/auth ownership + migrates legacy auth.db).
     If Player connects but never gets video after JWT auth: re-run setup.sh
     (JWKS loopback :9080 + publish JWT + encoder restart).
NEXT
