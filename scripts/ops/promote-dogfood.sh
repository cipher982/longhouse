#!/usr/bin/env bash
# Promote one explicitly selected, canary-qualified release to personal dogfood.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO="${GITHUB_REPOSITORY:-cipher982/longhouse}"
IMAGE_REPO="ghcr.io/cipher982/longhouse-runtime"
PUBLISH_WORKFLOW="Publish Runtime Image"
DEPLOY_WORKFLOW="Deploy and Verify"
SHA="${1:-}"

for tool in gh docker jq python3; do
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

deploy_json="$(gh run list --repo "$REPO" --workflow "$DEPLOY_WORKFLOW" --commit "$SHA" --event push --status success --limit 20 --json headSha,databaseId,number,workflowName)"
deploy_record="$(jq -c --arg sha "$SHA" 'map(select(.headSha == $sha)) | .[0] // empty' <<<"$deploy_json")"
if [[ -z "$deploy_record" ]]; then
  echo "Refusing $SHA: no successful push-triggered $DEPLOY_WORKFLOW run." >&2
  exit 1
fi
publish_json="$(gh run list --repo "$REPO" --workflow "$PUBLISH_WORKFLOW" --commit "$SHA" --event push --status success --limit 20 --json headSha,databaseId,number,workflowName)"
publish_record="$(jq -c --arg sha "$SHA" 'map(select(.headSha == $sha)) | .[0] // empty' <<<"$publish_json")"
if [[ -z "$publish_record" ]]; then
  echo "Refusing $SHA: no successful push-triggered $PUBLISH_WORKFLOW run." >&2
  exit 1
fi
run_number="$(jq -r '.number // empty' <<<"$publish_record")"
publish_run_id="$(jq -r '.databaseId // empty' <<<"$publish_record")"
if [[ -z "$run_number" || "$run_number" == "null" || -z "$publish_run_id" || "$publish_run_id" == "null" ]]; then
  echo "Successful $PUBLISH_WORKFLOW run has no immutable workflow order or run id." >&2
  exit 1
fi
publish_attempt="$(gh run view "$publish_run_id" --repo "$REPO" --json attempt --jq '.attempt // empty')"
if [[ ! "$publish_attempt" =~ ^[1-9][0-9]*$ ]]; then
  echo "Successful $PUBLISH_WORKFLOW run has no immutable run attempt." >&2
  exit 1
fi
digest="$(docker buildx imagetools inspect "$IMAGE_REPO:$SHA" --format '{{.Manifest.Digest}}')"
if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "Refusing $SHA: image manifest has no immutable digest." >&2
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
export LH_DEPLOYMENT_REASON="manual dogfood promotion of exact source ${SHA}"
lh_hosted_reprovision "$LH_INSTANCE_ID" "$IMAGE_REPO@$digest"
echo "Promoted $SUBDOMAIN to exact digest $digest (source $SHA, workflow run $run_number)."
