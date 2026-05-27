#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${MANAGED_CODEX_CONTRACT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

failed=0
MATCHES_FILE="$(mktemp "${TMPDIR:-/tmp}/managed-codex-contract.XXXXXX")"
trap 'rm -f "$MATCHES_FILE"' EXIT

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

  set +e
  rg -n --hidden \
    --glob '!.git' \
    --glob '!**/check-managed-codex-contract.sh' \
    --glob '!**/managed-codex-contract.test.py' \
    --glob '!**/routers/threads.py' \
    --glob '!**/generated/**' \
    -- "$pattern" "${paths[@]}" >"$MATCHES_FILE"
  local rc=$?
  set -e

  case "$rc" in
    0)
      echo "Forbidden managed Codex contract reference matched: $label" >&2
      cat "$MATCHES_FILE" >&2
      failed=1
      ;;
    1)
      ;;
    *)
      echo "managed Codex contract scan failed for: $label" >&2
      cat "$MATCHES_FILE" >&2 || true
      failed=1
      ;;
  esac
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

  set +e
  rg -n --hidden -- "$pattern" "$ROOT_DIR/$rel_path" >"$MATCHES_FILE"
  local rc=$?
  set -e

  case "$rc" in
    0)
      ;;
    1)
      echo "Required managed Codex contract reference is missing: $label" >&2
      failed=1
      ;;
    *)
      echo "managed Codex contract required-pattern scan failed for: $label" >&2
      cat "$MATCHES_FILE" >&2 || true
      failed=1
      ;;
  esac
}

scan_legacy_headless_writers() {
  local label="detached-ui writers must not persist legacy headless state"
  local engine_src="$ROOT_DIR/engine/src"

  if [[ ! -d "$engine_src" ]]; then
    return 0
  fi

  set +e
  python3 - "$engine_src" "$MATCHES_FILE" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

engine_src = Path(sys.argv[1])
matches_file = Path(sys.argv[2])
pattern = re.compile(
    r"launch_mode\s*[:=]\s*Some\([^;}]*?(?:LEGACY_LAUNCH_MODE_HEADLESS|\"headless\")",
    re.DOTALL,
)
matches: list[str] = []

for path in sorted(engine_src.rglob("*.rs")):
    text = path.read_text(encoding="utf-8")
    for match in pattern.finditer(text):
        snippet = text[match.start() : min(len(text), match.end() + 120)]
        if "LEGACY_HEADLESS_COMPAT_OK" in snippet:
            continue
        line = text.count("\n", 0, match.start()) + 1
        first_line = snippet.splitlines()[0].strip()
        matches.append(f"{path}:{line}:{first_line}")

if matches:
    matches_file.write_text("\n".join(matches) + "\n", encoding="utf-8")
    raise SystemExit(1)
matches_file.write_text("", encoding="utf-8")
PY
  local rc=$?
  set -e

  case "$rc" in
    0)
      ;;
    1)
      echo "Forbidden managed Codex contract reference matched: $label" >&2
      cat "$MATCHES_FILE" >&2
      failed=1
      ;;
    *)
      echo "managed Codex contract scan failed for: $label" >&2
      cat "$MATCHES_FILE" >&2 || true
      failed=1
      ;;
  esac
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
  "server/zerg" \
  "scripts/qa" \
  "scripts/ci"

scan_forbidden_pattern "detached-ui persisted alias must not point at legacy headless" \
  "PERSISTED_DETACHED_UI_LAUNCH_MODE[[:space:]]*:.*LEGACY_LAUNCH_MODE_HEADLESS" \
  "engine/src/codex_bridge.rs"

scan_legacy_headless_writers

require_pattern "detached-ui writer persists detached_ui, not legacy headless" \
  "PERSISTED_DETACHED_UI_LAUNCH_MODE[[:space:]]*:[^=]*=[[:space:]]*LAUNCH_MODE_DETACHED_UI" \
  "engine/src/codex_bridge.rs"

if [[ "$failed" -ne 0 ]]; then
  echo "Managed Codex must use stock upstream codex from PATH, create threads explicitly, and persist detached-ui managed sessions as detached_ui." >&2
  exit 1
fi

echo "managed Codex contract check passed"
