#!/usr/bin/env bash
# Ship an exact main revision to TestFlight through the ios-testflight workflow
# and wait for it. By default, it submits for Beta App Review when needed, publishes to
# the public group, and prints the public link in its job summary.
#
#   scripts/ops/testflight-ship.sh [sha]        # default: HEAD (must be on origin/main)
#   WHATS_NEW="note for testers" scripts/ops/testflight-ship.sh
#   INTERNAL_ONLY=1 scripts/ops/testflight-ship.sh   # internal group only: no public link, no Beta App Review
#   DRY_RUN=1 scripts/ops/testflight-ship.sh         # archive and export on wisp; upload and publish nothing
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
INTERNAL_ONLY_INPUT=false
if [ "${INTERNAL_ONLY:-}" = "1" ]; then INTERNAL_ONLY_INPUT=true; fi
DRY_RUN_INPUT=false
if [ "${DRY_RUN:-}" = "1" ]; then DRY_RUN_INPUT=true; fi
gh workflow run "$WORKFLOW" -R "$REPO" --ref main -f source_sha="$SHA" ${WHATS_NEW:+-f whats_new="$WHATS_NEW"} -f internal_only="$INTERNAL_ONLY_INPUT" -f dry_run="$DRY_RUN_INPUT"

run_id=""
for _ in $(seq 1 24); do
  sleep 5
  run_id="$(gh run list -R "$REPO" -w "$WORKFLOW" --limit 5 --json databaseId,headSha,event \
    -q "[.[] | select(.event == \"workflow_dispatch\" and .databaseId > $before)] | last | .databaseId // empty")"
  [ -n "$run_id" ] && break
done
[ -n "$run_id" ] || die "dispatched, but no new run appeared within 2 minutes; check: gh run list -R $REPO -w $WORKFLOW"

echo "testflight-ship: $SHA -> run $run_id (https://github.com/$REPO/actions/runs/$run_id)"
# Never `gh run watch`: it polls every 3 s and spends the shared GitHub rate budget.
conclusion=""
for _ in $(seq 1 120); do
  state="$(gh run view "$run_id" -R "$REPO" --json status,conclusion -q '.status + " " + (.conclusion // "")')" || state=""
  case "$state" in
    "completed "*) conclusion="${state#completed }"; break ;;
  esac
  sleep 45
done
[ "$conclusion" = "success" ] || die "run $run_id ended '${conclusion:-still running after 90 minutes}': https://github.com/$REPO/actions/runs/$run_id"
echo "testflight-ship: done. See publish details in the job summary of run $run_id, or use 'make testflight-status'"
