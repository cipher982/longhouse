#!/usr/bin/env bash
# Keep the provider-name-literal census in step with the source tree.
#
# docs/generated/provider_census.json records which files mention provider
# names, so adding/removing a provider literal anywhere invalidates it. A
# stale census fails `make validate-provider-census` in CI's Validation job —
# it went red twice in 24 hours (2026-07-31 and 2026-08-01) for exactly this,
# each time hours after the offending commit landed.
#
# Regenerating is mechanical and has no judgement in it, so this fixes rather
# than complains: it rewrites the census and fails the commit only to make
# you stage the result.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

before=$(git hash-object docs/generated/provider_census.json 2>/dev/null || echo missing)
(cd server && uv run --extra dev python ../scripts/generate_provider_census.py --write) >/dev/null
after=$(git hash-object docs/generated/provider_census.json)

if [[ "$before" != "$after" ]]; then
  echo "provider_census.json was stale and has been regenerated." >&2
  echo "A provider-name literal changed somewhere in the tree." >&2
  echo "Stage docs/generated/provider_census.json and commit again." >&2
  exit 1
fi
