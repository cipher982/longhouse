#!/usr/bin/env bash
set -euo pipefail

echo '## Pluginize connector tools to keep OSS core lean.'

echo '\n$ rg --files apps/zerg/backend/zerg/tools/builtin -g '*github*' -g '*jira*' -g '*linear*' -g '*notion*' -g '*slack*' -g '*discord*' -g '*email*' -g '*sms*''
rg --files apps/zerg/backend/zerg/tools/builtin -g '*github*' -g '*jira*' -g '*linear*' -g '*notion*' -g '*slack*' -g '*discord*' -g '*email*' -g '*sms*'
