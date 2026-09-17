#!/usr/bin/env bash
# deploy-status.sh — observe served release identity without host mutation access.
set -euo pipefail

DOGFOOD_SUBDOMAIN="${LONGHOUSE_DEFAULT_SUBDOMAIN:-david010}"
# Authority is the repo variable of the same name (the deploy workflows read
# vars.HOSTED_CANARY_SUBDOMAIN). A hardcoded fallback here silently drifts: it
# kept reporting the retired kernel-canary as "unreachable" after the ring had
# already moved, which read as a dead canary and invited agents to keep
# cancelling deploys that had no target problem at all.
HOSTED_CANARY_SUBDOMAIN="${HOSTED_CANARY_SUBDOMAIN:-release-canary-a}"
DOGFOOD_HEALTH_URL="${DOGFOOD_HEALTH_URL:-https://${DOGFOOD_SUBDOMAIN}.longhouse.ai/api/health}"
HOSTED_CANARY_HEALTH_URL="${HOSTED_CANARY_HEALTH_URL:-https://${HOSTED_CANARY_SUBDOMAIN}.longhouse.ai/api/health}"
DEMO_HEALTH_URL="${DEMO_HEALTH_URL:-https://longhouse.ai/api/health}"
CP_HEALTH_URL="${CP_HEALTH_URL:-https://control.longhouse.ai/health}"

health_json() {
  curl -sf --max-time 10 "$1" 2>/dev/null || printf '{}'
}
health_field() {
  local body="$1"
  local field="$2"
  python3 -c '
import json
import sys
try:
    value = json.load(sys.stdin)
except Exception:
    value = {}
field = sys.argv[1]
build = value.get("build") or {}
if field == "status":
    print(value.get("status", "unreachable"))
elif field == "source_sha":
    print(
        value.get("source_sha")
        or build.get("source_sha")
        or build.get("commit")
        or value.get("commit")
        or "-"
    )
elif field == "build_identity":
    print(value.get("build_identity") or build.get("build_identity") or "-")
else:
    print(value.get(field) or build.get(field) or "-")
' "$field" <<<"$body"
}

short_sha() {
  local value="$1"
  if [[ "$value" != "-" && ${#value} -gt 12 ]]; then
    printf '%s\n' "${value:0:10}"
  else
    printf '%s\n' "$value"
  fi
}

observe_surface() {
  local url="$1"
  local body status source_sha build_identity
  body="$(health_json "$url")"
  status="$(health_field "$body" status)"
  source_sha="$(short_sha "$(health_field "$body" source_sha)")"
  build_identity="$(health_field "$body" build_identity)"
  printf '%s\t%s\t%s\n' "$source_sha" "$status" "$build_identity"
}

IFS=$'\t' read -r demo_sha demo_health demo_identity <<<"$(observe_surface "$DEMO_HEALTH_URL")"
IFS=$'\t' read -r cp_sha cp_health cp_identity <<<"$(observe_surface "$CP_HEALTH_URL")"
IFS=$'\t' read -r dogfood_sha dogfood_health dogfood_identity <<<"$(observe_surface "$DOGFOOD_HEALTH_URL")"
IFS=$'\t' read -r canary_sha canary_health canary_identity <<<"$(observe_surface "$HOSTED_CANARY_HEALTH_URL")"
local_sha="$(git rev-parse --short=10 HEAD 2>/dev/null || echo '-')"

printf '\n'
printf '%-24s %-12s %-14s %-28s %s\n' 'Surface' 'SHA' 'Health' 'Build identity' 'Uptime'
printf '%-24s %-12s %-14s %-28s %s\n' '-------' '---' '------' '-------------' '------'
printf '%-24s %-12s %-14s %-28s %s\n' 'Demo runtime' "$demo_sha" "$demo_health" "$demo_identity" '-'
printf '%-24s %-12s %-14s %-28s %s\n' 'Control plane' "$cp_sha" "$cp_health" "$cp_identity" '-'
printf '%-24s %-12s %-14s %-28s %s\n' "Dogfood $DOGFOOD_SUBDOMAIN" "$dogfood_sha" "$dogfood_health" "$dogfood_identity" '-'
printf '%-24s %-12s %-14s %-28s %s\n' "Canary $HOSTED_CANARY_SUBDOMAIN" "$canary_sha" "$canary_health" "$canary_identity" '-'
printf '%-24s %-12s\n' 'Local HEAD' "$local_sha"
printf '\n'

if [[ "$demo_sha" != '-' && "$demo_sha" != "$local_sha" ]]; then
  echo "Local HEAD ($local_sha) differs from deployed demo ($demo_sha)"
fi
