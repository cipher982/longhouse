#!/usr/bin/env bash
# Terminal-side lane for a paired iPhone: screenshot or record its screen,
# build and install the app onto it, and read the app's own diagnostics
# from the hosted tenant log. No AirDrop, no Xcode GUI, no sudo.
#
#   scripts/ops/phone.sh shot [label]            PNG into artifacts/phone/
#   scripts/ops/phone.sh record <seconds> [label] MP4 into artifacts/phone/
#   scripts/ops/phone.sh build                    Debug build for the device
#   scripts/ops/phone.sh install                  install + relaunch the last build
#   scripts/ops/phone.sh deploy                   build, then install
#   scripts/ops/phone.sh launch [<session-id>]    relaunch the app, optionally on a session
#   scripts/ops/phone.sh console [--seconds 60]   relaunch and stream the app's stdout + OSLog
#   scripts/ops/phone.sh profile [--template T] [--seconds 15] [label]
#                                                 Instruments trace of the running app + summary
#   scripts/ops/phone.sh logs [--since 30m] [--session <id>] [--server] [--follow]
#                                                 client_diag lines from the tenant log
#
# Environment:
#   PHONE_DEVICE           devicectl name/UDID (default: first connected physical iPhone)
#   PHONE_OUT_DIR          capture directory (default: artifacts/phone)
#   PHONE_TEAM_ID          Apple team id (default: read from the Apple Development cert)
#   PHONE_DERIVED_DATA     xcodebuild derived data path
#   PHONE_OPTIMIZED        1 (default): Debug compiled -O, whole-module, no debug
#                          dylib, the way it ships; 0 for a plain -Onone build
#   PHONE_LOG_SSH_TARGET   ssh alias of the host running the tenant (default: zerg)
#   PHONE_LOG_CONTAINER    tenant container name (default: longhouse-david010)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="${PHONE_OUT_DIR:-$ROOT_DIR/artifacts/phone}"
BUNDLE_ID="ai.longhouse.ios"
PROJECT="$ROOT_DIR/ios/XcodeHarness/LonghouseIOS.xcodeproj"
DERIVED="${PHONE_DERIVED_DATA:-$HOME/Library/Developer/Xcode/DerivedData/LonghouseIOS-Phone}"
SSH_TARGET="${PHONE_LOG_SSH_TARGET:-zerg}"
CONTAINER="${PHONE_LOG_CONTAINER:-longhouse-david010}"

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

die() {
  echo "phone: $*" >&2
  exit 1
}

resolve_device() {
  if [[ -n "${PHONE_DEVICE:-}" ]]; then
    printf '%s\n' "$PHONE_DEVICE"
    return
  fi
  xcrun devicectl list devices --json-output - 2>/dev/null | python3 -c '
import json, sys
devices = json.load(sys.stdin)["result"]["devices"]
phones = [
    dev for dev in devices
    if dev.get("hardwareProperties", {}).get("reality") == "physical"
    and dev.get("hardwareProperties", {}).get("platform") == "iOS"
]
# A paired phone whose tunnel is idle still answers devicectl; it reconnects
# on demand. Prefer a live tunnel, fall back to any paired phone.
phones.sort(key=lambda dev: dev.get("connectionProperties", {}).get("tunnelState") != "connected")
if phones:
    print(phones[0]["identifier"])
'
}

require_device() {
  DEVICE="$(resolve_device)"
  [[ -n "$DEVICE" ]] || die "no connected physical iPhone; pair one in Xcode or set PHONE_DEVICE"
}

stamp() {
  date -u +%Y%m%dT%H%M%SZ
}

team_id() {
  if [[ -n "${PHONE_TEAM_ID:-}" ]]; then
    printf '%s\n' "$PHONE_TEAM_ID"
    return
  fi
  security find-certificate -c "Apple Development" -p 2>/dev/null \
    | openssl x509 -noout -subject 2>/dev/null \
    | sed -n 's/.*OU *= *\([A-Z0-9]*\).*/\1/p' | head -1
}

built_app() {
  printf '%s\n' "$DERIVED/Build/Products/Debug-iphoneos/Longhouse.app"
}

cmd_shot() {
  require_device
  local label="${1:-screen}"
  mkdir -p "$OUT_DIR"
  local dest
  dest="$OUT_DIR/$(stamp)-$label.png"
  xcrun devicectl device capture screenshot --device "$DEVICE" --destination "$dest" >/dev/null
  printf '%s\n' "$dest"
}

cmd_record() {
  require_device
  local seconds="${1:-}"
  [[ "$seconds" =~ ^[0-9]+$ ]] || die "record needs a duration in seconds"
  local label="${2:-screen}"
  mkdir -p "$OUT_DIR"
  local dest
  dest="$OUT_DIR/$(stamp)-$label.mp4"
  xcrun devicectl device capture screen-record --device "$DEVICE" --destination "$dest" --duration "$seconds" >/dev/null
  printf '%s\n' "$dest"
}

cmd_build() {
  require_device
  local team
  team="$(team_id)"
  [[ -n "$team" ]] || die "no Apple team id; set PHONE_TEAM_ID"
  (
    cd "$ROOT_DIR"
    make ios-project >/dev/null
    mkdir -p "$DERIVED" "$OUT_DIR"
    local log="$OUT_DIR/build-$(stamp).log"
    # The phone is where David uses the app, so it gets optimized code. Plain
    # Debug (-Onone) made per-byte SSE parsing, key conversion, and decoding
    # several times costlier in the phone's own traces; DEBUG hooks stay.
    local -a optimize=()
    if [[ "${PHONE_OPTIMIZED:-1}" == 1 ]]; then
      optimize=(SWIFT_OPTIMIZATION_LEVEL=-O SWIFT_COMPILATION_MODE=wholemodule GCC_OPTIMIZATION_LEVEL=s ENABLE_DEBUG_DYLIB=NO)
    fi
    # The build's exit status is the verdict; a filtered pipeline would hide
    # a failure behind yesterday's still-present .app.
    if ! xcodebuild \
      -project "$PROJECT" \
      -scheme Longhouse \
      -configuration Debug \
      -destination "id=$DEVICE" \
      -derivedDataPath "$DERIVED" \
      -allowProvisioningUpdates \
      DEVELOPMENT_TEAM="$team" \
      ${optimize[@]+"${optimize[@]}"} \
      build > "$log" 2>&1; then
      grep -E "error:|BUILD FAILED" "$log" | head -20 >&2
      die "build failed; full log at $log"
    fi
    grep -E "BUILD SUCCEEDED" "$log" >/dev/null || die "build produced no success marker; log at $log"
    test -d "$(built_app)" || die "build produced no app at $(built_app)"
    report_warnings "$log"
  )
  printf '%s\n' "$(built_app)"
}

cmd_launch() {
  require_device
  local session_id="${1:-}"
  local -a payload=()
  if [[ -n "$session_id" ]]; then
    payload=(--payload-url "ai.longhouse.ios://session/$session_id")
  fi
  local output
  if ! output="$(xcrun devicectl device process launch --device "$DEVICE" --terminate-existing "${payload[@]}" "$BUNDLE_ID" 2>&1)"; then
    [[ "$output" == *"BSErrorCodeDescription = Locked"* ]] \
      && die "phone is locked: the installed build runs the next time the app is opened; unlock to launch, shot, console, or profile"
    printf '%s\n' "$output" | tail -8 >&2
    die "launch failed"
  fi
  echo "launched $BUNDLE_ID${session_id:+ on session $session_id}"
}

# Compiler warnings go to stderr after a green build. Xcode's GUI only shows
# warnings for files it recompiled, so an incremental click hides them; a
# "nearly matches optional requirement" warning here once meant WebKit never
# called the transcript's navigation guard.
report_warnings() {
  local log="$1" warnings
  warnings="$(grep -E '^/.*: warning: ' "$log" | sed "s|^$ROOT_DIR/||" | sort -u || true)"
  [[ -z "$warnings" ]] && return
  echo "$(printf '%s\n' "$warnings" | wc -l | tr -d ' ') compiler warning(s):" >&2
  printf '%s\n' "$warnings" | head -30 >&2
}

# The app's own stdout/stderr and OSLog, as Xcode's console shows them.
# OS_ACTIVITY_DT_MODE makes os_log mirror to stderr, which is how Xcode
# gets it; devicectl forwards DEVICECTL_CHILD_* into the app's environment.
# There is no devicectl log stream, so this relaunches the app.
cmd_console() {
  require_device
  local seconds="60"
  [[ "${1:-}" == "--seconds" ]] && seconds="$2"
  mkdir -p "$OUT_DIR"
  local dest
  dest="$OUT_DIR/$(stamp)-console.log"
  echo "console: $dest (${seconds}s)" >&2
  DEVICECTL_CHILD_OS_ACTIVITY_DT_MODE=enable \
    timeout "$seconds" xcrun devicectl device process launch --device "$DEVICE" \
    --terminate-existing --console "$BUNDLE_ID" 2>&1 | tee "$dest" || true
}

# Instruments from the terminal: attach to the running app, record, then
# print a one-page summary (CPU by thread and symbol, hangs, hitches).
# Templates: "Time Profiler" (default), "Animation Hitches", "SwiftUI",
# "Allocations", "Leaks", "App Launch"; `xcrun xctrace list templates`.
cmd_profile() {
  local template="Time Profiler" seconds="15" label="profile"
  while (($# > 0)); do
    case "$1" in
      --template) template="$2"; shift 2 ;;
      --seconds) seconds="$2"; shift 2 ;;
      *) label="$1"; shift ;;
    esac
  done
  require_device
  mkdir -p "$OUT_DIR"
  local dest
  dest="$OUT_DIR/$(stamp)-$label.trace"
  xcrun xctrace record --template "$template" --device "$DEVICE" --attach Longhouse \
    --time-limit "${seconds}s" --no-prompt --output "$dest" >/dev/null
  echo "trace: $dest"
  python3 "$ROOT_DIR/scripts/ops/trace_summary.py" "$dest"
}

cmd_install() {
  require_device
  local app
  app="$(built_app)"
  test -d "$app" || die "no build at $app; run build first"
  xcrun devicectl device install app --device "$DEVICE" "$app" >/dev/null
  echo "installed $app"
  cmd_launch
}

cmd_deploy() {
  cmd_build
  cmd_install
}

cmd_logs() {
  local since="30m" session="" server="false" follow="false"
  while (($# > 0)); do
    case "$1" in
      --since) since="$2"; shift 2 ;;
      --session) session="$2"; shift 2 ;;
      --server) server="true"; shift ;;
      --follow|-f) follow="true"; shift ;;
      *) die "unknown logs option: $1" ;;
    esac
  done
  local pattern='CLIENT_DIAG'
  if [[ "$server" == "true" ]]; then
    [[ -n "$session" ]] || die "--server needs --session so the server side can be filtered"
    pattern="CLIENT_DIAG|$session"
  fi
  local follow_flag=""
  [[ "$follow" == "true" ]] && follow_flag="-f"
  # shellcheck disable=SC2029
  ssh -o BatchMode=yes "$SSH_TARGET" \
    "docker logs $follow_flag --since $since $CONTAINER 2>&1 | grep --line-buffered -E '$pattern'" \
    | { if [[ -n "$session" ]]; then grep --line-buffered -E "$session|session=None"; else cat; fi; } \
    | sed -E 's/ INFO +\[(LONGHOUSE\.CLIENT_DIAG|ACCESS)\]/ \1/; s/LONGHOUSE\.CLIENT_DIAG client_diag/PHONE/' \
    | cut -c1-320
}

main() {
  local cmd="${1:-}"
  shift || true
  case "$cmd" in
    shot) cmd_shot "$@" ;;
    record) cmd_record "$@" ;;
    build) cmd_build "$@" ;;
    install) cmd_install "$@" ;;
    deploy) cmd_deploy "$@" ;;
    launch) cmd_launch "$@" ;;
    logs) cmd_logs "$@" ;;
    console) cmd_console "$@" ;;
    profile) cmd_profile "$@" ;;
    -h|--help|help|"") usage ;;
    *) die "unknown command: $cmd" ;;
  esac
}

main "$@"
