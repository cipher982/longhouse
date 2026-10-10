"""Synchronous catalog operations executed on catalogd's dedicated DB thread."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Collection
from collections.abc import Iterable
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL
from uuid import UUID
from uuid import uuid4
from uuid import uuid5

from sqlalchemy import Connection
from sqlalchemy import DateTime
from sqlalchemy import Engine
from sqlalchemy import Float
from sqlalchemy import MetaData
from sqlalchemy import and_
from sqlalchemy import bindparam
from sqlalchemy import case
from sqlalchemy import cast
from sqlalchemy import column
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy import tuple_
from sqlalchemy import union
from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from zerg.catalogd.fact_reducer import MAX_DELEGATION_VALUE_JSON_BYTES
from zerg.catalogd.fact_reducer import MAX_REDUCER_FACTS
from zerg.catalogd.fact_reducer import MAX_VALUE_JSON_BYTES
from zerg.catalogd.fact_reducer import ReducerFact
from zerg.catalogd.fact_reducer import _advance_commit_seq
from zerg.catalogd.fact_reducer import _current_commit_seq
from zerg.catalogd.fact_reducer import read_registry_lifecycle_heads
from zerg.catalogd.fact_reducer import reduce_fact_batch_setwise
from zerg.catalogd.fact_reducer import reducer_facts_from_machine_evidence
from zerg.catalogd.models import FactHead
from zerg.catalogd.models import FactParityDelta
from zerg.catalogd.models import LegacyMigrationSession
from zerg.catalogd.models import MediaObject
from zerg.catalogd.models import ProjectorState
from zerg.catalogd.models import RawObject as LiveRawObject
from zerg.catalogd.models import RenderGeneration
from zerg.catalogd.models import RenderObject
from zerg.catalogd.models import SessionMediaRef
from zerg.catalogd.models import SessionProviderFact
from zerg.catalogd.models import SessionTombstone as LiveSessionTombstone
from zerg.catalogd.models import StorageSession
from zerg.embedding_space import EMBEDDING_PROJECTOR_ID
from zerg.machine_evidence import MAX_MACHINE_EVIDENCE_BYTES
from zerg.machine_evidence import canonical_evidence_hash
from zerg.machine_evidence import canonical_value_json
from zerg.machine_evidence import machine_evidence_bytes
from zerg.models.live_store import LiveAPNSLiveActivityRegistration
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveConsoleTurn
from zerg.models.live_store import LiveControlLease
from zerg.models.live_store import LiveDeviceToken
from zerg.models.live_store import LiveHeartbeatStamp
from zerg.models.live_store import LiveInteractionRequest
from zerg.models.live_store import LiveLaunchReadiness
from zerg.models.live_store import LiveMachineControlOperation
from zerg.models.live_store import LiveNotificationClientPresence
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSession
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionInputAttachment
from zerg.models.live_store import LiveSessionInputReceipt
from zerg.models.live_store import LiveSessionLaunchAttempt
from zerg.models.live_store import LiveSessionLivePreview
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveSessionThreadAlias
from zerg.models.live_store import LiveTimelineCard
from zerg.models.live_store import LiveUser
from zerg.services.internal_sessions import SYNTHETIC_BENCH_PROJECTS
from zerg.services.internal_sessions import factory_title_assurance_session_clause
from zerg.services.session_title import RESUME_SEED_TOKEN
from zerg.services.session_title import sanitize_timeline_title
from zerg.services.session_title import sanitize_title
from zerg.services.session_visibility_policy import SessionVisibilityFacts
from zerg.services.session_visibility_policy import evaluate_origin_visibility
from zerg.services.session_visibility_policy import title_origin_eligible_clause
from zerg.storage_v2.contracts import DurableReceipt

# Log a stage breakdown when one write crosses this. Matched to the hosted hot
# API p95 budget (speed-of-light-database.md) so the log names writes that can
# push a caller past it, rather than narrating ordinary work.
_STAGE_TIMER_SLOW_MS = float(os.getenv("CATALOGD_STAGE_SLOW_MS", "250"))

DEVICE_TOKEN_LIMIT_PER_OWNER = 1_000
SESSION_READ_LIMIT = 100
# search-v2 claims newest sessions first. Sorting every eligible row costs
# ~86 ms per claim at a 40k-session rebuild backlog, held in the single writer;
# walking sessions by ix_sessions_last_activity_at stops at the first eligible
# rows (0.2 ms) but steps over every completed newer session. Walk when the
# backlog is large, sort when it is small (steady state, idle polls).
SEARCH_CLAIM_WALK_BACKLOG = 2_000
# Everything newer than the first row a walk could claim was completed, leased
# or waiting on retry, so the next walk starts there instead of re-stepping
# over the finished head of a rebuild (which made the walk O(completed) per
# claim, ~19k steps and catalogd at 100% midway through a 40k rebuild). New
# activity above the floor, expired leases and retries wait at most this long.
SEARCH_CLAIM_WALK_RESTART_SECONDS = 30.0
MACHINE_ENROLLMENT_LIMIT = 1_000
MACHINE_HEALTH_LIMIT = 100
# The capability projector consumes only its highest-ranked connection.  The
# ordering below is deliberately identical to that projector, so returning the
# winner preserves semantics while keeping a 100-row page bounded.
SESSION_CONNECTION_LIMIT = 1
SHADOW_STATE_FACT_HEAD_LIMIT = 256
SHADOW_STATE_HEALTH_SAMPLE_LIMIT = 100
SHADOW_STATE_HEALTH_WINDOW = timedelta(minutes=15)
_CONTROL_LEASE_TTL = timedelta(minutes=15)
# How long a dispatched Console turn counts as in flight for page admission
# once its own state stops being touched. Matches the control lease horizon:
# a turn nobody has advanced in fifteen minutes is not current work, and
# `live_console_turns` keeps such rows forever.
_CONSOLE_TURN_FRESHNESS = timedelta(minutes=15)
_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})
_SHADOW_PARITY_ENV = "LONGHOUSE_SHADOW_PARITY_ENABLED"
_MAX_PARITY_DELTAS = 2_048


def _has_durable_timeline_content(row) -> Any:
    """SQL predicate for content that may enter default history."""

    return or_(
        row.c.transcript_revision > 0,
        row.c.user_messages > 0,
        row.c.assistant_messages > 0,
        row.c.tool_calls > 0,
    )


def _run_bound_activity_is_live(*, session_id, thread_id, observed_at: datetime, prefix: str) -> Any:
    """A run-bound activity head saying the provider loop is thinking or running."""

    run = LiveSessionRun.__table__.alias(f"{prefix}_run")
    thread = LiveSessionThread.__table__.alias(f"{prefix}_thread")
    head = FactHead.__table__.alias(f"{prefix}_activity_head")
    value = head.c.value_json

    active_activity = (
        select(1)
        .select_from(
            run.join(
                thread,
                and_(
                    thread.c.id == run.c.thread_id,
                    thread.c.session_id == session_id,
                    thread.c.branch_kind == "root",
                    thread.c.is_primary == 1,
                ),
            ).join(head, head.c.session_id == session_id)
        )
        .where(
            or_(thread_id.is_(None), run.c.thread_id == thread_id),
            run.c.ended_at.is_(None),
            head.c.family == "activity",
            head.c.valid_until > observed_at,
            func.json_extract(value, "$.authority_class") == "provider_runtime",
            func.json_extract(value, "$.session_id") == session_id,
            func.json_extract(value, "$.run_id") == run.c.id,
            func.json_extract(value, "$.kind").in_(("thinking", "running")),
        )
        .exists()
    )
    return active_activity


def _attached_terminal_is_live(*, session_id, thread_id, observed_at: datetime, prefix: str) -> Any:
    """An exact, fresh control head saying a human terminal is attached."""

    run = LiveSessionRun.__table__.alias(f"{prefix}_run")
    thread = LiveSessionThread.__table__.alias(f"{prefix}_thread")
    control = LiveSessionConnection.__table__.alias(f"{prefix}_control")
    head = FactHead.__table__.alias(f"{prefix}_control_head")
    value = head.c.value_json

    control_valid_until = func.julianday(head.c.observed_at) + (cast(func.json_extract(value, "$.lease_ttl_ms"), Float) / 86_400_000.0)
    attached_terminal = (
        select(1)
        .select_from(
            run.join(
                thread,
                and_(
                    thread.c.id == run.c.thread_id,
                    thread.c.session_id == session_id,
                    thread.c.branch_kind == "root",
                    thread.c.is_primary == 1,
                ),
            )
            .join(control, control.c.run_id == run.c.id)
            .join(head, head.c.session_id == session_id)
        )
        .where(
            or_(thread_id.is_(None), run.c.thread_id == thread_id),
            run.c.ended_at.is_(None),
            control.c.released_at.is_(None),
            control.c.state.notin_(("released", "ended")),
            control.c.adapter_connection_id.is_not(None),
            control.c.lease_generation.is_not(None),
            head.c.family == "control",
            head.c.observed_at.is_not(None),
            control_valid_until > func.julianday(observed_at),
            func.json_extract(value, "$.authority_class") == "provider_control",
            func.json_extract(value, "$.session_id") == session_id,
            func.json_extract(value, "$.run_id") == run.c.id,
            func.json_extract(value, "$.connection_id") == control.c.adapter_connection_id,
            func.json_extract(value, "$.lease_generation") == control.c.lease_generation,
            func.json_extract(value, "$.terminal_attached") == 1,
        )
        .exists()
    )
    return attached_terminal


def _pending_interaction_is_open(*, session_id, observed_at: datetime, prefix: str) -> Any:
    """An unexpired provider interaction nobody has answered.

    Mirrors the canonical pending read the served pending-interaction axis is
    built from (`status = 'pending'`, `expires_at` in the future), so a
    Needs-you row cannot be paged out of the window.
    """

    interaction = LiveInteractionRequest.__table__.alias(f"{prefix}_interaction")
    return (
        select(1)
        .select_from(interaction)
        .where(
            interaction.c.session_id == session_id,
            interaction.c.status == "pending",
            or_(interaction.c.expires_at.is_(None), interaction.c.expires_at > observed_at),
        )
        .exists()
    )


def _delegated_work_is_pending(*, session_id, observed_at: datetime, prefix: str) -> Any:
    """A fresh delegation head reporting background work still in flight."""

    head = FactHead.__table__.alias(f"{prefix}_delegation_head")
    return (
        select(1)
        .select_from(head)
        .where(
            head.c.session_id == session_id,
            head.c.family == "delegation",
            head.c.valid_until > observed_at,
            cast(func.json_extract(head.c.value_json, "$.count"), Float) > 0,
        )
        .exists()
    )


def _console_turn_is_in_flight(*, session_id, observed_at: datetime, prefix: str) -> Any:
    """A Console turn dispatched and not yet terminal, and recently touched.

    Console dispatches are open before they carry any activity evidence, so the
    activity branch cannot see them. The gate is the turn's own `updated_at`:
    `live_console_turns` retains stale in-flight rows whose runs never ended
    (105 of them on the dogfood catalog), and admitting those would refill the
    page with the same dead rows this predicate exists to keep out.
    """

    turn = LiveConsoleTurn.__table__.alias(f"{prefix}_console_turn")
    return (
        select(1)
        .select_from(turn)
        .where(
            turn.c.session_id == session_id,
            turn.c.state.in_(("queued", "starting", "active", "draining")),
            turn.c.terminal_at.is_(None),
            turn.c.updated_at > observed_at - _CONSOLE_TURN_FRESHNESS,
        )
        .exists()
    )


def _empty_human_helm_is_open(
    *,
    session_id,
    thread_id,
    observed_at: datetime,
    possibly_open_ids: list[str] | None = None,
) -> Any:
    """Admit an empty human Helm only on canonical current-work evidence.

    Empty shells are not history. A human terminal launch enters the timeline
    only while a run-bound activity head says it is executing or an exact,
    fresh control head says its terminal is attached. The served projector
    still owns the final working-set classification from these same heads.

    These are two of the open predicate's branches, so its superset of
    possibly-open ids, when the caller has read it, prefilters them exactly.
    """

    live = or_(
        _run_bound_activity_is_live(
            session_id=session_id,
            thread_id=thread_id,
            observed_at=observed_at,
            prefix="timeline_empty",
        ),
        _attached_terminal_is_live(
            session_id=session_id,
            thread_id=thread_id,
            observed_at=observed_at,
            prefix="timeline_empty",
        ),
    )
    if possibly_open_ids is None:
        return live
    return and_(session_id.in_(possibly_open_ids), live)


def _timeline_current_work_is_open(
    *,
    session_id,
    thread_id,
    observed_at: datetime,
    possibly_open_ids: list[str] | None = None,
) -> Any:
    """SQL superset of the served `open` working set, for page admission and rank.

    The page window decides which sessions a client is ever told about, and the
    client then ranks what it receives. Both must agree on what a person has
    open right now, and the window may not rank on anything else. Ordering it by
    the newest reducer-head write put a rotating batch of *dead* sessions
    (re-stamped `detached` control heads, hundreds of them) at the top of a
    40-row page, so live sessions were pushed out every second, the client
    deleted every row that left the page, and re-added it on the way back in.

    This is deliberately a *superset* of `_working_set` in
    `zerg.services.session_state_contract`, branch for branch against that
    function: attached terminal, run-bound activity, pending interaction,
    pending delegation, and an in-flight Console turn. Over-inclusion costs a
    page slot and can never mislabel a row — the served projector classifies
    every returned row from the same snapshot and owns `working_set` — while
    under-inclusion would page a genuinely open session out of view.
    """

    # Every branch below is an EXISTS over a driving table (fact heads,
    # interaction requests, Console turns) joined back to this session. The
    # session ids those tables admit under each branch's own-column conditions
    # form an exact superset, so testing membership first changes no answer; it
    # only spares the other tens of thousands of catalog rows five correlated
    # subqueries each. SQLite materializes the non-correlated IN set once per
    # statement. On the dogfood catalog (40k storage sessions, 24k cards) the
    # correlated form cost ~260 ms of a 358 ms timeline read. A caller that
    # reads the set once in its own snapshot passes the ids, sparing the four
    # separate materializations one timeline statement would otherwise run.
    possibly_open = possibly_open_ids if possibly_open_ids is not None else _timeline_possibly_open_session_ids(observed_at=observed_at)
    return and_(
        session_id.in_(possibly_open),
        or_(
            _run_bound_activity_is_live(
                session_id=session_id,
                thread_id=thread_id,
                observed_at=observed_at,
                prefix="timeline_open_activity",
            ),
            _attached_terminal_is_live(
                session_id=session_id,
                thread_id=thread_id,
                observed_at=observed_at,
                prefix="timeline_open_terminal",
            ),
            _pending_interaction_is_open(
                session_id=session_id,
                observed_at=observed_at,
                prefix="timeline_open_interaction",
            ),
            _delegated_work_is_pending(
                session_id=session_id,
                observed_at=observed_at,
                prefix="timeline_open_delegation",
            ),
            _console_turn_is_in_flight(
                session_id=session_id,
                observed_at=observed_at,
                prefix="timeline_open_console",
            ),
        ),
    )


def _timeline_possibly_open_session_ids(*, observed_at: datetime) -> Any:
    """Session ids that could satisfy `_timeline_current_work_is_open`.

    One SELECT per branch over that branch's driving table, keeping only the
    conditions on the driving table's own columns. A branch can be true for a
    session only if its driving table has a qualifying row for it, so the union
    is a superset of every branch; the exact predicate still decides within it.
    """

    activity = FactHead.__table__.alias("timeline_possibly_open_activity")
    control = FactHead.__table__.alias("timeline_possibly_open_control")
    delegation = FactHead.__table__.alias("timeline_possibly_open_delegation")
    interaction = LiveInteractionRequest.__table__.alias("timeline_possibly_open_interaction")
    turn = LiveConsoleTurn.__table__.alias("timeline_possibly_open_turn")
    control_valid_until = func.julianday(control.c.observed_at) + (
        cast(func.json_extract(control.c.value_json, "$.lease_ttl_ms"), Float) / 86_400_000.0
    )
    return union(
        select(activity.c.session_id).where(
            activity.c.family == "activity",
            activity.c.valid_until > observed_at,
            func.json_extract(activity.c.value_json, "$.authority_class") == "provider_runtime",
            func.json_extract(activity.c.value_json, "$.kind").in_(("thinking", "running")),
        ),
        select(control.c.session_id).where(
            control.c.family == "control",
            control.c.observed_at.is_not(None),
            control_valid_until > func.julianday(observed_at),
            func.json_extract(control.c.value_json, "$.authority_class") == "provider_control",
            func.json_extract(control.c.value_json, "$.terminal_attached") == 1,
        ),
        select(interaction.c.session_id).where(
            interaction.c.status == "pending",
            or_(interaction.c.expires_at.is_(None), interaction.c.expires_at > observed_at),
        ),
        select(delegation.c.session_id).where(
            delegation.c.family == "delegation",
            delegation.c.valid_until > observed_at,
            cast(func.json_extract(delegation.c.value_json, "$.count"), Float) > 0,
        ),
        select(turn.c.session_id).where(
            turn.c.state.in_(("queued", "starting", "active", "draining")),
            turn.c.terminal_at.is_(None),
            turn.c.updated_at > observed_at - _CONSOLE_TURN_FRESHNESS,
        ),
    )


_MACHINE_ACTIVITY_TOP_PROJECTS = 3


def _activity_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _summarize_machine_activity_rows(
    rows: Any,
    *,
    window_start: datetime,
    utc_offset_minutes: int,
) -> list[dict[str, Any]]:
    """Fold timeline candidates into per-machine activity aggregates.

    Days are the caller's local calendar days (``utc_offset_minutes`` east of
    UTC); only sessions started inside the window count toward ``daily`` and
    ``top_projects``. ``latest`` is the most recent activity among every
    candidate, which includes open and unread work older than the window.
    ``open_session_ids`` are the candidates whose open flag is set, newest
    activity first, read from this one snapshot.
    """

    offset = timedelta(minutes=utc_offset_minutes)
    machines: dict[str, dict[str, Any]] = {}
    for row in rows:
        device_id = str(row["device_id"])
        entry = machines.setdefault(
            device_id,
            {
                "device_id": device_id,
                "sessions_started": 0,
                "daily": {},
                "projects": {},
                "latest": None,
                "latest_at": None,
                "open_candidates": 0,
                "open": [],
            },
        )
        order_at = _activity_utc(row["order_at"])
        session_id = str(row["session_id"])
        if int(row["open_now"] or 0):
            entry["open_candidates"] += 1
            entry["open"].append((order_at or window_start, session_id))
        if order_at is not None and (
            entry["latest_at"] is None
            or order_at > entry["latest_at"]
            or (order_at == entry["latest_at"] and session_id > entry["latest"]["session_id"])
        ):
            entry["latest_at"] = order_at
            entry["latest"] = {
                "session_id": session_id,
                "title": sanitize_timeline_title(row["title"]),
                "project": row["project"],
                "provider": row["provider"],
                "last_activity_at": order_at.isoformat(),
            }
        started_at = _activity_utc(row["started_at"])
        if started_at is None or started_at < window_start:
            continue
        entry["sessions_started"] += 1
        day = (started_at + offset).date().isoformat()
        provider = str(row["provider"] or "unknown")
        by_provider = entry["daily"].setdefault(day, {})
        by_provider[provider] = by_provider.get(provider, 0) + 1
        if row["project"]:
            project = str(row["project"])
            entry["projects"][project] = entry["projects"].get(project, 0) + 1
    result = []
    for device_id in sorted(machines):
        entry = machines[device_id]
        projects = sorted(entry.pop("projects").items(), key=lambda item: (-item[1], item[0]))
        entry.pop("latest_at")
        # Every open candidate is returned, newest first: the flag is a superset
        # of the served working set, so callers project each id before counting
        # a session live. It already requires current evidence (an activity or
        # control lease, a pending interaction, an active Console turn, pending
        # delegated work), so its size is bounded by what is running now, not by
        # history; a cap here would turn an exact count into a silent undercount.
        opened = sorted(entry.pop("open"), key=lambda item: (item[0], item[1]), reverse=True)
        entry["open_session_ids"] = [session_id for _, session_id in opened]
        entry["top_projects"] = [{"project": name, "sessions": count} for name, count in projects[:_MACHINE_ACTIVITY_TOP_PROJECTS]]
        entry["daily"] = [{"date": day, "by_provider": counts} for day, counts in sorted(entry["daily"].items())]
        result.append(entry)
    return result


_MACHINE_HEALTH_HEARTBEAT_FIELDS = frozenset(
    {
        "device_id",
        "received_at",
        "version",
        "last_ship_at",
        "last_ship_attempt_at",
        "last_ship_result",
        "last_ship_latency_ms",
        "last_ship_http_status",
        "spool_pending",
        "spool_dead",
        "parse_errors_1h",
        "ship_attempts_1h",
        "ship_successes_1h",
        "ship_rate_limited_1h",
        "ship_server_errors_1h",
        "ship_payload_rejections_1h",
        "ship_payload_too_large_1h",
        "ship_retryable_client_errors_1h",
        "ship_connect_errors_1h",
        "ship_latency_p50_ms_1h",
        "ship_latency_p95_ms_1h",
        "disk_free_bytes",
        "is_offline",
    }
)
_MACHINE_HEALTH_RAW_FIELDS = frozenset(
    {
        "archive_backlog",
        "history_import",
        "last_ship_error_kind",
        "last_ship_error_message",
        "ship_attempts_10m",
        "ship_successes_10m",
        "ship_rate_limited_10m",
        "ship_server_errors_10m",
        "ship_retryable_client_errors_10m",
        "ship_connect_errors_10m",
        "shipping_progress",
        "storage_v2_outbox",
        "runtime_event_outbox",
        "managed_launch_recovery",
    }
)
# This JSON string is encoded again inside the RPC response. A 32 KiB inner
# cap leaves deterministic headroom below the 8 MiB frame even when all 100
# rows contain escape-heavy content that doubles during outer JSON encoding.
_MACHINE_HEALTH_RAW_MAX_BYTES = 32 * 1024
_MACHINE_HEALTH_QUERY_FIELDS = _MACHINE_HEALTH_HEARTBEAT_FIELDS | frozenset({"raw_json"})


# Storage-v2 per-row title retry budget. Shared provider failures (auth,
# timeout, rate limit, or service outage) belong to the
# dependency circuit and do not spend this budget. The cap remains for
# row-specific malformed input or programming errors so one pathological
# session cannot retry forever.
MAX_TITLE_ATTEMPTS = 5
RETRYABLE_TITLE_ROW_ERRORS = frozenset({"empty_model_response"})
TITLE_ROW_TRANSIENT_RETRY_DELAY = timedelta(minutes=5)
TITLE_DEPENDENCY_USE_CASE = "session_title"
TITLE_DEPENDENCY_PROBE_DELAY = timedelta(seconds=60)
TITLE_DEPENDENCY_AVAILABILITY_PROBE_DELAY = timedelta(seconds=5)
TITLE_DEPENDENCY_LEGACY_REPAIR_VERSION = 2
TITLE_BACKLOG_DEGRADED_AFTER = timedelta(minutes=5)


def _title_auth_failure_clause(table):
    """Match only legacy title errors that identify shared credential failure."""

    error = func.lower(func.coalesce(table.c.title_last_error, ""))
    return or_(
        error.like("%401%"),
        error.like("%403%"),
        error.like("%unauthoriz%"),
        error.like("%forbidden%"),
        error.like("%authentication%"),
        error.like("%api key%"),
        error.like("%user not found%"),
    )


def _title_availability_failure_clause(table):
    """Match legacy shared-provider failures written before the circuit grew."""

    error = func.lower(func.coalesce(table.c.title_last_error, ""))
    return or_(
        error.like("%timeout%"),
        error.like("%timed out%"),
        error.like("%rate limit%"),
        error.like("%429%"),
        error.like("%500%"),
        error.like("%502%"),
        error.like("%503%"),
        error.like("%504%"),
        error.like("%temporarily unavailable%"),
        error.like("%connection%"),
    )


def _legacy_title_dependency_failure_clause(table):
    """Terminal debt attributable to a pre-circuit shared provider failure."""

    return and_(
        table.c.title_attempt_count >= MAX_TITLE_ATTEMPTS,
        or_(_title_auth_failure_clause(table), _title_availability_failure_clause(table)),
    )


def _retryable_title_row_failure_clause(table):
    """Row-local model output failures that remain durable obligations."""

    return func.lower(func.coalesce(table.c.title_last_error, "")).in_(RETRYABLE_TITLE_ROW_ERRORS)


def _title_anchor_replaceable_clause(table):
    """Anchors a Longhouse AI title may still write over.

    Empty, a provider-native fallback, or a leaked provider special token
    (`<｜DSML｜tool_calls>`) stored before the title sanitizer rejected them.
    Selection, the dependency claim, and completion must agree on this set, or
    the reconciler re-selects rows the claim then refuses, forever.
    """

    return or_(
        table.c.anchor_title.is_(None),
        table.c.anchor_title == "",
        table.c.anchor_title_source == "provider",
        table.c.anchor_title.like("<｜%"),
        table.c.anchor_title.like("<|%"),
    )


def _title_anchor_replaceable(anchor_title: object, anchor_title_source: object) -> bool:
    anchor = str(anchor_title or "")
    return not anchor or anchor_title_source == "provider" or anchor.startswith(("<｜", "<|"))


def _storage_title_obligation_clause(table):
    """Rows that should eventually receive an AI title."""

    factory_assurance = factory_title_assurance_session_clause(table)
    worker_only = (
        select(LiveSessionThread.__table__.c.id)
        .where(
            LiveSessionThread.__table__.c.session_id == table.c.session_id,
            LiveSessionThread.__table__.c.is_primary == 1,
            LiveSessionThread.__table__.c.branch_kind == "subagent",
        )
        .exists()
    )
    return and_(
        table.c.user_messages > 0,
        or_(table.c.provider != "claude", table.c.semantic_projection_version >= 1),
        # A provider-native name is only a fallback: it never blocks the
        # Longhouse AI title, the single title authority. An unanchored row
        # and a provider-sourced anchor both remain obligations; only an
        # anchor the AI itself wrote (or a legacy anchor with no recorded
        # source, already served as "ai") is done.
        _title_anchor_replaceable_clause(table),
        table.c.first_user_message_preview.is_not(None),
        func.length(func.trim(table.c.first_user_message_preview)) > 0,
        ~table.c.first_user_message_preview.contains(RESUME_SEED_TOKEN, autoescape=True),
        ~func.lower(func.coalesce(table.c.project, "")).in_(SYNTHETIC_BENCH_PROJECTS),
        or_(table.c.title_last_error.is_(None), table.c.title_last_error != "no_meaningful_user_text"),
        or_(factory_assurance, title_origin_eligible_clause(table)),
        ~worker_only,
    )


def _storage_title_candidate_clause(table, *, observed_at: datetime):
    """Due executable subset of the durable title obligation set."""

    return and_(
        _storage_title_obligation_clause(table),
        or_(table.c.title_retry_at.is_(None), table.c.title_retry_at <= observed_at),
        or_(
            table.c.title_attempt_count < MAX_TITLE_ATTEMPTS,
            table.c.title_dependency_incident_id.is_not(None),
            _retryable_title_row_failure_clause(table),
        ),
    )


def _json_launch_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        **result,
        "session_id": str(result["session_id"]),
    }


def _receipt_error_code(receipt: LiveSessionInputReceipt | None) -> str | None:
    if receipt is None or not receipt.error_json:
        return None
    try:
        payload = json.loads(receipt.error_json)
    except (TypeError, ValueError):
        return None
    code = payload.get("code") if isinstance(payload, dict) else None
    return str(code).strip() or None if code else None


def _console_turn_attachments(turn: LiveConsoleTurn) -> dict[str, Any]:
    """Decode `attachments_json`; an absent or malformed column is no attachments."""
    raw = getattr(turn, "attachments_json", None)
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _live_console_turn_dto(
    turn: LiveConsoleTurn,
    *,
    message: str | None = None,
    client_request_id: str | None = None,
    provider_config: str | None = None,
    model: str | None = None,
    resume_session_file: str | None = None,
    error_code: str | None = None,
    reporting_turn_ids: Collection[str] = (),
) -> dict[str, Any]:
    config = _decode_json_object(provider_config)
    config.pop("model", None)
    if model is not None:
        config["model"] = model
    return {
        "turn_id": turn.id,
        "session_id": turn.session_id,
        "thread_id": turn.thread_id,
        "run_id": turn.run_id,
        "receipt_id": turn.receipt_id,
        "state": turn.state,
        "report_id": turn.report_id,
        "attachments": _console_turn_attachments(turn).get("refs") or [],
        "provider": turn.provider,
        "device_id": turn.device_id,
        "cwd": turn.cwd,
        "message": message,
        "client_request_id": client_request_id,
        "origin": str(getattr(turn, "origin", None) or "user"),
        "wake_id": getattr(turn, "wake_id", None),
        "invocation_id": getattr(turn, "invocation_id", None),
        "provider_config": config,
        "resume_provider_thread_id": turn.resume_provider_thread_id,
        "resume_session_file": resume_session_file,
        "fork_from_provider_thread_id": turn.fork_from_provider_thread_id,
        "error_code": error_code,
        "error": turn.error,
        # Reconciliation uses updated_at as the start time of the current
        # state. Keep both timestamps so a queued turn can wait in FIFO
        # without consuming the starting-state TTL.
        "created_at": _encode_datetime(turn.created_at),
        "updated_at": _encode_datetime(turn.updated_at),
        # An idempotent replay returns this row unchanged; the caller must not
        # present an old nonterminal turn as current work. Replays pass the
        # reporting evidence so they agree with the receipt reads.
        "is_fresh": _console_turn_state_is_fresh(turn, reporting_turn_ids=reporting_turn_ids),
    }


def _live_thread_source_path(orm: Session, *, thread_id: str, provider: str) -> str | None:
    row = (
        orm.query(LiveSessionThreadAlias.alias_value)
        .filter(
            LiveSessionThreadAlias.thread_id == str(thread_id),
            LiveSessionThreadAlias.provider == str(provider),
            LiveSessionThreadAlias.alias_kind == "source_path",
        )
        .order_by(
            LiveSessionThreadAlias.last_seen_at.desc(),
            LiveSessionThreadAlias.first_seen_at.desc(),
            LiveSessionThreadAlias.id.desc(),
        )
        .first()
    )
    return str(row[0]).strip() if row and str(row[0] or "").strip() else None


CONSOLE_TURN_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
# A provider adapter's terminal_signal is the Console turn's result. The same
# mapping used to live in the runtime HTTP route, which settled the turn in a
# second catalog call after the runtime batch had already ended the run.
CONSOLE_TURN_OUTCOME_BY_RUN_TERMINAL = {
    "run_completed": "completed",
    "run_failed": "failed",
    "run_cancelled": "cancelled",
}


def _console_turn_dispatch_dto(orm: Session, turn: LiveConsoleTurn) -> dict[str, Any]:
    receipt = orm.get(LiveSessionInputReceipt, turn.receipt_id)
    thread = orm.get(LiveSessionThread, turn.thread_id)
    return _live_console_turn_dto(
        turn,
        message=receipt.text if receipt is not None else None,
        client_request_id=receipt.client_request_id if receipt is not None else None,
        provider_config=thread.provider_config_json if thread is not None else None,
        model=turn.model,
        error_code=_receipt_error_code(receipt),
        resume_session_file=(_live_thread_source_path(orm, thread_id=thread.id, provider=turn.provider) if thread is not None else None),
    )


def _starting_console_turn_dto(orm: Session, *, thread_id: str) -> dict[str, Any] | None:
    """Return the thread's durable `starting` owner, for exact-replay redispatch.

    A process can die after a terminal transition claimed the next turn but
    before its machine command was sent. Its run_id is also the idempotent
    machine command_id, so returning it on replay is safe.
    """

    starting = (
        orm.query(LiveConsoleTurn)
        .filter(LiveConsoleTurn.thread_id == thread_id, LiveConsoleTurn.state == "starting")
        .order_by(LiveConsoleTurn.created_at.asc(), LiveConsoleTurn.id.asc())
        .first()
    )
    return _console_turn_dispatch_dto(orm, starting) if starting is not None else None


def _create_console_turn_rows(
    orm: Session,
    *,
    session: LiveSessionCatalog,
    thread: LiveSessionThread,
    owner_id: int,
    message: str,
    client_request_id: str | None,
    created_at: datetime,
    origin: str = "user",
    wake_id: str | None = None,
    invocation_id: str | None = None,
    report_id: str | None = None,
    attachments_json: str | None = None,
    model: str | None = None,
    receipt_id: str | None = None,
) -> tuple[LiveConsoleTurn, LiveSessionInputReceipt, str | None]:
    """Create a receipt and its FIFO turn, claiming it if the thread is idle."""

    resume_alias = (
        orm.query(LiveSessionThreadAlias)
        .filter(
            LiveSessionThreadAlias.thread_id == thread.id,
            LiveSessionThreadAlias.provider == session.provider,
            LiveSessionThreadAlias.alias_kind == "provider_session_id",
        )
        .order_by(
            LiveSessionThreadAlias.last_seen_at.desc(),
            LiveSessionThreadAlias.first_seen_at.desc(),
            LiveSessionThreadAlias.id.desc(),
        )
        .first()
    )
    source_path = _live_thread_source_path(orm, thread_id=thread.id, provider=session.provider)
    receipt_id = receipt_id or str(uuid4())
    now = _as_aware_utc(created_at) or datetime.now(UTC)
    receipt = LiveSessionInputReceipt(
        id=receipt_id,
        owner_id=owner_id,
        session_id=session.session_id,
        thread_id=thread.id,
        provider=session.provider,
        device_id=thread.device_id,
        client_request_id=client_request_id,
        intent="auto",
        status="queued",
        text=message,
        created_at=now,
        updated_at=now,
    )
    turn = LiveConsoleTurn(
        id=str(uuid4()),
        session_id=session.session_id,
        thread_id=thread.id,
        receipt_id=receipt_id,
        origin=origin,
        wake_id=wake_id,
        invocation_id=invocation_id,
        state="queued",
        report_id=report_id,
        attachments_json=attachments_json,
        provider=session.provider,
        device_id=thread.device_id,
        cwd=thread.cwd,
        model=model,
        resume_provider_thread_id=resume_alias.alias_value if resume_alias is not None else None,
        created_at=now,
        updated_at=now,
    )
    orm.add_all([receipt, turn])
    owner = (
        orm.query(LiveConsoleTurn.id)
        .filter(
            LiveConsoleTurn.thread_id == thread.id,
            LiveConsoleTurn.state.in_(("starting", "active", "draining")),
        )
        .first()
    )
    if owner is None:
        run_id = str(uuid4())
        turn.run_id = run_id
        turn.state = "starting"
        receipt.status = "delivering"
        receipt.delivery_request_id = run_id
        orm.add(
            LiveSessionRun(
                id=run_id,
                thread_id=thread.id,
                provider=session.provider,
                host_id=thread.device_id,
                cwd=thread.cwd,
                launch_origin="longhouse_spawned",
                started_at=now,
            )
        )
    return turn, receipt, source_path


_WAKE_TRIGGER_LABELS = {
    "monitor_event": "Monitor event",
    "task_completed": "Background task finished",
    "subagent_result": "Background agent finished",
    "scheduled": "Scheduled wake-up",
}


def _wake_trigger_summary(trigger: Mapping[str, Any]) -> str:
    label = _WAKE_TRIGGER_LABELS.get(str(trigger.get("kind") or "").strip(), "Background work update")
    summary = str(trigger.get("summary") or "").strip()
    if not summary:
        task_ids = trigger.get("task_ids")
        if isinstance(task_ids, list):
            summary = ", ".join(str(task_id).strip() for task_id in task_ids[:8] if str(task_id).strip())
    if not summary:
        return label
    return f"{label}: {summary[:512]}"


def _enqueue_console_wake_turn(orm: Session, event: Any, *, observed_at: datetime) -> dict[str, Any] | None:
    """Turn one provider wake into the same durable receipt/FIFO used by user input."""

    payload = event.payload if isinstance(event.payload, Mapping) else {}
    wake_id = str(payload.get("wake_id") or "").strip()
    invocation_id = str(payload.get("invocation_id") or "").strip()
    provider_thread_id = str(payload.get("provider_thread_id") or "").strip()
    session_id = str(event.session_id or "")
    reason = None
    if not wake_id or len(wake_id) > 249 or not invocation_id or len(invocation_id) > 255:
        reason = "invalid_wake_identity"
    elif not provider_thread_id or len(provider_thread_id) > 1024:
        reason = "provider_thread_missing"
    session = orm.get(LiveSessionCatalog, session_id) if session_id else None
    if reason is None and (session is None or str(session.origin_kind or "").strip().lower() != "console"):
        reason = "session_not_console"
    thread = orm.get(LiveSessionThread, str(session.primary_thread_id)) if session is not None and session.primary_thread_id else None
    if reason is None and (
        thread is None
        or event.thread_id is None
        or str(thread.id) != str(event.thread_id)
        or str(thread.id) != str(session.primary_thread_id)
    ):
        reason = "console_thread_missing_or_mismatched"
    provider = str(event.provider or "").strip().lower()
    if reason is None and (
        provider != str(session.provider or "").strip().lower()
        or (event.device_id is not None and str(event.device_id) != str(thread.device_id))
    ):
        reason = "provider_or_device_mismatch"
    alias = None
    if reason is None:
        alias = (
            orm.query(LiveSessionThreadAlias)
            .filter(
                LiveSessionThreadAlias.provider == provider,
                LiveSessionThreadAlias.alias_kind == "provider_session_id",
                LiveSessionThreadAlias.alias_value == provider_thread_id,
            )
            .one_or_none()
        )
        if alias is None or str(alias.thread_id) != str(thread.id):
            reason = "provider_thread_mismatch"
    if reason is not None:
        logging.getLogger(__name__).warning(
            "Ignoring Console wake signal reason=%s session=%s thread=%s provider=%s wake_id=%s",
            reason,
            session_id or None,
            event.thread_id,
            event.provider,
            wake_id or None,
        )
        return None

    owner_key = CatalogStore._resolve_session_owner_id(orm.connection(), session_id=session_id)
    try:
        owner_id = int(owner_key) if owner_key is not None else None
    except (TypeError, ValueError):
        owner_id = None
    if owner_id is None or not CatalogStore._session_explicitly_belongs_to_owner(
        orm.connection(),
        session_id=session_id,
        owner_id=owner_id,
    ):
        logging.getLogger(__name__).warning(
            "Ignoring Console wake signal reason=owner_missing session=%s wake_id=%s",
            session_id,
            wake_id,
        )
        return None
    client_request_id = f"wake:{wake_id}"
    existing_receipt = (
        orm.query(LiveSessionInputReceipt)
        .filter(
            LiveSessionInputReceipt.owner_id == owner_id,
            LiveSessionInputReceipt.session_id == session_id,
            LiveSessionInputReceipt.client_request_id == client_request_id,
        )
        .one_or_none()
    )
    if existing_receipt is not None:
        return None
    active_turn = (
        orm.query(LiveConsoleTurn.id)
        .filter(
            LiveConsoleTurn.thread_id == thread.id,
            LiveConsoleTurn.state.in_(("starting", "active", "draining")),
        )
        .first()
    )
    execution_owner = (
        orm.query(LiveSessionRun.id)
        .join(LiveSessionConnection, LiveSessionConnection.run_id == LiveSessionRun.id)
        .filter(
            LiveSessionRun.thread_id == thread.id,
            LiveSessionRun.ended_at.is_(None),
            LiveSessionConnection.acquisition_kind.in_(("spawned_control", "adopted_control")),
            LiveSessionConnection.released_at.is_(None),
        )
        .first()
    )
    if execution_owner is not None and active_turn is None:
        logging.getLogger(__name__).warning(
            "Ignoring Console wake signal reason=execution_owner_conflict session=%s wake_id=%s",
            session_id,
            wake_id,
        )
        return None
    trigger = payload.get("trigger") if isinstance(payload.get("trigger"), Mapping) else {}
    turn, receipt, source_path = _create_console_turn_rows(
        orm,
        session=session,
        thread=thread,
        owner_id=owner_id,
        message=_wake_trigger_summary(trigger),
        client_request_id=client_request_id,
        created_at=_as_aware_utc(event.occurred_at) or observed_at,
        origin="wake",
        wake_id=wake_id,
        invocation_id=invocation_id,
    )
    return (
        {
            "owner_id": owner_id,
            "turn": _live_console_turn_dto(
                turn,
                message=receipt.text,
                client_request_id=receipt.client_request_id,
                provider_config=thread.provider_config_json,
                model=turn.model,
                resume_session_file=source_path,
            ),
        }
        if turn.state == "starting"
        else None
    )


# What a close proved about the invocation's processes (the engine's
# `InvocationCleanup`). Closing provider input closes the invocation; whether its
# processes are gone is a separate fact, so the notice says when they may not be.
# Nothing reaps them later: the managed-process janitor only reports.
INVOCATION_CLEANUP_NOTICES = {
    "complete": None,
    "survivors": "Some processes didn't exit and may still be running.",
    "unverified": "Longhouse couldn't confirm all its processes exited, and left any that remain alone.",
}


def _invocation_close_notice(reason: str, stopped: list[Mapping[str, Any]], cleanup: str | None = None) -> str:
    descriptions = [
        str(item.get("description") or item.get("kind") or item.get("id") or "background work").strip()[:512] for item in stopped
    ]
    count = len(stopped)
    task_noun = "background task" if count == 1 else "background tasks"
    summary = "; ".join(descriptions)
    if reason == "machine_agent_restart":
        notice = f"Longhouse restarted; {count} {task_noun} stopped: {summary}"
    else:
        notice = f"Stopped {count} {task_noun}: {summary}"
    cleanup_line = INVOCATION_CLEANUP_NOTICES.get(cleanup or "")
    if cleanup_line:
        notice = f"{notice if notice.endswith('.') else notice + '.'} {cleanup_line}"
    return notice


def _record_console_invocation_closed_notice(orm: Session, event: Any, *, observed_at: datetime) -> None:
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    invocation_id = str(payload.get("invocation_id") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    raw_stopped = payload.get("stopped")
    # Absent from engines before the cleanup outcome existed; an unknown value
    # adds no line rather than dropping the notice.
    cleanup = str(payload.get("cleanup") or "").strip() or None
    session_id = str(event.session_id or "")
    if (
        not invocation_id
        or len(invocation_id) > 249
        or reason not in {"user_stop", "machine_agent_restart"}
        or not isinstance(raw_stopped, list)
        or not raw_stopped
        or len(raw_stopped) > 256
        or any(not isinstance(item, Mapping) for item in raw_stopped)
        or str(event.dedupe_key or "") != f"close:{invocation_id}"
        or event.run_id is None
        or event.thread_id is None
        or event.device_id is None
    ):
        return
    stopped = [item for item in raw_stopped if isinstance(item, Mapping)]
    if len(stopped) != len(raw_stopped):
        return
    session = orm.get(LiveSessionCatalog, session_id) if session_id else None
    if (
        session is None
        or str(session.origin_kind or "").strip().lower() != "console"
        or session.closed_at is not None
        or str(session.primary_thread_id or "") != str(event.thread_id)
    ):
        return
    thread = orm.get(LiveSessionThread, str(event.thread_id))
    provider = str(event.provider or "").strip().lower()
    if (
        thread is None
        or str(thread.session_id) != session_id
        or str(thread.device_id or "") != str(event.device_id)
        or provider not in {"claude", "codex", "omp"}
        or provider != str(session.provider or "").strip().lower()
    ):
        return
    owner_key = CatalogStore._resolve_session_owner_id(orm.connection(), session_id=session_id)
    try:
        owner_id = int(owner_key) if owner_key is not None else None
    except (TypeError, ValueError):
        owner_id = None
    if owner_id is None or not CatalogStore._session_explicitly_belongs_to_owner(
        orm.connection(),
        session_id=session_id,
        owner_id=owner_id,
    ):
        return

    client_request_id = f"close:{invocation_id}"
    existing = (
        orm.query(LiveSessionInputReceipt.id)
        .filter(
            LiveSessionInputReceipt.owner_id == owner_id,
            LiveSessionInputReceipt.session_id == session_id,
            LiveSessionInputReceipt.client_request_id == client_request_id,
        )
        .first()
    )
    if existing is not None:
        return

    run_id = str(event.run_id)
    latest_run = (
        orm.query(LiveSessionRun)
        .filter(LiveSessionRun.thread_id == str(event.thread_id))
        .order_by(LiveSessionRun.started_at.desc(), LiveSessionRun.id.desc())
        .first()
    )
    if latest_run is None or str(latest_run.id) != run_id or latest_run.ended_at is None:
        return
    turn = (
        orm.query(LiveConsoleTurn)
        .filter(
            LiveConsoleTurn.session_id == session_id,
            LiveConsoleTurn.thread_id == str(event.thread_id),
            LiveConsoleTurn.run_id == run_id,
        )
        .one_or_none()
    )
    if turn is None or turn.state not in CONSOLE_TURN_TERMINAL_STATES:
        return

    occurred_at = _as_aware_utc(event.occurred_at) or observed_at
    orm.add(
        LiveSessionInputReceipt(
            id=str(uuid4()),
            owner_id=owner_id,
            session_id=session_id,
            thread_id=str(event.thread_id),
            provider=provider,
            device_id=str(event.device_id),
            origin="longhouse",
            client_request_id=client_request_id,
            intent="auto",
            status="delivered",
            text=_invocation_close_notice(reason, stopped, cleanup),
            created_at=occurred_at,
            updated_at=occurred_at,
        )
    )


def _settle_console_turn(
    orm: Session,
    turn: LiveConsoleTurn,
    receipt: LiveSessionInputReceipt | None,
    *,
    next_state: str,
    error: str | None,
    error_code: str | None,
    now: datetime,
) -> dict[str, Any] | None:
    """Write one Console turn transition and everything that derives from it.

    For a terminal state this is the single place the turn result, the session's
    unread stamp, the run's end, the released control connections and the next
    FIFO claim are written, inside the caller's transaction. Returns the claimed
    next turn, if any, for the caller to dispatch.
    """

    turn.state = next_state
    turn.updated_at = now
    turn.error = error
    if receipt is not None:
        # A Stop after the provider took the input leaves it delivered: the
        # receipt describes delivery, the turn describes the outcome. Only an
        # input cancelled before delivery (still queued/starting) is cancelled.
        stopped_after_delivery = next_state == "cancelled" and receipt.status == "delivered"
        if next_state in {"active", "completed"}:
            receipt.status = "delivered"
        elif next_state == "failed":
            receipt.status = "failed"
        elif next_state == "cancelled" and not stopped_after_delivery:
            receipt.status = "cancelled"
        receipt.error_json = (
            json.dumps({"code": error_code, "message": error}, sort_keys=True, separators=(",", ":"))
            if error and not stopped_after_delivery
            else None
        )
        receipt.updated_at = now
    if next_state not in CONSOLE_TURN_TERMINAL_STATES:
        return None
    turn.terminal_at = now
    # Console unread acknowledgement: denormalize the terminal result onto the
    # catalog row so unread derives from two session-row columns (spec:
    # console-unread-acknowledgement.md).
    catalog_row = orm.get(LiveSessionCatalog, turn.session_id)
    if catalog_row is not None:
        catalog_row.last_console_result_at = now
        catalog_row.last_console_result_outcome = next_state
        catalog_row.updated_at = now
    run = orm.get(LiveSessionRun, turn.run_id)
    if run is not None:
        # The turn result is the Console run's terminal evidence. Keep the end
        # time and exit status the runtime reducer already wrote for the same
        # signal: its `exit_0` or explicit exit_status is richer than the turn
        # outcome.
        if run.ended_at is None:
            run.ended_at = now
            run.exit_status = error_code or next_state
        elif not run.exit_status:
            run.exit_status = error_code or next_state
        for connection_row in (
            orm.query(LiveSessionConnection)
            .filter(LiveSessionConnection.run_id == turn.run_id, LiveSessionConnection.released_at.is_(None))
            .all()
        ):
            connection_row.state = "ended"
            connection_row.released_at = now
            connection_row.last_health_at = now
            connection_row.can_send_input = False
            connection_row.can_interrupt = False
            connection_row.can_terminate = False
            connection_row.can_tail_output = False
            connection_row.can_resume = False
    next_turn = (
        orm.query(LiveConsoleTurn)
        .filter(LiveConsoleTurn.thread_id == turn.thread_id, LiveConsoleTurn.state == "queued")
        .order_by(LiveConsoleTurn.created_at.asc(), LiveConsoleTurn.id.asc())
        .first()
    )
    if next_turn is None:
        return None
    next_receipt = orm.get(LiveSessionInputReceipt, next_turn.receipt_id)
    resume_alias = (
        orm.query(LiveSessionThreadAlias)
        .filter(
            LiveSessionThreadAlias.thread_id == next_turn.thread_id,
            LiveSessionThreadAlias.provider == next_turn.provider,
            LiveSessionThreadAlias.alias_kind == "provider_session_id",
        )
        .order_by(
            LiveSessionThreadAlias.last_seen_at.desc(),
            LiveSessionThreadAlias.first_seen_at.desc(),
            LiveSessionThreadAlias.id.desc(),
        )
        .first()
    )
    next_run_id = str(uuid4())
    next_turn.run_id = next_run_id
    next_turn.state = "starting"
    next_turn.resume_provider_thread_id = resume_alias.alias_value if resume_alias is not None else None
    next_turn.updated_at = now
    if next_receipt is not None:
        next_receipt.status = "delivering"
        next_receipt.delivery_request_id = next_run_id
        next_receipt.updated_at = now
    orm.add(
        LiveSessionRun(
            id=next_run_id,
            thread_id=next_turn.thread_id,
            provider=next_turn.provider,
            host_id=next_turn.device_id,
            cwd=next_turn.cwd,
            launch_origin="longhouse_spawned",
            started_at=now,
        )
    )
    return _console_turn_dispatch_dto(orm, next_turn)


def _settle_console_turns_from_runtime(orm: Session, events: list[Any], *, observed_at: datetime) -> list[dict[str, Any]]:
    """Settle each Console turn whose adapter reported a run terminal in this batch.

    Runs inside the runtime batch's transaction, so the turn result, the run end
    and the served facts commit together or not at all. Returns the next turns
    to dispatch: freshly claimed ones, plus -- on an exact replay of a terminal
    already applied -- the durable `starting` owner a crashed dispatch left.
    """

    dispatch: list[dict[str, Any]] = []
    for event in events:
        if event.kind == "wake_signal":
            wake_turn = _enqueue_console_wake_turn(orm, event, observed_at=observed_at)
            if wake_turn is not None:
                dispatch.append(wake_turn)
            continue
        if event.kind == "invocation_closed":
            _record_console_invocation_closed_notice(orm, event, observed_at=observed_at)
            continue
        outcome = CONSOLE_TURN_OUTCOME_BY_RUN_TERMINAL.get(str((event.payload or {}).get("terminal_state") or ""))
        if (
            event.kind != "terminal_signal"
            or outcome is None
            or event.run_id is None
            or event.session_id is None
            or event.thread_id is None
            or event.device_id is None
        ):
            continue
        turn = (
            orm.query(LiveConsoleTurn)
            .filter(
                LiveConsoleTurn.run_id == str(event.run_id),
                LiveConsoleTurn.session_id == str(event.session_id),
                LiveConsoleTurn.thread_id == str(event.thread_id),
                LiveConsoleTurn.provider == event.provider,
                LiveConsoleTurn.device_id == event.device_id,
            )
            .one_or_none()
        )
        if turn is None:
            continue
        receipt = orm.get(LiveSessionInputReceipt, turn.receipt_id)
        owner_id = receipt.owner_id if receipt is not None else None
        if turn.state in CONSOLE_TURN_TERMINAL_STATES:
            next_turn = _starting_console_turn_dto(orm, thread_id=turn.thread_id) if turn.state == outcome else None
        else:
            next_turn = _settle_console_turn(
                orm,
                turn,
                receipt,
                next_state=outcome,
                error=None if outcome == "completed" else _console_terminal_error(event.payload),
                error_code=None,
                now=_as_aware_utc(event.occurred_at) or observed_at,
            )
        if next_turn is not None and owner_id is not None:
            dispatch.append({"owner_id": int(owner_id), "turn": next_turn})
    return dispatch


def _console_terminal_error(payload: dict[str, Any]) -> str:
    """The terminal state plus the adapter's reason, when it sent one.

    A bare "run_failed" hid a Codex 401 on 2026-10-07; the engine already
    ships the provider's message as ``stderr_tail``.
    """
    state = str(payload["terminal_state"])
    detail = str(payload.get("stderr_tail") or "").strip()
    return f"{state}: {detail[-1000:]}" if detail else state


def _canonical_outbox_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"__longhouse_datetime__": (_as_aware_utc(value) or value).isoformat()}
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _canonical_outbox_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical_outbox_value(item) for item in value]
    return value


def _interaction_id(request_key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"longhouse-pause:{request_key}"))


def _interaction_dto(row: LiveInteractionRequest) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "session_id": str(row.session_id),
        "runtime_key": str(row.runtime_key),
        "run_id": row.run_id,
        "provider": str(row.provider),
        "request_key": str(row.request_key),
        "provider_request_id": row.provider_request_id,
        "source": row.source,
        "reply_transport": row.reply_transport,
        "kind": str(row.kind),
        "status": str(row.status),
        "can_respond": bool(row.can_respond),
        "projection": _bounded_pause_projection(row.projection_json, compact=False),
        "response_payload": row.response_payload_json if isinstance(row.response_payload_json, dict) else None,
        "response_text": row.response_text,
        "occurred_at": _encode_datetime(row.occurred_at),
        "last_seen_at": _encode_datetime(row.last_seen_at),
        "resolved_at": _encode_datetime(row.resolved_at),
        "expires_at": _encode_datetime(row.expires_at),
    }


def _runtime_interaction_dto(runtime: LiveRuntimeState) -> dict[str, Any] | None:
    projection = _bounded_pause_projection(runtime.pending_interaction_projection_json, compact=False)
    request_key = str(runtime.pending_interaction_id or "").strip()
    if projection is None or not request_key:
        return None
    interaction_id = str(projection.get("id") or _interaction_id(request_key))
    provider = str(runtime.provider)
    kind = str(runtime.pending_interaction_kind or projection.get("kind") or "structured_question")
    request_prefix = f"{provider}:{runtime.runtime_key}:"
    provider_request_id = request_key[len(request_prefix) :] if request_key.startswith(request_prefix) else None
    source = None
    reply_transport = None
    if kind == "permission_prompt" and provider == "claude":
        source = "claude_permission_gate"
        reply_transport = "claude_pretooluse_pull"
    elif kind == "permission_prompt" and provider == "cursor":
        source = "cursor_permission_gate"
        reply_transport = "cursor_permission_poll"
    elif kind == "permission_prompt" and provider == "opencode":
        source = "opencode_bridge"
        reply_transport = "managed_push"
    return {
        "id": interaction_id,
        "session_id": str(runtime.session_id),
        "runtime_key": str(runtime.runtime_key),
        "provider": provider,
        "request_key": request_key,
        "provider_request_id": provider_request_id,
        "source": source,
        "reply_transport": reply_transport,
        "kind": kind,
        "status": "pending",
        "can_respond": bool(runtime.pending_interaction_can_respond),
        "projection": projection,
        "response_payload": None,
        "response_text": None,
        "occurred_at": _encode_datetime(runtime.pending_interaction_opened_at),
        "last_seen_at": _encode_datetime(runtime.pending_interaction_updated_at or runtime.updated_at),
        "resolved_at": None,
        "expires_at": projection.get("expires_at"),
    }


def _live_control_session_dto(session: Any) -> dict[str, Any]:
    return {
        "id": str(session.id),
        "provider": session.provider,
        "device_id": session.device_id,
        "device_name": session.device_name,
        "cwd": session.cwd,
        "project": session.project,
        "git_repo": session.git_repo,
        "git_branch": session.git_branch,
        "ended_at": _encode_datetime(session.ended_at),
        "closed_at": _encode_datetime(session.closed_at),
        "close_reason": session.close_reason,
        "permission_mode": session.permission_mode,
        "primary_thread_id": str(session.primary_thread_id) if session.primary_thread_id else None,
    }


class _RowReceipt:
    """Attribute access over a row mapping, so one DTO serves ORM rows and core rows."""

    def __init__(self, row: Any) -> None:
        self._row = row

    def __getattr__(self, name: str) -> Any:
        try:
            return self._row[name]
        except KeyError:
            return None


def _console_turn_state_is_fresh(
    turn: Any,
    *,
    observed_at: datetime | None = None,
    reporting_turn_ids: Collection[str] = (),
) -> bool:
    """Keep terminal evidence authoritative; age out nonterminal observations.

    A turn's `updated_at` is when it entered its state, not when anything last
    vouched for it, so a nonterminal turn older than the horizon is still
    current while its run is reporting (`reporting_turn_ids`, from
    `_console_turns_with_reporting_run`).
    """
    if getattr(turn, "terminal_at", None) is not None:
        return True
    updated_at = _as_aware_utc(getattr(turn, "updated_at", None))
    if updated_at is not None and updated_at > (observed_at or datetime.now(UTC)) - _CONSOLE_TURN_FRESHNESS:
        return True
    return str(getattr(turn, "id", "")) in reporting_turn_ids


def _console_turns_with_reporting_run(orm: Session, turns: Iterable[Any], *, observed_at: datetime) -> frozenset[str]:
    """Ids of past-horizon nonterminal `turns` whose run is still reporting.

    A turn stays `active` through a long tool call, and the turns queued behind
    it stay `queued`, while the Machine Agent restates the run's status every
    few seconds (status assertions). Without this, a long Bash call read as
    "Console activity is stale" once the turn was fifteen minutes old. A
    dispatched turn counts only its own run; a queued turn (no run yet) counts
    the run its thread is executing. The run is reporting under the rule that
    keeps it the thread's execution owner (`_open_run_holds_live_ownership`).
    """
    horizon = observed_at - _CONSOLE_TURN_FRESHNESS
    stale = [
        turn
        for turn in turns
        if getattr(turn, "terminal_at", None) is None
        and str(getattr(turn, "state", "")) in ("queued", "starting", "active", "draining")
        and not ((updated_at := _as_aware_utc(getattr(turn, "updated_at", None))) is not None and updated_at > horizon)
    ]
    if not stale:
        return frozenset()
    own_run_ids = {str(turn.run_id) for turn in stale if turn.run_id is not None}
    waiting_thread_ids = {str(turn.thread_id) for turn in stale if turn.run_id is None}
    candidates = []
    if own_run_ids:
        candidates.extend(
            orm.query(LiveSessionRun).filter(LiveSessionRun.id.in_(sorted(own_run_ids)), LiveSessionRun.ended_at.is_(None)).all()
        )
    if waiting_thread_ids:
        candidates.extend(
            orm.query(LiveSessionRun)
            .join(LiveConsoleTurn, LiveConsoleTurn.run_id == LiveSessionRun.id)
            .filter(
                LiveConsoleTurn.thread_id.in_(sorted(waiting_thread_ids)),
                LiveConsoleTurn.state.in_(("starting", "active", "draining")),
                LiveConsoleTurn.terminal_at.is_(None),
                LiveSessionRun.ended_at.is_(None),
            )
            .all()
        )
    reporting = {
        str(run.id): str(run.thread_id) for run in candidates if _open_run_holds_live_ownership(orm, run=run, observed_at=observed_at)
    }
    reporting_threads = set(reporting.values())
    return frozenset(
        str(turn.id)
        for turn in stale
        if (str(turn.run_id) in reporting if turn.run_id is not None else str(turn.thread_id) in reporting_threads)
    )


def _input_receipt_dto(
    receipt: Any,
    *,
    turn: Any | None = None,
    attachments: list[dict[str, Any]] | None = None,
    origin: str | None = None,
    reporting_turn_ids: Collection[str] = (),
) -> dict[str, Any]:
    turn_identity = None
    origin = str(origin or getattr(turn, "origin", None) or getattr(receipt, "origin", None) or "user")
    if turn is not None:
        turn_identity = {
            "turn_id": str(turn.id),
            "run_id": str(turn.run_id) if turn.run_id is not None else None,
            "state": str(turn.state),
            "origin": origin,
            "is_fresh": _console_turn_state_is_fresh(turn, reporting_turn_ids=reporting_turn_ids),
        }
    return {
        "id": receipt.id,
        "owner_id": receipt.owner_id,
        "session_id": receipt.session_id,
        "provider": receipt.provider,
        "text": receipt.text,
        "intent": receipt.intent,
        "status": receipt.status,
        "client_request_id": receipt.client_request_id,
        "origin": origin,
        "payload_digest": getattr(receipt, "payload_digest", None),
        "archive_session_input_id": receipt.archive_session_input_id,
        "durable_event_id": getattr(receipt, "durable_event_id", None),
        "delivery_request_id": receipt.delivery_request_id,
        "error_json": receipt.error_json,
        "turn": turn_identity,
        "attachments": attachments or [],
        "created_at": _encode_datetime(receipt.created_at),
        "updated_at": _encode_datetime(receipt.updated_at),
    }


def _input_attachment_dto(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "input_receipt_id": str(row.input_receipt_id),
        "owner_id": int(row.owner_id),
        "session_id": str(row.session_id),
        "mime_type": str(row.mime_type),
        "byte_size": int(row.byte_size),
        "sha256": str(row.sha256),
        "blob_path": str(row.blob_path),
        "original_filename": row.original_filename,
        "original_byte_size": int(row.original_byte_size) if row.original_byte_size is not None else None,
        "created_at": _encode_datetime(row.created_at),
        "expires_at": _encode_datetime(row.expires_at),
    }


def _input_attachment_summary_dto(row: LiveSessionInputAttachment) -> dict[str, Any]:
    """Return only bounded display metadata, never blob paths or digests."""
    return {
        "filename": str(row.original_filename or "image"),
        "mime_type": str(row.mime_type),
        "byte_size": int(row.byte_size),
    }


def _input_attachment_summaries_by_receipt(
    orm: Session,
    *,
    session_id: str,
    receipts: list[Any],
) -> dict[str, list[dict[str, Any]]]:
    owner_by_receipt_id = {str(receipt.id): int(receipt.owner_id) for receipt in receipts}
    if not owner_by_receipt_id:
        return {}

    rows = (
        orm.query(LiveSessionInputAttachment)
        .filter(
            LiveSessionInputAttachment.session_id == session_id,
            LiveSessionInputAttachment.input_receipt_id.in_(tuple(owner_by_receipt_id)),
            LiveSessionInputAttachment.owner_id.in_(tuple(sorted(set(owner_by_receipt_id.values())))),
        )
        .order_by(
            LiveSessionInputAttachment.created_at.asc(),
            LiveSessionInputAttachment.id.asc(),
        )
        .all()
    )
    summaries: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        receipt_id = str(row.input_receipt_id)
        if owner_by_receipt_id.get(receipt_id) != int(row.owner_id):
            continue
        summaries.setdefault(receipt_id, []).append(_input_attachment_summary_dto(row))
    return summaries


def _directed_input_dto(row: Any, receipt: Any | None = None) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "source_session_id": str(row.source_session_id),
        "target_session_id": str(row.target_session_id),
        "text": str(row.body),
        "reply_to_id": int(row.reply_to_id) if row.reply_to_id is not None else None,
        "client_request_id": str(row.client_request_id),
        "created_at": _encode_datetime(row.created_at),
        "input_receipt": _input_receipt_dto(receipt) if receipt is not None else None,
    }


SEMANTIC_PROJECTOR_ID = "semantic-v2"
KNOWN_PROJECTORS = (SEMANTIC_PROJECTOR_ID, "search-v2", EMBEDDING_PROJECTOR_ID)


def storage_projectors_for_provider(provider: str | None) -> tuple[str, ...]:
    """Return only the derived ledgers that can do work for one provider.

    Semantic-v2 exists solely to replay Claude's sequence-dependent native
    controls.  Registering it for every provider turned a few dozen real
    Claude repairs into a 27k-row no-op backlog after restart, delaying the
    human title debt the projector was introduced to repair.
    """

    downstream = ("search-v2", EMBEDDING_PROJECTOR_ID)
    return (SEMANTIC_PROJECTOR_ID, *downstream) if str(provider or "").strip().lower() == "claude" else downstream


# Projector rows are only ever created for these names. Anything else in
# projector_state is a retired generation: EMBEDDING_PROJECTOR_ID carries the
# embedding revision and a partition suffix, so bumping either renames the
# projector and strands every row under the old name where no worker will ever
# poll them again. Those rows are derived and disposable; reap them.
ACTIVE_PROJECTORS = ("render-v2", SEMANTIC_PROJECTOR_ID, "search-v2", EMBEDDING_PROJECTOR_ID)

# Backoff applied to a "permanent" projection failure, replacing the old
# terminal quarantine. Long enough that a truly unprojectable row is cheap,
# short enough that a fix deployed today heals it today.
PERMANENT_FAILURE_RETRY_INTERVAL = timedelta(hours=1)


def _provider_fact_dto(row: Any) -> dict[str, Any]:
    return {
        "kind": row["kind"],
        "at": (_encode_datetime(row["at"]) if isinstance(row["at"], datetime) else str(row["at"])),
        "source_epoch": row["source_epoch"],
        "source_position": int(row["source_position"]),
        "payload": row["payload_json"],
        "commit_seq": str(row["commit_seq"]),
    }


_DELEGATION_FACT_KINDS = ("delegation.metadata", "delegation.spawn", "delegation.activity")
_SESSION_READ_DELEGATION_FACT_LIMIT = 256
# One row each: the newest recap, provider title and usage are what the
# session chrome shows, and a long session's turn facts must never evict them.
_SESSION_READ_LATEST_FACT_KINDS = ("session.recap", "session.title", "turn.usage")
# Every turn duration, newest first, so any loaded page can anchor its footers.
_SESSION_READ_TURN_FACT_LIMIT = 2_000
# The busiest real session carries a few hundred screenshots; past that the
# session read stops being a bounded payload. The workspace joins what is here
# and simply has no ref for anything beyond it.
_SESSION_READ_MEDIA_REF_LIMIT = 500


def _provider_fact_rows(connection: Connection, *, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """Newest provider facts for a session, as the list RPC serves them."""
    table = SessionProviderFact.__table__
    rows = (
        connection.execute(
            select(table)
            .where(table.c.session_id == session_id)
            .order_by(table.c.at.desc(), table.c.source_position.desc(), table.c.id.desc())
            .limit(limit)
        )
        .mappings()
        .all()
    )
    return [_provider_fact_dto(row) for row in rows]


def _delegation_fact_rows(
    connection: Connection, *, session_id: str, limit: int = _SESSION_READ_DELEGATION_FACT_LIMIT
) -> list[dict[str, Any]]:
    """Bounded, source-ordered raw delegation observations for one session."""
    table = SessionProviderFact.__table__
    rows = (
        connection.execute(
            select(table)
            .where(table.c.session_id == session_id, table.c.kind.in_(_DELEGATION_FACT_KINDS))
            .order_by(table.c.source_position.asc(), table.c.kind.asc(), table.c.id.asc())
            .limit(limit)
        )
        .mappings()
        .all()
    )
    return [_provider_fact_dto(row) for row in rows]


def _session_read_provider_facts(connection: Connection, *, session_id: str) -> list[dict[str, Any]]:
    """The facts the workspace needs on every page, from the coalesced session read."""
    table = SessionProviderFact.__table__
    newest_first = (table.c.at.desc(), table.c.source_position.desc(), table.c.id.desc())
    rows: list[Any] = []
    for kind in _SESSION_READ_LATEST_FACT_KINDS:
        rows.extend(
            connection.execute(select(table).where(table.c.session_id == session_id, table.c.kind == kind).order_by(*newest_first).limit(1))
            .mappings()
            .all()
        )
    rows.extend(
        connection.execute(
            select(table)
            .where(table.c.session_id == session_id, table.c.kind == "turn.duration")
            .order_by(*newest_first)
            .limit(_SESSION_READ_TURN_FACT_LIMIT)
        )
        .mappings()
        .all()
    )
    rows.extend(
        connection.execute(
            select(table)
            .where(table.c.session_id == session_id, table.c.kind.in_(_DELEGATION_FACT_KINDS))
            .order_by(table.c.source_position.asc(), table.c.kind.asc(), table.c.id.asc())
            .limit(_SESSION_READ_DELEGATION_FACT_LIMIT)
        )
        .mappings()
        .all()
    )
    return [_provider_fact_dto(row) for row in rows]


def _input_receipt_rows(connection: Connection, *, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Newest input receipts for a session regardless of status, for provenance."""
    table = LiveSessionInputReceipt.__table__
    turn_table = LiveConsoleTurn.__table__
    rows = (
        connection.execute(
            select(table, turn_table.c.origin.label("turn_origin"), turn_table.c.state.label("turn_state"))
            .select_from(table.outerjoin(turn_table, turn_table.c.receipt_id == table.c.id))
            .where(table.c.session_id == session_id)
            .order_by(table.c.created_at.desc(), table.c.id.desc())
            .limit(limit)
        )
        .mappings()
        .all()
    )
    # The turn's state tells a client whether a delivered send that never became
    # a transcript row was lost (its run failed before the provider read it).
    return [{**_input_receipt_dto(_RowReceipt(row), origin=row["turn_origin"]), "turn_state": row["turn_state"]} for row in rows]


def _session_read_media_refs(connection: Connection, *, session_id: str) -> list[dict[str, Any]]:
    """Active media references for one session, from the coalesced session read.

    A reference carries the provider source position of the line that mentioned
    the media, and the engine stamps a render record with that same position, so
    the workspace can place each image on the event that owns it. Bytes are never
    in this payload; the client fetches the media blob route.
    """
    refs = SessionMediaRef.__table__
    media = MediaObject.__table__
    # The preview rides the manifest as a hash, and only a preview the store
    # still holds may be offered: an object can be retired independently of the
    # image that points at it. Joining it here keeps that decision with the data
    # rather than trusting whoever wrote the link.
    thumb = media.alias("thumb")
    rows = (
        connection.execute(
            select(
                refs.c.media_hash,
                refs.c.envelope_id,
                refs.c.ref_key,
                media.c.state.label("media_state"),
                media.c.mime_type,
                media.c.byte_size,
                media.c.thumb_hash,
                media.c.width,
                media.c.height,
                thumb.c.state.label("thumb_state"),
                thumb.c.derived_from.label("thumb_derived_from"),
            )
            .select_from(
                refs.outerjoin(media, media.c.media_hash == refs.c.media_hash).outerjoin(thumb, thumb.c.media_hash == media.c.thumb_hash)
            )
            .where(refs.c.session_id == session_id, refs.c.state == "active")
            .order_by(refs.c.id.asc())
            .limit(_SESSION_READ_MEDIA_REF_LIMIT)
        )
        .mappings()
        .all()
    )
    return [_session_media_ref_dto(row) for row in rows]


def _session_media_ref_dto(row) -> dict[str, Any]:
    return {
        "media_hash": str(row["media_hash"]),
        "envelope_id": row["envelope_id"],
        "ref_key": str(row["ref_key"]),
        "media_state": row["media_state"],
        "mime_type": row["mime_type"],
        "byte_size": int(row["byte_size"]) if row["byte_size"] is not None else None,
        "thumb_hash": (row["thumb_hash"] if row["thumb_state"] == "present" and row["thumb_derived_from"] == row["media_hash"] else None),
        "width": row["width"],
        "height": row["height"],
    }


def _insert_provider_facts(
    connection: Connection,
    *,
    session_id: str,
    source_epoch: str,
    provider_facts: tuple[dict[str, Any], ...],
    commit_seq: int,
    now: datetime,
) -> int:
    """One immutable row per (session, epoch, position, kind); repeats are no-ops."""
    if not provider_facts:
        return 0
    table = SessionProviderFact.__table__
    statement = sqlite_insert(table).on_conflict_do_nothing(index_elements=["session_id", "source_epoch", "source_position", "kind"])
    rows = [
        {
            "session_id": session_id,
            "kind": str(fact["kind"]),
            "at": fact["at"],
            "source_epoch": source_epoch,
            "source_position": int(fact["source_position"]),
            "payload_json": json.dumps(fact["payload"], sort_keys=True, separators=(",", ":")),
            "commit_seq": commit_seq,
            "created_at": now,
        }
        for fact in provider_facts
    ]
    result = connection.execute(statement, rows)
    return int(result.rowcount or 0)


def _delegation_payload(fact: Mapping[str, Any]) -> dict[str, Any] | None:
    payload = fact.get("payload")
    if isinstance(payload, Mapping):
        return dict(payload)
    try:
        decoded = json.loads(str(payload))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _delegation_metadata_facts(provider_facts: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    return tuple(
        payload
        for fact in provider_facts
        if fact.get("kind") == "delegation.metadata"
        and isinstance((payload := _delegation_payload(fact)), dict)
        and isinstance(payload.get("provider_session_id"), str)
    )


def _delegation_native_ids(*, provider_facts: tuple[dict[str, Any], ...], session_facts: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for value in (
        session_facts.get("provider_session_id"),
        *[payload.get("provider_session_id") for payload in _delegation_metadata_facts(provider_facts)],
        # OpenCode records the parent's native session id only in each child
        # metadata entry of the parent's delegation.spawn fact. Preserve that
        # source evidence as a provider alias so a child envelope that arrived
        # first can be adopted when this parent is committed later.
        *[
            child.get("metadata", {}).get("parentSessionId")
            for child in _spawn_child_payloads(provider_facts)
            if isinstance(child.get("metadata"), Mapping)
        ],
    ):
        native_id = str(value or "").strip()
        if native_id and native_id not in ids:
            ids.append(native_id)
    return ids


def _delegation_parent_id(*, provider_facts: tuple[dict[str, Any], ...], session_facts: Mapping[str, Any]) -> str | None:
    explicit = str(session_facts.get("parent_provider_session_id") or "").strip() or None
    candidates = {
        str(payload["parent_provider_session_id"]).strip()
        for payload in _delegation_metadata_facts(provider_facts)
        if payload.get("parent_provider_session_id")
    }
    if explicit is not None:
        return explicit if not candidates or candidates == {explicit} else None
    return next(iter(candidates)) if len(candidates) == 1 else None


def _provider_alias_values_for_session(connection: Connection, *, session_id: str, provider: str) -> list[str]:
    alias = LiveSessionThreadAlias.__table__
    thread = LiveSessionThread.__table__
    values = [
        str(row[0])
        for row in connection.execute(
            select(alias.c.alias_value)
            .select_from(alias.join(thread, thread.c.id == alias.c.thread_id))
            .where(
                thread.c.session_id == session_id,
                alias.c.provider == provider,
                alias.c.alias_kind == "provider_session_id",
            )
            .order_by(alias.c.id.asc())
        ).all()
    ]
    native_id = connection.execute(
        select(StorageSession.provider_session_id).where(StorageSession.session_id == session_id, StorageSession.provider == provider)
    ).scalar_one_or_none()
    if native_id and native_id not in values:
        values.append(str(native_id))
    return values


def _spawn_child_payloads(provider_facts: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    for fact in provider_facts:
        if fact.get("kind") != "delegation.spawn":
            continue
        payload = _delegation_payload(fact)
        if not isinstance(payload, dict) or not isinstance(payload.get("children"), list):
            continue
        for child in payload["children"]:
            if not isinstance(child, Mapping) or not isinstance(child.get("provider_session_id"), str):
                continue
            children.append(
                {
                    "provider_session_id": str(child["provider_session_id"]),
                    "parent_tool_call_id": child.get("parent_tool_call_id"),
                    "metadata": child.get("metadata") if isinstance(child.get("metadata"), dict) else {},
                }
            )
            if len(children) >= _SESSION_READ_DELEGATION_FACT_LIMIT:
                return children
    return children


def _bind_spawn_child(
    connection: Connection,
    *,
    parent_session_id: str,
    parent_provider: str,
    parent_owner_id: str | None,
    parent_machine_id: str,
    parent_native_ids: list[str],
    child: Mapping[str, Any],
    commit_seq: int,
    commit_time: datetime,
) -> int:
    child_native_id = str(child.get("provider_session_id") or "").strip()
    if parent_owner_id is None or child_native_id in parent_native_ids:
        return 0
    storage = StorageSession.__table__
    child_session_ids: set[str] = set()
    if child_native_id:
        resolved_child = _resolve_session_id_by_provider_session_id(
            connection,
            provider=parent_provider,
            provider_session_id=child_native_id,
            owner_id=parent_owner_id,
            machine_id=parent_machine_id,
        )
        if resolved_child is not None:
            child_session_ids.add(resolved_child)
    # Storage-v2 child rows may have no live thread/native alias. Their raw
    # envelope still preserves the exact parent native pointer, so use that
    # indexed lineage column as the late-arrival key in this scope.
    if not child_session_ids and parent_native_ids:
        pointer_rows = connection.execute(
            select(storage.c.session_id).where(
                storage.c.provider == parent_provider,
                storage.c.owner_id == str(parent_owner_id),
                storage.c.machine_id == parent_machine_id,
                storage.c.is_subagent == 1,
                storage.c.subagent_parent_provider_session_id.in_(parent_native_ids),
            )
        ).all()
        # A native-less row must already identify itself as a worker. A plain
        # fork's parent pointer is ancestry, not evidence it is the spawned
        # worker. Multiple workers sharing the pointer remain unresolved.
        if len(pointer_rows) == 1:
            child_session_ids.add(str(pointer_rows[0][0]))
    child_session_ids.discard(parent_session_id)
    if not child_session_ids:
        return 0
    parent_native_id = parent_native_ids[0] if parent_native_ids else None
    parent_tool_call_id = str(child.get("parent_tool_call_id") or "").strip() or None
    bound = 0
    for child_session_id in child_session_ids:
        child_row = connection.execute(select(storage).where(storage.c.session_id == child_session_id)).mappings().first()
        if child_row is None:
            continue
        existing_parent_native = str(child_row["subagent_parent_provider_session_id"] or "").strip() or None
        existing_parent_session = str(child_row["subagent_parent_session_id"] or "").strip() or None
        if existing_parent_native not in (None, parent_native_id) or existing_parent_session not in (None, parent_session_id):
            continue
        hidden = int(evaluate_origin_visibility(SessionVisibilityFacts(is_subagent=True)).system_hidden)
        values: dict[str, Any] = {
            "subagent_parent_session_id": parent_session_id,
            "is_subagent": 1,
            "hidden_from_default_timeline": hidden,
            "updated_at": commit_time,
            "commit_seq": commit_seq,
        }
        if parent_native_id is not None:
            values["subagent_parent_provider_session_id"] = parent_native_id
        if parent_tool_call_id is not None and child_row["subagent_parent_tool_call_id"] is None:
            values["subagent_parent_tool_call_id"] = parent_tool_call_id
        bound += int(connection.execute(update(storage).where(storage.c.session_id == child_session_id).values(**values)).rowcount or 0)
        for policy_table in (LiveSessionCatalog.__table__, LiveTimelineCard.__table__):
            connection.execute(
                update(policy_table)
                .where(policy_table.c.session_id == child_session_id)
                .values(hidden_from_default_timeline=hidden, updated_at=commit_time)
            )
        thread = LiveSessionThread.__table__
        connection.execute(
            update(thread)
            .where(thread.c.session_id == child_session_id, thread.c.is_primary == 1)
            .values(hidden_from_default_timeline=hidden, updated_at=commit_time)
        )
    return bound


def _apply_delegation_lineage(
    connection: Connection,
    *,
    session_id: str,
    provider: str,
    owner_id: str | None,
    machine_id: str,
    native_ids: list[str],
    provider_facts: tuple[dict[str, Any], ...],
    session_facts: Mapping[str, Any] | None = None,
    commit_seq: int,
    commit_time: datetime,
) -> int:
    """Bind only owner/machine/provider-matching source observations.

    Spawn rows stay immutable provider facts. Resolved children receive native
    parent/tool lineage; an exact spawn also authorizes worker-only visibility.
    This never creates a child or infers a live pending claim from ancestry.
    """

    if owner_id is None:
        return 0
    bound = 0
    current_facts = session_facts or {}
    parent_native_id = _delegation_parent_id(provider_facts=provider_facts, session_facts=current_facts)
    parent_source_id = _normalized_parent_source_id(parent_native_id or "")
    if parent_native_id and parent_native_id not in native_ids:
        # Resolve native ids and OMP absolute parentSession paths through the
        # same scoped source identity used by commit_raw_object. Never replace
        # the raw pointer with an alias: later envelopes and audit evidence
        # must retain exactly what the provider supplied.
        parent_session_id = _resolve_session_id_by_provider_session_id(
            connection,
            provider=provider,
            provider_session_id=parent_native_id,
            owner_id=owner_id,
            machine_id=machine_id,
        )
        if parent_session_id is None:
            parent_session_id = _resolve_session_id_by_source_path(
                connection,
                provider=provider,
                source_path=parent_native_id,
                owner_id=owner_id,
                machine_id=machine_id,
            )
        current = (
            connection.execute(select(StorageSession.__table__).where(StorageSession.__table__.c.session_id == session_id))
            .mappings()
            .first()
        )
        current_parent_native = str((current or {}).get("subagent_parent_provider_session_id") or "").strip() or None
        current_parent_source = str((current or {}).get("subagent_parent_source_id") or "").strip() or None
        current_parent_session = str((current or {}).get("subagent_parent_session_id") or "").strip() or None
        if (
            parent_session_id is not None
            and parent_session_id != session_id
            and current_parent_native in (None, parent_native_id)
            and current_parent_source in (None, parent_source_id)
            and current_parent_session in (None, parent_session_id)
        ):
            values: dict[str, Any] = {
                "subagent_parent_provider_session_id": parent_native_id,
                "subagent_parent_session_id": parent_session_id,
                "updated_at": commit_time,
                "commit_seq": commit_seq,
            }
            if parent_source_id is not None:
                values["subagent_parent_source_id"] = parent_source_id
            bound += int(
                connection.execute(
                    update(StorageSession.__table__).where(StorageSession.__table__.c.session_id == session_id).values(**values)
                ).rowcount
                or 0
            )
    for child in _spawn_child_payloads(provider_facts):
        bound += _bind_spawn_child(
            connection,
            parent_session_id=session_id,
            parent_provider=provider,
            parent_owner_id=owner_id,
            parent_machine_id=machine_id,
            parent_native_ids=native_ids,
            child=child,
            commit_seq=commit_seq,
            commit_time=commit_time,
        )

    # A parent spawn can arrive before the child has an envelope. On the
    # child's later commit, inspect only a bounded owner-scoped slice of spawn
    # facts; no global source-head scan or graph is needed. A raw child may
    # have no native alias, but its exact parent pointer is still indexed.
    current_parent_pointer = connection.execute(
        select(StorageSession.__table__.c.subagent_parent_provider_session_id).where(
            StorageSession.__table__.c.session_id == session_id,
        )
    ).scalar_one_or_none()
    if not native_ids and not str(current_parent_pointer or "").strip():
        return bound
    rows = (
        connection.execute(
            _NEWEST_SCOPED_SPAWN_FACTS,
            {
                "session_id": session_id,
                "owner_id": str(owner_id),
                "machine_id": machine_id,
                "provider": provider,
                "limit": _SESSION_READ_DELEGATION_FACT_LIMIT,
            },
        )
        .mappings()
        .all()
    )
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("children"), list):
            continue
        for child in payload["children"]:
            child_native_id = str(child.get("provider_session_id") or "").strip() if isinstance(child, Mapping) else ""
            if isinstance(child, Mapping) and (not native_ids or not child_native_id or child_native_id in native_ids):
                child_metadata = child.get("metadata") if isinstance(child.get("metadata"), Mapping) else {}
                parent_pointer = str(child_metadata.get("parentSessionId") or "").strip()
                if parent_pointer and _resolve_session_id_by_provider_session_id(
                    connection,
                    provider=provider,
                    provider_session_id=parent_pointer,
                    owner_id=owner_id,
                    machine_id=machine_id,
                ) != str(row["session_id"]):
                    # Multiple scoped spawn facts for one native parent pointer
                    # are ambiguous; leave the child unresolved rather than
                    # selecting whichever fact happens to be newest.
                    continue
                parent_native_ids = _provider_alias_values_for_session(
                    connection,
                    session_id=str(row["session_id"]),
                    provider=provider,
                )
                parent_native_ids.extend(
                    value
                    for value in _delegation_native_ids(
                        provider_facts=(
                            {
                                "kind": "delegation.spawn",
                                "payload": payload,
                            },
                        ),
                        session_facts={},
                    )
                    if value not in parent_native_ids
                )
                bound += _bind_spawn_child(
                    connection,
                    parent_session_id=str(row["session_id"]),
                    parent_provider=provider,
                    parent_owner_id=owner_id,
                    parent_machine_id=machine_id,
                    parent_native_ids=parent_native_ids,
                    child=child,
                    commit_seq=commit_seq,
                    commit_time=commit_time,
                )
    return bound


def _open_run_holds_live_ownership(orm: Session, *, run: LiveSessionRun, observed_at: datetime) -> bool:
    """Whether an unended run still owns its provider thread right now.

    `ended_at IS NULL` alone is not ownership. A wrapper killed before it could
    ship its terminal fact (closed terminal, SIGKILL, machine sleep) leaves a
    run row that nothing ever reconciles, and the run is then indistinguishable
    from a live one to any reader that only looks at the column. The product's
    own horizon is the control lease (`_CONTROL_LEASE_TTL`, the same one
    `get_live_control_grant` fails closed on): a run whose attachment the machine
    has not stamped inside that window -- and which has published no runtime
    signal, asserted or otherwise, inside it -- is an orphan, not a second
    execution owner. Registration in flight is the remaining case that must still
    win: a pending, unexpired launch attempt is a resume that has not reported
    yet.

    The attachment's *state* does not decide this. A `detached` connection with a
    current stamp is a wrapper still reporting on this thread (the channel is
    only whether Longhouse holds the leash), while `attached` with a stale stamp
    is a process that stopped talking. Freshness is the signal; state is not.
    """

    lease_floor = observed_at - _CONTROL_LEASE_TTL
    live_control = (
        orm.query(LiveSessionConnection.id)
        .filter(
            LiveSessionConnection.run_id == str(run.id),
            LiveSessionConnection.released_at.is_(None),
            LiveSessionConnection.state.in_(("attached", "detached", "degraded")),
            LiveSessionConnection.last_health_at.is_not(None),
            LiveSessionConnection.last_health_at > lease_floor,
        )
        .first()
    )
    if live_control is not None:
        return True
    fresh_signal = (
        orm.query(LiveRuntimeState.runtime_key)
        .filter(
            LiveRuntimeState.run_id == str(run.id),
            LiveRuntimeState.terminal_state.is_(None),
            or_(
                LiveRuntimeState.freshness_expires_at > observed_at,
                LiveRuntimeState.last_runtime_signal_at > lease_floor,
                LiveRuntimeState.last_asserted_at > lease_floor,
                LiveRuntimeState.updated_at > lease_floor,
            ),
        )
        .first()
    )
    if fresh_signal is not None:
        return True
    pending_attempt = (
        orm.query(LiveSessionLaunchAttempt.id)
        .filter(
            LiveSessionLaunchAttempt.run_id == str(run.id),
            LiveSessionLaunchAttempt.state == "pending",
            or_(
                LiveSessionLaunchAttempt.expires_at.is_(None),
                LiveSessionLaunchAttempt.expires_at > observed_at,
            ),
        )
        .first()
    )
    return pending_attempt is not None


def _retire_orphaned_open_run(orm: Session, *, run: LiveSessionRun, observed_at: datetime) -> None:
    """End an unended run whose execution owner stopped reporting.

    Mirrors the terminal-evidence path (`_apply_exact_run_terminal_evidence`):
    the run ends and its control attachments close, so nothing is served as
    open and no stale `can_*` capability outlives the owner.
    """

    run.ended_at = observed_at
    run.exit_status = "stale_run_superseded"
    for connection in (
        orm.query(LiveSessionConnection)
        .filter(
            LiveSessionConnection.run_id == str(run.id),
            LiveSessionConnection.released_at.is_(None),
        )
        .all()
    ):
        connection.state = "ended"
        connection.released_at = observed_at
        connection.last_health_at = observed_at
        connection.can_send_input = 0
        connection.can_interrupt = 0
        connection.can_terminate = 0
        connection.can_tail_output = 0
        connection.can_resume = 0


def _storage_catalog_compat_row(row) -> dict[str, Any]:
    ai_title = str(row["anchor_title"] or "").strip()
    return {
        "session_id": str(row["session_id"]),
        "provider": str(row["provider"]),
        "environment": str(row["environment"]),
        "project": row["project"],
        "device_id": str(row["machine_id"]),
        "device_name": str(row["machine_id"]),
        "cwd": row["cwd"],
        "git_repo": row["git_repo"],
        "git_branch": row["git_branch"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        # Storage ``ended_at`` is the transcript/source high-water timestamp.
        # Session closure is explicit, monotonic catalog state and must never
        # be inferred from a quiet or fully shipped transcript.
        "closed_at": None,
        "close_reason": None,
        "last_activity_at": row["last_activity_at"],
        "user_messages": int(row["user_messages"]),
        "assistant_messages": int(row["assistant_messages"]),
        "tool_calls": int(row["tool_calls"]),
        "summary": None,
        "summary_title": row["summary_title"],
        "anchor_title": ai_title or None,
        "anchor_title_source": (row["anchor_title_source"] if ai_title else None),
        "title_retry_at": row["title_retry_at"],
        "title_last_error": row["title_last_error"],
        "first_user_message_preview": row["first_user_message_preview"],
        "transcript_revision": int(row["transcript_revision"]),
        "summary_revision": 0,
        "user_state": str(row["user_state"]),
        "user_state_at": row["updated_at"],
        "last_console_result_at": row["last_console_result_at"],
        "last_console_result_outcome": row["last_console_result_outcome"],
        "last_read_at": row["last_read_at"],
        "last_user_input_at": row["last_user_input_at"],
        "primary_thread_id": None,
        "notification_muted": bool(row["notification_muted"]),
        "origin_kind": row["origin_kind"],
        "hidden_from_default_timeline": int(row["hidden_from_default_timeline"]),
        "user_hidden_from_timeline": int(row["user_hidden_from_timeline"] or 0),
        "user_hidden_at": row["user_hidden_at"],
        "launch_actor": row["launch_actor"],
        "launch_surface": row["launch_surface"],
        "permission_mode": "bypass",
        # Storage rows carry no posture of their own, so this is a placeholder
        # rather than an observation. Leaving the source unset is what keeps a
        # caller from reading it as one.
        "permission_mode_source": None,
    }


def _machine_is_automation(connection: Connection, *, owner_id: object, machine_id: str) -> bool:
    """Whether the owner marked this machine's credentials automation.

    Read inside the commit's own transaction, not from the request's auth
    snapshot, so an envelope authenticated just before the machine was marked
    cannot slip past both the backfill and ingest
    (docs/specs/automation-machine-credentials.md).
    """

    if owner_id is None:
        return False
    try:
        owner = int(str(owner_id))
    except ValueError:
        return False
    token = LiveDeviceToken.__table__
    return (
        connection.execute(
            select(token.c.id)
            .where(
                token.c.owner_id == owner,
                token.c.device_id == machine_id,
                token.c.revoked_at.is_(None),
                token.c.automation.is_(True),
            )
            .limit(1)
        ).first()
        is not None
    )


_STORAGE_CATALOG_OVERLAY_FIELDS = frozenset(
    {
        "ended_at",
        "last_activity_at",
        "user_messages",
        "assistant_messages",
        "tool_calls",
        "summary_title",
        "anchor_title",
        "anchor_title_source",
        "title_retry_at",
        "title_last_error",
        "first_user_message_preview",
        "transcript_revision",
    }
)
_STORAGE_CATALOG_FALLBACK_FIELDS = frozenset(
    {
        "project",
        "cwd",
        "git_repo",
        "git_branch",
        "device_name",
        "origin_kind",
        "launch_actor",
        "launch_surface",
    }
)


def _merge_storage_catalog_row(storage_row, live_catalog_row) -> dict[str, Any]:
    """Combine archive facts with live session identity and disposition.

    Storage owns transcript/archive progress. The live catalog owns explicit
    closure, control identity, launch provenance, and user preferences. A
    storage-only historical session remains open at the session level even if
    its transcript has an ``ended_at`` high-water mark.
    """

    storage_catalog = _storage_catalog_compat_row(storage_row)
    if live_catalog_row is None:
        return storage_catalog

    merged = dict(live_catalog_row)
    for field in _STORAGE_CATALOG_OVERLAY_FIELDS:
        merged[field] = storage_catalog[field]
    for field in _STORAGE_CATALOG_FALLBACK_FIELDS:
        if merged.get(field) is None:
            merged[field] = storage_catalog[field]
    return merged


def _legacy_migration_run_dto(row) -> dict[str, Any]:
    return {
        "run_id": str(row["run_id"]),
        "legacy_high_watermark": str(row["legacy_high_watermark"]),
        "expected_session_count": int(row["expected_session_count"]),
        "state": str(row["state"]),
        "commit_seq": str(row["commit_seq"]),
        "created_at": _encode_datetime(row["created_at"]),
        "updated_at": _encode_datetime(row["updated_at"]),
        "completed_at": _encode_datetime(row["completed_at"]),
    }


def _legacy_migration_summary(connection, run_id: str) -> dict[str, Any]:
    rows = LegacyMigrationSession.__table__
    state_counts = {
        str(state): int(count)
        for state, count in connection.execute(
            select(rows.c.state, func.count()).where(rows.c.run_id == run_id).group_by(rows.c.state)
        ).all()
    }
    totals = connection.execute(
        select(
            func.count(),
            func.coalesce(func.sum(rows.c.source_expected), 0),
            func.coalesce(func.sum(rows.c.source_covered), 0),
            func.coalesce(func.sum(rows.c.source_missing), 0),
            func.coalesce(func.sum(rows.c.media_expected), 0),
            func.coalesce(func.sum(rows.c.media_covered), 0),
            func.coalesce(func.sum(rows.c.media_missing), 0),
        ).where(rows.c.run_id == run_id)
    ).one()
    return {
        "registered_session_count": int(totals[0]),
        "state_counts": {state: state_counts.get(state, 0) for state in ("pending", "migrating", "verified", "degraded")},
        "source_expected": int(totals[1]),
        "source_covered": int(totals[2]),
        "source_missing": int(totals[3]),
        "media_expected": int(totals[4]),
        "media_covered": int(totals[5]),
        "media_missing": int(totals[6]),
    }


def _storage_card_compat_row(row) -> dict[str, Any]:
    title = sanitize_timeline_title(row["anchor_title"], max_words=6) or sanitize_timeline_title(
        row["first_user_message_preview"], max_words=6
    )
    return {
        "session_id": str(row["session_id"]),
        "last_activity_at": row["last_activity_at"],
        "summary_title": title,
        "first_user_message_preview": row["first_user_message_preview"],
        "user_messages": int(row["user_messages"]),
        "assistant_messages": int(row["assistant_messages"]),
        "tool_calls": int(row["tool_calls"]),
        "transcript_revision": int(row["transcript_revision"]),
        "archive_state": "current" if row["render_state"] == "ready" else "pending",
    }


def _assemble_session_facts(
    connection,
    *,
    session_ids: list[str],
    observed_at: datetime,
    compact: bool,
) -> list[dict[str, Any]]:
    """Bulk-load response-relevant session facts without presentation inference."""

    if not session_ids:
        return []
    catalog_table = LiveSessionCatalog.__table__
    card_table = LiveTimelineCard.__table__
    runtime_table = LiveRuntimeState.__table__
    interaction_table = LiveInteractionRequest.__table__
    heartbeat_table = LiveHeartbeatStamp.__table__
    readiness_table = LiveLaunchReadiness.__table__
    thread_table = LiveSessionThread.__table__
    run_table = LiveSessionRun.__table__
    connection_table = LiveSessionConnection.__table__
    control_lease_table = LiveControlLease.__table__
    live_preview_table = LiveSessionLivePreview.__table__
    alias_table = LiveSessionThreadAlias.__table__
    console_turn_table = LiveConsoleTurn.__table__
    storage_table = StorageSession.__table__
    live_session_table = LiveSession.__table__
    tombstone_table = LiveSessionTombstone.__table__

    tombstoned_session_ids = {
        str(row["session_id"])
        for row in connection.execute(select(tombstone_table.c.session_id).where(tombstone_table.c.session_id.in_(session_ids))).mappings()
    }
    session_ids = [session_id for session_id in session_ids if session_id not in tombstoned_session_ids]
    if not session_ids:
        return []

    catalogs = {
        str(row["session_id"]): row
        for row in connection.execute(select(catalog_table).where(catalog_table.c.session_id.in_(session_ids))).mappings()
    }
    cards = {
        str(row["session_id"]): row
        for row in connection.execute(select(card_table).where(card_table.c.session_id.in_(session_ids))).mappings()
    }
    storage_rows = {
        str(row["session_id"]): row
        for row in connection.execute(
            select(storage_table).where(
                storage_table.c.session_id.in_(session_ids),
                ~select(tombstone_table.c.session_id).where(tombstone_table.c.session_id == storage_table.c.session_id).exists(),
            )
        ).mappings()
    }
    owner_by_session = {
        str(row["session_id"]): int(row["owner_id"])
        for row in connection.execute(
            select(live_session_table.c.session_id, live_session_table.c.owner_id).where(live_session_table.c.session_id.in_(session_ids))
        ).mappings()
        if row["owner_id"] is not None
    }
    for session_id, row in storage_rows.items():
        if row.get("owner_id") is not None:
            owner_by_session.setdefault(session_id, int(row["owner_id"]))
    missing_console_owner_ids = [
        session_id for session_id, row in catalogs.items() if session_id not in owner_by_session and row.get("origin_kind") == "console"
    ]
    if missing_console_owner_ids:
        outbox_table = LiveArchiveOutbox.__table__
        keys = [f"console_session_create.v1:{session_id}" for session_id in missing_console_owner_ids]
        for row in connection.execute(
            select(
                outbox_table.c.idempotency_key,
                func.json_extract(outbox_table.c.payload_json, "$.session.owner_id").label("owner_id"),
            ).where(outbox_table.c.idempotency_key.in_(keys))
        ).mappings():
            if row["owner_id"] is not None:
                owner_by_session[str(row["idempotency_key"]).rsplit(":", 1)[-1]] = int(row["owner_id"])
    for session_id, row in storage_rows.items():
        catalogs[session_id] = _merge_storage_catalog_row(row, catalogs.get(session_id))
        cards[session_id] = _storage_card_compat_row(row)
    # `source_revision` is the newest raw commit the render is expected to cover,
    # so it is read only from raw objects that carry a live render object. A
    # raw-only envelope (Cursor's transcript projection) declares that it has no
    # render to attach; counting its commit would call the render "behind" a
    # source it never covers, permanently. `last_append_at` still sees every
    # append: that is when bytes arrived, whatever renders them.
    raw_transcript_by_session = {
        str(row["session_id"]): row
        for row in connection.execute(
            select(
                LiveRawObject.session_id,
                func.max(
                    case(
                        (
                            select(RenderObject.object_id)
                            .where(
                                RenderObject.source_envelope_id == LiveRawObject.envelope_id,
                                RenderObject.retired_at.is_(None),
                            )
                            .exists(),
                            LiveRawObject.commit_seq,
                        ),
                        else_=None,
                    )
                ).label("source_revision"),
                func.max(LiveRawObject.sealed_at).label("last_append_at"),
            )
            .where(
                LiveRawObject.session_id.in_(session_ids),
                LiveRawObject.retired_at.is_(None),
            )
            .group_by(LiveRawObject.session_id)
        ).mappings()
    }
    current_render_ids = [
        str(row["current_render_generation"]) for row in storage_rows.values() if row.get("current_render_generation") is not None
    ]
    render_revision_by_generation = {
        str(row["generation_id"]): int(row["commit_seq"])
        for row in connection.execute(
            select(RenderGeneration.generation_id, RenderGeneration.commit_seq).where(
                RenderGeneration.generation_id.in_(current_render_ids)
            )
        ).mappings()
    }
    runtime_by_session: dict[str, Any] = {}
    for row in connection.execute(
        select(runtime_table)
        .where(runtime_table.c.session_id.in_(session_ids))
        .order_by(
            runtime_table.c.updated_at.desc(),
            runtime_table.c.runtime_version.desc(),
            runtime_table.c.runtime_key.desc(),
        )
    ).mappings():
        runtime_by_session.setdefault(str(row["session_id"]), row)
    readiness_by_session = {
        str(row["session_id"]): row
        for row in connection.execute(select(readiness_table).where(readiness_table.c.session_id.in_(session_ids))).mappings()
        if _as_aware_utc(row["expires_at"]) is None or _as_aware_utc(row["expires_at"]) > observed_at
    }
    pending_interaction_by_session: dict[str, Any] = {}
    for row in connection.execute(
        select(interaction_table)
        .where(
            interaction_table.c.session_id.in_(session_ids),
            interaction_table.c.status == "pending",
            or_(interaction_table.c.expires_at.is_(None), interaction_table.c.expires_at > observed_at),
        )
        .order_by(
            interaction_table.c.last_seen_at.desc(),
            interaction_table.c.occurred_at.desc(),
            interaction_table.c.id.desc(),
        )
    ).mappings():
        pending_interaction_by_session.setdefault(str(row["session_id"]), row)

    device_ids = {str(row.get("device_id") or "").strip() for row in catalogs.values() if str(row.get("device_id") or "").strip()}
    heartbeat_by_device: dict[str, Any] = {}
    if device_ids:
        # A machine can accumulate tens of thousands of heartbeat receipts.
        # Fetching all of them and retaining the first row made every timeline
        # snapshot proportional to heartbeat history and triggered an SSE retry
        # storm once the query crossed the catalog client's deadline. Resolve
        # the latest timestamp and deterministic id tie-break inside SQLite so
        # the response remains bounded to one row per requested device.
        # One index seek per device: the newest receipt, id breaking ties, read
        # backwards off (device_id, received_at) with rowid as its last key.
        # GROUP BY device_id with max(received_at) walked every index entry
        # for the requested devices instead, ~180k for one busy machine on the
        # dogfood catalog, 21 ms of every timeline read.
        for device_id in sorted(device_ids):
            row = (
                connection.execute(
                    select(heartbeat_table)
                    .where(heartbeat_table.c.device_id == device_id)
                    .order_by(heartbeat_table.c.received_at.desc(), heartbeat_table.c.id.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is not None:
                heartbeat_by_device[str(row["device_id"])] = row

    thread_rows = list(
        connection.execute(
            select(thread_table)
            .where(thread_table.c.session_id.in_(session_ids))
            .order_by(thread_table.c.created_at.asc(), thread_table.c.id.asc())
        ).mappings()
    )
    threads_by_session: dict[str, list[Any]] = {}
    for row in thread_rows:
        threads_by_session.setdefault(str(row["session_id"]), []).append(row)
    primary_by_session: dict[str, Any] = {}
    for session_id, rows in threads_by_session.items():
        requested = catalogs.get(session_id, {}).get("primary_thread_id")
        primary_by_session[session_id] = next(
            (row for row in rows if requested is not None and str(row["id"]) == str(requested)),
            next((row for row in rows if int(row["is_primary"] or 0) == 1), rows[0]),
        )

    thread_ids = [str(row["id"]) for row in primary_by_session.values()]
    latest_run_by_thread: dict[str, Any] = {}
    if thread_ids:
        for row in connection.execute(
            select(run_table).where(run_table.c.thread_id.in_(thread_ids)).order_by(run_table.c.started_at.desc(), run_table.c.id.desc())
        ).mappings():
            latest_run_by_thread.setdefault(str(row["thread_id"]), row)
    run_ids = [str(row["id"]) for row in latest_run_by_thread.values()]
    connections_by_run: dict[str, list[Any]] = {}
    if run_ids:
        for row in connection.execute(
            select(connection_table)
            .where(connection_table.c.run_id.in_(run_ids))
            .order_by(connection_table.c.acquired_at.asc(), connection_table.c.id.asc())
        ).mappings():
            connections_by_run.setdefault(str(row["run_id"]), []).append(row)

    console_turn_by_session: dict[str, Any] = {}
    turn_priority = {"queued": 1, "starting": 2, "active": 3, "draining": 4}
    for row in connection.execute(
        select(console_turn_table)
        .where(
            console_turn_table.c.session_id.in_(session_ids),
            console_turn_table.c.state.in_(tuple(turn_priority)),
        )
        .order_by(console_turn_table.c.created_at.asc(), console_turn_table.c.id.asc())
    ).mappings():
        session_id = str(row["session_id"])
        current = console_turn_by_session.get(session_id)
        if current is None or turn_priority[str(row["state"])] > turn_priority[str(current["state"])]:
            console_turn_by_session[session_id] = row

    control_leases_by_session: dict[str, list[Any]] = {}
    live_preview_by_session: dict[str, Any] = {}
    if not compact:
        ranked_control_leases = (
            select(
                control_lease_table,
                func.row_number()
                .over(
                    partition_by=control_lease_table.c.session_id,
                    order_by=(control_lease_table.c.heartbeat_at.desc(), control_lease_table.c.id.desc()),
                )
                .label("lease_rank"),
            )
            .where(control_lease_table.c.session_id.in_(session_ids))
            .subquery()
        )
        for row in connection.execute(
            select(ranked_control_leases)
            .where(ranked_control_leases.c.lease_rank <= 8)
            .order_by(
                ranked_control_leases.c.session_id.asc(),
                ranked_control_leases.c.heartbeat_at.desc(),
                ranked_control_leases.c.id.desc(),
            )
        ).mappings():
            control_leases_by_session.setdefault(str(row["session_id"]), []).append(row)
        live_preview_by_session = {
            str(row["session_id"]): row
            for row in connection.execute(
                select(live_preview_table).where(
                    live_preview_table.c.session_id.in_(session_ids),
                    live_preview_table.c.superseded_at.is_(None),
                )
            ).mappings()
        }

    provider_alias_by_thread: dict[str, Any] = {}
    source_alias_by_thread: dict[str, Any] = {}
    if thread_ids:
        for row in connection.execute(
            select(alias_table)
            .where(alias_table.c.thread_id.in_(thread_ids))
            .order_by(alias_table.c.last_seen_at.desc(), alias_table.c.id.desc())
        ).mappings():
            target = provider_alias_by_thread if row["alias_kind"] == "provider_session_id" else source_alias_by_thread
            if row["alias_kind"] in {"provider_session_id", "source_path"}:
                target.setdefault(str(row["thread_id"]), row)

    ever_managed_threads: set[str] = set()
    if thread_ids:
        ever_managed_threads = {
            str(row[0])
            for row in connection.execute(
                select(run_table.c.thread_id)
                .join(connection_table, connection_table.c.run_id == run_table.c.id)
                .where(run_table.c.thread_id.in_(thread_ids))
                .distinct()
            )
        }

    result: list[dict[str, Any]] = []
    for session_id in session_ids:
        catalog = catalogs.get(session_id)
        card = cards.get(session_id)
        if catalog is None:
            continue
        primary_thread = primary_by_session.get(session_id)
        thread_id = str(primary_thread["id"]) if primary_thread is not None else None
        latest_run = latest_run_by_thread.get(thread_id) if thread_id is not None else None
        run_id = str(latest_run["id"]) if latest_run is not None else None
        result.append(
            {
                "owner_id": owner_by_session.get(session_id),
                "catalog": _row_dto(catalog, fields=_CATALOG_FIELDS, text_limits=_CATALOG_TEXT_LIMITS),
                "card": _row_dto(card, fields=_CARD_FIELDS, text_limits=_CARD_TEXT_LIMITS),
                "transcript_coordinates": (
                    {
                        "source_revision": (
                            int(raw_transcript_by_session[session_id]["source_revision"])
                            if raw_transcript_by_session.get(session_id, {}).get("source_revision") is not None
                            else None
                        ),
                        "durable_revision": int(storage_rows[session_id]["commit_seq"]),
                        "render_revision": render_revision_by_generation.get(str(storage_rows[session_id]["current_render_generation"])),
                        "last_append_at": _encode_datetime(raw_transcript_by_session[session_id]["last_append_at"])
                        if session_id in raw_transcript_by_session
                        else None,
                    }
                    if session_id in storage_rows
                    else None
                ),
                "runtime": _runtime_dto(runtime_by_session.get(session_id), compact=compact),
                "pending_interaction": (
                    _interaction_dto(SimpleNamespace(**pending_interaction_by_session[session_id]))
                    if session_id in pending_interaction_by_session
                    else None
                ),
                "machine_heartbeat": _row_dto(
                    heartbeat_by_device.get(str(catalog.get("device_id") or "")),
                    fields=frozenset({"device_id", "received_at", "is_offline"}),
                ),
                "readiness": _row_dto(
                    readiness_by_session.get(session_id),
                    fields=_READINESS_FIELDS,
                    text_limits={"error_message": 256},
                ),
                "primary_thread": _row_dto(primary_thread, fields=_THREAD_FIELDS),
                "latest_run": _row_dto(latest_run, fields=_RUN_FIELDS, text_limits=_RUN_TEXT_LIMITS),
                "connections": [
                    _row_dto(row, fields=_CONNECTION_FIELDS, text_limits=_CONNECTION_TEXT_LIMITS)
                    for row in _bounded_connections(connections_by_run.get(run_id, []), observed_at=observed_at)
                ],
                "latest_console_turn": _row_dto(
                    console_turn_by_session.get(session_id),
                    fields=frozenset({"id", "session_id", "thread_id", "run_id", "state", "origin", "created_at", "updated_at"}),
                ),
                **(
                    {
                        "control_leases": [
                            _row_dto(row, fields=_CONTROL_LEASE_FIELDS, text_limits=_CONTROL_LEASE_TEXT_LIMITS)
                            for row in control_leases_by_session.get(session_id, [])
                        ],
                        "live_preview": _row_dto(
                            live_preview_by_session.get(session_id),
                            fields=_LIVE_PREVIEW_FIELDS,
                            text_limits=_LIVE_PREVIEW_TEXT_LIMITS,
                        ),
                    }
                    if not compact
                    else {}
                ),
                "provider_alias": (
                    _truncate_utf8(str(provider_alias_by_thread[thread_id]["alias_value"]), 512)
                    if not compact and thread_id in provider_alias_by_thread
                    else None
                ),
                "resume": (
                    {
                        "provider_session_id": (
                            _truncate_utf8(str(provider_alias_by_thread[thread_id]["alias_value"]), 512)
                            if thread_id in provider_alias_by_thread
                            else None
                        ),
                        "source_path": (
                            _truncate_utf8(str(source_alias_by_thread[thread_id]["alias_value"]), 4096)
                            if thread_id in source_alias_by_thread
                            else None
                        ),
                        "ever_managed": thread_id in ever_managed_threads,
                    }
                    if thread_id is not None and not compact
                    else None
                ),
            }
        )
    return result


_CATALOG_TEXT_LIMITS = {
    "provider": 64,
    "environment": 32,
    "project": 255,
    "device_id": 255,
    "device_name": 255,
    "cwd": 512,
    "git_repo": 512,
    "git_branch": 255,
    "summary": 768,
    "summary_title": 255,
    "anchor_title": 255,
    "title_last_error": 128,
    "first_user_message_preview": 384,
}
_CARD_TEXT_LIMITS = {
    "summary_title": 255,
    "first_user_message_preview": 384,
    "archive_state": 32,
    "origin_kind": 64,
    "launch_actor": 32,
    "launch_surface": 32,
}

_CATALOG_FIELDS = frozenset(
    {
        "session_id",
        "provider",
        "environment",
        "project",
        "device_id",
        "device_name",
        "cwd",
        "git_repo",
        "git_branch",
        "started_at",
        "ended_at",
        "closed_at",
        "close_reason",
        "last_activity_at",
        "user_messages",
        "assistant_messages",
        "tool_calls",
        "summary",
        "summary_title",
        "anchor_title",
        "anchor_title_source",
        "title_retry_at",
        "title_last_error",
        "first_user_message_preview",
        "transcript_revision",
        "summary_revision",
        "user_state",
        "user_state_at",
        "last_console_result_at",
        "last_console_result_outcome",
        "last_read_at",
        "last_user_input_at",
        "primary_thread_id",
        "notification_muted",
        "origin_kind",
        "hidden_from_default_timeline",
        "user_hidden_from_timeline",
        "user_hidden_at",
        "launch_actor",
        "launch_surface",
        "permission_mode",
        # Callers deciding what a session may do need to know whether the
        # posture above was recorded or merely defaulted.
        "permission_mode_source",
    }
)
_CARD_FIELDS = frozenset(
    {
        "session_id",
        "last_activity_at",
        "summary_title",
        "first_user_message_preview",
        "user_messages",
        "assistant_messages",
        "tool_calls",
        "transcript_revision",
        "archive_state",
    }
)
_RUNTIME_FIELDS = frozenset(
    {
        "runtime_key",
        "session_id",
        "thread_id",
        "run_id",
        "provider",
        "device_id",
        "phase",
        "phase_source",
        "active_tool",
        "phase_started_at",
        "execution_started_at",
        "last_runtime_signal_at",
        "last_progress_at",
        "last_live_at",
        # What the activity lease reads when the provider has gone quiet: a
        # hook provider states its phase once and says nothing for the length
        # of a tool call, so the observation above ages while the session runs.
        "last_asserted_at",
        "timeline_anchor_at",
        "freshness_expires_at",
        "terminal_state",
        "terminal_reason",
        "terminal_source",
        "terminal_at",
        "pending_interaction_id",
        "pending_interaction_kind",
        "pending_interaction_opened_at",
        "pending_interaction_updated_at",
        "pending_interaction_projection_json",
        "pending_interaction_can_respond",
        "runtime_version",
        "updated_at",
    }
)
_READINESS_FIELDS = frozenset(
    {
        "session_id",
        "owner_id",
        "client_request_id",
        "provider",
        "device_id",
        "machine_id",
        "project",
        "execution_lifetime",
        "state",
        "command_id",
        "error_code",
        "error_message",
        "expires_at",
        "created_at",
        "updated_at",
    }
)
_THREAD_FIELDS = frozenset(
    {
        "id",
        "session_id",
        "provider",
        "device_id",
        "cwd",
        "provider_config_json",
        "parent_thread_id",
        "parent_session_id",
        "parent_event_id",
        "branch_kind",
        "origin_kind",
        "hidden_from_default_timeline",
        "is_primary",
        "created_at",
        "updated_at",
    }
)
_RUN_FIELDS = frozenset(
    {
        "id",
        "thread_id",
        "provider",
        "host_id",
        "boot_id",
        "pid",
        "process_start_time",
        "cwd",
        "launch_origin",
        "started_at",
        "ended_at",
        "exit_status",
    }
)
_CONNECTION_FIELDS = frozenset(
    {
        "id",
        "run_id",
        "adapter_connection_id",
        "lease_generation",
        "control_plane",
        "acquisition_kind",
        "state",
        "device_id",
        "can_send_input",
        "can_interrupt",
        "can_terminate",
        "can_tail_output",
        "can_resume",
        "acquired_at",
        "released_at",
        "last_health_at",
    }
)
_CONTROL_LEASE_FIELDS = frozenset(
    {
        "id",
        "session_id",
        "provider",
        "device_id",
        "machine_id",
        "state",
        "sequence",
        "heartbeat_at",
        "payload_json",
        "updated_at",
    }
)
_LIVE_PREVIEW_FIELDS = frozenset(
    {
        "session_id",
        "thread_id",
        "turn_key",
        "seq",
        "preview_text",
        "provisional_cursor",
        "provisional_complete",
        "event_origin",
        "preview_observed_at",
        "preview_updated_at",
        "source",
        "last_observation_id",
        "superseded_at",
    }
)
_RUNTIME_TEXT_LIMITS = {
    "runtime_key": 255,
    "provider": 64,
    "device_id": 255,
    "phase": 32,
    "phase_source": 32,
    "active_tool": 128,
    "terminal_state": 32,
    "terminal_reason": 64,
    "terminal_source": 64,
    "pending_interaction_id": 255,
    "pending_interaction_kind": 32,
}
_RUN_TEXT_LIMITS = {
    "provider": 64,
    "host_id": 255,
    "boot_id": 64,
    "cwd": 512,
    "launch_origin": 32,
    "exit_status": 64,
}
_CONNECTION_TEXT_LIMITS = {
    "control_plane": 64,
    "acquisition_kind": 32,
    "state": 32,
    "device_id": 255,
}
_CONTROL_LEASE_TEXT_LIMITS = {
    "provider": 64,
    "device_id": 255,
    "machine_id": 255,
    "state": 32,
    "payload_json": 2048,
}
_LIVE_PREVIEW_TEXT_LIMITS = {
    "thread_id": 255,
    "turn_key": 512,
    "preview_text": 8_192,
    "provisional_cursor": 512,
    "event_origin": 32,
    "source": 128,
    "last_observation_id": 512,
}


def _row_dto(
    row,
    *,
    fields: frozenset[str] | None = None,
    text_limits: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    if row is None:
        return None
    limits = text_limits or {}
    result: dict[str, Any] = {}
    for key, value in row.items():
        if fields is not None and key not in fields:
            continue
        if isinstance(value, datetime):
            result[key] = _encode_datetime(value)
        elif isinstance(value, UUID):
            result[key] = str(value)
        elif isinstance(value, str):
            result[key] = _truncate_utf8(value, limits.get(key, 255))
        else:
            result[key] = value
    return result


def _machine_health_heartbeat_dto(row) -> dict[str, Any]:
    result = _row_dto(row, fields=_MACHINE_HEALTH_HEARTBEAT_FIELDS)
    assert result is not None
    raw = _decode_json_object(row.get("raw_json"))
    projected_raw = {key: raw[key] for key in _MACHINE_HEALTH_RAW_FIELDS if key in raw}
    encoded_raw = json.dumps(projected_raw, separators=(",", ":"), sort_keys=True)
    if len(encoded_raw.encode("utf-8")) > _MACHINE_HEALTH_RAW_MAX_BYTES:
        projected_raw.pop("archive_backlog", None)
        encoded_raw = json.dumps(projected_raw, separators=(",", ":"), sort_keys=True)
    if len(encoded_raw.encode("utf-8")) > _MACHINE_HEALTH_RAW_MAX_BYTES:
        projected_raw["history_import"] = {"state": "unavailable"}
        encoded_raw = json.dumps(projected_raw, separators=(",", ":"), sort_keys=True)
    if len(encoded_raw.encode("utf-8")) > _MACHINE_HEALTH_RAW_MAX_BYTES:
        encoded_raw = "{}"
    assert len(encoded_raw.encode("utf-8")) <= _MACHINE_HEALTH_RAW_MAX_BYTES
    result["raw_json"] = encoded_raw
    return result


def _runtime_dto(row, *, compact: bool) -> dict[str, Any] | None:
    result = _row_dto(row, fields=_RUNTIME_FIELDS, text_limits=_RUNTIME_TEXT_LIMITS)
    if result is not None:
        result["pending_interaction_projection_json"] = _bounded_pause_projection(
            result.get("pending_interaction_projection_json"),
            compact=compact,
        )
    return result


def _bounded_pause_projection(value: object, *, compact: bool) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    string_limits = {
        "id": 128,
        "request_key": 255,
        "session_id": 64,
        "runtime_key": 255,
        "kind": 64,
        "status": 32,
        "provider": 64,
        "title": 96 if compact else 160,
        "summary": 128 if compact else 256,
        "tool_name": 128,
        "occurred_at": 64,
        "last_seen_at": 64,
        "resolved_at": 64,
        "expires_at": 64,
    }
    for key, maximum in string_limits.items():
        raw = value.get(key)
        result[key] = _truncate_utf8(str(raw), maximum) if raw is not None else None
    result["can_respond"] = bool(value.get("can_respond"))
    questions: list[dict[str, Any]] = []
    for raw_question in value.get("questions", [])[:3] if isinstance(value.get("questions"), list) else []:
        if not isinstance(raw_question, dict):
            continue
        options: list[dict[str, str | None]] = []
        for raw_option in raw_question.get("options", [])[:4] if isinstance(raw_question.get("options"), list) else []:
            if not isinstance(raw_option, dict):
                continue
            options.append(
                {
                    "label": _truncate_utf8(str(raw_option.get("label") or ""), 32 if compact else 48),
                    "description": (
                        _truncate_utf8(
                            str(raw_option["description"]),
                            32 if compact else 64,
                        )
                        if raw_option.get("description")
                        else None
                    ),
                    "value": (
                        _truncate_utf8(str(raw_option["value"]), 32 if compact else 48) if raw_option.get("value") is not None else None
                    ),
                }
            )
        questions.append(
            {
                "id": _truncate_utf8(str(raw_question.get("id") or ""), 128),
                "header": (_truncate_utf8(str(raw_question["header"]), 48 if compact else 64) if raw_question.get("header") else None),
                "question": _truncate_utf8(
                    str(raw_question.get("question") or "Answer required"),
                    128 if compact else 192,
                ),
                "multi_select": bool(raw_question.get("multi_select")),
                "options": options,
            }
        )
    result["questions"] = questions
    return result


def _bounded_connections(rows: list[Any], *, observed_at: datetime) -> list[Any]:
    state_priority = {"attached": 5, "degraded": 4, "detached": 3, "released": 2, "ended": 1}

    def key(row) -> tuple[Any, ...]:
        state = str(row["state"] or "")
        last_health = _as_aware_utc(row["last_health_at"])
        if state in {"attached", "degraded"} and (last_health is None or observed_at - last_health > _CONTROL_LEASE_TTL):
            state = "detached"
        capabilities = sum(bool(row[field]) for field in ("can_send_input", "can_interrupt", "can_terminate", "can_tail_output"))
        return (
            state_priority.get(state, 0),
            capabilities,
            last_health or datetime.min.replace(tzinfo=UTC),
            int(row["id"] or 0),
        )

    return sorted(rows, key=key, reverse=True)[:SESSION_CONNECTION_LIMIT]


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore")


def _heartbeat_request_sha256(
    *,
    heartbeat: dict[str, Any],
    managed_leases: list[dict[str, Any]],
    managed_leases_present: bool,
    owner_id: int | None,
) -> str:
    payload = {
        "heartbeat": _jsonable_catalog_value(heartbeat),
        "managed_leases": _jsonable_catalog_value(managed_leases),
        "managed_leases_present": managed_leases_present,
        "owner_id": owner_id,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _jsonable_catalog_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _encode_datetime(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable_catalog_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable_catalog_value(item) for item in value]
    return value


def _decode_json_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        decoded = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("catalog receipt JSON is invalid") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("catalog receipt JSON is not an object")
    return decoded


def _live_control_grant_payload(grant: object) -> dict[str, Any]:
    return {
        "connection_id": getattr(grant, "connection_id"),
        "catalog_connection_id": int(getattr(grant, "catalog_connection_id")),
        "run_id": str(getattr(grant, "run_id")),
        "lease_generation": str(getattr(grant, "lease_generation")),
        "identity_source": str(getattr(grant, "identity_source")),
    }


def _stored_live_control_grant_matches(stored: dict[str, Any], current: dict[str, Any]) -> bool:
    connection_id = stored.get("connection_id")
    catalog_connection_id = stored.get("catalog_connection_id")
    identity_source = str(stored.get("identity_source") or "").strip()
    if catalog_connection_id is None and isinstance(connection_id, int):
        catalog_connection_id = connection_id
        identity_source = "legacy_synthetic"
    return bool(
        isinstance(catalog_connection_id, int)
        and catalog_connection_id == current["catalog_connection_id"]
        and str(connection_id) == str(current["connection_id"])
        and str(stored.get("run_id") or "") == current["run_id"]
        and str(stored.get("lease_generation") or "") == current["lease_generation"]
        and identity_source == current["identity_source"]
    )


def _non_negative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _source_epoch_dto(row) -> dict[str, Any]:
    return {
        "source_epoch": str(row["source_epoch"]),
        "tenant_id": str(row["tenant_id"]),
        "machine_id": str(row["machine_id"]),
        "provider": str(row["provider"]),
        "opaque_source_id": str(row["opaque_source_id"]),
        "range_kind": str(row["range_kind"]),
        "state": str(row["state"]),
        "predecessor_source_epoch": row["predecessor_source_epoch"],
        "replaced_by_source_epoch": row["replaced_by_source_epoch"],
        "accepted_through": str(int(row["accepted_through"])),
        "object_count": int(row["object_count"]),
        "commit_seq": str(row["commit_seq"]),
        "closed_commit_seq": str(row["closed_commit_seq"]) if row["closed_commit_seq"] is not None else None,
        "opened_at": _encode_datetime(row["opened_at"]),
        "closed_at": _encode_datetime(row["closed_at"]),
        "close_reason": row["close_reason"],
    }


def _raw_object_receipt(row) -> dict[str, object]:
    missing = tuple(json.loads(str(row["missing_media_hashes_json"] or "[]")))
    return DurableReceipt(
        envelope_id=str(row["envelope_id"]),
        object_hash=str(row["object_hash"]),
        commit_seq=int(row["commit_seq"]),
        render_state=str(row["render_state"]),
        media_state=str(row["media_state"]),
        missing_media_hashes=missing,
    ).as_wire()


def _raw_object_manifest_dto(row) -> dict[str, Any]:
    return {
        "envelope_id": str(row["envelope_id"]),
        "tenant_id": str(row["tenant_id"]),
        "session_id": str(row["session_id"]),
        "machine_id": str(row["machine_id"]),
        "provider": str(row["provider"]),
        "opaque_source_id": str(row["opaque_source_id"]),
        "source_epoch": str(row["source_epoch"]),
        "range_kind": str(row["range_kind"]),
        "range_start": str(int(row["range_start"])),
        "range_end": str(int(row["range_end"])),
        "record_count": int(row["record_count"]),
        "object_hash": str(row["object_hash"]),
        "payload_hash": str(row["payload_hash"]),
        "object_path": str(row["object_path"]),
        "uncompressed_size": int(row["uncompressed_size"]),
        "compressed_size": int(row["compressed_size"]),
        "provenance_kind": str(row["provenance_kind"]),
        "commit_seq": str(row["commit_seq"]),
        "render_state": str(row["render_state"]),
        "media_state": str(row["media_state"]),
        "retired_at": _encode_datetime(row["retired_at"]),
        "retirement_revision": (str(row["retirement_revision"]) if row["retirement_revision"] is not None else None),
    }


def _session_keeps_published_render(connection, session: Mapping[str, Any] | None) -> bool:
    """Whether a session that publishes a render still does, after a render-less commit.

    ``sessions.render_state`` is a session-level fact: ``ready`` means the
    session serves a current render generation, and only a commit that carries
    a manifest (or a repair that restores a generation) publishes one.
    A raw-only commit -- Cursor's ``agent-transcripts`` projection, which must
    never claim render authority over ``store.db`` -- attaches no render and
    withdraws none, so it leaves ``ready`` alone. The exception is a
    replacement epoch that retires the last live render object of the current
    generation in the same transaction: nothing is then published, and
    ``pending`` is the truth.
    """

    if session is None or session["render_state"] != "ready" or session["current_render_generation"] is None:
        return False
    objects = RenderObject.__table__
    generations = RenderGeneration.__table__
    return (
        connection.execute(
            select(objects.c.object_id)
            .select_from(objects.join(generations, generations.c.generation_id == objects.c.generation_id))
            .where(
                objects.c.generation_id == session["current_render_generation"],
                generations.c.state == "current",
                objects.c.retired_at.is_(None),
            )
            .limit(1)
        ).first()
        is not None
    )


def _recompute_render_generation_projection(
    connection,
    *,
    session_id: str,
    generation_id: str,
    commit_seq: int,
    commit_time: datetime,
    reset_derived_title: bool = False,
    semantic_projection_version: int | None = None,
) -> None:
    """Rebuild bounded heads after the rare source-epoch replacement path."""

    generation = RenderGeneration.__table__
    objects = RenderObject.__table__
    sessions = StorageSession.__table__
    rows = list(
        connection.execute(
            select(objects).where(
                objects.c.session_id == session_id,
                objects.c.generation_id == generation_id,
                objects.c.retired_at.is_(None),
            )
        )
        .mappings()
        .all()
    )
    first_order = None
    last_order = None
    first_preview_row = None
    last_preview_row = None
    for row in rows:
        first_order = _minimum_order_key(first_order, row["first_order_key"])
        last_order = _maximum_order_key(last_order, row["last_order_key"])
        if (
            row["first_user_message_preview"] is not None
            and row["first_order_key"] is not None
            and (
                first_preview_row is None
                or tuple(json.loads(str(row["first_order_key"]))) < tuple(json.loads(str(first_preview_row["first_order_key"])))
            )
        ):
            first_preview_row = row
        if (
            row["last_visible_text_preview"] is not None
            and row["last_order_key"] is not None
            and (
                last_preview_row is None
                or tuple(json.loads(str(row["last_order_key"]))) > tuple(json.loads(str(last_preview_row["last_order_key"])))
            )
        ):
            last_preview_row = row
    source_ids = sorted(str(row["source_envelope_id"]) for row in rows)
    source_chain_hash = hashlib.sha256(
        b"longhouse-render-source-set-v1\0" + b"".join(bytes.fromhex(value) for value in source_ids)
    ).hexdigest()
    generation_values = {
        "state": "current",
        "source_chain_hash": source_chain_hash,
        "object_count": len(rows),
        "event_count": sum(int(row["event_count"]) for row in rows),
        "first_order_key": first_order,
        "last_order_key": last_order,
        "commit_seq": commit_seq,
        "updated_at": commit_time,
    }
    connection.execute(update(generation).where(generation.c.generation_id == generation_id).values(**generation_values))
    session_row = connection.execute(select(sessions).where(sessions.c.session_id == session_id)).mappings().first()
    projection_values: dict[str, Any] = {
        "current_render_generation": generation_id,
        "user_messages": sum(int(row["user_messages"]) for row in rows),
        "assistant_messages": sum(int(row["assistant_messages"]) for row in rows),
        "tool_calls": sum(int(row["tool_calls"]) for row in rows),
        "semantic_projection_version": (
            semantic_projection_version
            if semantic_projection_version is not None
            else min((int(row["semantic_projection_version"] or 0) for row in rows), default=0)
        ),
        "summary_title": func.coalesce(
            sessions.c.summary_title,
            sanitize_timeline_title(
                str(first_preview_row["first_user_message_preview"]) if first_preview_row is not None else None,
                max_words=6,
            ),
        ),
        "first_user_message_preview": (str(first_preview_row["first_user_message_preview"]) if first_preview_row is not None else None),
        "last_visible_text_preview": (str(last_preview_row["last_visible_text_preview"]) if last_preview_row is not None else None),
    }
    reset_title = bool(
        reset_derived_title
        and session_row is not None
        and _semantic_title_needs_reset(
            old_first_preview=session_row["first_user_message_preview"],
            old_summary_title=session_row["summary_title"],
            old_anchor_title=session_row["anchor_title"],
            new_first_preview=projection_values["first_user_message_preview"],
        )
    )
    if reset_title:
        # A pre-semantic title may have been generated from a provider-local
        # command. Clear both the drifting summary and its frozen headline so
        # the normal title reconciler can regenerate from the recovered first
        # conversational prompt. There is no user-editable title field here;
        # anchor_title is the write-once AI projection.
        projection_values.update(
            {
                "summary_title": sanitize_timeline_title(
                    str(projection_values["first_user_message_preview"] or ""),
                    max_words=6,
                ),
                "anchor_title": None,
                "title_retry_at": commit_time,
                "title_last_error": None,
            }
        )
    connection.execute(update(sessions).where(sessions.c.session_id == session_id).values(**projection_values))
    if reset_title:
        connection.execute(
            update(LiveSessionCatalog.__table__)
            .where(LiveSessionCatalog.__table__.c.session_id == session_id)
            .values(
                summary_title=None,
                anchor_title=None,
                title_retry_at=commit_time,
                title_last_error=None,
                first_user_message_preview=projection_values["first_user_message_preview"],
                last_visible_text_preview=projection_values["last_visible_text_preview"],
                user_messages=projection_values["user_messages"],
                assistant_messages=projection_values["assistant_messages"],
                tool_calls=projection_values["tool_calls"],
                updated_at=commit_time,
            )
        )
        connection.execute(
            update(LiveTimelineCard.__table__)
            .where(LiveTimelineCard.__table__.c.session_id == session_id)
            .values(
                summary_title=None,
                first_user_message_preview=projection_values["first_user_message_preview"],
                last_visible_text_preview=projection_values["last_visible_text_preview"],
                user_messages=projection_values["user_messages"],
                assistant_messages=projection_values["assistant_messages"],
                tool_calls=projection_values["tool_calls"],
                updated_at=commit_time,
            )
        )


def _semantic_title_needs_reset(
    *,
    old_first_preview: object,
    old_summary_title: object,
    old_anchor_title: object,
    new_first_preview: object,
) -> bool:
    """Reset titles when semantic repair removes the entire first-message set.

    Storage-v2 version-zero is also used for unrelated legacy rows. When a
    repaired projection still has a conversational first message, preserve a
    frozen title unless it plainly equals the old fallback or contains native
    control markup. When the repaired projection has no first message at all,
    the old title cannot be justified by semantic content; this also handles
    an LLM title that paraphrased a provider-local control.
    """

    old_first = str(old_first_preview or "").strip()
    new_first = str(new_first_preview or "").strip()
    if not old_first or old_first == new_first:
        return False
    old_fallback = sanitize_title(old_first, max_words=6)
    lowered = old_first.lower()
    old_first_was_control = any(
        marker in lowered
        for marker in (
            "<local-command-caveat>",
            "<local-command-stdout>",
            "<command-name>",
            "<command-message>",
            "<command-args>",
        )
    )
    if not new_first:
        # A control-only session has no replacement preview. The old AI title
        # may be a paraphrase, so comparing strings cannot establish that it
        # is safe to keep. This path only runs while converting a version-zero
        # projection whose complete aggregate now has no semantic prompt.
        return bool(str(old_summary_title or "").strip() or str(old_anchor_title or "").strip())
    if old_fallback and str(old_summary_title or "").strip() == old_fallback:
        return True
    if old_fallback and str(old_anchor_title or "").strip() == old_fallback:
        return True
    return old_first_was_control


def _minimum_order_key(left: object, right: object) -> str | None:
    values = [value for value in (left, right) if value is not None]
    return min(values, key=lambda value: tuple(json.loads(str(value)))) if values else None


def _maximum_order_key(left: object, right: object) -> str | None:
    values = [value for value in (left, right) if value is not None]
    return max(values, key=lambda value: tuple(json.loads(str(value)))) if values else None


def _render_order_columns(first: object, last: object) -> dict[str, object | None]:
    values: dict[str, object | None] = {}
    fields = (
        "order_time_us",
        "machine_id",
        "provider",
        "opaque_source_id",
        "source_epoch",
        "source_position",
        "event_subordinal",
    )
    for prefix, raw in (("first", first), ("last", last)):
        decoded = json.loads(str(raw)) if raw is not None else [None] * len(fields)
        values.update({f"{prefix}_{field}": value for field, value in zip(fields, decoded, strict=True)})
    return values


def _runtime_dependency_dto(row) -> dict[str, Any]:
    return {
        "use_case": str(row["use_case"]),
        "provider": str(row["provider"]),
        "model": str(row["model"]),
        "credential_binding": str(row["credential_binding"]),
        "state": str(row["state"]),
        "incident_id": row["incident_id"],
        "failure_class": row["failure_class"],
        "first_failure_at": _encode_datetime(row["first_failure_at"]),
        "last_failure_at": _encode_datetime(row["last_failure_at"]),
        "next_probe_at": _encode_datetime(row["next_probe_at"]),
        # This is a one-way fingerprint used only to notice credential repair;
        # it is not credential material and cannot be reversed into the key.
        "credential_generation": str(row["credential_generation"]),
        "probe_expires_at": _encode_datetime(row["probe_expires_at"]),
        "last_error": row["last_error"],
        "recovered_at": _encode_datetime(row["recovered_at"]),
        "legacy_repair_version": int(row["legacy_repair_version"] or 0),
        "updated_at": _encode_datetime(row["updated_at"]),
    }


def _storage_session_dto(row) -> dict[str, Any]:
    return {
        "session_id": str(row["session_id"]),
        "tenant_id": str(row["tenant_id"]),
        "owner_id": row["owner_id"],
        "provider": str(row["provider"]),
        "environment": str(row["environment"]),
        "machine_id": str(row["machine_id"]),
        "project": row["project"],
        "cwd": row["cwd"],
        "git_repo": row["git_repo"],
        "git_branch": row["git_branch"],
        "started_at": _encode_datetime(row["started_at"]),
        "last_activity_at": _encode_datetime(row["last_activity_at"]),
        "ended_at": _encode_datetime(row["ended_at"]),
        "user_messages": int(row["user_messages"]),
        "assistant_messages": int(row["assistant_messages"]),
        "tool_calls": int(row["tool_calls"]),
        "summary_title": row["summary_title"],
        "anchor_title": row["anchor_title"],
        "anchor_title_source": row["anchor_title_source"],
        "title_attempt_count": int(row["title_attempt_count"] or 0),
        "title_last_attempt_at": _encode_datetime(row["title_last_attempt_at"]),
        "title_retry_at": _encode_datetime(row["title_retry_at"]),
        "title_last_error": row["title_last_error"],
        "title_dependency_incident_id": row["title_dependency_incident_id"],
        "first_user_message_preview": row["first_user_message_preview"],
        "last_visible_text_preview": row["last_visible_text_preview"],
        "transcript_revision": str(row["transcript_revision"]),
        "semantic_projection_version": int(row["semantic_projection_version"] or 0),
        "current_render_generation": row["current_render_generation"],
        "raw_state": str(row["raw_state"]),
        "render_state": str(row["render_state"]),
        "media_state": str(row["media_state"]),
        "missing_media_hashes": list(json.loads(str(row["missing_media_hashes_json"] or "[]"))),
        "user_state": str(row["user_state"]),
        "notification_muted": bool(row["notification_muted"]),
        "origin_kind": row["origin_kind"],
        "hidden_from_default_timeline": bool(row["hidden_from_default_timeline"]),
        "user_hidden_from_timeline": bool(row["user_hidden_from_timeline"]),
        "user_hidden_at": _encode_datetime(row["user_hidden_at"]),
        "launch_actor": row["launch_actor"],
        "launch_surface": row["launch_surface"],
        "is_subagent": bool(row["is_subagent"]),
        "subagent_parent_provider_session_id": row["subagent_parent_provider_session_id"],
        "subagent_parent_session_id": row["subagent_parent_session_id"],
        "subagent_parent_tool_call_id": row["subagent_parent_tool_call_id"],
        "subagent_run_id": row["subagent_run_id"],
        "commit_seq": str(row["commit_seq"]),
        "created_at": _encode_datetime(row["created_at"]),
        "updated_at": _encode_datetime(row["updated_at"]),
    }


def _delete_bounded_live_session_state(connection, *, session_key: str, deleted_at: datetime) -> int:
    threads = LiveSessionThread.__table__
    aliases = LiveSessionThreadAlias.__table__
    runs = LiveSessionRun.__table__
    connections = LiveSessionConnection.__table__
    thread_ids = list(connection.execute(select(threads.c.id).where(threads.c.session_id == session_key)).scalars())
    run_ids = list(connection.execute(select(runs.c.id).where(runs.c.thread_id.in_(thread_ids))).scalars()) if thread_ids else []
    removed = 0
    if run_ids:
        removed += int(connection.execute(delete(connections).where(connections.c.run_id.in_(run_ids))).rowcount or 0)
        removed += int(connection.execute(delete(runs).where(runs.c.id.in_(run_ids))).rowcount or 0)
    if thread_ids:
        removed += int(connection.execute(delete(aliases).where(aliases.c.thread_id.in_(thread_ids))).rowcount or 0)
        removed += int(connection.execute(delete(threads).where(threads.c.id.in_(thread_ids))).rowcount or 0)
    for table in (
        LiveSessionCatalog.__table__,
        LiveTimelineCard.__table__,
        LiveSessionLaunchAttempt.__table__,
        LiveSession.__table__,
        LiveRuntimeState.__table__,
        LiveInteractionRequest.__table__,
        LiveControlLease.__table__,
        LiveLaunchReadiness.__table__,
        LiveSessionLivePreview.__table__,
        LiveMachineControlOperation.__table__,
        LiveSessionInputReceipt.__table__,
    ):
        removed += int(connection.execute(delete(table).where(table.c.session_id == session_key)).rowcount or 0)
    connection.execute(
        update(LiveNotificationClientPresence.__table__)
        .where(LiveNotificationClientPresence.session_id == session_key)
        .values(session_id=None, updated_at=deleted_at)
    )
    connection.execute(
        update(LiveAPNSLiveActivityRegistration.__table__)
        .where(
            LiveAPNSLiveActivityRegistration.session_id == session_key,
            LiveAPNSLiveActivityRegistration.ended_at.is_(None),
        )
        .values(ended_at=deleted_at, updated_at=deleted_at)
    )
    return removed


def _render_generation_dto(row) -> dict[str, Any]:
    return {
        "generation_id": str(row["generation_id"]),
        "session_id": str(row["session_id"]),
        "parser_revision": str(row["parser_revision"]),
        "ordering_revision": str(row["ordering_revision"]),
        "state": str(row["state"]),
        "source_chain_hash": str(row["source_chain_hash"]),
        "object_count": int(row["object_count"]),
        "event_count": int(row["event_count"]),
        "first_order_key": row["first_order_key"],
        "last_order_key": row["last_order_key"],
        "commit_seq": str(row["commit_seq"]),
        "created_at": _encode_datetime(row["created_at"]),
        "updated_at": _encode_datetime(row["updated_at"]),
        "superseded_at": _encode_datetime(row["superseded_at"]),
    }


def _render_object_manifest_dto(row) -> dict[str, Any]:
    return {
        "object_id": str(row["object_id"]),
        "generation_id": str(row["generation_id"]),
        "session_id": str(row["session_id"]),
        "source_envelope_id": str(row["source_envelope_id"]),
        "object_hash": str(row["object_hash"]),
        "payload_hash": str(row["payload_hash"]),
        "object_path": str(row["object_path"]),
        "uncompressed_size": int(row["uncompressed_size"]),
        "compressed_size": int(row["compressed_size"]),
        "event_count": int(row["event_count"]),
        "user_messages": int(row["user_messages"]),
        "assistant_messages": int(row["assistant_messages"]),
        "tool_calls": int(row["tool_calls"]),
        "abandoned_events": int(row["abandoned_events"]) if row["abandoned_events"] is not None else None,
        "first_user_message_preview": row["first_user_message_preview"],
        "last_visible_text_preview": row["last_visible_text_preview"],
        "semantic_projection_version": int(row["semantic_projection_version"] or 0),
        "first_order_key": row["first_order_key"],
        "last_order_key": row["last_order_key"],
        "commit_seq": str(row["commit_seq"]),
        "created_at": _encode_datetime(row["created_at"]),
        "retired_at": _encode_datetime(row["retired_at"]),
        "retirement_revision": str(row["retirement_revision"]) if row["retirement_revision"] is not None else None,
    }


def _media_object_dto(row) -> dict[str, Any]:
    return {
        "media_hash": str(row["media_hash"]),
        "state": str(row["state"]),
        "mime_type": row["mime_type"],
        "byte_size": int(row["byte_size"]) if row["byte_size"] is not None else None,
        "object_path": row["object_path"],
        "thumb_hash": row["thumb_hash"],
        "derived_from": row["derived_from"],
        "width": row["width"],
        "height": row["height"],
        "commit_seq": str(row["commit_seq"]),
        "observed_at": _encode_datetime(row["observed_at"]),
        "verified_at": _encode_datetime(row["verified_at"]),
        "deleted_at": _encode_datetime(row["deleted_at"]),
    }


def _media_ref_dto(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "session_id": str(row["session_id"]),
        "media_hash": str(row["media_hash"]),
        "envelope_id": row["envelope_id"],
        "ref_key": str(row["ref_key"]),
        "state": str(row["state"]),
        "commit_seq": str(row["commit_seq"]),
        "created_at": _encode_datetime(row["created_at"]),
        "retired_at": _encode_datetime(row["retired_at"]),
        "deletion_revision": str(row["deletion_revision"]) if row["deletion_revision"] is not None else None,
    }


def _projector_state_dto(row) -> dict[str, Any]:
    return {
        "projector": str(row["projector"]),
        "session_id": str(row["session_id"]),
        "desired_revision": str(row["desired_revision"]),
        "completed_revision": str(row["completed_revision"]),
        "claimed_revision": str(row["claimed_revision"]) if row["claimed_revision"] is not None else None,
        "claim_token": row["claim_token"],
        "worker_id": row["worker_id"],
        "claim_expires_at": _encode_datetime(row["claim_expires_at"]),
        "status": str(row["status"]),
        "failure_count": int(row["failure_count"]),
        "last_error_code": row["last_error_code"],
        "last_error_message": row["last_error_message"],
        "retry_at": _encode_datetime(row["retry_at"]),
        "commit_seq": str(row["commit_seq"]),
        "updated_at": _encode_datetime(row["updated_at"]),
    }


def _raw_object_matches(row, immutable: dict[str, Any]) -> bool:
    for key, value in immutable.items():
        existing = row[key]
        if isinstance(value, datetime):
            if _as_aware_utc(existing) != value:
                return False
        elif existing != value:
            return False
    return True


def _u64_key(value: int) -> str:
    if not 0 <= value < 1 << 64:
        raise ValueError("value exceeds u64")
    return f"{value:020d}"


def _source_epoch_conflict(connection, *, reason: str, **evidence: Any) -> dict[str, Any]:
    """Refuse an epoch admission and say why.

    The transport already carries this: `catalogd/server.py` forwards
    `conflict_details` into the 409's `details`. Nothing ever populated it, so
    every admission refusal reached the shipper as a bare "conflict" and the
    engine could only record the manifest probe that followed. Two envelopes
    blocked on 2026-08-04 were still unexplainable three days later for exactly
    this reason.

    Only identity the caller already owns goes into `evidence` — these rows are
    filtered by the caller's own tenant, machine, provider and source — so this
    reveals nothing about anyone else's epochs.
    """
    details: dict[str, Any] = {"reason": reason}
    details.update({key: value for key, value in evidence.items() if value is not None})
    return {
        "source_epoch_conflict": True,
        "conflict_details": details,
        "commit_seq": str(_current_commit_seq(connection)),
    }


def _opaque_source_ids_for_path(source_path: str) -> tuple[str, ...]:
    """Return storage identities for an absolute provider transcript path.

    The engine ships only its stable ``path-sha256`` identity.  The raw
    ``parentSession`` value remains the evidence on the child, so resolve it
    by applying the same hash rather than inventing an alias storage-v2 never
    sends.
    """

    if not source_path or not os.path.isabs(source_path):
        return ()
    candidates = (source_path, os.path.normpath(source_path))
    identities: list[str] = []
    for candidate in candidates:
        identity = f"path-sha256:{hashlib.sha256(candidate.encode()).hexdigest()}"
        if identity not in identities:
            identities.append(identity)
    return tuple(identities)


def _normalized_parent_source_id(source_path: str) -> str | None:
    """Return the indexed identity used for an absolute parent path."""

    identities = _opaque_source_ids_for_path(source_path)
    return identities[-1] if identities else None


def _resolve_session_id_by_source_id(
    connection,
    *,
    provider: str,
    opaque_source_id: str,
    owner_id: str | None,
    machine_id: str,
) -> str | None:
    """Resolve one raw source identity in the owner/machine/provider scope."""

    if owner_id is None or not opaque_source_id:
        return None
    raw_table = LiveRawObject.__table__
    storage_table = StorageSession.__table__
    rows = connection.execute(
        select(raw_table.c.session_id)
        .select_from(raw_table.join(storage_table, storage_table.c.session_id == raw_table.c.session_id))
        .where(raw_table.c.provider == provider)
        .where(raw_table.c.opaque_source_id == opaque_source_id)
        .where(storage_table.c.owner_id == str(owner_id))
        .where(storage_table.c.machine_id == machine_id)
        .where(storage_table.c.provider == provider)
        .where(storage_table.c.raw_state != "retired")
        .distinct()
    ).all()
    return str(rows[0][0]) if len(rows) == 1 else None


def _without_retired_sessions(connection, session_ids: set[str]) -> set[str]:
    """Drop retired sessions: a retired session is never a parent.

    A source replacement retires the predecessor session but keeps its row,
    which still carries the same native id and source aliases as its
    successor. Counting it made every parent of a replaced source ambiguous,
    so its subagents never bound and every append of the parent re-resolved
    them inside the catalog writer (~0.9 s holds on david010, 2026-10-08).
    """

    if not session_ids:
        return session_ids
    storage_table = StorageSession.__table__
    retired = {
        str(row[0])
        for row in connection.execute(
            select(storage_table.c.session_id).where(
                storage_table.c.session_id.in_(sorted(session_ids)),
                storage_table.c.raw_state == "retired",
            )
        ).all()
    }
    return session_ids - retired


def _resolve_session_id_by_provider_session_id(
    connection,
    *,
    provider: str,
    provider_session_id: str,
    owner_id: str | None,
    machine_id: str,
) -> str | None:
    """Resolve one native id only inside an unambiguous owner/machine scope."""

    if owner_id is None or not provider_session_id:
        return None
    alias_table = LiveSessionThreadAlias.__table__
    thread_table = LiveSessionThread.__table__
    storage_table = StorageSession.__table__
    live_table = LiveSession.__table__
    session_key = thread_table.c.session_id
    scope = or_(
        select(storage_table.c.session_id)
        .where(
            storage_table.c.session_id == session_key,
            storage_table.c.owner_id == str(owner_id),
            storage_table.c.machine_id == machine_id,
        )
        .exists(),
        select(live_table.c.session_id)
        .where(
            live_table.c.session_id == session_key,
            live_table.c.owner_id == str(owner_id),
            live_table.c.machine_id == machine_id,
        )
        .exists(),
    )
    rows = connection.execute(
        select(thread_table.c.session_id)
        .select_from(alias_table.join(thread_table, thread_table.c.id == alias_table.c.thread_id))
        .where(alias_table.c.provider == provider)
        .where(alias_table.c.alias_kind == "provider_session_id")
        .where(alias_table.c.alias_value == provider_session_id)
        .where(scope)
        .distinct()
    ).all()
    session_ids = {str(row[0]) for row in rows}
    session_ids.update(
        str(row[0])
        for row in connection.execute(
            select(storage_table.c.session_id).where(
                storage_table.c.provider_session_id == provider_session_id,
                storage_table.c.owner_id == str(owner_id),
                storage_table.c.machine_id == machine_id,
                storage_table.c.provider == provider,
            )
        ).all()
    )
    # Storage-v2 sessions do not necessarily have a live thread (and therefore
    # cannot have a LiveSessionThreadAlias). OpenCode preserves the parent's
    # native id in the parent's delegation.spawn fact metadata instead. Resolve
    # that durable source evidence in the same owner/machine/provider scope.
    fact_table = SessionProviderFact.__table__
    # Only OpenCode keeps the parent's native id in delegation.spawn facts. The
    # match is a LIKE over the JSON payload, which no index serves, so other
    # providers must not pay a fact-table scan for evidence they never write.
    fact_rows = (
        []
        if provider.strip().lower() != "opencode"
        else connection.execute(
            select(fact_table.c.session_id)
            .select_from(fact_table.join(storage_table, storage_table.c.session_id == fact_table.c.session_id))
            .where(
                fact_table.c.kind == "delegation.spawn",
                fact_table.c.payload_json.contains(f'"parentSessionId":"{provider_session_id}"', autoescape=True),
                storage_table.c.owner_id == str(owner_id),
                storage_table.c.machine_id == machine_id,
                storage_table.c.provider == provider,
            )
            .distinct()
        ).all()
    )
    session_ids.update(str(row[0]) for row in fact_rows)
    session_ids = _without_retired_sessions(connection, session_ids)
    return next(iter(session_ids)) if len(session_ids) == 1 else None


def _resolve_session_id_by_source_path(
    connection,
    *,
    provider: str,
    source_path: str,
    owner_id: str | None,
    machine_id: str,
) -> str | None:
    """Resolve a path through raw identity or an existing scoped binding.

    Storage-v2 persists ``path-sha256`` in the raw envelope and does not send
    a source-path alias. A live binding signal may nevertheless have written
    one before ingest, so accept either representation but bind only when the
    union has exactly one durable session; ambiguity stays unknown.
    """

    if owner_id is None or not source_path or not os.path.isabs(source_path):
        return None
    path_values = (source_path, os.path.normpath(source_path))
    opaque_ids = _opaque_source_ids_for_path(source_path)
    if not opaque_ids:
        return None
    session_ids: set[str] = set()
    alias_table = LiveSessionThreadAlias.__table__
    thread_table = LiveSessionThread.__table__
    storage_table = StorageSession.__table__
    live_table = LiveSession.__table__
    session_key = thread_table.c.session_id
    scope = or_(
        select(storage_table.c.session_id)
        .where(
            storage_table.c.session_id == session_key,
            storage_table.c.owner_id == str(owner_id),
            storage_table.c.machine_id == machine_id,
        )
        .exists(),
        select(live_table.c.session_id)
        .where(
            live_table.c.session_id == session_key,
            live_table.c.owner_id == str(owner_id),
            live_table.c.machine_id == machine_id,
        )
        .exists(),
    )
    session_ids.update(
        str(row[0])
        for row in connection.execute(
            select(thread_table.c.session_id)
            .select_from(alias_table.join(thread_table, thread_table.c.id == alias_table.c.thread_id))
            .where(alias_table.c.provider == provider)
            .where(alias_table.c.alias_kind == "source_path")
            .where(alias_table.c.alias_value.in_(path_values))
            .where(scope)
            .distinct()
        ).all()
    )
    for opaque_source_id in opaque_ids:
        resolved = _resolve_session_id_by_source_id(
            connection,
            provider=provider,
            opaque_source_id=opaque_source_id,
            owner_id=owner_id,
            machine_id=machine_id,
        )
        if resolved is not None:
            session_ids.add(resolved)
    session_ids = _without_retired_sessions(connection, session_ids)
    return next(iter(session_ids)) if len(session_ids) == 1 else None


def _bind_orphan_subagents_to_parent(
    connection,
    *,
    provider: str,
    session_key: str,
    alias_values: list[str],
    parent_source_id: str | None,
    owner_id: str | None,
    machine_id: str,
    commit_seq: int,
    commit_time,
) -> int:
    """Adopt indexed same-scope children after a unique parent resolution."""

    if owner_id is None:
        return 0
    source_parent = (
        _resolve_session_id_by_source_id(
            connection,
            provider=provider,
            opaque_source_id=parent_source_id,
            owner_id=owner_id,
            machine_id=machine_id,
        )
        if parent_source_id is not None
        else None
    )
    source_match = source_parent == session_key
    if not source_match and not alias_values:
        return 0
    session_table = StorageSession.__table__
    # One select per pointer kind, each pinned to its own selective index. As
    # one OR the planner chose subagent_parent_session_id IS NULL, true for
    # almost every session, and walked them all on every append (17 ms on
    # david010); without fresh statistics it picks ix_sessions_owner_id, which
    # one owner's catalog also walks end to end. INDEXED BY holds either way.
    scope = {"provider": provider, "owner_id": str(owner_id), "machine_id": machine_id, "session_key": session_key}
    orphan_rows = []
    if source_match:
        orphan_rows.extend(connection.execute(_ORPHANS_BY_PARENT_SOURCE, {**scope, "parent_source_id": parent_source_id}).all())
    if alias_values:
        orphan_rows.extend(connection.execute(_ORPHANS_BY_PARENT_NATIVE_ID, {**scope, "alias_values": list(alias_values)}).all())
    if not orphan_rows:
        return 0
    bound = 0
    for child_session_id, parent_pointer, child_source_id in orphan_rows:
        if source_match and child_source_id == parent_source_id:
            should_bind = True
        else:
            parent_pointer = str(parent_pointer or "").strip()
            should_bind = (
                bool(parent_pointer)
                and _resolve_session_id_by_provider_session_id(
                    connection,
                    provider=provider,
                    provider_session_id=parent_pointer,
                    owner_id=owner_id,
                    machine_id=machine_id,
                )
                == session_key
            )
        if not should_bind:
            continue
        bound += int(
            connection.execute(
                update(session_table)
                .where(
                    session_table.c.session_id == str(child_session_id),
                    session_table.c.subagent_parent_session_id.is_(None),
                )
                .values(subagent_parent_session_id=session_key, commit_seq=commit_seq, updated_at=commit_time)
            ).rowcount
            or 0
        )
    return bound


def adopt_orphan_subagents_once(engine) -> int:
    """Once per catalog, bind orphan subagents whose parent now resolves.

    A parent adopts its orphans only while it commits. Subagents that named a
    parent made ambiguous by a retired predecessor session never bound, and a
    parent that has since gone quiet will not commit again to adopt them. This
    pass resolves each orphan's parent pointer with the same rules a commit
    uses and binds the unambiguous ones. It is a data reconciliation gated by
    its own ``catalog_meta`` marker, so the catalog schema contract does not
    move.
    """

    from zerg.catalogd.schema import SUBAGENT_PARENT_GENERATION
    from zerg.catalogd.schema import catalog_meta

    session_table = StorageSession.__table__
    bound = 0
    with engine.begin() as connection:
        marker = connection.execute(
            select(catalog_meta.c.subagent_parent_generation).where(catalog_meta.c.singleton == 1)
        ).scalar_one_or_none()
        if marker == SUBAGENT_PARENT_GENERATION:
            return 0
        orphans = connection.execute(
            select(
                session_table.c.session_id,
                session_table.c.provider,
                session_table.c.owner_id,
                session_table.c.machine_id,
                session_table.c.subagent_parent_provider_session_id,
                session_table.c.subagent_parent_source_id,
            ).where(
                session_table.c.subagent_parent_session_id.is_(None),
                or_(
                    session_table.c.subagent_parent_provider_session_id.is_not(None),
                    session_table.c.subagent_parent_source_id.is_not(None),
                ),
            )
        ).all()
        now = datetime.now(UTC)
        for child_id, provider, owner_id, machine_id, pointer, source_pointer in orphans:
            # The same evidence a commit uses: the native pointer, or the raw
            # source identity when that is all the child carries.
            parent = (
                _resolve_session_id_by_provider_session_id(
                    connection,
                    provider=str(provider),
                    provider_session_id=str(pointer),
                    owner_id=owner_id,
                    machine_id=str(machine_id),
                )
                if pointer
                else _resolve_session_id_by_source_id(
                    connection,
                    provider=str(provider),
                    opaque_source_id=str(source_pointer),
                    owner_id=owner_id,
                    machine_id=str(machine_id),
                )
            )
            if parent is None or parent == str(child_id):
                continue
            bound += int(
                connection.execute(
                    update(session_table)
                    .where(
                        session_table.c.session_id == str(child_id),
                        session_table.c.subagent_parent_session_id.is_(None),
                    )
                    .values(subagent_parent_session_id=parent, updated_at=now)
                ).rowcount
                or 0
            )
        connection.execute(
            catalog_meta.update().where(catalog_meta.c.singleton == 1).values(subagent_parent_generation=SUBAGENT_PARENT_GENERATION)
        )
    return bound


def _active_session_ids(connection, *, limit: int, days_back: int, observed_at: datetime) -> list[str]:
    live = LiveSession.__table__
    catalog = LiveSessionCatalog.__table__
    cutoff = observed_at - timedelta(days=days_back)
    rows = connection.execute(
        select(live.c.session_id)
        .join(catalog, catalog.c.session_id == live.c.session_id)
        .where(
            live.c.state.notin_(("missing", "ended")),
            catalog.c.user_state.notin_(("archived", "snoozed")),
            catalog.c.user_hidden_from_timeline == 0,
            live.c.last_seen_at >= cutoff,
        )
        .order_by(live.c.last_seen_at.desc(), live.c.updated_at.desc(), live.c.session_id.desc())
        .limit(limit)
    ).all()
    return [str(row[0]) for row in rows]


def _runtime_activity_facts(
    connection,
    *,
    events: list[Any],
    updated_runtime_keys: set[str],
) -> list[ReducerFact]:
    """Promote accepted runtime phase events into the served fact reducer.

    Runtime events already update the authoritative ``LiveRuntimeState`` row.
    The served session contract intentionally ignores that row, so failing to
    reduce the same accepted event left short Console turns with zero canonical
    activity heads.  Bind every promoted event to its exact durable run before
    it can become served evidence.
    """

    from zerg.services.session_runtime import phase_freshness_ms

    run_table = LiveSessionRun.__table__
    thread_table = LiveSessionThread.__table__
    facts: list[ReducerFact] = []
    for event in events:
        if event.runtime_key not in updated_runtime_keys or event.kind != "phase_signal":
            continue
        if event.session_id is None or event.run_id is None:
            continue
        phase = str(event.phase or "").strip().lower()
        freshness_ms = event.freshness_ms if event.freshness_ms is not None else phase_freshness_ms(phase)
        if phase not in {"thinking", "running", "blocked", "stalled", "needs_user", "idle"} or freshness_ms is None:
            continue
        session_id = str(event.session_id)
        run_id = str(event.run_id)
        bound = connection.execute(
            select(run_table.c.id)
            .select_from(run_table.join(thread_table, thread_table.c.id == run_table.c.thread_id))
            .where(run_table.c.id == run_id, thread_table.c.session_id == session_id)
        ).scalar_one_or_none()
        if bound is None:
            continue
        occurred_at = _as_aware_utc(event.occurred_at)
        if occurred_at is None:
            continue
        raw_source = str(event.source or "runtime_event").strip() or "runtime_event"
        value = {
            "authority_class": "provider_runtime",
            "provider": str(event.provider),
            "session_id": session_id,
            "run_id": run_id,
            "kind": phase,
            "raw_kind": phase,
            "tool_name": str(event.tool_name) if event.tool_name else None,
            "source": raw_source,
            "observed_at": occurred_at.isoformat(),
            "valid_until": (occurred_at + timedelta(milliseconds=freshness_ms)).isoformat(),
        }
        dedupe_key = hashlib.sha256(f"runtime-activity:{raw_source}:{event.dedupe_key}:{run_id}".encode()).hexdigest()
        facts.append(
            ReducerFact(
                family="activity",
                subject_key=f"run:{run_id}",
                source=raw_source,
                source_epoch=run_id,
                source_seq=None,
                dedupe_key=dedupe_key,
                evidence_hash=canonical_evidence_hash(value),
                value=value,
                observed_at=occurred_at,
                session_id=session_id,
                valid_until=occurred_at + timedelta(milliseconds=freshness_ms),
                raw_locator=f"runtime:{raw_source}:{event.dedupe_key}"[:1024],
            )
        )
    return facts


#: How long one observed delegation snapshot may speak for the session. Its own
#: clock on purpose: activity expires in 90-600s, but a background agent
#: outlives turns, and a snapshot that vanished while the work continued is the
#: defect this axis exists to fix. The producer may narrow it per observation.
_DELEGATION_DEFAULT_FRESHNESS_MS = 30 * 60 * 1000
_DELEGATION_KIND_LIMIT = 8
_DELEGATION_COUNT_LIMIT = 256


#: Families bounded by SHADOW_STATE_FACT_HEAD_LIMIT. delegation_lifecycle is
#: read separately, only for subagents a delegation registry names: it keeps
#: one head per subagent ever started, so a long session outgrows any cap.
_STATE_HEAD_FAMILIES = ("activity", "control", "continuation", "delegation")


def _merge_registry_lifecycle_heads(connection, heads_by_session: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Append each session's registry-matched lifecycle heads in place."""
    for session_id, lifecycle in read_registry_lifecycle_heads(connection, heads_by_session=heads_by_session).items():
        heads_by_session.setdefault(session_id, []).extend(lifecycle)
    return heads_by_session


def _attach_delegation_children(connection, *, facts: list[dict[str, Any]], heads_by_session: Mapping[str, Any]) -> None:
    """Join named tasks to source-authored child lineage within this read snapshot."""
    requested: dict[str, set[str]] = {}
    native_requested: dict[tuple[str, str], str] = {}
    by_session = {str(fact["catalog"]["session_id"]): fact for fact in facts}
    for session_id, fact in by_session.items():
        run_id = (fact.get("latest_run") or {}).get("id")
        if not run_id or (fact.get("latest_run") or {}).get("ended_at") is not None:
            continue
        for head in heads_by_session.get(session_id, []):
            if head["family"] not in {"delegation", "delegation_lifecycle"}:
                continue
            value = json.loads(head["value_json"])
            if value.get("run_id") != str(run_id):
                continue
            items = (
                [*(value.get("items") or []), *(value.get("recent_items") or [])] if head["family"] == "delegation" else [value.get("item")]
            )
            for item in items or []:
                if not isinstance(item, Mapping):
                    continue
                tool_id = item.get("parent_tool_call_id")
                if item.get("kind") == "subagent" and isinstance(tool_id, str) and tool_id:
                    requested.setdefault(session_id, set()).add(tool_id)
                native_id = item.get("native_child_id")
                native_path = item.get("native_child_source_path")
                if item.get("kind") == "subagent" and isinstance(native_id, str) and native_id and isinstance(native_path, str):
                    for opaque_id in _opaque_source_ids_for_path(native_path):
                        native_requested[(session_id, opaque_id)] = f"native:{native_id}"
    _attach_native_delegation_children(connection, by_session=by_session, requested=native_requested)
    if not requested:
        return
    child = StorageSession.__table__
    parent = child.alias("delegation_parent")
    rendered = (child.c.render_state == "ready") & child.c.current_render_generation.is_not(None)
    tool_ids = {tool_id for values in requested.values() for tool_id in values}
    rows = connection.execute(
        select(
            child.c.subagent_parent_session_id,
            child.c.subagent_parent_tool_call_id,
            child.c.session_id,
            child.c.started_at,
            child.c.last_activity_at,
            case((rendered, child.c.user_messages), else_=None).label("user_messages"),
            case((rendered, child.c.assistant_messages), else_=None).label("assistant_messages"),
            case((rendered, child.c.tool_calls), else_=None).label("tool_calls"),
        )
        .select_from(child.join(parent, parent.c.session_id == child.c.subagent_parent_session_id))
        .where(
            child.c.subagent_parent_session_id.in_(requested),
            child.c.subagent_parent_tool_call_id.in_(tool_ids),
            child.c.owner_id == parent.c.owner_id,
            child.c.provider == parent.c.provider,
            child.c.machine_id == parent.c.machine_id,
            child.c.raw_state != "retired",
            child.c.render_state != "retired",
        )
    ).mappings()
    ambiguous: set[tuple[str, str]] = set()
    for row in rows:
        session_id, tool_id = str(row["subagent_parent_session_id"]), str(row["subagent_parent_tool_call_id"])
        if tool_id not in requested[session_id]:
            continue
        children = by_session[session_id].setdefault("delegation_children", {})
        coordinate = (session_id, tool_id)
        if tool_id in children or coordinate in ambiguous:
            children.pop(tool_id, None)
            ambiguous.add(coordinate)
            continue
        children[tool_id] = {
            "session_id": str(row["session_id"]),
            "started_at": _encode_datetime(row["started_at"]),
            "last_activity_at": _encode_datetime(row["last_activity_at"]),
            "user_messages": int(row["user_messages"]) if row["user_messages"] is not None else None,
            "assistant_messages": int(row["assistant_messages"]) if row["assistant_messages"] is not None else None,
            "tool_calls": int(row["tool_calls"]) if row["tool_calls"] is not None else None,
        }


def _attach_native_delegation_children(connection, *, by_session: Mapping[str, Any], requested: Mapping[tuple[str, str], str]) -> None:
    """Resolve exact OMP task artifacts, never reusable job handles."""
    if not requested:
        return
    child = StorageSession.__table__
    parent = child.alias("native_delegation_parent")
    raw = LiveRawObject.__table__
    rendered = (child.c.render_state == "ready") & child.c.current_render_generation.is_not(None)
    rows = connection.execute(
        select(
            child.c.subagent_parent_session_id,
            raw.c.opaque_source_id,
            child.c.session_id,
            child.c.started_at,
            child.c.last_activity_at,
            case((rendered, child.c.user_messages), else_=None).label("user_messages"),
            case((rendered, child.c.assistant_messages), else_=None).label("assistant_messages"),
            case((rendered, child.c.tool_calls), else_=None).label("tool_calls"),
        )
        .select_from(
            child.join(parent, parent.c.session_id == child.c.subagent_parent_session_id).join(raw, raw.c.session_id == child.c.session_id)
        )
        .where(
            child.c.subagent_parent_session_id.in_({key[0] for key in requested}),
            raw.c.opaque_source_id.in_({key[1] for key in requested}),
            child.c.owner_id == parent.c.owner_id,
            child.c.provider == parent.c.provider,
            child.c.provider == "omp",
            raw.c.provider == "omp",
            child.c.machine_id == parent.c.machine_id,
            child.c.is_subagent == 1,
            child.c.raw_state != "retired",
            child.c.render_state != "retired",
        )
        .distinct()
    ).mappings()
    ambiguous: set[tuple[str, str]] = set()
    for row in rows:
        session_id = str(row["subagent_parent_session_id"])
        key = requested.get((session_id, str(row["opaque_source_id"])))
        if key is None:
            continue
        children = by_session[session_id].setdefault("delegation_children", {})
        coordinate = (session_id, key)
        if coordinate in ambiguous:
            continue
        existing = children.get(key)
        if existing is not None:
            if existing["session_id"] != str(row["session_id"]):
                children.pop(key)
                ambiguous.add(coordinate)
            continue
        children[key] = {
            "session_id": str(row["session_id"]),
            "started_at": _encode_datetime(row["started_at"]),
            "last_activity_at": _encode_datetime(row["last_activity_at"]),
            "user_messages": int(row["user_messages"]) if row["user_messages"] is not None else None,
            "assistant_messages": int(row["assistant_messages"]) if row["assistant_messages"] is not None else None,
            "tool_calls": int(row["tool_calls"]) if row["tool_calls"] is not None else None,
        }


def _runtime_delegation_facts(connection, *, events: list[Any]) -> list[ReducerFact]:
    """Reduce registry observations independently of the parent's activity clock."""
    from zerg.services.session_state_contract import SessionDelegationProgress
    from zerg.services.session_state_contract import SessionDelegationTaskResponse

    run_table = LiveSessionRun.__table__
    thread_table = LiveSessionThread.__table__
    head_table = FactHead.__table__
    facts: list[ReducerFact] = []
    prior_by_run: dict[tuple[str, str], dict[str, Any]] = {}
    # One source observation may report a registry and an exact child edge.
    # Reduce both independently, with the edge following its own snapshot.
    for event, lane in (
        (event, lane)
        for event in sorted(events, key=lambda item: item.occurred_at)
        for lane in ("delegation", "delegation_update")
        if isinstance((event.payload or {}).get(lane), Mapping)
    ):
        if event.kind not in {"phase_signal", "delegation_signal"} or event.session_id is None or event.run_id is None:
            continue
        snapshot = (event.payload or {}).get("delegation") if lane == "delegation" else None
        update = (event.payload or {}).get("delegation_update") if lane == "delegation_update" else None
        session_id, run_id = str(event.session_id), str(event.run_id)
        bound = connection.execute(
            select(run_table.c.id)
            .select_from(run_table.join(thread_table, thread_table.c.id == run_table.c.thread_id))
            .where(run_table.c.id == run_id, thread_table.c.session_id == session_id)
        ).scalar_one_or_none()
        if bound is None:
            continue
        raw_observed = (snapshot if isinstance(snapshot, Mapping) else update).get("observed_at")
        try:
            occurred_at = _as_aware_utc(
                datetime.fromisoformat(raw_observed.replace("Z", "+00:00"))
                if isinstance(raw_observed, str)
                else raw_observed
                if isinstance(raw_observed, datetime)
                else event.occurred_at
            )
        except ValueError:
            continue
        if occurred_at is None or occurred_at > _as_aware_utc(event.occurred_at):
            continue
        raw_source = str(event.source or "runtime_event").strip() or "runtime_event"
        coordinate = (raw_source, run_id)
        if coordinate not in prior_by_run:
            previous = connection.execute(
                select(head_table.c.value_json).where(
                    head_table.c.family == "delegation",
                    head_table.c.subject_key == f"run:{run_id}",
                    head_table.c.source == raw_source,
                    head_table.c.source_epoch == run_id,
                )
            ).scalar_one_or_none()
            prior_by_run[coordinate] = json.loads(previous) if previous else {}
        prior = prior_by_run[coordinate]
        if not isinstance(snapshot, Mapping):
            if str(event.provider) != "claude":
                continue
            raw_item = update.get("item")
            operation = update.get("operation")
            if (
                update.get("membership") != "existing_exact_link_only"
                or operation not in {"observe", "remove"}
                or update.get("source_event") != ("SubagentStart" if operation == "observe" else "SubagentStop")
                or not isinstance(raw_item, Mapping)
                or not isinstance(raw_item.get("id"), str)
                or not 0 < len(raw_item["id"]) <= 256
                or not isinstance(raw_item.get("status"), str)
                or not 0 < len(raw_item["status"]) <= 32
                or raw_item.get("kind") != "subagent"
                or raw_item.get("id") != update.get("source_agent_id")
            ):
                continue
            value = {
                "authority_class": "provider_runtime",
                "provider": str(event.provider),
                "session_id": session_id,
                "run_id": run_id,
                "source": raw_source,
                "observed_at": occurred_at.isoformat(),
                "operation": operation,
                "item": dict(raw_item),
                "source_event": update["source_event"],
            }
            if len(canonical_value_json(value).encode()) > MAX_VALUE_JSON_BYTES:
                continue
            dedupe_key = hashlib.sha256(f"runtime-delegation-edge:{raw_source}:{event.dedupe_key}:{run_id}".encode()).hexdigest()
            facts.append(
                ReducerFact(
                    family="delegation_lifecycle",
                    subject_key=f"run:{run_id}:agent:{raw_item['id']}",
                    source=raw_source,
                    source_epoch=run_id,
                    source_seq=None,
                    dedupe_key=dedupe_key,
                    evidence_hash=canonical_evidence_hash(value),
                    value=value,
                    observed_at=occurred_at,
                    session_id=session_id,
                    raw_locator=f"runtime:{raw_source}:{event.dedupe_key}"[:1024],
                )
            )
            continue
        previous_items = {(item.get("kind"), item.get("id")): item for item in prior.get("items", []) or [] if isinstance(item, dict)}
        kinds: dict[str, int] = {}
        items: list[dict[str, Any]] | None = None
        raw_items = snapshot.get("items")
        if isinstance(raw_items, list):
            # Reject an oversized or malformed registry rather than manufacture
            # an authoritative empty observation from an invalid producer.
            if len(raw_items) > _DELEGATION_COUNT_LIMIT:
                continue
            items = []
            seen: set[str] = set()
            invalid = False
            for raw in raw_items:
                if not isinstance(raw, Mapping):
                    invalid = True
                    break
                task_id, kind, task_status = raw.get("id"), raw.get("kind"), raw.get("status")
                if (
                    not isinstance(task_id, str)
                    or not task_id.strip()
                    or len(task_id) > 256
                    or not isinstance(kind, str)
                    or not kind.strip()
                    or len(kind) > 32
                    or not isinstance(task_status, str)
                    or not task_status.strip()
                    or len(task_status) > 32
                ):
                    invalid = True
                    break
                if task_id in seen:
                    continue
                seen.add(task_id)
                previous_item = previous_items.get((kind, task_id), {})
                registered_at = raw.get("registered_at")
                if registered_at != previous_item.get("registered_at") and registered_at is not None:
                    previous_item = {}
                item = {
                    "id": task_id,
                    "kind": kind,
                    "status": task_status,
                    "description": raw["description"][:512] if isinstance(raw.get("description"), str) else None,
                    "first_observed_at": previous_item.get("first_observed_at") or occurred_at.isoformat(),
                }
                tool_call_id = raw.get("parent_tool_call_id")
                if isinstance(tool_call_id, str) and 0 < len(tool_call_id) <= 256:
                    item["parent_tool_call_id"] = tool_call_id
                native_child_id = raw.get("native_child_id")
                if isinstance(native_child_id, str) and 0 < len(native_child_id) <= 256:
                    item["native_child_id"] = native_child_id
                native_child_source_path = raw.get("native_child_source_path")
                if isinstance(native_child_source_path, str) and 0 < len(native_child_source_path) <= 2048:
                    item["native_child_source_path"] = native_child_source_path
                if isinstance(registered_at, str):
                    item["registered_at"] = registered_at
                progress = raw.get("native_progress")
                if isinstance(progress, Mapping):
                    try:
                        item["native_progress"] = SessionDelegationProgress.model_validate(progress).model_dump(
                            mode="json", exclude_none=True
                        )
                    except ValueError:
                        invalid = True
                        break
                elif previous_item.get("native_progress") is not None:
                    item["native_progress"] = previous_item["native_progress"]
                items.append(item)
                kinds[kind] = kinds.get(kind, 0) + 1
            if invalid:
                continue
            count = len(items)
        else:
            raw_kinds = snapshot.get("kinds")
            if isinstance(raw_kinds, Mapping):
                for kind, amount in list(raw_kinds.items())[:_DELEGATION_KIND_LIMIT]:
                    if isinstance(kind, str) and kind and type(amount) is int and 0 < amount <= _DELEGATION_COUNT_LIMIT:
                        kinds[kind] = amount
            raw_count = snapshot.get("count")
            count = raw_count if type(raw_count) is int and 0 <= raw_count <= _DELEGATION_COUNT_LIMIT else sum(kinds.values())
        recent_items = None
        raw_recent = snapshot.get("recent_items")
        if isinstance(raw_recent, list) and len(raw_recent) <= _DELEGATION_COUNT_LIMIT:
            recent_items = []
            for raw in raw_recent:
                if not isinstance(raw, Mapping) or raw.get("status") not in {"completed", "failed", "cancelled", "aborted"}:
                    continue
                if any(
                    not isinstance(raw.get(key), str) or not raw[key].strip() or len(raw[key]) > bound
                    for key, bound in (("id", 256), ("kind", 32))
                ):
                    continue
                fields = {
                    key: raw[key]
                    for key in ("id", "kind", "status", "description", "registered_at", "ended_at", "native_progress")
                    if key in raw
                }
                try:
                    terminal = SessionDelegationTaskResponse.model_validate(fields).model_dump(mode="json", exclude_none=True)
                except ValueError:
                    continue
                terminal["first_observed_at"] = occurred_at.isoformat()
                for key, bound in (("parent_tool_call_id", 256), ("native_child_id", 256), ("native_child_source_path", 2048)):
                    if isinstance(raw.get(key), str) and 0 < len(raw[key]) <= bound:
                        terminal[key] = raw[key]
                recent_items.append(terminal)
            if raw_recent and not recent_items:
                recent_items = None
        raw_freshness = snapshot.get("freshness_ms")
        freshness_ms = (
            raw_freshness
            if type(raw_freshness) is int and 0 < raw_freshness <= _DELEGATION_DEFAULT_FRESHNESS_MS
            else _DELEGATION_DEFAULT_FRESHNESS_MS
        )
        valid_until = occurred_at + timedelta(milliseconds=freshness_ms)
        value = {
            "authority_class": "provider_runtime",
            "provider": str(event.provider),
            "session_id": session_id,
            "run_id": run_id,
            "count": count,
            "kinds": kinds,
            "items": items,
            "recent_items": recent_items,
            "source": raw_source,
            "observed_at": occurred_at.isoformat(),
            "valid_until": valid_until.isoformat(),
        }
        # An invalid registry must not poison the enclosing parent activity
        # batch or renew the previous observation. Never truncate it to fit.
        if len(canonical_value_json(value).encode()) > MAX_DELEGATION_VALUE_JSON_BYTES:
            continue
        if not prior.get("observed_at") or occurred_at >= datetime.fromisoformat(prior["observed_at"].replace("Z", "+00:00")):
            prior_by_run[coordinate] = value
        dedupe_key = hashlib.sha256(f"runtime-delegation:{raw_source}:{event.dedupe_key}:{run_id}".encode()).hexdigest()
        facts.append(
            ReducerFact(
                family="delegation",
                subject_key=f"run:{run_id}",
                source=raw_source,
                source_epoch=run_id,
                source_seq=None,
                dedupe_key=dedupe_key,
                evidence_hash=canonical_evidence_hash(value),
                value=value,
                observed_at=occurred_at,
                session_id=session_id,
                valid_until=valid_until,
                raw_locator=f"runtime:{raw_source}:{event.dedupe_key}"[:1024],
            )
        )
    return facts


# The owner an auth-disabled (trial) instance creates; zerg.services.single_tenant.OSS_DEFAULT_EMAIL.
_OSS_TRIAL_OWNER_EMAIL = "local@zerg"


def _is_oss_trial_owner(row) -> bool:
    return row["provider"] == "local" and str(row["email"]).casefold() == _OSS_TRIAL_OWNER_EMAIL


def _user_dto(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "provider": row["provider"],
        "provider_user_id": row["provider_user_id"],
        "cp_user_id": row["cp_user_id"],
        "email_verified": bool(row["email_verified"]),
        "is_active": bool(row["is_active"]),
        "role": str(row["role"]),
        "display_name": row["display_name"],
        "avatar_url": row["avatar_url"],
        "prefs": row["prefs"],
        "context": row["context"] or {},
        "last_login": _encode_datetime(row["last_login"]),
        "created_at": _encode_datetime(row["created_at"]),
        "updated_at": _encode_datetime(row["updated_at"]),
    }


@contextmanager
def _read_snapshot(engine: Engine):
    """Open a real SQLite read transaction under pysqlite legacy mode."""

    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN")
        try:
            yield connection
        finally:
            connection.rollback()


def _apply_shadow_reducer(
    connection,
    *,
    heartbeat: dict[str, Any],
    machine_evidence: dict[str, Any] | None,
    received_at: datetime,
    commit_seq: int,
) -> dict[str, Any]:
    """Reduce retained schema-v3 evidence without affecting legacy heartbeat writes."""

    timer = _StageTimer("shadow_reducer")
    try:
        with connection.begin_nested():
            evidence_status, facts = _shadow_facts_from_heartbeat(
                machine_evidence=machine_evidence,
            )
            if evidence_status != "ready":
                return {"status": evidence_status}
            timer.mark("extract_facts")
            reduced = reduce_fact_batch_setwise(
                connection,
                facts,
                received_at=received_at,
                commit_seq_override=commit_seq,
            )
            timer.mark("reduce")
            # The reducer was one of three per-fact loops in this transaction.
            # Timing the other two separately answers whether they matter at
            # production fact mixes, rather than assuming they do.
            identity_binding = _bind_control_evidence_identities(connection, facts, device_id=str(heartbeat["device_id"]))
            timer.mark("bind_identities")
            run_terminal = _apply_exact_run_terminal_evidence(connection, facts)
            timer.mark("run_terminal")
            timer.log_if_slow()
            return {
                "status": "applied",
                "changed_heads": reduced.changed_heads,
                "duplicates": reduced.duplicates,
                "stale": reduced.stale,
                "conflicts": reduced.conflicts,
                "identity_binding": identity_binding,
                "run_terminal": run_terminal,
            }
    except (DBAPIError, SQLAlchemyError):
        # A database failure here must abort the whole observation, not just its
        # savepoint. This used to swallow the error and let the heartbeat, the
        # lease reconciliation and a failure receipt commit without it -- which
        # was defensible while fact heads were diagnostic. They are served now
        # (`live_catalog_timeline` raises without them), so committing the
        # heartbeat while its reduction rolled back leaves served state diverged
        # from the evidence that produced it, silently and durably.
        raise
    except (RecursionError, TypeError, ValueError):
        # Malformed external evidence is different in kind: the machine sent
        # something unusable, the database is fine, and refusing the whole
        # heartbeat would let one bad payload block a machine's liveness. Reject
        # the evidence, keep the heartbeat.
        return {"status": "failed", "reason": "invalid_evidence"}


def _apply_exact_run_terminal_evidence(connection, facts) -> dict[str, int]:
    """End only the exact durable run named by validated process-exit evidence."""

    run_table = LiveSessionRun.__table__
    thread_table = LiveSessionThread.__table__
    connection_table = LiveSessionConnection.__table__
    counts = {"ended": 0, "already_ended": 0, "unbound": 0}
    for fact in facts:
        if fact.family != "run":
            continue
        value = fact.value
        run_id = str(value.get("run_id") or "")
        session_id = str(value.get("session_id") or "")
        ended_at = fact.observed_at
        if not run_id or not session_id or ended_at is None:
            counts["unbound"] += 1
            continue
        bound = (
            connection.execute(
                select(run_table.c.ended_at, run_table.c.started_at)
                .select_from(run_table.join(thread_table, thread_table.c.id == run_table.c.thread_id))
                .where(run_table.c.id == run_id, thread_table.c.session_id == session_id)
                .limit(1)
            )
            .mappings()
            .first()
        )
        if bound is None:
            counts["unbound"] += 1
            continue
        if bound["ended_at"] is not None:
            counts["already_ended"] += 1
            continue
        started_at = _as_aware_utc(bound["started_at"])
        terminal_at = max(ended_at, started_at) if started_at is not None else ended_at
        updated = connection.execute(
            update(run_table)
            .where(run_table.c.id == run_id, run_table.c.ended_at.is_(None))
            .values(ended_at=terminal_at, exit_status="process_gone")
        ).rowcount
        if updated != 1:
            counts["already_ended"] += 1
            continue
        connection.execute(
            update(connection_table)
            .where(
                connection_table.c.run_id == run_id,
                connection_table.c.state.in_(("attached", "degraded", "detached")),
            )
            .values(
                state="ended",
                released_at=terminal_at,
                last_health_at=terminal_at,
                can_send_input=0,
                can_interrupt=0,
                can_terminate=0,
                can_tail_output=0,
                can_resume=0,
            )
        )
        counts["ended"] += 1
    return counts


def _bind_control_evidence_identities(connection, facts, *, device_id: str) -> dict[str, int]:
    """Bind adapter UUIDs to an exact live catalog connection and generation.

    A control coordinate is usable only while its exact durable run is open and
    its exact connection has not been released. This is the fencing invariant:
    late evidence may remain auditable in the reducer, but it must never bind a
    released coordinate or replace authority belonging to a newer run.
    """

    from zerg.services.managed_provider_contracts import contract_for_provider

    connection_table = LiveSessionConnection.__table__
    run_table = LiveSessionRun.__table__
    thread_table = LiveSessionThread.__table__
    counts = {"bound": 0, "matched": 0, "unbound": 0, "mismatched": 0}
    incoming_generations: dict[tuple[str, str, str, str], set[tuple[str, str]]] = {}
    for fact in facts:
        if fact.family != "control":
            continue
        value = fact.value
        session_id = str(value.get("session_id") or "").strip()
        run_id = str(value.get("run_id") or "").strip()
        provider = str(value.get("provider") or "").strip().lower()
        connection_id = str(value.get("connection_id") or "").strip()
        generation = str(value.get("lease_generation") or "").strip()
        if session_id and run_id and provider and connection_id and generation:
            contract = contract_for_provider(provider)
            if contract is not None:
                incoming_generations.setdefault(
                    (session_id, run_id, provider, contract.control_plane),
                    set(),
                ).add((connection_id, generation))
    for fact in facts:
        if fact.family != "control":
            continue
        value = fact.value
        try:
            adapter_connection_id = str(UUID(str(value.get("connection_id"))))
            lease_generation = str(UUID(str(value.get("lease_generation"))))
            run_id = str(UUID(str(value.get("run_id"))))
            session_id = str(UUID(str(value.get("session_id"))))
        except (TypeError, ValueError):
            counts["unbound"] += 1
            continue
        contract = contract_for_provider(str(value.get("provider") or "").strip().lower())
        if contract is None:
            counts["unbound"] += 1
            continue
        row = (
            connection.execute(
                select(
                    connection_table.c.id,
                    run_table.c.provider,
                    connection_table.c.released_at,
                    connection_table.c.state,
                    run_table.c.ended_at,
                    connection_table.c.adapter_connection_id,
                    connection_table.c.lease_generation,
                )
                .select_from(
                    connection_table.join(run_table, run_table.c.id == connection_table.c.run_id).join(
                        thread_table, thread_table.c.id == run_table.c.thread_id
                    )
                )
                .where(
                    connection_table.c.run_id == run_id,
                    connection_table.c.control_plane == contract.control_plane,
                    connection_table.c.device_id == device_id,
                    thread_table.c.session_id == session_id,
                )
                .limit(1)
            )
            .mappings()
            .first()
        )
        if row is None:
            counts["unbound"] += 1
            continue
        if str(row["provider"] or "").strip().lower() != str(value.get("provider") or "").strip().lower():
            counts["mismatched"] += 1
            continue
        # Binding is an authority transition, not historical bookkeeping.
        # Once either side of the exact coordinate is terminal, late evidence
        # must not populate it (or make it eligible to compete with a newer
        # run). Keep the fact itself for audit/reducer diagnostics; only the
        # durable identity binding is fenced here.
        if (
            row["ended_at"] is not None
            or row["released_at"] is not None
            or str(row["state"] or "").strip().lower() in {"released", "ended"}
        ):
            counts["mismatched"] += 1
            continue
        current_adapter = str(row["adapter_connection_id"] or "")
        current_generation = str(row["lease_generation"] or "")
        if current_adapter == adapter_connection_id and current_generation == lease_generation:
            counts["matched"] += 1
            continue
        provider_name = str(value.get("provider") or "").strip().lower()
        safe_rebind = (provider_name == "pi" and current_adapter != adapter_connection_id and current_generation != lease_generation) or (
            provider_name == "omp" and current_generation != lease_generation
        )
        if (current_adapter or current_generation) and not safe_rebind:
            counts["mismatched"] += 1
            continue
        target = (str(session_id), str(run_id), str(value.get("provider") or "").strip().lower(), contract.control_plane)
        if len(incoming_generations.get(target, ())) != 1:
            counts["mismatched"] += 1
            continue
        if current_adapter or current_generation:
            if not current_adapter or not current_generation:
                counts["mismatched"] += 1
                continue
            old_subject = f"connection:{current_adapter}:{current_generation}"
            old_observations = list(
                connection.execute(
                    select(FactHead.observed_at).where(
                        FactHead.family == "control",
                        FactHead.subject_key == old_subject,
                        FactHead.session_id == session_id,
                    )
                ).scalars()
            )
            old_observed_at = max(
                (_as_aware_utc(value) for value in old_observations if _as_aware_utc(value) is not None),
                default=None,
            )
            incoming_observed_at = _as_aware_utc(fact.observed_at)
            if old_observed_at is None or incoming_observed_at is None or incoming_observed_at <= old_observed_at:
                counts["mismatched"] += 1
                continue
        collision = connection.execute(
            select(connection_table.c.id)
            .where(
                connection_table.c.id != row["id"],
                or_(
                    connection_table.c.adapter_connection_id == adapter_connection_id,
                    connection_table.c.lease_generation == lease_generation,
                ),
            )
            .limit(1)
        ).scalar_one_or_none()
        if collision is not None:
            counts["mismatched"] += 1
            continue
        updated = connection.execute(
            update(connection_table)
            .where(
                connection_table.c.id == row["id"],
                connection_table.c.adapter_connection_id == row["adapter_connection_id"],
                connection_table.c.lease_generation == row["lease_generation"],
            )
            .values(
                adapter_connection_id=adapter_connection_id,
                lease_generation=lease_generation,
            )
        ).rowcount
        if updated == 1:
            counts["bound"] += 1
        else:
            counts["mismatched"] += 1
    return counts


def _apply_shadow_parity(
    connection,
    *,
    heartbeat: dict[str, Any],
    machine_evidence: dict[str, Any] | None,
    managed_leases_present: bool,
    received_at: datetime,
    commit_seq: int,
    known_delta_count: int | None,
) -> tuple[dict[str, Any], int | None]:
    """Persist bounded candidate-level deltas without creating served state."""

    original_delta_count = known_delta_count
    if os.getenv(_SHADOW_PARITY_ENV, "").strip().lower() not in _TRUTHY_ENV:
        return {"status": "disabled"}, known_delta_count
    if not managed_leases_present:
        return {"status": "legacy_unavailable"}, known_delta_count
    try:
        with connection.begin_nested():
            evidence_status, facts = _shadow_facts_from_heartbeat(
                machine_evidence=machine_evidence,
            )
            if evidence_status != "ready":
                return {"status": evidence_status}, known_delta_count
            candidates = {
                (fact.family, fact.subject_key, fact.source, fact.source_epoch): fact for fact in facts if fact.family == "control"
            }
            unsupported_families = [
                {"family": family, "reason": "canonical_projector_unavailable"}
                for family in sorted({fact.family for fact in facts if fact.family != "control"})
            ]
            # A heartbeat already bounds its facts to MAX_REDUCER_FACTS. Read
            # their heads and legacy leases set-wise instead of holding the
            # single writer through two SELECTs per control candidate.
            heads = {}
            if candidates:
                heads = {
                    (row["family"], row["subject_key"], row["source"], row["source_epoch"]): row
                    for row in connection.execute(
                        select(FactHead.__table__).where(
                            FactHead.family == "control",
                            tuple_(FactHead.subject_key, FactHead.source, FactHead.source_epoch).in_([key[1:] for key in candidates]),
                        )
                    ).mappings()
                }
            shadow_values = {}
            legacy_keys = set()
            for key, head in heads.items():
                value = json.loads(str(head["value_json"]))
                if not isinstance(value, dict):
                    raise ValueError("shadow fact head value must be an object")
                shadow_values[key] = value
                legacy_keys.add((str(value.get("session_id") or ""), str(value.get("provider") or "").strip().lower()))
            legacy_rows = {}
            if legacy_keys:
                for row in connection.execute(
                    select(LiveControlLease.__table__).where(
                        LiveControlLease.device_id == str(heartbeat["device_id"]).strip(),
                        tuple_(LiveControlLease.session_id, LiveControlLease.provider).in_(legacy_keys),
                    )
                ).mappings():
                    legacy_rows.setdefault((row["session_id"], row["provider"]), row)
            compared_axes = deltas = missing_heads = 0
            for fact in (candidates[key] for key in sorted(candidates)):
                key = (fact.family, fact.subject_key, fact.source, fact.source_epoch)
                head = heads.get(key)
                if head is None:
                    missing_heads += 1
                    continue
                shadow_value = shadow_values[key]
                session_id = str(shadow_value.get("session_id") or "")
                provider = str(shadow_value.get("provider") or "").strip().lower()
                legacy = legacy_rows.get((session_id, provider))
                if legacy is None:
                    compared_axes += 1
                    deltas += _record_shadow_parity_delta(
                        connection,
                        fact=fact,
                        head_hash=str(head["evidence_hash"]),
                        axis="managed_lease",
                        legacy_value=None,
                        reason="legacy_missing",
                        received_at=received_at,
                        commit_seq=commit_seq,
                    )
                    continue
                legacy_payload = json.loads(str(legacy["payload_json"] or "{}"))
                if not isinstance(legacy_payload, dict):
                    raise ValueError("legacy control lease payload must be an object")
                axes = (
                    ("state", legacy["state"], "state"),
                    ("bridge_status", legacy_payload.get("bridge_status"), "bridge_status"),
                    (
                        "thread_subscription_status",
                        legacy_payload.get("thread_subscription_status"),
                        "thread_subscription_status",
                    ),
                )
                for legacy_axis, legacy_value, shadow_axis in axes:
                    compared_axes += 1
                    legacy_value = _normalized_parity_axis(legacy_axis, legacy_value)
                    shadow_axis_value = _normalized_parity_axis(legacy_axis, shadow_value.get(shadow_axis))
                    if legacy_value == shadow_axis_value:
                        continue
                    deltas += _record_shadow_parity_delta(
                        connection,
                        fact=fact,
                        head_hash=str(head["evidence_hash"]),
                        axis=legacy_axis,
                        legacy_value=legacy_value,
                        reason="value_mismatch",
                        received_at=received_at,
                        commit_seq=commit_seq,
                    )
            if deltas:
                if known_delta_count is None:
                    known_delta_count = int(connection.execute(select(func.count()).select_from(FactParityDelta.__table__)).scalar_one())
                else:
                    known_delta_count += deltas
                if known_delta_count > _MAX_PARITY_DELTAS:
                    known_delta_count = _prune_shadow_parity_deltas(connection, known_delta_count=known_delta_count)
            return (
                {
                    "status": "compared",
                    "compared_axes": compared_axes,
                    "deltas": deltas,
                    "missing_heads": missing_heads,
                    "unsupported_families": unsupported_families,
                },
                known_delta_count,
            )
    except DBAPIError as exc:
        if exc.connection_invalidated or connection.invalidated:
            raise
        return {"status": "failed", "reason": "database_error"}, original_delta_count
    except SQLAlchemyError:
        if connection.invalidated:
            raise
        return {"status": "failed", "reason": "database_error"}, original_delta_count
    except (RecursionError, TypeError, ValueError):
        return {"status": "failed", "reason": "invalid_evidence"}, original_delta_count


def _shadow_facts_from_heartbeat(
    *,
    machine_evidence: dict[str, Any] | None,
):
    """Facts for this heartbeat's schema-v3 evidence, or why there are none.

    Evidence arrives as its own argument. It used to ride the stamp's
    ``raw_json``, which is a size-capped forensic copy of the payload, so a
    machine whose evidence grew past the cap had every heartbeat refused --
    liveness died with the evidence it carried.
    """

    evidence = machine_evidence
    if not isinstance(evidence, dict):
        return "no_evidence", []
    # The Runtime Host drops oversize evidence before it reaches the wire; this
    # repeats the bound for anyone calling the RPC directly, and reports it as
    # evidence state rather than refusing the heartbeat.
    if machine_evidence_bytes(evidence) > MAX_MACHINE_EVIDENCE_BYTES:
        return "oversize_evidence", []
    if evidence.get("schema_version") != 3:
        return "unsupported_schema", []
    facts = reducer_facts_from_machine_evidence(evidence)
    if len(facts) > MAX_REDUCER_FACTS:
        raise ValueError(f"shadow reducer batch exceeds {MAX_REDUCER_FACTS} facts")
    return "ready", facts


def _record_shadow_parity_delta(
    connection,
    *,
    fact,
    head_hash: str,
    axis: str,
    legacy_value: object,
    reason: str,
    received_at: datetime,
    commit_seq: int,
) -> int:
    legacy_fingerprint = hashlib.sha256(
        json.dumps(
            {"axis": axis, "value": legacy_value},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    delta_key = hashlib.sha256(
        "\0".join(
            (
                fact.family,
                fact.subject_key,
                fact.source,
                fact.source_epoch,
                axis,
                reason,
                legacy_fingerprint,
                head_hash,
            )
        ).encode()
    ).hexdigest()
    table = FactParityDelta.__table__
    inserted = connection.execute(
        sqlite_insert(table)
        .values(
            delta_key=delta_key,
            family=fact.family,
            subject_key=fact.subject_key,
            source=fact.source,
            source_epoch=fact.source_epoch,
            axis=axis,
            reason=reason,
            legacy_fingerprint=legacy_fingerprint,
            shadow_head_hash=head_hash,
            detected_at=received_at,
            commit_seq=commit_seq,
        )
        .on_conflict_do_nothing(index_elements=[table.c.delta_key])
        .returning(table.c.delta_key)
    ).scalar_one_or_none()
    return 1 if inserted is not None else 0


def _prune_shadow_parity_deltas(connection, *, known_delta_count: int) -> int:
    table = FactParityDelta.__table__
    if known_delta_count <= _MAX_PARITY_DELTAS:
        return known_delta_count
    keep = select(table.c.delta_key).order_by(table.c.commit_seq.desc(), table.c.delta_key).limit(_MAX_PARITY_DELTAS)
    connection.execute(delete(table).where(table.c.delta_key.not_in(keep)))
    return _MAX_PARITY_DELTAS


def _normalized_parity_axis(axis: str, value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    return normalized.lower() if axis == "state" else normalized


class _StageTimer:
    """Attribute a slow write to a stage rather than to the whole function.

    Deliberately dumb: a dict of elapsed milliseconds and one log line. It exists
    because the alternative was reasoning about which of six plausible stages
    costs a second, and reasoning is what produced two wrong hypotheses before
    this one.
    """

    __slots__ = ("_label", "_stages", "_started", "_last", "_dimensions")

    def __init__(self, label: str) -> None:
        self._label = label
        self._stages: dict[str, float] = {}
        self._started = time.perf_counter()
        self._last = self._started
        self._dimensions: dict[str, int | str] = {}

    def annotate(self, **dimensions: int | str) -> None:
        self._dimensions.update(dimensions)

    def mark(self, name: str) -> None:
        """Close the stage that ends here.

        A mark rather than a wrapping block: the stages are long multi-line
        statements, and re-indenting them to instrument them would make the diff
        about formatting instead of about measurement.
        """

        now = time.perf_counter()
        self._stages[name] = self._stages.get(name, 0.0) + (now - self._last) * 1000.0
        self._last = now

    def log_if_slow(self) -> None:
        total_ms = (time.perf_counter() - self._started) * 1000.0
        if total_ms < _STAGE_TIMER_SLOW_MS:
            return
        # Unmeasured time is the transaction's own commit plus anything not
        # wrapped. That is a real answer, so name it rather than let it hide in
        # the gap between the stages and the total.
        measured = sum(self._stages.values())
        breakdown = " ".join(f"{name}={value:.0f}ms" for name, value in sorted(self._stages.items(), key=lambda item: -item[1]))
        dimensions = " ".join(f"{name}={value}" for name, value in sorted(self._dimensions.items()))
        logging.getLogger(__name__).warning(
            "%s took %.0fms: %s unmeasured=%.0fms %s",
            self._label,
            total_ms,
            breakdown,
            max(0.0, total_ms - measured),
            dimensions,
        )


@contextmanager
def _write_transaction(engine: Engine, *, timer: _StageTimer | None = None):
    """Acquire SQLite's write reservation before mutation read-checks."""

    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        if timer is not None:
            timer.mark("begin")
        try:
            yield connection
            connection.commit()
            if timer is not None:
                timer.mark("commit")
        except BaseException:
            connection.rollback()
            raise
        finally:
            if timer is not None:
                timer.log_if_slow()


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _encode_datetime(value: datetime | None) -> str | None:
    normalized = _as_aware_utc(value)
    return normalized.isoformat() if normalized is not None else None


# A repair statement names at most this many sessions, well under SQLite's
# bound-parameter limit, so a whole-corpus backfill (a bumped embedding
# identity) still runs as primary-key probes instead of one oversized IN.
_PROJECTOR_REPAIR_BATCH = 500


@dataclass(frozen=True)
class ProjectorRepairPlan:
    """Sessions a read-snapshot scan found needing projector-ledger repair."""

    eligible_sessions: int
    irrelevant_semantic: tuple[str, ...]
    missing_semantic: tuple[str, ...]
    missing_search: tuple[str, ...]
    missing_embeddings: tuple[str, ...]
    stale_render: tuple[str, ...]
    misaligned_embeddings: tuple[str, ...]
    retired: tuple[str, ...]

    def is_empty(self) -> bool:
        return not (
            self.irrelevant_semantic
            or self.missing_semantic
            or self.missing_search
            or self.missing_embeddings
            or self.stale_render
            or self.misaligned_embeddings
            or self.retired
        )


def _projector_repair_predicates() -> SimpleNamespace:
    """The predicates shared by the repair scan and the write that re-checks it."""

    sessions = StorageSession.__table__
    states = ProjectorState.__table__
    eligible = (
        sessions.c.user_state != "deleted",
        sessions.c.current_render_generation.is_not(None),
        sessions.c.render_state == "ready",
    )
    # search-v2 reads render objects frozen at its claimed revision. A
    # re-render that committed a newer current generation without raising
    # this target left the snapshot empty: the projector published zero
    # objects and recorded the session complete. On the 2026-09-24 owner
    # catalog 15,442 of 40,222 sessions were behind their generation, served
    # only from a search.db built before the re-render, and a rebuild dropped
    # them silently. Raise the target to the newest revision of the session's
    # current render; embeddings follow via the alignment below.
    # The render objects themselves were rewritten too, past both the session
    # and generation revisions, so the target is the newest of the three: the
    # first fix (session revision) still froze one 2,373-object session at 168
    # visible objects.
    render_objects = RenderObject.__table__
    current_generation_revision = func.max(
        sessions.c.commit_seq,
        func.coalesce(
            select(RenderGeneration.__table__.c.commit_seq)
            .where(RenderGeneration.__table__.c.generation_id == sessions.c.current_render_generation)
            .scalar_subquery(),
            0,
        ),
        func.coalesce(
            select(func.max(render_objects.c.commit_seq))
            .where(render_objects.c.generation_id == sessions.c.current_render_generation)
            .scalar_subquery(),
            0,
        ),
    )
    session_revision = (
        select(current_generation_revision)
        .where(
            sessions.c.session_id == states.c.session_id,
            *eligible,
            current_generation_revision > states.c.desired_revision,
        )
        .scalar_subquery()
    )
    search_alignment = states.alias("search_alignment")
    search_revision = (
        select(search_alignment.c.desired_revision)
        .where(
            search_alignment.c.projector == "search-v2",
            search_alignment.c.session_id == states.c.session_id,
        )
        .scalar_subquery()
    )
    retired_revision = (
        select(sessions.c.commit_seq)
        .where(sessions.c.session_id == states.c.session_id, sessions.c.render_state == "retired")
        .scalar_subquery()
    )
    search_rows = states.alias("embedding_seed_search")
    return SimpleNamespace(
        sessions=sessions,
        states=states,
        search_rows=search_rows,
        eligible=eligible,
        irrelevant_semantic=(
            states.c.projector == SEMANTIC_PROJECTOR_ID,
            states.c.session_id.in_(select(sessions.c.session_id).where(func.lower(sessions.c.provider) != "claude")),
        ),
        semantic_candidates=(
            *eligible,
            func.lower(sessions.c.provider) == "claude",
            sessions.c.semantic_projection_version < 1,
        ),
        embedding_seed=(search_rows.c.projector == "search-v2",),
        session_revision=session_revision,
        stale_render=(states.c.projector == "search-v2", session_revision.is_not(None)),
        search_revision=search_revision,
        alignment=(
            states.c.projector == EMBEDDING_PROJECTOR_ID,
            search_revision.is_not(None),
            states.c.desired_revision != search_revision,
        ),
        retired_revision=retired_revision,
        retired=(
            states.c.projector.in_(KNOWN_PROJECTORS),
            retired_revision.is_not(None),
            states.c.desired_revision < retired_revision,
        ),
    )


__all__ = ["CatalogStore", "DEVICE_TOKEN_LIMIT_PER_OWNER"]


# Every commit with a native id reads the newest delegation.spawn facts in its
# owner/machine/provider scope. Walk ix_session_provider_facts_kind_at newest
# first and check each fact's scope by primary key, so the read stops at the
# limit. As a join the planner started from every session of the provider and
# sorted all their spawn facts (22.7 ms for claude on david010 with 290 spawn
# facts, ~300 ms with ~100k); the index's trailing columns are the full ORDER BY.
_NEWEST_SCOPED_SPAWN_FACTS = text(
    """
    SELECT f.payload_json, f.source_position, f.at, f.session_id
    FROM session_provider_facts AS f INDEXED BY ix_session_provider_facts_kind_at
    WHERE f.kind = 'delegation.spawn'
      AND f.session_id != :session_id
      AND EXISTS (
        SELECT 1 FROM sessions AS s
        WHERE s.session_id = f.session_id
          AND s.owner_id = :owner_id AND s.machine_id = :machine_id AND s.provider = :provider
      )
    ORDER BY f.at DESC, f.source_position DESC, f.id DESC
    LIMIT :limit
    """
)

_ORPHAN_SCOPE = """
      AND provider = :provider AND owner_id = :owner_id AND machine_id = :machine_id
      AND subagent_parent_session_id IS NULL AND session_id != :session_key
"""

_ORPHANS_BY_PARENT_SOURCE = text(
    "SELECT session_id, subagent_parent_provider_session_id, subagent_parent_source_id "
    "FROM sessions INDEXED BY ix_sessions_subagent_parent_source_id "
    "WHERE subagent_parent_source_id = :parent_source_id" + _ORPHAN_SCOPE
)

_ORPHANS_BY_PARENT_NATIVE_ID = text(
    "SELECT session_id, subagent_parent_provider_session_id, subagent_parent_source_id "
    "FROM sessions INDEXED BY ix_sessions_subagent_parent_provider_session_id "
    "WHERE subagent_parent_provider_session_id IN :alias_values "
    "AND subagent_parent_source_id IS NULL" + _ORPHAN_SCOPE
).bindparams(bindparam("alias_values", expanding=True))


# Every claim poll looks its token up. The production catalog's planner
# statistics predate most of its rows and say all 196 rows share one
# claim_token value, so SQLite scanned the projector (28 ms on the frozen
# owner catalog) instead of probing this index (0.01 ms). A capped ANALYZE
# misjudges it the same way; INDEXED BY holds whatever the statistics say.
_PROJECTOR_ROWS_BY_CLAIM_TOKEN = text(
    f"""
    SELECT {", ".join(column.name for column in ProjectorState.__table__.c)}
    FROM projector_state INDEXED BY ix_projector_state_claim_token
    WHERE claim_token = :claim_token AND projector = :projector
    ORDER BY session_id
    """
).columns(*ProjectorState.__table__.c)


# The same eligibility as claim_projector_lag's predicates for search-v2 (no
# tombstone filter for that projector). CROSS JOIN pins sessions as the outer
# loop, so SQLite walks ix_sessions_last_activity_at instead of sorting. The
# select list is named, not p.*: .columns() maps by position, and a long-lived
# database's physical column order differs from the model's once startup has
# auto-added columns.
_PROJECTOR_STATE_SELECT_LIST = ", ".join(f"p.{column.name}" for column in ProjectorState.__table__.c)
_SEARCH_CLAIM_NEWEST_FIRST_WALK = (
    text(
        f"""
        SELECT {_PROJECTOR_STATE_SELECT_LIST}, s.last_activity_at AS walk_activity_at
        FROM sessions AS s CROSS JOIN projector_state AS p
          ON p.projector = 'search-v2' AND p.session_id = s.session_id
        WHERE p.desired_revision > p.completed_revision
          AND (p.claim_expires_at IS NULL OR p.claim_expires_at <= :now)
          AND (p.retry_at IS NULL OR p.retry_at <= :now)
          AND (:floor IS NULL OR s.last_activity_at <= :floor)
        ORDER BY s.last_activity_at DESC
        LIMIT :row_limit
        """
    )
    .bindparams(bindparam("now", type_=DateTime()), bindparam("floor", type_=DateTime()))
    .columns(*ProjectorState.__table__.c, column("walk_activity_at", DateTime()))
)


# The mixins import this module's helpers, so they load after them.
from zerg.catalogd.store_mixins.accounts import AccountsMixin  # noqa: E402
from zerg.catalogd.store_mixins.ingest import IngestMixin  # noqa: E402
from zerg.catalogd.store_mixins.inputs import InputsMixin  # noqa: E402
from zerg.catalogd.store_mixins.interactions import InteractionsMixin  # noqa: E402
from zerg.catalogd.store_mixins.launch import LaunchMixin  # noqa: E402
from zerg.catalogd.store_mixins.legacy import LegacyMigrationMixin  # noqa: E402
from zerg.catalogd.store_mixins.machines import MachinesMixin  # noqa: E402
from zerg.catalogd.store_mixins.projectors import ProjectorsMixin  # noqa: E402
from zerg.catalogd.store_mixins.sessions import SessionsMixin  # noqa: E402
from zerg.catalogd.store_mixins.storage import StorageMixin  # noqa: E402


class CatalogStore(
    AccountsMixin,
    MachinesMixin,
    InteractionsMixin,
    LaunchMixin,
    InputsMixin,
    SessionsMixin,
    IngestMixin,
    StorageMixin,
    ProjectorsMixin,
    LegacyMigrationMixin,
):
    """Small product operations over the bounded catalog.

    Methods are deliberately synchronous: the daemon invokes mutations on one
    executor and explicitly read-only operations on a separate bounded pool,
    keeping SQLite work off the asyncio socket loop while WAL readers remain
    available during background writes.
    """

    def reset_e2e_user_data(self) -> dict[str, object]:
        """Clear browser-test product state while retaining its authority.

        Catalogd remains the only SQLite owner.  The E2E Runtime Host keeps the
        dev user, device tokens, and runner identities created by Playwright;
        every session/fact/interaction row is removed between tests.
        """

        if os.getenv("ENVIRONMENT", "").strip() != "test:e2e" or os.getenv("TESTING", "").strip().lower() not in _TRUTHY_ENV:
            raise ValueError("E2E catalog reset is available only in test:e2e")
        metadata = MetaData()
        metadata.reflect(bind=self.engine)
        preserved = {"catalog_meta", "users", "device_tokens", "runners"}
        cleared: list[str] = []
        with self.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            try:
                for table in reversed(metadata.sorted_tables):
                    if table.name in preserved:
                        continue
                    connection.execute(table.delete())
                    cleared.append(table.name)
                connection.commit()
            finally:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        from zerg.catalogd.schema import initialize_catalog_schema

        initialize_catalog_schema(self.engine)
        self._shadow_parity_delta_count = None
        return {"reset": True, "tables_cleared": sorted(cleared)}

    def retire_archive_outbox(self) -> dict[str, int | str]:
        """Remove dead monolith projections and retain launch rows only as completed receipts."""

        table = LiveArchiveOutbox.__table__
        observed_at = datetime.now(UTC)
        obsolete_kinds = ("heartbeat_stamp.v1", "runtime_event.v1", "session_input_receipt.v1")
        launch_kinds = ("managed_local_launch.v1",)
        with _write_transaction(self.engine) as connection:
            deleted = connection.execute(delete(table).where(table.c.kind.in_(obsolete_kinds))).rowcount or 0
            completed = (
                connection.execute(
                    update(table)
                    .where(table.c.kind.in_(launch_kinds), table.c.drained_at.is_(None))
                    .values(drained_at=table.c.created_at, last_error=None)
                ).rowcount
                or 0
            )
            pruned = (
                connection.execute(
                    delete(table).where(
                        table.c.kind.in_(launch_kinds),
                        table.c.drained_at.isnot(None),
                        table.c.drained_at < observed_at - timedelta(days=30),
                    )
                ).rowcount
                or 0
            )
            commit_seq = _advance_commit_seq(connection, observed_at) if deleted or completed or pruned else _current_commit_seq(connection)
            return {
                "deleted": int(deleted),
                "completed": int(completed),
                "pruned": int(pruned),
                "commit_seq": str(commit_seq),
            }

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._shadow_parity_delta_count: int | None = None
        # Newest-first search claims resume their session walk below the
        # newest row the previous walk could claim; see SEARCH_CLAIM_WALK_RESTART.
        self._search_walk_floor: datetime | None = None
        self._search_walk_started = 0.0
        self._search_claim_turn = 0

    @staticmethod
    def _resolve_session_owner_id(connection: Any, *, session_id: str) -> str | None:
        """Resolve the durable owner binding for one session, or None when unbound."""

        live_owner = connection.execute(select(LiveSession.owner_id).where(LiveSession.session_id == session_id)).scalar_one_or_none()
        if live_owner is not None:
            return str(live_owner)
        storage_owner = connection.execute(
            select(StorageSession.owner_id).where(StorageSession.session_id == session_id)
        ).scalar_one_or_none()
        if storage_owner is not None:
            return str(storage_owner)

        # A Helm launch binds its owner on the attempt row before any transcript
        # ingest exists, so it is the only durable owner a freshly launched
        # managed session has. Ambiguous attempts bind nothing.
        launch_owners = {
            str(row.owner_id)
            for row in connection.execute(
                select(LiveSessionLaunchAttempt.owner_id).where(
                    LiveSessionLaunchAttempt.session_id == session_id,
                    LiveSessionLaunchAttempt.owner_id.is_not(None),
                )
            )
        }
        if len(launch_owners) == 1:
            return launch_owners.pop()

        catalog_origin = connection.execute(
            select(LiveSessionCatalog.origin_kind).where(LiveSessionCatalog.session_id == session_id)
        ).scalar_one_or_none()
        if catalog_origin != "console":
            return None
        outbox_owner = connection.execute(
            select(func.json_extract(LiveArchiveOutbox.payload_json, "$.session.owner_id")).where(
                LiveArchiveOutbox.idempotency_key == f"console_session_create.v1:{session_id}"
            )
        ).scalar_one_or_none()
        return str(outbox_owner) if outbox_owner is not None else None

    @staticmethod
    def _session_explicitly_belongs_to_owner(connection: Any, *, session_id: str, owner_id: int) -> bool:
        """Fail closed unless a durable row explicitly binds the session owner."""

        owner_exists = connection.execute(
            select(LiveUser.id).where(LiveUser.id == owner_id, LiveUser.is_active.is_(True))
        ).scalar_one_or_none()
        if owner_exists is None:
            return False
        return CatalogStore._resolve_session_owner_id(connection, session_id=session_id) == str(owner_id)

    def checkpoint_passive(self) -> dict[str, int]:
        """Run a non-blocking WAL checkpoint owned by catalogd."""

        with self.engine.connect() as connection:
            busy, log_frames, checkpointed_frames = connection.exec_driver_sql("PRAGMA wal_checkpoint(PASSIVE)").one()
        return {
            "busy": int(busy),
            "log_frames": int(log_frames),
            "checkpointed_frames": int(checkpointed_frames),
        }

    def checkpoint_truncate(self) -> dict[str, int]:
        """Reclaim the WAL file itself.

        PASSIVE checkpoints reuse WAL space but never shrink the file, so the
        on-disk WAL only ever records its own high-water mark -- 692 MB on the
        dogfood tenant, long after the burst that caused it. TRUNCATE is the only
        mode that gives the space back. It blocks, so callers must run it when
        the writer is idle and treat `busy` as "try again later", never as an
        error.
        """

        with self.engine.connect() as connection:
            busy, log_frames, checkpointed_frames = connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").one()
        return {
            "busy": int(busy),
            "log_frames": int(log_frames),
            "checkpointed_frames": int(checkpointed_frames),
        }
