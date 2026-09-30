#!/usr/bin/env bash
# Install the pre-push hook that asks the review gate before any push that lands commits on main.
#
#   make install-push-gate        (once per clone; every worktree of the clone shares the hook)
#
# The landing rule (scripts/ops/review-policy.toml) was cooperative: only `make check-push-readiness`,
# `make ship` and release.sh asked, so a bare `git push origin HEAD:main`, which is the landing recipe an
# agent that started before the rule existed still follows, skipped it. On 2026-09-29 three commits to
# review_gate.py and promote-production.sh landed with no receipt that way. A hook in the shared git dir
# asks for every worktree, whichever recipe the pusher follows.
#
# What the hook does: a push that updates main gets `review_gate.py pre-push`; refused (exit 1) when a
# commit touching the blocking list has no completed receipt. It does not gate topic branches or tags.
# When the gate cannot decide (exit 2: unknown or unreadable policy, internal error, git data it needs
# missing) the push is allowed with the reason printed, so a gate fault cannot stop every push. `git push --no-verify` and the logged
# LONGHOUSE_REVIEW_OVERRIDE bypass it; the promotion rule is the backstop for both.
#
# Idempotent. Refuses to replace a pre-push hook that is not this one.
set -euo pipefail

hooks_dir="$(git rev-parse --path-format=absolute --git-path hooks)"
hook="$hooks_dir/pre-push"
marker="longhouse-review-gate-pre-push"

if [[ -e "$hook" ]] && ! grep -q "$marker" "$hook"; then
  echo "install-push-gate: $hook exists and is not the review-gate hook; not replacing it." >&2
  exit 1
fi

mkdir -p "$hooks_dir"
tmp="$(mktemp "$hooks_dir/.pre-push.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
cat >"$tmp" <<'HOOK'
#!/bin/sh
# longhouse-review-gate-pre-push: installed by scripts/ops/install-push-gate.sh (see it for what this does).
command -v python3 >/dev/null 2>&1 || { echo "review-gate: python3 is not on PATH; this push was not checked." >&2; exit 0; }
top="$(git rev-parse --show-toplevel)" || exit 0
gate="$top/scripts/ops/review_gate.py"
# A worktree cut before the gate existed has no copy; the primary checkout's is the fallback.
[ -f "$gate" ] || gate="$(cd "$(git rev-parse --git-common-dir)/.." && pwd)/scripts/ops/review_gate.py"
if [ ! -f "$gate" ]; then
  echo "review-gate: no scripts/ops/review_gate.py in this worktree or the primary checkout; this push was not checked." >&2
  exit 0
fi
python3 "$gate" --repo "$top" pre-push "$@"
status=$?
if [ "$status" -eq 2 ]; then
  echo "review-gate: could not decide (above), so this push was not checked. Fix the gate, or ask David." >&2
  exit 0
fi
exit "$status"
HOOK
chmod +x "$tmp"
mv "$tmp" "$hook"
trap - EXIT
echo "install-push-gate: pre-push hook installed at $hook"
