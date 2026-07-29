#!/usr/bin/env bash
# Exercises nexvue-ops-update.sh status: remote_version + changelog JSON.
# Run: bash test/test-ops-update-changelog.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HELPER="${ROOT}/nexvue-ops-update.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }

[[ -x "$HELPER" || -f "$HELPER" ]] || fail "missing $HELPER"
chmod +x "$HELPER"

ETC="$TMP/etc"
DATA="$TMP/data"
BARE="$TMP/bare.git"
WORK="$TMP/work"
mkdir -p "$ETC" "$DATA"
git init --bare "$BARE" >/dev/null

git clone "$BARE" "$WORK" >/dev/null 2>&1
cd "$WORK"
git config user.email "test@nexvue.local"
git config user.name "NexVUE Test"
printf '1.0.0\n' > VERSION
printf '#!/bin/bash\nexit 0\n' > setup.sh
chmod +x setup.sh
git add VERSION setup.sh
git commit -m "release 1.0.0 initial" >/dev/null
git push -u origin HEAD:main >/dev/null 2>&1

printf '1.0.1\n' > VERSION
git add VERSION
git commit -m "fix captions idle erase" >/dev/null
printf '1.0.2\n' > VERSION
git add VERSION
git commit -m "Services: clearer update status" >/dev/null
git push origin HEAD:main >/dev/null 2>&1

# Leave clone two commits behind origin/main.
git reset --hard HEAD~2 >/dev/null

export NEXVUE_ETC="$ETC"
export NEXVUE_DATA="$DATA"
export NEXVUE_REPO="$WORK"
export NEXVUE_UPDATE_BRANCH=main

JSON="$("$HELPER" status)"
echo "$JSON" | grep -q '"ok":true' || fail "status not ok: $JSON"
echo "$JSON" | grep -q '"version":"1.0.0"' || fail "local version: $JSON"
echo "$JSON" | grep -q '"remote_version":"1.0.2"' || fail "remote_version: $JSON"
echo "$JSON" | grep -q '"behind":2' || fail "behind: $JSON"
echo "$JSON" | grep -q '"update_available":true' || fail "update_available: $JSON"
echo "$JSON" | grep -q 'Services: clearer update status' || fail "changelog missing newest subject: $JSON"
echo "$JSON" | grep -q 'fix captions idle erase' || fail "changelog missing older subject: $JSON"
# Operator-facing fields present; SHA still in JSON but UI no longer paints it.
echo "$JSON" | grep -q '"git_sha":"' || fail "git_sha missing: $JSON"
echo "$JSON" | grep -q '"changelog":\[' || fail "changelog array: $JSON"

pass "status reports remote_version + changelog when behind"

# Up to date: catch up and re-check.
git reset --hard origin/main >/dev/null
JSON2="$("$HELPER" status)"
echo "$JSON2" | grep -q '"behind":0' || fail "behind after catch-up: $JSON2"
echo "$JSON2" | grep -q '"update_available":false' || fail "update_available after catch-up: $JSON2"
echo "$JSON2" | grep -q '"changelog":\[\]' || fail "empty changelog when current: $JSON2"
echo "$JSON2" | grep -q '"remote_version":"1.0.2"' || fail "remote_version when current: $JSON2"

pass "status empty changelog when up to date"
echo "ALL PASSED"
