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

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
URL_OVERRIDE="${LONGHOUSE_DOGFOOD_URL:-}"
MACHINE_NAME_OVERRIDE="${LONGHOUSE_DOGFOOD_MACHINE_NAME:-}"
MENUBAR=1
SKIP_ENGINE=0

resolve_longhouse_home() {
  local provider_home="$1"
  local basename
  basename="$(basename "$provider_home")"
  if [[ "$basename" == ".longhouse" ]]; then
    printf '%s\n' "$provider_home"
    return
  fi
  printf '%s\n' "$(dirname "$provider_home")/.longhouse"
}

LONGHOUSE_HOME="$(resolve_longhouse_home "$CLAUDE_DIR")"

usage() {
  cat <<'EOF'
Usage:
  scripts/dev/dogfood-runtime.sh refresh [--url <url>] [--machine-name <name>] [--claude-dir <path>] [--no-menubar] [--skip-engine]
  scripts/dev/dogfood-runtime.sh check [--claude-dir <path>]

Purpose:
  Refresh installs the real local Longhouse runtime from current repo source.
  Check shows the installed runtime state and local health.

Notes:
  - This is the dogfood loop for repo work. It installs into the actual local runtime.
  - DMG/drag-install is release transport only. Daily dogfooding should use this script.
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
  configured_url="$(read_trimmed_file "$LONGHOUSE_HOME/machine/target-url")"
  if [[ -n "$configured_url" ]]; then
    printf '%s\n' "$configured_url"
    return
  fi

  fail "No configured Longhouse URL found. Pass --url or run onboarding first."
}

resolve_machine_name() {
  if [[ -n "$MACHINE_NAME_OVERRIDE" ]]; then
    printf '%s\n' "$MACHINE_NAME_OVERRIDE"
    return
  fi

  local configured_name
  configured_name="$(read_trimmed_file "$LONGHOUSE_HOME/machine/name")"
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
  (cd "$ENGINE_DIR" && cargo build --release)

  if [[ "$(uname -s)" == "Darwin" ]] && command -v codesign >/dev/null 2>&1; then
    log "==> Ad-hoc signing engine"
    codesign -s - "$ENGINE_DIR/target/release/longhouse-engine" >/dev/null
  fi

  ln -sf "$ENGINE_DIR/target/release/longhouse-engine" "$HOME/.local/bin/longhouse-engine"
  log "Engine ready at $HOME/.local/bin/longhouse-engine"
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
  run_repo_longhouse connect --status
  log ""
  log "==> Local health"
  run_repo_longhouse local-health
}

run_refresh() {
  local url
  local machine_name
  local app_bundle=""
  url="$(resolve_url)"
  machine_name="$(resolve_machine_name)"

  require_cmd uv
  require_cmd python3

  if (( SKIP_ENGINE == 0 )); then
    install_engine_from_source
  else
    log "==> Skipping engine rebuild"
  fi

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
  run_check
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
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $1"
      ;;
  esac
done

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
