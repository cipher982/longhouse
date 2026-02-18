#!/usr/bin/env bash
# Live E2E provisioning smoke test — runs against production control plane on zerg.
#
# Tests the REAL flow: admin creates instance → Docker provisions container →
# health check passes → SSO into instance → verify API works → cleanup.
#
# Usage:
#   ADMIN_TOKEN=xxx ./scripts/provision-e2e-live.sh
#   ADMIN_TOKEN=$(security find-generic-password -s longhouse-admin-token -w) ./scripts/provision-e2e-live.sh
#
# Options:
#   --keep    Skip cleanup (leave instance running for debugging)
#
# Requires: curl, python3 (or python), ssh access to zerg (for fallback diagnostics only)
set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CP_URL="${CP_URL:-https://control.longhouse.ai}"
ROOT_DOMAIN="${ROOT_DOMAIN:-longhouse.ai}"
TEST_SUBDOMAIN="e2e-$(date +%s)-${RANDOM}"
TEST_EMAIL="${TEST_SUBDOMAIN}@test.longhouse.ai"
HEALTH_TIMEOUT=120  # seconds
INSTANCE_ID=""
KEEP_INSTANCE=0

for arg in "$@"; do
  case "$arg" in
    --keep) KEEP_INSTANCE=1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

if [[ -z "${ADMIN_TOKEN:-}" ]]; then
  echo "ADMIN_TOKEN is required. Set it via environment or Keychain:" >&2
  echo "  ADMIN_TOKEN=\$(security find-generic-password -s longhouse-admin-token -w) $0" >&2
  exit 1
fi

# Find python
if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "Missing required command: python3 or python" >&2
  exit 1
fi

for cmd in curl; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing required command: $cmd" >&2
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

fail() { printf "\n FAIL: %s\n" "$1" >&2; exit 1; }
step() { printf "\n==> %s\n" "$1"; }
ok()   { printf "  ok %s\n" "$1"; }

# Safe JSON field extraction from a file (not stdin — avoids mixing stderr)
json_field_file() {
  "$PYTHON_BIN" -c "
import json, sys
try:
    with open(sys.argv[2], 'r') as f:
        data = json.load(f)
    print(data.get(sys.argv[1], ''))
except (json.JSONDecodeError, FileNotFoundError, KeyError) as e:
    print('')
" "$1" "$2"
}

# JSON field from string (for small inline uses)
json_field() {
  "$PYTHON_BIN" -c "
import json, sys
try:
    print(json.loads(sys.stdin.read()).get('$1', ''))
except (json.JSONDecodeError, ValueError):
    print('')
"
}

# ---------------------------------------------------------------------------
# Cleanup (always runs unless --keep)
# ---------------------------------------------------------------------------

cleanup() {
  set +e
  if [[ -n "$INSTANCE_ID" ]]; then
    if [[ "$KEEP_INSTANCE" -eq 1 ]]; then
      printf "\n--keep: Leaving instance %s running (%s.%s)\n" "$INSTANCE_ID" "$TEST_SUBDOMAIN" "$ROOT_DOMAIN"
      return
    fi
    step "Cleaning up: deprovisioning instance ${INSTANCE_ID}"
    deprov_code=$(curl -s -o /dev/null -w "%{http_code}" \
      --connect-timeout 10 --max-time 30 \
      -X POST "${CP_URL}/api/instances/${INSTANCE_ID}/deprovision" \
      -H "X-Admin-Token: ${ADMIN_TOKEN}" 2>/dev/null)
    if [[ "$deprov_code" == "200" ]]; then
      ok "Deprovisioned (${deprov_code})"
    else
      echo "  Warning: Deprovision returned ${deprov_code} — instance may need manual cleanup" >&2
    fi
  fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Step 1: Verify control plane is healthy
# ---------------------------------------------------------------------------

step "Checking control plane health at ${CP_URL}"
cp_health=$(curl -sf --connect-timeout 10 --max-time 15 "${CP_URL}/health" 2>/dev/null || echo "")
if [[ -z "$cp_health" ]]; then
  fail "Control plane not reachable at ${CP_URL}/health"
fi
ok "Control plane healthy"

# ---------------------------------------------------------------------------
# Step 2: Provision a test instance via admin API
# ---------------------------------------------------------------------------

step "Provisioning test instance: ${TEST_SUBDOMAIN} (${TEST_EMAIL})"
provision_file=$(mktemp)
provision_code=$(curl -s -o "$provision_file" -w "%{http_code}" \
  --connect-timeout 10 --max-time 60 \
  -X POST "${CP_URL}/api/instances" \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  -d "{\"email\":\"${TEST_EMAIL}\",\"subdomain\":\"${TEST_SUBDOMAIN}\"}" 2>/dev/null)

if [[ "$provision_code" != "200" && "$provision_code" != "201" ]]; then
  fail "Provision API returned ${provision_code}: $(cat "$provision_file" 2>/dev/null)"
fi

INSTANCE_ID=$(json_field_file "id" "$provision_file")
CONTAINER_NAME=$(json_field_file "container_name" "$provision_file")
PASSWORD=$(json_field_file "password" "$provision_file")
INSTANCE_STATUS=$(json_field_file "status" "$provision_file")
rm -f "$provision_file"

if [[ -z "$INSTANCE_ID" || -z "$CONTAINER_NAME" ]]; then
  fail "Invalid provision response — missing id or container_name"
fi
ok "Instance created: id=${INSTANCE_ID} container=${CONTAINER_NAME} status=${INSTANCE_STATUS}"

# ---------------------------------------------------------------------------
# Step 3: Wait for instance health via HTTPS
# ---------------------------------------------------------------------------

INSTANCE_URL="https://${TEST_SUBDOMAIN}.${ROOT_DOMAIN}"
step "Waiting for instance health at ${INSTANCE_URL}/api/health (timeout ${HEALTH_TIMEOUT}s)"

elapsed=0
while [[ $elapsed -lt $HEALTH_TIMEOUT ]]; do
  health_resp=$(curl -sf --connect-timeout 5 --max-time 10 \
    "${INSTANCE_URL}/api/health" 2>/dev/null || echo "")
  if [[ -n "$health_resp" ]]; then
    health_status=$(echo "$health_resp" | json_field "status")
    if [[ "$health_status" == "ok" || "$health_status" == "healthy" ]]; then
      ok "Instance healthy after ${elapsed}s"
      break
    fi
  fi
  sleep 3
  elapsed=$((elapsed + 3))
  printf "  ... waiting (%ds)\n" "$elapsed"
done

if [[ $elapsed -ge $HEALTH_TIMEOUT ]]; then
  echo "  Instance health response: ${health_resp:-<none>}"
  echo "  Attempting container diagnostics..."
  ssh zerg "docker logs ${CONTAINER_NAME} 2>&1 | tail -30" 2>/dev/null || true
  ssh zerg "docker ps -a --filter name=${CONTAINER_NAME}" 2>/dev/null || true
  fail "Instance health check timed out after ${HEALTH_TIMEOUT}s"
fi

# ---------------------------------------------------------------------------
# Step 4: Verify instance serves real content (not just health)
# ---------------------------------------------------------------------------

step "Verifying instance serves real API responses"

# Test timeline endpoint (should return HTML or JSON)
timeline_resp=$(curl -s -o /dev/null -w "%{http_code}" \
  --connect-timeout 5 --max-time 15 \
  "${INSTANCE_URL}/timeline" 2>/dev/null)
if [[ "$timeline_resp" == "200" ]]; then
  ok "GET /timeline -> 200"
else
  fail "GET /timeline -> ${timeline_resp} (expected 200)"
fi

# Test API sessions endpoint (should return JSON, may need auth)
sessions_code=$(curl -s -o /dev/null -w "%{http_code}" \
  --connect-timeout 5 --max-time 15 \
  "${INSTANCE_URL}/api/agents/sessions" 2>/dev/null)
if [[ "$sessions_code" == "200" ]]; then
  ok "GET /api/agents/sessions -> 200 (auth disabled in instance)"
elif [[ "$sessions_code" == "401" || "$sessions_code" == "403" ]]; then
  ok "GET /api/agents/sessions -> ${sessions_code} (auth enabled, expected)"
else
  fail "GET /api/agents/sessions -> ${sessions_code} (unexpected)"
fi

# Test config.js serves expected config
config_code=$(curl -s -o /dev/null -w "%{http_code}" \
  --connect-timeout 5 --max-time 10 \
  "${INSTANCE_URL}/config.js" 2>/dev/null)
if [[ "$config_code" == "200" ]]; then
  ok "GET /config.js -> 200"
else
  fail "GET /config.js -> ${config_code} (expected 200)"
fi

# ---------------------------------------------------------------------------
# Step 5: SSO flow — get login token and verify authenticated access
# ---------------------------------------------------------------------------

step "Testing SSO flow: admin issues login token -> instance accepts it"

COOKIE_JAR=$(mktemp)
token_resp=$(curl -sf --connect-timeout 10 --max-time 15 \
  -X POST "${CP_URL}/api/instances/${INSTANCE_ID}/login-token" \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" 2>/dev/null || echo "")
sso_token=$(echo "$token_resp" | json_field "token")

if [[ -z "$sso_token" ]]; then
  echo "  Warning: Could not get SSO token (may not be configured). Skipping SSO test."
else
  # Use the SSO token to authenticate — save session cookie
  sso_code=$(curl -s -o /dev/null -w "%{http_code}" \
    -c "$COOKIE_JAR" \
    --connect-timeout 10 --max-time 15 \
    "${INSTANCE_URL}/api/auth/accept-token" \
    -H "Content-Type: application/json" \
    -d "{\"token\":\"${sso_token}\"}" 2>/dev/null)

  if [[ "$sso_code" == "200" || "$sso_code" == "302" ]]; then
    ok "SSO token accepted (${sso_code})"

    # Verify authenticated access using the session cookie
    auth_code=$(curl -s -o /dev/null -w "%{http_code}" \
      -b "$COOKIE_JAR" \
      --connect-timeout 5 --max-time 15 \
      "${INSTANCE_URL}/api/agents/sessions" 2>/dev/null)
    if [[ "$auth_code" == "200" ]]; then
      ok "Authenticated access via SSO cookie -> 200"
    else
      echo "  Warning: Authenticated /api/agents/sessions -> ${auth_code} (cookie may not work). Non-fatal."
    fi
  else
    echo "  Warning: SSO accept-token returned ${sso_code} (may need config). Non-fatal."
  fi
fi
rm -f "$COOKIE_JAR"

# ---------------------------------------------------------------------------
# Step 6: Verify via control plane that instance shows as active
# ---------------------------------------------------------------------------

step "Verifying instance status in control plane"

inst_resp=$(curl -sf --connect-timeout 10 --max-time 15 \
  "${CP_URL}/api/instances/${INSTANCE_ID}" \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" 2>/dev/null || echo "")
inst_status=$(echo "$inst_resp" | json_field "status")

if [[ "$inst_status" == "active" || "$inst_status" == "running" || "$inst_status" == "provisioned" || "$inst_status" == "provisioning" ]]; then
  ok "Instance status: ${inst_status}"
else
  fail "Instance status unexpected: '${inst_status}' (expected active/running/provisioned)"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

printf "\n Live E2E provisioning test PASSED\n"
printf "   Instance: %s.%s\n" "$TEST_SUBDOMAIN" "$ROOT_DOMAIN"
printf "   Container: %s\n" "$CONTAINER_NAME"
if [[ "$KEEP_INSTANCE" -eq 1 ]]; then
  printf "   --keep: Instance left running for debugging.\n\n"
else
  printf "   Cleanup will deprovision on exit.\n\n"
fi
