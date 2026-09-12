#!/usr/bin/env bash
set -euo pipefail

if [[ "${LONGHOUSE_TEST_ISOLATED:-}" != "1" || ! -f /tmp/longhouse-test-isolated ]]; then
  echo "Native harnesses run in a disposable hosted macOS VM. Use make menubar-harness MODE=test." >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PKG_PATH="$ROOT/desktop/LonghouseMenuBarHarness"
XCODE_HARNESS_PATH="$PKG_PATH/XcodeHarness"
RUN_ID="${LONGHOUSE_MENUBAR_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
ARTIFACT_DIR="${LONGHOUSE_MENUBAR_ARTIFACT_DIR:-$ROOT/artifacts/menubar-harness/$RUN_ID}"
BUILD_DIR="$ARTIFACT_DIR/swift-build"
XCODE_PROJECT_DIR="$ARTIFACT_DIR/xcode-project"
mkdir -p "$ARTIFACT_DIR" "$BUILD_DIR" "$XCODE_PROJECT_DIR"
export LONGHOUSE_MENUBAR_ARTIFACT_DIR="$ARTIFACT_DIR"
export LONGHOUSE_MENUBAR_RUN_ID="$RUN_ID"

cmd="${1:-}"
shift || true

usage() {
  local fixture_names
  fixture_names="$(find "$PKG_PATH/Fixtures" -maxdepth 1 -name '*.json' -print | sed 's#.*/##' | sed 's#\\.json$##' | sort | sed 's/^/  /')"
  cat <<'EOF'
Usage:
  scripts/qa/menubar-harness.sh test
  scripts/qa/menubar-harness.sh snapshot-fixture <fixture-name> [output.png]
  scripts/qa/menubar-harness.sh snapshot-live [output.png]
  scripts/qa/menubar-harness.sh raw-snapshot-fixture <fixture-name> [output.png]
  scripts/qa/menubar-harness.sh raw-snapshot-live [output.png]
  scripts/qa/menubar-harness.sh compare-header-variants <fixture-name>
  scripts/qa/menubar-harness.sh render-fixtures
  scripts/qa/menubar-harness.sh render-trust-states
  scripts/qa/menubar-harness.sh smoke [fixture-name]
  scripts/qa/menubar-harness.sh xcuitest
  scripts/qa/menubar-harness.sh full
  scripts/qa/menubar-harness.sh window-fixture <fixture-name>
  scripts/qa/menubar-harness.sh window-live
  scripts/qa/menubar-harness.sh menubar-fixture <fixture-name>
  scripts/qa/menubar-harness.sh menubar-live

Fixtures:
EOF
  printf '%s\n' "$fixture_names"
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
  local name="${1:-}"
  if [[ ! "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ || "$name" == *..* ]]; then
    echo "invalid fixture name: $name" >&2
    return 2
  fi
  local path="$PKG_PATH/Fixtures/${name}.json"
  if [[ ! -f "$path" ]]; then
    echo "unknown fixture: $name" >&2
    return 2
  fi
  printf '%s\n' "$path"
}

raw_snapshot_exec() {
  swift run --package-path "$PKG_PATH" --scratch-path "$BUILD_DIR" LonghouseMenuBarHarnessSnapshot "$@"
}

capture_fixture_render() {
  local input_json="$1"
  local output_png="$2"

  # Capture the actual native material/appearance. The window host freezes
  # fixture labels to collected_at while keeping producer trust on wall time.
  local app_bin
  app_bin="$(build_app_binary)"
  capture_window_render "$app_bin" "$input_json" "$output_png"
}

run_owned_swift_product() (
  local product="$1"
  shift
  local pid=""
  local pgid=""
  local command_status=0

  cleanup_swift_product() {
    if [[ -n "$pid" ]]; then
      stop_owned_process "$pid" "$pgid"
      pid=""
      pgid=""
    fi
  }
  trap cleanup_swift_product EXIT
  trap 'cleanup_swift_product; exit 143' INT TERM

  start_owned_process swift run \
    --package-path "$PKG_PATH" \
    --scratch-path "$BUILD_DIR" \
    "$product" "$@"
  pid="$OWNED_PID"
  pgid="$OWNED_PGID"
  wait "$pid" || command_status=$?
  cleanup_swift_product
  trap - EXIT INT TERM
  return "$command_status"
)

app_exec() {
  run_owned_swift_product LonghouseMenuBarHarnessApp "$@"
}

menubar_exec() {
  run_owned_swift_product LonghouseMenuBarHarnessMenuBar "$@"
}

build_app_binary() {
  local build_log="$ARTIFACT_DIR/swift-build.log"
  if ! swift build \
    --package-path "$PKG_PATH" \
    --scratch-path "$BUILD_DIR" \
    --configuration debug \
    --product LonghouseMenuBarHarnessApp >"$build_log" 2>&1; then
    cat "$build_log" >&2
    return 1
  fi
  local bin_dir
  if ! bin_dir="$(swift build \
    --package-path "$PKG_PATH" \
    --scratch-path "$BUILD_DIR" \
    --configuration debug \
    --show-bin-path 2>>"$build_log")"; then
    cat "$build_log" >&2
    return 1
  fi
  local binary="$bin_dir/LonghouseMenuBarHarnessApp"
  if [[ ! -x "$binary" ]]; then
    echo "built app binary not found: $binary" >&2
    return 1
  fi
  printf '%s\n' "$binary"
}

wait_for_window_id() {
  local owner_pid="$1"
  local window_title="$2"
  local window_id=""
  local attempt
  for attempt in $(seq 1 80); do
    window_id="$(swift - "$owner_pid" "$window_title" <<'SWIFT'
import Foundation
import CoreGraphics

let ownerPID = Int(CommandLine.arguments[1]) ?? -1
let windowTitle = CommandLine.arguments[2]
let infos = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] ?? []
for row in infos {
    let owner = row[kCGWindowOwnerPID as String] as? Int ?? -1
    let name = row[kCGWindowName as String] as? String ?? ""
    if owner == ownerPID && name == windowTitle {
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

  echo "Timed out waiting for window '$window_title' owned by PID '$owner_pid'" >&2
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

SELF_PGID="$(ps -o pgid= -p "$$" | tr -d ' ')"

start_owned_process() {
  local command="$1"
  shift
  python3 - "$command" "$@" <<'PY' &
import os
import sys

os.setsid()
os.execvp(sys.argv[1], sys.argv[1:])
PY
  OWNED_PID=$!
  OWNED_PGID="$(ps -o pgid= -p "$OWNED_PID" | tr -d ' ')"
  if [[ -z "$OWNED_PGID" || "$OWNED_PGID" == "$SELF_PGID" ]]; then
    echo "failed to allocate an owned process group for $command" >&2
    kill "$OWNED_PID" >/dev/null 2>&1 || true
    wait "$OWNED_PID" >/dev/null 2>&1 || true
    return 1
  fi
}

stop_owned_process() {
  local pid="${1:-}"
  local pgid="${2:-}"
  if [[ ! "$pid" =~ ^[0-9]+$ || ! "$pgid" =~ ^[0-9]+$ || "$pgid" == "$SELF_PGID" ]]; then
    return 0
  fi

  local actual_pgid=""
  actual_pgid="$(ps -o pgid= -p "$pid" | tr -d ' ')" || true
  if [[ "$actual_pgid" == "$pgid" ]] || kill -0 -- "-$pgid" >/dev/null 2>&1; then
    kill -TERM -- "-$pgid" >/dev/null 2>&1 || true
    sleep 0.2
    kill -KILL -- "-$pgid" >/dev/null 2>&1 || true
  elif kill -0 "$pid" >/dev/null 2>&1; then
    kill -TERM "$pid" >/dev/null 2>&1 || true
  fi
  wait "$pid" >/dev/null 2>&1 || true
}

capture_window_render() {
  local app_bin="$1"
  local input_json="$2"
  local output_png="$3"

  capture_window_render_args "$app_bin" "$output_png" --input "$input_json"
}

# Same capture, but the caller supplies the app arguments. Trust-state renders
# need a live source pointed at a broken health command, which --input cannot
# express: a fixture always loads, so it can never produce a stale banner.
capture_window_render_args() (
  local app_bin="$1"
  local output_png="$2"
  shift 2
  local pid=""
  local pgid=""
  local capture_status=0
  local window_id=""

  cleanup_capture() {
    if [[ -n "$pid" ]]; then
      stop_owned_process "$pid" "$pgid"
      pid=""
      pgid=""
    fi
  }
  trap cleanup_capture EXIT
  trap 'cleanup_capture; exit 143' INT TERM

  rm -f "$output_png"
  start_owned_process "$app_bin" "$@" --quit-after 30 >/dev/null 2>&1
  pid="$OWNED_PID"
  pgid="$OWNED_PGID"

  if window_id="$(wait_for_window_id "$pid" "Longhouse Desktop")"; then
    :
  else
    capture_status=$?
  fi

  # The window appears in its booting state. Anything that has to be observed
  # after the store settles must wait out SnapshotStore.bootGraceSeconds first.
  if [[ $capture_status -eq 0 && "${CAPTURE_SETTLE_SECONDS:-0}" != "0" ]]; then
    sleep "$CAPTURE_SETTLE_SECONDS"
  fi

  if [[ $capture_status -eq 0 ]]; then
    if screencapture -x -l "$window_id" "$output_png"; then
      :
    else
      capture_status=$?
    fi
  fi

  if [[ $capture_status -eq 0 ]]; then
    if verify_png_has_visible_content "$output_png"; then
      :
    else
      capture_status=$?
    fi
  fi

  cleanup_capture
  trap - EXIT INT TERM
  if [[ $capture_status -ne 0 ]]; then
    return "$capture_status"
  fi

  echo "$output_png"
)

xcode_ui_exec() (
  local project_path="$XCODE_PROJECT_DIR/LonghouseMenuBarHarnessXcode.xcodeproj"
  local result_bundle="$ARTIFACT_DIR/LonghouseMenuBarWindowHost.xcresult"
  local log_path="$ARTIFACT_DIR/xcuitest.log"
  local pid=""
  local pgid=""
  local command_status=0
  require_tool xcodegen
  require_tool xcodebuild
  remove_path "$result_bundle"
  xcodegen \
    --spec "$XCODE_HARNESS_PATH/project.yml" \
    --project-root "$XCODE_HARNESS_PATH" \
    --project "$XCODE_PROJECT_DIR" \
    >/dev/null

  cleanup_xcode() {
    if [[ -n "$pid" ]]; then
      stop_owned_process "$pid" "$pgid"
      pid=""
      pgid=""
    fi
  }
  trap cleanup_xcode EXIT
  trap 'cleanup_xcode; exit 143' INT TERM

  start_owned_process xcodebuild \
    -project "$project_path" \
    -scheme LonghouseMenuBarWindowHost \
    -destination 'platform=macOS' \
    -derivedDataPath "$ARTIFACT_DIR/derived-data" \
    -resultBundlePath "$result_bundle" \
    test >"$log_path" 2>&1
  pid="$OWNED_PID"
  pgid="$OWNED_PGID"
  wait "$pid" || command_status=$?
  cat "$log_path"
  cleanup_xcode
  trap - EXIT INT TERM
  return "$command_status"
)

SMOKE_ACTIONS="refresh,runDoctor,repairInstall,inspectStorageSource,openLogs,openLonghouse,copyDiagnostics"

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
    "inspectStorageSource",
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


write_manifest() {
  python3 - "$ARTIFACT_DIR" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

artifact_dir = Path(sys.argv[1])
pngs = sorted(str(path) for path in artifact_dir.glob("*.png"))
manifest = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "artifacts": {
        "window_smoke_log": str(artifact_dir / "window-smoke-actions.jsonl"),
        "menubar_smoke_log": str(artifact_dir / "menubar-smoke-actions.jsonl"),
        "xcuitest_log": str(artifact_dir / "xcuitest.log"),
        "xcuitest_result_bundle": str(artifact_dir / "LonghouseMenuBarWindowHost.xcresult"),
    },
    "png_snapshots": pngs,
}
path = artifact_dir / "manifest.json"
path.write_text(json.dumps(manifest, indent=2) + "\n")
print(path)
PY
}

case "$cmd" in
  test)
    swift test --package-path "$PKG_PATH" --scratch-path "$BUILD_DIR"
    ;;
  snapshot-fixture)
    fixture="${1:-}"
    if [[ -z "$fixture" ]]; then
      usage
      exit 2
    fi
    fixture_file="$(fixture_path "$fixture")"
    output="${2:-$ARTIFACT_DIR/${fixture}.png}"
    capture_fixture_render "$fixture_file" "$output"
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
    fixture_file="$(fixture_path "$fixture")"
    output="${2:-$ARTIFACT_DIR/${fixture}.png}"
    raw_snapshot_exec --input "$fixture_file" --output "$output"
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
  compare-header-variants)
    fixture="${1:-}"
    if [[ -z "$fixture" ]]; then
      usage
      exit 2
    fi
    fixture_file="$(fixture_path "$fixture")"
    for variant in minimal telemetry-rail session-ribbon; do
      output="$ARTIFACT_DIR/${fixture}-${variant}.png"
      raw_snapshot_exec --input "$fixture_file" --output "$output" --header-variant "$variant"
      echo "$output"
    done
    ;;
  render-fixtures)
    while IFS= read -r fixture_json; do
      fixture_name="$(basename "$fixture_json" .json)"
      capture_fixture_render "$fixture_json" "$ARTIFACT_DIR/${fixture_name}.png"
    done < <(find "$PKG_PATH/Fixtures" -maxdepth 1 -name '*.json' -print | sort)
    ;;
  render-trust-states)
    # The banner the panel shows when it cannot read this Mac. Driven by a real
    # failing producer rather than a fixture, because trust lives in the store:
    # a fixture always loads and can never produce a stale banner. The store
    # renders its last-good cache underneath, which is the incident shape --
    # real-looking content the app must refuse to call current.
    app_bin="$(build_app_binary)"
    output="$ARTIFACT_DIR/trust-never-loaded.png"
    # No --live: an explicit live flag suppresses the status window, and this
    # capture needs the window on screen.
    CAPTURE_SETTLE_SECONDS=14 capture_window_render_args "$app_bin" "$output" \
      --health-exec "$ARTIFACT_DIR/nonexistent-local-health" \
      --health-arg --json
    ;;
  smoke)
    fixture="${1:-healthy}"
    run_smoke_shell "window" "$fixture" "$ARTIFACT_DIR/window-smoke-actions.jsonl" app_exec
    run_smoke_shell "menubar" "$fixture" "$ARTIFACT_DIR/menubar-smoke-actions.jsonl" menubar_exec
    ;;
  xcuitest)
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
    fixture_file="$(fixture_path "$fixture")"
    app_exec --input "$fixture_file" --action-log "$ARTIFACT_DIR/actions.jsonl"
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
    fixture_file="$(fixture_path "$fixture")"
    menubar_exec --input "$fixture_file" --action-log "$ARTIFACT_DIR/actions.jsonl"
    ;;
  menubar-live)
    menubar_exec --live --refresh-seconds 10 --action-log "$ARTIFACT_DIR/actions.jsonl"
    ;;
  *)
    usage
    exit 2
    ;;
esac
