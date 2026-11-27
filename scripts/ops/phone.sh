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
#   PHONE_MAC_HOST         off a Mac: ssh alias of the Mac the phone is paired with (default: cinder)
#   PHONE_SHA              off a Mac: the commit deploy builds (default: origin/main)
#
# Off a Mac (cube-ws), deploy and launch run on PHONE_MAC_HOST; deploy builds
# the exact commit in a throwaway worktree there. Over SSH the login keychain
# is locked, so the build runs as a one-shot launchd job in the GUI session.
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

# Over SSH the login keychain belongs to another security session and
# codesign fails with errSecInternalComponent. A launchd job in the user's GUI
# session signs with the keychain the logged-in user already unlocked.
keychain_usable() {
  security show-keychain-info "$HOME/Library/Keychains/login.keychain-db" >/dev/null 2>&1
}

build_in_gui_session() {
  local dir label rc
  dir="$(mktemp -d /tmp/phone-build.XXXXXX)"
  label="ai.longhouse.phone-build.$$"
  cat > "$dir/run.sh" <<EOF
#!/bin/zsh -l
export PHONE_GUI_SESSION=1 PHONE_DEVICE=$(printf %q "$DEVICE") PHONE_OUT_DIR=$(printf %q "$OUT_DIR")
export PHONE_OPTIMIZED=$(printf %q "${PHONE_OPTIMIZED:-1}") PHONE_DERIVED_DATA=$(printf %q "$DERIVED")
${PHONE_TEAM_ID:+export PHONE_TEAM_ID=$(printf %q "$PHONE_TEAM_ID")}
cd $(printf %q "$ROOT_DIR") && scripts/ops/phone.sh build > $(printf %q "$dir/out.log") 2>&1
echo \$? > $(printf %q "$dir/exit")
EOF
  chmod +x "$dir/run.sh"
  cat > "$dir/job.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$label</string>
<key>ProgramArguments</key><array><string>$dir/run.sh</string></array>
<key>RunAtLoad</key><true/>
</dict></plist>
EOF
  echo "phone: login keychain is locked in this session; building in the GUI session ($label)" >&2
  launchctl bootstrap "gui/$(id -u)" "$dir/job.plist" || { rm -rf "$dir"; die "could not start the GUI-session build; is a user logged in?"; }
  local waited=0
  while [[ ! -f "$dir/exit" && $waited -lt 1800 ]]; do sleep 2; waited=$((waited + 2)); done
  launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
  cat "$dir/out.log" 2>/dev/null || true
  rc="$(cat "$dir/exit" 2>/dev/null || echo 124)"
  rm -rf "$dir"
  [[ "$rc" == 0 ]] || die "GUI-session build failed (exit $rc)"
}

cmd_build() {
  require_device
  if [[ -z "${PHONE_GUI_SESSION:-}" ]] && ! keychain_usable; then
    build_in_gui_session
    return
  fi
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
    if [[ "$output" == *"BSErrorCodeDescription = Locked"* ]]; then
      echo "phone: phone is locked: the installed build runs the next time the app is opened; unlock to launch, shot, console, or profile" >&2
      return 3
    fi
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
  local rc=0
  cmd_launch || rc=$?
  # A locked phone has the new build; it runs when the app is next opened.
  [[ $rc == 0 || $rc == 3 ]] || return "$rc"
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

# Off a Mac the phone is reached through the Mac it is paired with. deploy
# builds an exact commit in a throwaway worktree there, so the Mac's shared
# checkout and anyone's uncommitted edits never reach the phone.
run_on_mac() {
  local cmd="$1"
  shift
  local host="${PHONE_MAC_HOST:-cinder}"
  case "$cmd" in
    launch)
      # shellcheck disable=SC2029
      ssh "$host" "zsh -lc 'cd ~/git/zerg/longhouse && scripts/ops/phone.sh launch ${1:-}'"
      ;;
    deploy)
      ssh "$host" zsh -l -s -- "${PHONE_SHA:-origin/main}" <<'EOF'
set -euo pipefail
repo=~/git/zerg/longhouse
git -C "$repo" fetch -q origin
sha="$(git -C "$repo" rev-parse --verify "$1^{commit}")"
wt=~/git/_wt/longhouse-phone-${sha[1,12]}
trap 'git -C "$repo" worktree remove --force "$wt" >/dev/null 2>&1 || true' EXIT
git -C "$repo" worktree add -q --detach "$wt" "$sha"
echo "phone: deploying $sha from $(hostname -s)" >&2
cd "$wt"
PHONE_OUT_DIR=/tmp/agents/phone scripts/ops/phone.sh deploy </dev/null
EOF
      ;;
    *) die "$cmd needs the Mac the phone is paired with: ssh ${host} and run it there" ;;
  esac
}

main() {
  local cmd="${1:-}"
  shift || true
  if [[ "$(uname)" != Darwin ]]; then
    case "$cmd" in
      shot|record|build|install|deploy|launch|console|profile) run_on_mac "$cmd" "$@"; return ;;
    esac
  fi
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
