#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
WORK_ROOT=""
KEEP_WORK_ROOT=0
TEST_SHELL="${UPGRADE_TEST_SHELL:-${SHELL:-/bin/zsh}}"

usage() {
  cat <<'USAGE'
Usage: scripts/qa/test-cli-upgrade.sh [options]

Build two disposable Longhouse wheels and rehearse install -> upgrade in a temp HOME.
This is the canonical local QA loop for CLI/package upgrade work.

Options:
  --work-root <path>   Reuse an existing temp root (skip mktemp)
  --keep               Keep temp root after success/failure
  --shell <path>       Shell to simulate for installer PATH checks (default: $SHELL)
USAGE
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

ensure_frontend_dist() {
  if [[ -f "$ROOT_DIR/web/dist/index.html" ]]; then
    echo "♻️  Reusing existing web/dist for upgrade rehearsal"
    return 0
  fi

  require_cmd bun
  echo "🏗️  Building frontend dist for upgrade rehearsal"
  (
    cd "$ROOT_DIR"
    bun install --frozen-lockfile --silent
    cd web
    bun run build
  )
}

cleanup() {
  if [[ "$KEEP_WORK_ROOT" -eq 0 && -n "$WORK_ROOT" && -d "$WORK_ROOT" ]]; then
    rm -rf "$WORK_ROOT"
  fi
}
trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --work-root)
      WORK_ROOT="${2:-}"
      shift 2
      ;;
    --keep)
      KEEP_WORK_ROOT=1
      shift
      ;;
    --shell)
      TEST_SHELL="${2:-}"
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

require_cmd rsync
require_cmd perl
require_cmd uv
require_cmd python3

if [[ -z "$WORK_ROOT" ]]; then
  WORK_ROOT="$(mktemp -d -t longhouse-cli-upgrade-XXXXXX)"
fi

ensure_frontend_dist

CURRENT_VERSION="$(python3 - <<'PY'
import tomllib
from pathlib import Path
payload = tomllib.loads(Path("server/pyproject.toml").read_text(encoding="utf-8"))
print(payload["project"]["version"])
PY
)"

NEXT_VERSION="$(python3 - "$CURRENT_VERSION" <<'PY'
import sys
parts = sys.argv[1].split(".")
if not parts or not parts[-1].isdigit():
    raise SystemExit("Current version must end in a numeric patch segment")
parts[-1] = str(int(parts[-1]) + 1)
print(".".join(parts))
PY
)"

prepare_pkg() {
  local version="$1"
  local target="$WORK_ROOT/$version"
  local wheel_path=""
  mkdir -p \
    "$target/server" \
    "$target/web" \
    "$target/control-plane" \
    "$target/config" \
    "$target/desktop/LonghouseMenuBarHarness"
  rsync -a \
    --exclude '.uv_cache' \
    --exclude '.uv_tmp' \
    --exclude '__pycache__' \
    --exclude '.pytest_cache' \
    --exclude 'dist' \
    --exclude '.venv' \
    "$ROOT_DIR/server/" "$target/server/"
  cp -R "$ROOT_DIR/web/dist" "$target/web/dist"
  cp -R "$ROOT_DIR/control-plane/longhouse_shared" "$target/control-plane/longhouse_shared"
  cp "$ROOT_DIR/config/models.json" "$target/config/models.json"
  cp "$ROOT_DIR/desktop/LonghouseMenuBarHarness/Package.swift" "$target/desktop/LonghouseMenuBarHarness/Package.swift"
  cp -R "$ROOT_DIR/desktop/LonghouseMenuBarHarness/Sources" "$target/desktop/LonghouseMenuBarHarness/Sources"
  cp -R "$ROOT_DIR/desktop/LonghouseMenuBarHarness/Fixtures" "$target/desktop/LonghouseMenuBarHarness/Fixtures"
  perl -0pi -e 's/^version = "[^"]+"/version = "'"$version"'"/m' "$target/server/pyproject.toml"
  python3 "$ROOT_DIR/scripts/build/generate_build_identity.py" \
    --output "$target/.build/build-identity.json" \
    --pyproject-path "$target/server/pyproject.toml" \
    --skip-python-package \
    >/dev/null
  cp "$target/.build/build-identity.json" "$target/server/zerg/build_identity.json"
  (
    cd "$target/server"
    uv build --wheel >/dev/null
  )
  wheel_path="$(find "$target/server/dist" -maxdepth 1 -name 'longhouse-*.whl' | head -1)"
  if [[ -z "$wheel_path" ]]; then
    fail "Failed to build wheel for version $version"
  fi
  printf '%s\n' "$wheel_path"
}

OLD_WHEEL="$(prepare_pkg "$CURRENT_VERSION")"
NEW_WHEEL="$(prepare_pkg "$NEXT_VERSION")"

echo "🧪 CLI upgrade rehearsal"
echo "  current version: $CURRENT_VERSION"
echo "  next version:    $NEXT_VERSION"
echo "  work root:       $WORK_ROOT"
echo "  old wheel:       $OLD_WHEEL"
echo "  new wheel:       $NEW_WHEEL"

cd "$ROOT_DIR"
./scripts/ci/installer-first-run.sh \
  --installer local \
  --pkg-source "$OLD_WHEEL" \
  --upgrade-pkg-source "$NEW_WHEEL" \
  --expected-upgrade-version "$NEXT_VERSION" \
  --shell "$TEST_SHELL" \
  --keep-home \
  --home "$WORK_ROOT/home"

echo "✅ CLI upgrade rehearsal passed"
