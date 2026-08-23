#!/usr/bin/env bash
# nexvue-tls-deploy.sh — lego --deploy-hook. Copies LEGO_CERT_* into /etc/nexvue/tls.
set -euo pipefail
exec /usr/bin/python3 "${NEXVUE_TLS_PY:-/usr/local/bin/nexvue-tls.py}" deploy
