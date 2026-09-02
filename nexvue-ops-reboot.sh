#!/usr/bin/env bash
# nexvue-ops-reboot.sh — allowlisted host reboot for Services (admin).
#
# Usage: nexvue-ops-reboot.sh
# No arguments. Invokes `systemctl reboot` (clean systemd shutdown — not
# reboot -f). Extra args are rejected so sudoers can omit a trailing *.
#
# Tests stub systemctl on PATH (see test/test_nexvue_ops_reboot.py).
set -euo pipefail

[ "$#" -eq 0 ] || { echo "usage: nexvue-ops-reboot.sh" >&2; exit 2; }

exec systemctl reboot
