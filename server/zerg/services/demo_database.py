"""Build the disposable demo corpus used by the marketing and demo stacks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
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
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionThreadAlias
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
from zerg.services.demo_seed import DEMO_PRESENTATION
from zerg.services.demo_seed import seed_missing_demo_sessions
from zerg.services.legacy_corpus_migration import LegacyCorpusConverter
from zerg.services.legacy_corpus_migration import _normalized_event_source
from zerg.services.legacy_corpus_migration import create_inventory_run
from zerg.storage_v2.render_objects import read_render_object
from zerg.utils.time import utc_now_naive

_TENANT_ID = "demo-tenant"
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


def _ensure_legacy_owner(db, email: str) -> None:
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


def _build_legacy_source(output_path: Path, *, owner_email: str, anchor: datetime) -> sessionmaker:
    engine = make_engine(f"sqlite:///{output_path}").execution_options(schema_translate_map={"zerg": None, "agents": None})
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        _ensure_legacy_owner(db, owner_email)
        _ensure_agents_fts(engine)
        seeded_count, failed_count = seed_missing_demo_sessions(db, now=anchor)
        if failed_count:
            raise RuntimeError(f"failed to seed {failed_count} demo sessions")
        if seeded_count != len(DEMO_PRESENTATION):
            raise RuntimeError(f"expected {len(DEMO_PRESENTATION)} demo sessions, seeded {seeded_count}")
        _add_lossless_demo_source_lines(db, anchor=anchor)
    finally:
        db.close()
    return factory


def _add_lossless_demo_source_lines(db, *, anchor: datetime) -> None:
    """Give the build-time migration a lossless source inventory.

    The normal synthetic seed intentionally stores parsed events only. The
    migration contract distinguishes byte-covered source records from its
    normalized fallback, so add invented normalized source lines to the
    disposable staging DB before converting it to storage-v2.
    """

    events = db.query(AgentEvent).order_by(AgentEvent.id.asc()).all()
    for offset, event in enumerate(events):
        raw_value = _normalized_event_source((event,))
        if raw_value is None:
            raise RuntimeError(f"could not normalize demo event {event.id}")
        encoded = raw_value.encode("utf-8")
        db.add(
            AgentSourceLine(
                session_id=event.session_id,
                thread_id=event.thread_id,
                source_path=f"demo/{event.session_id}.jsonl",
                source_offset=offset,
                branch_id=int(event.branch_id or 0),
                revision=1,
                is_branch_copy=0,
                raw_json=raw_value,
                raw_json_z=None,
                raw_json_codec=0,
                line_hash=hashlib.sha256(encoded).hexdigest(),
                created_at=anchor,
            )
        )
    db.commit()


def _object_root() -> Path:
    override = os.getenv("LONGHOUSE_STORAGE_V2_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[3] / "data" / "objects-v2"


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


async def _migrate_legacy_source(
    legacy_factory: sessionmaker,
    live_path: Path,
    object_root: Path,
    *,
    anchor: datetime,
) -> None:
    # Catalogd enforces the portable Unix socket limit. The demo path under a
    # repository checkout is long enough to exceed it on macOS.
    socket_dir = Path("/tmp") / f"lhcd-demo-{os.getpid()}"
    socket_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    socket_path = socket_dir / "catalogd.sock"
    daemon = CatalogDaemon(database_path=live_path, socket_path=socket_path, checkpoint_interval_seconds=0)
    await daemon.start()
    catalog = CatalogClient(socket_path)
    try:
        with legacy_factory() as db:
            inventory = await create_inventory_run(db, catalog, created_at=anchor)
        converter = LegacyCorpusConverter(
            session_factory=legacy_factory,
            catalog=catalog,
            object_root=object_root,
            tenant_id=_TENANT_ID,
        )
        summary = await converter.migrate_run(UUID(inventory["run_id"]), workers=1)
        counts = (summary.get("summary") or {}).get("state_counts") or {}
        if counts.get("verified") != len(DEMO_PRESENTATION) or any(counts.get(state, 0) for state in ("pending", "migrating", "degraded")):
            raise RuntimeError(f"demo storage migration did not verify: {counts}")
    finally:
        await catalog.close()
        await daemon.close()
        socket_dir.rmdir()


def _legacy_session_ids(legacy_factory: sessionmaker) -> dict[str, AgentSession]:
    with legacy_factory() as db:
        rows = (
            db.query(AgentSession, SessionThreadAlias.alias_value)
            .join(SessionThread, SessionThread.session_id == AgentSession.id)
            .join(SessionThreadAlias, SessionThreadAlias.thread_id == SessionThread.id)
            .filter(SessionThread.is_primary == 1, SessionThreadAlias.alias_kind == "provider_session_id")
            .all()
        )
        return {str(alias): session for session, alias in rows}


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
    evidence_hash = hashlib.sha256(encoded.encode()).hexdigest()
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


def _seed_live_catalog(live_path: Path, legacy_factory: sessionmaker, *, anchor: datetime) -> None:
    legacy_by_provider_id = _legacy_session_ids(legacy_factory)
    engine = create_catalog_engine(live_path)
    managed_by_id = {
        provider_id: (legacy_by_provider_id[provider_id], config)
        for provider_id, config in _MANAGED_SESSIONS.items()
        if provider_id in legacy_by_provider_id
    }
    if len(managed_by_id) != len(_MANAGED_SESSIONS):
        missing = sorted(set(_MANAGED_SESSIONS) - set(managed_by_id))
        raise RuntimeError(f"managed demo sessions are missing from legacy source: {missing}")

    with engine.begin() as connection:
        storage_rows = {str(row["session_id"]): row for row in connection.execute(select(StorageSession.__table__)).mappings()}
        for provider_id, (legacy_session, config) in managed_by_id.items():
            session_id = str(legacy_session.id)
            storage = storage_rows[session_id]
            thread_id = str(uuid5(NAMESPACE_URL, f"demo-thread:{provider_id}"))
            run_id = str(uuid5(NAMESPACE_URL, f"demo-run:{provider_id}"))
            adapter_connection_id = str(uuid5(NAMESPACE_URL, f"demo-connection:{provider_id}"))
            lease_generation = str(uuid5(NAMESPACE_URL, f"demo-lease:{provider_id}"))
            is_finished = config["phase"] == "finished"
            closed_at = anchor - timedelta(minutes=8) if is_finished else None
            ended_at = closed_at
            last_health_at = anchor if not is_finished else anchor - timedelta(minutes=8)
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
                    loop_mode="assist",
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
                    freshness_expires_at=(anchor + timedelta(minutes=5) if not is_finished else anchor - timedelta(minutes=1)),
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
                        heartbeat_at=anchor,
                        payload_json=json.dumps({"bridge_status": "ready", "lease_ttl_ms": 300_000}),
                        updated_at=anchor,
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
                    "valid_until": (anchor + timedelta(minutes=5)).isoformat()
                    if not is_finished
                    else (anchor - timedelta(minutes=1)).isoformat(),
                },
                observed_at=last_health_at,
                valid_until=(anchor + timedelta(minutes=5) if not is_finished else anchor - timedelta(minutes=1)),
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
                        "granted_operations": ["interrupt", "send_input", "terminate", "tail_output", "resume"],
                        "state": "attached",
                        "lease_ttl_ms": 300_000,
                        "source": "provider_control",
                        "observed_at": anchor.isoformat(),
                    },
                    observed_at=anchor,
                    valid_until=anchor + timedelta(minutes=5),
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
            sessions = catalog_connection.execute(select(StorageSession.__table__)).mappings().all()
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
    """Build the legacy staging DB plus the live catalog and derived search DB."""

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

    legacy_factory = _build_legacy_source(output_path, owner_email=owner_email, anchor=observed_at)
    try:
        _initialize_live_catalog(live_path, owner_email=owner_email)
        asyncio.run(_migrate_legacy_source(legacy_factory, live_path, object_root, anchor=observed_at))
        _seed_live_catalog(live_path, legacy_factory, anchor=observed_at)
        _build_search_index(live_path, search_path, object_root)
    finally:
        legacy_factory.kw["bind"].dispose()

    return {"legacy": output_path, "live": live_path, "search": search_path, "objects": object_root}


__all__ = ["build_demo_database"]
