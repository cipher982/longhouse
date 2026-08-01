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
# Regenerating is mechanical and has no judgement in it, so this fixes rather
# than complains: it rewrites the manifest and fails the commit only to make
# you stage the result.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

before=$(git hash-object server/zerg/config/managed_provider_contracts.json 2>/dev/null || echo missing)
(cd server && uv run --extra dev python ../scripts/generate_managed_provider_contracts.py --write) >/dev/null
after=$(git hash-object server/zerg/config/managed_provider_contracts.json)

if [[ "$before" != "$after" ]]; then
  echo "managed_provider_contracts.json was stale and has been regenerated." >&2
  echo "An adapter source changed, which invalidates the contract digest." >&2
  echo "Stage server/zerg/config/managed_provider_contracts.json and commit again." >&2
  exit 1
fi
