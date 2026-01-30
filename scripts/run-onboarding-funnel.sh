#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
README_PATH="${1:-$ROOT_DIR/README.md}"

extract_contract() {
  python3 - "$README_PATH" <<'PY'
import json
import re
import sys
from pathlib import Path

readme_path = Path(sys.argv[1])
content = readme_path.read_text(encoding="utf-8")
pattern = r"<!-- onboarding-contract:start -->\\s*```json\\s*(.*?)\\s*```\\s*<!-- onboarding-contract:end -->"
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

WORKDIR_OVERRIDE=""
if [[ "${2:-}" == "--workdir" && -n "${3:-}" ]]; then
  WORKDIR_OVERRIDE="$3"
fi

CONTRACT_WORKDIR="$(get_field workdir)"
WORKDIR="${WORKDIR_OVERRIDE:-$CONTRACT_WORKDIR}"

if [[ -z "$WORKDIR" ]]; then
  WORKDIR="/tmp/zerg-onboarding-funnel"
fi

if [[ -z "$WORKDIR_OVERRIDE" ]]; then
  echo "📦 Preparing sandbox at $WORKDIR"
  rm -rf "$WORKDIR"
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

cleanup() {
  if [[ ${#cleanup_cmds[@]} -eq 0 ]]; then
    return
  fi
  echo "🧹 Running cleanup steps..."
  for cmd in "${cleanup_cmds[@]}"; do
    echo "→ $cmd"
    bash -lc "$cmd"
  done
}

trap cleanup EXIT

echo "🚦 Running onboarding funnel steps..."
while IFS= read -r cmd; do
  echo "→ $cmd"
  bash -lc "$cmd"
done < <(run_steps steps)

echo "✅ Onboarding funnel complete."
