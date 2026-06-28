#!/usr/bin/env bash
# Detect the stale-duplicate-commit divergence that bites shared-worktree pushes.
#
# ~30 agents share one `main` across ~30 worktrees and one `.git`. When another
# agent rebases/cherry-picks work onto origin/main, this worktree's local main
# can keep the PRE-rebase versions of those same commits. They are identical by
# content but differ by SHA, so `git push` rejects with a non-fast-forward and
# the cause is non-obvious mid-push. This script surfaces it in ~1s beforehand.
#
# It NEVER mutates anything and NEVER suggests force-push or rebasing pushed
# work — only roll-forward recovery (`git reset --hard origin/main`), matching
# the repo's anti-force-push doctrine.
set -euo pipefail

remote="${PUSH_READINESS_REMOTE:-origin}"
branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"

# Only main is shared+contended. Topic branches are owned by one worktree.
if [ "$branch" != "main" ]; then
  echo "check-push-readiness: on '$branch' (not main) — nothing to check."
  exit 0
fi

# Read-only refresh of the remote ref. Network failures are non-fatal: a
# preflight should never block work just because origin is unreachable.
if ! git fetch --quiet "$remote" main 2>/dev/null; then
  echo "check-push-readiness: WARN could not fetch $remote/main; skipping check."
  exit 0
fi

upstream="$remote/main"

ahead="$(git rev-list --count "$upstream"..HEAD 2>/dev/null || echo 0)"
behind="$(git rev-list --count HEAD.."$upstream" 2>/dev/null || echo 0)"

# `git cherry` compares by patch-id (content), so it sees through the SHA
# change a rebase introduces. Lines starting with '-' are local commits whose
# content is ALREADY on the upstream — the stale-duplicate signature.
stale="$(git cherry "$upstream" HEAD 2>/dev/null | grep -c '^-' || true)"

if [ "$stale" -gt 0 ]; then
  echo "check-push-readiness: BLOCKED — $stale local commit(s) on main are already on $upstream by content (stale duplicates)."
  echo
  echo "These are pre-rebase copies of work another agent already landed. Your push will be rejected as non-fast-forward."
  echo "Stale-duplicate commits:"
  git cherry -v "$upstream" HEAD 2>/dev/null | grep '^-' | sed 's/^/  /'
  echo
  echo "Recovery (roll forward, do NOT force-push):"
  echo "  1. Note any commit below that is genuinely yours (marked '+'):"
  git cherry -v "$upstream" HEAD 2>/dev/null | grep '^+' | sed 's/^/       /' || true
  echo "  2. git reset --hard $upstream      # adopt the canonical line"
  echo "  3. Re-apply ONLY your unique commits (cherry-pick the '+' SHAs), then push."
  exit 1
fi

if [ "$behind" -gt 0 ]; then
  echo "check-push-readiness: $upstream is $behind commit(s) ahead and you have $ahead unique. Fast-forward first:"
  echo "  git pull --ff-only $remote main   # then re-run; if it refuses, you have a real merge, not a stale dup"
  exit 1
fi

echo "check-push-readiness: OK — $ahead unique commit(s) ahead, 0 behind, no stale duplicates. Safe to push."
