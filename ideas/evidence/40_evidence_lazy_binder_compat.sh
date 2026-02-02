#!/usr/bin/env bash
set -euo pipefail

echo '## Drop non-lazy binder compatibility path.'

echo '\n$ rg -n 'compat' apps/zerg/backend/zerg/tools/lazy_binder.py'
rg -n 'compat' apps/zerg/backend/zerg/tools/lazy_binder.py
