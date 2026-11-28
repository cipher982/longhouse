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
    "console_blocked_by": null,
    "console_ready_providers": ["claude"]
  }
}
JSON

output="$(print_runtime_readiness_summary "$snapshot")"

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
require_line "console_blocked_by: -"
require_line "console_ready_providers: claude"
require_line "launch_readiness.reasons: machine_name_runner_name_mismatch"
require_line "launch_readiness.warnings: service_generation_mismatch"

# The refresh waits until the new Machine Agent is connected and has a heartbeat verdict.
reported() {
  printf '%s' "$1" >"$snapshot"
  machine_agent_reported "$snapshot"
}
reported '{"control_channel":{"status":"connected"},"heartbeat_transport":{"evidence_state":"applied"}}' \
  || { echo "connected + applied should count as reported" >&2; exit 1; }
reported '{"control_channel":{"status":"connected"},"heartbeat_transport":{"evidence_state":"rejected"}}' \
  || { echo "a rejected verdict is still a report" >&2; exit 1; }
if reported '{"control_channel":{"status":"disconnected"},"heartbeat_transport":{"evidence_state":"applied"}}'; then
  echo "disconnected must not count as reported" >&2; exit 1
fi
if reported '{"control_channel":{"status":"connected"},"heartbeat_transport":{"evidence_state":"unknown"}}'; then
  echo "no heartbeat verdict yet must not count as reported" >&2; exit 1
fi
if reported 'not json'; then
  echo "an unreadable snapshot must not count as reported" >&2; exit 1
fi

rm -f "$snapshot"

echo "dogfood-runtime.test.sh: OK"
