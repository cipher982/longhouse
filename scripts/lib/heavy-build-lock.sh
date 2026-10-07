#!/usr/bin/env bash
# One heavy build at a time on this machine (cargo, xcodebuild, Docker, a release
# validation): run it under the shared heavy-build lock.
#
#   lh_run_heavy COMMAND [ARGS...]
#
# An outer `lockf <lock> make ...` wrapper already holds the lock (taking it again
# would deadlock on ourselves), so that is detected and the command just runs.
# Without lockf (Linux guests) it just runs too.
HEAVY_BUILD_LOCK="${LONGHOUSE_HEAVY_BUILD_LOCK:-/tmp/agents/longhouse-heavy-build.lock}"

heavy_lock_held_by_ancestor() {
  local pid=$$ command first
  while [[ -n "$pid" && "$pid" -gt 1 ]]; do
    command="$(ps -o command= -p "$pid" 2>/dev/null || true)"
    first="${command%% *}"
    # argv[0] must be lockf itself: a shell whose -c text merely mentions it is not a holder.
    [[ "${first##*/}" == lockf && "$command" == *"$HEAVY_BUILD_LOCK"* ]] && return 0
    pid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
  done
  return 1
}

lh_run_heavy() {
  if ! command -v lockf >/dev/null 2>&1 || heavy_lock_held_by_ancestor; then
    "$@"
    return
  fi
  mkdir -p "$(dirname "$HEAVY_BUILD_LOCK")"
  echo "Waiting for the heavy-build lock ($HEAVY_BUILD_LOCK) if another build holds it..."
  lockf -k "$HEAVY_BUILD_LOCK" "$@"
}
