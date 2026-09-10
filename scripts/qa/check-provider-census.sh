#!/usr/bin/env bash
# Keep the provider-name-literal census in step with the source tree.
#
# docs/generated/provider_census.json records which files mention provider
# names, so adding/removing a provider literal anywhere invalidates it. A
# stale census fails `make validate-provider-census` in CI's Validation job —
# it went red twice in 24 hours (2026-07-31 and 2026-08-01) for exactly this,
# each time hours after the offending commit landed.
#
# Checking is deliberately non-mutating: rewriting the artifact here clobbered
# concurrent work in a shared checkout, and pre-commit's stash/restore of
# unstaged files then lost an edit. Authors regenerate explicitly with
# scripts/generate_provider_census.py --write after this fails.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

if (cd server && uv run --extra dev python ../scripts/generate_provider_census.py --check) >/dev/null; then
  exit 0
fi

echo "provider_census.json is stale and must be regenerated." >&2
echo "A provider-name literal changed somewhere in the tree." >&2
echo "Run scripts/generate_provider_census.py --write, then stage docs/generated/provider_census.json and commit again." >&2
exit 1
