#!/usr/bin/env bash
set -euo pipefail

workflow="ci-test.yml"
workflow_ref="${WORKFLOW_REF:-main}"

usage() {
  cat <<'USAGE'
Usage:
  scripts/ci/run-on-ci.sh <suite> [ref] [--test <path>] [--no-watch]

Examples:
  scripts/ci/run-on-ci.sh unit main
  scripts/ci/run-on-ci.sh e2e-core HEAD
  scripts/ci/run-on-ci.sh e2e-single main --test tests/core/sessions.spec.ts
USAGE
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

suite="${1:-}"
shift || true

if [ -z "${suite}" ]; then
  usage
  exit 2
fi

ref="main"
if [ $# -gt 0 ] && [ "${1#--}" = "${1}" ]; then
  ref="$1"
  shift
fi

test_path=""
watch="true"

while [ $# -gt 0 ]; do
  case "$1" in
    --test)
      test_path="${2:-}"
      shift 2
      ;;
    --no-watch)
      watch="false"
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if ! command -v gh >/dev/null 2>&1; then
  echo "gh CLI is required" >&2
  exit 2
fi

if [ "$suite" = "e2e-single" ] && [ -z "$test_path" ]; then
  echo "--test is required for e2e-single" >&2
  exit 2
fi

if [ -n "$test_path" ] && ! echo "$test_path" | grep -Eq '^[A-Za-z0-9_./-]+$'; then
  echo "--test has invalid characters" >&2
  exit 2
fi

args=("-f" "ref=$ref" "-f" "suite=$suite")
if [ -n "$test_path" ]; then
  args+=("-f" "test=$test_path")
fi

echo "Triggering CI: suite=$suite ref=$ref"

gh workflow run "$workflow" --ref "$workflow_ref" "${args[@]}" >/dev/null

run_id=""
run_url=""

for _ in $(seq 1 30); do
  info=$(gh run list --workflow "$workflow" --limit 20 \
    --json databaseId,headBranch,headSha,createdAt,url,event \
    | python3 -c "
import json, re, sys
ref = sys.argv[1]
is_sha = bool(re.fullmatch(r'[0-9a-fA-F]{7,40}', ref))
data = json.load(sys.stdin)
candidates = []
for run in data:
    if run.get('event') != 'workflow_dispatch':
        continue
    if is_sha:
        if run.get('headSha', '').startswith(ref):
            candidates.append(run)
    else:
        if run.get('headBranch') == ref:
            candidates.append(run)
candidates.sort(key=lambda r: r.get('createdAt', ''), reverse=True)
if candidates:
    r = candidates[0]
    print(str(r.get('databaseId', '')) + '|' + r.get('url', ''))
" "$ref"
  )

  if [ -n "$info" ]; then
    run_id="${info%%|*}"
    run_url="${info#*|}"
    break
  fi
  sleep 2
done

if [ -z "$run_id" ]; then
  echo "Failed to locate workflow run" >&2
  exit 2
fi

echo "Run: $run_url"

if [ "$watch" = "false" ]; then
  exit 0
fi

last_status=""
while true; do
  status=$(gh run view "$run_id" --json status,conclusion \
    | python3 -c "
import json, sys
j = json.load(sys.stdin)
s = j.get('status') or ''
c = j.get('conclusion') or ''
print(s + '|' + c)
")

  current_status="${status%%|*}"
  current_conclusion="${status#*|}"

  if [ "$current_status" != "$last_status" ]; then
    echo "Status: $current_status"
    last_status="$current_status"
  fi

  if [ -n "$current_conclusion" ]; then
    echo "Conclusion: $current_conclusion"
    if [ "$current_conclusion" != "success" ]; then
      echo "See logs: gh run view $run_id --log-failed"
      exit 1
    fi
    exit 0
  fi

  sleep 10
done
