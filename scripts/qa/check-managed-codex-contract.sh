#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

failed=0

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

patterns=(
  "LONGHOUSE_CODEX_SOURCE"
  "--codex-source"
  "managed-codex\\.patch"
  "download-managed-codex"
  "build-managed-codex"
  "RuntimeComponent\\.MANAGED_CODEX"
)

for pattern in "${patterns[@]}"; do
  if rg -n --hidden --glob '!.git' --glob '!scripts/qa/check-managed-codex-contract.sh' -- "$pattern" "$ROOT_DIR" >/tmp/managed-codex-contract.matches; then
    echo "Forbidden managed Codex packaging reference matched: $pattern" >&2
    cat /tmp/managed-codex-contract.matches >&2
    failed=1
  fi
done

rm -f /tmp/managed-codex-contract.matches

if [[ "$failed" -ne 0 ]]; then
  echo "Managed Codex must use stock upstream codex from PATH; do not reintroduce packaged Codex runtimes." >&2
  exit 1
fi

echo "managed Codex contract check passed"
