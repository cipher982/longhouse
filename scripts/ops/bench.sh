#!/usr/bin/env bash
# Run iOS build, simulator, and profiling work on the test bench Mac instead
# of this laptop. The working tree (uncommitted edits included, .gitignore'd
# output excluded) is mirrored to the bench, the command runs there under a
# lock, and anything it writes to $BENCH_OUT comes back here.
#
#   scripts/ops/bench.sh sync                 mirror this checkout to the bench
#   scripts/ops/bench.sh run <command...>     sync, then run it in the mirror
#   scripts/ops/bench.sh ssh                  a shell in the mirror
#
#   scripts/ops/bench.sh run make test-ios
#   scripts/ops/bench.sh run scripts/ops/sim.sh deploy
#   scripts/ops/bench.sh run 'python3 scripts/qa/simlab.py up --build && python3 scripts/qa/simlab.py run --deploy hostile-transcript'
#
# In the command, $BENCH_OUT is a fresh directory whose contents are copied
# back to /tmp/agents/bench/<stamp>/ afterwards; sim.sh and phone.sh already
# write there (SIM_OUT_DIR/PHONE_OUT_DIR point at it). One command runs at a
# time: the bench has 8 GB and one simulator, so a second caller waits.
#
# Environment:
#   BENCH_HOST   ssh host (default: wisp)
#   BENCH_DIR    mirror path on the bench (default: ~/bench/<checkout name>)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOST="${BENCH_HOST:-wisp}"
NAME="$(basename "$ROOT_DIR")"
REMOTE_DIR="${BENCH_DIR:-bench/$NAME}"

die() { echo "bench: $*" >&2; exit 1; }

cmd_sync() {
  local started=$SECONDS git_dir head excludes
  git_dir="$(git -C "$ROOT_DIR" rev-parse --path-format=absolute --git-common-dir)"
  head="$(git -C "$ROOT_DIR" rev-parse HEAD)"
  excludes="$(mktemp)"
  # git decides what is ignored, not rsync: rsync's own .gitignore reading
  # gets negations and ** wrong and dropped tracked directories. Excluded
  # paths are neither sent nor deleted, so the bench keeps its build caches.
  {
    printf '%s\n' /.git/ /.build/ /server/.venv/ /artifacts/ node_modules/ .DS_Store
    git -C "$ROOT_DIR" ls-files --others --ignored --exclude-standard --directory | sed 's|^|/|'
  } > "$excludes"
  ssh -o BatchMode=yes "$HOST" "mkdir -p '$REMOTE_DIR/.git'"
  rsync -a --delete --exclude-from="$excludes" "$ROOT_DIR/" "$HOST:$REMOTE_DIR/"
  rm -f "$excludes"
  # The object store too, so the mirror answers git questions (build identity
  # reads HEAD and dirtiness); a worktree's common dir works the same way.
  rsync -a --delete --exclude=index --exclude='*.lock' --exclude=worktrees/ \
    "$git_dir/" "$HOST:$REMOTE_DIR/.git/"
  ssh -o BatchMode=yes "$HOST" "cd '$REMOTE_DIR' && git update-ref --no-deref HEAD $head && git reset -q"
  echo "bench: synced $NAME to $HOST:$REMOTE_DIR in $((SECONDS - started))s" >&2
}

cmd_run() {
  (($# > 0)) || die "run needs a command"
  cmd_sync
  local stamp out started=$SECONDS status=0
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  out="/tmp/agents/bench-out/$NAME-$stamp"
  # lockf serializes callers across agents and checkouts; -k keeps the lock
  # file. The login shell supplies the bench's PATH (uv, cargo, xcodegen).
  ssh -o BatchMode=yes "$HOST" "mkdir -p '$out' && cd '$REMOTE_DIR' && \
    BENCH_OUT='$out' SIM_OUT_DIR='$out' PHONE_OUT_DIR='$out' TOUR_OUT_DIR='$out' \
    lockf -k /tmp/longhouse-bench.lock zsh -lc $(printf '%q' "$*")" || status=$?
  mkdir -p "/tmp/agents/bench/$stamp"
  rsync -a "$HOST:$out/" "/tmp/agents/bench/$stamp/" 2>/dev/null || true
  echo "bench: exit $status after $((SECONDS - started))s; outputs in /tmp/agents/bench/$stamp" >&2
  return "$status"
}

case "${1:-}" in
  sync) cmd_sync ;;
  run) shift; cmd_run "$@" ;;
  ssh) exec ssh -t "$HOST" "cd '$REMOTE_DIR' && exec zsh -l" ;;
  *) sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
