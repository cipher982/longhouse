#!/bin/bash
# Reclaim unrelated Docker image storage on a Runtime Host.
#
# Longhouse runtime and control-plane images are owned by their release
# collectors. This generic policy must never remove one: an image ID can have
# several tags, and a digest reference can survive after its tag is removed.
# Therefore this script computes protected IDs from both RepoTags and
# RepoDigests, then removes only old, unrelated IDs. It uses explicit image-ID
# deletion rather than an unfiltered image prune that can bypass protection.
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
# one so a heavy deploy day cannot fill the disk between timer runs.
PRESSURE_PERCENT="${PRESSURE_PERCENT:-70}"
PRESSURE_WINDOW="${PRESSURE_WINDOW:-1h}"
DISK_PATH="${DISK_PATH:-/}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

usage_percent() {
    df --output=pcent "$DISK_PATH" | tail -n 1 | tr -dc '0-9'
}

window_seconds() {
    local value="$1" number unit multiplier
    if [[ ! "$value" =~ ^([0-9]+)([smhd])$ ]]; then
        log "WARN: unsupported image retention window '$value'"
        return 1
    fi
    number="${BASH_REMATCH[1]}"
    unit="${BASH_REMATCH[2]}"
    case "$unit" in
        s) multiplier=1 ;;
        m) multiplier=60 ;;
        h) multiplier=3600 ;;
        d) multiplier=86400 ;;
    esac
    echo $((number * multiplier))
}

created_epoch() {
    local value="$1"
    # Docker emits RFC3339 with fractional seconds. GNU date is present on the
    # Linux Runtime Host; the BSD fallback keeps local diagnostics usable.
    date -d "$value" +%s 2>/dev/null && return 0
    date -j -f '%Y-%m-%dT%H:%M:%S' "${value%%.*}" +%s 2>/dev/null
}

is_runtime_reference() {
    local refs="$1"
    # Match an exact final repository component, not unrelated names that merely
    # contain the same text. JSON formatting from docker image inspect is fine.
    [[ "$refs" == *'"longhouse-runtime:'* || "$refs" == *'"longhouse-runtime@'* || \
        "$refs" == *'/longhouse-runtime:'* || "$refs" == *'/longhouse-runtime@'* || \
        "$refs" == *'"longhouse-control-plane:'* || "$refs" == *'"longhouse-control-plane@'* || \
        "$refs" == *'/longhouse-control-plane/control-plane:'* || \
        "$refs" == *'/longhouse-control-plane/control-plane@'* ]]
}

prune_unprotected_older_than() {
    local window="$1" age cutoff id refs created created_at removed=0
    age="$(window_seconds "$window")" || return 0
    cutoff=$(( $(date +%s) - age ))

    # Inspect every image ID once. RepoTags and RepoDigests are both included so
    # a multi-tag ID remains protected when any one reference is Longhouse's.
    while IFS= read -r id; do
        [[ -n "$id" ]] || continue
        refs="$(docker image inspect --format '{{json .RepoTags}} {{json .RepoDigests}}' "$id" 2>/dev/null || true)"
        if is_runtime_reference "$refs"; then
            continue
        fi
        created="$(docker image inspect --format '{{.Created}}' "$id" 2>/dev/null || true)"
        [[ -n "$created" ]] || continue
        created_at="$(created_epoch "$created" || true)"
        [[ "$created_at" =~ ^[0-9]+$ ]] || continue
        if ((created_at < cutoff)); then
            if docker image rm "$id" >/dev/null 2>&1; then
                removed=$((removed + 1))
            else
                log "WARN: unable to remove unrelated image $id"
            fi
        fi
    done < <(docker image ls --no-trunc --format '{{.ID}}' | sort -u)
    log "Removed ${removed} unrelated image IDs older than ${window}"
}

before="$(usage_percent)"
log "Docker image GC starting; ${DISK_PATH} at ${before}%"

prune_unprotected_older_than "$RETAIN_WINDOW"
docker builder prune -af --filter "until=${RETAIN_WINDOW}" >/dev/null 2>&1 || log "WARN: builder prune failed"

after="$(usage_percent)"
if ((after >= PRESSURE_PERCENT)); then
    log "Still at ${after}% after windowed prune; reclaiming unrelated images down to ${PRESSURE_WINDOW}"
    prune_unprotected_older_than "$PRESSURE_WINDOW"
    docker builder prune -af --filter "until=${PRESSURE_WINDOW}" >/dev/null 2>&1 || log "WARN: pressure builder prune failed"
    after="$(usage_percent)"
fi

log "Docker image GC finished; ${DISK_PATH} at ${after}% (was ${before}%)"
