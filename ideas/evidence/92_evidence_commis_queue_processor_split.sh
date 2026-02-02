#!/usr/bin/env bash
set -euo pipefail

echo '## Merge commis_job_queue and commis_job_processor modules.'

echo '\n$ rg --files apps/zerg/backend/zerg/services -g 'commis_job_*''
rg --files apps/zerg/backend/zerg/services -g 'commis_job_*'
