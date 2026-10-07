#!/usr/bin/env bash
# A disposable checkout of one exact commit, for the operations that used to need
# the primary checkout (`make release`, `make dogfood-refresh`) and so blocked each
# other and every agent that left that checkout dirty or behind origin/main.
#
#   lh_exact_checkout REPO OP REV   sets LH_EXACT_CHECKOUT (the path) and LH_EXACT_SHA
#   lh_exact_checkout_cleanup       call it from the EXIT trap; safe to call twice
#
# It is a linked worktree (`git worktree add --detach`), not a clone: objects are
# shared, and so is the git common dir, which holds the review receipts, the ring
# locks and the release validation stamps every other checkout reads. It lives
# under /tmp/agents (LONGHOUSE_EXACT_CHECKOUT_PARENT overrides), so one a crash
# leaves behind is swept with the rest of the scratch; `git worktree prune` then
# drops its record.
LH_EXACT_CHECKOUT=""
LH_EXACT_SHA=""
LH_EXACT_REPO=""

lh_exact_checkout() {
  local repo="${1:?repo}" op="${2:?operation name}" rev="${3:?revision}" parent
  parent="${LONGHOUSE_EXACT_CHECKOUT_PARENT:-/tmp/agents}"
  LH_EXACT_SHA="$(git -C "$repo" rev-parse --verify --quiet "${rev}^{commit}")" || {
    echo "exact-checkout: $rev is not a commit in $repo (fetch first)." >&2
    return 1
  }
  mkdir -p "$parent"
  LH_EXACT_REPO="$repo"
  LH_EXACT_CHECKOUT="$(mktemp -d "$parent/longhouse-$op-${LH_EXACT_SHA:0:12}.XXXXXX")"
  git -C "$repo" worktree add --detach --quiet "$LH_EXACT_CHECKOUT" "$LH_EXACT_SHA" >&2
}

lh_exact_checkout_cleanup() {
  local path="${LH_EXACT_CHECKOUT:-}"
  [[ -n "$path" ]] || return 0
  LH_EXACT_CHECKOUT=""
  # --force: the operation leaves build output and untracked files behind; they go with it.
  git -C "$LH_EXACT_REPO" worktree remove --force "$path" >/dev/null 2>&1 || true
  rmdir "$path" 2>/dev/null || true  # `worktree add` failed and left the empty directory mktemp made
  git -C "$LH_EXACT_REPO" worktree prune >/dev/null 2>&1 || true
  if [[ -e "$path" ]]; then
    echo "exact-checkout: could not remove $path; /tmp/agents is swept, or remove it with git worktree remove --force." >&2
  fi
}
