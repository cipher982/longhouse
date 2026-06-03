#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GIT_COMMON_DIR="$(git -C "$ROOT" rev-parse --git-common-dir)"
LOCK_DIR="$GIT_COMMON_DIR/longhouse-ship.lock"

usage() {
  cat <<'EOF' >&2
Usage: ship.sh [--sha <commit>] [--branch <branch>] [ship-monitor args...]

Pushes one exact commit SHA to the target branch, then waits on push-triggered
workflow runs for that same SHA.
EOF
}

SHA=""
BRANCH=""
MONITOR_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha)
      SHA="${2:-}"
      shift 2
      ;;
    --branch)
      BRANCH="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      MONITOR_ARGS+=("$1")
      shift
      ;;
  esac
done

cleanup_lock() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}

while ! mkdir "$LOCK_DIR" 2>/dev/null; do
  echo "Waiting for ship lock..." >&2
  sleep 0.2
done
trap cleanup_lock EXIT

if [[ -z "$SHA" ]]; then
  echo "No explicit --sha supplied; defaulting to current HEAD under lock. Shared-worktree agents should pass an exact SHA." >&2
  SHA="$(git -C "$ROOT" rev-parse HEAD)"
fi

if [[ -z "$BRANCH" ]]; then
  BRANCH="$(git -C "$ROOT" symbolic-ref --quiet --short HEAD)"
fi

SHA="$(git -C "$ROOT" rev-parse --verify "${SHA}^{commit}")"
SUBJECT="$(git -C "$ROOT" log -1 --format=%s "$SHA")"

echo "Starting cowbell for commit ${SHA:0:10}: ${SUBJECT}" >&2
echo "Target branch: ${BRANCH}" >&2

git -C "$ROOT" fetch --quiet origin "$BRANCH"
REMOTE_REF="refs/remotes/origin/$BRANCH"

if git -C "$ROOT" merge-base --is-ancestor "$SHA" "$REMOTE_REF"; then
  echo "Commit ${SHA:0:10} is already on origin/${BRANCH}; skipping push and verifying exact SHA." >&2
else
  echo "Pushing exact commit ${SHA:0:10} to ${BRANCH}..." >&2
  git -C "$ROOT" push origin "$SHA:refs/heads/$BRANCH"
fi

cleanup_lock
trap - EXIT

exec "$ROOT/scripts/ops/ship-monitor.py" --sha "$SHA" ${MONITOR_ARGS[@]+"${MONITOR_ARGS[@]}"}
