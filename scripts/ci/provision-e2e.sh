#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTROL_PLANE_DIR="$ROOT_DIR/apps/control-plane"
API_URL="http://127.0.0.1:48080"
INSTANCE_PORT=8000
INSTANCE_URL="http://127.0.0.1:${INSTANCE_PORT}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_cmd docker
require_cmd curl
require_cmd python

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not available. This gate requires a working Docker daemon." >&2
  exit 1
fi

IMAGE_TAG="longhouse-runtime:ci-${GITHUB_SHA:-local}"

printf "\n==> Building runtime image: %s\n" "$IMAGE_TAG"
docker build -f "$ROOT_DIR/docker/runtime.dockerfile" -t "$IMAGE_TAG" "$ROOT_DIR"

make_secret() {
  python - <<'PY'
import base64, os
print(base64.urlsafe_b64encode(os.urandom(32)).decode())
PY
}

make_token() {
  python - <<'PY'
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

CONTROL_PLANE_DB="/tmp/longhouse-control-plane.db"
if [[ -d "/home/runner/_work" ]]; then
  INSTANCE_DATA_ROOT="/home/runner/_work/longhouse-instance-data"
else
  INSTANCE_DATA_ROOT="/tmp/longhouse-instance-data"
fi
mkdir -p "$INSTANCE_DATA_ROOT"

cleanup() {
  set +e
  if [[ -n "${INSTANCE_ID:-}" ]]; then
    curl -sf -X POST "${API_URL}/api/instances/${INSTANCE_ID}/deprovision" \
      -H "X-Admin-Token: ${ADMIN_TOKEN}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${CONTROL_PLANE_PID:-}" ]]; then
    kill "$CONTROL_PLANE_PID" >/dev/null 2>&1 || true
  fi
  rm -f "$CONTROL_PLANE_DB" || true
  rm -rf "$INSTANCE_DATA_ROOT" || true
}
trap cleanup EXIT

printf "\n==> Starting control plane\n"
(
  cd "$CONTROL_PLANE_DIR"
  uv sync
  CONTROL_PLANE_ADMIN_TOKEN="$ADMIN_TOKEN" \
  CONTROL_PLANE_JWT_SECRET="$JWT_SECRET" \
  CONTROL_PLANE_DATABASE_URL="sqlite:///$CONTROL_PLANE_DB" \
  CONTROL_PLANE_DOCKER_HOST="unix:///var/run/docker.sock" \
  CONTROL_PLANE_IMAGE="$IMAGE_TAG" \
  CONTROL_PLANE_PUBLISH_PORTS="1" \
  CONTROL_PLANE_PROXY_NETWORK="" \
  CONTROL_PLANE_INSTANCE_DATA_ROOT="$INSTANCE_DATA_ROOT" \
  CONTROL_PLANE_INSTANCE_AUTH_DISABLED="1" \
  CONTROL_PLANE_INSTANCE_JWT_SECRET="$INSTANCE_JWT_SECRET" \
  CONTROL_PLANE_INSTANCE_INTERNAL_API_SECRET="$INSTANCE_INTERNAL_SECRET" \
  CONTROL_PLANE_INSTANCE_FERNET_SECRET="$FERNET_SECRET" \
  CONTROL_PLANE_INSTANCE_TRIGGER_SIGNING_SECRET="$TRIGGER_SECRET" \
  uv run uvicorn control_plane.main:app --host 127.0.0.1 --port 48080
) &
CONTROL_PLANE_PID=$!

printf "\n==> Waiting for control plane health\n"
for _ in {1..30}; do
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
response=$(curl -sf -X POST "${API_URL}/api/instances" \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  -d '{"email":"ci@example.com","subdomain":"ci"}')

INSTANCE_ID=$(python - <<'PY'
import json, sys
print(json.load(sys.stdin)["id"])
PY
<<<"$response")

CONTAINER_NAME=$(python - <<'PY'
import json, sys
print(json.load(sys.stdin)["container_name"])
PY
<<<"$response")

printf "\n==> Waiting for instance health (%s)\n" "$CONTAINER_NAME"
for _ in {1..40}; do
  if curl -sf "${INSTANCE_URL}/api/system/health" >/dev/null; then
    break
  fi
  sleep 2
  if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "Instance container not running." >&2
    docker ps -a
    exit 1
  fi
done

if ! curl -sf "${INSTANCE_URL}/api/system/health" >/dev/null; then
  echo "Instance health check failed." >&2
  docker ps -a
  docker logs "$CONTAINER_NAME" || true
  exit 1
fi

printf "\n==> Running smoke checks\n"
curl -sf "${INSTANCE_URL}/health" >/dev/null
curl -sf "${INSTANCE_URL}/api/system/health" >/dev/null
curl -sf "${INSTANCE_URL}/timeline" >/dev/null

echo "✅ Provisioning E2E checks passed."
