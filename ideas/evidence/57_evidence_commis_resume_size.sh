#!/usr/bin/env bash
set -euo pipefail

echo '## Split commis_resume.py (large file).'

echo '\n$ wc -l apps/zerg/backend/zerg/services/commis_resume.py'
wc -l apps/zerg/backend/zerg/services/commis_resume.py
