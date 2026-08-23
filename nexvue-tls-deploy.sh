#!/usr/bin/env bash
# nexvue-tls-deploy.sh — lego --deploy-hook. Copies the issued pair into
# /etc/nexvue/tls (lego v5 LEGO_HOOK_CERT_*; v4 LEGO_CERT_* fallback).
set -euo pipefail
exec /usr/bin/python3 "${NEXVUE_TLS_PY:-/usr/local/bin/nexvue-tls.py}" deploy
