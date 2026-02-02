#!/usr/bin/env bash
set -euo pipefail

echo '## Archive stop-docker Postgres script if Docker is legacy.'

echo '\n$ rg -n 'postgres' scripts/stop-docker.sh'
rg -n 'postgres' scripts/stop-docker.sh
