#!/usr/bin/env bash
# Terminal-side lane for the iOS Simulator: the same verbs as scripts/ops/phone.sh,
# with no phone in the loop and the app's own OSLog streamed live (no sudo
# needed on a simulator). A Debug launch signs in headlessly from environment.
#
#   scripts/ops/sim.sh boot                        boot the simulator CI uses
#   scripts/ops/sim.sh build                       Debug build for the simulator
#   scripts/ops/sim.sh install                     install the last build
#   scripts/ops/sim.sh launch [<session-id>]       (re)launch signed in, optionally on a session
#   scripts/ops/sim.sh deploy [<session-id>]       boot, build, install, launch
#   scripts/ops/sim.sh url <url>                   open a deep link in the running app
#   scripts/ops/sim.sh shot [label]                PNG into artifacts/sim/
#   scripts/ops/sim.sh record <seconds> [label]    MP4 into artifacts/sim/
#   scripts/ops/sim.sh logs [--follow] [--since 5m] [--all]
#                                                  the app's OSLog (ai.longhouse.ios)
#   scripts/ops/sim.sh shutdown                    shut the simulator down
#
# Environment:
#   SIM_SERVER_URL      server the app signs into (default: this machine's runtime_url)
#   SIM_AUTH_TOKEN      runtime token for SIM_SERVER_URL (default: this machine's device token;
#                       any non-empty value works against a server running with AUTH_DISABLED=1)
#   SIM_UDID            simulator to use (default: the one scripts/ci/select_ios_simulator.py picks)
#   SIM_OUT_DIR         capture directory (default: artifacts/sim)
#   SIM_DERIVED_DATA    xcodebuild derived data path
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="${SIM_OUT_DIR:-$ROOT_DIR/artifacts/sim}"
BUNDLE_ID="ai.longhouse.ios"
PROJECT="$ROOT_DIR/ios/XcodeHarness/LonghouseIOS.xcodeproj"
DERIVED="${SIM_DERIVED_DATA:-$HOME/Library/Developer/Xcode/DerivedData/LonghouseIOS-Sim}"
LOG_PREDICATE='subsystem == "ai.longhouse.ios"'

usage() {
  sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

die() {
  echo "sim: $*" >&2
  exit 1
}

stamp() {
  date -u +%Y%m%dT%H%M%SZ
}

resolve_udid() {
  if [[ -n "${SIM_UDID:-}" ]]; then
    printf '%s\n' "$SIM_UDID"
    return
  fi
  local destination
  destination="$(cd "$ROOT_DIR" && python3 scripts/ci/select_ios_simulator.py "$PROJECT" Longhouse)"
  printf '%s\n' "$destination" | sed -n 's/.*id=\([0-9A-Fa-f-]*\).*/\1/p'
}

ensure_project() {
  [[ -d "$PROJECT" ]] && return
  (
    cd "$ROOT_DIR"
    python3 scripts/build/generate_build_identity.py
    bash scripts/build/stage_ios_build_identity.sh
    xcodegen --spec ios/XcodeHarness/project.yml --project-root ios/XcodeHarness >/dev/null
  )
}

require_udid() {
  UDID="$(resolve_udid)"
  [[ -n "$UDID" ]] || die "no iOS Simulator found; set SIM_UDID or install a runtime in Xcode"
}

server_url() {
  if [[ -n "${SIM_SERVER_URL:-}" ]]; then
    printf '%s\n' "$SIM_SERVER_URL"
    return
  fi
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["runtime_url"])' "$HOME/.longhouse/machine/state.json" 2>/dev/null || true
}

auth_token() {
  if [[ -n "${SIM_AUTH_TOKEN:-}" ]]; then
    printf '%s\n' "$SIM_AUTH_TOKEN"
    return
  fi
  cat "$HOME/.longhouse/machine/device-token" 2>/dev/null || true
}

built_app() {
  printf '%s\n' "$DERIVED/Build/Products/Debug-iphonesimulator/Longhouse.app"
}

cmd_boot() {
  ensure_project
  require_udid
  xcrun simctl boot "$UDID" 2>/dev/null || true
  xcrun simctl bootstatus "$UDID" -b >/dev/null
  echo "booted $UDID"
}

cmd_shutdown() {
  require_udid
  xcrun simctl shutdown "$UDID" 2>/dev/null || true
  echo "shut down $UDID"
}

cmd_build() {
  ensure_project
  require_udid
  mkdir -p "$DERIVED" "$OUT_DIR"
  local log
  log="$OUT_DIR/build-$(stamp).log"
  (
    cd "$ROOT_DIR"
    python3 scripts/build/generate_build_identity.py
    bash scripts/build/stage_ios_build_identity.sh
    xcodegen --spec ios/XcodeHarness/project.yml --project-root ios/XcodeHarness >/dev/null
  )
  # The build's exit status is the verdict; a filtered pipeline would hide a
  # failure behind a still-present .app from last time.
  if ! xcodebuild \
    -project "$PROJECT" \
    -scheme Longhouse \
    -configuration Debug \
    -destination "platform=iOS Simulator,id=$UDID" \
    -derivedDataPath "$DERIVED" \
    build > "$log" 2>&1; then
    grep -E "error:|BUILD FAILED" "$log" | head -20 >&2
    die "build failed; full log at $log"
  fi
  test -d "$(built_app)" || die "build produced no app at $(built_app)"
  printf '%s\n' "$(built_app)"
}

cmd_install() {
  require_udid
  local app
  app="$(built_app)"
  test -d "$app" || die "no build at $app; run build first"
  xcrun simctl install "$UDID" "$app"
  echo "installed $app"
}

cmd_launch() {
  require_udid
  local session_id="${1:-}"
  local url token
  url="$(server_url)"
  token="$(auth_token)"
  [[ -n "$url" ]] || die "no server URL; set SIM_SERVER_URL"
  [[ -n "$token" ]] || die "no auth token; set SIM_AUTH_TOKEN"
  xcrun simctl terminate "$UDID" "$BUNDLE_ID" 2>/dev/null || true
  # simctl forwards SIMCTL_CHILD_* into the app's environment. The session
  # to open rides along the same way: a URL opened from outside the app
  # stops at the system's "Open in Longhouse?" prompt, which nothing here
  # can tap.
  SIMCTL_CHILD_LONGHOUSE_HEADLESS_SERVER_URL="$url" \
  SIMCTL_CHILD_LONGHOUSE_HEADLESS_AUTH_TOKEN="$token" \
  SIMCTL_CHILD_LONGHOUSE_HEADLESS_OPEN_SESSION="$session_id" \
    xcrun simctl launch "$UDID" "$BUNDLE_ID" >/dev/null
  echo "launched $BUNDLE_ID against $url${session_id:+ on session $session_id}"
}

# Opens a URL as the system would; a custom-scheme link shows the
# "Open in Longhouse?" prompt, so prefer `launch <session-id>` for sessions.
cmd_url() {
  require_udid
  local url="${1:-}"
  [[ -n "$url" ]] || die "url needs a URL"
  xcrun simctl openurl "$UDID" "$url"
  echo "opened $url"
}

cmd_deploy() {
  cmd_boot
  cmd_build
  cmd_install
  cmd_launch "$@"
}

cmd_shot() {
  require_udid
  local label="${1:-screen}"
  mkdir -p "$OUT_DIR"
  local dest
  dest="$OUT_DIR/$(stamp)-$label.png"
  xcrun simctl io "$UDID" screenshot "$dest" >/dev/null
  printf '%s\n' "$dest"
}

cmd_record() {
  require_udid
  local seconds="${1:-}"
  [[ "$seconds" =~ ^[0-9]+$ ]] || die "record needs a duration in seconds"
  local label="${2:-screen}"
  mkdir -p "$OUT_DIR"
  local dest
  dest="$OUT_DIR/$(stamp)-$label.mp4"
  xcrun simctl io "$UDID" recordVideo --codec h264 --force "$dest" >/dev/null 2>&1 &
  local recorder=$!
  sleep "$seconds"
  kill -INT "$recorder" 2>/dev/null || true
  wait "$recorder" 2>/dev/null || true
  printf '%s\n' "$dest"
}

cmd_logs() {
  require_udid
  local follow="false" since="5m" predicate="$LOG_PREDICATE"
  while (($# > 0)); do
    case "$1" in
      --follow|-f) follow="true"; shift ;;
      --since) since="$2"; shift 2 ;;
      --all) predicate="process == \"Longhouse\""; shift ;;
      *) die "unknown logs option: $1" ;;
    esac
  done
  # The app's lifecycle marks are info and debug level; `log` hides those
  # unless told otherwise.
  if [[ "$follow" == "true" ]]; then
    exec xcrun simctl spawn "$UDID" log stream --info --debug --style compact --predicate "$predicate"
  fi
  xcrun simctl spawn "$UDID" log show --info --debug --last "$since" --style compact --predicate "$predicate" \
    | grep -vE "^Timestamp|^Filtering|^=+" || true
}

main() {
  local cmd="${1:-}"
  shift || true
  case "$cmd" in
    boot) cmd_boot "$@" ;;
    shutdown) cmd_shutdown "$@" ;;
    build) cmd_build "$@" ;;
    install) cmd_install "$@" ;;
    launch) cmd_launch "$@" ;;
    deploy) cmd_deploy "$@" ;;
    url) cmd_url "$@" ;;
    shot) cmd_shot "$@" ;;
    record) cmd_record "$@" ;;
    logs) cmd_logs "$@" ;;
    -h|--help|help|"") usage ;;
    *) die "unknown command: $cmd" ;;
  esac
}

main "$@"
