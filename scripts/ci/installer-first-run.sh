#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
INSTALLER_MODE="local"
INSTALLER_URL="https://get.longhouse.ai/install.sh"
INSTALLER_SCRIPT=""
PACKAGE_SOURCE=""
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
  --pkg-source <path>         Package source for installer (default: apps/zerg/backend for local mode)
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
  PACKAGE_SOURCE="$ROOT_DIR/apps/zerg/backend"
fi

if [[ -n "$PACKAGE_SOURCE" && "$INSTALLER_MODE" == "local" ]]; then
  require_cmd bun
  log "🏗️  Building frontend dist for local package install..."
  (
    cd "$ROOT_DIR"
    bun install --frozen-lockfile --silent
    cd apps/zerg/frontend-web
    bun run build
  )
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

log "📦 Running installer..."
env "${env_vars[@]}" bash "$INSTALLER_SCRIPT"

export PATH="$HOME/.local/bin:$ORIGINAL_PATH"

if ! command -v longhouse >/dev/null 2>&1; then
  fail "longhouse not found after installer"
fi

PROFILE_PATH="$(profile_path_for_shell "$TEST_SHELL")"
if [[ ! -f "$PROFILE_PATH" ]]; then
  fail "Installer did not create shell profile: $PROFILE_PATH"
fi
if ! grep -q ".local/bin" "$PROFILE_PATH"; then
  fail "Installer profile missing ~/.local/bin export: $PROFILE_PATH"
fi

assert_fresh_shell_path "$PROFILE_PATH" "$TEST_SHELL"

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
SSH_CONNECTION="installer-smoke" longhouse onboard --quick --no-shipper --no-demo --port "$PORT"

if ! wait_for_health "http://127.0.0.1:${PORT}/api/health" "Onboarded local server"; then
  fail "Onboarding server did not become healthy"
fi

log "🛑 Stopping onboarded server..."
longhouse serve --stop

log "🎭 Starting demo server..."
longhouse serve --demo-fresh --host 127.0.0.1 --port "$DEMO_PORT" --daemon

if ! wait_for_health "http://127.0.0.1:${DEMO_PORT}/api/health" "Demo server"; then
  fail "Demo server did not become healthy"
fi

SESSIONS_JSON="$(mktemp -t longhouse-sessions.XXXXXX.json)"
curl -fsS "http://127.0.0.1:${DEMO_PORT}/api/agents/sessions" > "$SESSIONS_JSON"
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
