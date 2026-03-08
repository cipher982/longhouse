#!/usr/bin/env bash
# Live E2E provisioning smoke test — runs against production control plane on zerg.
#
# Tests the REAL flow: admin creates instance → Docker provisions container →
# health check passes → SSO into instance → verify API works → cleanup.
#
# Usage:
#   ./scripts/provision-e2e-live.sh                                 # default: fetch CONTROL_PLANE_ADMIN_TOKEN from Infisical ops-infra/prod
#   CONTROL_PLANE_ADMIN_TOKEN=xxx ./scripts/provision-e2e-live.sh   # explicit override
#
# Options:
#   --keep    Skip cleanup (leave instance running for debugging)
#
# Requires: curl, python3 (or python), ssh access to zerg (for fallback diagnostics only)
set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOSTED_INSTANCE_HELPER="$ROOT_DIR/scripts/lib/hosted-instance.sh"
TEST_SUBDOMAIN="e2e-$(date +%s)-${RANDOM}"
TEST_EMAIL="${TEST_SUBDOMAIN}@test.longhouse.ai"
HEALTH_TIMEOUT=120  # seconds
INSTANCE_ID=""
INSTANCE_URL=""
KEEP_INSTANCE=0

for arg in "$@"; do
  case "$arg" in
    --keep) KEEP_INSTANCE=1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

if [[ ! -f "$HOSTED_INSTANCE_HELPER" ]]; then
  echo "Hosted instance helper missing: $HOSTED_INSTANCE_HELPER" >&2
  exit 1
fi

# shellcheck disable=SC1090
. "$HOSTED_INSTANCE_HELPER"
if ! lh_hosted_prepare_control_plane_auth; then
  echo "Unable to resolve hosted control-plane auth. Set CONTROL_PLANE_ADMIN_TOKEN/ADMIN_TOKEN explicitly or log into Infisical and populate CONTROL_PLANE_ADMIN_TOKEN in ops-infra/prod." >&2
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

# ---------------------------------------------------------------------------
# Cleanup (always runs unless --keep)
# ---------------------------------------------------------------------------

cleanup() {
  set +e
  if [[ -n "$INSTANCE_ID" ]]; then
    if [[ "$KEEP_INSTANCE" -eq 1 ]]; then
      printf "\n--keep: Leaving instance %s running (%s)\n" "$INSTANCE_ID" "$INSTANCE_URL"
      return
    fi
    step "Cleaning up: deprovisioning instance ${INSTANCE_ID}"
    if lh_hosted_deprovision "$INSTANCE_ID" >/dev/null 2>&1; then
      ok "Deprovisioned"
    else
      echo "  Warning: Deprovision failed — instance may need manual cleanup" >&2
    fi
  fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Step 1: Verify control plane is healthy
# ---------------------------------------------------------------------------

step "Checking control plane health at ${CONTROL_PLANE_URL}"
cp_health=$(curl -sf --connect-timeout 10 --max-time 15 "${CONTROL_PLANE_URL}/health" 2>/dev/null || echo "")
if [[ -z "$cp_health" ]]; then
  fail "Control plane not reachable at ${CONTROL_PLANE_URL}/health"
fi
ok "Control plane healthy"

# ---------------------------------------------------------------------------
# Step 2: Provision a test instance via admin API
# ---------------------------------------------------------------------------

step "Provisioning test instance: ${TEST_SUBDOMAIN} (${TEST_EMAIL})"
if ! lh_hosted_create_instance "$TEST_EMAIL" "$TEST_SUBDOMAIN"; then
  fail "Provision API failed for ${TEST_SUBDOMAIN}"
fi

INSTANCE_ID="$LH_INSTANCE_ID"
INSTANCE_URL="$LH_INSTANCE_URL"
INSTANCE_STATUS="$LH_INSTANCE_STATUS"
CONTAINER_NAME="$LH_INSTANCE_CONTAINER_NAME"

ok "Instance created: id=${INSTANCE_ID} url=${INSTANCE_URL} status=${INSTANCE_STATUS}"

# ---------------------------------------------------------------------------
# Step 3: Wait for instance health via HTTPS
# ---------------------------------------------------------------------------

step "Waiting for instance health at ${INSTANCE_URL}/api/health (timeout ${HEALTH_TIMEOUT}s)"

elapsed=0
while [[ $elapsed -lt $HEALTH_TIMEOUT ]]; do
  health_resp=$(curl -sf --connect-timeout 5 --max-time 10 \
    "${INSTANCE_URL}/api/health" 2>/dev/null || echo "")
  if printf '%s' "$health_resp" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"(ok|healthy)"'; then
    ok "Instance healthy after ${elapsed}s"
    break
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
LH_INSTANCE_ID="$INSTANCE_ID"
LH_INSTANCE_URL="$INSTANCE_URL"
export LH_INSTANCE_ID LH_INSTANCE_URL
sso_token="$(lh_hosted_issue_login_token "$INSTANCE_ID" 2>/dev/null || true)"

if [[ -z "$sso_token" ]]; then
  echo "  Warning: Could not get SSO token (may not be configured). Skipping SSO test."
else
  if lh_hosted_accept_login_token "$sso_token" "$COOKIE_JAR" "$INSTANCE_URL" >/dev/null 2>&1; then
    ok "SSO token accepted"

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
    echo "  Warning: SSO accept-token failed (may need config). Non-fatal."
  fi
fi
rm -f "$COOKIE_JAR"

# ---------------------------------------------------------------------------
# Step 6: Verify via control plane that instance shows as active
# ---------------------------------------------------------------------------

step "Verifying instance status in control plane"

if ! lh_hosted_get_instance "$INSTANCE_ID"; then
  fail "Failed to fetch control-plane status for instance ${INSTANCE_ID}"
fi
inst_status="$LH_INSTANCE_STATUS"

if [[ "$inst_status" == "active" || "$inst_status" == "running" || "$inst_status" == "provisioned" || "$inst_status" == "provisioning" ]]; then
  ok "Instance status: ${inst_status}"
else
  fail "Instance status unexpected: '${inst_status}' (expected active/running/provisioned)"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

printf "\n Live E2E provisioning test PASSED\n"
printf "   Instance: %s\n" "$INSTANCE_URL"
printf "   Container: %s\n" "$CONTAINER_NAME"
if [[ "$KEEP_INSTANCE" -eq 1 ]]; then
  printf "   --keep: Instance left running for debugging.\n\n"
else
  printf "   Cleanup will deprovision on exit.\n\n"
fi
