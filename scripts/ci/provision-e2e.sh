#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOSTED_INSTANCE_HELPER="$ROOT_DIR/scripts/lib/hosted-instance.sh"
CONTROL_PLANE_DIR="$ROOT_DIR/control-plane"
API_URL="http://127.0.0.1:48080"
INSTANCE_PORT="${PROVISION_E2E_INSTANCE_PORT:-}"
INSTANCE_URL=""
CI_SUBDOMAIN="ci"
CI_CONTAINER_NAME="longhouse-${CI_SUBDOMAIN}"
IMAGE_TAG="longhouse-runtime:ci-${GITHUB_SHA:-local}"
PROVISION_MODE="${PROVISION_E2E_MODE:-core}"

usage() {
  cat <<'USAGE'
Usage: scripts/ci/provision-e2e.sh [--mode core|extended]

Modes:
  core      Provision a hosted instance and verify the launch-critical ready path
  extended  Run core checks plus image backfill and rolling deploy
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      PROVISION_MODE="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

case "$PROVISION_MODE" in
  core|extended) ;;
  *)
    echo "Unsupported provision mode: $PROVISION_MODE" >&2
    usage >&2
    exit 1
    ;;
esac

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

pick_port() {
  "$PYTHON_BIN" - <<'PY'
import socket

sock = socket.socket()
sock.bind(("", 0))
print(sock.getsockname()[1])
sock.close()
PY
}

require_cmd docker
require_cmd curl

if command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  echo "Missing required command: python or python3" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not available. This gate requires a working Docker daemon." >&2
  exit 1
fi

if [[ ! -f "$HOSTED_INSTANCE_HELPER" ]]; then
  echo "Hosted instance helper missing: $HOSTED_INSTANCE_HELPER" >&2
  exit 1
fi

if [[ -z "$INSTANCE_PORT" ]]; then
  INSTANCE_PORT="$(pick_port)"
fi
INSTANCE_URL="http://127.0.0.1:${INSTANCE_PORT}"

# shellcheck disable=SC1090
. "$HOSTED_INSTANCE_HELPER"

wait_for_runtime_image() {
  local image_ref="$1"
  local attempts="${2:-45}"
  local sleep_seconds="${3:-4}"
  local attempt=0
  local platform="${PROVISION_E2E_RUNTIME_PLATFORM:-}"
  local -a pull_cmd=(docker pull)

  if docker image inspect "$image_ref" >/dev/null 2>&1; then
    return 0
  fi

  if [[ -n "$platform" ]]; then
    pull_cmd+=(--platform "$platform")
  fi

  printf "\n==> Waiting for published runtime image: %s\n" "$image_ref"
  for attempt in $(seq 1 "$attempts"); do
    if "${pull_cmd[@]}" "$image_ref" >/dev/null 2>&1; then
      printf "  Pulled runtime image on attempt %s/%s\n" "$attempt" "$attempts"
      return 0
    fi
    printf "  Not available yet (%s/%s); retrying in %ss\n" "$attempt" "$attempts" "$sleep_seconds"
    sleep "$sleep_seconds"
  done

  echo "Timed out waiting for published runtime image: $image_ref" >&2
  return 1
}

prepare_runtime_image() {
  local published_image="${PROVISION_E2E_RUNTIME_IMAGE:-}"

  if [[ -n "$published_image" ]]; then
    printf "\n==> Using published runtime image: %s\n" "$published_image"
    wait_for_runtime_image "$published_image"
    docker tag "$published_image" "$IMAGE_TAG"
    return 0
  fi

  printf "\n==> Building runtime image: %s\n" "$IMAGE_TAG"
  docker build -f "$ROOT_DIR/docker/runtime.dockerfile" -t "$IMAGE_TAG" "$ROOT_DIR"
}

prepare_runtime_image

printf "\n==> Provisioning mode: %s\n" "$PROVISION_MODE"
printf "==> Published instance port: %s\n" "$INSTANCE_PORT"

make_secret() {
  "$PYTHON_BIN" - <<'PY'
import base64, os
print(base64.urlsafe_b64encode(os.urandom(32)).decode())
PY
}

make_token() {
  "$PYTHON_BIN" - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
}

ADMIN_TOKEN="$(make_token)"
JWT_SECRET="$(make_token)"
INSTANCE_JWT_SECRET="$(make_token)"
INSTANCE_INTERNAL_SECRET="$(make_token)"
FERNET_SECRET="$(make_secret)"
TRIGGER_SECRET="$(make_secret)"

CONTROL_PLANE_URL="$API_URL"
CONTROL_PLANE_ADMIN_TOKEN="$ADMIN_TOKEN"
export CONTROL_PLANE_URL CONTROL_PLANE_ADMIN_TOKEN

CONTROL_PLANE_DB="/tmp/longhouse-control-plane.db"
if [[ -d "/home/runner/_work" ]]; then
  INSTANCE_DATA_ROOT="/home/runner/_work/longhouse-instance-data"
else
  INSTANCE_DATA_ROOT="/tmp/longhouse-instance-data"
fi
mkdir -p "$INSTANCE_DATA_ROOT"

cleanup_instance_data_root() {
  local data_root="$1"
  local cleanup_image="${2:-}"

  if [[ ! -d "$data_root" ]]; then
    return 0
  fi

  if rm -rf "$data_root" >/dev/null 2>&1; then
    return 0
  fi

  if [[ -n "$cleanup_image" ]] && docker image inspect "$cleanup_image" >/dev/null 2>&1; then
    docker run --rm -u 0:0 \
      -v "$data_root:/cleanup" \
      --entrypoint sh \
      "$cleanup_image" \
      -lc 'find /cleanup -mindepth 1 -maxdepth 1 -exec rm -rf {} +' \
      >/dev/null 2>&1 || true
  fi

  rm -rf "$data_root" >/dev/null 2>&1 || true
}

cleanup() {
  set +e
  if [[ -n "${INSTANCE_ID:-}" ]]; then
    lh_hosted_deprovision "$INSTANCE_ID" >/dev/null 2>&1 || true
  fi
  docker rm -f "${CI_CONTAINER_NAME}" >/dev/null 2>&1 || true
  if [[ -n "${CONTROL_PLANE_PID:-}" ]]; then
    kill "$CONTROL_PLANE_PID" >/dev/null 2>&1 || true
  fi
  rm -f "$CONTROL_PLANE_DB" || true
  cleanup_instance_data_root "$INSTANCE_DATA_ROOT" "$IMAGE_TAG"
}
trap cleanup EXIT

printf "\n==> Starting control plane\n"
cd "$CONTROL_PLANE_DIR"
uv sync
CONTROL_PLANE_ADMIN_TOKEN="$ADMIN_TOKEN" \
CONTROL_PLANE_JWT_SECRET="$JWT_SECRET" \
CONTROL_PLANE_DATABASE_URL="sqlite:///$CONTROL_PLANE_DB" \
CONTROL_PLANE_DOCKER_HOST="unix:///var/run/docker.sock" \
CONTROL_PLANE_IMAGE="$IMAGE_TAG" \
CONTROL_PLANE_PUBLISH_PORTS="1" \
CONTROL_PLANE_INSTANCE_PORT="$INSTANCE_PORT" \
CONTROL_PLANE_PROXY_NETWORK="" \
CONTROL_PLANE_INSTANCE_DATA_ROOT="$INSTANCE_DATA_ROOT" \
CONTROL_PLANE_INSTANCE_AUTH_DISABLED="1" \
CONTROL_PLANE_INSTANCE_JWT_SECRET="$INSTANCE_JWT_SECRET" \
CONTROL_PLANE_INSTANCE_INTERNAL_API_SECRET="$INSTANCE_INTERNAL_SECRET" \
CONTROL_PLANE_INSTANCE_FERNET_SECRET="$FERNET_SECRET" \
CONTROL_PLANE_INSTANCE_TRIGGER_SIGNING_SECRET="$TRIGGER_SECRET" \
uv run uvicorn control_plane.main:app --host 127.0.0.1 --port 48080 &
CONTROL_PLANE_PID=$!
cd "$ROOT_DIR"

printf "\n==> Waiting for control plane health\n"
for _ in {1..150}; do
  if curl -sf "${API_URL}/health" >/dev/null; then
    break
  fi
  sleep 1
  if ! kill -0 "$CONTROL_PLANE_PID" >/dev/null 2>&1; then
    echo "Control plane exited early." >&2
    exit 1
  fi
done

if ! curl -sf "${API_URL}/health" >/dev/null; then
  echo "Control plane health check failed." >&2
  exit 1
fi

printf "\n==> Provisioning test instance\n"
docker rm -f "${CI_CONTAINER_NAME}" >/dev/null 2>&1 || true
if ! lh_hosted_create_instance "ci@example.com" "$CI_SUBDOMAIN"; then
  echo "Failed to create CI instance." >&2
  exit 1
fi

INSTANCE_ID="$LH_INSTANCE_ID"
CONTAINER_NAME="${LH_INSTANCE_CONTAINER_NAME:-$CI_CONTAINER_NAME}"

printf "\n==> Waiting for instance health (%s)\n" "$CONTAINER_NAME"
for _ in {1..40}; do
  if curl -sf "${INSTANCE_URL}/api/readyz" >/dev/null; then
    break
  fi
  sleep 2
  if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "Instance container not running." >&2
    docker ps -a
    docker logs "$CONTAINER_NAME" || true
    exit 1
  fi
done

if ! curl -sf "${INSTANCE_URL}/api/readyz" >/dev/null; then
  echo "Instance readiness check failed." >&2
  echo "Host readiness probe:" >&2
  curl -sv --connect-timeout 2 --max-time 5 "${INSTANCE_URL}/api/readyz" -o /dev/null || true
  echo "Container health:" >&2
  docker inspect --format '{{json .State.Health}}' "$CONTAINER_NAME" 2>/dev/null || true
  docker ps -a
  docker logs "$CONTAINER_NAME" || true
  exit 1
fi

printf "\n==> Running smoke checks\n"
curl -sf "${INSTANCE_URL}/api/readyz" >/dev/null
curl -sf "${INSTANCE_URL}/api/health" >/dev/null
curl -sf "${INSTANCE_URL}/timeline" >/dev/null

if [[ "$PROVISION_MODE" == "core" ]]; then
  echo "✅ Provisioning E2E core checks passed."
  exit 0
fi

printf "\n==> Backfilling instance images\n"
curl -sf -X POST "${API_URL}/api/instances/backfill-images" \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" >/dev/null

# ---------------------------------------------------------------------------
# Rolling deploy test
# ---------------------------------------------------------------------------

DEPLOY_TAG="longhouse-runtime:ci-${GITHUB_SHA:-local}-deploy"
printf "\n==> Tagging deploy image: %s\n" "$DEPLOY_TAG"
docker tag "$IMAGE_TAG" "$DEPLOY_TAG"

printf "\n==> Starting rolling deploy (%s)\n" "$DEPLOY_TAG"
deploy_resp=$(mktemp)
curl -sf -X POST "${API_URL}/api/deployments" \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  -d "{\"image\":\"${DEPLOY_TAG}\"}" \
  -o "$deploy_resp"

DEPLOY_ID=$("$PYTHON_BIN" - <<'PY' "$deploy_resp"
import json, sys
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as handle:
    data = json.load(handle)
print(data["id"])
PY
) || { echo "Failed to parse deployment response:"; cat "$deploy_resp"; exit 1; }

printf "\n==> Waiting for deployment %s\n" "$DEPLOY_ID"
status_file=$(mktemp)
deploy_status="pending"
for _ in {1..60}; do
  curl -sf "${API_URL}/api/deployments/${DEPLOY_ID}" \
    -H "X-Admin-Token: ${ADMIN_TOKEN}" \
    -o "$status_file"
  read -r deploy_status deploy_succeeded deploy_failed <<<"$("$PYTHON_BIN" - <<'PY' "$status_file"
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as handle:
    data = json.load(handle)
print(data.get("status", ""), data.get("succeeded", 0), data.get("failed", 0))
PY
)"
  if [[ "$deploy_status" != "pending" && "$deploy_status" != "in_progress" ]]; then
    break
  fi
  sleep 1
done

if [[ "$deploy_status" != "completed" ]]; then
  echo "Deployment did not complete successfully (status=${deploy_status})." >&2
  cat "$status_file" >&2
  exit 1
fi
if [[ "${deploy_succeeded:-0}" -ne 1 || "${deploy_failed:-0}" -ne 0 ]]; then
  echo "Unexpected deployment counts: succeeded=${deploy_succeeded:-0}, failed=${deploy_failed:-0}" >&2
  cat "$status_file" >&2
  exit 1
fi

printf "\n==> Verifying instance health after deploy\n"
if ! curl -sf "${INSTANCE_URL}/api/readyz" >/dev/null; then
  echo "Instance readiness check failed after deploy." >&2
  echo "Host readiness probe after deploy:" >&2
  curl -sv --connect-timeout 2 --max-time 5 "${INSTANCE_URL}/api/readyz" -o /dev/null || true
  echo "Container health after deploy:" >&2
  docker inspect --format '{{json .State.Health}}' "$CONTAINER_NAME" 2>/dev/null || true
  docker logs "$CONTAINER_NAME" || true
  exit 1
fi

echo "✅ Provisioning E2E extended checks passed."
