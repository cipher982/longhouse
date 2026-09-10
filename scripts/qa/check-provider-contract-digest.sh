#!/usr/bin/env bash
# Keep the managed-provider contract manifest in step with its adapter sources.
#
# The manifest digests the *contents* of every file listed under
# adapter_sources in schemas/managed_providers.yml, so editing any of them
# invalidates server/zerg/config/managed_provider_contracts.json. A stale
# manifest aborts `make validate` at its step, which hides every check behind
# it — three separate red CI runs on 2026-07-31 traced to exactly that, plus a
# blocked release.
#
# Checking is deliberately non-mutating: authors can regenerate explicitly with
# scripts/generate_managed_provider_contracts.py --write after this hook fails.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

if (cd server && uv run --extra dev python ../scripts/generate_managed_provider_contracts.py --check) >/dev/null; then
  exit 0
fi

echo "managed_provider_contracts.json is stale and must be regenerated." >&2
echo "An adapter source changed, which invalidates the contract digest." >&2
echo "Run scripts/generate_managed_provider_contracts.py --write, then stage server/zerg/config/managed_provider_contracts.json and commit again." >&2
exit 1
