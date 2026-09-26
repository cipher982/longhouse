#!/usr/bin/env bash
# Promote a dogfood-soaked release to every production hosted tenant, the
# public demo, and the new-tenant default image pointer.
#
# See control-plane/docs/specs/release-rings.md change 2. Production only
# ever runs an image dogfood has already run for at least SOAK_HOURS: this
# script never inspects or trusts a caller-supplied digest, it re-derives one
# from the exact successful `promote-dogfood` deployment for VERSION's commit.
#
# Requires CONTROL_PLANE_ADMIN_TOKEN (or ADMIN_TOKEN); operators get it with:
#   python3 ~/git/me/scripts/infisical-get.py CONTROL_PLANE_ADMIN_TOKEN --project ops-infra --env prod
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO="${GITHUB_REPOSITORY:-cipher982/longhouse}"
PUBLISH_WORKFLOW="Publish Runtime Image"
VERSION="${1:-}"
SOAK_HOURS="${SOAK_HOURS:-24}"
DOGFOOD_SUBDOMAIN="${SUBDOMAIN:-${LONGHOUSE_DEFAULT_SUBDOMAIN:-david010}}"
DEMO_SSH_HOST="${DEMO_SSH_HOST:-zerg}"
DEMO_ENV_PATH="${DEMO_ENV_PATH:-/home/zerg/manual-apps/longhouse-demo/.env}"
DEMO_COMPOSE_SERVICE="${DEMO_COMPOSE_SERVICE:-longhouse-demo}"
DEMO_HEALTH_URL="${DEMO_HEALTH_URL:-https://longhouse.ai/api/health}"
DEMO_VERIFY_TIMEOUT="${DEMO_VERIFY_TIMEOUT:-300}"
DOGFOOD_HEALTH_URL="${DOGFOOD_HEALTH_URL:-https://${DOGFOOD_SUBDOMAIN}.longhouse.ai/api/health}"

soak_response=""
instances_response=""
trap '[[ -z "$soak_response" ]] || rm -f "$soak_response"; [[ -z "$instances_response" ]] || rm -f "$instances_response"' EXIT

for tool in gh jq python3 curl git ssh; do
  command -v "$tool" >/dev/null 2>&1 || { echo "promote-production needs '$tool' on PATH." >&2; exit 1; }
done

if [[ -z "$VERSION" ]]; then
  echo "Usage: promote-production.sh VERSION (e.g. v0.1.52)" >&2
  exit 2
fi
if [[ ! "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "VERSION must match vX.Y.Z. Got: $VERSION" >&2
  exit 2
fi

case "${DOGFOOD_SUBDOMAIN,,}" in
  demo|kernel-canary|canary)
    echo "Refusing: dogfood subdomain resolved to a non-dogfood surface: $DOGFOOD_SUBDOMAIN" >&2
    exit 1
    ;;
esac

# --- Resolve VERSION -> exact commit SHA from the tag on origin, and require
# the GitHub release to exist (the laptop release shipped).
TAG_REF="refs/tags/${VERSION}"
ls_remote_output="$(git -C "$ROOT" ls-remote --tags origin "$TAG_REF" "${TAG_REF}^{}" 2>/dev/null || true)"
SHA="$(awk -v ref="${TAG_REF}^{}" '$2 == ref { print $1 }' <<<"$ls_remote_output")"
if [[ -z "$SHA" ]]; then
  SHA="$(awk -v ref="$TAG_REF" '$2 == ref { print $1 }' <<<"$ls_remote_output")"
fi
if [[ ! "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Tag $VERSION not found on origin. Cut it first: make release VERSION=$VERSION" >&2
  exit 1
fi

if ! gh release view "$VERSION" --repo "$REPO" >/dev/null 2>&1; then
  echo "GitHub release $VERSION not found. Cut it first: make release VERSION=$VERSION" >&2
  exit 1
fi
echo "Resolved $VERSION -> $SHA (release confirmed)."

# --- Authenticate like promote-dogfood.
. "$ROOT/scripts/lib/hosted-instance.sh"
lh_hosted_prepare_control_plane_auth

# --- Soak: the exact SHA must have a successful, sufficiently old
# promote-dogfood deployment. Production runs the exact digest dogfood ran.
SUBMISSION_KEY="promote-dogfood-${DOGFOOD_SUBDOMAIN}-${SHA}"
soak_response="$(mktemp)"
soak_http_code="$(curl -sS -o "$soak_response" -w "%{http_code}" \
  --connect-timeout 10 --max-time 30 \
  -H "X-Admin-Token: ${CONTROL_PLANE_ADMIN_TOKEN}" \
  -G --data-urlencode "submission_key=${SUBMISSION_KEY}" --data-urlencode "limit=50" \
  "${CONTROL_PLANE_URL%/}/api/deployments")"
if [[ "$soak_http_code" != "200" ]]; then
  echo "Failed to list deployments for soak check (HTTP ${soak_http_code})" >&2
  cat "$soak_response" >&2
  exit 1
fi

soak_result="$(SHA="$SHA" SOAK_HOURS="$SOAK_HOURS" python3 - "$soak_response" <<'PY'
import json
import os
import re
import sys
from datetime import datetime
from datetime import timedelta
from datetime import timezone

path = sys.argv[1]
sha = os.environ["SHA"]
soak_hours = float(os.environ["SOAK_HOURS"])

with open(path, encoding="utf-8") as handle:
    payload = json.load(handle)


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


rows = [
    row
    for row in payload.get("deployments", [])
    if row.get("status") == "success"
    and row.get("source_sha") == sha
    and row.get("completed_at")
]
if not rows:
    print("REFUSE\tnot_promoted\t-")
    raise SystemExit(0)

rows.sort(key=lambda row: parse(row["completed_at"]), reverse=True)
best = rows[0]
completed_at = parse(best["completed_at"])

digest = (best.get("image_digest") or "").strip()
image = (best.get("image") or "").strip()
image_ref = digest or image
if not re.search(r"@sha256:[0-9a-f]{64}$", image_ref):
    print("REFUSE\tno_digest\t-")
    raise SystemExit(0)

ready_at = completed_at + timedelta(hours=soak_hours)
now = datetime.now(timezone.utc)
if now < ready_at:
    print(f"REFUSE\ttoo_young\t{ready_at.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    raise SystemExit(0)

print(f"OK\t{best.get('id', '')}\t{image_ref}\t{completed_at.isoformat()}")
PY
)"

IFS=$'\t' read -r soak_state soak_field_2 soak_field_3 soak_field_4 <<<"$soak_result"
case "$soak_state" in
  OK)
    DEPLOYMENT_ID="$soak_field_2"
    PROD_IMAGE="$soak_field_3"
    SOAK_COMPLETED_AT="$soak_field_4"
    ;;
  REFUSE)
    case "$soak_field_2" in
      not_promoted)
        echo "Refusing $VERSION ($SHA): no successful dogfood deployment of this commit found." >&2
        echo "  promote-dogfood $SHA first (make promote-dogfood SHA=$SHA), then wait ${SOAK_HOURS}h before promoting production." >&2
        ;;
      too_young)
        echo "Refusing $VERSION ($SHA): dogfood soak not yet complete." >&2
        echo "  promote-dogfood $SHA already ran; wait until $soak_field_3 before promoting production." >&2
        ;;
      no_digest)
        echo "Refusing $VERSION ($SHA): the matching dogfood deployment has no usable digest-pinned image." >&2
        ;;
      *)
        echo "Refusing $VERSION ($SHA): soak check failed ($soak_field_2)." >&2
        ;;
    esac
    exit 1
    ;;
  *)
    echo "Unexpected soak-check output: $soak_result" >&2
    exit 1
    ;;
esac
echo "Soak satisfied: dogfood deployment $DEPLOYMENT_ID completed $SOAK_COMPLETED_AT, image $PROD_IMAGE."

# --- Dogfood healthy now.
dogfood_body="$(curl -sfL --max-time 10 "$DOGFOOD_HEALTH_URL" 2>/dev/null || echo '{}')"
dogfood_status="$(python3 -c '
import json
import sys
try:
    payload = json.loads(sys.argv[1] or "{}")
except ValueError:
    payload = {}
print((payload.get("status") if isinstance(payload, dict) else None) or "unreachable")
' "$dogfood_body")"
if [[ "$dogfood_status" != "healthy" ]]; then
  echo "Refusing $VERSION ($SHA): dogfood ($DOGFOOD_SUBDOMAIN) is not healthy right now (status=$dogfood_status)." >&2
  exit 1
fi
echo "Dogfood ($DOGFOOD_SUBDOMAIN) healthy."

# --- Targets: every active hosted instance except dogfood.
instances_response="$(mktemp)"
instances_http_code="$(curl -sS -o "$instances_response" -w "%{http_code}" \
  --connect-timeout 10 --max-time 30 \
  -H "X-Admin-Token: ${CONTROL_PLANE_ADMIN_TOKEN}" \
  "${CONTROL_PLANE_URL%/}/api/instances")"
if [[ "$instances_http_code" != "200" ]]; then
  echo "Failed to list control-plane instances (HTTP ${instances_http_code})" >&2
  cat "$instances_response" >&2
  exit 1
fi

TARGET_IDS_JSON="$(DOGFOOD_SUBDOMAIN="$DOGFOOD_SUBDOMAIN" python3 - "$instances_response" <<'PY'
import json
import os
import sys

path = sys.argv[1]
dogfood = os.environ["DOGFOOD_SUBDOMAIN"]
with open(path, encoding="utf-8") as handle:
    payload = json.load(handle)
ids = sorted(
    int(inst["id"])
    for inst in payload.get("instances", [])
    if inst.get("status") == "active" and inst.get("subdomain") != dogfood
)
print(json.dumps(ids, separators=(",", ":")))
PY
)"
TARGET_COUNT="$(python3 -c 'import json,sys; print(len(json.loads(sys.argv[1])))' "$TARGET_IDS_JSON")"
if [[ "$TARGET_COUNT" == "0" ]]; then
  echo "Targets: none (pointer-only promotion)."
else
  echo "Targets: $TARGET_COUNT active hosted instance(s): $TARGET_IDS_JSON"
fi

# --- Resolve publish-workflow provenance for the qualified image (the same
# provenance fields lh_hosted_reprovision requires); schema/build-identity are
# re-derived directly from the image itself inside lh_hosted_reprovision_production.
publish_run_json="$(gh run list --repo "$REPO" --workflow "$PUBLISH_WORKFLOW" --commit "$SHA" --status success --limit 5 \
  --json databaseId,number,attempt,headSha,workflowName,conclusion)"
publish_run_id="$(jq -r --arg sha "$SHA" '[.[] | select(.headSha == $sha)][0].databaseId // empty' <<<"$publish_run_json")"
publish_run_number="$(jq -r --arg sha "$SHA" '[.[] | select(.headSha == $sha)][0].number // empty' <<<"$publish_run_json")"
publish_run_attempt="$(jq -r --arg sha "$SHA" '[.[] | select(.headSha == $sha)][0].attempt // empty' <<<"$publish_run_json")"
if [[ -z "$publish_run_id" || -z "$publish_run_number" || -z "$publish_run_attempt" ]]; then
  echo "Refusing $VERSION ($SHA): no successful $PUBLISH_WORKFLOW run found to source deployment provenance." >&2
  exit 1
fi

export LH_DEPLOYMENT_SOURCE_WORKFLOW="$PUBLISH_WORKFLOW"
export LH_DEPLOYMENT_SOURCE_ORDER="$publish_run_number"
export LH_DEPLOYMENT_QUALIFICATION_ID="runtime-image-${publish_run_id}-${publish_run_attempt}"
export LH_DEPLOYMENT_REASON="production promotion of dogfood-soaked source ${SHA} (release ${VERSION}, dogfood deployment ${DEPLOYMENT_ID})"
export LH_DEPLOYMENT_IDEMPOTENCY_KEY="promote-production-${VERSION}-${SHA}"

lh_hosted_reprovision_production "$PROD_IMAGE" "$TARGET_IDS_JSON"

# --- Pin the public demo to the same digest and verify it.
echo "Pinning public demo ($DEMO_SSH_HOST:$DEMO_ENV_PATH) to $PROD_IMAGE..."
demo_dir="$(dirname "$DEMO_ENV_PATH")"
ssh "$DEMO_SSH_HOST" "set -euo pipefail
if grep -q '^LONGHOUSE_DEMO_IMAGE=' '$DEMO_ENV_PATH' 2>/dev/null; then
  sed -i \"s|^LONGHOUSE_DEMO_IMAGE=.*|LONGHOUSE_DEMO_IMAGE=$PROD_IMAGE|\" '$DEMO_ENV_PATH'
else
  printf 'LONGHOUSE_DEMO_IMAGE=%s\n' '$PROD_IMAGE' >> '$DEMO_ENV_PATH'
fi
cd '$demo_dir'
docker compose up -d --force-recreate '$DEMO_COMPOSE_SERVICE'"

echo "Verifying public demo reports $SHA..."
deadline=$(( $(date +%s) + DEMO_VERIFY_TIMEOUT ))
demo_commit=""
while [[ "$(date +%s)" -lt "$deadline" ]]; do
  demo_body="$(curl -sfL --max-time 10 "$DEMO_HEALTH_URL" 2>/dev/null || echo '{}')"
  demo_commit="$(python3 -c '
import json
import sys
try:
    payload = json.loads(sys.argv[1] or "{}")
except ValueError:
    payload = {}
build = payload.get("build") if isinstance(payload, dict) else None
print((build or {}).get("commit") or "")
' "$demo_body")"
  if [[ "$demo_commit" == "$SHA" ]]; then
    break
  fi
  sleep 5
done
if [[ "$demo_commit" != "$SHA" ]]; then
  echo "Timed out waiting for public demo to report commit $SHA (last seen: ${demo_commit:-<unreachable>})." >&2
  exit 1
fi
echo "Public demo verified at $SHA."

echo "Promoted production: version=$VERSION sha=$SHA digest=$PROD_IMAGE targets=$TARGET_COUNT demo_verified=true"
