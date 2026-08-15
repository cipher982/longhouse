#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GIT_COMMON_DIR="$(git -C "$ROOT" rev-parse --git-common-dir)"
LOCK_DIR="$GIT_COMMON_DIR/longhouse-ship.lock"

usage() {
  cat <<'EOF' >&2
Usage: ship.sh [--sha <commit>] [--branch <branch>] [--require-clean] [ship-monitor args...]

Pushes one exact commit SHA to the target branch, then waits on push-triggered
workflow runs for that same SHA.

Always reports shipped-path files present in the working tree but absent from
the shipped commit. --require-clean turns that report into a hard failure.
EOF
}

SHA=""
BRANCH=""
REQUIRE_CLEAN=0
MONITOR_ARGS=()

# Paths whose contents this ship actually deploys. Anything modified here and
# not contained in the shipped commit is, by definition, not going out.
SHIPPED_PATHS=(server web engine config docker/runtime.dockerfile)

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
    --require-clean)
      REQUIRE_CLEAN=1
      shift
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

# State what this ship does NOT contain.
#
# A ship reports success on the SHA it was handed, which is true and useless if
# that SHA is not the work you meant to send. A commit that silently no-ops
# leaves HEAD behind the working tree, `rev-parse HEAD` resolves to the previous
# commit, and the deploy then succeeds against already-live code with every
# health check green. That happened; nothing in the output contradicted it.
#
# This does not guess intent and does not fail by default -- the checkout is
# shared, so another agent's work in progress is expected. It just refuses to
# stay quiet about the difference between the tree and the artifact.
UNSHIPPED="$(git -C "$ROOT" diff --name-only "$SHA" -- "${SHIPPED_PATHS[@]}" 2>/dev/null || true)"
if [[ -n "$UNSHIPPED" ]]; then
  echo "" >&2
  echo "NOT INCLUDED IN THIS SHIP -- modified in the working tree, absent from ${SHA:0:10}:" >&2
  while IFS= read -r path; do
    [[ -n "$path" ]] && echo "  $path" >&2
  done <<< "$UNSHIPPED"
  echo "If any of those are the change you meant to ship, your commit did not land." >&2
  echo "" >&2
  if [[ "$REQUIRE_CLEAN" == "1" ]]; then
    echo "Refusing to ship with --require-clean set." >&2
    exit 3
  fi
fi

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
