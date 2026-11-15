#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GIT_COMMON_DIR="$(git -C "$ROOT" rev-parse --git-common-dir)"
LOCK_DIR="$GIT_COMMON_DIR/longhouse-ship.lock"

usage() {
  cat <<'EOF' >&2
Usage: ship.sh [--sha <commit>] [--branch <branch>] [--require-clean] [--wait]
               [--deadline-min N] [ship-monitor args...]

Pushes one exact commit SHA to the target branch and returns. A watcher on
clifford (Sauron job longhouse-ship-watch) follows the SHA through CI, canary
deploy and Terminal Truth and messages this Longhouse session
($LONGHOUSE_MANAGED_SESSION_ID) only if one fails or the deadline passes with
runs unfinished. The push registers that watch as the ref
refs/ship/<sha>/<session>/<pushed-epoch>/<deadline-epoch>.

--wait (make ship WAIT=1) keeps the old behavior: stay in the foreground and
follow the push-triggered runs with ship-monitor.py until a verdict.

Always reports shipped-path files present in the working tree but absent from
the shipped commit. --require-clean turns that report into a hard failure.
EOF
}

SHA=""
BRANCH=""
REQUIRE_CLEAN=0
WAIT=0
GATE=1
DEADLINE_MIN="${SHIP_DEADLINE_MIN:-60}"
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
    --wait)
      WAIT=1
      shift
      ;;
    --no-gate)
      # Same as LONGHOUSE_PREPUSH_GATE=skip: the push is not checked and says so.
      GATE=0
      shift
      ;;
    --deadline-min)
      DEADLINE_MIN="${2:-}"
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

# Pre-push gate (scripts/ci/prepush_gate.py): the CI lanes for these paths, on the ci VM, before the push. It runs
# before the ship lock so one agent's gate does not hold every other agent's push. The push below tells the git
# pre-push hook this exact commit was already gated.
GATED=0
gate_first() {
  [[ "$GATE" == "1" && "$BRANCH_FOR_GATE" == "main" ]] || return 0
  git -C "$ROOT" fetch --quiet origin main
  if git -C "$ROOT" merge-base --is-ancestor "$SHA" refs/remotes/origin/main; then return 0; fi
  python3 "$ROOT/scripts/ci/prepush_gate.py" --base origin/main --head "$SHA" >&2 || {
    echo "ship: pre-push gate failed; nothing was pushed. (LONGHOUSE_PREPUSH_GATE=skip bypasses it for a revert that must land now.)" >&2
    exit 4
  }
  GATED=1
}
if [[ -z "$SHA" ]]; then
  echo "No explicit --sha supplied; defaulting to current HEAD. Shared-worktree agents should pass an exact SHA." >&2
  SHA="$(git -C "$ROOT" rev-parse HEAD)"
fi
SHA="$(git -C "$ROOT" rev-parse --verify "${SHA}^{commit}")"
BRANCH_FOR_GATE="${BRANCH:-$(git -C "$ROOT" symbolic-ref --quiet --short HEAD || true)}"
if [[ "${LONGHOUSE_PREPUSH_GATE:-}" == "skip" ]]; then GATE=0; fi
gate_first

while ! mkdir "$LOCK_DIR" 2>/dev/null; do
  echo "Waiting for ship lock..." >&2
  sleep 0.2
done
trap cleanup_lock EXIT

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
  # The verifier/subject import allowlist only shrinks (scripts/ci/verifier_boundary.py), checked on the
  # tree of the exact commit being pushed.
  python3 "$ROOT/scripts/ci/verifier_boundary.py" check --root "$ROOT" --rev "$SHA" --base "origin/$BRANCH" >&2
  echo "Pushing exact commit ${SHA:0:10} to ${BRANCH}..." >&2
  LONGHOUSE_PREPUSH_GATE="$([[ "$GATED" == "1" ]] && echo "done:$SHA" || echo "${LONGHOUSE_PREPUSH_GATE:-}")" \
    git -C "$ROOT" push origin "$SHA:refs/heads/$BRANCH"
fi

cleanup_lock
trap - EXIT

# Register the watch. The pusher does not wait: the Sauron job
# longhouse-ship-watch reads these refs, follows the exact SHA, and sends a
# message to the session named in the ref only on failure or a missed deadline,
# then writes refs/ship-done/<sha>. Refs outside refs/heads and refs/tags start
# no workflow. A failure to register is loud, never fatal: the push itself stands.
register_watch() {
  local session="${LONGHOUSE_MANAGED_SESSION_ID:-none}" now deadline ref
  [[ "$session" =~ ^[0-9A-Za-z-]{1,64}$ ]] || session="none"
  [[ "$DEADLINE_MIN" =~ ^[1-9][0-9]*$ ]] || { echo "ship: --deadline-min must be a positive integer" >&2; return 1; }
  now="$(date +%s)"
  deadline=$((now + DEADLINE_MIN * 60))
  ref="refs/ship/${SHA}/${session}/${now}/${deadline}"
  if git -C "$ROOT" push --quiet origin "${SHA}:${ref}" 2>/dev/null; then
    echo "Watch registered for ${SHA:0:10} (session ${session}, deadline ${DEADLINE_MIN} min). You are messaged only on failure or a missed deadline." >&2
  else
    echo "WARNING: could not register the watch ref ${ref}; nothing will message you about ${SHA:0:10}. Follow it with: make ship-watch SHA=${SHA}" >&2
  fi
}

if [[ "$BRANCH" == "main" && "$WAIT" != "1" ]]; then
  register_watch || true
  echo "Pushed ${SHA:0:10} to ${BRANCH}; not waiting. Move on. (make ship WAIT=1 blocks; make ship-watch SHA=${SHA} follows by hand.)" >&2
  exit 0
fi

# Say how busy CI is before waiting on it, so the agent can decide whether to
# block here, batch its next commits, or do other work. Never fails the ship.
(cd "$ROOT" && "$ROOT/scripts/ops/ci-queue.py" --sha "$SHA" --brief) >&2 || true

exec "$ROOT/scripts/ops/ship-monitor.py" --sha "$SHA" ${MONITOR_ARGS[@]+"${MONITOR_ARGS[@]}"}
