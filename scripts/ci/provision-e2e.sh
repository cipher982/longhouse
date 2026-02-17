#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTROL_PLANE_DIR="$ROOT_DIR/apps/control-plane"
API_URL="http://127.0.0.1:48080"
INSTANCE_PORT=8000
INSTANCE_URL="http://127.0.0.1:${INSTANCE_PORT}"
CI_SUBDOMAIN="ci"
CI_CONTAINER_NAME="longhouse-${CI_SUBDOMAIN}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
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

IMAGE_TAG="longhouse-runtime:ci-${GITHUB_SHA:-local}"

printf "\n==> Building runtime image: %s\n" "$IMAGE_TAG"
docker build -f "$ROOT_DIR/docker/runtime.dockerfile" -t "$IMAGE_TAG" "$ROOT_DIR"

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
  docker rm -f "${CI_CONTAINER_NAME}" >/dev/null 2>&1 || true
  if [[ -n "${CONTROL_PLANE_PID:-}" ]]; then
    kill "$CONTROL_PLANE_PID" >/dev/null 2>&1 || true
  fi
  rm -f "$CONTROL_PLANE_DB" || true
  rm -rf "$INSTANCE_DATA_ROOT" || true
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
response_file=$(mktemp)
curl -sf -X POST "${API_URL}/api/instances" \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  -d "{\"email\":\"ci@example.com\",\"subdomain\":\"${CI_SUBDOMAIN}\"}" \
  -o "$response_file"

INSTANCE_ID=$("$PYTHON_BIN" - <<'PY' "$response_file"
import json, sys
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as handle:
    data = json.load(handle)
print(data["id"])
PY
) || { echo "Failed to parse instance response:"; cat "$response_file"; exit 1; }

CONTAINER_NAME=$("$PYTHON_BIN" - <<'PY' "$response_file"
import json, sys
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as handle:
    data = json.load(handle)
print(data["container_name"])
PY
) || { echo "Failed to parse instance response:"; cat "$response_file"; exit 1; }

printf "\n==> Waiting for instance health (%s)\n" "$CONTAINER_NAME"
for _ in {1..40}; do
  if curl -sf "${INSTANCE_URL}/api/health" >/dev/null; then
    break
  fi
  sleep 2
  if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "Instance container not running." >&2
    docker ps -a
    exit 1
  fi
done

if ! curl -sf "${INSTANCE_URL}/api/health" >/dev/null; then
  echo "Instance health check failed." >&2
  docker ps -a
  docker logs "$CONTAINER_NAME" || true
  exit 1
fi

printf "\n==> Running smoke checks\n"
curl -sf "${INSTANCE_URL}/api/health" >/dev/null
curl -sf "${INSTANCE_URL}/api/health" >/dev/null
curl -sf "${INSTANCE_URL}/timeline" >/dev/null

printf "\n==> Backfilling instance images\n"
curl -sf -X POST "${API_URL}/api/instances/backfill-images" \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" >/dev/null

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
if ! curl -sf "${INSTANCE_URL}/api/health" >/dev/null; then
  echo "Instance health check failed after deploy." >&2
  docker logs "$CONTAINER_NAME" || true
  exit 1
fi

echo "✅ Provisioning E2E checks passed."
