#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
INSTALLER_MODE="local"
INSTALLER_URL="https://get.longhouse.ai/install.sh"
INSTALLER_SCRIPT=""
PACKAGE_SOURCE=""
UPGRADE_PACKAGE_SOURCE=""
EXPECTED_UPGRADE_VERSION=""
KEEP_HOME=0
PORT=""
DEMO_PORT=""
TEST_SHELL="${INSTALLER_TEST_SHELL:-${SHELL:-/bin/bash}}"
WORK_HOME=""
INSTALLER_TMP=""
ORIGINAL_PATH="${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"

usage() {
  cat <<'USAGE'
Usage: scripts/ci/installer-first-run.sh [options]

Disposable first-run smoke for the Longhouse installer.
Uses a temporary HOME so onboarding/install checks do not touch the real machine.

Options:
  --installer <local|remote>  Installer source (default: local)
  --installer-url <url>       Remote installer URL (default: https://get.longhouse.ai/install.sh)
  --pkg-source <path>         Package source for installer (default: server for local mode)
  --upgrade-pkg-source <path> Package source to use with `longhouse upgrade`
  --expected-upgrade-version <version>
                              Expected installed version after upgrade
  --shell <path>              Shell to simulate for PATH/profile checks (default: $SHELL)
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

build_local_health_app_bundle() {
  local bundle_root="$1"
  local package_path="$ROOT_DIR/desktop/LonghouseMenuBarHarness"
  local app_path="$bundle_root/Longhouse.app"
  local exec_path="$app_path/Contents/MacOS/Longhouse"

  require_cmd swift
  log "🍎 Building local Longhouse.app bundle for installer validation..." >&2
  swift build --package-path "$package_path" -c release --product LonghouseMenuBarHarnessMenuBar >/dev/null
  local menubar_bin_dir
  menubar_bin_dir="$(swift build --package-path "$package_path" -c release --show-bin-path)"

  rm -rf "$app_path"
  mkdir -p "$app_path/Contents/MacOS"
  cp "$menubar_bin_dir/LonghouseMenuBarHarnessMenuBar" "$exec_path"
  chmod +x "$exec_path"
  cat > "$app_path/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleExecutable</key>
  <string>Longhouse</string>
  <key>CFBundleIdentifier</key>
  <string>ai.longhouse.localhealth</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>Longhouse</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>0.0.0-dev</string>
  <key>CFBundleVersion</key>
  <string>0.0.0-dev</string>
  <key>LSUIElement</key>
  <true/>
</dict>
</plist>
PLIST

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

dump_debug() {
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
    --shell)
      TEST_SHELL="${2:-}"
      shift 2
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

if [[ -n "$PACKAGE_SOURCE" && "$INSTALLER_MODE" == "local" && -d "$PACKAGE_SOURCE" ]]; then
  require_cmd bun
  log "🏗️  Building frontend dist for local package install..."
  (
    cd "$ROOT_DIR"
    bun install --frozen-lockfile --silent
    cd web
    bun run build
  )

  if [[ ! -x "$ROOT_DIR/engine/target/release/longhouse-engine" ]]; then
    log "🦀 Building local engine binary for installer validation..."
    (
      cd "$ROOT_DIR/engine"
      cargo_build_release
    )
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

env_vars=(
  "HOME=$HOME"
  "PATH=$ORIGINAL_PATH"
  "SHELL=$TEST_SHELL"
  "LONGHOUSE_NO_WIZARD=1"
)

if [[ -n "$PACKAGE_SOURCE" ]]; then
  env_vars+=("LONGHOUSE_PKG_SOURCE=$PACKAGE_SOURCE")
fi

if [[ "$INSTALLER_MODE" == "local" ]]; then
  export LONGHOUSE_ENGINE_SOURCE="$ROOT_DIR/engine/target/release/longhouse-engine"
  env_vars+=("LONGHOUSE_ENGINE_SOURCE=$ROOT_DIR/engine/target/release/longhouse-engine")
fi

ENABLE_MENUBAR_SMOKE="${INSTALLER_TEST_MENUBAR:-}"
if [[ -z "$ENABLE_MENUBAR_SMOKE" && "$(uname -s)" == "Darwin" && -z "${CI:-}" ]]; then
  ENABLE_MENUBAR_SMOKE=1
fi

if [[ "$ENABLE_MENUBAR_SMOKE" == "1" && "$(uname -s)" == "Darwin" ]]; then
  APP_BUNDLE_STAGE_DIR="$HOME/.longhouse-app-build"
  LONGHOUSE_APP_BUNDLE="$(build_local_health_app_bundle "$APP_BUNDLE_STAGE_DIR")"
  export LONGHOUSE_LOCAL_HEALTH_APP_SOURCE="$LONGHOUSE_APP_BUNDLE"
  export LONGHOUSE_INSTALL_MENUBAR=1
  env_vars+=("LONGHOUSE_LOCAL_HEALTH_APP_SOURCE=$LONGHOUSE_APP_BUNDLE")
  env_vars+=("LONGHOUSE_INSTALL_MENUBAR=1")
fi

log "📦 Running installer..."
env "${env_vars[@]}" bash "$INSTALLER_SCRIPT"

export PATH="$HOME/.local/bin:$ORIGINAL_PATH"

if ! command -v longhouse >/dev/null 2>&1; then
  fail "longhouse not found after installer"
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

log "🧭 Running onboarding quickstart (safe mode)..."
ONBOARD_LOG="$(mktemp -t longhouse-onboard.XXXXXX.log)"
longhouse onboard --quick --no-demo --no-browser --port "$PORT" | tee "$ONBOARD_LOG"

if grep -q "\[WARN\] Test event failed" "$ONBOARD_LOG"; then
  fail "Onboarding verification emitted a test-event warning"
fi
rm -f "$ONBOARD_LOG"

log "🔌 Verifying local runtime install..."
if [[ -n "${CI:-}" ]]; then
  log "ℹ️  Skipping service-manager status assertion in CI."
else
  CONNECT_STATUS_LOG="$(mktemp -t longhouse-connect-status.XXXXXX.log)"
  longhouse connect --status | tee "$CONNECT_STATUS_LOG"
  if ! grep -q "Status: running" "$CONNECT_STATUS_LOG"; then
    fail "connect --status did not report a running engine service"
  fi
  if [[ "$ENABLE_MENUBAR_SMOKE" == "1" ]]; then
    if [[ "$(grep -c 'Status: running' "$CONNECT_STATUS_LOG")" -lt 2 ]]; then
      fail "connect --status did not report a running ambient menu bar service"
    fi
  fi
  rm -f "$CONNECT_STATUS_LOG"
fi

LOCAL_HEALTH_JSON="$(mktemp -t longhouse-local-health.XXXXXX.json)"
longhouse local-health --json > "$LOCAL_HEALTH_JSON"
python3 - "$LOCAL_HEALTH_JSON" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("schema_version") != 1:
    raise SystemExit("unexpected local-health schema version")
if payload.get("service", {}).get("status") not in {"running", "stopped", "not-installed"}:
    raise SystemExit("unexpected local-health service status")
PY
rm -f "$LOCAL_HEALTH_JSON"

log "🧪 Starting local server from onboarded config..."
longhouse serve --stop >/dev/null 2>&1 || true
if ! wait_for_down "http://127.0.0.1:${PORT}/api/readyz" "Onboarding server"; then
  fail "Onboarding server did not stop cleanly"
fi
RUNTIME_PORT="$(pick_port)"
longhouse serve --host 127.0.0.1 --port "$RUNTIME_PORT" --daemon

if ! wait_for_health "http://127.0.0.1:${RUNTIME_PORT}/api/readyz" "Onboarded local server"; then
  fail "Onboarding server did not become ready"
fi

log "🛑 Stopping onboarded server..."
longhouse serve --stop
if ! wait_for_down "http://127.0.0.1:${RUNTIME_PORT}/api/readyz" "Onboarded local server"; then
  fail "Onboarded local server did not stop cleanly"
fi

log "🎭 Starting demo server..."
longhouse serve --demo-fresh --host 127.0.0.1 --port "$DEMO_PORT" --daemon

if ! wait_for_health "http://127.0.0.1:${DEMO_PORT}/api/readyz" "Demo server"; then
  fail "Demo server did not become ready"
fi

log "🔐 Creating local device token for demo server..."
longhouse auth --url "http://127.0.0.1:${DEMO_PORT}" --device installer-smoke --force >/dev/null

TOKEN_PATH="$HOME/.claude/longhouse-device-token"
if [[ ! -s "$TOKEN_PATH" ]]; then
  fail "Device token file missing after local auth: $TOKEN_PATH"
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

log "🛑 Stopping demo server..."
longhouse serve --stop

log "✅ Installer first-run smoke passed"
