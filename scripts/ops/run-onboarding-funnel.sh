#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
README_PATH="$ROOT_DIR/README.md"
WORKDIR_OVERRIDE=""
RUN_SHELL="${SHELL:-/bin/bash}"

if [[ ! -x "$RUN_SHELL" ]]; then
  RUN_SHELL="/bin/bash"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workdir)
      WORKDIR_OVERRIDE="${2:-}"
      shift 2
      ;;
    --readme)
      README_PATH="${2:-}"
      shift 2
      ;;
    *)
      README_PATH="$1"
      shift
      ;;
  esac
done

extract_contract() {
  python3 - "$README_PATH" <<'PY'
import json
import re
import sys
from pathlib import Path

readme_path = Path(sys.argv[1])
content = readme_path.read_text(encoding="utf-8")
pattern = r"<!-- onboarding-contract:start -->\s*```json\s*(.*?)\s*```\s*<!-- onboarding-contract:end -->"
match = re.search(pattern, content, re.DOTALL)
if not match:
    sys.exit("onboarding contract block not found in README")
raw = match.group(1).strip()
try:
    data = json.loads(raw)
except json.JSONDecodeError as exc:
    sys.exit(f"onboarding contract JSON invalid: {exc}")
print(json.dumps(data))
PY
}

contract_json="$(extract_contract)"
contract_file="$(mktemp)"
printf "%s" "$contract_json" > "$contract_file"

get_field() {
  python3 - "$contract_file" "$1" <<'PY'
import json
import sys

path = sys.argv[2].split(".")
data = json.load(open(sys.argv[1]))
cur = data
for key in path:
    if not isinstance(cur, dict) or key not in cur:
        cur = None
        break
    cur = cur[key]
if isinstance(cur, (dict, list)):
    print(json.dumps(cur))
elif cur is None:
    print("")
else:
    print(cur)
PY
}

CONTRACT_WORKDIR="$(get_field workdir)"
WORKDIR="${WORKDIR_OVERRIDE:-$CONTRACT_WORKDIR}"

if [[ -z "$WORKDIR" ]]; then
  WORKDIR="/tmp/zerg-onboarding-funnel"
fi

if [[ -z "$WORKDIR_OVERRIDE" ]]; then
  echo "📦 Preparing workspace at $WORKDIR"
  if [[ -d "$WORKDIR" ]]; then
    if ! rm -rf "$WORKDIR" 2>/dev/null; then
      true
    fi
  fi
  if [[ -d "$WORKDIR" ]]; then
    fallback="${WORKDIR}-$(date +%s)"
    echo "⚠️  Could not clean $WORKDIR; using $fallback instead."
    WORKDIR="$fallback"
  fi
  git clone "$ROOT_DIR" "$WORKDIR" >/dev/null
else
  echo "📦 Using existing workspace at $WORKDIR"
fi

cd "$WORKDIR"

steps_json="$(get_field steps)"
cleanup_json="$(get_field cleanup)"

if [[ -z "$steps_json" || "$steps_json" == "null" ]]; then
  echo "❌ onboarding contract missing steps"
  exit 1
fi

run_steps() {
  python3 - "$contract_file" "$1" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1]))
steps = data.get(sys.argv[2], [])
if not isinstance(steps, list):
    sys.exit(f"{sys.argv[2]} must be a list")
for step in steps:
    if not isinstance(step, str):
        sys.exit(f"{sys.argv[2]} entries must be strings")
    print(step)
PY
}

cleanup_cmds=()
if [[ -n "$cleanup_json" && "$cleanup_json" != "null" ]]; then
  while IFS= read -r line; do
    cleanup_cmds+=("$line")
  done < <(run_steps cleanup)
fi

DIAGNOSTICS_DIR="$WORKDIR/onboarding-diagnostics"

collect_diagnostics() {
  mkdir -p "$DIAGNOSTICS_DIR" 2>/dev/null || true
  if [[ -f "$WORKDIR/.qa-home/.longhouse/server.log" ]]; then
    cp "$WORKDIR/.qa-home/.longhouse/server.log" "$DIAGNOSTICS_DIR/server.log" 2>/dev/null || true
    echo "🧾 Server log tail:"
    tail -80 "$WORKDIR/.qa-home/.longhouse/server.log" || true
  fi
  if [[ -d "$WORKDIR/.qa-home/.longhouse" ]]; then
    find "$WORKDIR/.qa-home/.longhouse" -maxdepth 1 -type f -print > "$DIAGNOSTICS_DIR/longhouse-files.txt" 2>/dev/null || true
  fi
}

cleanup() {
  if [[ ${#cleanup_cmds[@]} -eq 0 ]]; then
    return
  fi
  echo "🧹 Running cleanup steps..."
  for cmd in "${cleanup_cmds[@]}"; do
    resolved="${cmd//\{\{WORKDIR\}\}/$WORKDIR}"
    echo "→ $resolved"
    "$RUN_SHELL" -lc "$resolved"
  done
}

on_exit() {
  status=$?
  if [[ "$status" -ne 0 ]]; then
    collect_diagnostics
  fi
  cleanup
  exit "$status"
}

wait_for_onboarding_health() {
  python3 - <<'PY'
import json
import time
import urllib.error
import urllib.request

url = "http://127.0.0.1:8080/api/health"
last_error = None
for attempt in range(1, 61):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.load(response)
        if payload.get("status") == "healthy":
            print(f"✓ onboarding runtime is healthy (attempt {attempt})")
            raise SystemExit(0)
        last_error = f"status payload was not healthy: {payload!r}"
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        last_error = f"{type(exc).__name__}: {exc}"
    print(f"  onboarding runtime not ready (attempt {attempt}/60): {last_error}")
    time.sleep(2)

raise SystemExit(f"onboarding runtime did not become healthy: {last_error}")
PY
}

trap on_exit EXIT

echo "🚦 Running onboarding funnel steps..."
while IFS= read -r cmd; do
  resolved="${cmd//\{\{WORKDIR\}\}/$WORKDIR}"
  echo "→ $resolved"
  if [[ "$resolved" == *"http://127.0.0.1:8080/api/health"* ]]; then
    wait_for_onboarding_health
  else
    "$RUN_SHELL" -lc "$resolved"
  fi
done < <(run_steps steps)

echo "✅ Onboarding funnel complete."
