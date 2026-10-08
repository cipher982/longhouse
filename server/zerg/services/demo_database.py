"""Build the disposable demo corpus used by the marketing and demo stacks."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL
from uuid import UUID
from uuid import uuid5

from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.models import FactHead
from zerg.catalogd.models import RenderObject
from zerg.catalogd.models import StorageSession
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.crud import get_user_by_email
from zerg.database import Base
from zerg.database import _ensure_agents_fts
from zerg.database import make_engine
from zerg.machine_evidence import canonical_evidence_hash
from zerg.models.live_store import LiveControlLease
from zerg.models.live_store import LiveHeartbeatStamp
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSession
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveSessionThreadAlias
from zerg.models.live_store import LiveUser
from zerg.searchd.store import SearchStore
from zerg.searchd.store import object_set_hash
from zerg.searchd.store import open_search_database
from zerg.services.agents import SessionIngest
from zerg.services.demo_seed import DEMO_PRESENTATION
from zerg.services.demo_sessions import build_demo_agent_sessions
from zerg.services.provider_interaction_semantics import seed_provider_interaction_sequence_context
from zerg.services.raw_json_compression import decode_raw_json
from zerg.storage_v2.normalized_events import ORDERING_REVISION
from zerg.storage_v2.normalized_events import PARSER_REVISION
from zerg.storage_v2.normalized_events import aware
from zerg.storage_v2.normalized_events import normalized_event_source
from zerg.storage_v2.normalized_events import opaque_source_id
from zerg.storage_v2.normalized_events import optional_text
from zerg.storage_v2.normalized_events import render_order_key
from zerg.storage_v2.normalized_events import render_record
from zerg.storage_v2.normalized_events import stable_uuid
from zerg.storage_v2.raw_objects import RawObjectSpec
from zerg.storage_v2.raw_objects import RawRecord
from zerg.storage_v2.raw_objects import seal_raw_object
from zerg.storage_v2.render_objects import SEMANTIC_PROJECTION_VERSION
from zerg.storage_v2.render_objects import RenderObjectSpec
from zerg.storage_v2.render_objects import read_render_object
from zerg.storage_v2.render_objects import seal_render_object
from zerg.utils.time import utc_now_naive

_TENANT_ID = "demo-tenant"
_DEMO_LIVE_STAMP_AHEAD = timedelta(minutes=30)
_DEMO_LIVE_LEASE_TTL_MS = 24 * 60 * 60 * 1000
# Keep the public corpus mixed: two currently steerable Helm sessions, one
# Console session, and one recently closed managed session. The other six
# sessions remain storage-backed archive/search examples.
_MANAGED_SESSIONS = {
    "demo-claude-05": {
        "control_plane": "claude_channel_bridge",
        "phase": "running",
        "tool": "Bash",
        "origin_kind": "managed",
        "launch_surface": "terminal",
    },
    "demo-antigravity-02": {
        "control_plane": "cursor_helm",
        "phase": "idle",
        "tool": None,
        "origin_kind": "managed",
        "launch_surface": "terminal",
    },
    "demo-codex-03": {
        "control_plane": "codex_bridge",
        "phase": "idle",
        "tool": None,
        "origin_kind": "console",
        "launch_surface": "console",
    },
    "demo-claude-04": {
        "control_plane": "opencode_server_bridge",
        "phase": "finished",
        "tool": None,
        "origin_kind": "managed",
        "launch_surface": "terminal",
    },
}


def _remove_database_family(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        candidate.unlink(missing_ok=True)


def _ensure_owner(db, email: str) -> None:
    if get_user_by_email(db, email) is not None:
        return
    now = utc_now_naive()
    from zerg.models.models import User

    db.add(
        User(
            email=email,
            provider="dev",
            provider_user_id=email,
            role="ADMIN",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
    )
    db.commit()


def _build_main_database(output_path: Path, *, owner_email: str) -> None:
    """Create the runtime's main database with its schema and owner; sessions live in storage-v2."""

    engine = make_engine(f"sqlite:///{output_path}").execution_options(schema_translate_map={"zerg": None, "agents": None})
    try:
        Base.metadata.create_all(bind=engine)
        _ensure_agents_fts(engine)
        with sessionmaker(bind=engine, expire_on_commit=False)() as db:
            _ensure_owner(db, owner_email)
    finally:
        engine.dispose()


@dataclass
class _DemoEvent:
    """A parsed demo event in the shape the normalized-event renderer reads."""

    id: int
    thread_id: UUID
    branch_id: int
    role: str
    content_text: str | None
    tool_name: str | None
    tool_input_json: Any
    tool_output_text: str | None
    tool_call_id: str | None
    timestamp: datetime
    source_path: str | None
    source_offset: int | None
    raw_json: str | None
    raw_json_codec: int = 0
    raw_json_z: bytes | None = None


@dataclass
class _DemoSession:
    session_id: UUID
    branch_id: int
    data: SessionIngest
    events: list[_DemoEvent]


def _demo_session_id(provider_session_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"longhouse-demo-session:{provider_session_id}")


def _complete_demo_tool_call_pairs(session: _DemoSession) -> None:
    """Link every authored demo tool call to its result.

    The workspace derives the durable ``completed`` state from matching call IDs.
    The seed intentionally omits IDs on some simple tool exchanges, so restore
    those links here without changing the shared, provider-shaped seed corpus.
    Existing IDs remain authoritative and result timestamps provide the duration.
    """

    pending_calls: list[_DemoEvent] = []
    calls_by_id: dict[str, _DemoEvent] = {}
    for event in sorted(session.events, key=lambda item: (item.timestamp, item.id)):
        if event.role == "assistant" and event.tool_name:
            if not event.tool_call_id:
                event.tool_call_id = f"demo-tool-{event.id}"
            pending_calls.append(event)
            calls_by_id[str(event.tool_call_id)] = event
            continue
        if event.role != "tool":
            continue
        matched = calls_by_id.pop(str(event.tool_call_id), None) if event.tool_call_id else None
        if matched is None and not event.tool_call_id and pending_calls:
            matched = pending_calls.pop(0)
            event.tool_call_id = matched.tool_call_id
            calls_by_id.pop(str(matched.tool_call_id), None)
        elif matched is not None:
            pending_calls.remove(matched)
    if pending_calls:
        missing_results = ", ".join(str(event.id) for event in pending_calls)
        raise RuntimeError(f"demo tool calls without results in {session.session_id}: {missing_results}")


def _demo_sessions(anchor: datetime) -> list[_DemoSession]:
    """The demo corpus with stable session ids and globally ordered event ids."""

    sessions: list[_DemoSession] = []
    next_event_id = 1
    for branch_id, data in enumerate(build_demo_agent_sessions(anchor), start=1):
        if not data.provider_session_id or data.provider_session_id not in DEMO_PRESENTATION:
            raise RuntimeError(f"demo session is missing from the presentation contract: {data.provider_session_id}")
        session_id = _demo_session_id(data.provider_session_id)
        # The same thread id _seed_live_catalog gives the managed sessions' live thread.
        thread_id = uuid5(NAMESPACE_URL, f"demo-thread:{data.provider_session_id}")
        events: list[_DemoEvent] = []
        for item in data.events:
            events.append(
                _DemoEvent(
                    id=next_event_id,
                    thread_id=thread_id,
                    branch_id=branch_id,
                    role=item.role,
                    content_text=item.content_text,
                    tool_name=item.tool_name,
                    tool_input_json=item.tool_input_json,
                    tool_output_text=item.tool_output_text,
                    tool_call_id=item.tool_call_id,
                    timestamp=aware(item.timestamp),
                    source_path=item.source_path,
                    source_offset=item.source_offset,
                    raw_json=item.raw_json,
                )
            )
            next_event_id += 1
        session = _DemoSession(session_id=session_id, branch_id=branch_id, data=data, events=events)
        _complete_demo_tool_call_pairs(session)
        sessions.append(session)
    if len(sessions) != len(DEMO_PRESENTATION):
        raise RuntimeError(f"expected {len(DEMO_PRESENTATION)} demo sessions, built {len(sessions)}")
    return sessions


def _object_root() -> Path:
    # The runtime reads transcripts from the same root; a separate default here
    # put a container's demo objects where the server never looks.
    from zerg.services.raw_object_workers import storage_v2_root

    return storage_v2_root().resolve()


def _initialize_live_catalog(live_path: Path, *, owner_email: str) -> None:
    engine = create_catalog_engine(live_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            LiveUser.__table__.insert().values(
                id=1,
                provider="dev",
                provider_user_id=owner_email,
                email=owner_email,
                email_verified=True,
                is_active=True,
                role="ADMIN",
                prefs={},
                context={},
                created_at=now,
                updated_at=now,
            )
        )
    engine.dispose()


def _demo_raw_commit(
    session: _DemoSession,
    raw_spec: RawObjectSpec,
    sealed_raw,
    render_spec: RenderObjectSpec,
    sealed_render,
    *,
    owner_id: str,
) -> dict[str, Any]:
    data = session.data
    started_at = aware(data.started_at)
    last_activity_at = max((event.timestamp for event in session.events), default=started_at)
    return {
        "protocol_version": 2,
        "tenant_id": _TENANT_ID,
        "owner_id": owner_id,
        "session_id": str(session.session_id),
        "machine_id": raw_spec.machine_id,
        "provider": raw_spec.provider,
        "opaque_source_id": raw_spec.opaque_source_id,
        "source_epoch": str(raw_spec.source_epoch),
        "predecessor_source_epoch": None,
        "epoch_opened_at": started_at.isoformat(),
        "range_kind": raw_spec.range_kind,
        "range_start": raw_spec.range_start,
        "range_end": raw_spec.range_end,
        "record_hashes": list(sealed_raw.record_hashes),
        "envelope_id": sealed_raw.envelope_id,
        "object_hash": sealed_raw.object_hash,
        "payload_hash": sealed_raw.payload_hash,
        "compressed_hash": sealed_raw.compressed_hash,
        "object_path": sealed_raw.object_path,
        "uncompressed_size": sealed_raw.uncompressed_size,
        "compressed_size": sealed_raw.compressed_size,
        "provenance_kind": raw_spec.provenance_kind,
        "render_state": "ready",
        "media_refs": [],
        "projectors": [],
        "render_manifest": {
            "generation_id": str(render_spec.render_generation),
            "parser_revision": render_spec.parser_revision,
            "ordering_revision": render_spec.ordering_revision,
            "object_id": sealed_render.object_id,
            "object_hash": sealed_render.object_hash,
            "payload_hash": sealed_render.payload_hash,
            "object_path": sealed_render.object_path,
            "uncompressed_size": sealed_render.uncompressed_size,
            "compressed_size": sealed_render.compressed_size,
            "event_count": sealed_render.event_count,
            "first_order_key": sealed_render.first_order_key,
            "last_order_key": sealed_render.last_order_key,
            "user_messages": sealed_render.user_messages,
            "assistant_messages": sealed_render.assistant_messages,
            "tool_calls": sealed_render.tool_calls,
            "abandoned_events": sealed_render.abandoned_events,
            "first_user_message_preview": sealed_render.first_user_message_preview,
            "last_visible_text_preview": sealed_render.last_visible_text_preview,
            "semantic_projection_version": SEMANTIC_PROJECTION_VERSION,
        },
        "session_facts": {
            "environment": data.environment,
            "project": optional_text(data.project),
            "cwd": optional_text(data.cwd),
            "git_repo": optional_text(data.git_repo),
            "git_branch": optional_text(data.git_branch),
            "started_at": started_at.isoformat(),
            "last_activity_at": last_activity_at.isoformat(),
            "ended_at": None,
            "origin_kind": None,
            # Demo samples are sessions a visitor should see. Automation/test
            # launch labels are what the timeline hides as QA noise, so leave
            # them unset; _seed_live_catalog labels the managed ones.
            "hidden_from_default_timeline": False,
            "launch_actor": None,
            "launch_surface": None,
        },
        "conversation_resets": [],
        "sealed_at": datetime.now(UTC).isoformat(),
    }


async def _import_demo_sessions(sessions: list[_DemoSession], live_path: Path, object_root: Path) -> None:
    """Commit each demo session as one normalized-event raw object with its ready render."""

    # Catalogd enforces the portable Unix socket limit. The demo path under a
    # repository checkout is long enough to exceed it on macOS.
    socket_dir = Path("/tmp") / f"lhcd-demo-{os.getpid()}"
    socket_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    socket_path = socket_dir / "catalogd.sock"
    daemon = CatalogDaemon(database_path=live_path, socket_path=socket_path, checkpoint_interval_seconds=0)
    await daemon.start()
    catalog = CatalogClient(socket_path)
    try:
        owner = await catalog.call("auth.owner.get.v2", {}, timeout_seconds=5.0)
        if owner.get("found") is not True or owner.get("owner_id") is None:
            raise RuntimeError("demo corpus requires an active catalog owner")
        owner_id = str(owner["owner_id"])
        for session in sessions:
            data = session.data
            events = sorted(session.events, key=lambda item: (item.timestamp, item.id))
            source_path = f"legacy-unmatched-events:{session.session_id}"
            records: list[RawRecord] = []
            for position, event in enumerate(events):
                normalized = normalized_event_source((event,))
                if normalized is None:
                    raise RuntimeError(f"could not normalize demo event {event.id}")
                records.append(RawRecord(source_position=position, data=normalized.encode("utf-8")))
            sequence_context: dict[str, Any] = {}
            if str(data.provider or "").strip().lower() == "claude":
                seed_provider_interaction_sequence_context(data.provider, [decode_raw_json(event) for event in events], sequence_context)
            render_records = sorted(
                (
                    render_record(
                        event,
                        position,
                        position,
                        session.session_id,
                        head_branch_id=session.branch_id,
                        provider=data.provider,
                        sequence_context=sequence_context,
                    )
                    for position, event in enumerate(events)
                ),
                key=render_order_key,
            )
            opaque_id = opaque_source_id(session.session_id, source_path, "legacy_normalized_event")
            source_epoch = stable_uuid("demo-source", str(session.session_id))
            raw_spec = RawObjectSpec(
                tenant_id=_TENANT_ID,
                machine_id=data.device_id or "legacy",
                session_id=session.session_id,
                provider=data.provider,
                opaque_source_id=opaque_id,
                source_epoch=source_epoch,
                range_kind="record_ordinal",
                range_start=0,
                range_end=len(records),
                records=tuple(records),
                provenance_kind="legacy_normalized_event",
            )
            sealed_raw = await asyncio.to_thread(seal_raw_object, object_root, raw_spec)
            render_spec = RenderObjectSpec(
                session_id=session.session_id,
                render_generation=stable_uuid("demo-render", str(session.session_id)),
                parser_revision=PARSER_REVISION,
                ordering_revision=ORDERING_REVISION,
                machine_id=raw_spec.machine_id,
                provider=data.provider,
                opaque_source_id=opaque_id,
                source_epoch=source_epoch,
                source_envelope_id=sealed_raw.envelope_id,
                records=tuple(render_records),
            )
            sealed_render = await asyncio.to_thread(seal_render_object, object_root, render_spec)
            await catalog.call(
                "storage.raw_object.commit.v2",
                _demo_raw_commit(session, raw_spec, sealed_raw, render_spec, sealed_render, owner_id=owner_id),
                timeout_seconds=10.0,
            )
    finally:
        await catalog.close()
        await daemon.close()
        socket_dir.rmdir()


def _fact_head(
    connection,
    *,
    family: str,
    subject_key: str,
    source: str,
    session_id: str,
    value: dict[str, object],
    observed_at: datetime,
    valid_until: datetime | None,
) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    evidence_hash = canonical_evidence_hash(value)
    source_epoch = str(uuid5(NAMESPACE_URL, f"demo-fact:{session_id}:{family}"))
    connection.execute(
        FactHead.__table__.insert().values(
            family=family,
            subject_key=subject_key,
            source=source,
            source_epoch=source_epoch,
            session_id=session_id,
            ordering_mode="observed",
            source_seq=1,
            evidence_hash=evidence_hash,
            observed_at=observed_at,
            valid_until=valid_until,
            value_json=encoded,
            raw_locator=None,
            updated_commit_seq=1,
            received_at=observed_at,
        )
    )


def _seed_live_catalog(live_path: Path, sessions: list[_DemoSession], *, anchor: datetime) -> None:
    session_ids = {str(session.data.provider_session_id): session.session_id for session in sessions}
    engine = create_catalog_engine(live_path)
    managed_by_id = {
        provider_id: (session_ids[provider_id], config) for provider_id, config in _MANAGED_SESSIONS.items() if provider_id in session_ids
    }
    if len(managed_by_id) != len(_MANAGED_SESSIONS):
        missing = sorted(set(_MANAGED_SESSIONS) - set(managed_by_id))
        raise RuntimeError(f"managed demo sessions are missing from the demo corpus: {missing}")

    with engine.begin() as connection:
        storage_rows = {str(row["session_id"]): row for row in connection.execute(select(StorageSession.__table__)).mappings()}
        for provider_id, (title, _summary) in DEMO_PRESENTATION.items():
            session_id = str(session_ids[provider_id])
            connection.execute(
                update(StorageSession.__table__)
                .where(StorageSession.__table__.c.session_id == session_id)
                # Each demo window is complete, so Claude's hold for later
                # local-command evidence (semantic version 0) does not apply.
                .values(summary_title=title, anchor_title=title, semantic_projection_version=SEMANTIC_PROJECTION_VERSION)
            )
        for provider_id, (demo_session_id, config) in managed_by_id.items():
            session_id = str(demo_session_id)
            storage = storage_rows[session_id]
            thread_id = str(uuid5(NAMESPACE_URL, f"demo-thread:{provider_id}"))
            run_id = str(uuid5(NAMESPACE_URL, f"demo-run:{provider_id}"))
            adapter_connection_id = str(uuid5(NAMESPACE_URL, f"demo-connection:{provider_id}"))
            lease_generation = str(uuid5(NAMESPACE_URL, f"demo-lease:{provider_id}"))
            is_finished = config["phase"] == "finished"
            closed_at = anchor - timedelta(minutes=8) if is_finished else None
            ended_at = closed_at
            live_stamp = anchor + _DEMO_LIVE_STAMP_AHEAD if not is_finished else anchor - timedelta(minutes=8)
            last_health_at = live_stamp
            title, summary = DEMO_PRESENTATION[provider_id]

            connection.execute(
                update(StorageSession.__table__)
                .where(StorageSession.__table__.c.session_id == session_id)
                .values(
                    summary_title=title,
                    anchor_title=title,
                    first_user_message_preview=storage["first_user_message_preview"],
                )
            )
            connection.execute(
                LiveSession.__table__.insert().values(
                    session_id=session_id,
                    owner_id="1",
                    provider=storage["provider"],
                    device_id=storage["machine_id"],
                    machine_id=storage["machine_id"],
                    state="closed" if is_finished else "online",
                    started_at=storage["started_at"],
                    last_seen_at=last_health_at,
                    updated_at=last_health_at,
                )
            )
            connection.execute(
                LiveSessionCatalog.__table__.insert().values(
                    session_id=session_id,
                    provider=storage["provider"],
                    environment=storage["environment"],
                    project=storage["project"],
                    device_id=storage["machine_id"],
                    device_name=f"Demo {storage['machine_id']}",
                    cwd=storage["cwd"],
                    git_repo=storage["git_repo"],
                    git_branch=storage["git_branch"],
                    started_at=storage["started_at"],
                    ended_at=ended_at,
                    closed_at=closed_at,
                    close_reason="completed" if is_finished else None,
                    last_activity_at=storage["last_activity_at"],
                    user_messages=storage["user_messages"],
                    assistant_messages=storage["assistant_messages"],
                    tool_calls=storage["tool_calls"],
                    summary=summary,
                    summary_title=title,
                    anchor_title=title,
                    first_user_message_preview=storage["first_user_message_preview"],
                    last_visible_text_preview=storage["last_visible_text_preview"],
                    last_user_message_preview=storage["first_user_message_preview"],
                    last_assistant_message_preview=storage["last_visible_text_preview"],
                    transcript_revision=storage["transcript_revision"],
                    summary_revision=1,
                    user_state="active",
                    user_state_at=anchor,
                    primary_thread_id=thread_id,
                    notification_muted=False,
                    origin_kind=config["origin_kind"],
                    hidden_from_default_timeline=0,
                    user_hidden_from_timeline=0,
                    launch_actor="human_ui",
                    launch_surface=config["launch_surface"],
                    permission_mode="bypass",
                    created_at=storage["started_at"],
                    updated_at=anchor,
                )
            )
            connection.execute(
                LiveSessionThread.__table__.insert().values(
                    id=thread_id,
                    session_id=session_id,
                    provider=storage["provider"],
                    device_id=storage["machine_id"],
                    cwd=storage["cwd"],
                    provider_config_json="{}",
                    branch_kind="root",
                    origin_kind=config["origin_kind"],
                    hidden_from_default_timeline=0,
                    is_primary=1,
                    created_at=storage["started_at"],
                    updated_at=anchor,
                )
            )
            connection.execute(
                LiveSessionThreadAlias.__table__.insert().values(
                    thread_id=thread_id,
                    provider=storage["provider"],
                    alias_kind="provider_session_id",
                    alias_value=provider_id,
                    first_seen_at=storage["started_at"],
                    last_seen_at=anchor,
                )
            )
            connection.execute(
                LiveSessionRun.__table__.insert().values(
                    id=run_id,
                    thread_id=thread_id,
                    provider=storage["provider"],
                    host_id=storage["machine_id"],
                    boot_id="demo-boot",
                    cwd=storage["cwd"],
                    argv_redacted_json="[]",
                    launch_origin="longhouse_spawned",
                    started_at=storage["started_at"],
                    ended_at=ended_at,
                    exit_status="0" if is_finished else None,
                )
            )
            connection.execute(
                LiveSessionConnection.__table__.insert().values(
                    run_id=run_id,
                    adapter_connection_id=adapter_connection_id,
                    lease_generation=lease_generation,
                    control_plane=config["control_plane"],
                    acquisition_kind="spawned_control",
                    state="released" if is_finished else "attached",
                    external_name=f"demo-{provider_id}",
                    device_id=storage["machine_id"],
                    can_send_input=0 if is_finished else 1,
                    can_interrupt=0 if is_finished else 1,
                    can_terminate=0 if is_finished else 1,
                    can_tail_output=1,
                    can_resume=1,
                    acquired_at=storage["started_at"],
                    released_at=closed_at,
                    last_health_at=last_health_at,
                )
            )
            connection.execute(
                LiveRuntimeState.__table__.insert().values(
                    runtime_key=f"demo:{provider_id}",
                    session_id=session_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    provider=storage["provider"],
                    device_id=storage["machine_id"],
                    phase=config["phase"],
                    phase_source=config["control_plane"],
                    active_tool=config["tool"],
                    phase_started_at=last_health_at,
                    execution_started_at=storage["started_at"],
                    last_runtime_signal_at=last_health_at,
                    last_progress_at=last_health_at,
                    last_live_at=last_health_at,
                    timeline_anchor_at=storage["last_activity_at"],
                    freshness_expires_at=(
                        live_stamp + timedelta(milliseconds=_DEMO_LIVE_LEASE_TTL_MS) if not is_finished else anchor - timedelta(minutes=1)
                    ),
                    terminal_state="completed" if is_finished else None,
                    terminal_reason="completed" if is_finished else None,
                    terminal_source=config["control_plane"] if is_finished else None,
                    terminal_at=closed_at,
                    runtime_version=1,
                    updated_at=anchor,
                )
            )
            if not is_finished:
                connection.execute(
                    LiveControlLease.__table__.insert().values(
                        session_id=session_id,
                        provider=storage["provider"],
                        device_id=storage["machine_id"],
                        machine_id=storage["machine_id"],
                        state="attached",
                        sequence=1,
                        heartbeat_at=live_stamp,
                        payload_json=json.dumps({"bridge_status": "ready", "lease_ttl_ms": _DEMO_LIVE_LEASE_TTL_MS}),
                        updated_at=live_stamp,
                    )
                )
            _fact_head(
                connection,
                family="activity",
                subject_key=f"run:{run_id}",
                source="provider_runtime",
                session_id=session_id,
                value={
                    "authority_class": "provider_runtime",
                    "provider": storage["provider"],
                    "session_id": session_id,
                    "run_id": run_id,
                    "kind": config["phase"] if config["phase"] != "finished" else "idle",
                    "raw_kind": config["phase"],
                    "tool_name": config["tool"],
                    "source": "provider_runtime",
                    "observed_at": last_health_at.isoformat(),
                    "valid_until": (live_stamp + timedelta(milliseconds=_DEMO_LIVE_LEASE_TTL_MS)).isoformat()
                    if not is_finished
                    else (anchor - timedelta(minutes=1)).isoformat(),
                },
                observed_at=last_health_at,
                valid_until=(
                    live_stamp + timedelta(milliseconds=_DEMO_LIVE_LEASE_TTL_MS) if not is_finished else anchor - timedelta(minutes=1)
                ),
            )
            if not is_finished and config["origin_kind"] != "console":
                _fact_head(
                    connection,
                    family="control",
                    subject_key=f"connection:{adapter_connection_id}:{lease_generation}",
                    source="provider_control",
                    session_id=session_id,
                    value={
                        "authority_class": "provider_control",
                        "provider": storage["provider"],
                        "session_id": session_id,
                        "run_id": run_id,
                        "connection_id": adapter_connection_id,
                        "lease_generation": lease_generation,
                        "granted_operations": ["interrupt", "resume", "send_input", "tail_output", "terminate"],
                        "state": "attached",
                        "lease_ttl_ms": _DEMO_LIVE_LEASE_TTL_MS,
                        "source": "provider_control",
                        "observed_at": live_stamp.isoformat(),
                    },
                    observed_at=live_stamp,
                    valid_until=live_stamp + timedelta(milliseconds=_DEMO_LIVE_LEASE_TTL_MS),
                )
            connection.execute(
                LiveHeartbeatStamp.__table__.insert().values(
                    device_id=storage["machine_id"],
                    received_at=last_health_at,
                    version="demo",
                    last_ship_at=last_health_at,
                    last_ship_attempt_at=last_health_at,
                    last_ship_result="ok",
                    last_ship_http_status=200,
                    disk_free_bytes=100_000_000_000,
                    is_offline=1 if is_finished else 0,
                )
            )
    engine.dispose()


def _build_search_index(live_path: Path, search_path: Path, object_root: Path) -> None:
    _remove_database_family(search_path)
    connection = open_search_database(search_path)
    store = SearchStore(connection)
    store.startup_maintenance()
    catalog_engine = create_catalog_engine(live_path)
    try:
        with catalog_engine.connect() as catalog_connection:
            session_table = StorageSession.__table__
            catalog_table = LiveSessionCatalog.__table__
            sessions = (
                catalog_connection.execute(
                    select(session_table, catalog_table.c.device_id.label("catalog_device_id")).select_from(
                        session_table.outerjoin(catalog_table, catalog_table.c.session_id == session_table.c.session_id)
                    )
                )
                .mappings()
                .all()
            )
            for session in sessions:
                generation_id = session["current_render_generation"]
                if not generation_id or session["render_state"] != "ready":
                    raise RuntimeError(f"demo session {session['session_id']} has no ready render generation")
                manifests = (
                    catalog_connection.execute(
                        select(RenderObject.__table__)
                        .where(RenderObject.__table__.c.session_id == session["session_id"])
                        .where(RenderObject.__table__.c.generation_id == generation_id)
                        .where(RenderObject.__table__.c.retired_at.is_(None))
                        .order_by(RenderObject.__table__.c.object_id.asc())
                    )
                    .mappings()
                    .all()
                )
                object_ids: list[str] = []
                event_count = 0
                for manifest in manifests:
                    decoded = read_render_object(
                        object_root,
                        str(manifest["object_path"]),
                        expected_object_hash=str(manifest["object_hash"]),
                    )
                    records = [
                        {
                            "event_id": record.event_id,
                            "record_ordinal": ordinal,
                            "order_time_us": record.order_time_us,
                            "source_position": record.source_position,
                            "event_subordinal": record.event_subordinal,
                            "role": record.role,
                            "interaction_kind": record.interaction_kind,
                            "content_text": record.content_text,
                            "tool_name": record.tool_name,
                            "tool_output_text": record.tool_output_text,
                            "tool_call_id": record.tool_call_id,
                            "thread_id": record.thread_id,
                            "branch_kind": record.branch_kind,
                        }
                        for ordinal, record in enumerate(decoded.spec.records)
                    ]
                    store.index_object(
                        session_id=str(session["session_id"]),
                        generation_id=str(generation_id),
                        object_id=str(manifest["object_id"]),
                        desired_revision=int(session["commit_seq"]),
                        provider=str(session["provider"]),
                        machine_id=str(session["machine_id"]),
                        project=session["project"],
                        environment=str(session["environment"]),
                        cwd=session["cwd"],
                        git_repo=session["git_repo"],
                        opaque_source_id=decoded.spec.opaque_source_id,
                        source_epoch=str(decoded.spec.source_epoch),
                        records=records,
                    )
                    object_ids.append(str(manifest["object_id"]))
                    event_count += len(records)
                published = store.publish_generation(
                    session_id=str(session["session_id"]),
                    generation_id=str(generation_id),
                    owner_id=str(session["owner_id"]),
                    desired_revision=int(session["commit_seq"]),
                    object_count=len(object_ids),
                    object_set_hash=object_set_hash(object_ids),
                    event_count=event_count,
                    project=session["project"],
                    provider=str(session["provider"]),
                    environment=str(session["environment"]),
                    device_id=(
                        str(session["catalog_device_id"])
                        if session["catalog_device_id"] is not None
                        else (str(session["machine_id"]) if session["machine_id"] is not None else None)
                    ),
                    cwd=session["cwd"],
                    git_repo=session["git_repo"],
                    started_at=session["started_at"].isoformat(),
                )
                if published.get("published") is not True:
                    raise RuntimeError(f"search index publication failed for {session['session_id']}: {published}")
    finally:
        catalog_engine.dispose()
        connection.close()


def build_demo_database(output_path: Path, *, owner_email: str = "local@zerg", anchor: datetime | None = None) -> dict[str, Path]:
    """Build the main DB, the storage-v2 live catalog and the derived search DB."""

    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    live_path = output_path.with_name(f"{output_path.stem}-live{output_path.suffix}")
    search_path = output_path.parent / "search.db"
    for path in (output_path, live_path, search_path):
        _remove_database_family(path)
    live_path.with_suffix(f"{live_path.suffix}.catalogd.lock").unlink(missing_ok=True)
    search_path.with_suffix(f"{search_path.suffix}.searchd.lock").unlink(missing_ok=True)

    observed_at = anchor or datetime.now(UTC)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    object_root = _object_root()
    object_root.mkdir(mode=0o700, parents=True, exist_ok=True)

    sessions = _demo_sessions(observed_at)
    _build_main_database(output_path, owner_email=owner_email)
    _initialize_live_catalog(live_path, owner_email=owner_email)
    asyncio.run(_import_demo_sessions(sessions, live_path, object_root))
    _seed_live_catalog(live_path, sessions, anchor=observed_at)
    _build_search_index(live_path, search_path, object_root)

    return {"main": output_path, "live": live_path, "search": search_path, "objects": object_root}


__all__ = ["build_demo_database"]
