#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVER_PROJECT="$ROOT_DIR/server"
DESKTOP_PACKAGE_PATH="$ROOT_DIR/desktop/LonghouseMenuBarHarness"
ARTIFACT_DIR="$ROOT_DIR/artifacts/dogfood-runtime"
LH="$HOME/.local/bin/longhouse"
LH_SERVER="$HOME/.local/bin/longhouse-server"
APP_IDENTITY=/Applications/Longhouse.app/Contents/Resources/build-identity.json
GH_REPO=cipher982/longhouse
CI_WORKFLOW=contract-first-ci.yml

COMMAND="${1:-}"
if [[ -n "${COMMAND}" ]]; then
  shift
fi
ARGS=("$@")

ENGINE_PROFILE="${LONGHOUSE_DOGFOOD_ENGINE_PROFILE:-ci}"
READY_TIMEOUT="${LONGHOUSE_DOGFOOD_READY_TIMEOUT_SECS:-120}"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
MENUBAR=1
SKIP_ENGINE=0
FORCE=0
FULL_HEALTH="${LONGHOUSE_DOGFOOD_FULL_HEALTH:-0}"
SWIFT_BUILD_DIR=""
BUILT_APP_BUNDLE=""
SHA_REQUEST=""
HERE=0
FORWARD_ARGS=()

cleanup_dogfood_scratch() {
  if [[ -n "$SWIFT_BUILD_DIR" && -d "$SWIFT_BUILD_DIR" ]]; then
    rm -rf "$SWIFT_BUILD_DIR"
  fi
}

trap cleanup_dogfood_scratch EXIT

resolve_longhouse_home() {
  local provider_home="$1"
  if [[ -n "${LONGHOUSE_HOME:-}" ]]; then
    printf '%s\n' "$LONGHOUSE_HOME"
    return
  fi
  local basename
  basename="$(basename "$provider_home")"
  if [[ "$basename" == ".longhouse" ]]; then
    printf '%s\n' "$provider_home"
    return
  fi
  printf '%s\n' "$(dirname "$provider_home")/.longhouse"
}

usage() {
  cat <<'EOF'
Usage:
  scripts/dev/dogfood-runtime.sh refresh [--sha <rev>] [--force] [--here] [--no-menubar] [--skip-engine] [--claude-dir <path>]
  scripts/dev/dogfood-runtime.sh check [--claude-dir <path>]

Purpose:
  Refresh installs the real local Longhouse runtime from one exact commit: the
  newest main commit whose CI passed, unless --sha names another. It refuses a
  --sha whose CI has not passed, and does nothing when this Mac already runs
  that commit; --force does both anyway (with no --sha it takes origin/main's
  tip). It builds from a disposable worktree of that commit under /tmp/agents,
  removed on exit, under the machine-wide heavy-build lock, reusing the primary
  checkout's Cargo target. --here builds this working tree as it is instead
  (uncommitted edits included).
  The refresh runs detached and kept awake, so a dropped SSH session, a closed
  terminal or Ctrl-C stops only the log you are following; it ends by waiting
  (LONGHOUSE_DOGFOOD_READY_TIMEOUT_SECS, default 120) for the new Machine Agent
  to connect and get a heartbeat verdict before it prints the readiness summary.
  Check shows the installed runtime state and local health.

Notes:
  - DMG/drag-install is release transport only. Daily dogfooding should use this script.
  - Engine builds use LONGHOUSE_DOGFOOD_ENGINE_PROFILE=ci by default. Set it to release to force the slower release-LTO profile.
  - Set LONGHOUSE_DOGFOOD_FULL_HEALTH=1 to also print the full human local-health report.
  - To point this Mac at another Runtime Host, run `longhouse auth --url <url>` first.
EOF
}

log() {
  printf '%s\n' "$*"
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

current_version() {
  python3 - "$SERVER_PROJECT/pyproject.toml" <<'PY'
import sys
import tomllib
from pathlib import Path

payload = tomllib.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(payload["project"]["version"])
PY
}

resolve_url() {
  local state_path="$LONGHOUSE_HOME/machine/state.json" url=""
  [[ -f "$state_path" ]] && url="$(python3 -c 'import json, sys; print((json.load(open(sys.argv[1])).get("runtime_url") or "").strip())' "$state_path")"
  [[ -n "$url" ]] || fail "No canonical machine state found at $state_path. Run longhouse auth --url <runtime-url> once."
  printf '%s\n' "$url"
}

install_engine_from_source() {
  require_cmd cargo
  mkdir -p "$HOME/.local/bin"

  case "$ENGINE_PROFILE" in
    ci|release) ;;
    *) fail "Unsupported LONGHOUSE_DOGFOOD_ENGINE_PROFILE=$ENGINE_PROFILE (expected ci or release)" ;;
  esac

  local engine_binary
  local facade_binary

  log "==> Building Rust engine ($ENGINE_PROFILE profile)"
  # build-identity.json must be regenerated for the current HEAD before
  # cargo runs, otherwise engine/build.rs reads a stale identity and the
  # resulting binary reports the previous commit's SHA. See "BUILD DRIFT"
  # in the menu bar if this is wrong.
  python3 "$ROOT_DIR/scripts/build/generate_build_identity.py"
  python3 "$ROOT_DIR/scripts/build/cargo.py" exec -- build \
    --manifest-path "$ROOT_DIR/engine/Cargo.toml" \
    --profile "$ENGINE_PROFILE" --bin longhouse --bin longhouse-engine
  engine_binary="$(python3 "$ROOT_DIR/scripts/build/cargo.py" artifact --profile "$ENGINE_PROFILE" --bin longhouse-engine)"
  facade_binary="$(python3 "$ROOT_DIR/scripts/build/cargo.py" artifact --profile "$ENGINE_PROFILE" --bin longhouse)"

  if [[ "$(uname -s)" == "Darwin" ]] && command -v codesign >/dev/null 2>&1; then
    log "==> Ad-hoc signing engine"
    codesign -s - "$facade_binary" "$engine_binary" >/dev/null
  fi

  rm -f "$LH" "$HOME/.local/bin/longhouse-engine"
  install -m 755 "$facade_binary" "$LH"
  install -m 755 "$engine_binary" "$HOME/.local/bin/longhouse-engine"
  "$LH" verify-pair >/dev/null
  log "Native pair ready: $("$LH" build-identity)"
}

install_server_cli_from_source() {
  # `longhouse-server` (the uv tool named "longhouse") used to be an editable
  # install pointing at the primary checkout. Nothing advances that checkout
  # since release and refresh moved to disposable worktrees, so the CLI drifted
  # 80 commits behind the engine on 2026-10-09 and lacked a guard that had
  # already landed. Install it from this exact checkout, non-editable, so the
  # whole Mac runs one commit. A later `uv tool install -e .` for local CLI
  # work is still fine; the next refresh puts this back.
  log "==> Installing longhouse-server from this checkout"
  python3 "$ROOT_DIR/scripts/build/generate_build_identity.py"
  # The wheel force-includes web/dist (server/pyproject.toml), which is
  # gitignored and absent from a fresh exact checkout; build it first so the
  # installed `longhouse-server serve` carries the UI.
  ensure_frontend_dist
  # Rebuild only longhouse itself: a bare --reinstall implies --refresh and
  # revalidates every cached dependency.
  uv tool install --force --reinstall-package longhouse --quiet "$SERVER_PROJECT"
  log "longhouse-server ready: $("$LH_SERVER" --version 2>/dev/null || echo unknown)"
}

ensure_frontend_dist() {
  local dist_index="$ROOT_DIR/web/dist/index.html"
  local needs_build=0
  local reason="missing"

  if [[ -f "$dist_index" ]]; then
    reason="stale"
    needs_build=0
    if find \
      "$ROOT_DIR/web/src" \
      "$ROOT_DIR/web/public" \
      "$ROOT_DIR/web/index.html" \
      "$ROOT_DIR/web/package.json" \
      "$ROOT_DIR/web/vite.config.ts" \
      "$ROOT_DIR/bun.lock" \
      -newer "$dist_index" -print -quit | grep -q .; then
      needs_build=1
    fi
  else
    needs_build=1
  fi

  if (( needs_build == 0 )); then
    log "==> Reusing existing frontend dist"
    return
  fi

  require_cmd bun
  log "==> Building frontend dist for CLI wheel ($reason)"
  (
    cd "$ROOT_DIR"
    bun install --frozen-lockfile --silent
    cd "$ROOT_DIR/web"
    bun run build >/dev/null
  )
}

build_desktop_app_bundle() {
  require_cmd swift
  mkdir -p "$ARTIFACT_DIR"

  local scratch_path="$1"
  local bundle_version
  local menubar_binary
  bundle_version="$(current_version)"

  printf '==> Building Longhouse.app from current source\n' >&2
  menubar_binary="$("$ROOT_DIR/scripts/build/resolve-swift-product-path.sh" \
    --package-path "$DESKTOP_PACKAGE_PATH" \
    --product LonghouseMenuBarHarnessMenuBar \
    --configuration release \
    --scratch-path "$scratch_path")"

  # The bundled build identity is how the next refresh knows the app already runs this commit.
  "$ROOT_DIR/scripts/release/macos-package-app.sh" \
    --binary "$menubar_binary" \
    --app-name Longhouse \
    --exec-name Longhouse \
    --bundle-id ai.longhouse.app \
    --version "${bundle_version}-local" \
    --short-version "${bundle_version}-local" \
    --output-dir "$ARTIFACT_DIR" \
    --icon-png "$ROOT_DIR/web/public/favicon-512.png" \
    --build-identity "$ROOT_DIR/.build/build-identity.json" \
    --lsuielement true >/dev/null

  BUILT_APP_BUNDLE="$ARTIFACT_DIR/Longhouse.app"
}

print_runtime_readiness_summary() {
  local snapshot_path="$1"
  python3 - "$snapshot_path" <<'PY'
import json
import sys
from pathlib import Path

snapshot = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
launch = snapshot.get("launch_readiness") or {}
control = snapshot.get("control_channel") or {}

def render_list(value):
    items = [str(item) for item in list(value or []) if str(item).strip()]
    return ", ".join(items) if items else "-"

print("==> Runtime readiness summary")
print(f"  launch_readiness.state: {launch.get('state') or '-'}")
print(f"  control_channel_status: {control.get('status') or '-'}")
print(f"  console_blocked_by: {control.get('console_blocked_by') or '-'}")
print(f"  console_ready_providers: {render_list(control.get('console_ready_providers'))}")
print(f"  launch_readiness.reasons: {render_list(launch.get('reasons'))}")
print(f"  launch_readiness.warnings: {render_list(launch.get('warnings'))}")
# Whether the Runtime Host applied this machine's evidence. A 204 heartbeat
# with refused evidence looks healthy everywhere else (2026-11-17).
heartbeat = snapshot.get("heartbeat_transport") or {}
evidence = heartbeat.get("evidence_state") or "-"
refusal = heartbeat.get("evidence_refusal_summary")
print(f"  heartbeat.evidence_state: {evidence}" + (f" ({refusal})" if refusal else ""))
PY
}

# Exit 0 once a local-health snapshot shows the control channel connected and a
# heartbeat verdict (applied, partial or rejected): the Machine Agent has reported.
machine_agent_reported() {
  python3 - "$1" 2>/dev/null <<'PY'
import json
import sys

snapshot = json.load(open(sys.argv[1], encoding="utf-8"))
connected = (snapshot.get("control_channel") or {}).get("status") == "connected"
verdict = (snapshot.get("heartbeat_transport") or {}).get("evidence_state") not in (None, "", "unknown")
sys.exit(0 if connected and verdict else 1)
PY
}

# `machine repair` returns once the restarted agent writes fresh local status; the
# control channel and the first heartbeat verdict come later. Summarizing at that
# point printed disconnected/unknown after good refreshes (2026-11-28).
wait_for_machine_agent() {
  local snapshot deadline=$((SECONDS + READY_TIMEOUT))
  snapshot="$(mktemp -t longhouse-dogfood-ready.XXXXXX)"
  log "==> Waiting up to ${READY_TIMEOUT}s for the new Machine Agent to connect and report"
  until "$LH" local-health --json >"$snapshot" 2>/dev/null && machine_agent_reported "$snapshot"; do
    if (( SECONDS >= deadline )); then
      log "WARN: the Machine Agent did not connect and report within ${READY_TIMEOUT}s; the summary below is its current state."
      break
    fi
    sleep 2
  done
  rm -f "$snapshot"
}

run_check() {
  local snapshot_file=""
  log "==> Installed runtime status"
  if command -v longhouse >/dev/null 2>&1; then
    log "CLI: $(command -v longhouse) ($(longhouse build-identity))"
  else
    log "CLI: not found on PATH"
  fi
  snapshot_file="$(mktemp -t longhouse-dogfood-health.XXXXXX)"
  if "$LH" local-health --json >"$snapshot_file"; then
    print_runtime_readiness_summary "$snapshot_file"
    if [[ "$FULL_HEALTH" == "1" || "$FULL_HEALTH" == "true" || "$FULL_HEALTH" == "yes" ]]; then
      log ""
      log "==> Local health"
      "$LH" local-health
    fi
  else
    log "WARN: local-health --json failed; falling back to human local-health output"
    log ""
    log "==> Local health"
    "$LH" local-health
  fi
  rm -f "$snapshot_file"
}

# Prints the first installed piece that does not run commit $1, or nothing. With
# "app" it also reads Longhouse.app's bundled identity, which only apps built by
# this script since 2026-11-28 carry, so the post-install check leaves it out.
installed_mismatch() {
  local sha="$1" short="${1:0:8}" got
  if (( SKIP_ENGINE == 0 )); then
    got="$("$LH" build-identity 2>/dev/null || true)"
    [[ "$got" == *"+$short"* ]] || { echo "Installed longhouse reports '$got', not $short."; return; }
  fi
  got="$("$LH_SERVER" --version 2>/dev/null || true)"
  [[ "$got" == *"+$short"* ]] || { echo "Installed longhouse-server reports '$got', not $short."; return; }
  if [[ "${2:-}" == app && "$(uname -s)" == "Darwin" ]] && (( MENUBAR == 1 )); then
    got="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["commit"])' "$APP_IDENTITY" 2>/dev/null || true)"
    [[ "$got" == "$sha" ]] || echo "Longhouse.app reports '${got:-no build identity}', not $short."
  fi
  return 0
}

# CI on main drops a queued run when a newer push lands, so many commits have no
# run at all; the newest green one is the safe default.
newest_green_main() {
  gh run list -R "$GH_REPO" --workflow "$CI_WORKFLOW" --branch main --status success --limit 1 \
    --json headSha -q '.[0].headSha // empty' 2>/dev/null
}

ci_verdict() {
  gh run list -R "$GH_REPO" --workflow "$CI_WORKFLOW" --commit "$1" --json status,conclusion \
    -q '[.[] | if (.conclusion // "") == "" then .status else .conclusion end] | if index("success") then "success" else (first // "no CI run") end' \
    2>/dev/null || echo "unknown (gh run list failed)"
}

run_refresh_exact() {
  # shellcheck source=../lib/exact-checkout.sh
  . "$ROOT_DIR/scripts/lib/exact-checkout.sh"
  # shellcheck source=../lib/heavy-build-lock.sh
  . "$ROOT_DIR/scripts/lib/heavy-build-lock.sh"
  local rev="$SHA_REQUEST" sha verdict common owner mismatch status=0
  git -C "$ROOT_DIR" fetch --quiet origin main || [[ -n "$rev" ]] || fail "Could not fetch origin main; pass --sha <rev> or --here."
  if [[ -z "$rev" ]] && (( FORCE == 1 )); then
    rev=origin/main
  elif [[ -z "$rev" ]]; then
    rev="$(newest_green_main)" || true
    [[ -n "$rev" ]] || fail "Could not find a green $CI_WORKFLOW run on main (gh run list). Pass --sha <rev> --force to install without one."
    log "==> Newest main commit with green CI: ${rev:0:10} (origin/main is $(git -C "$ROOT_DIR" rev-parse --short=10 origin/main))"
  fi
  sha="$(git -C "$ROOT_DIR" rev-parse --verify --quiet "${rev}^{commit}")" || fail "$rev is not a commit in $ROOT_DIR (fetch it first)."
  if [[ -n "$SHA_REQUEST" ]] && (( FORCE == 0 )); then
    verdict="$(ci_verdict "$sha")"
    [[ "$verdict" == success ]] || fail "CI for ${sha:0:10} is '$verdict', not success. Pick a green commit, or pass --force (FORCE=1) to install it anyway."
  fi
  if (( FORCE == 0 )) && [[ -z "$(installed_mismatch "$sha" app)" ]]; then
    log "==> This Mac already runs ${sha:0:8}; nothing to install (--force reinstalls)"
    run_check
    return
  fi
  trap 'cleanup_dogfood_scratch; lh_exact_checkout_cleanup' EXIT
  lh_exact_checkout "$ROOT_DIR" dogfood-refresh "$sha" || fail "Cannot check out $sha."
  # Build into the clone's primary checkout's Cargo target (scripts/build/cargo.py
  # LONGHOUSE_CARGO_TARGET_OWNER): one warm build output, not a cold one per refresh.
  common="$(git -C "$ROOT_DIR" rev-parse --path-format=absolute --git-common-dir)"
  owner="$ROOT_DIR"
  [[ "$(basename "$common")" != ".git" ]] || owner="$(dirname "$common")"
  log "==> Refreshing this Mac from ${LH_EXACT_SHA} ($rev) in $LH_EXACT_CHECKOUT"
  (
    cd "$LH_EXACT_CHECKOUT"
    LONGHOUSE_DOGFOOD_CHECKOUT=1 LONGHOUSE_CARGO_TARGET_OWNER="$owner" \
      lh_run_heavy bash "$LH_EXACT_CHECKOUT/scripts/dev/dogfood-runtime.sh" refresh ${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}
  ) || status=$?
  (( status == 0 )) || exit "$status"
  mismatch="$(installed_mismatch "$sha")"
  [[ -z "$mismatch" ]] || fail "$mismatch"
  (( SKIP_ENGINE == 1 )) || log "Installed exactly ${sha:0:8}: $("$LH" build-identity)"
  log "longhouse-server exactly ${sha:0:8}: $("$LH_SERVER" --version)"
}

run_refresh() {
  local url
  local app_bundle=""
  local tool_python

  require_cmd uv
  require_cmd python3
  url="$(resolve_url)"

  if (( SKIP_ENGINE == 0 )); then
    install_engine_from_source
  else
    log "==> Skipping engine rebuild"
  fi
  install_server_cli_from_source

  if [[ "$(uname -s)" == "Darwin" ]] && (( MENUBAR == 1 )); then
    SWIFT_BUILD_DIR="$(mktemp -d -t longhouse-dogfood-swift.XXXXXX)"
    build_desktop_app_bundle "$SWIFT_BUILD_DIR"
    app_bundle="$BUILT_APP_BUNDLE"
  fi

  log "==> Refreshing installed local runtime from repo source"
  log "URL: $url"

  if [[ -n "$app_bundle" ]]; then
    log "==> Installing Longhouse.app"
    rm -rf /Applications/Longhouse.app
    ditto "$app_bundle" /Applications/Longhouse.app

    # Regenerate the launch agent too. Copying the bundle leaves the existing
    # plist untouched, so a --health-exec recorded against a binary that has
    # since been removed survives every refresh -- which is how the panel sat on
    # an 11-day-old cache. `longhouse machine repair` does not cover this: it
    # repairs the Machine Agent service and never reads this plist. It runs on
    # the longhouse-server tool just installed from this commit; `uv run` here
    # built a whole dev environment in the throwaway checkout every refresh.
    log "==> Reinstalling Longhouse.app launch agent"
    tool_python="$(uv tool dir)/longhouse/bin/python"
    "$tool_python" -c '
import sys
from zerg.services.desktop_app import install_desktop_app_service

install_desktop_app_service(ui_url=sys.argv[1], claude_dir=sys.argv[2])
' "$url" "$CLAUDE_DIR"
  fi
  "$LH" machine repair --repair-service --json >/dev/null
  wait_for_machine_agent

  run_check
}

# Replacing the runtime must not stop halfway because the SSH session dropped, the
# terminal closed or the Mac slept: run the refresh in the background, immune to
# hangup and Ctrl-C, kept awake by caffeinate, and follow its log. The exit code
# is the refresh's.
run_detached() {
  local log_path pid rc=0 awake=()
  ! command -v caffeinate >/dev/null 2>&1 || awake=(caffeinate -is)
  log_path="$(mktemp "${TMPDIR:-/tmp}/longhouse-dogfood-refresh.XXXXXX")"
  LONGHOUSE_DOGFOOD_DETACHED=1 nohup ${awake[@]+"${awake[@]}"} bash "$ROOT_DIR/scripts/dev/dogfood-runtime.sh" refresh \
    ${ARGS[@]+"${ARGS[@]}"} >"$log_path" 2>&1 </dev/null &
  pid=$!
  log "==> Refresh runs detached as pid $pid, so a dropped session does not interrupt it. Log: $log_path"
  tail -n +1 -f "$log_path" &
  FOLLOWER=$!  # global: the EXIT trap runs after this function's locals are gone
  trap 'kill "$FOLLOWER" 2>/dev/null || true' EXIT
  wait "$pid" || rc=$?
  sleep 1  # let the follower print the last lines
  return "$rc"
}

if [[ "${LONGHOUSE_DOGFOOD_RUNTIME_SOURCE_ONLY:-0}" == "1" ]]; then
  return 0 2>/dev/null || exit 0
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --claude-dir)
      CLAUDE_DIR="${2:-}"
      FORWARD_ARGS+=("$1" "${2:-}")
      shift 2
      ;;
    --no-menubar)
      MENUBAR=0
      FORWARD_ARGS+=("$1")
      shift
      ;;
    --skip-engine)
      SKIP_ENGINE=1
      FORWARD_ARGS+=("$1")
      shift
      ;;
    --sha)
      SHA_REQUEST="${2:-}"
      [[ -n "$SHA_REQUEST" ]] || fail "--sha needs a revision"
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --here)
      HERE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $1"
      ;;
  esac
done

LONGHOUSE_HOME="$(resolve_longhouse_home "$CLAUDE_DIR")"

case "$COMMAND" in
  refresh)
    if [[ "${LONGHOUSE_DOGFOOD_CHECKOUT:-0}" == "1" ]]; then
      run_refresh
    elif [[ "${LONGHOUSE_DOGFOOD_DETACHED:-0}" != "1" ]]; then
      run_detached
    elif (( HERE == 1 )); then
      run_refresh
    else
      run_refresh_exact
    fi
    ;;
  check)
    run_check
    ;;
  ""|-h|--help|help)
    usage
    ;;
  *)
    fail "Unknown command: $COMMAND"
    ;;
esac
