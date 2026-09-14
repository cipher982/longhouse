#!/usr/bin/env bash
# Promote a canary-verified runtime image to the owner's dogfood instance.
#
# Usage: scripts/ops/promote-dogfood.sh [SHA]
#
# A commit is promotable when its push-triggered "Deploy and Verify" run
# succeeded and ghcr.io/cipher982/longhouse-runtime:<sha> exists. A push that
# changed no runtime files deploys the previous image as :latest and publishes
# no image of its own, so it is not promotable. SHA defaults to the newest
# promotable main commit among the latest 100 successful runs. The dogfood
# instance is updated on purpose -- never by an ordinary push, which only
# reaches demo and canary.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO="${GITHUB_REPOSITORY:-cipher982/longhouse}"
IMAGE_REPO="ghcr.io/cipher982/longhouse-runtime"
WORKFLOW="Deploy and Verify"
SUBDOMAIN="${SUBDOMAIN:-${LONGHOUSE_DEFAULT_SUBDOMAIN:-}}"
SHA="${1:-}"

for tool in gh docker curl; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "promote-dogfood needs '$tool' on PATH." >&2
    exit 1
  fi
done
if [[ -z "$SUBDOMAIN" ]]; then
  echo "Set SUBDOMAIN or LONGHOUSE_DEFAULT_SUBDOMAIN to the dogfood instance." >&2
  exit 1
fi
if [[ "$(printf '%s' "$SUBDOMAIN" | tr '[:upper:]' '[:lower:]')" == "demo" ]]; then
  echo "The public demo is deployed by Deploy and Verify, not promoted." >&2
  exit 1
fi

image_exists() {
  docker manifest inspect "$IMAGE_REPO:$1" >/dev/null 2>&1
}

if [[ -z "$SHA" ]]; then
  if ! candidates="$(gh run list --repo "$REPO" --workflow "$WORKFLOW" --branch main --event push \
    --status success --limit 100 --json headSha --jq '.[].headSha')"; then
    echo "Could not list $WORKFLOW runs (is gh authenticated?)." >&2
    exit 1
  fi
  for candidate in $candidates; do
    if image_exists "$candidate"; then
      SHA="$candidate"
      break
    fi
  done
  if [[ -z "$SHA" ]]; then
    echo "No promotable main commit: none has both a successful push Deploy and Verify and its own image." >&2
    exit 1
  fi
else
  if ! SHA="$(git -C "$ROOT" rev-parse --verify --quiet "${SHA}^{commit}")" || [[ ! "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "Refusing ${1}: not a commit in this checkout (fetch first)." >&2
    exit 1
  fi
  if ! verified="$(gh run list --repo "$REPO" --workflow "$WORKFLOW" --commit "$SHA" --event push \
    --status success --limit 1 --json headSha --jq '.[].headSha')"; then
    echo "Could not list $WORKFLOW runs for $SHA (is gh authenticated?)." >&2
    exit 1
  fi
  if ! grep -Fqx -- "$SHA" <<<"$verified"; then
    echo "Refusing $SHA: no successful push-triggered $WORKFLOW run." >&2
    exit 1
  fi
  if ! image_exists "$SHA"; then
    echo "Refusing $SHA: $IMAGE_REPO:$SHA does not exist (that push changed no runtime files)." >&2
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
