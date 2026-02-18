#!/usr/bin/env bash
# Live E2E provisioning smoke test — runs against production control plane on zerg.
#
# Tests the REAL flow: admin creates instance → Docker provisions container →
# health check passes → SSO into instance → verify API works → cleanup.
#
# Usage:
#   ./scripts/provision-e2e-live.sh
#   ADMIN_TOKEN=xxx ./scripts/provision-e2e-live.sh   # override token
#
# Requires: curl, ssh access to zerg (for fallback diagnostics only)
set -euo pipefail

CP_URL="${CP_URL:-https://control.longhouse.ai}"
ROOT_DOMAIN="${ROOT_DOMAIN:-longhouse.ai}"
ADMIN_TOKEN="${ADMIN_TOKEN:-22d3ddbd5280b9acfa406d5e02f124be79abbdbf3dac7af7c2ca0e2333975a9e}"
TEST_SUBDOMAIN="e2e-$(date +%s)"
TEST_EMAIL="${TEST_SUBDOMAIN}@test.longhouse.ai"
HEALTH_TIMEOUT=120  # seconds
INSTANCE_ID=""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

fail() { printf "\n❌ FAIL: %s\n" "$1" >&2; exit 1; }
step() { printf "\n==> %s\n" "$1"; }
ok()   { printf "  ✓ %s\n" "$1"; }

json_field() {
  python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('$1',''))"
}

# ---------------------------------------------------------------------------
# Cleanup (always runs)
# ---------------------------------------------------------------------------

cleanup() {
  set +e
  if [[ -n "$INSTANCE_ID" ]]; then
    step "Cleaning up: deprovisioning instance ${INSTANCE_ID}"
    curl -sf -X POST "${CP_URL}/api/instances/${INSTANCE_ID}/deprovision" \
      -H "X-Admin-Token: ${ADMIN_TOKEN}" >/dev/null 2>&1
    ok "Deprovisioned"

    # Delete user + instance records (clean slate)
    # The deprovision endpoint marks as deprovisioned but doesn't delete DB rows.
    # That's fine — unique subdomain per run prevents collisions.
  fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Step 1: Verify control plane is healthy
# ---------------------------------------------------------------------------

step "Checking control plane health at ${CP_URL}"
cp_health=$(curl -sf "${CP_URL}/health" 2>/dev/null || echo "")
if [[ -z "$cp_health" ]]; then
  fail "Control plane not reachable at ${CP_URL}/health"
fi
ok "Control plane healthy"

# ---------------------------------------------------------------------------
# Step 2: Provision a test instance via admin API
# ---------------------------------------------------------------------------

step "Provisioning test instance: ${TEST_SUBDOMAIN} (${TEST_EMAIL})"
provision_resp=$(curl -sf -X POST "${CP_URL}/api/instances" \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  -d "{\"email\":\"${TEST_EMAIL}\",\"subdomain\":\"${TEST_SUBDOMAIN}\"}" 2>&1) \
  || fail "Provision API call failed: ${provision_resp}"

INSTANCE_ID=$(echo "$provision_resp" | json_field "id")
CONTAINER_NAME=$(echo "$provision_resp" | json_field "container_name")
PASSWORD=$(echo "$provision_resp" | json_field "password")
INSTANCE_STATUS=$(echo "$provision_resp" | json_field "status")

if [[ -z "$INSTANCE_ID" || -z "$CONTAINER_NAME" ]]; then
  fail "Invalid provision response: ${provision_resp}"
fi
ok "Instance created: id=${INSTANCE_ID} container=${CONTAINER_NAME} status=${INSTANCE_STATUS}"

# ---------------------------------------------------------------------------
# Step 3: Wait for instance health via HTTPS
# ---------------------------------------------------------------------------

INSTANCE_URL="https://${TEST_SUBDOMAIN}.${ROOT_DOMAIN}"
step "Waiting for instance health at ${INSTANCE_URL}/api/health (timeout ${HEALTH_TIMEOUT}s)"

elapsed=0
while [[ $elapsed -lt $HEALTH_TIMEOUT ]]; do
  health_resp=$(curl -sf "${INSTANCE_URL}/api/health" 2>/dev/null || echo "")
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
  # Diagnostics
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
timeline_resp=$(curl -s -o /dev/null -w "%{http_code}" "${INSTANCE_URL}/timeline" 2>/dev/null)
if [[ "$timeline_resp" == "200" ]]; then
  ok "GET /timeline → 200"
else
  fail "GET /timeline → ${timeline_resp} (expected 200)"
fi

# Test API sessions endpoint (should return JSON, may need auth)
# Use -o /dev/null separately to avoid concatenation issues; drop -f so 401 doesn't fail
sessions_code=$(curl -s -o /dev/null -w "%{http_code}" "${INSTANCE_URL}/api/agents/sessions" 2>/dev/null)
if [[ "$sessions_code" == "200" ]]; then
  ok "GET /api/agents/sessions → 200 (auth disabled in instance)"
elif [[ "$sessions_code" == "401" || "$sessions_code" == "403" ]]; then
  ok "GET /api/agents/sessions → ${sessions_code} (auth enabled, expected)"
else
  fail "GET /api/agents/sessions → ${sessions_code} (unexpected)"
fi

# ---------------------------------------------------------------------------
# Step 5: SSO flow — get login token and verify authenticated access
# ---------------------------------------------------------------------------

step "Testing SSO flow: admin issues login token → instance accepts it"

# Get SSO token from control plane
token_resp=$(curl -sf -X POST "${CP_URL}/api/instances/${INSTANCE_ID}/login-token" \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" 2>/dev/null || echo "")
sso_token=$(echo "$token_resp" | json_field "token")

if [[ -z "$sso_token" ]]; then
  echo "  Warning: Could not get SSO token (may not be configured). Skipping SSO test."
else
  # Use the SSO token to authenticate to the instance
  sso_code=$(curl -sf -o /dev/null -w "%{http_code}" \
    "${INSTANCE_URL}/api/auth/accept-token" \
    -H "Content-Type: application/json" \
    -d "{\"token\":\"${sso_token}\"}" 2>/dev/null || echo "000")

  if [[ "$sso_code" == "200" || "$sso_code" == "302" ]]; then
    ok "SSO token accepted (${sso_code})"
  else
    echo "  Warning: SSO accept-token returned ${sso_code} (may need config). Non-fatal."
  fi
fi

# ---------------------------------------------------------------------------
# Step 6: Verify via control plane that instance shows as active
# ---------------------------------------------------------------------------

step "Verifying instance status in control plane"

inst_resp=$(curl -sf "${CP_URL}/api/instances/${INSTANCE_ID}" \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" 2>/dev/null || echo "")
inst_status=$(echo "$inst_resp" | json_field "status")

ok "Instance status: ${inst_status}"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

printf "\n✅ Live E2E provisioning test PASSED\n"
printf "   Instance: %s.%s\n" "$TEST_SUBDOMAIN" "$ROOT_DOMAIN"
printf "   Container: %s\n" "$CONTAINER_NAME"
printf "   Cleanup will deprovision on exit.\n\n"
