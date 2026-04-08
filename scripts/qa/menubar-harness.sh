#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PKG_PATH="$ROOT/desktop/LonghouseMenuBarHarness"
ARTIFACT_DIR="$ROOT/artifacts/menubar-harness"

cmd="${1:-}"
shift || true

mkdir -p "$ARTIFACT_DIR"

usage() {
  cat <<'EOF'
Usage:
  scripts/qa/menubar-harness.sh test
  scripts/qa/menubar-harness.sh snapshot-fixture <fixture-name> [output.png]
  scripts/qa/menubar-harness.sh snapshot-live [output.png]
  scripts/qa/menubar-harness.sh render-fixtures
  scripts/qa/menubar-harness.sh smoke [fixture-name]
  scripts/qa/menubar-harness.sh full
  scripts/qa/menubar-harness.sh window-fixture <fixture-name>
  scripts/qa/menubar-harness.sh window-live
  scripts/qa/menubar-harness.sh menubar-fixture <fixture-name>
  scripts/qa/menubar-harness.sh menubar-live

Fixtures:
  healthy
  degraded
  broken
EOF
}

fixture_path() {
  local name="$1"
  echo "$PKG_PATH/Fixtures/${name}.json"
}

snapshot_exec() {
  swift run --package-path "$PKG_PATH" LonghouseMenuBarHarnessSnapshot "$@"
}

app_exec() {
  swift run --package-path "$PKG_PATH" LonghouseMenuBarHarnessApp "$@"
}

menubar_exec() {
  swift run --package-path "$PKG_PATH" LonghouseMenuBarHarnessMenuBar "$@"
}

SMOKE_ACTIONS="refresh,runDoctor,repairInstall,openLogs,openLonghouse,copyDiagnostics"

verify_action_log() {
  local log_path="$1"
  local label="$2"
  python3 - "$log_path" "$label" <<'PY'
import json
import sys
from pathlib import Path

log_path = Path(sys.argv[1])
label = sys.argv[2]
expected = {
    "refresh",
    "runDoctor",
    "repairInstall",
    "openLogs",
    "openLonghouse",
    "copyDiagnostics",
}

if not log_path.exists():
    raise SystemExit(f"{label}: missing action log at {log_path}")

seen = set()
for line in log_path.read_text().splitlines():
    if not line.strip():
        continue
    seen.add(json.loads(line)["action"])

missing = sorted(expected - seen)
if missing:
    raise SystemExit(f"{label}: missing actions {', '.join(missing)}")

print(f"{label}: ok ({len(seen)} actions)")
PY
}

run_smoke_shell() {
  local label="$1"
  local fixture="$2"
  local log_path="$3"
  shift 3
  rm -f "$log_path"
  "$@" \
    --input "$(fixture_path "$fixture")" \
    --action-log "$log_path" \
    --effect-mode log-only \
    --exercise-actions "$SMOKE_ACTIONS" \
    --quit-after 1.5
  verify_action_log "$log_path" "$label"
}

cleanup_harness_processes() {
  pkill -f 'LonghouseMenuBarHarness(App|MenuBar)' >/dev/null 2>&1 || true
}

write_manifest() {
  python3 - "$ARTIFACT_DIR" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

artifact_dir = Path(sys.argv[1])
manifest = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "artifacts": {
        "healthy_png": str(artifact_dir / "healthy.png"),
        "degraded_png": str(artifact_dir / "degraded.png"),
        "broken_png": str(artifact_dir / "broken.png"),
        "live_png": str(artifact_dir / "live.png"),
        "window_smoke_log": str(artifact_dir / "window-smoke-actions.jsonl"),
        "menubar_smoke_log": str(artifact_dir / "menubar-smoke-actions.jsonl"),
    },
}
path = artifact_dir / "manifest.json"
path.write_text(json.dumps(manifest, indent=2) + "\n")
print(path)
PY
}

case "$cmd" in
  test)
    swift test --package-path "$PKG_PATH"
    ;;
  snapshot-fixture)
    fixture="${1:-}"
    if [[ -z "$fixture" ]]; then
      usage
      exit 2
    fi
    output="${2:-$ARTIFACT_DIR/${fixture}.png}"
    snapshot_exec --input "$(fixture_path "$fixture")" --output "$output"
    echo "$output"
    ;;
  snapshot-live)
    output="${1:-$ARTIFACT_DIR/live.png}"
    tmp_json="$(mktemp "${TMPDIR:-/tmp}/lh-menubar-live.XXXXXX.json")"
    trap 'rm -f "$tmp_json"' EXIT
    (cd "$ROOT" && uv run --project server longhouse local-health --json > "$tmp_json")
    snapshot_exec --input "$tmp_json" --output "$output"
    echo "$output"
    ;;
  render-fixtures)
    "$0" snapshot-fixture healthy "$ARTIFACT_DIR/healthy.png"
    "$0" snapshot-fixture degraded "$ARTIFACT_DIR/degraded.png"
    "$0" snapshot-fixture broken "$ARTIFACT_DIR/broken.png"
    ;;
  smoke)
    fixture="${1:-healthy}"
    cleanup_harness_processes
    run_smoke_shell "window" "$fixture" "$ARTIFACT_DIR/window-smoke-actions.jsonl" app_exec
    run_smoke_shell "menubar" "$fixture" "$ARTIFACT_DIR/menubar-smoke-actions.jsonl" menubar_exec
    ;;
  full)
    "$0" test
    "$0" render-fixtures
    "$0" snapshot-live "$ARTIFACT_DIR/live.png"
    "$0" smoke healthy
    write_manifest
    ;;
  window-fixture)
    fixture="${1:-}"
    if [[ -z "$fixture" ]]; then
      usage
      exit 2
    fi
    app_exec --input "$(fixture_path "$fixture")" --action-log "$ARTIFACT_DIR/actions.jsonl"
    ;;
  window-live)
    app_exec --live --refresh-seconds 10 --action-log "$ARTIFACT_DIR/actions.jsonl"
    ;;
  menubar-fixture)
    fixture="${1:-}"
    if [[ -z "$fixture" ]]; then
      usage
      exit 2
    fi
    menubar_exec --input "$(fixture_path "$fixture")" --action-log "$ARTIFACT_DIR/actions.jsonl"
    ;;
  menubar-live)
    menubar_exec --live --refresh-seconds 10 --action-log "$ARTIFACT_DIR/actions.jsonl"
    ;;
  *)
    usage
    exit 2
    ;;
esac
