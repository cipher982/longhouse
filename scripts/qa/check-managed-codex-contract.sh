#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${MANAGED_CODEX_CONTRACT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

failed=0
MATCHES_FILE="$(mktemp "${TMPDIR:-/tmp}/managed-codex-contract.XXXXXX")"

scan_forbidden_pattern() {
  local label="$1"
  local pattern="$2"
  shift 2

  local paths=()
  local rel_path
  for rel_path in "$@"; do
    if [[ -e "$ROOT_DIR/$rel_path" ]]; then
      paths+=("$ROOT_DIR/$rel_path")
    fi
  done

  if [[ "${#paths[@]}" -eq 0 ]]; then
    return 0
  fi

  if rg -n --hidden \
    --glob '!.git' \
    --glob '!**/check-managed-codex-contract.sh' \
    --glob '!**/managed-codex-contract.test.py' \
    -- "$pattern" "${paths[@]}" >"$MATCHES_FILE"; then
    echo "Forbidden managed Codex contract reference matched: $label" >&2
    cat "$MATCHES_FILE" >&2
    failed=1
  fi
}

require_pattern() {
  local label="$1"
  local pattern="$2"
  local rel_path="$3"

  if [[ ! -e "$ROOT_DIR/$rel_path" ]]; then
    echo "Required managed Codex contract file is missing: $rel_path" >&2
    failed=1
    return 0
  fi

  if ! rg -n --hidden -- "$pattern" "$ROOT_DIR/$rel_path" >"$MATCHES_FILE"; then
    echo "Required managed Codex contract reference is missing: $label" >&2
    failed=1
  fi
}

for path in \
  "scripts/release/download-managed-codex.sh" \
  "scripts/release/build-managed-codex.sh" \
  "scripts/release/managed-codex.patch"
do
  if [[ -e "$ROOT_DIR/$path" ]]; then
    echo "Forbidden managed Codex packaging artifact exists: $path" >&2
    failed=1
  fi
done

scan_forbidden_pattern "packaged Codex source selector" \
  "LONGHOUSE_CODEX_SOURCE|--codex-source|managed-codex\\.patch|download-managed-codex|build-managed-codex|RuntimeComponent\\.MANAGED_CODEX" \
  "engine" \
  "server/zerg" \
  "scripts"

scan_forbidden_pattern "legacy Codex start-thread flag" \
  "--start-thread|start_thread" \
  "engine/src" \
  "server/zerg/cli/codex.py" \
  "scripts/qa" \
  "scripts/ci"

scan_forbidden_pattern "detached-ui writers must not persist legacy headless state" \
  "PERSISTED_DETACHED_UI_LAUNCH_MODE[[:space:]]*:.*LEGACY_LAUNCH_MODE_HEADLESS|launch_mode[[:space:]]*:[[:space:]]*Some\\([^)]*LEGACY_LAUNCH_MODE_HEADLESS|launch_mode[[:space:]]*:[[:space:]]*Some\\([^)]*\"headless\"" \
  "engine/src/codex_bridge.rs"

require_pattern "detached-ui writer persists detached_ui, not legacy headless" \
  "pub const PERSISTED_DETACHED_UI_LAUNCH_MODE: &str = LAUNCH_MODE_DETACHED_UI;" \
  "engine/src/codex_bridge.rs"

rm -f "$MATCHES_FILE"

if [[ "$failed" -ne 0 ]]; then
  echo "Managed Codex must use stock upstream codex from PATH, create threads explicitly, and persist detached-ui managed sessions as detached_ui." >&2
  exit 1
fi

echo "managed Codex contract check passed"
