#!/usr/bin/env bash
set -euo pipefail

echo '## Remove HEURISTIC or HYBRID decision modes in LLM decider.'

echo '\n$ rg -n 'HEURISTIC' apps/zerg/backend/zerg/services/llm_decider.py'
rg -n 'HEURISTIC' apps/zerg/backend/zerg/services/llm_decider.py
echo '\n$ rg -n 'HYBRID' apps/zerg/backend/zerg/services/llm_decider.py'
rg -n 'HYBRID' apps/zerg/backend/zerg/services/llm_decider.py
