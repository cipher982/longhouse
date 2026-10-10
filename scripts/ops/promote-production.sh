#!/usr/bin/env bash
# Promote the exact image dogfood is serving to every production hosted tenant,
# the public demo and the new-tenant default image.
#
#   promote-production.sh [--check] [SHA]
#
# SHA is the exact 40-character commit; omit it to promote what dogfood serves
# right now. --check evaluates the gates and prints the receipt without moving
# anything. There is no tag or release step: the laptop release is independent.
#
# Contract (control-plane/docs/specs/release-rings.md, "Production"). Every gate
# is decided by scripts/ops/promotion_gates.py, all of them run, and the receipt
# (JSON, stdout) records the evidence each used:
#   dogfood        serves SHA and the control plane records it running the digest
#   hosted_qa      a completed Hosted Live QA run on SHA recorded verdict passed
#   engine_compat  the previous released engine shipped to SHA's server
#   soak           the control plane says the 24 h soak (on once any real tenant
#                  exists) has elapsed; an unreadable answer is a refusal
#   archive        Archive Runtime Image sealed the digest's OCI closure in the
#                  object store (runtime-oci-archive-<sha> receipt)
# The digest is the one dogfood's deployment recorded; nothing here accepts a
# caller-supplied image or soak. Rollout is one control-plane deployment, one
# tenant at a time, halting on the first failure; only then the demo is pinned.
#
# PROMOTION_ATTEMPT=N (default 1) picks a new idempotency key after a halted or
# rolled-back wave. Requires CONTROL_PLANE_ADMIN_TOKEN (or ADMIN_TOKEN):
#   python3 ~/git/me/scripts/infisical-get.py CONTROL_PLANE_ADMIN_TOKEN --project ops-infra --env prod
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO="${GITHUB_REPOSITORY:-cipher982/longhouse}"
PUBLISH_WORKFLOW="Publish Runtime Image"
DOGFOOD_SUBDOMAIN="${SUBDOMAIN:-${LONGHOUSE_DEFAULT_SUBDOMAIN:-david010}}"
DEMO_SSH_HOST="${DEMO_SSH_HOST:-zerg}"
DEMO_ENV_PATH="${DEMO_ENV_PATH:-/home/zerg/manual-apps/longhouse-demo/.env}"
DEMO_COMPOSE_SERVICE="${DEMO_COMPOSE_SERVICE:-longhouse-demo}"
DEMO_HEALTH_URL="${DEMO_HEALTH_URL:-https://longhouse.ai/api/health}"
DEMO_VERIFY_TIMEOUT="${DEMO_VERIFY_TIMEOUT:-300}"
DEMO_POLL_SECONDS="${DEMO_POLL_SECONDS:-5}"
ATTEMPT="${PROMOTION_ATTEMPT:-1}"
RECEIPT_DIR="${PROMOTION_RECEIPT_DIR:-/tmp/agents/promotion-receipts}"

CHECK_ONLY=0
SHA=""
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    -*) echo "Unknown option: $arg. Usage: promote-production.sh [--check] [SHA]" >&2; exit 2 ;;
    *)
      [[ -z "$SHA" ]] || { echo "Usage: promote-production.sh [--check] [SHA]" >&2; exit 2; }
      SHA="$arg"
      ;;
  esac
done
if [[ -n "$SHA" && ! "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "SHA must be an exact 40-character lowercase commit (omit it to promote what dogfood serves). Got: $SHA" >&2
  exit 2
fi
if [[ ! "$ATTEMPT" =~ ^[1-9][0-9]*$ ]]; then
  echo "PROMOTION_ATTEMPT must be a positive integer. Got: $ATTEMPT" >&2
  exit 2
fi

for tool in gh jq python3 curl ssh; do
  command -v "$tool" >/dev/null 2>&1 || { echo "promote-production needs '$tool' on PATH." >&2; exit 1; }
done

case "${DOGFOOD_SUBDOMAIN,,}" in
  demo|kernel-canary|canary)
    echo "Refusing: dogfood subdomain resolved to a non-dogfood surface: $DOGFOOD_SUBDOMAIN" >&2
    exit 1
    ;;
esac

receipt="$(mktemp)"
. "$ROOT/scripts/lib/ring-lock.sh"
trap 'rm -f "$receipt"; lh_ring_lock_release' EXIT

. "$ROOT/scripts/lib/hosted-instance.sh"
lh_hosted_prepare_control_plane_auth

# One writer for production (tenants, the new-tenant pointer and the demo pin), held
# from before the gates read what dogfood serves until the script exits. --check moves
# nothing and takes no lock. TTL 45 min: measured promotions take 1-2 min (pointer-only
# waves finish in the control plane in 0-1 s; the demo pin and verify is the long part),
# and the script's own worst case is the 1800 s wave wait plus the 300 s demo verify
# plus a few minutes of gates; the keepalive below renews it every minute, so the TTL
# only bounds a holder that stopped renewing. A holder that dies frees it at once.
if [[ "$CHECK_ONLY" != "1" ]]; then
  lh_ring_lock_acquire production "${SHA:-dogfood-served}" 2700 "promote-production ${SHA:-<dogfood-served>}" || {
    echo "Refusing: could not take the production lock (above). Nothing was changed." >&2
    exit 1
  }
  lh_ring_lock_keepalive 60 2700  # renewed while this script lives
fi

# --- The gates. Every one runs; the receipt is printed whether or not they pass.
gate_args=(--dogfood-subdomain "$DOGFOOD_SUBDOMAIN" --repo "$REPO")
[[ -z "$SHA" ]] || gate_args+=(--sha "$SHA")
[[ -z "${DOGFOOD_HEALTH_URL:-}" ]] || gate_args+=(--dogfood-health-url "$DOGFOOD_HEALTH_URL")
gates_ok=1
python3 "$ROOT/scripts/ops/promotion_gates.py" "${gate_args[@]}" >"$receipt" || gates_ok=0
save_receipt() {
  local name
  name="$(jq -r '.sha // empty' "$receipt" 2>/dev/null || true)"
  name="${name:-unresolved}"
  mkdir -p "$RECEIPT_DIR" && cp "$receipt" "$RECEIPT_DIR/production-${name}-$(date -u +%Y%m%dT%H%M%SZ).json" || true
}
# A stop after the control plane took the submission keeps the receipt too, with the deployment and
# key it used, so the recovery commands the stop prints can be run from it later.
save_stopped_receipt() {
  jq --arg outcome "$1" --arg deployment "${LH_DEPLOYMENT_ID:-}" --arg key "${LH_DEPLOYMENT_IDEMPOTENCY_KEY:-}" --arg attempt "$ATTEMPT" \
    '. + {promotion: {outcome: $outcome, deployment_id: $deployment, idempotency_key: $key, attempt: ($attempt | tonumber), demo_verified: false}}' \
    "$receipt" >"$receipt.next" && mv "$receipt.next" "$receipt" || true
  save_receipt
}
if [[ "$gates_ok" != "1" ]]; then
  echo "Refusing to promote to production: the gates above did not all pass. Nothing was changed." >&2
  cat "$receipt"
  save_receipt
  exit 1
fi

SHA="$(jq -r '.sha' "$receipt")"
[[ "$CHECK_ONLY" == "1" ]] || lh_ring_lock_renew 2700 "$SHA" >/dev/null || {
  echo "Refusing: this run lost the production lock (above). Nothing was changed." >&2
  exit 1
}
PROD_IMAGE="$(jq -r '.image_digest' "$receipt")"
TARGET_IDS_JSON="$(jq -c '[.plan.targets[].id]' "$receipt")"
TARGET_COUNT="$(jq -r '.plan.targets | length' "$receipt")"
DOGFOOD_DEPLOYMENT="$(jq -r '.gates.dogfood.evidence.deployment_id' "$receipt")"
echo "Gates passed for $SHA ($PROD_IMAGE)." >&2

# --- Review: every code commit between what production serves and SHA needs a completed
# review receipt (scripts/ops/review_gate.py). It sits outside the receipt's five gates
# because it reads this machine's review store, not the control plane or GitHub.
. "$ROOT/scripts/lib/review-gate.sh"
if ! lh_review_gate_promotion "$SHA" "$DEMO_HEALTH_URL" >&2; then
  echo "Refusing to promote to production: the range holds commits without a completed review. Nothing was changed." >&2
  save_receipt
  exit 1
fi
if [[ "$TARGET_COUNT" == "0" ]]; then
  echo "Targets: none (pointer-only promotion)." >&2
else
  echo "Targets: $TARGET_COUNT active hosted instance(s): $TARGET_IDS_JSON" >&2
fi

# --- Publish-workflow provenance for the image (the fields lh_hosted_reprovision
# requires; schema and build identity are re-derived from the image itself). It
# is resolved before --check exits: a promotion that would refuse here is not
# promotable.
publish_run_json="$(gh run list --repo "$REPO" --workflow "$PUBLISH_WORKFLOW" --commit "$SHA" --status success --limit 5 \
  --json databaseId,number,attempt,headSha,workflowName,conclusion)"
publish_run_id="$(jq -r --arg sha "$SHA" '[.[] | select(.headSha == $sha)][0].databaseId // empty' <<<"$publish_run_json")"
publish_run_number="$(jq -r --arg sha "$SHA" '[.[] | select(.headSha == $sha)][0].number // empty' <<<"$publish_run_json")"
publish_run_attempt="$(jq -r --arg sha "$SHA" '[.[] | select(.headSha == $sha)][0].attempt // empty' <<<"$publish_run_json")"
if [[ -z "$publish_run_id" || -z "$publish_run_number" || -z "$publish_run_attempt" ]]; then
  echo "Refusing $SHA: no successful $PUBLISH_WORKFLOW run found to source deployment provenance. Nothing was changed." >&2
  save_receipt
  exit 1
fi
jq --argjson id "$publish_run_id" --argjson number "$publish_run_number" --argjson attempt "$publish_run_attempt" \
  '. + {publish_run: {id: $id, number: $number, attempt: $attempt}}' "$receipt" >"$receipt.next" && mv "$receipt.next" "$receipt"

if [[ "$CHECK_ONLY" == "1" ]]; then
  echo "--check: promotable; nothing was changed." >&2
  cat "$receipt"
  exit 0
fi

export LH_DEPLOYMENT_SOURCE_WORKFLOW="$PUBLISH_WORKFLOW"
export LH_DEPLOYMENT_SOURCE_ORDER="$publish_run_number"
export LH_DEPLOYMENT_QUALIFICATION_ID="runtime-image-${publish_run_id}-${publish_run_attempt}"
# The reason and key are functions of the commit alone, so a rerun after a
# partial failure is an idempotent replay of the same submission.
export LH_DEPLOYMENT_REASON="production promotion of dogfood-run source ${SHA} (dogfood deployment ${DOGFOOD_DEPLOYMENT})"
KEY_SUFFIX=""
[[ "$ATTEMPT" == "1" ]] || KEY_SUFFIX="-attempt-${ATTEMPT}"
export LH_DEPLOYMENT_IDEMPOTENCY_KEY="promote-production-${SHA}${KEY_SUFFIX}"

# --- Tenants and the new-tenant default: one deployment, one tenant at a time,
# halting on the first failure. The default moves only if every tenant succeeded.
LH_DEPLOYMENT_ID=""
if ! lh_hosted_reprovision_production "$PROD_IMAGE" "$TARGET_IDS_JSON"; then
  if [[ -z "${LH_DEPLOYMENT_ID:-}" ]]; then
    cat >&2 <<EOF

The control plane did not confirm a deployment (see its answer above), so the run stopped before the demo.
The submission is idempotent: rerunning replays the same key. If unsure whether it was recorded, read
  GET ${CONTROL_PLANE_URL%/}/api/deployments?submission_key=$LH_DEPLOYMENT_IDEMPOTENCY_KEY
EOF
    save_receipt
    exit 1
  fi
  cat >&2 <<EOF

Production rollout stopped before the public demo was touched (deployment ${LH_DEPLOYMENT_ID}).
The wave halts on the first failed tenant. The new-tenant default moves only when every tenant
succeeded, so it is unchanged; tenants ahead of the failure are already on $PROD_IMAGE.
  inspect:   GET ${CONTROL_PLANE_URL%/}/api/deployments/${LH_DEPLOYMENT_ID}  (failed_instances)
  roll back: POST ${CONTROL_PLANE_URL%/}/api/deployments/${LH_DEPLOYMENT_ID}/rollback  {"scope":"all"}
  roll forward after fixing the cause: PROMOTION_ATTEMPT=$((ATTEMPT + 1)) make promote-production SHA=$SHA
EOF
  save_stopped_receipt wave-halted
  exit 1
fi
DEPLOYMENT_ID="${LH_DEPLOYMENT_ID:-}"

# --- Pin the public demo to the same digest and verify it. The tenants and the
# default are already promoted here, so a failure leaves only the demo behind.
demo_recovery() {
  cat >&2 <<EOF

The tenants and the new-tenant default are already on $PROD_IMAGE (deployment $DEPLOYMENT_ID); only the
public demo is not verified at $SHA. Nothing to roll back. Fix the demo host ($DEMO_SSH_HOST) and rerun
  make promote-production SHA=$SHA
The rerun replays the finished deployment (same key) and repins the demo idempotently. If the control
plane answers 409 because the tenant set changed meanwhile, rerun with PROMOTION_ATTEMPT=$((ATTEMPT + 1)).
EOF
}
echo "Pinning public demo ($DEMO_SSH_HOST:$DEMO_ENV_PATH) to $PROD_IMAGE..." >&2
demo_dir="$(dirname "$DEMO_ENV_PATH")"
if ! ssh "$DEMO_SSH_HOST" "set -euo pipefail
if grep -q '^LONGHOUSE_DEMO_IMAGE=' '$DEMO_ENV_PATH' 2>/dev/null; then
  sed -i \"s|^LONGHOUSE_DEMO_IMAGE=.*|LONGHOUSE_DEMO_IMAGE=$PROD_IMAGE|\" '$DEMO_ENV_PATH'
else
  printf 'LONGHOUSE_DEMO_IMAGE=%s\n' '$PROD_IMAGE' >> '$DEMO_ENV_PATH'
fi
cd '$demo_dir'
docker compose up -d --force-recreate '$DEMO_COMPOSE_SERVICE'"; then
  echo "Could not pin the public demo over ssh." >&2
  demo_recovery
  save_stopped_receipt demo-not-pinned
  exit 1
fi

echo "Verifying public demo reports $SHA..." >&2
deadline=$(( $(date +%s) + DEMO_VERIFY_TIMEOUT ))
demo_commit=""
# Poll at least once: the deadline has whole-second resolution, so a short
# timeout started late in a second used to expire before the first look.
first_poll=1
while [[ "$first_poll" == 1 || "$(date +%s)" -lt "$deadline" ]]; do
  first_poll=0
  demo_body="$(curl -sfL --max-time 10 "$DEMO_HEALTH_URL" 2>/dev/null || echo '{}')"
  demo_commit="$(jq -r '.build.commit // empty' <<<"$demo_body" 2>/dev/null || true)"
  if [[ "$demo_commit" == "$SHA" ]]; then
    break
  fi
  sleep "$DEMO_POLL_SECONDS"
done
if [[ "$demo_commit" != "$SHA" ]]; then
  echo "Timed out waiting for public demo to report commit $SHA (last seen: ${demo_commit:-<unreachable>})." >&2
  demo_recovery
  save_stopped_receipt demo-not-verified
  exit 1
fi

# --- The receipt of what was checked and what moved.
final="$(jq \
  --arg deployment "$DEPLOYMENT_ID" --arg key "$LH_DEPLOYMENT_IDEMPOTENCY_KEY" --arg demo_host "$DEMO_SSH_HOST" \
  --arg attempt "$ATTEMPT" --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --argjson targets "$TARGET_COUNT" \
  '. + {promotion: {deployment_id: $deployment, idempotency_key: $key, attempt: ($attempt | tonumber), targets: $targets,
        demo_host: $demo_host, demo_verified: true, promoted_at: $at}}' "$receipt")"
printf '%s\n' "$final" >"$receipt"
save_receipt
echo "Promoted production: sha=$SHA digest=$PROD_IMAGE targets=$TARGET_COUNT demo_verified=true" >&2
printf '%s\n' "$final"
