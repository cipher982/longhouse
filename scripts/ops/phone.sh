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
#   scripts/ops/phone.sh logs [--since 30m] [--session <id>] [--server] [--follow]
#                                                 client_diag lines from the tenant log
#
# Environment:
#   PHONE_DEVICE           devicectl name/UDID (default: first connected physical iPhone)
#   PHONE_OUT_DIR          capture directory (default: artifacts/phone)
#   PHONE_TEAM_ID          Apple team id (default: read from the Apple Development cert)
#   PHONE_DERIVED_DATA     xcodebuild derived data path
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
    python3 scripts/build/generate_build_identity.py
    bash scripts/build/stage_ios_build_identity.sh
    xcodegen --spec ios/XcodeHarness/project.yml --project-root ios/XcodeHarness >/dev/null
    mkdir -p "$DERIVED"
    xcodebuild \
      -project "$PROJECT" \
      -scheme Longhouse \
      -configuration Debug \
      -destination "id=$DEVICE" \
      -derivedDataPath "$DERIVED" \
      -allowProvisioningUpdates \
      DEVELOPMENT_TEAM="$team" \
      build \
      | grep -E "error:|warning: .*Longhouse|BUILD (SUCCEEDED|FAILED)" || true
    test -d "$(built_app)" || die "build produced no app at $(built_app)"
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
  xcrun devicectl device process launch --device "$DEVICE" --terminate-existing "${payload[@]}" "$BUNDLE_ID" >/dev/null
  echo "launched $BUNDLE_ID${session_id:+ on session $session_id}"
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
    -h|--help|help|"") usage ;;
    *) die "unknown command: $cmd" ;;
  esac
}

main "$@"
