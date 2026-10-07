#!/usr/bin/env bash
# Promote one explicitly selected release to personal dogfood.
#
#   promote-dogfood.sh SHA               canary-qualified: a successful Deploy and Verify canary
#                                        receipt for SHA, and every code commit since what dogfood
#                                        serves reviewed (review-gate.sh)
#   promote-dogfood.sh --fast-lane SHA   published: the Publish Runtime Image receipt for SHA is
#                                        enough, because nothing between what dogfood serves and SHA
#                                        touches the review blocking list (review-policy.toml). CI,
#                                        canary, Hosted Live QA and review run in parallel; production
#                                        still waits for all of them. Exit 3 when the range touches the
#                                        blocking list (it needs the canary-qualified path), 0 when
#                                        dogfood already serves SHA. Never moves dogfood backwards.
#
# The Promote Rings workflow (.github/workflows/promote-rings.yml) runs both; see
# control-plane/docs/specs/release-rings.md "Continuous promotion".
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO="${GITHUB_REPOSITORY:-cipher982/longhouse}"
IMAGE_REPO="ghcr.io/cipher982/longhouse-runtime"
PUBLISH_WORKFLOW="Publish Runtime Image"
DEPLOY_WORKFLOW="Deploy and Verify"
FAST_LANE=0
if [[ "${1:-}" == "--fast-lane" ]]; then
  FAST_LANE=1
  shift
fi
SHA="${1:-}"
receipt_zip=""
receipt_path=""
. "$ROOT/scripts/lib/ring-lock.sh"
cleanup() {
  [[ -z "$receipt_zip" ]] || rm -f "$receipt_zip"
  [[ -z "$receipt_path" ]] || rm -f "$receipt_path"
  lh_ring_lock_release
}
trap cleanup EXIT

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

# One writer per ring: held from before the gates read what dogfood serves until the
# script exits, renewed every minute while it lives. TTL 20 min: a promotion takes
# 1-2 min end to end (31 dogfood deployments 2026-10-03..07 ran 28-55 s in the
# control plane) and the script's own worst case is the 900 s deployment wait plus
# the 180 s identity check plus a couple of minutes of gates; with the keepalive the
# TTL only bounds a holder that stopped renewing. A holder that dies frees it at once
# (ring_lock.py checks its pid).
lane="canary-qualified"
[[ "$FAST_LANE" != "1" ]] || lane="fast-lane"
lh_ring_lock_acquire "dogfood-${SUBDOMAIN,,}" "$SHA" 1200 "promote-dogfood ($lane) $SHA" || {
  echo "Refusing: could not take the $SUBDOMAIN promotion lock (above). Nothing was changed." >&2
  exit 1
}
lh_ring_lock_keepalive 60 1200

HEALTH_URL="https://${SUBDOMAIN}.longhouse.ai/api/health"
receipt_zip="$(mktemp)"
receipt_path="$(mktemp)"

# Download one artifact of a run and extract FILE from it into $receipt_path.
fetch_artifact_file() {
  local artifact_id="$1" file="$2" gh_token
  gh_token="${GH_TOKEN:-$(gh auth token)}"
  curl --fail-with-body --silent --show-error --location \
    --header "Authorization: Bearer $gh_token" \
    --header "Accept: application/vnd.github+json" \
    --header "X-GitHub-Api-Version: 2022-11-28" \
    --output "$receipt_zip" \
    "https://api.github.com/repos/$REPO/actions/artifacts/$artifact_id/zip"
  unzip -p "$receipt_zip" "$file" > "$receipt_path"
}

if [[ "$FAST_LANE" == "1" ]]; then
  served="$(curl -sfL --max-time 15 "$HEALTH_URL" | jq -r '.build.commit // empty' 2>/dev/null || true)"
  if [[ ! "$served" =~ ^[0-9a-f]{40}$ ]]; then
    echo "Refusing the fast lane: cannot read what $SUBDOMAIN serves from $HEALTH_URL." >&2
    exit 1
  fi
  if [[ "$served" == "$SHA" ]]; then
    echo "$SUBDOMAIN already serves $SHA; nothing to do."
    exit 0
  fi
  git -C "$ROOT" cat-file -e "${served}^{commit}" 2>/dev/null || git -C "$ROOT" fetch --quiet origin >/dev/null 2>&1 || true
  if ! git -C "$ROOT" merge-base --is-ancestor "$served" "$SHA" 2>/dev/null; then
    echo "Refusing the fast lane: $SHA does not contain what $SUBDOMAIN serves ($served); it never moves dogfood backwards or sideways." >&2
    exit 1
  fi
  # The guard: david010 holds irreplaceable history, so a range that touches the review blocking
  # list keeps the canary-qualified, reviewed path. Exit 1 lists commits; 2 is "could not decide".
  blocking_rc=0
  python3 "$ROOT/scripts/ops/review_gate.py" --repo "$ROOT" blocking --base "$served" --head "$SHA" >&2 || blocking_rc=$?
  if [[ "$blocking_rc" == "1" ]]; then
    echo "Not fast-lane: ${served:0:12}..${SHA:0:12} touches the review blocking list (above); it needs the canary-qualified path (promote-dogfood.sh $SHA)." >&2
    exit 3
  elif [[ "$blocking_rc" != "0" ]]; then
    echo "Refusing the fast lane: the blocking-list check could not decide (above)." >&2
    exit 1
  fi

  publish_json="$(gh run list --repo "$REPO" --workflow "$PUBLISH_WORKFLOW" --commit "$SHA" --status success --limit 20 --json headSha,databaseId,attempt,event)"
  publish_pick="$(jq -c --arg sha "$SHA" \
    '[.[] | select(.headSha == $sha and (.event == "push" or .event == "workflow_dispatch"))] | sort_by(.databaseId) | last // empty' \
    <<<"$publish_json")"
  publish_run_id="$(jq -r '.databaseId // empty' <<<"$publish_pick")"
  publish_attempt="$(jq -r '.attempt // empty' <<<"$publish_pick")"
  if [[ ! "$publish_run_id" =~ ^[1-9][0-9]*$ || ! "$publish_attempt" =~ ^[1-9][0-9]*$ ]]; then
    echo "Refusing $SHA: no successful $PUBLISH_WORKFLOW run for it (the image is not published yet)." >&2
    exit 1
  fi
  artifact_json="$(gh api "repos/$REPO/actions/runs/$publish_run_id/artifacts?per_page=100")"
  artifact_id="$(jq -r --arg name "runtime-publication-${publish_run_id}-${publish_attempt}" \
    '[.artifacts[] | select(.expired == false and .name == $name)] | first | .id // empty' <<<"$artifact_json")"
  if [[ ! "$artifact_id" =~ ^[1-9][0-9]*$ ]]; then
    echo "Refusing $SHA: $PUBLISH_WORKFLOW run $publish_run_id has no publication receipt." >&2
    exit 1
  fi
  fetch_artifact_file "$artifact_id" runtime-publication.json
  verification_json="$(
    python3 "$ROOT/scripts/ops/release-artifacts.py" verify-publication \
      --receipt "$receipt_path" \
      --source-sha "$SHA"
  )"
  qualification_note="published by $PUBLISH_WORKFLOW run $publish_run_id; no blocking-list change since ${served}"
else
  # Every code commit between what dogfood serves now and SHA needs a completed review.
  . "$ROOT/scripts/lib/review-gate.sh"
  lh_review_gate_promotion "$SHA" "$HEALTH_URL" >&2

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

    # `gh run rerun --failed` reruns only the failed jobs, so the canary receipt of a run that
    # succeeded on attempt N was often uploaded by an earlier attempt (the canary jobs passed
    # there; a later job such as the demo deploy failed and was rerun). The run's final
    # conclusion is what counts; the receipt may come from any attempt up to it, newest first.
    # Whichever attempt it comes from, the receipt must match the exact SHA (verify-publication
    # below) and the publishing run it names must be the same successful publication (gh run view).
    artifact_json="$(gh api "repos/$REPO/actions/runs/$deploy_run_id/artifacts?per_page=100")"
    artifact_id="$(jq -r --arg prefix "runtime-verification-${deploy_run_id}-" --argjson final "$deploy_attempt" \
      '[.artifacts[] | select(.expired == false and (.name | startswith($prefix)))
        | {id, attempt: (.name[($prefix | length):] | if test("^[1-9][0-9]*$") then tonumber else empty end)}
        | select(.attempt <= $final)]
       | sort_by(.attempt) | last | .id // empty' \
      <<<"$artifact_json")"
    # A successful path-filtered run may not have deployed anything.
    # Only a canary receipt of this run can authorize promotion.
    if [[ "$artifact_id" =~ ^[1-9][0-9]*$ ]]; then
      break
    fi
  done <<<"$deploy_run_ids"
  if [[ ! "$artifact_id" =~ ^[1-9][0-9]*$ ]]; then
    echo "Refusing $SHA: no successful $DEPLOY_WORKFLOW run has a usable canary verification receipt (any attempt)." >&2
    exit 1
  fi
  fetch_artifact_file "$artifact_id" runtime-verification.json
  verification_json="$(
    python3 "$ROOT/scripts/ops/release-artifacts.py" verify-publication \
      --receipt "$receipt_path" \
      --source-sha "$SHA" \
      --require-verification
  )"
  canary_deployment_id="$(jq -er '.canary_deployment_id' <<<"$verification_json")"
  qualification_note="canary $canary_deployment_id"
fi
digest="$(jq -er '.image_digest' <<<"$verification_json")"
publish_run_id="$(jq -er '.build_run_id' <<<"$verification_json")"
publish_attempt="$(jq -er '.build_attempt' <<<"$verification_json")"
run_number="$(jq -er '.source_order' <<<"$verification_json")"

publish_view="$(gh run view "$publish_run_id" --repo "$REPO" --json headSha,number,attempt,status,conclusion,workflowName)"
if [[ "$(jq -r '.headSha // empty' <<<"$publish_view")" != "$SHA" ||
      "$(jq -r '.workflowName // empty' <<<"$publish_view")" != "$PUBLISH_WORKFLOW" ||
      "$(jq -r '.status // empty' <<<"$publish_view")" != "completed" ||
      "$(jq -r '.conclusion // empty' <<<"$publish_view")" != "success" ||
      "$(jq -r '.number // empty' <<<"$publish_view")" != "$run_number" ||
      "$(jq -r '.attempt // empty' <<<"$publish_view")" != "$publish_attempt" ]]; then
  echo "The receipt links to a publishing run that is not the same successful publication." >&2
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

# Submission is durable and observed by ID; lh_hosted_reprovision waits for the
# instance to report this exact build before it returns.
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
export LH_DEPLOYMENT_REASON="${lane} dogfood promotion of source ${SHA} (${qualification_note})"
lh_hosted_reprovision "$LH_INSTANCE_ID" "$IMAGE_REPO@$digest"
echo "Promoted $SUBDOMAIN ($lane) to exact digest $digest (source $SHA, workflow run $run_number, $qualification_note)."
