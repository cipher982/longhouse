#!/bin/bash
# Fetch latest Coolify deployment logs for zerg application
# Usage: ./scripts/get-coolify-logs.sh [limit]

LIMIT=${1:-1}
APP_ID="30"  # zerg application ID in Coolify

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
echo "=== Fetching full logs for latest deployment ==="
echo ""

ssh clifford "docker exec coolify-db psql -U coolify -d coolify -t -c \"
SELECT logs
FROM application_deployment_queues
WHERE application_id = '$APP_ID'
ORDER BY created_at DESC
LIMIT 1;
\"" 2>&1
