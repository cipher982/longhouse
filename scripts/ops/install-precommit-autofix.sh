#!/usr/bin/env bash
# Install the self-staging pre-commit shims used by scripts/qa/precommit_autofix.py.
#
#   make install-precommit-autofix  (once per clone; every worktree shares the git hooks)
#
# `pre-commit.legacy` runs before pre-commit stashes unstaged changes and records which files have them,
# so a fix is staged only where that is safe. `post-commit` brings the real index up to a fix that
# `git commit -o` staged in its temporary index. Without these shims a fix stays in the working tree
# and the commit fails, as before.
#
# Idempotent. Replaces only its own shims and preserves unrelated hooks.
set -euo pipefail

hooks_dir="$(git rev-parse --path-format=absolute --git-path hooks)"
mkdir -p "$hooks_dir"
obsolete_hook="$hooks_dir/pre-push"
if [[ -e "$obsolete_hook" ]] && grep -q '^# longhouse-review-gate-pre-push:' "$obsolete_hook"; then
  rm "$obsolete_hook"
  echo "install-precommit-autofix: retired its obsolete pre-push hook at $obsolete_hook"
elif [[ -e "$obsolete_hook" ]]; then
  echo "install-precommit-autofix: preserving unrelated pre-push hook at $obsolete_hook" >&2
fi

# --- the self-staging pre-commit shims ----------------------------------------------------------------
install_shim() {  # NAME MARKER BODY
  local path="$hooks_dir/$1" tmp
  if [[ -e "$path" ]] && ! grep -q "$2" "$path"; then
    echo "install-precommit-autofix: $path exists and is not its shim; preserving it." >&2
    return 0
  fi
  tmp="$(mktemp "$hooks_dir/.$1.XXXXXX")"
  printf '%s\n' "$3" >"$tmp"
  chmod +x "$tmp"
  mv "$tmp" "$path"
  echo "install-precommit-autofix: $1 shim installed at $path"
}
shim_body() {  # MARKER MODE
  cat <<SHIM
#!/bin/sh
# $1: installed by scripts/ops/install-precommit-autofix.sh; see scripts/qa/precommit_autofix.py.
top="\$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
script="\$top/scripts/qa/precommit_autofix.py"
[ -f "\$script" ] && command -v python3 >/dev/null 2>&1 || exit 0
# PPID is the pre-commit process that then runs the hooks; the fixers trust only its record.
LONGHOUSE_AUTOFIX_PID="\$PPID" python3 "\$script" $2 || true
exit 0
SHIM
}
install_shim pre-commit.legacy longhouse-autofix-snapshot "$(shim_body longhouse-autofix-snapshot snapshot)"
install_shim post-commit longhouse-autofix-post-commit "$(shim_body longhouse-autofix-post-commit post-commit)"
