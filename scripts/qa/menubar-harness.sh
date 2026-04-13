#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PKG_PATH="$ROOT/desktop/LonghouseMenuBarHarness"
XCODE_HARNESS_PATH="$PKG_PATH/XcodeHarness"
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
  scripts/qa/menubar-harness.sh raw-snapshot-fixture <fixture-name> [output.png]
  scripts/qa/menubar-harness.sh raw-snapshot-live [output.png]
  scripts/qa/menubar-harness.sh render-fixtures
  scripts/qa/menubar-harness.sh smoke [fixture-name]
  scripts/qa/menubar-harness.sh xcuitest
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

require_tool() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    echo "missing required tool: $name" >&2
    exit 2
  fi
}

remove_path() {
  local path="$1"
  python3 - "$path" <<'PY'
import os
import shutil
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(0)

def onerror(func, target, _exc_info):
    try:
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    except OSError:
        pass
    func(target)

if path.is_dir() and not path.is_symlink():
    shutil.rmtree(path, onerror=onerror)
else:
    path.unlink()
PY
}

fixture_path() {
  local name="$1"
  echo "$PKG_PATH/Fixtures/${name}.json"
}

raw_snapshot_exec() {
  swift run --package-path "$PKG_PATH" LonghouseMenuBarHarnessSnapshot "$@"
}

app_exec() {
  swift run --package-path "$PKG_PATH" LonghouseMenuBarHarnessApp "$@"
}

menubar_exec() {
  swift run --package-path "$PKG_PATH" LonghouseMenuBarHarnessMenuBar "$@"
}

build_app_binary() {
  swift build --package-path "$PKG_PATH" --product LonghouseMenuBarHarnessApp >/dev/null
  echo "$(swift build --package-path "$PKG_PATH" --show-bin-path)/LonghouseMenuBarHarnessApp"
}

wait_for_window_id() {
  local owner_name="$1"
  local window_title="$2"
  local window_id=""
  local attempt
  for attempt in $(seq 1 80); do
    window_id="$(swift - "$owner_name" "$window_title" <<'SWIFT'
import Foundation
import CoreGraphics

let ownerName = CommandLine.arguments[1]
let windowTitle = CommandLine.arguments[2]
let infos = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] ?? []
for row in infos {
    let owner = row[kCGWindowOwnerName as String] as? String ?? ""
    let name = row[kCGWindowName as String] as? String ?? ""
    if owner == ownerName && name == windowTitle {
        print(row[kCGWindowNumber as String] ?? 0)
        break
    }
}
SWIFT
)"
    if [[ -n "$window_id" ]]; then
      echo "$window_id"
      return 0
    fi
    sleep 0.1
  done

  echo "Timed out waiting for window '$window_title' owned by '$owner_name'" >&2
  return 1
}

verify_png_has_visible_content() {
  local png_path="$1"
  swift - "$png_path" <<'SWIFT'
import AppKit
import Foundation

let pngPath = CommandLine.arguments[1]
let thresholdPercent = 5.0

guard let data = try? Data(contentsOf: URL(fileURLWithPath: pngPath)),
      let rep = NSBitmapImageRep(data: data) else {
    fputs("Failed to load PNG for validation: \(pngPath)\n", stderr)
    exit(1)
}

let width = rep.pixelsWide
let height = rep.pixelsHigh
guard width > 0, height > 0 else {
    fputs("Invalid PNG dimensions for validation: \(pngPath)\n", stderr)
    exit(1)
}

var darkPixels = 0
for y in 0..<height {
    for x in 0..<width {
        guard let color = rep.colorAt(x: x, y: y) else {
            continue
        }
        let rgb = color.usingColorSpace(.deviceRGB) ?? color
        let alpha = rgb.alphaComponent
        let red = rgb.redComponent * 255.0
        let green = rgb.greenComponent * 255.0
        let blue = rgb.blueComponent * 255.0
        if alpha > 0.01 && (red < 220.0 || green < 220.0 || blue < 220.0) {
            darkPixels += 1
        }
    }
}

let totalPixels = Double(width * height)
let darkPercent = (Double(darkPixels) / totalPixels) * 100.0
if darkPercent < thresholdPercent {
    fputs(
        String(
            format: "PNG appears blank or near-blank (dark pixel rate %.2f%% < %.2f%%): %@\n",
            darkPercent,
            thresholdPercent,
            pngPath
        ),
        stderr
    )
    exit(1)
}
SWIFT
}

capture_window_render() {
  local app_bin="$1"
  local input_json="$2"
  local output_png="$3"
  local pid=""
  local capture_status=0
  local window_id=""

  rm -f "$output_png"
  "$app_bin" --input "$input_json" --quit-after 30 >/dev/null 2>&1 &
  pid=$!

  if ! window_id="$(wait_for_window_id "LonghouseMenuBarHarnessApp" "Longhouse Desktop")"; then
    capture_status=$?
  fi

  if [[ $capture_status -eq 0 ]]; then
    if ! screencapture -x -l "$window_id" "$output_png"; then
      capture_status=$?
    fi
  fi

  if [[ $capture_status -eq 0 ]]; then
    if ! verify_png_has_visible_content "$output_png"; then
      capture_status=$?
    fi
  fi

  if [[ -n "$pid" ]]; then
    kill "$pid" >/dev/null 2>&1 || true
    wait "$pid" >/dev/null 2>&1 || true
  fi

  if [[ $capture_status -ne 0 ]]; then
    return "$capture_status"
  fi

  echo "$output_png"
}

xcode_ui_exec() {
  local project_path="$XCODE_HARNESS_PATH/LonghouseMenuBarHarnessXcode.xcodeproj"
  local result_bundle="$ARTIFACT_DIR/LonghouseMenuBarWindowHost.xcresult"
  local log_path="$ARTIFACT_DIR/xcuitest.log"
  require_tool xcodegen
  require_tool xcodebuild
  remove_path "$result_bundle"
  xcodegen --spec "$XCODE_HARNESS_PATH/project.yml" --project-root "$XCODE_HARNESS_PATH" >/dev/null
  xcodebuild \
    -project "$project_path" \
    -scheme LonghouseMenuBarWindowHost \
    -destination 'platform=macOS' \
    -resultBundlePath "$result_bundle" \
    test | tee "$log_path"
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
  set +e
  "$@" \
    --input "$(fixture_path "$fixture")" \
    --action-log "$log_path" \
    --effect-mode log-only \
    --exercise-actions "$SMOKE_ACTIONS" \
    --quit-after 1.5
  local command_status=$?
  set -e

  # SwiftUI harness shells can exit via SIGTERM when NSApplication terminates itself.
  if [[ $command_status -ne 0 && $command_status -ne 143 ]]; then
    return "$command_status"
  fi

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
        "xcuitest_log": str(artifact_dir / "xcuitest.log"),
        "xcuitest_result_bundle": str(artifact_dir / "LonghouseMenuBarWindowHost.xcresult"),
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
    app_bin="$(build_app_binary)"
    capture_window_render "$app_bin" "$(fixture_path "$fixture")" "$output"
    ;;
  snapshot-live)
    output="${1:-$ARTIFACT_DIR/live.png}"
    tmp_json="$(mktemp "${TMPDIR:-/tmp}/lh-menubar-live.XXXXXX.json")"
    trap 'rm -f "$tmp_json"' EXIT
    (cd "$ROOT" && uv run --project server longhouse local-health --json > "$tmp_json")
    app_bin="$(build_app_binary)"
    capture_window_render "$app_bin" "$tmp_json" "$output"
    ;;
  raw-snapshot-fixture)
    fixture="${1:-}"
    if [[ -z "$fixture" ]]; then
      usage
      exit 2
    fi
    output="${2:-$ARTIFACT_DIR/${fixture}.png}"
    raw_snapshot_exec --input "$(fixture_path "$fixture")" --output "$output"
    echo "$output"
    ;;
  raw-snapshot-live)
    output="${1:-$ARTIFACT_DIR/live.png}"
    tmp_json="$(mktemp "${TMPDIR:-/tmp}/lh-menubar-live.XXXXXX.json")"
    trap 'rm -f "$tmp_json"' EXIT
    (cd "$ROOT" && uv run --project server longhouse local-health --json > "$tmp_json")
    raw_snapshot_exec --input "$tmp_json" --output "$output"
    echo "$output"
    ;;
  render-fixtures)
    app_bin="$(build_app_binary)"
    capture_window_render "$app_bin" "$(fixture_path healthy)" "$ARTIFACT_DIR/healthy.png"
    capture_window_render "$app_bin" "$(fixture_path degraded)" "$ARTIFACT_DIR/degraded.png"
    capture_window_render "$app_bin" "$(fixture_path broken)" "$ARTIFACT_DIR/broken.png"
    ;;
  smoke)
    fixture="${1:-healthy}"
    cleanup_harness_processes
    run_smoke_shell "window" "$fixture" "$ARTIFACT_DIR/window-smoke-actions.jsonl" app_exec
    run_smoke_shell "menubar" "$fixture" "$ARTIFACT_DIR/menubar-smoke-actions.jsonl" menubar_exec
    ;;
  xcuitest)
    cleanup_harness_processes
    xcode_ui_exec
    ;;
  full)
    "$0" test
    "$0" render-fixtures
    "$0" snapshot-live "$ARTIFACT_DIR/live.png"
    "$0" smoke healthy
    "$0" xcuitest
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
