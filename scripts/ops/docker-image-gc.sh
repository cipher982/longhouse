#!/bin/bash
# Reclaim Docker image storage on a Runtime Host.
#
# Every push builds a new ghcr.io/cipher982/longhouse-runtime image, and each
# one carries a ~600MB embedding-model layer. Nothing removed the superseded
# tags, so the boot disk climbed until it tripped a Netdata emergency and
# someone pruned by hand. This makes the reclaim scheduled instead of manual.
#
# Pruning is safe for rollback: every tag stays in the registry, so an older
# SHA costs a pull rather than being lost. Images backing a running container
# are never removed by Docker, so the live deployment is never touched.
#
# Install on a Runtime Host:
#   scp scripts/ops/docker-image-gc.{sh,service,timer} <host>:/tmp/
#   ssh <host> '
#     sudo install -m 0755 -D /tmp/docker-image-gc.sh /usr/local/lib/longhouse/docker-image-gc.sh
#     sudo install -m 0644 /tmp/docker-image-gc.service /etc/systemd/system/
#     sudo install -m 0644 /tmp/docker-image-gc.timer /etc/systemd/system/
#     sudo systemctl daemon-reload && sudo systemctl enable --now docker-image-gc.timer'
set -euo pipefail

RETAIN_WINDOW="${RETAIN_WINDOW:-24h}"
# Above this usage the normal window is too slow: fall back to a much shorter
# one so a heavy deploy day cannot fill the disk between timer runs. It stays a
# window rather than an unfiltered prune so an in-flight deploy cannot have its
# freshly pulled image removed before the container starts.
PRESSURE_PERCENT="${PRESSURE_PERCENT:-70}"
PRESSURE_WINDOW="${PRESSURE_WINDOW:-1h}"
DISK_PATH="${DISK_PATH:-/}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

usage_percent() {
    df --output=pcent "$DISK_PATH" | tail -n 1 | tr -dc '0-9'
}

before="$(usage_percent)"
log "Docker image GC starting; ${DISK_PATH} at ${before}%"

docker image prune -af --filter "until=${RETAIN_WINDOW}" >/dev/null 2>&1 || log "WARN: windowed image prune failed"
docker builder prune -af --filter "until=${RETAIN_WINDOW}" >/dev/null 2>&1 || log "WARN: builder prune failed"

after="$(usage_percent)"
if ((after >= PRESSURE_PERCENT)); then
    log "Still at ${after}% after windowed prune; reclaiming down to ${PRESSURE_WINDOW}"
    docker image prune -af --filter "until=${PRESSURE_WINDOW}" >/dev/null 2>&1 || log "WARN: pressure image prune failed"
    docker builder prune -af --filter "until=${PRESSURE_WINDOW}" >/dev/null 2>&1 || log "WARN: pressure builder prune failed"
    after="$(usage_percent)"
fi

log "Docker image GC finished; ${DISK_PATH} at ${after}% (was ${before}%)"
