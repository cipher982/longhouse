#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVER_PROJECT="$ROOT_DIR/server"
ENGINE_DIR="$ROOT_DIR/engine"
DESKTOP_PACKAGE_PATH="$ROOT_DIR/desktop/LonghouseMenuBarHarness"
ARTIFACT_DIR="$ROOT_DIR/artifacts/dogfood-runtime"

COMMAND="${1:-}"
if [[ -n "${COMMAND}" ]]; then
  shift
fi

DEFAULT_ROUTE_E2E_PROVIDER="opencode"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
URL_OVERRIDE="${LONGHOUSE_DOGFOOD_URL:-}"
MACHINE_NAME_OVERRIDE="${LONGHOUSE_DOGFOOD_MACHINE_NAME:-}"
ROUTE_E2E_PROVIDER="${LONGHOUSE_DOGFOOD_PROVIDER_LIVE_ROUTE_PROVIDER:-$DEFAULT_ROUTE_E2E_PROVIDER}"
MENUBAR=1
SKIP_ENGINE=0
SKIP_ROUTE_E2E=0

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
  scripts/dev/dogfood-runtime.sh refresh [--url <url>] [--machine-name <name>] [--claude-dir <path>] [--no-menubar] [--skip-engine] [--skip-route-e2e] [--route-provider <provider|all>]
  scripts/dev/dogfood-runtime.sh check [--claude-dir <path>]

Purpose:
  Refresh installs the real local Longhouse runtime from current repo source.
  Check shows the installed runtime state and local health.

Notes:
  - This is the dogfood loop for repo work. It installs into the actual local runtime.
  - DMG/drag-install is release transport only. Daily dogfooding should use this script.
  - Refresh runs a no-token hosted provider-live route E2E after install unless --skip-route-e2e is set.
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

read_trimmed_file() {
  local path="$1"
  if [[ -f "$path" ]]; then
    tr -d '\n' < "$path"
  fi
}

read_machine_state_field() {
  local field="$1"
  local state_path="$LONGHOUSE_HOME/machine/state.json"
  [[ -f "$state_path" ]] || return 0

  python3 - "$state_path" "$field" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
field = sys.argv[2]
payload = json.loads(path.read_text(encoding="utf-8"))
value = payload.get(field)
if isinstance(value, str):
    print(value.strip())
PY
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
  if [[ -n "$URL_OVERRIDE" ]]; then
    printf '%s\n' "$URL_OVERRIDE"
    return
  fi

  local configured_url
  configured_url="$(read_machine_state_field runtime_url)"
  if [[ -n "$configured_url" ]]; then
    printf '%s\n' "$configured_url"
    return
  fi

  fail "No canonical machine state found at $LONGHOUSE_HOME/machine/state.json. Pass --url or run longhouse connect --install once."
}

resolve_machine_name() {
  if [[ -n "$MACHINE_NAME_OVERRIDE" ]]; then
    printf '%s\n' "$MACHINE_NAME_OVERRIDE"
    return
  fi

  local configured_name
  configured_name="$(read_machine_state_field machine_name)"
  if [[ -n "$configured_name" ]]; then
    printf '%s\n' "$configured_name"
    return
  fi

  hostname -s 2>/dev/null || hostname
}

install_engine_from_source() {
  require_cmd cargo
  mkdir -p "$HOME/.local/bin"

  log "==> Building Rust engine"
  # build-identity.json must be regenerated for the current HEAD before
  # cargo runs, otherwise engine/build.rs reads a stale identity and the
  # resulting binary reports the previous commit's SHA. See "BUILD DRIFT"
  # in the menu bar if this is wrong.
  python3 "$ROOT_DIR/scripts/build/generate_build_identity.py"
  (cd "$ENGINE_DIR" && cargo build --release)

  if [[ "$(uname -s)" == "Darwin" ]] && command -v codesign >/dev/null 2>&1; then
    log "==> Ad-hoc signing engine"
    codesign -s - "$ENGINE_DIR/target/release/longhouse-engine" >/dev/null
  fi

  rm -f "$HOME/.local/bin/longhouse-engine"
  install -m 755 "$ENGINE_DIR/target/release/longhouse-engine" "$HOME/.local/bin/longhouse-engine"
  log "Engine ready at $HOME/.local/bin/longhouse-engine"
}

install_cli_from_source() {
  require_cmd uv
  require_cmd python3

  log "==> Generating build identity"
  python3 "$ROOT_DIR/scripts/build/generate_build_identity.py"

  ensure_frontend_dist

  log "==> Building Longhouse CLI wheel from current repo source"
  local wheel_dir="$ROOT_DIR/.build/wheel"
  rm -rf "$wheel_dir"
  mkdir -p "$wheel_dir"
  (cd "$SERVER_PROJECT" && uv build --wheel --out-dir "$wheel_dir" >/dev/null)

  local wheel_path
  wheel_path="$(ls -1 "$wheel_dir"/longhouse-*.whl 2>/dev/null | head -n1)"
  [[ -n "$wheel_path" ]] || fail "wheel build produced no artifact under $wheel_dir"

  log "==> Installing CLI from wheel ($(basename "$wheel_path"))"
  uv tool install --force "$wheel_path" >/dev/null

  log "CLI ready at $(command -v longhouse)"
  log "CLI version: $(longhouse --version)"
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
  [[ "$(uname -s)" == "Darwin" ]] || return 0
  (( MENUBAR == 1 )) || return 0

  require_cmd swift

  mkdir -p "$ARTIFACT_DIR"
  local bundle_version
  local menubar_binary
  bundle_version="$(current_version)"

  printf '==> Building Longhouse.app from current source\n' >&2
  menubar_binary="$("$ROOT_DIR/scripts/resolve-swift-product-path.sh" \
    --package-path "$DESKTOP_PACKAGE_PATH" \
    --product LonghouseMenuBarHarnessMenuBar \
    --configuration release)"

  "$ROOT_DIR/scripts/release/macos-package-app.sh" \
    --binary "$menubar_binary" \
    --app-name Longhouse \
    --exec-name Longhouse \
    --bundle-id ai.longhouse.app \
    --version "${bundle_version}-local" \
    --short-version "${bundle_version}-local" \
    --output-dir "$ARTIFACT_DIR" \
    --icon-png "$ROOT_DIR/web/public/favicon-512.png" \
    --lsuielement true >/dev/null

  printf '%s\n' "$ARTIFACT_DIR/Longhouse.app"
}

run_repo_longhouse() {
  local -a cmd
  if [[ -x "$SERVER_PROJECT/.venv/bin/python" ]]; then
    cmd=("$SERVER_PROJECT/.venv/bin/python" -m zerg.cli.main "$@")
  else
    cmd=(uv run --project "$SERVER_PROJECT" python -m zerg.cli.main "$@")
  fi
  "${cmd[@]}"
}

run_check() {
  log "==> Installed runtime status"
  if command -v longhouse >/dev/null 2>&1; then
    log "CLI: $(command -v longhouse) ($(longhouse --version))"
  else
    log "CLI: not found on PATH"
  fi
  run_repo_longhouse connect --status
  log ""
  log "==> Local health"
  run_repo_longhouse local-health
}

publish_provider_live_proof() {
  local status=0
  require_cmd longhouse

  log "==> Publishing provider live proof"
  log "Proof dir: $PROVIDER_LIVE_PROOF_DIR"
  LONGHOUSE_PROVIDER_LIVE_PROOF_DIR="$PROVIDER_LIVE_PROOF_DIR" \
    longhouse provider-live publish \
      --repo-root "$ROOT_DIR" || status=$?
  if (( status != 0 )); then
    log "WARN: provider live proof published with failures (exit $status); local-health will show the sidecar state."
    log "Evidence root: $ROOT_DIR/.build/canaries/provider-live"
  fi
}

run_provider_live_route_e2e() {
  local status=0
  local route_artifact="$LONGHOUSE_HOME/provider-live-route-e2e/latest.json"
  local repo_artifact="$ARTIFACT_DIR/provider-live-route-e2e.json"
  require_cmd python3

  log ""
  log "==> Hosted provider-live route E2E"
  log "Provider: $ROUTE_E2E_PROVIDER"
  log "Evidence: $route_artifact"
  mkdir -p "$(dirname "$route_artifact")" "$ARTIFACT_DIR"
  LONGHOUSE_HOME="$LONGHOUSE_HOME" \
  LONGHOUSE_PROVIDER_LIVE_PROOF_DIR="$PROVIDER_LIVE_PROOF_DIR" \
    python3 "$ROOT_DIR/scripts/qa/provider-live-route-e2e.py" \
      --provider "$ROUTE_E2E_PROVIDER" \
      --artifact "$route_artifact" || status=$?
  cp -f "$route_artifact" "$repo_artifact" 2>/dev/null || true
  if (( status != 0 )); then
    fail "Hosted provider-live route E2E failed (exit $status). See $route_artifact"
  fi
}

run_refresh() {
  local url
  local machine_name
  local app_bundle=""

  require_cmd uv
  require_cmd python3
  url="$(resolve_url)"
  machine_name="$(resolve_machine_name)"

  if (( SKIP_ENGINE == 0 )); then
    install_engine_from_source
  else
    log "==> Skipping engine rebuild"
  fi

  install_cli_from_source

  if [[ "$(uname -s)" == "Darwin" ]] && (( MENUBAR == 1 )); then
    app_bundle="$(build_desktop_app_bundle)"
  fi

  log "==> Refreshing installed local runtime from repo source"
  log "URL: $url"
  log "Machine: $machine_name"
  if [[ "$(uname -s)" == "Darwin" ]]; then
    if (( MENUBAR == 1 )); then
      log "Desktop App: enabled"
    else
      log "Desktop App: disabled"
    fi
  fi

  if [[ -n "$app_bundle" ]]; then
    LONGHOUSE_DESKTOP_APP_SOURCE="$app_bundle" \
      run_repo_longhouse connect --install --url "$url" --machine-name "$machine_name" --menubar --claude-dir "$CLAUDE_DIR"
  else
    run_repo_longhouse connect --install --url "$url" --machine-name "$machine_name" --no-menubar --claude-dir "$CLAUDE_DIR"
  fi

  log ""
  publish_provider_live_proof
  log ""
  run_check
  if (( SKIP_ROUTE_E2E == 0 )); then
    run_provider_live_route_e2e
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)
      URL_OVERRIDE="${2:-}"
      shift 2
      ;;
    --machine-name)
      MACHINE_NAME_OVERRIDE="${2:-}"
      shift 2
      ;;
    --claude-dir)
      CLAUDE_DIR="${2:-}"
      shift 2
      ;;
    --no-menubar)
      MENUBAR=0
      shift
      ;;
    --skip-engine)
      SKIP_ENGINE=1
      shift
      ;;
    --skip-route-e2e)
      SKIP_ROUTE_E2E=1
      shift
      ;;
    --route-provider)
      ROUTE_E2E_PROVIDER="${2:-}"
      shift 2
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
PROVIDER_LIVE_PROOF_DIR="$LONGHOUSE_HOME/provider-live-proof"

case "$COMMAND" in
  refresh)
    run_refresh
    ;;
  check)
    require_cmd uv
    run_check
    ;;
  ""|-h|--help|help)
    usage
    ;;
  *)
    fail "Unknown command: $COMMAND"
    ;;
esac
