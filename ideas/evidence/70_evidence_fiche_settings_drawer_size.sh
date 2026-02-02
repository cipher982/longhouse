#!/usr/bin/env bash
set -euo pipefail

echo '## Split FicheSettingsDrawer.tsx (large file).'

echo '\n$ wc -l apps/zerg/frontend-web/src/components/fiche-settings/FicheSettingsDrawer.tsx'
wc -l apps/zerg/frontend-web/src/components/fiche-settings/FicheSettingsDrawer.tsx
