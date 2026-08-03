#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

ATTEMPTS="${SESSION_PROPAGATION_ATTEMPTS:-3}"
ITERATIONS="${SESSION_PROPAGATION_ITERATIONS:-1}"
SUBDOMAIN="${SESSION_PROPAGATION_SUBDOMAIN:-${LONGHOUSE_DEFAULT_SUBDOMAIN:-demo}}"
PROJECT="${SESSION_PROPAGATION_PROJECT:-zerg}"
SLA_CASE="${SESSION_PROPAGATION_SLA_CASE:-managed_codex_warm_live_graceful_close}"
PROFILE="${SESSION_PROPAGATION_PROFILE:-warm-live}"
PROVIDER="${SESSION_PROPAGATION_PROVIDER:-codex}"
OWNERSHIP="${SESSION_PROPAGATION_OWNERSHIP:-managed}"
CODEX_EFFORT="${SESSION_PROPAGATION_CODEX_EFFORT:-low}"
PROVIDER_TO_PIXEL_ONLY="${SESSION_PROPAGATION_PROVIDER_TO_PIXEL_ONLY:-false}"
EXPECTED_HOSTED_COMMIT="${SESSION_PROPAGATION_EXPECTED_HOSTED_COMMIT:-}"
BOOTSTRAP_ENGINE="${SESSION_PROPAGATION_BOOTSTRAP_ENGINE:-false}"
MACHINE_NAME="${SESSION_PROPAGATION_MACHINE_NAME:-gha-session-propagation-${PROVIDER}}"
SSH_TARGET="${SESSION_PROPAGATION_SSH_TARGET:-zerg}"
BASE_RUN_ID="${SESSION_PROPAGATION_RUN_ID:-session-propagation-$(date -u +%Y%m%dT%H%M%SZ)}"
OUTPUT_ROOT="${SESSION_PROPAGATION_OUTPUT_ROOT:-$ROOT_DIR/artifacts/session-propagation-sla/$BASE_RUN_ID}"
RETRY_SLEEP_SECS="${SESSION_PROPAGATION_RETRY_SLEEP_SECS:-15}"
PROFILER="$ROOT_DIR/scripts/ops/profile-managed-session-propagation.py"

mkdir -p "$OUTPUT_ROOT"

summary="$OUTPUT_ROOT/summary.md"
{
  echo "# Session Propagation SLA"
  echo ""
  echo "- Run ID: \`$BASE_RUN_ID\`"
  echo "- SLA case: \`$SLA_CASE\`"
  echo "- Profile: \`$PROFILE\`"
  echo "- Provider: \`$PROVIDER\`"
  echo "- Ownership: \`$OWNERSHIP\`"
  echo "- Subdomain: \`$SUBDOMAIN\`"
  echo "- Project: \`$PROJECT\`"
  echo "- Attempts: \`$ATTEMPTS\`"
  echo "- Iterations per attempt: \`$ITERATIONS\`"
  echo "- Provider-to-pixel only: \`$PROVIDER_TO_PIXEL_ONLY\`"
  echo "- Expected hosted commit: \`${EXPECTED_HOSTED_COMMIT:-not pinned}\`"
  echo "- Started: \`$(date -u +%Y-%m-%dT%H:%M:%SZ)\`"
  echo ""
  echo "## Attempts"
  echo ""
  echo "| Attempt | Exit | Classification | Artifact |"
  echo "| ---: | ---: | --- | --- |"
} > "$summary"

missing=0
required_cmds=(python3 bun longhouse longhouse-engine "$PROVIDER")
for required_cmd in "${required_cmds[@]}"; do
  if ! command -v "$required_cmd" >/dev/null 2>&1; then
    echo "Missing required command: $required_cmd" >&2
    missing=1
  fi
done
if [[ "$missing" == "1" ]]; then
  echo "| 0 | 3 | setup_error: missing local profiler prerequisite | \`$OUTPUT_ROOT\` |" >> "$summary"
  echo "$summary"
  exit 3
fi

engine_pid=""
cleanup() {
  if [[ -n "$engine_pid" ]] && kill -0 "$engine_pid" 2>/dev/null; then
    kill "$engine_pid" 2>/dev/null || true
    wait "$engine_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

if [[ "$BOOTSTRAP_ENGINE" == "true" ]]; then
  if [[ -z "${LONGHOUSE_DEVICE_TOKEN:-}" ]]; then
    echo "LONGHOUSE_DEVICE_TOKEN is required when SESSION_PROPAGATION_BOOTSTRAP_ENGINE=true" >&2
    echo "| 0 | 3 | setup_error: missing Machine Agent credential | \`$OUTPUT_ROOT\` |" >> "$summary"
    exit 3
  fi
  if [[ "$LONGHOUSE_DEVICE_TOKEN" != zdt_* ]]; then
    echo "LONGHOUSE_DEVICE_TOKEN must be a durable Machine Agent device token (zdt_), not a managed-session token" >&2
    echo "| 0 | 3 | setup_error: invalid Machine Agent credential type | \`$OUTPUT_ROOT\` |" >> "$summary"
    exit 3
  fi
  runtime_url="https://${SUBDOMAIN}.longhouse.ai"
  token_machine_id="$(python3 - "$runtime_url" <<'PY'
import json
import os
import sys
import urllib.request

request = urllib.request.Request(
    f"{sys.argv[1]}/api/agents/storage/v2/capabilities",
    headers={"X-Agents-Token": os.environ["LONGHOUSE_DEVICE_TOKEN"]},
)
with urllib.request.urlopen(request, timeout=15) as response:
    print(json.load(response)["machine_id"])
PY
)"
  LONGHOUSE_DEVICE_TOKEN="$LONGHOUSE_DEVICE_TOKEN" \
    longhouse auth \
      --url "$runtime_url" \
      --device "$token_machine_id" \
      --token-env LONGHOUSE_DEVICE_TOKEN >/dev/null
  longhouse-engine connect \
    --url "$runtime_url" \
    --token "$LONGHOUSE_DEVICE_TOKEN" \
    --machine-name "$token_machine_id" \
    >"$OUTPUT_ROOT/machine-agent.log" 2>&1 &
  engine_pid=$!
  sleep 2
  if ! kill -0 "$engine_pid" 2>/dev/null; then
    echo "Ephemeral Machine Agent failed to start" >&2
    echo "| 0 | 3 | setup_error: Machine Agent failed to start | \`$OUTPUT_ROOT/machine-agent.log\` |" >> "$summary"
    exit 3
  fi
fi

if ! [[ "$ATTEMPTS" =~ ^[0-9]+$ ]] || [[ "$ATTEMPTS" -lt 1 ]]; then
  echo "SESSION_PROPAGATION_ATTEMPTS must be a positive integer" >&2
  exit 1
fi
if ! [[ "$ITERATIONS" =~ ^[0-9]+$ ]] || [[ "$ITERATIONS" -lt 1 ]]; then
  echo "SESSION_PROPAGATION_ITERATIONS must be a positive integer" >&2
  exit 1
fi

classify_attempt() {
  local code="$1"
  local attempt_dir="$2"

  if [[ "$code" == "0" ]]; then
    echo "pass"
    return
  fi
  if [[ "$code" == "2" ]]; then
    echo "contaminated"
    return
  fi
  if [[ "$code" == "3" ]]; then
    echo "setup_error"
    return
  fi

  echo "fail"
}

last_classification="fail"
for attempt in $(seq 1 "$ATTEMPTS"); do
  attempt_run_id="${BASE_RUN_ID}-a$(printf '%02d' "$attempt")"
  attempt_dir="$OUTPUT_ROOT/$attempt_run_id"
  mkdir -p "$attempt_dir"

  cmd=(
    python3 "$PROFILER"
    --profile "$PROFILE"
    --sla-case "$SLA_CASE"
    --provider "$PROVIDER"
    --ownership "$OWNERSHIP"
    --iterations "$ITERATIONS"
    --subdomain "$SUBDOMAIN"
    --project "$PROJECT"
    --ssh-target "$SSH_TARGET"
    --run-id "$attempt_run_id"
    --output-dir "$attempt_dir"
  )
  if [[ "$PROVIDER" == "codex" && "$OWNERSHIP" == "managed" ]]; then
    cmd+=(--trust-longhouse-codex-hooks --codex-effort "$CODEX_EFFORT")
  fi
  if [[ "$PROVIDER_TO_PIXEL_ONLY" == "true" ]]; then
    cmd+=(--provider-to-pixel-only)
  fi
  if [[ -n "$EXPECTED_HOSTED_COMMIT" ]]; then
    cmd+=(--expected-hosted-commit "$EXPECTED_HOSTED_COMMIT")
  fi
  cmd+=("$@")

  printf '%q ' "${cmd[@]}" > "$attempt_dir/command.txt"
  echo >> "$attempt_dir/command.txt"

  set +e
  "${cmd[@]}" >"$attempt_dir/stdout.log" 2>"$attempt_dir/stderr.log"
  code=$?
  set -e

  classification="$(classify_attempt "$code" "$attempt_dir")"
  last_classification="$classification"
  echo "| $attempt | $code | $classification | \`$attempt_dir\` |" >> "$summary"

  if [[ "$classification" == "pass" ]]; then
    echo "" >> "$summary"
    echo "Result: pass" >> "$summary"
    echo "$summary"
    exit 0
  fi

  if [[ "$classification" == "fail" ]]; then
    echo "" >> "$summary"
    echo "Result: fail" >> "$summary"
    echo "$summary"
    exit 1
  fi

  if [[ "$classification" == "setup_error" ]]; then
    echo "" >> "$summary"
    echo "Result: setup_error" >> "$summary"
    echo "$summary"
    exit 3
  fi

  if [[ "$attempt" -lt "$ATTEMPTS" ]]; then
    echo "Attempt $attempt contaminated; retrying after ${RETRY_SLEEP_SECS}s..." >&2
    sleep "$RETRY_SLEEP_SECS"
  fi
done

echo "" >> "$summary"
if [[ "$last_classification" == "contaminated" ]]; then
  echo "Result: contaminated" >> "$summary"
  echo "$summary"
  exit 2
fi

echo "Result: fail" >> "$summary"
echo "$summary"
exit 1
