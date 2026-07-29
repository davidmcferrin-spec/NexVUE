#!/usr/bin/env bash
# Allowlisted git update + redeploy for NexVUE Services UI.
#
# Usage:
#   nexvue-ops-update.sh status   — JSON: version, git, ahead/behind, dirty
#   nexvue-ops-update.sh apply    — fetch, hard-reset to origin/<branch>, setup.sh
#
# Repo path: NEXVUE_REPO, else /etc/nexvue/repo.path (written by setup.sh).
# Branch:    NEXVUE_UPDATE_BRANCH (default main), else from nexvue.env if set.
#
# Prints one JSON object on stdout (and only that — progress goes to stderr).
set -euo pipefail

json_escape() {
  # Minimal JSON string escape for paths / short messages.
  local s="${1:-}"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\r'/}"
  s="${s//$'\t'/\\t}"
  printf '%s' "$s"
}

fail_json() {
  trap - ERR
  local msg="$1"
  echo "{\"ok\":false,\"error\":\"$(json_escape "$msg")\"}"
  exit 1
}

# Any unexpected set -e abort still returns JSON (PHP parses stdout).
trap 'ec=$?; trap - ERR; echo "{\"ok\":false,\"error\":\"update helper aborted (exit ${ec})\"}"; exit 1' ERR

CMD="${1:-}"
case "$CMD" in
  status|apply) ;;
  *)
    fail_json "usage: nexvue-ops-update.sh status|apply"
    ;;
esac

ETC="${NEXVUE_ETC:-/etc/nexvue}"
DATA="${NEXVUE_DATA:-/var/lib/nexvue}"
ENV_FILE="${ETC}/nexvue.env"
REPO_PATH_FILE="${ETC}/repo.path"
VERSION_STAMP="${DATA}/version.json"

# Optional station overrides from nexvue.env (safe KEY=value lines only).
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a
  # shellcheck source=/dev/null
  source <(grep -E '^(NEXVUE_REPO|NEXVUE_UPDATE_BRANCH)=' "$ENV_FILE" 2>/dev/null || true)
  set +a
fi

REPO="${NEXVUE_REPO:-}"
if [[ -z "$REPO" && -f "$REPO_PATH_FILE" ]]; then
  REPO="$(tr -d '\r\n' < "$REPO_PATH_FILE")"
fi
BRANCH="${NEXVUE_UPDATE_BRANCH:-main}"

if [[ -z "$REPO" ]]; then
  fail_json "repo path unknown — re-run setup.sh from the clone (writes /etc/nexvue/repo.path) or set NEXVUE_REPO in nexvue.env"
fi
if [[ ! -d "$REPO/.git" ]]; then
  fail_json "not a git clone: $REPO"
fi
if [[ ! -f "$REPO/setup.sh" || ! -f "$REPO/VERSION" ]]; then
  fail_json "clone missing setup.sh or VERSION: $REPO"
fi

# Reject path metacharacters (defense in depth; path comes from root-written file).
if [[ "$REPO" =~ [\$\`\;\|\&\<\>\ \'\"\\] ]]; then
  fail_json "disallowed characters in repo path"
fi
if [[ "$BRANCH" =~ [^A-Za-z0-9._/-] || ${#BRANCH} -gt 128 ]]; then
  fail_json "invalid NEXVUE_UPDATE_BRANCH"
fi

export GIT_TERMINAL_PROMPT=0
export GIT_ASKPASS=echo

cd "$REPO"

read_version() {
  local v
  v="$(tr -d '[:space:]' < "$REPO/VERSION" 2>/dev/null || true)"
  if [[ ! "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$ ]]; then
    v="0.0.0"
  fi
  printf '%s' "$v"
}

git_sha() {
  git rev-parse --short=12 HEAD 2>/dev/null || echo ""
}

git_full() {
  git rev-parse HEAD 2>/dev/null || echo ""
}

write_stamp() {
  mkdir -p "$DATA"
  local ver sha full br ts
  ver="$(read_version)"
  sha="$(git_sha)"
  full="$(git_full)"
  br="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "$BRANCH")"
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  cat > "$VERSION_STAMP" <<EOF
{
  "version": "$(json_escape "$ver")",
  "git_sha": "$(json_escape "$sha")",
  "git_full": "$(json_escape "$full")",
  "git_branch": "$(json_escape "$br")",
  "repo": "$(json_escape "$REPO")",
  "updated_at": "$(json_escape "$ts")"
}
EOF
  chmod 644 "$VERSION_STAMP" 2>/dev/null || true
}

collect_status() {
  local ver sha dirty behind ahead remote_sha fetch_note
  ver="$(read_version)"
  sha="$(git_sha)"
  dirty=false
  if [[ -n "$(git status --porcelain 2>/dev/null || true)" ]]; then
    dirty=true
  fi
  behind=0
  ahead=0
  remote_sha=""
  fetch_note=""
  local fetch_err
  fetch_err="$(mktemp)"
  if git fetch --quiet origin "$BRANCH" 2>"$fetch_err"; then
    remote_sha="$(git rev-parse --short=12 "origin/${BRANCH}" 2>/dev/null || echo "")"
    behind="$(git rev-list --count "HEAD..origin/${BRANCH}" 2>/dev/null || echo 0)"
    ahead="$(git rev-list --count "origin/${BRANCH}..HEAD" 2>/dev/null || echo 0)"
  else
    fetch_note="$(tr -d '\r' <"$fetch_err" | tail -n 3 | tr '\n' ' ')"
  fi
  rm -f "$fetch_err"
  write_stamp
  printf '{'
  printf '"ok":true,'
  printf '"action":"status",'
  printf '"version":"%s",' "$(json_escape "$ver")"
  printf '"git_sha":"%s",' "$(json_escape "$sha")"
  printf '"git_branch":"%s",' "$(json_escape "$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "$BRANCH")")"
  printf '"update_branch":"%s",' "$(json_escape "$BRANCH")"
  printf '"repo":"%s",' "$(json_escape "$REPO")"
  printf '"dirty":%s,' "$dirty"
  printf '"behind":%s,' "${behind:-0}"
  printf '"ahead":%s,' "${ahead:-0}"
  printf '"remote_sha":"%s",' "$(json_escape "$remote_sha")"
  if [[ -n "$fetch_note" ]]; then
    printf '"fetch_warning":"%s",' "$(json_escape "$fetch_note")"
  fi
  printf '"update_available":%s' "$([[ "${behind:-0}" -gt 0 ]] && echo true || echo false)"
  printf '}\n'
}

apply_update() {
  echo "fetching origin/${BRANCH}…" >&2
  if ! git fetch --quiet origin "$BRANCH"; then
    fail_json "git fetch failed — check network / deploy credentials for origin"
  fi

  local behind
  behind="$(git rev-list --count "HEAD..origin/${BRANCH}" 2>/dev/null || echo 0)"
  if [[ "${behind}" -eq 0 ]]; then
    write_stamp
    local ver sha
    ver="$(read_version)"
    sha="$(git_sha)"
    printf '{'
    printf '"ok":true,'
    printf '"action":"apply",'
    printf '"changed":false,'
    printf '"version":"%s",' "$(json_escape "$ver")"
    printf '"git_sha":"%s",' "$(json_escape "$sha")"
    printf '"git_branch":"%s",' "$(json_escape "$BRANCH")"
    printf '"repo":"%s",' "$(json_escape "$REPO")"
    printf '"message":"already up to date"'
    printf '}\n'
    return 0
  fi

  echo "resetting to origin/${BRANCH} (${behind} commit(s) behind)…" >&2
  # Hard reset: Services Update discards local clone commits and tracked edits.
  # Untracked files (e.g. local notes) are left alone.
  git reset --hard "origin/${BRANCH}"

  echo "running setup.sh…" >&2
  # Keep stdout JSON-only — setup banners/progress must not pollute the parse.
  if ! bash "$REPO/setup.sh" >&2; then
    fail_json "setup.sh failed after git reset — clone is at origin/${BRANCH}; fix errors and re-run setup.sh"
  fi

  write_stamp
  # Also refresh webroot VERSION if setup copied it (setup does).
  local ver sha
  ver="$(read_version)"
  sha="$(git_sha)"
  printf '{'
  printf '"ok":true,'
  printf '"action":"apply",'
  printf '"changed":true,'
  printf '"version":"%s",' "$(json_escape "$ver")"
  printf '"git_sha":"%s",' "$(json_escape "$sha")"
  printf '"git_branch":"%s",' "$(json_escape "$BRANCH")"
  printf '"repo":"%s",' "$(json_escape "$REPO")"
  printf '"behind_applied":%s,' "$behind"
  printf '"message":"updated and redeployed — restart encoders if needed"'
  printf '}\n'
}

case "$CMD" in
  status) collect_status ;;
  apply)  apply_update ;;
esac
