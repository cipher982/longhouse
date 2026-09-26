#!/usr/bin/env bash
# Promote one explicitly selected, canary-qualified release to personal dogfood.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO="${GITHUB_REPOSITORY:-cipher982/longhouse}"
IMAGE_REPO="ghcr.io/cipher982/longhouse-runtime"
PUBLISH_WORKFLOW="Publish Runtime Image"
DEPLOY_WORKFLOW="Deploy and Verify"
SHA="${1:-}"
receipt_zip=""
receipt_path=""
trap '[[ -z "$receipt_zip" ]] || rm -f "$receipt_zip"; [[ -z "$receipt_path" ]] || rm -f "$receipt_path"' EXIT

for tool in gh jq python3 curl unzip; do
  command -v "$tool" >/dev/null 2>&1 || { echo "promote-dogfood needs '$tool' on PATH." >&2; exit 1; }
done
if [[ -z "$SHA" ]]; then
  echo "An exact commit SHA is required; automatic personal dogfood promotion is prohibited." >&2
  exit 1
fi
if [[ -z "$SUBDOMAIN" ]]; then
  echo "Set SUBDOMAIN or LONGHOUSE_DEFAULT_SUBDOMAIN to the dogfood instance." >&2
  exit 1
fi
case "${SUBDOMAIN,,}" in
  demo|kernel-canary|canary|personal)
    echo "Refusing automated promotion of public demo or canary target: $SUBDOMAIN" >&2
    exit 1
    ;;
esac
SHA="$(git -C "$ROOT" rev-parse --verify --quiet "${SHA}^{commit}")" || {
  echo "Refusing ${1}: not a commit in this checkout (fetch first)." >&2
  exit 1
}

deploy_json="$(gh run list --repo "$REPO" --workflow "$DEPLOY_WORKFLOW" --commit "$SHA" --status success --limit 20 --json headSha,databaseId,workflowName,event)"
deploy_run_ids="$(jq -r --arg sha "$SHA" \
  '.[] | select(.headSha == $sha and (.event == "push" or .event == "workflow_dispatch")) | .databaseId' \
  <<<"$deploy_json")"
artifact_id=""
while IFS= read -r deploy_run_id; do
  [[ -n "$deploy_run_id" ]] || continue
  if [[ ! "$deploy_run_id" =~ ^[1-9][0-9]*$ ]]; then
    echo "Successful $DEPLOY_WORKFLOW run has no immutable run id." >&2
    exit 1
  fi
  deploy_view="$(gh run view "$deploy_run_id" --repo "$REPO" --json headSha,attempt,status,conclusion,workflowName)"
  deploy_attempt="$(jq -r '.attempt // empty' <<<"$deploy_view")"
  if [[ "$(jq -r '.headSha // empty' <<<"$deploy_view")" != "$SHA" ||
        "$(jq -r '.workflowName // empty' <<<"$deploy_view")" != "$DEPLOY_WORKFLOW" ||
        "$(jq -r '.status // empty' <<<"$deploy_view")" != "completed" ||
        "$(jq -r '.conclusion // empty' <<<"$deploy_view")" != "success" ]] ||
     ! [[ "$deploy_attempt" =~ ^[1-9][0-9]*$ ]]; then
    echo "Selected $DEPLOY_WORKFLOW run is no longer a successful exact-SHA run." >&2
    exit 1
  fi

  artifact_json="$(gh api "repos/$REPO/actions/runs/$deploy_run_id/artifacts?per_page=100")"
  artifact_id="$(jq -r --arg run_id "$deploy_run_id" --arg attempt "$deploy_attempt" \
    '[.artifacts[] | select(.expired == false and .name == ("runtime-verification-" + $run_id + "-" + $attempt))] | if length == 1 then .[0].id else empty end' \
    <<<"$artifact_json")"
  # A successful path-filtered run may not have deployed anything.
  # Only an exact-attempt canary receipt can authorize promotion.
  if [[ "$artifact_id" =~ ^[1-9][0-9]*$ ]]; then
    break
  fi
done <<<"$deploy_run_ids"
if [[ ! "$artifact_id" =~ ^[1-9][0-9]*$ ]]; then
  echo "Refusing $SHA: no successful $DEPLOY_WORKFLOW run has a usable canary verification receipt." >&2
  exit 1
fi
receipt_zip="$(mktemp)"
receipt_path="$(mktemp)"
gh_token="${GH_TOKEN:-$(gh auth token)}"
curl --fail-with-body --silent --show-error --location \
  --header "Authorization: Bearer $gh_token" \
  --header "Accept: application/vnd.github+json" \
  --header "X-GitHub-Api-Version: 2022-11-28" \
  --output "$receipt_zip" \
  "https://api.github.com/repos/$REPO/actions/artifacts/$artifact_id/zip"
unzip -p "$receipt_zip" runtime-verification.json > "$receipt_path"
verification_json="$(
  python3 "$ROOT/scripts/ops/release-artifacts.py" verify-publication \
    --receipt "$receipt_path" \
    --source-sha "$SHA" \
    --require-verification
)"
digest="$(jq -er '.image_digest' <<<"$verification_json")"
publish_run_id="$(jq -er '.build_run_id' <<<"$verification_json")"
publish_attempt="$(jq -er '.build_attempt' <<<"$verification_json")"
run_number="$(jq -er '.source_order' <<<"$verification_json")"
canary_deployment_id="$(jq -er '.canary_deployment_id' <<<"$verification_json")"

publish_view="$(gh run view "$publish_run_id" --repo "$REPO" --json headSha,number,attempt,status,conclusion,workflowName)"
if [[ "$(jq -r '.headSha // empty' <<<"$publish_view")" != "$SHA" ||
      "$(jq -r '.workflowName // empty' <<<"$publish_view")" != "$PUBLISH_WORKFLOW" ||
      "$(jq -r '.status // empty' <<<"$publish_view")" != "completed" ||
      "$(jq -r '.conclusion // empty' <<<"$publish_view")" != "success" ||
      "$(jq -r '.number // empty' <<<"$publish_view")" != "$run_number" ||
      "$(jq -r '.attempt // empty' <<<"$publish_view")" != "$publish_attempt" ]]; then
  echo "Canary verification receipt links to a publishing run that is not the same successful publication." >&2
  exit 1
fi

schema_metadata="$(python3 "$ROOT/scripts/ops/release-artifacts.py" inspect --image "$IMAGE_REPO@$digest")" || {
  echo "Refusing $SHA: selected image has no exact catalog schema metadata." >&2
  exit 1
}
schema_version="$(jq -r '.schema_version // empty' <<<"$schema_metadata")"
schema_min_reader="$(jq -r '.schema_min_reader // empty' <<<"$schema_metadata")"
selected_source_sha="$(jq -r '.source_sha // empty' <<<"$schema_metadata")"
if [[ "$selected_source_sha" != "$SHA" ]]; then
  echo "Refusing $SHA: selected digest source revision is ${selected_source_sha:-missing}; metadata does not match the requested commit." >&2
  exit 1
fi
schema_max_reader="$(jq -r '.schema_max_reader // empty' <<<"$schema_metadata")"
for value in "$schema_version" "$schema_min_reader" "$schema_max_reader"; do
  [[ "$value" =~ ^[0-9]+$ ]] || {
    echo "Refusing $SHA: selected image schema metadata is malformed." >&2
    exit 1
  }
done

# The canary deployment already performed the exact runtime readiness and
# functional acceptance. Submission remains durable and is observed by ID.
. "$ROOT/scripts/lib/hosted-instance.sh"
lh_hosted_prepare_control_plane_auth
lh_hosted_resolve_instance "$SUBDOMAIN"
export LH_DEPLOYMENT_SOURCE_SHA="$SHA"
export LH_DEPLOYMENT_SOURCE_WORKFLOW="$PUBLISH_WORKFLOW"
export LH_DEPLOYMENT_SOURCE_ORDER="$run_number"
export LH_DEPLOYMENT_QUALIFICATION_ID="runtime-image-${publish_run_id}-${publish_attempt}"
export LH_DEPLOYMENT_SCHEMA_VERSION="$schema_version"
export LH_DEPLOYMENT_SCHEMA_MIN_READER="$schema_min_reader"
export LH_DEPLOYMENT_SCHEMA_MAX_READER="$schema_max_reader"
export LH_DEPLOYMENT_IDEMPOTENCY_KEY="promote-dogfood-${SUBDOMAIN}-${SHA}"
export LH_DEPLOYMENT_REASON="manual dogfood promotion of canary-qualified source ${SHA} (${canary_deployment_id})"
# Pre-launch the owner's instance is production: the image it is promoted to is
# the image a new tenant gets. Without this the new-tenant default stayed on
# whatever the first tenant captured, and every Starter ran stale code.
export LH_DEPLOYMENT_PRODUCTION_PROMOTION=1
lh_hosted_reprovision "$LH_INSTANCE_ID" "$IMAGE_REPO@$digest"
echo "Promoted $SUBDOMAIN to exact digest $digest (source $SHA, workflow run $run_number, canary $canary_deployment_id)."
