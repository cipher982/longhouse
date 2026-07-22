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

rm -f "$snapshot"

tmp_home="$(mktemp -d)"
tmp_bin="$(mktemp -d)"
python_args_file="$(mktemp)"

cat >"$tmp_bin/python3" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$@" >"$PYTHON_ARGS_FILE"
artifact=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--artifact" ]]; then
    artifact="${2:-}"
    shift 2
  else
    shift
  fi
done
if [[ -n "$artifact" ]]; then
  mkdir -p "$(dirname "$artifact")"
  printf '{"status":"ok"}\n' >"$artifact"
fi
SH
chmod +x "$tmp_bin/python3"

PATH="$tmp_bin:$PATH" \
LONGHOUSE_HOME="$tmp_home" \
ARTIFACT_DIR="$tmp_home/artifacts" \
ROUTE_E2E_PROVIDER="auto" \
ROUTE_E2E_HTTP_TIMEOUT_S="45" \
ROUTE_E2E_ATTEMPTS="1" \
PYTHON_ARGS_FILE="$python_args_file" \
  run_provider_live_route_e2e

if ! grep -Fxq -- "--skip-mismatch" "$python_args_file"; then
  echo "Expected dogfood route E2E to skip mismatch checks" >&2
  cat "$python_args_file" >&2
  exit 1
fi

rm -rf "$tmp_home" "$tmp_bin"
rm -f "$python_args_file"

echo "dogfood-runtime.test.sh: OK"
