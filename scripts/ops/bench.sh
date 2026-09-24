#!/usr/bin/env bash
# Run iOS build, simulator, and profiling work on the test bench Mac instead
# of this laptop. The working tree (uncommitted edits included, .gitignore'd
# output excluded) is mirrored to the bench, the command runs there under a
# lock, and anything it writes to $BENCH_OUT comes back here.
#
#   scripts/ops/bench.sh sync                 mirror this checkout to the bench
#   scripts/ops/bench.sh run <command...>     sync, then run it in the mirror
#   scripts/ops/bench.sh start <command...>   same, detached on the bench; prints a job id
#   scripts/ops/bench.sh collect <job> [--wait]  fetch a job's outputs (wait for it first)
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
    # `/.git` without a trailing slash, because a linked worktree's `.git` is a
    # file that points at the shared common dir. Excluding only `/.git/` copied
    # that file and then tried to rsync into it, which fails with "cannot stat
    # destination .git/: Not a directory" on every worktree checkout.
    printf '%s\n' /.git /.build/ /server/.venv/ /artifacts/ node_modules/ .DS_Store
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

# The remote half of run/start: a fresh output dir, then the command under
# the bench lock with its output locations in the environment. Its exit code
# lands in $out/exit so a detached job can be collected later.
remote_job() {
  local out="$1"; shift
  printf '%s' "mkdir -p '$out' && cd '$REMOTE_DIR' && \
    BENCH_OUT='$out' SIM_OUT_DIR='$out' PHONE_OUT_DIR='$out' TOUR_OUT_DIR='$out' \
    lockf -k /tmp/longhouse-bench.lock zsh -lc $(printf '%q' "$*"); \
    code=\$?; echo \$code > '$out/exit'; exit \$code"
}

new_job() {
  printf '%s-%s' "$NAME" "$(date -u +%Y%m%dT%H%M%SZ)"
}

cmd_collect() {
  local job="${1:-}" wait="${2:-}"
  [[ -n "$job" ]] || die "collect needs a job id"
  local out="/tmp/agents/bench-out/$job" dest="/tmp/agents/bench/$job"
  if [[ "$wait" == "--wait" ]]; then
    ssh -o BatchMode=yes -o ServerAliveInterval=30 "$HOST" \
      "until [ -f '$out/exit' ]; do sleep 10; done"
  fi
  mkdir -p "$dest"
  rsync -a "$HOST:$out/" "$dest/" 2>/dev/null || true
  if [[ -f "$dest/exit" ]]; then
    [[ -f "$dest/run.log" ]] && tail -40 "$dest/run.log"
    echo "bench: $job exited $(cat "$dest/exit"); outputs in $dest" >&2
    return "$(cat "$dest/exit")"
  fi
  echo "bench: $job still running; outputs so far in $dest" >&2
  return 3
}

cmd_run() {
  (($# > 0)) || die "run needs a command"
  cmd_sync
  local job started=$SECONDS
  job="$(new_job)"
  ssh -o BatchMode=yes "$HOST" "$(remote_job "/tmp/agents/bench-out/$job" "$@")" || true
  echo "bench: finished after $((SECONDS - started))s" >&2
  cmd_collect "$job"
}

# Detached: the job belongs to the bench, not this ssh connection, so a
# sleeping or disconnected laptop does not kill it.
cmd_start() {
  (($# > 0)) || die "start needs a command"
  cmd_sync
  local job out
  job="$(new_job)"
  out="/tmp/agents/bench-out/$job"
  ssh -o BatchMode=yes "$HOST" "mkdir -p '$out' && nohup zsh -c $(printf '%q' "$(remote_job "$out" "$@")") > '$out/run.log' 2>&1 < /dev/null &"
  echo "$job"
  echo "bench: started $job; collect with: scripts/ops/bench.sh collect $job --wait" >&2
}

case "${1:-}" in
  sync) cmd_sync ;;
  run) shift; cmd_run "$@" ;;
  start) shift; cmd_start "$@" ;;
  collect) shift; cmd_collect "$@" ;;
  ssh) exec ssh -t "$HOST" "cd '$REMOTE_DIR' && exec zsh -l" ;;
  *) sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
