#!/usr/bin/env bash
set -euo pipefail

echo '## Remove legacy trigger key scanner once legacy shapes dropped.'

echo '\n$ sed -n '1,80p' scripts/check_legacy_triggers.sh'
sed -n '1,80p' scripts/check_legacy_triggers.sh
