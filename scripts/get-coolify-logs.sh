#!/bin/bash
# Fetch latest Coolify deployment logs
# Usage: ./scripts/get-coolify-logs.sh [limit] [app_id]

LIMIT=${1:-1}
APP_ID="${2:-${APP_ID:-48}}"

set -euo pipefail

echo "=== Latest deployments (app_id=${APP_ID}, limit=${LIMIT}) ==="
echo ""

ssh clifford "docker exec coolify-db psql -U coolify -d coolify -c \"
  SELECT
    deployment_uuid,
    status,
    commit,
    to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') as started,
    to_char(finished_at, 'YYYY-MM-DD HH24:MI:SS') as finished,
    substring(logs from 1 for 100) as log_preview
  FROM application_deployment_queues
  WHERE application_id = '$APP_ID'
  ORDER BY created_at DESC
  LIMIT $LIMIT;
\""

echo ""
echo "=== Full logs ==="
echo ""

DEPLOYMENTS_TSV=$(
  ssh clifford "docker exec coolify-db psql -U coolify -d coolify -t -A -F $'\\t' -c \"
    SELECT
      deployment_uuid,
      status,
      commit,
      to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') as started,
      to_char(finished_at, 'YYYY-MM-DD HH24:MI:SS') as finished
    FROM application_deployment_queues
    WHERE application_id = '$APP_ID'
    ORDER BY created_at DESC
    LIMIT $LIMIT;
  \"" 2>&1
)

if [[ -z "${DEPLOYMENTS_TSV//$'\n'/}" ]]; then
  echo "No deployments found for application_id=${APP_ID}."
  exit 0
fi

while IFS=$'\t' read -r DEPLOYMENT_UUID STATUS COMMIT STARTED FINISHED; do
  DEPLOYMENT_UUID="$(echo "${DEPLOYMENT_UUID}" | xargs)"
  [[ -z "$DEPLOYMENT_UUID" ]] && continue

  echo ""
  echo "-----"
  echo "deployment_uuid=${DEPLOYMENT_UUID}"
  echo "status=${STATUS}"
  echo "commit=${COMMIT}"
  echo "started=${STARTED}"
  echo "finished=${FINISHED}"
  echo "-----"
  echo ""

  ssh clifford "docker exec coolify-db psql -U coolify -d coolify -t -c \"
    SELECT logs
    FROM application_deployment_queues
    WHERE deployment_uuid = '$DEPLOYMENT_UUID'
    LIMIT 1;
  \"" 2>&1
done <<< "$DEPLOYMENTS_TSV"
