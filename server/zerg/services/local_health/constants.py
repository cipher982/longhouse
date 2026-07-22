"""Constants shared across the local_health package."""

from __future__ import annotations

import re
from datetime import timedelta

from zerg.provider_cli_contract import PROVIDER_CLI_ENV_BY_PROVIDER

SCHEMA_VERSION = 1
ENGINE_FRESH_SECONDS = 30
ENGINE_STALE_SECONDS = 120
OUTBOX_DEGRADED_AGE_SECONDS = 60
OUTBOX_BROKEN_AGE_SECONDS = 120
DEGRADED_BACKLOG_COUNT = 10
BROKEN_BACKLOG_COUNT = 25
DISK_DEGRADED_BYTES = 5 * 1024 * 1024 * 1024
DISK_BROKEN_BYTES = 1 * 1024 * 1024 * 1024
ACTIVITY_RECENT_MINUTES = 15
ACTIVITY_RECENCY_BANDS = [
    ("0-1m", timedelta(minutes=1)),
    ("1-5m", timedelta(minutes=5)),
    ("5-15m", timedelta(minutes=15)),
    ("15-60m", timedelta(hours=1)),
    ("1-6h", timedelta(hours=6)),
]
RECENT_TOUCH_LIMIT = 4
CODEX_BRIDGE_LOG_TAIL_BYTES = 128 * 1024
CODEX_BRIDGE_LIVE_RETRY_MARKER = "live runtime ingest retrying"
CODEX_BRIDGE_LIVE_SLOW_MARKER = "live runtime ingest slow"
CODEX_BRIDGE_RUNTIME_NETWORK_ERROR_MARKER = "runtime ingest network error"
CODEX_BRIDGE_RUNTIME_FAILED_MARKER = "runtime ingest failed"
CODEX_BRIDGE_LIVE_DROPPED_MARKER = "live runtime ingest dropped"
PROVIDER_HOOK_DIAGNOSTIC_WINDOW = timedelta(hours=24)
PROVIDER_HOOK_DIAGNOSTIC_ACTIONABLE_WINDOW = timedelta(hours=1)
PROVIDER_HOOK_DIAGNOSTIC_FILE_LIMIT = 24
PROVIDER_HOOK_DIAGNOSTIC_EVENT_LIMIT = 8

CONTROL_PATH_MANAGED = "managed"
CONTROL_PATH_UNMANAGED = "unmanaged"
LIVENESS_MODEL_CODEX_BRIDGE = "codex_bridge"
LIVENESS_MODEL_PROCESS_SCAN = "process_scan"
LIVENESS_MODEL_ENGINE_STATUS = "engine_status"
LIVENESS_MODEL_TRANSCRIPT = "transcript"
CODEX_BIN_ENV = PROVIDER_CLI_ENV_BY_PROVIDER["codex"]
OPENCODE_BIN_ENV = PROVIDER_CLI_ENV_BY_PROVIDER["opencode"]
ANTIGRAVITY_BIN_ENV = PROVIDER_CLI_ENV_BY_PROVIDER["antigravity"]
_ZOMBIE_PROCESS_STATUSES = frozenset({"z", "zombie"})

_SHELL_SPAWN_ENOENT_PATTERNS = (
    "posix_spawn '/bin/sh'",
    "spawn /bin/sh ENOENT",
    "spawnSync /bin/sh ENOENT",
)

_THREAD_SUBSCRIPTION_TRANSIENT_STATES = frozenset(
    {
        "waiting_for_thread",
        "waiting_for_turn",
        "waiting_for_rollout",
        "ready_to_subscribe",
        "subscribing",
        "retrying",
    }
)

_MANAGED_FINISHED_RETENTION_SECONDS = 10 * 60

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

_WATCHING_REASONS = {
    "consecutive_failures",
    "connect_errors",
    "server_errors",
    "rate_limited",
    "retryable_client_errors",
    "reported_offline",
    "engine_status_missing",
    "engine_status_aging",
    "engine_status_stale",
}

__all__ = [
    "SCHEMA_VERSION",
    "ENGINE_FRESH_SECONDS",
    "ENGINE_STALE_SECONDS",
    "OUTBOX_DEGRADED_AGE_SECONDS",
    "OUTBOX_BROKEN_AGE_SECONDS",
    "DEGRADED_BACKLOG_COUNT",
    "BROKEN_BACKLOG_COUNT",
    "DISK_DEGRADED_BYTES",
    "DISK_BROKEN_BYTES",
    "ACTIVITY_RECENT_MINUTES",
    "ACTIVITY_RECENCY_BANDS",
    "RECENT_TOUCH_LIMIT",
    "CODEX_BRIDGE_LOG_TAIL_BYTES",
    "CODEX_BRIDGE_LIVE_RETRY_MARKER",
    "CODEX_BRIDGE_LIVE_SLOW_MARKER",
    "CODEX_BRIDGE_RUNTIME_NETWORK_ERROR_MARKER",
    "CODEX_BRIDGE_RUNTIME_FAILED_MARKER",
    "CODEX_BRIDGE_LIVE_DROPPED_MARKER",
    "PROVIDER_HOOK_DIAGNOSTIC_WINDOW",
    "PROVIDER_HOOK_DIAGNOSTIC_ACTIONABLE_WINDOW",
    "PROVIDER_HOOK_DIAGNOSTIC_FILE_LIMIT",
    "PROVIDER_HOOK_DIAGNOSTIC_EVENT_LIMIT",
    "CONTROL_PATH_MANAGED",
    "CONTROL_PATH_UNMANAGED",
    "LIVENESS_MODEL_CODEX_BRIDGE",
    "LIVENESS_MODEL_PROCESS_SCAN",
    "LIVENESS_MODEL_ENGINE_STATUS",
    "LIVENESS_MODEL_TRANSCRIPT",
    "CODEX_BIN_ENV",
    "OPENCODE_BIN_ENV",
    "ANTIGRAVITY_BIN_ENV",
    "_ZOMBIE_PROCESS_STATUSES",
    "_SHELL_SPAWN_ENOENT_PATTERNS",
    "_THREAD_SUBSCRIPTION_TRANSIENT_STATES",
    "_MANAGED_FINISHED_RETENTION_SECONDS",
    "_UUID_RE",
    "_WATCHING_REASONS",
]
