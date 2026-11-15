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
HEAVY_BUILD_LOCK_TIMEOUT="${LONGHOUSE_HEAVY_BUILD_LOCK_TIMEOUT_SECS:-2700}"

_lh_describe_heavy_lock_holder() {
  local pids=""
  if command -v lsof >/dev/null 2>&1 && [[ -e "$HEAVY_BUILD_LOCK" ]]; then
    pids="$(lsof -Fp "$HEAVY_BUILD_LOCK" 2>/dev/null | sed -n 's/^p//p' | tr '\n' ',' | sed 's/,$//' || true)"
  fi
  if [[ -z "$pids" ]] && command -v pgrep >/dev/null 2>&1; then
    pids="$(pgrep -f "$HEAVY_BUILD_LOCK" 2>/dev/null | grep -vx "$$" | tr '\n' ',' | sed 's/,$//' || true)"
  fi
  if [[ -n "$pids" ]]; then
    ps -o pid=,etime=,command= -p "$pids" 2>/dev/null | sed 's/^/  holder: /' || true
  else
    echo "  holder: unavailable (no open lock descriptor or matching process for $HEAVY_BUILD_LOCK)"
  fi
}

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
  if ! lockf -k -t 0 "$HEAVY_BUILD_LOCK" true >/dev/null 2>&1; then
    echo "Waiting for the heavy-build lock ($HEAVY_BUILD_LOCK) held by another process..." >&2
    _lh_describe_heavy_lock_holder >&2
  fi
  if [[ "${HEAVY_BUILD_LOCK_TIMEOUT:-0}" =~ ^[0-9]+$ && "${HEAVY_BUILD_LOCK_TIMEOUT:-0}" -gt 0 ]]; then
    local rc=0
    lockf -k -t "$HEAVY_BUILD_LOCK_TIMEOUT" "$HEAVY_BUILD_LOCK" "$@" || rc=$?
    if [[ "$rc" -eq 75 ]]; then
      echo "ERROR: Timed out after ${HEAVY_BUILD_LOCK_TIMEOUT}s waiting for $HEAVY_BUILD_LOCK" >&2
      _lh_describe_heavy_lock_holder >&2
    fi
    return "$rc"
  else
    lockf -k "$HEAVY_BUILD_LOCK" "$@"
  fi
}
