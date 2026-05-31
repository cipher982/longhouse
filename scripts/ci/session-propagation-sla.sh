#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

ATTEMPTS="${SESSION_PROPAGATION_ATTEMPTS:-3}"
SUBDOMAIN="${SESSION_PROPAGATION_SUBDOMAIN:-${LONGHOUSE_DEFAULT_SUBDOMAIN:-demo}}"
PROJECT="${SESSION_PROPAGATION_PROJECT:-zerg}"
SLA_CASE="${SESSION_PROPAGATION_SLA_CASE:-managed_codex_warm_live_graceful_close}"
PROFILE="${SESSION_PROPAGATION_PROFILE:-warm-live}"
PROVIDER="${SESSION_PROPAGATION_PROVIDER:-codex}"
OWNERSHIP="${SESSION_PROPAGATION_OWNERSHIP:-managed}"
CODEX_EFFORT="${SESSION_PROPAGATION_CODEX_EFFORT:-low}"
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
  echo "- Started: \`$(date -u +%Y-%m-%dT%H:%M:%SZ)\`"
  echo ""
  echo "## Attempts"
  echo ""
  echo "| Attempt | Exit | Classification | Artifact |"
  echo "| ---: | ---: | --- | --- |"
} > "$summary"

missing=0
for required_cmd in python3 bun longhouse longhouse-engine codex; do
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

if ! [[ "$ATTEMPTS" =~ ^[0-9]+$ ]] || [[ "$ATTEMPTS" -lt 1 ]]; then
  echo "SESSION_PROPAGATION_ATTEMPTS must be a positive integer" >&2
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
    --subdomain "$SUBDOMAIN"
    --project "$PROJECT"
    --run-id "$attempt_run_id"
    --output-dir "$attempt_dir"
  )
  if [[ "$PROVIDER" == "codex" && "$OWNERSHIP" == "managed" ]]; then
    cmd+=(--trust-longhouse-codex-hooks --codex-effort "$CODEX_EFFORT")
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
