#!/usr/bin/env bash
set -euo pipefail

echo '## Archive dev-docker Postgres script if Docker is legacy.'

echo '\n$ rg -n 'postgres' scripts/dev-docker.sh'
rg -n 'postgres' scripts/dev-docker.sh
