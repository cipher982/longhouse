#!/usr/bin/env bash
# Ship an exact main revision to TestFlight through the ios-testflight workflow
# and wait for it. The workflow archives, uploads, waits for processing, submits
# for Beta App Review when needed, and prints the public link in its job summary.
#
#   scripts/ops/testflight-ship.sh [sha]        # default: HEAD (must be on origin/main)
#   WHATS_NEW="note for testers" scripts/ops/testflight-ship.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

REPO="cipher982/longhouse"
WORKFLOW="ios-testflight.yml"
SHA="$(git rev-parse "${1:-HEAD}")"

die() { echo "testflight-ship: $*" >&2; exit 1; }

git fetch -q origin main
git merge-base --is-ancestor "$SHA" origin/main \
  || die "$SHA is not on origin/main; push it first (only main ships to TestFlight)"

before="$(gh run list -R "$REPO" -w "$WORKFLOW" --limit 1 --json databaseId -q '.[0].databaseId // 0')"
gh workflow run "$WORKFLOW" -R "$REPO" --ref main -f source_sha="$SHA" ${WHATS_NEW:+-f whats_new="$WHATS_NEW"}

run_id=""
for _ in $(seq 1 24); do
  sleep 5
  run_id="$(gh run list -R "$REPO" -w "$WORKFLOW" --limit 5 --json databaseId,headSha,event \
    -q "[.[] | select(.event == \"workflow_dispatch\" and .databaseId > $before)] | last | .databaseId // empty")"
  [ -n "$run_id" ] && break
done
[ -n "$run_id" ] || die "dispatched, but no new run appeared within 2 minutes; check: gh run list -R $REPO -w $WORKFLOW"

echo "testflight-ship: $SHA -> run $run_id (https://github.com/$REPO/actions/runs/$run_id)"
gh run watch "$run_id" -R "$REPO" --exit-status
echo "testflight-ship: done. Link and review state: job summary of run $run_id, or 'make testflight-status'"
