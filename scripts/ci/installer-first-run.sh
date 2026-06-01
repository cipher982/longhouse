#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
INSTALLER_MODE="local"
INSTALLER_URL="https://get.longhouse.ai/install.sh"
INSTALLER_SCRIPT=""
PACKAGE_SOURCE=""
UPGRADE_PACKAGE_SOURCE=""
EXPECTED_UPGRADE_VERSION=""
EXPECTED_BUILD_COMMIT="${INSTALLER_TEST_EXPECTED_BUILD_COMMIT:-}"
EXPECTED_BUILD_VERSION="${INSTALLER_TEST_EXPECTED_BUILD_VERSION:-}"
KEEP_HOME=0
PORT=""
DEMO_PORT=""
TEST_SHELL="${INSTALLER_TEST_SHELL:-${SHELL:-/bin/bash}}"
WORK_HOME=""
INSTALLER_TMP=""
ORIGINAL_PATH="${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"
ENABLE_MENUBAR_SMOKE="${INSTALLER_TEST_MENUBAR:-0}"
REBUILD_FRONTEND="${INSTALLER_TEST_REBUILD_FRONTEND:-0}"
ENABLE_E2E_BROWSER="${INSTALLER_TEST_E2E_BROWSER:-0}"
BUILD_WHEEL="${INSTALLER_TEST_WHEEL:-0}"
ENABLE_RUNTIME_ARTIFACT_SMOKE="${INSTALLER_TEST_RUNTIME_ARTIFACT_SMOKE:-0}"
BUILD_IDENTITY_GENERATED=0
DEBUG_DUMPED=0

usage() {
  cat <<'USAGE'
Usage: scripts/ci/installer-first-run.sh [options]

Disposable first-run smoke for the Longhouse installer.
Uses a temporary HOME for CLI/runtime state. On macOS, the canonical app install still targets /Applications.

Options:
  --installer <local|remote>  Installer source (default: local)
  --installer-url <url>       Remote installer URL (default: https://get.longhouse.ai/install.sh)
  --installer-onboard         Deprecated no-op; onboarding is always part of this smoke
  --pkg-source <path>         Package source for installer (default: server for local mode)
  --upgrade-pkg-source <path> Package source to use with `longhouse upgrade`
  --expected-upgrade-version <version>
                              Expected installed version after upgrade
  --expected-build-commit <sha>
                              Expected `longhouse version --json` build.commit after install
  --expected-build-version <version>
                              Expected `longhouse version --json` build.version after install
  --shell <path>              Shell to simulate for PATH/profile checks (default: $SHELL)
  --menubar                   Enable macOS ambient menu bar smoke (heavy; explicit opt-in)
  --rebuild-frontend          Force a fresh web/dist build for local package installs
  --e2e-browser               Run Playwright browser verification after demo seed
  --wheel                     Build a wheel from server/ and install from it (tests real package)
  --runtime-artifact-smoke    Force-install released runtime artifacts via the repo smoke helper
  --port <port>               Fixed port for onboarding/demo server
  --demo-port <port>          Fixed port for demo server (default: random free port)
  --home <path>               Reuse an existing temp HOME (skip mktemp)
  --keep-home                 Keep temp HOME after success/failure
USAGE
}

log() {
  printf '%s\n' "$*"
}

fail() {
  printf '❌ %s\n' "$*" >&2
  if declare -F dump_debug >/dev/null 2>&1; then
    dump_debug >&2 || true
  fi
  exit 1
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    fail "Missing required command: $1"
  fi
}

cargo_build_release() {
  if command -v cargo >/dev/null 2>&1; then
    if cargo --version >/dev/null 2>&1; then
      cargo build --release "$@"
      return 0
    fi
    if cargo +stable --version >/dev/null 2>&1; then
      cargo +stable build --release "$@"
      return 0
    fi
  fi

  if command -v rustup >/dev/null 2>&1 && rustup run stable cargo --version >/dev/null 2>&1; then
    rustup run stable cargo build --release "$@"
    return 0
  fi

  fail "Rust toolchain unavailable for cargo build --release"
}

ensure_frontend_dist() {
  if [[ "$REBUILD_FRONTEND" != "1" && -f "$ROOT_DIR/web/dist/index.html" ]]; then
    log "♻️  Reusing existing web/dist for installer validation..."
    return 0
  fi

  require_cmd bun
  if [[ "$REBUILD_FRONTEND" == "1" ]]; then
    log "🏗️  Rebuilding frontend dist for installer validation..."
  else
    log "🏗️  Building frontend dist for installer validation..."
  fi
  (
    cd "$ROOT_DIR"
    bun install --frozen-lockfile --silent
    cd web
    bun run build
  )
}

ensure_build_identity() {
  if [[ "$BUILD_IDENTITY_GENERATED" == "1" ]]; then
    return 0
  fi
  log "🔖 Generating build identity..."
  python3 "$ROOT_DIR/scripts/build/generate_build_identity.py" >/dev/null
  BUILD_IDENTITY_GENERATED=1
}

build_desktop_app_bundle() {
  local bundle_root="$1"
  local package_path="$ROOT_DIR/desktop/LonghouseMenuBarHarness"
  local app_path="$bundle_root/Longhouse.app"
  local menubar_binary=""

  require_cmd swift
  log "🍎 Building local Longhouse.app bundle for installer validation..." >&2
  menubar_binary="$("$ROOT_DIR/scripts/resolve-swift-product-path.sh" \
    --package-path "$package_path" \
    --product LonghouseMenuBarHarnessMenuBar \
    --configuration release)"

  "$ROOT_DIR/scripts/release/macos-package-app.sh" \
    --binary "$menubar_binary" \
    --app-name Longhouse \
    --exec-name Longhouse \
    --bundle-id ai.longhouse.app \
    --version 0.0.0-dev \
    --short-version 0.0.0-dev \
    --output-dir "$bundle_root" \
    --icon-png "$ROOT_DIR/web/public/favicon-512.png" \
    --lsuielement true >/dev/null

  if [[ ! -d "$app_path" ]]; then
    fail "Longhouse.app bundle was not created: $app_path"
  fi

  printf '%s\n' "$app_path"
}

pick_port() {
  python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("", 0))
print(s.getsockname()[1])
s.close()
PY
}

profile_path_for_shell() {
  local shell_name
  shell_name="$(basename "$1")"

  case "$shell_name" in
    bash)
      if [[ "$(uname -s)" == "Darwin" ]]; then
        printf '%s\n' "$HOME/.bash_profile"
      else
        printf '%s\n' "$HOME/.bashrc"
      fi
      ;;
    zsh)
      printf '%s\n' "$HOME/.zshrc"
      ;;
    fish)
      printf '%s\n' "$HOME/.config/fish/config.fish"
      ;;
    *)
      fail "Unsupported shell for installer smoke: $1"
      ;;
  esac
}

wait_for_health() {
  local url="$1"
  local name="$2"
  local attempts="${3:-60}"

  for _ in $(seq 1 "$attempts"); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      log "✅ $name healthy: $url"
      return 0
    fi
    sleep 1
  done

  return 1
}

onboard_supports_flag() {
  local flag="$1"
  longhouse onboard --help 2>/dev/null | grep -Fq -- "$flag"
}

run_onboard_quickstart() {
  local log_path="$1"
  local -a cmd=(longhouse onboard --topology local --no-browser --port "$PORT")

  log "🧭 Onboarding command: ${cmd[*]}"
  "${cmd[@]}" | tee "$log_path"
}

resolve_device_token_path() {
  local candidate=""
  for candidate in \
    "$HOME/.longhouse/machine/device-token" \
    "$HOME/.claude/longhouse-device-token"
  do
    if [[ -s "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

wait_for_down() {
  local url="$1"
  local name="$2"
  local attempts="${3:-30}"

  for _ in $(seq 1 "$attempts"); do
    if ! curl -fsS "$url" >/dev/null 2>&1; then
      log "✅ $name stopped: $url"
      return 0
    fi
    sleep 1
  done

  return 1
}

server_pid_file() {
  printf '%s\n' "$HOME/.longhouse/server.pid"
}

wait_for_server_pid_clear() {
  local name="$1"
  local attempts="${2:-120}"
  local pid_file=""
  local pid=""

  pid_file="$(server_pid_file)"

  for _ in $(seq 1 "$attempts"); do
    if [[ ! -f "$pid_file" ]]; then
      log "✅ $name daemon PID cleared"
      return 0
    fi

    pid="$(tr -d '[:space:]' < "$pid_file" 2>/dev/null || true)"
    if [[ -z "$pid" || ! "$pid" =~ ^[0-9]+$ ]]; then
      rm -f "$pid_file"
      log "✅ $name daemon PID cleared"
      return 0
    fi

    if ! kill -0 "$pid" >/dev/null 2>&1; then
      rm -f "$pid_file"
      log "✅ $name daemon PID cleared"
      return 0
    fi

    sleep 0.5
  done

  return 1
}

stop_longhouse_server() {
  local name="$1"
  local url="$2"
  local attempts="${3:-30}"
  local stop_log=""

  stop_log="$(mktemp -t longhouse-stop.XXXXXX.log)"
  if ! longhouse serve --stop >"$stop_log" 2>&1; then
    log "ℹ️  $name stop command needed extra wait; polling PID state..."
  fi

  if ! wait_for_down "$url" "$name" "$attempts"; then
    if [[ -s "$stop_log" ]]; then
      log "Stop output for $name:"
      cat "$stop_log"
    fi
    rm -f "$stop_log"
    fail "$name did not stop cleanly"
  fi

  if ! wait_for_server_pid_clear "$name"; then
    if [[ -s "$stop_log" ]]; then
      log "Stop output for $name:"
      cat "$stop_log"
    fi
    rm -f "$stop_log"
    fail "$name daemon PID did not clear cleanly"
  fi

  rm -f "$stop_log"
}

connect_status_value() {
  local target="$1"
  local output="$2"

  awk -v target="$target" '
    /^(Service|Machine Agent): / { section = "service"; next }
    /^(Ambient UI|Desktop App): / { section = "ambient"; next }
    /^Status: / && section == target { print $2; exit }
  ' <<< "$output"
}

wait_for_connect_services() {
  local expect_ambient="${1:-0}"
  local attempts="${2:-30}"
  local delay_secs="${3:-2}"
  local output=""
  local engine_status=""
  local ambient_status=""

  for _ in $(seq 1 "$attempts"); do
    output="$(longhouse connect --status)"
    engine_status="$(connect_status_value service "$output")"
    ambient_status="$(connect_status_value ambient "$output")"

    if [[ "$engine_status" == "running" ]]; then
      if [[ "$expect_ambient" != "1" || "$ambient_status" == "running" ]]; then
        printf '%s\n' "$output"
        return 0
      fi
    fi

    sleep "$delay_secs"
  done

  printf '%s\n' "$output"
  return 1
}

validate_doctor_json() {
  local path="$1"
  python3 - "$path" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(payload, dict):
    raise SystemExit("doctor output is not a JSON object")
if not isinstance(payload.get("sections"), dict):
    raise SystemExit("doctor output missing sections")
if not isinstance(payload.get("summary"), dict):
    raise SystemExit("doctor output missing summary")
PY
}

extract_version_from_wheel_path() {
  local source_path="$1"
  python3 - "$source_path" <<'PY'
import re
import sys
from pathlib import Path

name = Path(sys.argv[1]).name
match = re.match(r"longhouse-([0-9][^-]*)-", name)
if match:
    print(match.group(1))
PY
}

validate_install_metadata() {
  local path="$1"
  local expected_source="${2:-}"
  local expected_version="${3:-}"
  python3 - "$path" "$expected_source" "$expected_version" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected_source = sys.argv[2]
expected_version = sys.argv[3]

if payload.get("install_method") != "uv":
    raise SystemExit("install metadata missing uv install_method")
if not payload.get("installed_version"):
    raise SystemExit("install metadata missing installed_version")
if expected_source and payload.get("install_source") != expected_source:
    raise SystemExit(f"install source mismatch: expected {expected_source}, got {payload.get('install_source')}")
if expected_version and payload.get("installed_version") != expected_version:
    raise SystemExit(
        f"installed version mismatch: expected {expected_version}, got {payload.get('installed_version')}"
    )
PY
}

validate_runtime_artifact_json() {
  local path="$1"
  local expected_component="$2"
  python3 - "$path" "$expected_component" <<'PY'
import json
import os
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected_component = sys.argv[2]
if payload.get("component") != expected_component:
    raise SystemExit(f"runtime artifact component mismatch: expected {expected_component}, got {payload.get('component')}")
artifact_path = Path(payload.get("path") or "")
launch_path = Path(payload.get("launch_path") or "")
if not artifact_path.exists():
    raise SystemExit(f"runtime artifact path missing: {artifact_path}")
if not launch_path.exists():
    raise SystemExit(f"runtime artifact launch path missing: {launch_path}")
if expected_component == "engine" and not os.access(launch_path, os.X_OK):
    raise SystemExit(f"engine launch path is not executable: {launch_path}")
PY
}

runtime_artifact_launch_path() {
  local path="$1"
  python3 - "$path" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(payload["launch_path"])
PY
}

smoke_runtime_artifact() {
  local component="$1"
  local artifact_home=""
  local artifact_json=""

  artifact_home="$(mktemp -d -t longhouse-runtime-home.XXXXXX)"
  artifact_json="$(mktemp -t longhouse-runtime-artifact.XXXXXX.json)"

  (
    local launch_path=""
    trap 'rm -f "$artifact_json"; rm -rf "$artifact_home"' EXIT

    cd "$ROOT_DIR/server"
    HOME="$artifact_home" uv run python ../scripts/ci/runtime-artifact-smoke.py --component "$component" --overwrite --json > "$artifact_json"
    validate_runtime_artifact_json "$artifact_json" "$component"
    launch_path="$(runtime_artifact_launch_path "$artifact_json")"

    if [[ "$component" == "engine" ]]; then
      if ! "$launch_path" --help >/dev/null 2>&1; then
        fail "Released engine binary was installed but did not respond to --help"
      fi
    fi
  )
}

assert_fresh_shell_path() {
  local profile_path="$1"
  local shell_bin="$2"
  local shell_name fresh_cmd resolved
  shell_name="$(basename "$shell_bin")"

  case "$shell_name" in
    fish)
      resolved=$(HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
        "$shell_bin" -c 'source $argv[1] >/dev/null 2>&1; and command -v longhouse' \
        -- "$profile_path" 2>/dev/null || true)
      ;;
    bash|zsh)
      fresh_cmd='source "$1" >/dev/null 2>&1 && command -v longhouse'
      resolved=$(HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
        "$shell_bin" -i -c "$fresh_cmd" _ "$profile_path" 2>/dev/null || true)
      ;;
    *)
      fail "Unsupported shell for fresh-path check: $shell_bin"
      ;;
  esac

  if [[ -z "$resolved" ]]; then
    fail "Fresh shell PATH check failed for $shell_bin"
  fi

  log "✅ Fresh shell resolves longhouse via $resolved"
}

verify_remote_installer_sync() {
  local sync_script="$ROOT_DIR/scripts/ci/check-public-installer-sync.sh"

  if [[ ! -x "$sync_script" ]]; then
    fail "Remote installer sync helper missing or not executable: $sync_script"
  fi

  log "🔎 Verifying published installer sync..."
  "$sync_script" --url "$INSTALLER_URL"
}

dump_debug() {
  if [[ "$DEBUG_DUMPED" == "1" ]]; then
    return
  fi
  DEBUG_DUMPED=1

  if [[ -z "$HOME" || ! -d "$HOME" ]]; then
    return
  fi

  log ""
  log "----- installer-first-run debug -----"
  log "HOME=$HOME"
  if [[ -f "$HOME/.longhouse/server.log" ]]; then
    log "server.log:"
    tail -n 120 "$HOME/.longhouse/server.log" || true
  fi
  if [[ -f "$HOME/.longhouse/demo.db" ]]; then
    log "demo.db exists: $HOME/.longhouse/demo.db"
  fi
  if [[ -d "$HOME/.longhouse" ]]; then
    log ".longhouse contents:"
    find "$HOME/.longhouse" -maxdepth 2 -type f | sort || true
  fi
}

cleanup() {
  if [[ -n "${HOME:-}" && -d "${HOME:-}" ]]; then
    PATH="$HOME/.local/bin:$ORIGINAL_PATH" longhouse serve --stop >/dev/null 2>&1 || true
    PATH="$HOME/.local/bin:$ORIGINAL_PATH" longhouse connect --uninstall >/dev/null 2>&1 || true
  fi

  if [[ -n "$INSTALLER_TMP" && -f "$INSTALLER_TMP" ]]; then
    rm -f "$INSTALLER_TMP"
  fi

  if [[ "$KEEP_HOME" -eq 0 && -n "$WORK_HOME" && -d "$WORK_HOME" ]]; then
    rm -rf "$WORK_HOME"
  fi
}
trap cleanup EXIT
trap 'dump_debug' ERR

while [[ $# -gt 0 ]]; do
  case "$1" in
    --installer)
      INSTALLER_MODE="${2:-}"
      shift 2
      ;;
    --installer-url)
      INSTALLER_URL="${2:-}"
      shift 2
      ;;
    --installer-onboard)
      shift
      ;;
    --pkg-source)
      PACKAGE_SOURCE="${2:-}"
      shift 2
      ;;
    --upgrade-pkg-source)
      UPGRADE_PACKAGE_SOURCE="${2:-}"
      shift 2
      ;;
    --expected-upgrade-version)
      EXPECTED_UPGRADE_VERSION="${2:-}"
      shift 2
      ;;
    --expected-build-commit)
      EXPECTED_BUILD_COMMIT="${2:-}"
      shift 2
      ;;
    --expected-build-version)
      EXPECTED_BUILD_VERSION="${2:-}"
      shift 2
      ;;
    --shell)
      TEST_SHELL="${2:-}"
      shift 2
      ;;
    --menubar)
      ENABLE_MENUBAR_SMOKE=1
      shift
      ;;
    --rebuild-frontend)
      REBUILD_FRONTEND=1
      shift
      ;;
    --e2e-browser)
      ENABLE_E2E_BROWSER=1
      shift
      ;;
    --wheel)
      BUILD_WHEEL=1
      shift
      ;;
    --runtime-artifact-smoke)
      ENABLE_RUNTIME_ARTIFACT_SMOKE=1
      shift
      ;;
    --port)
      PORT="${2:-}"
      shift 2
      ;;
    --demo-port)
      DEMO_PORT="${2:-}"
      shift 2
      ;;
    --home)
      WORK_HOME="${2:-}"
      shift 2
      ;;
    --keep-home)
      KEEP_HOME=1
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

case "$INSTALLER_MODE" in
  local|remote) ;;
  *)
    fail "--installer must be local or remote"
    ;;
esac

require_cmd bash
require_cmd curl
require_cmd python3
require_cmd uv

if [[ ! -x "$TEST_SHELL" ]]; then
  fail "Shell not executable: $TEST_SHELL"
fi

if [[ -z "$WORK_HOME" ]]; then
  WORK_HOME="$(mktemp -d -t longhouse-install-smoke-XXXXXX)"
fi

export HOME="$WORK_HOME"

if [[ -z "$PORT" ]]; then
  PORT="$(pick_port)"
fi

if [[ -z "$DEMO_PORT" ]]; then
  DEMO_PORT="$(pick_port)"
fi

if [[ -z "$PACKAGE_SOURCE" && "$INSTALLER_MODE" == "local" ]]; then
  PACKAGE_SOURCE="$ROOT_DIR/server"
fi

if [[ "$BUILD_WHEEL" == "1" && "$INSTALLER_MODE" == "local" ]]; then
  # Build a wheel from server/ — tests the same artifact real users get from PyPI.
  # Frontend must be built first so it gets bundled into the wheel.
  ensure_frontend_dist

  require_cmd uv
  ensure_build_identity
  log "📦 Building wheel from server/ ..."
  (
    cd "$ROOT_DIR/server"
    rm -rf dist
    uv build --wheel --quiet
  )
  WHEEL_PATH="$(ls -1 "$ROOT_DIR/server/dist"/longhouse-*.whl 2>/dev/null | head -1)"
  if [[ -z "$WHEEL_PATH" || ! -f "$WHEEL_PATH" ]]; then
    fail "Wheel build produced no output in server/dist/"
  fi
  log "📦 Built wheel: $(basename "$WHEEL_PATH")"
  PACKAGE_SOURCE="$WHEEL_PATH"
fi

if [[ -z "$EXPECTED_BUILD_COMMIT" && "$INSTALLER_MODE" == "local" ]]; then
  EXPECTED_BUILD_COMMIT="$(git -C "$ROOT_DIR" rev-parse HEAD)"
fi

if [[ -z "$EXPECTED_BUILD_VERSION" && "$INSTALLER_MODE" == "local" ]]; then
  EXPECTED_BUILD_VERSION="$(
    python3 - "$ROOT_DIR/server/pyproject.toml" <<'PY'
import re
import sys
from pathlib import Path

match = re.search(r'^version\s*=\s*"([^"]+)"', Path(sys.argv[1]).read_text(), re.MULTILINE)
if not match:
    raise SystemExit("server/pyproject.toml has no version line")
print(match.group(1))
PY
  )"
fi

if [[ -n "$PACKAGE_SOURCE" && "$INSTALLER_MODE" == "local" && -d "$PACKAGE_SOURCE" ]]; then
  # Directory source: build frontend for bundling at install time.
  ensure_frontend_dist
fi

# Build engine binary if in local mode and it doesn't already exist.
# The engine is independent of the package source (wheel or directory).
if [[ "$INSTALLER_MODE" == "local" ]]; then
  ensure_build_identity
  if [[ ! -x "$ROOT_DIR/engine/target/release/longhouse-engine" ]]; then
    if command -v cargo >/dev/null 2>&1 || command -v rustup >/dev/null 2>&1; then
      log "🦀 Building local engine binary for installer validation..."
      (
        cd "$ROOT_DIR/engine"
        cargo_build_release
      )
    else
      log "ℹ️  Rust toolchain not available — skipping engine build"
    fi
  else
    log "🦀 Reusing existing local engine binary for installer validation..."
  fi
fi

if [[ -n "$UPGRADE_PACKAGE_SOURCE" && -z "$EXPECTED_UPGRADE_VERSION" ]]; then
  EXPECTED_UPGRADE_VERSION="$(extract_version_from_wheel_path "$UPGRADE_PACKAGE_SOURCE" || true)"
fi

case "$INSTALLER_MODE" in
  local)
    INSTALLER_SCRIPT="$ROOT_DIR/scripts/install.sh"
    ;;
  remote)
    verify_remote_installer_sync
    INSTALLER_TMP="$(mktemp -t longhouse-installer.XXXXXX.sh)"
    curl -fsSL "$INSTALLER_URL" -o "$INSTALLER_TMP"
    chmod +x "$INSTALLER_TMP"
    INSTALLER_SCRIPT="$INSTALLER_TMP"
    ;;
esac

log "🚦 Installer smoke"
log "  mode: $INSTALLER_MODE"
log "  shell: $TEST_SHELL"
log "  home: $HOME"
log "  onboarding port: $PORT"
log "  demo port: $DEMO_PORT"
log "  menubar smoke: $ENABLE_MENUBAR_SMOKE"
log "  frontend rebuild: $REBUILD_FRONTEND"
log "  e2e browser: $ENABLE_E2E_BROWSER"
log "  wheel install: $BUILD_WHEEL"
log "  runtime artifact smoke: $ENABLE_RUNTIME_ARTIFACT_SMOKE"
log "  expected build commit: ${EXPECTED_BUILD_COMMIT:-<not checked>}"
log "  expected build version: ${EXPECTED_BUILD_VERSION:-<not checked>}"

EXPECT_SERVICE_INSTALL=0

env_vars=(
  "HOME=$HOME"
  "PATH=$ORIGINAL_PATH"
  "SHELL=$TEST_SHELL"
)

if [[ -n "$PACKAGE_SOURCE" ]]; then
  env_vars+=("LONGHOUSE_PKG_SOURCE=$PACKAGE_SOURCE")
fi

if [[ "$INSTALLER_MODE" == "local" && -x "$ROOT_DIR/engine/target/release/longhouse-engine" ]]; then
  export LONGHOUSE_ENGINE_SOURCE="$ROOT_DIR/engine/target/release/longhouse-engine"
  env_vars+=("LONGHOUSE_ENGINE_SOURCE=$ROOT_DIR/engine/target/release/longhouse-engine")
fi

if [[ "$ENABLE_MENUBAR_SMOKE" == "1" && "$(uname -s)" != "Darwin" ]]; then
  fail "--menubar smoke is only supported on macOS"
fi

if [[ "$(uname -s)" == "Darwin" ]]; then
  log "🍎 macOS installer smoke uses a temp HOME but still installs Longhouse.app into /Applications."
  if [[ "$ENABLE_MENUBAR_SMOKE" == "1" ]]; then
    log "⚠️  macOS ambient smoke enabled. This is the heavy local path; prefer GitHub Actions unless you are debugging menu bar install behavior."
  fi
  APP_BUNDLE_STAGE_DIR="$HOME/.longhouse-app-build"
  LONGHOUSE_APP_BUNDLE="$(build_desktop_app_bundle "$APP_BUNDLE_STAGE_DIR")"
  export LONGHOUSE_DESKTOP_APP_SOURCE="$LONGHOUSE_APP_BUNDLE"
  env_vars+=("LONGHOUSE_DESKTOP_APP_SOURCE=$LONGHOUSE_APP_BUNDLE")
  if [[ "$ENABLE_MENUBAR_SMOKE" == "1" ]]; then
    export LONGHOUSE_INSTALL_MENUBAR=1
    export LONGHOUSE_INSTALL_SERVICES_IN_CI=1
    env_vars+=("LONGHOUSE_INSTALL_MENUBAR=1")
    env_vars+=("LONGHOUSE_INSTALL_SERVICES_IN_CI=1")
    EXPECT_SERVICE_INSTALL=1
  fi
fi

log "📦 Running installer..."
env "${env_vars[@]}" bash "$INSTALLER_SCRIPT"

export PATH="$HOME/.local/bin:$ORIGINAL_PATH"

if ! command -v longhouse >/dev/null 2>&1; then
  fail "longhouse not found after installer"
fi

if [[ "$(uname -s)" == "Darwin" && ! -d "/Applications/Longhouse.app" ]]; then
  fail "Installer did not install Longhouse.app into /Applications"
fi

INSTALL_METADATA_PATH="$HOME/.longhouse/install.json"
if [[ ! -f "$INSTALL_METADATA_PATH" ]]; then
  fail "Installer did not create install metadata: $INSTALL_METADATA_PATH"
fi
validate_install_metadata "$INSTALL_METADATA_PATH" "$([[ -n "$PACKAGE_SOURCE" && "$PACKAGE_SOURCE" != "longhouse" ]] && echo custom || echo pypi)"

PROFILE_PATH="$(profile_path_for_shell "$TEST_SHELL")"
if [[ ! -f "$PROFILE_PATH" ]]; then
  fail "Installer did not create shell profile: $PROFILE_PATH"
fi
if ! grep -q ".local/bin" "$PROFILE_PATH"; then
  fail "Installer profile missing ~/.local/bin export: $PROFILE_PATH"
fi

assert_fresh_shell_path "$PROFILE_PATH" "$TEST_SHELL"

log "🏷️  Verifying version command..."
longhouse version >/dev/null
if [[ -n "$EXPECTED_BUILD_COMMIT" ]]; then
  identity_args=(
    --expected-commit "$EXPECTED_BUILD_COMMIT"
  )
  if [[ -n "$EXPECTED_BUILD_VERSION" ]]; then
    identity_args+=(--expected-version "$EXPECTED_BUILD_VERSION")
  fi
  "$ROOT_DIR/scripts/ci/assert-installed-build-identity.py" "${identity_args[@]}"
fi

if [[ -n "$UPGRADE_PACKAGE_SOURCE" ]]; then
  log "⬆️  Running CLI upgrade from override package source..."
  longhouse upgrade --package-source "$UPGRADE_PACKAGE_SOURCE"
  validate_install_metadata "$INSTALL_METADATA_PATH" "custom" "$EXPECTED_UPGRADE_VERSION"
fi

log "🩺 Running doctor..."
DOCTOR_JSON="$(mktemp -t longhouse-doctor.XXXXXX.json)"
DOCTOR_STATUS=0
if ! longhouse doctor --json > "$DOCTOR_JSON"; then
  DOCTOR_STATUS=$?
fi
validate_doctor_json "$DOCTOR_JSON"
if [[ "$DOCTOR_STATUS" -ne 0 ]]; then
  log "ℹ️  Doctor reported expected pre-onboarding findings (exit $DOCTOR_STATUS)"
fi
rm -f "$DOCTOR_JSON"

if [[ "$ENABLE_RUNTIME_ARTIFACT_SMOKE" == "1" ]]; then
  log "🧱 Verifying released runtime artifacts..."
  smoke_runtime_artifact engine
  if [[ "$(uname -s)" == "Darwin" ]]; then
    smoke_runtime_artifact desktop-app
  fi
fi

log "🧭 Running local quickstart..."
ONBOARD_LOG="$(mktemp -t longhouse-onboard.XXXXXX.log)"
run_onboard_quickstart "$ONBOARD_LOG"
rm -f "$ONBOARD_LOG"

log "🔌 Verifying local runtime install..."
if [[ "$EXPECT_SERVICE_INSTALL" -eq 1 || -z "${CI:-}" ]]; then
  if ! CONNECT_STATUS_OUTPUT="$(wait_for_connect_services "$ENABLE_MENUBAR_SMOKE")"; then
    printf '%s\n' "$CONNECT_STATUS_OUTPUT"
    fail "connect --status did not settle to the expected running services"
  fi
  printf '%s\n' "$CONNECT_STATUS_OUTPUT"
elif [[ -n "${CI:-}" ]]; then
  log "ℹ️  Skipping service-manager status assertion in CI."
fi

DESKTOP_STATUS_JSON="$(mktemp -t longhouse-status.XXXXXX.json)"
longhouse local-health --json > "$DESKTOP_STATUS_JSON"
python3 - "$DESKTOP_STATUS_JSON" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("schema_version") != 1:
    raise SystemExit("unexpected local status schema version")
if payload.get("service", {}).get("status") not in {"running", "stopped", "not-installed"}:
    raise SystemExit("unexpected local status service state")
PY
rm -f "$DESKTOP_STATUS_JSON"

log "🧪 Starting local server from onboarded config..."
stop_longhouse_server "Onboarding server" "http://127.0.0.1:${PORT}/api/readyz"
RUNTIME_PORT="$(pick_port)"
longhouse serve --host 127.0.0.1 --port "$RUNTIME_PORT" --daemon

if ! wait_for_health "http://127.0.0.1:${RUNTIME_PORT}/api/readyz" "Onboarded local server"; then
  fail "Onboarding server did not become ready"
fi

log "🛑 Stopping onboarded server..."
stop_longhouse_server "Onboarded local server" "http://127.0.0.1:${RUNTIME_PORT}/api/readyz"

log "🎭 Starting demo server..."
if [[ "$ENABLE_E2E_BROWSER" == "1" ]]; then
  AUTH_DISABLED=1 longhouse serve --demo-fresh --host 127.0.0.1 --port "$DEMO_PORT" --daemon
else
  longhouse serve --demo-fresh --host 127.0.0.1 --port "$DEMO_PORT" --daemon
fi

if ! wait_for_health "http://127.0.0.1:${DEMO_PORT}/api/readyz" "Demo server"; then
  fail "Demo server did not become ready"
fi

log "🔐 Creating local device token for demo server..."
longhouse auth --url "http://127.0.0.1:${DEMO_PORT}" --device installer-smoke --force >/dev/null

if ! TOKEN_PATH="$(resolve_device_token_path)"; then
  fail "Device token file missing after local auth in supported locations (~/.longhouse/machine/device-token or ~/.claude/longhouse-device-token)"
fi
DEMO_DEVICE_TOKEN="$(tr -d '\n' < "$TOKEN_PATH")"

SESSIONS_JSON="$(mktemp -t longhouse-sessions.XXXXXX.json)"
curl -fsS \
  -H "X-Agents-Token: $DEMO_DEVICE_TOKEN" \
  "http://127.0.0.1:${DEMO_PORT}/api/agents/sessions" > "$SESSIONS_JSON"
python3 - "$SESSIONS_JSON" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if isinstance(payload, dict):
    sessions = payload.get("sessions")
elif isinstance(payload, list):
    sessions = payload
else:
    raise SystemExit("unexpected sessions payload")

if sessions is None:
    raise SystemExit("sessions payload missing 'sessions'")
PY
rm -f "$SESSIONS_JSON"

if [[ "$ENABLE_E2E_BROWSER" == "1" ]]; then
  log "🎭 Running browser E2E verification..."
  if ! command -v bunx >/dev/null 2>&1; then
    fail "bunx not found — required for --e2e-browser"
  fi
  if ! (cd "$ROOT_DIR" && bun install --frozen-lockfile --silent); then
    fail "Failed to install E2E Node dependencies"
  fi
  if ! (cd "$ROOT_DIR/e2e" && bunx playwright install --with-deps chromium 2>/dev/null); then
    fail "Failed to install Playwright chromium"
  fi
  if ! (cd "$ROOT_DIR/e2e" && bunx tsx scripts/verify-installer-browser.ts --url "http://127.0.0.1:${DEMO_PORT}"); then
    fail "Browser verification failed: timeline did not render demo sessions"
  fi
  log "✅ Browser E2E verification passed"
fi

log "🛑 Stopping demo server..."
stop_longhouse_server "Demo server" "http://127.0.0.1:${DEMO_PORT}/api/readyz"

log "✅ Installer first-run smoke passed"
