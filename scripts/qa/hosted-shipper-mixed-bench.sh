#!/usr/bin/env bash
# Run the mixed live/archive shipper bench against a hosted Runtime Host.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ROOT_DIR/.env"
  set +a
fi

# shellcheck disable=SC1091
. "$ROOT_DIR/scripts/lib/hosted-instance.sh"

INSTANCE_SUBDOMAIN="${QA_INSTANCE_SUBDOMAIN:-${INSTANCE_SUBDOMAIN:-${LONGHOUSE_DEFAULT_SUBDOMAIN:-demo}}}"
FRONTEND_URL="${QA_INSTANCE_URL:-${PLAYWRIGHT_BASE_URL:-${FRONTEND_URL:-}}}"
API_URL="${PLAYWRIGHT_API_BASE_URL:-${API_URL:-$FRONTEND_URL}}"

lh_hosted_prepare_target "$INSTANCE_SUBDOMAIN" "$FRONTEND_URL" "$API_URL" "${LONGHOUSE_DEFAULT_SUBDOMAIN:-demo}"
API_URL="$LH_TARGET_API_URL"
INSTANCE_SUBDOMAIN="${LH_TARGET_SUBDOMAIN:-$INSTANCE_SUBDOMAIN}"

LH_BENCH_DEVICE_TOKEN_ID=""
LH_BENCH_ACCESS_TOKEN=""

cleanup_ephemeral_device_token() {
  if [[ -z "$LH_BENCH_DEVICE_TOKEN_ID" || -z "$LH_BENCH_ACCESS_TOKEN" ]]; then
    return 0
  fi

  if ! lh_hosted_revoke_device_token "$LH_BENCH_ACCESS_TOKEN" "$LH_BENCH_DEVICE_TOKEN_ID" "$API_URL" >/dev/null 2>&1; then
    echo "Warning: failed to revoke hosted shipper bench device token $LH_BENCH_DEVICE_TOKEN_ID" >&2
  fi
}

if [[ -z "${LONGHOUSE_DEVICE_TOKEN:-}" ]]; then
  if [[ -z "${SMOKE_RUNTIME_TOKEN:-}" ]]; then
    echo "Set LONGHOUSE_DEVICE_TOKEN or SMOKE_RUNTIME_TOKEN before hosted shipper bench." >&2
    exit 1
  fi

  echo "Provisioning ephemeral hosted shipper bench device token for $INSTANCE_SUBDOMAIN..." >&2
  LH_BENCH_ACCESS_TOKEN="$SMOKE_RUNTIME_TOKEN"
  IFS=$'\t' read -r LH_BENCH_DEVICE_TOKEN_ID LONGHOUSE_DEVICE_TOKEN <<< \
    "$(lh_hosted_create_device_token "$LH_BENCH_ACCESS_TOKEN" "$API_URL" "hosted-shipper-bench-${INSTANCE_SUBDOMAIN}-${RANDOM}")"
  export LONGHOUSE_DEVICE_TOKEN
  trap cleanup_ephemeral_device_token EXIT
fi

echo "Running hosted mixed live/archive shipper bench against $API_URL" >&2
python3 "$ROOT_DIR/scripts/build/generate_build_identity.py"
cd "$ROOT_DIR/engine"
cargo run --profile "${CARGO_PROFILE:-release}" -- bench \
  --synthetic-files "${HOSTED_SHIPPER_BENCH_FILES:-4}" \
  --synthetic-events-per-file "${HOSTED_SHIPPER_BENCH_EVENTS_PER_FILE:-40}" \
  --synthetic-bytes-per-event "${HOSTED_SHIPPER_BENCH_BYTES_PER_EVENT:-1024}" \
  --level L3 \
  --ship-url "$API_URL" \
  --ship-token "$LONGHOUSE_DEVICE_TOKEN" \
  --ship-concurrency "${HOSTED_SHIPPER_BENCH_CONCURRENCY:-3}" \
  --mixed-live-count "${HOSTED_SHIPPER_BENCH_LIVE_COUNT:-6}" \
  --mixed-live-max-p95-ms "${HOSTED_SHIPPER_BENCH_LIVE_MAX_P95_MS:-10000}"
