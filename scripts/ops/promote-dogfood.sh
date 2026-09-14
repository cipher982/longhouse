#!/usr/bin/env bash
# Promote a canary-verified runtime image to the owner's dogfood instance.
#
# Usage: scripts/ops/promote-dogfood.sh [SHA]
#
# SHA defaults to the newest main commit whose push-triggered "Deploy and
# Verify" run succeeded; an explicit SHA must have such a run. The dogfood
# instance is updated on purpose with an image that already passed the canary
# pipeline -- never by an ordinary push, which only reaches demo and canary.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO="${GITHUB_REPOSITORY:-cipher982/longhouse}"
IMAGE_REPO="ghcr.io/cipher982/longhouse-runtime"
SUBDOMAIN="${SUBDOMAIN:-${LONGHOUSE_DEFAULT_SUBDOMAIN:-}}"
SHA="${1:-}"

if [[ -z "$SUBDOMAIN" ]]; then
  echo "Set SUBDOMAIN or LONGHOUSE_DEFAULT_SUBDOMAIN to the dogfood instance." >&2
  exit 1
fi
if [[ "$(printf '%s' "$SUBDOMAIN" | tr '[:upper:]' '[:lower:]')" == "demo" ]]; then
  echo "The public demo is deployed by Deploy and Verify, not promoted." >&2
  exit 1
fi

if [[ -z "$SHA" ]]; then
  SHA="$(gh run list --repo "$REPO" --workflow "Deploy and Verify" --branch main --status success \
    --limit 50 --json headSha,event --jq '[.[] | select(.event == "push")][0].headSha // ""')"
  if [[ -z "$SHA" ]]; then
    echo "No successful Deploy and Verify run on main to promote." >&2
    exit 1
  fi
else
  SHA="$(git -C "$ROOT" rev-parse --verify "${SHA}^{commit}" 2>/dev/null || printf '%s' "$SHA")"
  verified="$(gh run list --repo "$REPO" --workflow "Deploy and Verify" --commit "$SHA" --status success \
    --limit 5 --json headSha --jq 'length')"
  if [[ "$verified" == "0" ]]; then
    echo "Refusing $SHA: it has no successful Deploy and Verify run." >&2
    exit 1
  fi
fi

. "$ROOT/scripts/lib/hosted-instance.sh"

health_file="$(mktemp)"
trap 'rm -f "$health_file"' EXIT
if curl -fsS --max-time 10 -o "$health_file" "https://${SUBDOMAIN}.longhouse.ai/api/health"; then
  current="$(_lh_hosted_parse_health_commit "$health_file")"
  if [[ -n "$current" ]] && _lh_hosted_commit_matches_image_tag "$current" "$SHA"; then
    echo "$SUBDOMAIN already runs $SHA."
    exit 0
  fi
  echo "$SUBDOMAIN runs ${current:-unknown}; promoting $SHA."
fi

lh_hosted_prepare_control_plane_auth
lh_hosted_resolve_instance "$SUBDOMAIN"
lh_hosted_reprovision "$LH_INSTANCE_ID" "$IMAGE_REPO:$SHA"
echo "Promoted $SUBDOMAIN to $SHA."
