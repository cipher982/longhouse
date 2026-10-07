#!/usr/bin/env bash
# Single writer per live surface (scripts/ops/ring_lock.py): an expiring lock the
# scripts that write a ring take before they read anything they decide on, and
# release in their EXIT trap.
#
#   lh_ring_lock_acquire SURFACE SHA TTL_SECONDS [OP]  1 while held (holder on stderr), 2 when it cannot decide
#   lh_ring_lock_renew TTL_SECONDS [SHA]               non-zero when this run no longer holds it
#   lh_ring_lock_keepalive INTERVAL TTL_SECONDS        renew in the background while this script lives
#   lh_ring_lock_release                               safe to call twice, and when nothing was taken
#
# The holder is this script's pid ($$), so a holder that dies without its trap
# (kill -9, a closed terminal) frees the lock at once for anyone on this machine;
# every holder runs the keepalive, so the TTL bounds only a holder that stopped
# renewing (suspended, or its keepalive died). ROOT must name a checkout.
LH_RING_LOCK_SURFACE="${LH_RING_LOCK_SURFACE:-}"
LH_RING_LOCK_TOKEN="${LH_RING_LOCK_TOKEN:-}"
LH_RING_LOCK_KEEPALIVE_PID=""

_lh_ring_lock() {
  python3 "${ROOT:?}/scripts/ops/ring_lock.py" --repo "$ROOT" "$@"
}

lh_ring_lock_acquire() {
  local surface="${1:?surface}" sha="${2:?sha}" ttl="${3:?ttl seconds}" op="${4:-$1}" token
  token="$(_lh_ring_lock acquire "$surface" --sha "$sha" --ttl "$ttl" --pid "$$" --op "$op")" || return $?
  LH_RING_LOCK_SURFACE="$surface"
  LH_RING_LOCK_TOKEN="$token"
}

lh_ring_lock_renew() {
  local ttl="${1:?ttl seconds}" sha="${2:-}"
  [[ -n "$LH_RING_LOCK_TOKEN" ]] || return 0
  if [[ -n "$sha" ]]; then
    _lh_ring_lock renew "$LH_RING_LOCK_SURFACE" --token "$LH_RING_LOCK_TOKEN" --ttl "$ttl" --sha "$sha"
  else
    _lh_ring_lock renew "$LH_RING_LOCK_SURFACE" --token "$LH_RING_LOCK_TOKEN" --ttl "$ttl"
  fi
}

lh_ring_lock_keepalive() {
  local interval="${1:?interval seconds}" ttl="${2:?ttl seconds}" parent="$$"
  [[ -n "$LH_RING_LOCK_TOKEN" ]] || return 0
  # Detached from the caller's stdout/stderr, so a `make release | tee` pipe is not held open by it.
  (
    while sleep "$interval"; do
      kill -0 "$parent" 2>/dev/null || exit 0
      lh_ring_lock_renew "$ttl" || exit 0
    done
  ) >/dev/null 2>&1 &
  LH_RING_LOCK_KEEPALIVE_PID=$!
}

lh_ring_lock_release() {
  if [[ -n "$LH_RING_LOCK_KEEPALIVE_PID" ]]; then
    kill "$LH_RING_LOCK_KEEPALIVE_PID" 2>/dev/null || true
    LH_RING_LOCK_KEEPALIVE_PID=""
  fi
  [[ -n "$LH_RING_LOCK_TOKEN" ]] || return 0
  _lh_ring_lock release "$LH_RING_LOCK_SURFACE" --token "$LH_RING_LOCK_TOKEN" || true
  LH_RING_LOCK_TOKEN=""
}
