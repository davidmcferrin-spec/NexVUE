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
  nexvue-encode.sh nexvue-encode@.service
  nexvue-status-server.py nexvue-status.service
  nexvue-metrics-server.py nexvue-metrics.service
  nexvue-metrics.php nexvue-status.php nexvue-qr.js metrics.html index.html multiview.html
  cast-receiver.html
  nexvue-ops.php services.html channels.html
  nexvue-ops-env-update.py nexvue-ops.sudoers
  nexvue-ops-status.sh nexvue-ops-journal.sh
  nexvue-ops-env-read.sh nexvue-ops-env-write.sh nexvue-ops-restart.sh
  channels-example.env
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
  intel-media-va-driver-non-free vainfo \
  build-essential curl ca-certificates jq \
  php-sqlite3
ok "apt packages installed (python: stdlib only — no pip; php-sqlite3 for metrics.php)"

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
mkdir -p /etc/nexvue/channels
ok "/etc/nexvue/channels ready"

step "4/5 NexVUE files"
# Config: never clobber a live config — install only if absent.
if [ -f /etc/nexvue/mediamtx.yml ]; then
  ok "/etc/nexvue/mediamtx.yml exists — left untouched (diff against repo manually)"
else
  install -m 644 "${REPO_DIR}/mediamtx.yml" /etc/nexvue/mediamtx.yml
  ok "installed mediamtx.yml"
fi
install -m 755 "${REPO_DIR}/nexvue-encode.sh" /usr/local/bin/nexvue-encode.sh
install -m 755 "${REPO_DIR}/nexvue-status-server.py" /usr/local/bin/nexvue-status-server.py
install -m 755 "${REPO_DIR}/nexvue-metrics-server.py" /usr/local/bin/nexvue-metrics-server.py
install -m 755 "${REPO_DIR}/nexvue-ops-env-update.py" /usr/local/bin/nexvue-ops-env-update.py
install -m 755 "${REPO_DIR}/nexvue-ops-status.sh" /usr/local/bin/nexvue-ops-status.sh
install -m 755 "${REPO_DIR}/nexvue-ops-journal.sh" /usr/local/bin/nexvue-ops-journal.sh
install -m 755 "${REPO_DIR}/nexvue-ops-env-read.sh" /usr/local/bin/nexvue-ops-env-read.sh
install -m 755 "${REPO_DIR}/nexvue-ops-env-write.sh" /usr/local/bin/nexvue-ops-env-write.sh
install -m 755 "${REPO_DIR}/nexvue-ops-restart.sh" /usr/local/bin/nexvue-ops-restart.sh
install -m 644 "${REPO_DIR}/mediamtx.service" \
               "${REPO_DIR}/nexvue-encode@.service" \
               "${REPO_DIR}/nexvue-status.service" \
               "${REPO_DIR}/nexvue-metrics.service" /etc/systemd/system/
systemctl daemon-reload
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
    warn "nexvue-ops.sudoers failed visudo -cf — NOT installed; Services/Channels pages will not work until fixed"
  fi
  rm -f "${TMP_SUDOERS}"
else
  warn "visudo not found — copy nexvue-ops.sudoers to /etc/sudoers.d/nexvue-ops manually (mode 0440)"
fi

# Apache docroot: player, multiviewer, metrics, ops pages + PHP.
# Override with NEXVUE_WEBROOT if the site isn't under /var/www/html.
WEBROOT="${NEXVUE_WEBROOT:-/var/www/html}"
if [ -d "${WEBROOT}" ]; then
  install -m 644 "${REPO_DIR}/index.html" \
                 "${REPO_DIR}/multiview.html" \
                 "${REPO_DIR}/metrics.html" \
                 "${REPO_DIR}/cast-receiver.html" \
                 "${REPO_DIR}/nexvue-metrics.php" \
                 "${REPO_DIR}/nexvue-status.php" \
                 "${REPO_DIR}/nexvue-qr.js" \
                 "${REPO_DIR}/services.html" \
                 "${REPO_DIR}/channels.html" \
                 "${REPO_DIR}/nexvue-ops.php" \
                 "${WEBROOT}/"
  ok "web UI installed to ${WEBROOT} (player / multiview / metrics / services / channels / cast)"
else
  warn "Apache docroot ${WEBROOT} missing — after Apache is up: sudo cp index.html multiview.html metrics.html cast-receiver.html nexvue-metrics.php nexvue-status.php nexvue-qr.js services.html channels.html nexvue-ops.php ${WEBROOT}/"
fi

step "5/5 decklink-status helper"
if [ -x /usr/local/bin/decklink-status ]; then
  ok "decklink-status already installed"
elif [ -f "${REPO_DIR}/decklink-status.cpp" ] && ls /opt/decklink-sdk/Linux/include/DeckLinkAPI.h >/dev/null 2>&1; then
  ( cd "${REPO_DIR}" && make DECKLINK_SDK=/opt/decklink-sdk && make install )
  ok "decklink-status built from SDK at /opt/decklink-sdk"
else
  warn "decklink-status not built — download the DeckLink SDK, then: make DECKLINK_SDK=/path && sudo make install (player input-status dots stay grey until then)"
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

# GStreamer elements
for el in decklinkvideosrc vah264enc x264enc watchdog deinterlace opusenc rtspclientsink; do
  if gst-inspect-1.0 "$el" >/dev/null 2>&1; then
    ok "gstreamer element: $el"
  else
    case "$el" in
      decklinkvideosrc) warn "missing $el — install Blackmagic Desktop Video (deb) and reboot" ;;
      vah264enc)        warn "missing $el — VA driver issue (see vainfo above); x264enc fallback works for 1-2 channels only" ;;
      *)                warn "missing $el — check gstreamer package install" ;;
    esac
  fi
done

# MediaMTX + units
[ -x /usr/local/bin/mediamtx ] && ok "mediamtx binary present" || warn "mediamtx binary missing"
for u in mediamtx.service nexvue-encode@.service nexvue-status.service nexvue-metrics.service; do
  [ -f "/etc/systemd/system/$u" ] && ok "unit installed: $u" || warn "unit missing: $u"
done

# Metrics PHP reader (SQLite) + web UI
if command -v php >/dev/null 2>&1 && php -m 2>/dev/null | grep -qi sqlite3; then
  ok "php sqlite3 extension present (nexvue-metrics.php)"
else
  warn "php sqlite3 missing — apt install php-sqlite3 (and libapache2-mod-php if Apache has no PHP yet)"
fi
WEBROOT="${NEXVUE_WEBROOT:-/var/www/html}"
if [ -d "${WEBROOT}" ]; then
  for f in index.html multiview.html metrics.html cast-receiver.html nexvue-metrics.php nexvue-status.php nexvue-qr.js services.html channels.html nexvue-ops.php; do
    [ -f "${WEBROOT}/$f" ] && ok "web UI: ${WEBROOT}/$f" || warn "web UI missing: ${WEBROOT}/$f"
  done
else
  warn "Apache docroot ${WEBROOT} not present — web UI not deployed yet"
fi

# Ops wrappers + sudoers
for w in nexvue-ops-status.sh nexvue-ops-journal.sh nexvue-ops-env-read.sh \
         nexvue-ops-env-write.sh nexvue-ops-restart.sh nexvue-ops-env-update.py; do
  [ -x "/usr/local/bin/$w" ] || [ -f "/usr/local/bin/$w" ] \
    && ok "ops helper: $w" || warn "ops helper missing: /usr/local/bin/$w"
done
if [ -f /etc/sudoers.d/nexvue-ops ]; then
  ok "sudoers drop-in: /etc/sudoers.d/nexvue-ops"
else
  warn "sudoers drop-in missing — Services/Channels need /etc/sudoers.d/nexvue-ops"
fi

# decklink-status
[ -x /usr/local/bin/decklink-status ] && ok "decklink-status helper present" \
  || warn "decklink-status helper not installed (optional; see step 5)"

###############################################################################
# Optional: Phase 1 firewall rules (only with --firewall — never silent)
###############################################################################
if $APPLY_FIREWALL; then
  step "Firewall (ufw) — Phase 1 LAN rules"
  if ! command -v ufw >/dev/null; then
    warn "ufw not installed — skipping (apt install ufw to use --firewall)"
  else
    # NOTE: does not enable ufw for you — enabling can drop your SSH session if
    # 22 isn't already allowed. Opens NexVUE ports only; you enable ufw.
    ufw allow 80/tcp comment 'NexVUE player (Apache)' >/dev/null
    ufw allow 8889/tcp comment 'NexVUE WHEP signaling' >/dev/null
    ufw allow 8189 comment 'NexVUE WebRTC media (UDP+TCP)' >/dev/null
    ufw allow 9997/tcp comment 'NexVUE MediaMTX API' >/dev/null
    ufw allow 9998/tcp comment 'NexVUE status daemon' >/dev/null
    # Metrics has NO port at all — the collector doesn't listen on anything;
    # PHP reads its SQLite file directly and Apache serves the result on 443.
    # See README "Usage Metrics Dashboard" for that install.
    ok "NexVUE ports opened (80,8889,8189/udp+tcp,9997,9998)"
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
       sudo systemctl enable --now mediamtx nexvue-status nexvue-metrics nexvue-encode@0
  5. Firewall (if ufw is in use): open ports with
       sudo ./setup.sh --firewall     (then: sudo ufw enable, once 22/ssh is allowed)
     or apply the rules manually — see the Firewall section in README.md
  6. Apache + PHP (if not already serving pages):
       sudo apt install -y libapache2-mod-php   # if PHP module not enabled yet
       sudo a2enmod php8.3                      # adjust version; then: sudo systemctl restart apache2
       # setup.sh copies web UI + nexvue-ops.php into /var/www/html when present
       # (override with NEXVUE_WEBROOT=) and installs /etc/sudoers.d/nexvue-ops.
  7. Verify:  http://<edge-ip>:8889/ch0
              http://<edge-ip>/index.html  (Player) /multiview.html /metrics.html
              http://<edge-ip>/services.html  /channels.html
     Ops pages are LAN-trust — do not DMZ-expose without Phase 2 auth.
NEXT
