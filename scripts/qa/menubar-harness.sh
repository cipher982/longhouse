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
