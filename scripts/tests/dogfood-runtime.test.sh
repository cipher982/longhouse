#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

set --
# shellcheck disable=SC1091
LONGHOUSE_DOGFOOD_RUNTIME_SOURCE_ONLY=1 source "$ROOT_DIR/scripts/dev/dogfood-runtime.sh"

snapshot="$(mktemp)"
cat >"$snapshot" <<'JSON'
{
  "launch_readiness": {
    "state": "broken",
    "reasons": ["machine_name_runner_name_mismatch"],
    "warnings": ["service_generation_mismatch"]
  },
  "control_channel": {
    "status": "connected",
    "can_launch_codex": false,
    "launch_blocked_by": "no_launch_support",
    "launchable_providers": ["claude"]
  }
}
JSON

output="$(print_launch_readiness_summary "$snapshot")"

require_line() {
  local expected="$1"
  if ! grep -Fq "$expected" <<<"$output"; then
    echo "Expected dogfood launch summary to include: $expected" >&2
    echo "$output" >&2
    exit 1
  fi
}

require_line "launch_readiness.state: broken"
require_line "control_channel_status: connected"
require_line "can_launch_codex: false"
require_line "launch_blocked_by: no_launch_support"
require_line "launchable_providers: claude"
require_line "launch_readiness.reasons: machine_name_runner_name_mismatch"
require_line "launch_readiness.warnings: service_generation_mismatch"

rm -f "$snapshot"

echo "dogfood-runtime.test.sh: OK"
