#!/usr/bin/env bash
set -euo pipefail

echo '## Strip Life Hub networks or env from sauron docker-compose.'

echo '\n$ rg -n 'life-hub' apps/sauron/docker-compose.yml'
rg -n 'life-hub' apps/sauron/docker-compose.yml
echo '\n$ rg -n 'LIFE_HUB' apps/sauron/docker-compose.yml'
rg -n 'LIFE_HUB' apps/sauron/docker-compose.yml
