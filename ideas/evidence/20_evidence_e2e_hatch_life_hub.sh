#!/usr/bin/env bash
set -euo pipefail

echo '## Update e2e hatch script referencing ship_session_to_life_hub.'

echo '\n$ rg -n 'life_hub' apps/zerg/e2e/bin/hatch'
rg -n 'life_hub' apps/zerg/e2e/bin/hatch
