#!/usr/bin/env bash
# deploy-status.sh — print deployed SHA, uptime, and health for all Longhouse surfaces.
set -euo pipefail

# SSH alias for the runtime host (configured by scripts/ci/setup-deploy-ssh.sh)
RUNTIME_HOST="${RUNTIME_HOST:-runtime-host}"
CANARY_CONTAINER_NAME="${CANARY_CONTAINER_NAME:-longhouse-${LONGHOUSE_DEFAULT_SUBDOMAIN:-demo}}"
CANARY_HEALTH_URL="${CANARY_HEALTH_URL:-https://${LONGHOUSE_DEFAULT_SUBDOMAIN:-demo}.longhouse.ai/api/health}"

# --- Gather container state from zerg ----------------------------------------

read_container() {
    local name_or_label="$1"
    local filter="$2"
    local info
    info=$(ssh "$RUNTIME_HOST" "docker ps --format '{{.Image}} {{.Status}}' $filter" 2>/dev/null | head -1)
    if [[ -z "$info" ]]; then
        echo "- - -"
        return
    fi
    local image status
    image=$(echo "$info" | awk '{print $1}')
    status=$(echo "$info" | cut -d' ' -f2-)
    local sha="${image##*:}"
    [[ ${#sha} -gt 12 ]] && sha="${sha:0:10}"
    echo "$sha $status"
}

# Demo runtime — Coolify container with randomized name
demo_raw=$(read_container "demo" "--filter 'label=coolify.serviceName=longhouse-demo'")
demo_sha=$(echo "$demo_raw" | awk '{print $1}')
demo_uptime=$(echo "$demo_raw" | cut -d' ' -f2-)

# Control plane — Coolify container
cp_raw=$(read_container "control-plane" "--filter 'label=coolify.serviceName=longhouse-control-plane'")
cp_sha=$(echo "$cp_raw" | awk '{print $1}')
cp_uptime=$(echo "$cp_raw" | cut -d' ' -f2-)

# Canary — direct container name
if [[ -n "$CANARY_CONTAINER_NAME" ]]; then
    canary_raw=$(ssh "$RUNTIME_HOST" "docker ps --format '{{.Image}} {{.Status}}' --filter 'name=$CANARY_CONTAINER_NAME'" 2>/dev/null | head -1)
else
    canary_raw=""
fi
if [[ -n "$canary_raw" ]]; then
    canary_image=$(echo "$canary_raw" | awk '{print $1}')
    canary_sha="${canary_image##*:}"
    [[ ${#canary_sha} -gt 12 ]] && canary_sha="${canary_sha:0:10}"
    canary_uptime=$(echo "$canary_raw" | cut -d' ' -f2-)
else
    canary_sha="-"
    canary_uptime="-"
fi

# --- Gather health -----------------------------------------------------------

health_status() {
    local url="$1"
    local result
    result=$(curl -sf --max-time 5 "$url" 2>/dev/null) || { echo "unreachable"; return; }
    echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null || echo "unknown"
}

demo_health=$(health_status "https://longhouse.ai/api/health")
cp_health=$(health_status "https://control.longhouse.ai/health")
if [[ -n "$CANARY_HEALTH_URL" ]]; then
    canary_health=$(health_status "$CANARY_HEALTH_URL")
else
    canary_health="-"
fi

# --- Local HEAD for comparison ------------------------------------------------

local_sha=$(git rev-parse --short=10 HEAD 2>/dev/null || echo "-")

# --- Print table --------------------------------------------------------------

printf "\n"
printf "%-20s %-12s %-10s %s\n" "Surface" "SHA" "Health" "Uptime"
printf "%-20s %-12s %-10s %s\n" "-------" "---" "------" "------"
printf "%-20s %-12s %-10s %s\n" "Demo runtime"    "$demo_sha"   "$demo_health"   "$demo_uptime"
printf "%-20s %-12s %-10s %s\n" "Control plane"   "$cp_sha"     "$cp_health"     "$cp_uptime"
printf "%-20s %-12s %-10s %s\n" "Canary"          "$canary_sha" "$canary_health" "$canary_uptime"
printf "%-20s %-12s\n"          "Local HEAD"       "$local_sha"
printf "\n"

# --- Drift warning ------------------------------------------------------------

if [[ "$demo_sha" != "-" && "$demo_sha" != "$local_sha" ]]; then
    echo "⚠  Local HEAD ($local_sha) differs from deployed demo ($demo_sha)"
fi
