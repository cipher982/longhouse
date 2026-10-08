"""CatalogStore: session reads: timeline, detail, shadow state, provider facts, visibility and aliases."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from sqlalchemy import Connection
from sqlalchemy import and_
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import union_all
from sqlalchemy import update
from sqlalchemy.orm import Session

from zerg.catalogd.fact_reducer import MAX_HEADS_PER_FAMILY
from zerg.catalogd.fact_reducer import _advance_commit_seq
from zerg.catalogd.fact_reducer import _current_commit_seq
from zerg.catalogd.fact_reducer import read_bounded_session_fact_heads
from zerg.catalogd.fact_reducer import read_bounded_sessions_fact_heads
from zerg.catalogd.models import FactConflict
from zerg.catalogd.models import FactHead
from zerg.catalogd.models import FactParityDelta
from zerg.catalogd.models import FactReceipt
from zerg.catalogd.models import SessionTombstone as LiveSessionTombstone
from zerg.catalogd.models import StorageSession
from zerg.catalogd.schema import catalog_meta

# store.py imports this module after defining these, at its end.
from zerg.catalogd.store import _SESSION_READ_DELEGATION_FACT_LIMIT
from zerg.catalogd.store import _SHADOW_PARITY_ENV
from zerg.catalogd.store import _STATE_HEAD_FAMILIES
from zerg.catalogd.store import _TRUTHY_ENV
from zerg.catalogd.store import MACHINE_ENROLLMENT_LIMIT
from zerg.catalogd.store import SHADOW_STATE_FACT_HEAD_LIMIT
from zerg.catalogd.store import SHADOW_STATE_HEALTH_SAMPLE_LIMIT
from zerg.catalogd.store import SHADOW_STATE_HEALTH_WINDOW
from zerg.catalogd.store import _active_session_ids
from zerg.catalogd.store import _as_aware_utc
from zerg.catalogd.store import _assemble_session_facts
from zerg.catalogd.store import _attach_delegation_children
from zerg.catalogd.store import _decode_json_object
from zerg.catalogd.store import _delegation_fact_rows
from zerg.catalogd.store import _delegation_payload
from zerg.catalogd.store import _empty_human_helm_is_open
from zerg.catalogd.store import _encode_datetime
from zerg.catalogd.store import _has_durable_timeline_content
from zerg.catalogd.store import _insert_provider_facts
from zerg.catalogd.store import _merge_registry_lifecycle_heads
from zerg.catalogd.store import _non_negative_int
from zerg.catalogd.store import _provider_fact_rows
from zerg.catalogd.store import _read_snapshot
from zerg.catalogd.store import _StageTimer
from zerg.catalogd.store import _summarize_machine_activity_rows
from zerg.catalogd.store import _timeline_current_work_is_open
from zerg.catalogd.store import _timeline_possibly_open_session_ids
from zerg.catalogd.store import _write_transaction
from zerg.models.live_store import LiveDeviceToken
from zerg.models.live_store import LiveHeartbeatStamp
from zerg.models.live_store import LiveSession
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveSessionThreadAlias
from zerg.models.live_store import LiveTimelineCard
from zerg.models.live_store import LiveUser
from zerg.services.session_title import sanitize_timeline_title
from zerg.services.session_visibility_policy import SessionVisibilityFacts
from zerg.services.session_visibility_policy import effective_system_hidden_clause
from zerg.services.session_visibility_policy import evaluate_origin_visibility
from zerg.services.session_visibility_policy import primary_worker_only_clause
from zerg.services.session_visibility_policy import visible_in_test_scope


class SessionsMixin:
    def _served_session_projection(self, connection: Connection, session: Any, *, observed_at: datetime) -> Any | None:
        """Project one session's served state from its fact heads.

        `claim_queued_input` and the input router must agree on one answer to
        "is a turn running right now"; both read it here rather than each
        deriving it. Returns `None` when the session has no projectable facts.
        """

        from zerg.catalogd.fact_reducer import read_session_fact_heads
        from zerg.services.managed_provider_contracts import contract_for_provider
        from zerg.services.session_state_contract import SessionHostFacts
        from zerg.services.session_state_contract import SessionTranscriptFacts
        from zerg.services.session_state_facts_projector import project_served_session_state_facts

        session_id = str(session.id)
        session_facts = _assemble_session_facts(
            connection,
            session_ids=[session_id],
            observed_at=observed_at,
            compact=True,
        )
        if not session_facts:
            return None
        fact_commit_seq, heads = read_session_fact_heads(connection, session_id=session_id)
        contract = contract_for_provider(session.provider)
        supported_operations = {
            operation
            for operation in ("send_input", "interrupt", "terminate", "tail_output", "resume")
            if contract is not None and bool(getattr(contract, "can_resume" if operation == "resume" else operation, False))
        }
        return project_served_session_state_facts(
            session_id=session_id,
            commit_seq=fact_commit_seq,
            catalog_facts=session_facts[0],
            heads=heads,
            supported_operations=supported_operations,
            pending_interaction=None,
            transcript=SessionTranscriptFacts(convergence="unknown"),
            host=SessionHostFacts(state="unknown"),
            now=observed_at,
        )

    def read_session_activity(self, *, session_id: str, owner_id: int | None = None) -> dict[str, Any]:
        """Report whether the served projection sees a turn running right now.

        Delivering input uses this: a SEND is not dispatched into a running
        turn, because the provider's mid-turn queue is not durable, and STEER
        is only meaningful against one.
        """

        from zerg.services.live_control_catalog import load_live_control_session
        from zerg.services.session_state_contract import input_activity_state

        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            orm = Session(bind=connection, expire_on_commit=False)
            try:
                if owner_id is not None and not self._session_explicitly_belongs_to_owner(
                    connection,
                    session_id=session_id,
                    owner_id=owner_id,
                ):
                    return {"found": False, "observed_at": observed_at.isoformat(), "activity_state": None}
                session = load_live_control_session(orm, session_id)
                if session is None:
                    return {"found": False, "observed_at": observed_at.isoformat(), "activity_state": None}
                projection = self._served_session_projection(connection, session, observed_at=observed_at)
            finally:
                orm.close()
            return {
                "found": True,
                "observed_at": observed_at.isoformat(),
                "activity_state": None if projection is None else input_activity_state(projection),
            }

    def list_session_provider_facts(self, *, session_id: str, limit: int = 200) -> dict[str, Any]:
        """The newest provider facts for a session, newest first."""
        with _read_snapshot(self.engine) as connection:
            facts = _provider_fact_rows(connection, session_id=session_id, limit=limit)
            return {"facts": facts, "commit_seq": str(_current_commit_seq(connection))}

    def insert_session_provider_facts(
        self,
        *,
        session_id: str,
        source_epoch: str,
        provider_facts: tuple[dict[str, Any], ...],
    ) -> dict[str, Any]:
        """Facts for bytes the catalog already holds: replay or backfill. Idempotent."""
        timer = _StageTimer("insert_session_provider_facts")
        with _write_transaction(self.engine, timer=timer) as connection:
            inserted = _insert_provider_facts(
                connection,
                session_id=session_id,
                source_epoch=source_epoch,
                provider_facts=provider_facts,
                commit_seq=int(_current_commit_seq(connection)),
                now=datetime.now(UTC),
            )
            return {"inserted": inserted, "commit_seq": str(_current_commit_seq(connection))}

    def list_session_timeline(
        self,
        *,
        project: str | None,
        provider: str | None,
        environment: str | None,
        include_test: bool,
        hide_autonomous: bool,
        include_automation: bool,
        include_hidden: bool = False,
        device_id: str | None,
        days_back: int | None,
        limit: int,
        offset: int,
        owner_id: int | None = None,
        include_state_heads: bool = False,
        title_query: str | None = None,
        machine_activity: bool = False,
        utc_offset_minutes: int = 0,
    ) -> dict[str, Any]:
        """Return one bounded timeline page and all raw facts in one snapshot.

        ``machine_activity`` reuses this exact candidate set (same ownership,
        visibility and current-work admission) to return per-machine activity
        aggregates instead of a page: sessions started per local day and
        provider, top projects, the latest session, and the counts of open
        and unread candidates. The open count is the SQL superset; callers
        project the served working set before claiming a session is live.
        """

        observed_at = datetime.now(UTC)
        since = observed_at - timedelta(days=days_back) if days_back is not None else None
        machine_window_start: datetime | None = None
        if machine_activity:
            if days_back is None or owner_id is None:
                raise ValueError("machine activity requires days_back and owner_id")
            offset = timedelta(minutes=utc_offset_minutes)
            local_first_day = (observed_at + offset).date() - timedelta(days=days_back - 1)
            machine_window_start = datetime.combine(local_first_day, datetime.min.time(), tzinfo=UTC) - offset
            since = machine_window_start
        title_terms = list(dict.fromkeys(re.findall(r"\w+", title_query, flags=re.UNICODE))) if title_query is not None else []
        if title_query is not None and not title_terms:
            return {"matches": []}
        card = LiveTimelineCard.__table__
        catalog = LiveSessionCatalog.__table__
        storage = StorageSession.__table__
        tombstones = LiveSessionTombstone.__table__
        with _read_snapshot(self.engine) as connection:
            # Unread sessions must survive the recency window and pagination:
            # a Console result nobody has acknowledged stays listed until it is
            # read or archived (docs/specs/console-unread-acknowledgement.md).
            legacy_unread = and_(
                catalog.c.last_console_result_at.isnot(None),
                or_(
                    catalog.c.last_read_at.is_(None),
                    catalog.c.last_console_result_at > catalog.c.last_read_at,
                ),
            )
            storage_unread = and_(
                storage.c.last_console_result_at.isnot(None),
                or_(
                    storage.c.last_read_at.is_(None),
                    storage.c.last_console_result_at > storage.c.last_read_at,
                ),
            )
            legacy_activity_at = func.coalesce(card.c.last_activity_at, card.c.started_at)
            storage_activity_at = func.coalesce(storage.c.last_activity_at, storage.c.started_at)
            # Current work is admitted by predicate, not by rank: a session the
            # served state calls open must survive the recency window and the
            # page cut no matter how quiet its transcript is.
            # Read once in this snapshot; the open predicate appears four times
            # in the candidate statement below.
            possibly_open_ids = [
                str(value) for value in connection.execute(_timeline_possibly_open_session_ids(observed_at=observed_at)).scalars()
            ]
            legacy_open = _timeline_current_work_is_open(
                session_id=card.c.session_id,
                thread_id=catalog.c.primary_thread_id,
                observed_at=observed_at,
                possibly_open_ids=possibly_open_ids,
            )
            storage_open = _timeline_current_work_is_open(
                session_id=storage.c.session_id,
                thread_id=catalog.c.primary_thread_id,
                observed_at=observed_at,
                possibly_open_ids=possibly_open_ids,
            )
            legacy_where = [
                or_(legacy_activity_at >= since, legacy_unread, legacy_open) if since is not None else True,
                catalog.c.user_state.notin_(("archived", "snoozed", "deleted")),
                ~select(storage.c.session_id).where(storage.c.session_id == card.c.session_id).exists(),
            ]
            storage_where = [
                or_(storage_activity_at >= since, storage_unread, storage_open) if since is not None else True,
                storage.c.user_state.notin_(("archived", "snoozed", "deleted")),
                ~select(tombstones.c.session_id).where(tombstones.c.session_id == storage.c.session_id).exists(),
            ]
            if not include_hidden:
                legacy_where.append(card.c.user_hidden_from_timeline == 0)
                storage_where.append(storage.c.user_hidden_from_timeline == 0)
            # Storage-v2 providers do not always repeat launch identity in
            # their native transcript metadata. The live catalog is the
            # authoritative identity for a managed session, so a durable
            # storage row with a NULL project/machine/provenance must still
            # survive the same filtered timeline request as its live shell.
            storage_project = func.coalesce(catalog.c.project, storage.c.project)
            storage_provider = func.coalesce(catalog.c.provider, storage.c.provider)
            storage_environment = func.coalesce(catalog.c.environment, storage.c.environment)
            storage_device = func.coalesce(catalog.c.device_id, storage.c.machine_id)
            storage_launch_actor = func.coalesce(catalog.c.launch_actor, storage.c.launch_actor)
            storage_launch_surface = func.coalesce(catalog.c.launch_surface, storage.c.launch_surface)
            legacy_empty_open = and_(
                catalog.c.launch_actor == "human_shell",
                catalog.c.launch_surface == "terminal",
                _empty_human_helm_is_open(
                    session_id=card.c.session_id,
                    thread_id=catalog.c.primary_thread_id,
                    observed_at=observed_at,
                    possibly_open_ids=possibly_open_ids,
                ),
            )
            storage_empty_open = and_(
                storage_launch_actor == "human_shell",
                storage_launch_surface == "terminal",
                _empty_human_helm_is_open(
                    session_id=storage.c.session_id,
                    thread_id=catalog.c.primary_thread_id,
                    observed_at=observed_at,
                    possibly_open_ids=possibly_open_ids,
                ),
            )
            # Policy visibility is independent from shell readiness. Content
            # may enter history; a zero-content human Helm is admitted only
            # while the canonical heads would place it in Open.
            legacy_where.append(or_(_has_durable_timeline_content(card), legacy_empty_open))
            storage_where.append(or_(_has_durable_timeline_content(storage), storage_empty_open))
            if project is not None:
                legacy_where.append(card.c.project == project)
                storage_where.append(storage_project == project)
            if provider is not None:
                legacy_where.append(card.c.provider == provider)
                storage_where.append(storage_provider == provider)
            if environment is not None:
                legacy_where.append(card.c.environment == environment)
                storage_where.append(storage_environment == environment)
            elif not include_test and not include_automation and not include_hidden:
                legacy_where.append(card.c.environment.notin_(("test", "e2e")))
                storage_where.append(storage_environment.notin_(("test", "e2e")))
            if device_id is not None:
                legacy_where.append(card.c.device_id == device_id)
                storage_where.append(storage_device == device_id)
            if hide_autonomous and not include_hidden:
                legacy_where.append(
                    or_(
                        card.c.user_messages > 0,
                        card.c.archive_state == "pending",
                        card.c.launch_actor == "human_ui",
                        card.c.launch_surface.in_(("web", "ios", "api")),
                        legacy_empty_open,
                    )
                )
                storage_where.append(
                    or_(
                        storage.c.user_messages > 0,
                        storage_launch_actor == "human_ui",
                        storage_launch_surface.in_(("web", "ios", "api")),
                        storage_empty_open,
                    )
                )
            if not include_automation and not include_hidden:
                legacy_worker_only = primary_worker_only_clause(card, LiveSessionThread.__table__)
                storage_worker_only = primary_worker_only_clause(storage, LiveSessionThread.__table__)
                legacy_where.append(
                    ~or_(
                        effective_system_hidden_clause(
                            card,
                            include_test=include_test,
                            worker_only_evidence=legacy_worker_only,
                        ),
                        effective_system_hidden_clause(
                            catalog,
                            include_test=include_test,
                            worker_only_evidence=legacy_worker_only,
                        ),
                    )
                )
                storage_where.append(
                    ~or_(
                        effective_system_hidden_clause(
                            storage,
                            include_test=include_test,
                            worker_only_evidence=storage_worker_only,
                        ),
                        effective_system_hidden_clause(
                            catalog,
                            include_test=include_test,
                            worker_only_evidence=storage_worker_only,
                        ),
                    )
                )
            if include_state_heads or title_query is not None or machine_activity:
                if owner_id is None:
                    raise ValueError("canonical timeline projection requires owner_id")
                owner_text = str(owner_id)
                legacy_where.append(
                    select(LiveSession.session_id)
                    .where(
                        LiveSession.session_id == card.c.session_id,
                        LiveSession.owner_id == owner_text,
                    )
                    .exists()
                )
                # Storage-v2 ingest may not carry the machine token's owner
                # binding, while the managed lease has already bound the
                # session in the live store. Match the detail/read ownership
                # rule: live ownership wins, with storage ownership as the
                # fallback for sessions that have no live row yet.
                storage_owner = func.coalesce(LiveSession.__table__.c.owner_id, storage.c.owner_id)
                storage_where.append(storage_owner == owner_text)

            legacy_title = func.coalesce(
                func.nullif(catalog.c.anchor_title, ""), func.nullif(card.c.summary_title, ""), card.c.first_user_message_preview, ""
            )
            storage_title = func.coalesce(
                func.nullif(storage.c.anchor_title, ""), func.nullif(storage.c.summary_title, ""), storage.c.first_user_message_preview, ""
            )
            if title_query is not None:
                for term in title_terms:
                    legacy_where.append(func.lower(legacy_title).contains(term.lower(), autoescape=True))
                    storage_where.append(func.lower(storage_title).contains(term.lower(), autoescape=True))
                # A search range is strict; unlike an ordinary timeline, unread
                # or current work cannot escape the range the caller requested.
                if since is not None:
                    legacy_where.append(legacy_activity_at >= since)
                    storage_where.append(storage_activity_at >= since)

            joined = card.join(catalog, catalog.c.session_id == card.c.session_id)
            storage_joined = storage.outerjoin(catalog, catalog.c.session_id == storage.c.session_id).outerjoin(
                LiveSession.__table__, LiveSession.__table__.c.session_id == storage.c.session_id
            )
            candidates = union_all(
                select(
                    card.c.session_id.label("session_id"),
                    legacy_activity_at.label("order_at"),
                    legacy_title.label("title"),
                    card.c.project.label("project"),
                    card.c.provider.label("provider"),
                    card.c.device_id.label("device_id"),
                    card.c.environment.label("environment"),
                    card.c.started_at.label("started_at"),
                    card.c.user_messages.label("user_messages"),
                    case((legacy_unread, 1), else_=0).label("unread"),
                    case((legacy_open, 1), else_=0).label("open_now"),
                )
                .select_from(joined)
                .where(*legacy_where),
                select(
                    storage.c.session_id.label("session_id"),
                    storage_activity_at.label("order_at"),
                    storage_title.label("title"),
                    storage_project.label("project"),
                    storage_provider.label("provider"),
                    storage_device.label("device_id"),
                    storage_environment.label("environment"),
                    storage.c.started_at.label("started_at"),
                    storage.c.user_messages.label("user_messages"),
                    case((storage_unread, 1), else_=0).label("unread"),
                    case((storage_open, 1), else_=0).label("open_now"),
                )
                .select_from(storage_joined)
                .where(*storage_where),
            ).subquery()
            if title_query is not None:
                matches = connection.execute(
                    select(candidates).order_by(candidates.c.order_at.desc(), candidates.c.session_id.asc()).limit(limit)
                ).mappings()
                return {
                    "matches": [
                        {
                            "session_id": str(row["session_id"]),
                            "title": sanitize_timeline_title(row["title"]),
                            "project": row["project"],
                            "provider": row["provider"],
                            "device_id": row["device_id"],
                            "environment": row["environment"],
                            "started_at": _encode_datetime(row["started_at"]),
                            "user_messages": int(row["user_messages"] or 0),
                        }
                        for row in matches
                    ]
                }
            if machine_activity:
                assert machine_window_start is not None
                activity_rows = connection.execute(
                    select(
                        candidates.c.session_id,
                        candidates.c.device_id,
                        candidates.c.order_at,
                        candidates.c.title,
                        candidates.c.project,
                        candidates.c.provider,
                        candidates.c.started_at,
                        candidates.c.unread,
                        candidates.c.open_now,
                    ).where(candidates.c.device_id.isnot(None), candidates.c.device_id != "")
                ).mappings()
                return {
                    "observed_at": observed_at.isoformat(),
                    "window_start": machine_window_start.isoformat(),
                    "machines": _summarize_machine_activity_rows(
                        activity_rows,
                        window_start=machine_window_start,
                        utc_offset_minutes=utc_offset_minutes,
                    ),
                }

            # Unread first, then current work, then transcript recency. A
            # session the served state calls open, or whose Console result
            # nobody has read, must never be paged out of the first window.
            # Nothing here may rank on when the reducer last wrote a head:
            # that clock moves while the session does not, which is what made
            # the page rotate under a watcher. Clients keep doing their own
            # visual sort.
            # The total rides on the page query as a window over the whole
            # candidate set. Counting separately evaluated every candidate's
            # correlated predicates a second time: half the statement's cost on
            # a real catalog. An empty page (offset past the end) still counts.
            page = connection.execute(
                select(candidates.c.session_id, func.count().over().label("total"))
                .order_by(
                    candidates.c.unread.desc(),
                    candidates.c.open_now.desc(),
                    candidates.c.order_at.desc(),
                    candidates.c.session_id.desc(),
                )
                .limit(limit)
                .offset(offset)
            ).all()
            session_ids = [str(row.session_id) for row in page]
            if page:
                total = int(page[0].total)
            else:
                total = int(connection.execute(select(func.count()).select_from(candidates)).scalar_one())
            facts = _assemble_session_facts(
                connection,
                session_ids=session_ids,
                observed_at=observed_at,
                compact=True,
            )
            heads_by_session: dict[str, tuple[list[dict[str, Any]], bool]] = {}
            if include_state_heads:
                _head_commit_seq, grouped_heads, truncated_sessions = read_bounded_sessions_fact_heads(
                    connection,
                    session_ids=session_ids,
                    families=_STATE_HEAD_FAMILIES,
                    limit_per_session=SHADOW_STATE_FACT_HEAD_LIMIT,
                )
                _merge_registry_lifecycle_heads(connection, grouped_heads)
                heads_by_session = {session_id: (heads, session_id in truncated_sessions) for session_id, heads in grouped_heads.items()}
                _attach_delegation_children(
                    connection, facts=facts, heads_by_session={key: value[0] for key, value in heads_by_session.items()}
                )
            has_real_sessions = total == 0 or any((item["catalog"].get("device_id") or "") != "demo-mac" for item in facts)
            return {
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "rows": [
                    {
                        "thread_id": item["primary_thread"]["id"] if item["primary_thread"] is not None else None,
                        "facts": item,
                        **(
                            {
                                "heads_truncated": heads_by_session[str(item["catalog"]["session_id"])][1],
                                "heads": [
                                    {
                                        "family": head["family"],
                                        "session_id": head["session_id"],
                                        "subject_key": head["subject_key"],
                                        "source": head["source"],
                                        "source_epoch": head["source_epoch"],
                                        "evidence_hash": head["evidence_hash"],
                                        "value_json": head["value_json"],
                                        "valid_until": (head["valid_until"].isoformat() if head["valid_until"] is not None else None),
                                        "updated_commit_seq": head["updated_commit_seq"],
                                        # What the activity lease anchors to. Omitted, every
                                        # head this path serializes reads as expired.
                                        "received_at": (head["received_at"].isoformat() if head["received_at"] is not None else None),
                                    }
                                    for head in heads_by_session[str(item["catalog"]["session_id"])][0]
                                ],
                            }
                            if include_state_heads
                            else {}
                        ),
                    }
                    for item in facts
                ],
                "total": total,
                "has_real_sessions": has_real_sessions,
            }

    def read_session(self, *, session_id: str, owner_id: int | None = None) -> dict[str, Any]:
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            if owner_id is not None and not self._session_explicitly_belongs_to_owner(
                connection,
                session_id=session_id,
                owner_id=owner_id,
            ):
                return {
                    "commit_seq": str(_current_commit_seq(connection)),
                    "observed_at": observed_at.isoformat(),
                    "found": False,
                    "facts": None,
                }
            facts = _assemble_session_facts(
                connection,
                session_ids=[session_id],
                observed_at=observed_at,
                compact=False,
            )
            return {
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "found": bool(facts),
                "facts": facts[0] if facts else None,
            }

    def read_shadow_session_state(self, *, session_id: str, owner_id: int) -> dict[str, Any]:
        """Read a diagnostic-only Phase 3 projection at one catalog snapshot."""

        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            if not self._session_explicitly_belongs_to_owner(
                connection,
                session_id=session_id,
                owner_id=owner_id,
            ):
                return {
                    "commit_seq": str(_current_commit_seq(connection)),
                    "observed_at": observed_at.isoformat(),
                    "found": False,
                    "provider": None,
                    "head_count": 0,
                    "heads_truncated": False,
                    "legacy_facts": None,
                    "heads": [],
                }
            session_facts = _assemble_session_facts(
                connection,
                session_ids=[session_id],
                observed_at=observed_at,
                compact=False,
            )
            if not session_facts:
                return {
                    "commit_seq": str(_current_commit_seq(connection)),
                    "observed_at": observed_at.isoformat(),
                    "found": False,
                    "provider": None,
                    "head_count": 0,
                    "heads_truncated": False,
                    "legacy_facts": None,
                    "heads": [],
                }
            provider = str(session_facts[0]["catalog"].get("provider") or "").strip().lower()
            commit_seq, heads, heads_truncated = read_bounded_session_fact_heads(
                connection,
                session_id=session_id,
                families=_STATE_HEAD_FAMILIES,
                limit=SHADOW_STATE_FACT_HEAD_LIMIT,
            )
            heads = _merge_registry_lifecycle_heads(connection, {session_id: heads})[session_id]
            _attach_delegation_children(connection, facts=session_facts, heads_by_session={session_id: heads})
            return {
                "commit_seq": str(commit_seq),
                "observed_at": observed_at.isoformat(),
                "found": True,
                "provider": provider or None,
                "head_count": len(heads),
                "heads_truncated": heads_truncated,
                "legacy_facts": session_facts[0],
                "heads": [
                    {
                        "family": head["family"],
                        "session_id": head["session_id"],
                        "subject_key": head["subject_key"],
                        "source": head["source"],
                        "source_epoch": head["source_epoch"],
                        "evidence_hash": head["evidence_hash"],
                        "value_json": head["value_json"],
                        "valid_until": head["valid_until"].isoformat() if head["valid_until"] is not None else None,
                        "updated_commit_seq": head["updated_commit_seq"],
                        # What the activity lease anchors to. Omitted, every
                        # head this path serializes reads as expired.
                        "received_at": head["received_at"].isoformat() if head["received_at"] is not None else None,
                    }
                    for head in heads
                ],
            }

    def read_shadow_sessions_state(self, *, session_ids: list[str], owner_id: int) -> dict[str, Any]:
        """Read up to twenty owner-scoped session projections in one snapshot."""

        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            owner_exists = connection.execute(
                select(LiveUser.id).where(LiveUser.id == owner_id, LiveUser.is_active.is_(True))
            ).scalar_one_or_none()
            owned_ids = (
                [
                    session_id
                    for session_id in session_ids
                    if self._resolve_session_owner_id(connection, session_id=session_id) == str(owner_id)
                ]
                if owner_exists is not None
                else []
            )
            facts = _assemble_session_facts(
                connection,
                session_ids=owned_ids,
                observed_at=observed_at,
                compact=False,
            )
            facts_by_id = {
                str(catalog["session_id"]): item
                for item in facts
                if isinstance(item, dict) and isinstance((catalog := item.get("catalog")), dict) and catalog.get("session_id")
            }
            commit_seq, heads_by_session, truncated = read_bounded_sessions_fact_heads(
                connection,
                session_ids=owned_ids,
                families=_STATE_HEAD_FAMILIES,
                limit_per_session=SHADOW_STATE_FACT_HEAD_LIMIT,
            )
            _merge_registry_lifecycle_heads(connection, heads_by_session)
            _attach_delegation_children(connection, facts=facts, heads_by_session=heads_by_session)
            sessions = []
            for session_id in session_ids:
                session_facts = facts_by_id.get(session_id)
                heads = heads_by_session.get(session_id, [])
                provider = str(session_facts["catalog"].get("provider") or "").strip().lower() if session_facts is not None else ""
                sessions.append(
                    {
                        "session_id": session_id,
                        "found": session_facts is not None,
                        "provider": provider or None,
                        "head_count": len(heads),
                        "heads_truncated": session_id in truncated,
                        "legacy_facts": session_facts,
                        "heads": [
                            {
                                "family": head["family"],
                                "session_id": head["session_id"],
                                "subject_key": head["subject_key"],
                                "source": head["source"],
                                "source_epoch": head["source_epoch"],
                                "evidence_hash": head["evidence_hash"],
                                "value_json": head["value_json"],
                                "valid_until": head["valid_until"].isoformat() if head["valid_until"] is not None else None,
                                "updated_commit_seq": head["updated_commit_seq"],
                                # What the activity lease anchors to. Omitted, every
                                # head this path serializes reads as expired.
                                "received_at": head["received_at"].isoformat() if head["received_at"] is not None else None,
                            }
                            for head in heads
                        ],
                    }
                )
            return {
                "commit_seq": str(commit_seq),
                "observed_at": observed_at.isoformat(),
                "sessions": sessions,
            }

    def read_sessions(self, *, session_ids: list[str], owner_id: int | None = None) -> dict[str, Any]:
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            if owner_id is not None:
                session_ids = [
                    session_id
                    for session_id in session_ids
                    if self._session_explicitly_belongs_to_owner(
                        connection,
                        session_id=session_id,
                        owner_id=owner_id,
                    )
                ]
            facts = _assemble_session_facts(
                connection,
                session_ids=session_ids,
                observed_at=observed_at,
                compact=False,
            )
            return {
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "facts": facts,
            }

    def read_shadow_session_state_health(self, *, owner_id: int) -> dict[str, Any]:
        """Summarize bounded reducer storage and recent heartbeat outcomes."""

        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            canonical_owners = connection.execute(
                select(LiveUser.id, LiveUser.is_active)
                .where(or_(LiveUser.provider != "service", LiveUser.provider.is_(None)))
                .order_by(LiveUser.id.asc())
                .limit(2)
            ).all()
            if len(canonical_owners) != 1 or int(canonical_owners[0].id) != owner_id or canonical_owners[0].is_active is not True:
                return {
                    "found": False,
                    "commit_seq": str(_current_commit_seq(connection)),
                    "observed_at": observed_at.isoformat(),
                }

            device_ids = tuple(
                str(value)
                for value in connection.execute(
                    select(LiveDeviceToken.device_id)
                    .where(LiveDeviceToken.owner_id == owner_id, LiveDeviceToken.revoked_at.is_(None))
                    .distinct()
                    .order_by(LiveDeviceToken.device_id.asc())
                    .limit(MACHINE_ENROLLMENT_LIMIT)
                ).scalars()
            )

            owner_text = str(owner_id)
            head_table = FactHead.__table__
            live_sessions = LiveSession.__table__
            storage_sessions = StorageSession.__table__
            head_owner_scope = head_table.outerjoin(
                live_sessions,
                live_sessions.c.session_id == head_table.c.session_id,
            ).outerjoin(
                storage_sessions,
                storage_sessions.c.session_id == head_table.c.session_id,
            )
            owner_predicate = or_(live_sessions.c.owner_id == owner_text, storage_sessions.c.owner_id == owner_text)
            head_counts = {
                str(family): int(count)
                for family, count in connection.execute(
                    select(head_table.c.family, func.count())
                    .select_from(head_owner_scope)
                    .where(owner_predicate)
                    .group_by(head_table.c.family)
                    .order_by(head_table.c.family.asc())
                )
            }

            def owned_child_count(child_table, identity_column) -> int:
                child_scope = (
                    child_table.join(
                        head_table,
                        and_(
                            child_table.c.family == head_table.c.family,
                            child_table.c.subject_key == head_table.c.subject_key,
                            child_table.c.source == head_table.c.source,
                            child_table.c.source_epoch == head_table.c.source_epoch,
                        ),
                    )
                    .outerjoin(live_sessions, live_sessions.c.session_id == head_table.c.session_id)
                    .outerjoin(storage_sessions, storage_sessions.c.session_id == head_table.c.session_id)
                )
                return int(
                    connection.execute(select(func.count(identity_column)).select_from(child_scope).where(owner_predicate)).scalar_one()
                )

            receipt_count = owned_child_count(FactReceipt.__table__, FactReceipt.id)
            conflict_count = owned_child_count(FactConflict.__table__, FactConflict.id)
            parity_delta_count = owned_child_count(FactParityDelta.__table__, FactParityDelta.delta_key)
            recent_rows = (
                connection.execute(
                    select(LiveHeartbeatStamp.received_at, LiveHeartbeatStamp.catalog_result_json)
                    .where(
                        LiveHeartbeatStamp.device_id.in_(device_ids),
                        LiveHeartbeatStamp.received_at >= observed_at - SHADOW_STATE_HEALTH_WINDOW,
                        LiveHeartbeatStamp.catalog_result_json.is_not(None),
                    )
                    .order_by(LiveHeartbeatStamp.received_at.desc())
                    .limit(SHADOW_STATE_HEALTH_SAMPLE_LIMIT + 1)
                ).all()
                if device_ids
                else []
            )
            recent_truncated = len(recent_rows) > SHADOW_STATE_HEALTH_SAMPLE_LIMIT
            recent_rows = recent_rows[:SHADOW_STATE_HEALTH_SAMPLE_LIMIT]

            reducer_status_counts: dict[str, int] = {}
            parity_status_counts: dict[str, int] = {}
            totals = {
                "changed_heads": 0,
                "duplicates": 0,
                "stale": 0,
                "conflicts": 0,
                "parity_deltas": 0,
                "parity_missing_heads": 0,
            }
            identity_binding_totals = {
                "bound": 0,
                "matched": 0,
                "unbound": 0,
                "mismatched": 0,
            }
            malformed_results = 0
            for row in recent_rows:
                try:
                    result = _decode_json_object(row.catalog_result_json)
                    reducer = result.get("shadow_reducer")
                    parity = result.get("shadow_parity")
                    if not isinstance(reducer, dict) or not isinstance(parity, dict):
                        raise ValueError("shadow outcome is missing")
                    reducer_status = reducer.get("status")
                    parity_status = parity.get("status")
                    if reducer_status not in {
                        "disabled",
                        "no_evidence",
                        "unsupported_schema",
                        "oversize_evidence",
                        "applied",
                        "failed",
                    }:
                        raise ValueError("unknown shadow reducer status")
                    if parity_status not in {
                        "disabled",
                        "legacy_unavailable",
                        "no_evidence",
                        "unsupported_schema",
                        "oversize_evidence",
                        "compared",
                        "failed",
                    }:
                        raise ValueError("unknown shadow parity status")
                    reducer_counters = {
                        field: _non_negative_int(reducer.get(field, 0), field)
                        for field in ("changed_heads", "duplicates", "stale", "conflicts")
                    }
                    parity_counters = {
                        "parity_deltas": _non_negative_int(parity.get("deltas", 0), "deltas"),
                        "parity_missing_heads": _non_negative_int(
                            parity.get("missing_heads", 0),
                            "missing_heads",
                        ),
                    }
                    identity_binding = reducer.get("identity_binding")
                    if identity_binding is not None:
                        if not isinstance(identity_binding, dict):
                            raise ValueError("shadow identity binding outcome is invalid")
                        for field in identity_binding_totals:
                            identity_binding_totals[field] += _non_negative_int(
                                identity_binding.get(field, 0),
                                f"identity_binding.{field}",
                            )
                    reducer_status_counts[reducer_status] = reducer_status_counts.get(reducer_status, 0) + 1
                    parity_status_counts[parity_status] = parity_status_counts.get(parity_status, 0) + 1
                    for field, value in {**reducer_counters, **parity_counters}.items():
                        totals[field] += value
                except (RuntimeError, TypeError, ValueError):
                    malformed_results += 1

            return {
                "found": True,
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "ingest_enabled": True,
                "parity_enabled": os.getenv(_SHADOW_PARITY_ENV, "").strip().lower() in _TRUTHY_ENV,
                "storage": {
                    "head_counts": head_counts,
                    "head_capacity_per_family": MAX_HEADS_PER_FAMILY,
                    "receipt_count": receipt_count,
                    "conflict_count": conflict_count,
                    "parity_delta_count": parity_delta_count,
                },
                "recent_batches": {
                    "sample_size": len(recent_rows),
                    "sample_limit": SHADOW_STATE_HEALTH_SAMPLE_LIMIT,
                    "window_seconds": int(SHADOW_STATE_HEALTH_WINDOW.total_seconds()),
                    "truncated": recent_truncated,
                    "newest_received_at": _encode_datetime(recent_rows[0].received_at) if recent_rows else None,
                    "oldest_received_at": _encode_datetime(recent_rows[-1].received_at) if recent_rows else None,
                    "malformed_results": malformed_results,
                    "reducer_status_counts": reducer_status_counts,
                    "parity_status_counts": parity_status_counts,
                    "identity_binding": identity_binding_totals,
                    **totals,
                },
            }

    def list_active_session_ids(self, *, limit: int, days_back: int, observed_at: datetime) -> dict[str, Any]:
        """Return bounded recently observed session identities from the live lane."""

        with _read_snapshot(self.engine) as connection:
            return {
                "session_ids": _active_session_ids(connection, limit=limit, days_back=days_back, observed_at=observed_at),
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def read_cutover_consistency(self, *, observed_at: datetime) -> dict[str, Any]:
        """Answer a deployment's read-consistency probe from one read snapshot.

        The probe used to make four calls spread over three read lanes: the
        active list on the two-worker interactive lane, which reconnecting
        desktops' timeline replays fill at exactly that moment, and the queued
        list on the unbounded projector lane, behind background work. The same
        queries now run once, here, on the control lane.
        """

        from zerg.services.live_session_inputs import list_session_ids_with_queued_live_receipts

        with _read_snapshot(self.engine) as connection:
            active_ids = _active_session_ids(connection, limit=1, days_back=1, observed_at=observed_at)
            orm = Session(bind=connection, expire_on_commit=False)
            try:
                queued_ids = list_session_ids_with_queued_live_receipts(orm, limit=1)
            finally:
                orm.close()
            meta = connection.execute(
                select(catalog_meta.c.catalog_id, catalog_meta.c.schema_version, catalog_meta.c.commit_seq).where(
                    catalog_meta.c.singleton == 1
                )
            ).one()
        return {
            "catalog_id": str(meta.catalog_id),
            "schema_version": meta.schema_version,
            "commit_seq": str(meta.commit_seq),
            "active_session_ids": active_ids,
            "queued_session_ids": [str(session_id) for session_id in queued_ids],
        }

    def reclassify_session_origin(
        self,
        *,
        session_id: str,
        origin_kind: str,
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Reclassify a session's hidden origin across the hot and durable stores.

        Operational backfill for already-archived automation rows: flips
        ``origin_kind`` + ``hidden_from_default_timeline`` on the live catalog,
        timeline card, thread rows, and the durable storage session in one write
        transaction. Not content-derived and not a user-hide.
        """

        normalized = str(origin_kind or "").strip().lower().replace("-", "_")
        if normalized not in ("hatch_automation", "test_or_canary"):
            return {"invalid_origin_kind": True}
        session_key = str(session_id)
        with _write_transaction(self.engine) as connection:
            changed_rows = 0
            commit_seq = _advance_commit_seq(connection, observed_at)
            for table in (LiveSessionCatalog.__table__, LiveTimelineCard.__table__, StorageSession.__table__):
                values: dict[str, Any] = {
                    "origin_kind": normalized,
                    "hidden_from_default_timeline": 1,
                    "launch_actor": "automation",
                    "launch_surface": "hatch" if normalized == "hatch_automation" else "test",
                    "updated_at": observed_at,
                }
                if table is StorageSession.__table__:
                    values["commit_seq"] = commit_seq
                result = connection.execute(update(table).where(table.c.session_id == session_key).values(**values))
                changed_rows += int(result.rowcount or 0)
            thread_result = connection.execute(
                update(LiveSessionThread.__table__)
                .where(LiveSessionThread.__table__.c.session_id == session_key)
                .values(
                    origin_kind=normalized,
                    hidden_from_default_timeline=1,
                    updated_at=observed_at,
                )
            )
            changed_rows += int(thread_result.rowcount or 0)
        return {"reclassified": changed_rows > 0, "rows_changed": changed_rows, "commit_seq": str(commit_seq)}

    def reconcile_session_visibility(
        self,
        *,
        session_id: str,
        system_hidden: bool,
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Converge the denormalized system flag without rewriting raw facts."""

        session_key = str(session_id)
        with _write_transaction(self.engine) as connection:
            storage_row = (
                connection.execute(
                    select(StorageSession.__table__.c.raw_state, StorageSession.__table__.c.render_state).where(
                        StorageSession.__table__.c.session_id == session_key
                    )
                )
                .mappings()
                .first()
            )
            retired = bool(storage_row and (storage_row["raw_state"] == "retired" or storage_row["render_state"] == "retired"))
            effective_system_hidden = bool(system_hidden or retired)
            target = int(effective_system_hidden)
            changed_rows = 0
            found = False
            for table in (LiveSessionCatalog.__table__, LiveTimelineCard.__table__, StorageSession.__table__):
                present = connection.execute(select(table.c.session_id).where(table.c.session_id == session_key)).first()
                found = found or present is not None
                result = connection.execute(
                    update(table)
                    .where(
                        table.c.session_id == session_key,
                        table.c.hidden_from_default_timeline != target,
                    )
                    .values(hidden_from_default_timeline=target, updated_at=observed_at)
                )
                changed_rows += int(result.rowcount or 0)
            result = connection.execute(
                update(LiveSessionThread.__table__)
                .where(
                    LiveSessionThread.__table__.c.session_id == session_key,
                    LiveSessionThread.__table__.c.hidden_from_default_timeline != target,
                )
                .values(hidden_from_default_timeline=target, updated_at=observed_at)
            )
            changed_rows += int(result.rowcount or 0)
            commit_seq = _advance_commit_seq(connection, observed_at) if changed_rows else _current_commit_seq(connection)
            if changed_rows:
                connection.execute(
                    update(StorageSession.__table__)
                    .where(StorageSession.__table__.c.session_id == session_key)
                    .values(commit_seq=commit_seq)
                )
        return {
            "found": found,
            "reconciled": found,
            "rows_changed": changed_rows,
            "system_hidden": effective_system_hidden,
            "commit_seq": str(commit_seq),
        }

    def reconcile_all_session_visibility(
        self,
        *,
        apply: bool,
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Evaluate the complete catalog/storage union and converge mirrors."""

        catalog_table = LiveSessionCatalog.__table__
        card_table = LiveTimelineCard.__table__
        storage_table = StorageSession.__table__
        thread_table = LiveSessionThread.__table__
        context = _write_transaction(self.engine) if apply else _read_snapshot(self.engine)
        with context as connection:
            catalogs = {str(row["session_id"]): row for row in connection.execute(select(catalog_table)).mappings()}
            cards = {str(row["session_id"]): row for row in connection.execute(select(card_table)).mappings()}
            storage = {str(row["session_id"]): row for row in connection.execute(select(storage_table)).mappings()}
            primary_threads = {
                str(row["session_id"]): row
                for row in connection.execute(select(thread_table).where(thread_table.c.is_primary == 1)).mappings()
            }
            session_ids = sorted(set(catalogs) | set(cards) | set(storage) | set(primary_threads))
            actionable: list[str] = []
            targets: dict[str, int] = {}
            unresolved: list[str] = []
            reason_counts: dict[str, int] = {}
            mirror_rows: list[dict[str, Any]] = []
            for session_id in session_ids:
                catalog_present = session_id in catalogs
                storage_present = session_id in storage
                catalog_row = catalogs.get(session_id) or {}
                storage_row = storage.get(session_id) or {}
                card_row = cards.get(session_id) or {}
                thread_row = primary_threads.get(session_id) or {}

                # Provenance has one authority per generation.  A missing
                # value on the authoritative row is unknown; it must not be
                # replaced by a stale denormalized mirror that can keep a
                # repaired human session classified as automation forever.
                provenance = catalog_row if catalog_present else storage_row if storage_present else card_row
                content = storage_row if storage_present else catalog_row if catalog_present else card_row
                decision = evaluate_origin_visibility(
                    SessionVisibilityFacts(
                        provider=provenance.get("provider"),
                        project=provenance.get("project"),
                        environment=provenance.get("environment"),
                        origin_kind=provenance.get("origin_kind"),
                        launch_actor=provenance.get("launch_actor"),
                        launch_surface=provenance.get("launch_surface"),
                        cwd=content.get("cwd"),
                        machine_id=content.get("machine_id") or provenance.get("device_id"),
                        primary_thread_is_worker_only=thread_row.get("branch_kind") == "subagent",
                        # Storage-v2 sessions have no live thread row, so the
                        # sweep must read the same worker evidence ingest stored.
                        # Without this it would revisit every hidden subagent and
                        # helpfully reveal it again.
                        is_subagent=bool(storage_row.get("is_subagent")) if storage_row else False,
                    )
                )
                retired = bool(storage_row and (storage_row.get("raw_state") == "retired" or storage_row.get("render_state") == "retired"))
                system_hidden = bool(decision.system_hidden or retired)
                for reason in decision.reason_keys:
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1
                if retired:
                    reason_counts["retired_source"] = reason_counts.get("retired_source", 0) + 1
                current_values = [
                    bool(row.get("hidden_from_default_timeline")) for row in (catalog_row, card_row, storage_row, thread_row) if row
                ]
                if any(value != system_hidden for value in current_values):
                    actionable.append(session_id)
                    targets[session_id] = int(system_hidden)
                final_hidden = system_hidden
                preferences = catalog_row if catalog_present else storage_row if storage_present else card_row
                user_hidden = bool(preferences.get("user_hidden_from_timeline"))
                user_state = str(preferences.get("user_state") or "active")
                # A reveal is just as important as a hide.  Include every
                # changed row so search/worklog projections can clear stale
                # flags; non-default rows are included for idempotent repair.
                if session_id in targets or final_hidden or user_hidden or user_state not in {"active", "parked"}:
                    mirror_rows.append(
                        {
                            "session_id": session_id,
                            "system_hidden": final_hidden,
                            "test_scope_visible": visible_in_test_scope(decision),
                            "user_hidden_from_timeline": user_hidden,
                            "user_state": user_state,
                        }
                    )

            changed_rows = 0
            commit_seq = _current_commit_seq(connection)
            if apply and actionable:
                commit_seq = _advance_commit_seq(connection, observed_at)
                for target in (0, 1):
                    target_ids = [session_id for session_id, value in targets.items() if value == target]
                    if not target_ids:
                        continue
                    for table in (catalog_table, card_table, storage_table):
                        values: dict[str, Any] = {
                            "hidden_from_default_timeline": target,
                            "updated_at": observed_at,
                        }
                        if table is storage_table:
                            values["commit_seq"] = commit_seq
                        result = connection.execute(
                            update(table)
                            .where(
                                table.c.session_id.in_(target_ids),
                                func.coalesce(table.c.hidden_from_default_timeline, 0) != target,
                            )
                            .values(**values)
                        )
                        changed_rows += int(result.rowcount or 0)
                    result = connection.execute(
                        update(thread_table)
                        .where(
                            thread_table.c.session_id.in_(target_ids),
                            thread_table.c.is_primary == 1,
                            func.coalesce(thread_table.c.hidden_from_default_timeline, 0) != target,
                        )
                        .values(hidden_from_default_timeline=target, updated_at=observed_at)
                    )
                    changed_rows += int(result.rowcount or 0)

            return {
                "mode": "apply" if apply else "dry_run",
                "evaluated": len(session_ids),
                "actionable_count": len(actionable),
                "actionable_session_ids": actionable,
                "proven_hidden_count": sum(1 for row in mirror_rows if row["system_hidden"]),
                "derived_visibility_count": len(mirror_rows),
                "unresolved_hidden_count": len(unresolved),
                "unresolved_hidden_session_ids": unresolved,
                "reason_counts": reason_counts,
                "changed_rows": changed_rows,
                "mirror_rows": mirror_rows if apply else [],
                "commit_seq": str(commit_seq),
            }

    def update_session_preferences(
        self,
        *,
        session_id: str,
        owner_id: int,
        user_state: str | None,
        notification_muted: bool | None,
        user_hidden_from_timeline: bool | None,
        observed_at: datetime,
        last_read_at: datetime | None = None,
        last_user_input_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Update bounded user-owned session state in one catalog transaction.

        Scoped like every other owner-bound write: a session that does not
        explicitly belong to ``owner_id`` reports ``found: False``, which is the
        same answer an unknown session id gets, so the caller cannot use this
        route as an existence oracle.
        """

        table = LiveSessionCatalog.__table__
        with _write_transaction(self.engine) as connection:
            if not self._session_explicitly_belongs_to_owner(connection, session_id=session_id, owner_id=owner_id):
                return {
                    "found": False,
                    "preferences": None,
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            current = (
                connection.execute(
                    select(
                        table.c.user_state,
                        table.c.user_state_at,
                        table.c.notification_muted,
                        table.c.user_hidden_from_timeline,
                        table.c.user_hidden_at,
                        table.c.last_read_at,
                        table.c.last_user_input_at,
                    ).where(table.c.session_id == session_id)
                )
                .mappings()
                .first()
            )
            if current is None:
                storage_current = (
                    connection.execute(
                        select(
                            StorageSession.__table__.c.user_state,
                            StorageSession.__table__.c.notification_muted,
                            StorageSession.__table__.c.user_hidden_from_timeline,
                            StorageSession.__table__.c.last_read_at,
                            StorageSession.__table__.c.last_user_input_at,
                        ).where(StorageSession.__table__.c.session_id == session_id)
                    )
                    .mappings()
                    .first()
                )
                if storage_current is not None:
                    storage_values: dict[str, Any] = {"updated_at": observed_at}
                    if user_state is not None:
                        storage_values["user_state"] = user_state
                    if notification_muted is not None:
                        storage_values["notification_muted"] = int(notification_muted)
                    if user_hidden_from_timeline is not None:
                        storage_values["user_hidden_from_timeline"] = int(user_hidden_from_timeline)
                        storage_values["user_hidden_at"] = observed_at if user_hidden_from_timeline else None
                    storage_read_at = _as_aware_utc(storage_current["last_read_at"])
                    if last_read_at is not None and (storage_read_at is None or last_read_at > storage_read_at):
                        storage_values["last_read_at"] = last_read_at
                    storage_input_at = _as_aware_utc(storage_current["last_user_input_at"])
                    if last_user_input_at is not None and (storage_input_at is None or last_user_input_at > storage_input_at):
                        storage_values["last_user_input_at"] = last_user_input_at
                    # A replayed (equal or older) max-write changes nothing; don't
                    # write or advance the commit sequence for it.
                    changed = set(storage_values) != {"updated_at"}
                    if changed:
                        connection.execute(
                            update(StorageSession.__table__)
                            .where(StorageSession.__table__.c.session_id == session_id)
                            .values(**storage_values)
                        )
                    commit_seq = _advance_commit_seq(connection, observed_at) if changed else _current_commit_seq(connection)
                    return {
                        "found": True,
                        "preferences": {
                            "user_state": str(storage_values.get("user_state", storage_current["user_state"]) or "active"),
                            "notification_muted": bool(storage_values.get("notification_muted", storage_current["notification_muted"])),
                            "user_hidden_from_timeline": (
                                user_hidden_from_timeline
                                if user_hidden_from_timeline is not None
                                else bool(storage_current["user_hidden_from_timeline"])
                            ),
                            "last_read_at": _encode_datetime(storage_values.get("last_read_at") or storage_read_at),
                            "last_user_input_at": _encode_datetime(storage_values.get("last_user_input_at") or storage_input_at),
                        },
                        "updated": changed,
                        "commit_seq": str(commit_seq),
                    }
                return {
                    "found": False,
                    "preferences": None,
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            values: dict[str, Any] = {}
            if user_state is not None and user_state != str(current["user_state"] or "active"):
                values["user_state"] = user_state
                values["user_state_at"] = observed_at
            if notification_muted is not None and notification_muted != bool(current["notification_muted"]):
                values["notification_muted"] = int(notification_muted)
            if user_hidden_from_timeline is not None and user_hidden_from_timeline != bool(current["user_hidden_from_timeline"]):
                values["user_hidden_from_timeline"] = int(user_hidden_from_timeline)
                values["user_hidden_at"] = observed_at if user_hidden_from_timeline else None
            current_read_at = _as_aware_utc(current["last_read_at"])
            # Mark-read is a max-write: read_through never moves last_read_at
            # backwards, and an already-read session is a true no-op.
            if last_read_at is not None and (current_read_at is None or last_read_at > current_read_at):
                values["last_read_at"] = last_read_at
            # Same max-write for the owner's last composer input.
            current_input_at = _as_aware_utc(current["last_user_input_at"])
            if last_user_input_at is not None and (current_input_at is None or last_user_input_at > current_input_at):
                values["last_user_input_at"] = last_user_input_at
            if values:
                values["updated_at"] = observed_at
                connection.execute(update(table).where(table.c.session_id == session_id).values(**values))
            # Reconcile the durable preferences even when the live value is
            # already correct: a repeated action must repair an older divergent
            # storage projection rather than remain a false no-op.
            storage_values = {
                key: values.get(key, current[key])
                for key in ("user_state", "notification_muted", "user_hidden_from_timeline", "user_hidden_at", "last_read_at")
            }
            storage_table = StorageSession.__table__
            storage_changed = connection.execute(
                update(storage_table)
                .where(
                    storage_table.c.session_id == session_id,
                    or_(*(storage_table.c[key].is_distinct_from(value) for key, value in storage_values.items())),
                )
                .values(**storage_values, updated_at=observed_at)
            ).rowcount
            if last_user_input_at is not None:
                # Max-write, not reconcile: never move the archived stamp backwards.
                storage_changed += connection.execute(
                    update(storage_table)
                    .where(
                        storage_table.c.session_id == session_id,
                        or_(storage_table.c.last_user_input_at.is_(None), storage_table.c.last_user_input_at < last_user_input_at),
                    )
                    .values(last_user_input_at=last_user_input_at, updated_at=observed_at)
                ).rowcount
            card_table = LiveTimelineCard.__table__
            card_values = {key: storage_values[key] for key in ("user_hidden_from_timeline", "user_hidden_at")}
            card_changed = connection.execute(
                update(card_table)
                .where(
                    card_table.c.session_id == session_id,
                    or_(*(card_table.c[key].is_distinct_from(value) for key, value in card_values.items())),
                )
                .values(**card_values, updated_at=observed_at)
            ).rowcount
            changed = bool(values) or bool(storage_changed) or bool(card_changed)
            commit_seq = _advance_commit_seq(connection, observed_at) if changed else _current_commit_seq(connection)
            return {
                "found": True,
                "preferences": {
                    "user_state": user_state if user_state is not None else str(current["user_state"] or "active"),
                    "user_state_at": _encode_datetime(observed_at if "user_state" in values else current["user_state_at"]),
                    "notification_muted": (notification_muted if notification_muted is not None else bool(current["notification_muted"])),
                    "user_hidden_from_timeline": (
                        user_hidden_from_timeline if user_hidden_from_timeline is not None else bool(current["user_hidden_from_timeline"])
                    ),
                    "last_read_at": _encode_datetime(values.get("last_read_at") or current_read_at),
                    "last_user_input_at": _encode_datetime(values.get("last_user_input_at") or current_input_at),
                },
                "updated": changed,
                "commit_seq": str(commit_seq),
            }

    def resolve_session_prefix(self, *, prefix: str, owner_id: int) -> dict[str, Any]:
        observed_at = datetime.now(UTC)
        catalog = LiveSessionCatalog.__table__
        live = LiveSession.__table__
        storage = StorageSession.__table__
        user = LiveUser.__table__
        with _read_snapshot(self.engine) as connection:
            matches = list(
                connection.execute(
                    select(
                        catalog.c.session_id,
                        catalog.c.provider,
                        catalog.c.device_name,
                        catalog.c.started_at,
                        catalog.c.ended_at,
                    )
                    .where(
                        catalog.c.session_id.like(f"{prefix}%"),
                        or_(
                            select(live.c.session_id)
                            .where(live.c.session_id == catalog.c.session_id, live.c.owner_id == str(owner_id))
                            .exists(),
                            select(storage.c.session_id)
                            .where(storage.c.session_id == catalog.c.session_id, storage.c.owner_id == str(owner_id))
                            .exists(),
                        ),
                    )
                    .order_by(catalog.c.session_id.asc())
                    .limit(2)
                ).mappings()
            )
            status = "unique" if len(matches) == 1 else "ambiguous" if len(matches) > 1 else "missing"
            session_preview: dict[str, Any] | None = None
            owner_preview: dict[str, str | None] | None = None
            if status == "unique":
                match = matches[0]
                session_preview = {
                    "session_id": str(match["session_id"]),
                    "provider": str(match["provider"]),
                    "device_name": match["device_name"],
                    "started_at": _encode_datetime(match["started_at"]),
                    "ended_at": _encode_datetime(match["ended_at"]),
                }
                owner_ref = self._resolve_session_owner_id(connection, session_id=str(match["session_id"]))
                owner_row = (
                    connection.execute(select(user.c.display_name, user.c.email).where(user.c.id == owner_ref)).mappings().first()
                    if owner_ref is not None
                    else None
                )
                if owner_row is not None:
                    display_name = str(owner_row["display_name"] or "").strip() or None
                    email = str(owner_row["email"] or "").strip()
                    email_local = email.split("@", 1)[0] or None if "@" in email else None
                    owner_preview = {"display_name": display_name, "email_local": email_local}
            return {
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "status": status,
                "session_id": session_preview["session_id"] if session_preview is not None else None,
                "session": session_preview,
                "owner": owner_preview,
            }

    @staticmethod
    def _iso_or_none(value) -> str | None:
        aware = _as_aware_utc(value)
        return aware.isoformat() if aware is not None else None

    def list_session_subagents(self, *, session_id: str, owner_id: str, limit: int = 200) -> dict[str, Any]:
        """List bounded child transcripts plus source-authored references.

        Historical spawn observations are evidence only. They do not make a
        child live or pending, and unresolved references remain visible without
        inventing a Longhouse session id.
        """

        session_table = StorageSession.__table__
        with _read_snapshot(self.engine) as connection:
            parent = (
                connection.execute(
                    select(session_table).where(session_table.c.session_id == session_id, session_table.c.owner_id == owner_id)
                )
                .mappings()
                .first()
            )
            if parent is None:
                return {"found": False, "session_id": session_id, "children": [], "child_references": []}
            child_limit = max(1, min(int(limit), 500))
            rows = (
                connection.execute(
                    select(
                        session_table.c.session_id,
                        session_table.c.provider,
                        session_table.c.provider_session_id,
                        session_table.c.started_at,
                        session_table.c.last_activity_at,
                        session_table.c.ended_at,
                        session_table.c.user_messages,
                        session_table.c.assistant_messages,
                        session_table.c.tool_calls,
                        session_table.c.summary_title,
                        session_table.c.first_user_message_preview,
                        session_table.c.last_visible_text_preview,
                        session_table.c.subagent_parent_provider_session_id,
                        session_table.c.subagent_parent_tool_call_id,
                        session_table.c.subagent_run_id,
                    )
                    .where(
                        session_table.c.subagent_parent_session_id == session_id,
                        or_(
                            session_table.c.is_subagent == 1,
                            primary_worker_only_clause(session_table, LiveSessionThread.__table__),
                        ),
                        session_table.c.owner_id == owner_id,
                        session_table.c.machine_id == parent["machine_id"],
                        session_table.c.provider == parent["provider"],
                    )
                    .order_by(session_table.c.started_at.asc(), session_table.c.session_id.asc())
                    .limit(child_limit)
                )
                .mappings()
                .all()
            )
            child_ids = [str(row["session_id"]) for row in rows]
            alias_map: dict[str, str] = {
                str(row["session_id"]): str(row["provider_session_id"]) for row in rows if row["provider_session_id"]
            }
            if child_ids:
                alias = LiveSessionThreadAlias.__table__
                thread = LiveSessionThread.__table__
                for alias_row in connection.execute(
                    select(thread.c.session_id, alias.c.alias_value)
                    .select_from(alias.join(thread, thread.c.id == alias.c.thread_id))
                    .where(
                        thread.c.session_id.in_(child_ids),
                        alias.c.provider == parent["provider"],
                        alias.c.alias_kind == "provider_session_id",
                    )
                    .order_by(alias.c.id.asc())
                ).mappings():
                    alias_map.setdefault(str(alias_row["session_id"]), str(alias_row["alias_value"]))
            spawn_facts = _delegation_fact_rows(connection, session_id=session_id)
            references: list[dict[str, Any]] = []
            children_by_tool_call: dict[str, list[str]] = {}
            for row in rows:
                tool_call_id = str(row["subagent_parent_tool_call_id"] or "").strip()
                if tool_call_id:
                    children_by_tool_call.setdefault(tool_call_id, []).append(str(row["session_id"]))
            metadata_by_native: dict[str, dict[str, Any]] = {}
            for fact in spawn_facts:
                payload = _delegation_payload(fact)
                if not isinstance(payload, dict) or not isinstance(payload.get("children"), list):
                    continue
                for child in payload["children"]:
                    if not isinstance(child, Mapping):
                        continue
                    native_id = str(child.get("provider_session_id") or "").strip()
                    if not native_id:
                        continue
                    metadata = child.get("metadata") if isinstance(child.get("metadata"), dict) else {}
                    metadata_by_native.setdefault(native_id, metadata)
                    resolved = next((child_id for child_id, value in alias_map.items() if value == native_id), None)
                    if resolved is None:
                        parent_tool_call_id = str(child.get("parent_tool_call_id") or "").strip()
                        tool_candidates = children_by_tool_call.get(parent_tool_call_id, []) if parent_tool_call_id else []
                        if len(tool_candidates) == 1:
                            resolved = tool_candidates[0]
                    references.append(
                        {
                            "session_id": resolved,
                            "provider_session_id": native_id,
                            "parent_tool_call_id": child.get("parent_tool_call_id"),
                            "metadata": metadata,
                            "kind": fact["kind"],
                            "source_epoch": fact["source_epoch"],
                            "source_position": fact["source_position"],
                            "at": fact["at"],
                        }
                    )
                    if len(references) >= _SESSION_READ_DELEGATION_FACT_LIMIT:
                        break
                if len(references) >= _SESSION_READ_DELEGATION_FACT_LIMIT:
                    break
        children = [
            {
                "session_id": str(row["session_id"]),
                "provider": row["provider"],
                "provider_session_id": alias_map.get(str(row["session_id"])),
                "parent_tool_call_id": row["subagent_parent_tool_call_id"],
                "run_id": row["subagent_run_id"],
                "started_at": self._iso_or_none(row["started_at"]),
                "last_activity_at": self._iso_or_none(row["last_activity_at"]),
                "ended_at": self._iso_or_none(row["ended_at"]),
                "user_messages": int(row["user_messages"] or 0),
                "assistant_messages": int(row["assistant_messages"] or 0),
                "tool_calls": int(row["tool_calls"] or 0),
                "title": row["summary_title"],
                "first_user_message_preview": row["first_user_message_preview"],
                "last_visible_text_preview": row["last_visible_text_preview"],
                "metadata": metadata_by_native.get(alias_map.get(str(row["session_id"]), ""), {}),
            }
            for row in rows
        ]
        return {"found": True, "session_id": session_id, "children": children, "child_references": references}

    def resolve_session_alias(self, *, provider_session_id: str, owner_id: int) -> dict[str, Any]:
        """Resolve a provider-native session id alias to its Longhouse session id.

        Read-side counterpart of the ``provider_session_id`` thread-alias upserts:
        callers holding only the id a provider handed the user resolve it here.
        Primary-key lookups happen before this everywhere, so this never shadows
        a real Longhouse id.
        """

        observed_at = datetime.now(UTC)
        alias = LiveSessionThreadAlias.__table__
        thread = LiveSessionThread.__table__
        live = LiveSession.__table__
        storage = StorageSession.__table__
        with _read_snapshot(self.engine) as connection:
            row = (
                connection.execute(
                    select(thread.c.session_id)
                    .select_from(alias.join(thread, alias.c.thread_id == thread.c.id))
                    .where(alias.c.alias_kind == "provider_session_id")
                    .where(alias.c.alias_value == provider_session_id)
                    .where(
                        or_(
                            select(live.c.session_id)
                            .where(live.c.session_id == thread.c.session_id, live.c.owner_id == str(owner_id))
                            .exists(),
                            select(storage.c.session_id)
                            .where(storage.c.session_id == thread.c.session_id, storage.c.owner_id == str(owner_id))
                            .exists(),
                        )
                    )
                    .order_by(alias.c.last_seen_at.desc(), alias.c.id.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is None:
                candidates = (
                    connection.execute(
                        select(storage.c.session_id)
                        .where(storage.c.provider_session_id == provider_session_id, storage.c.owner_id == str(owner_id))
                        .limit(2)
                    )
                    .mappings()
                    .all()
                )
                row = candidates[0] if len(candidates) == 1 else None
            return {
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "found": row is not None,
                "session_id": str(row["session_id"]) if row is not None else None,
            }

    def list_owned_sessions(self, *, owner_id: str, after_session_id: str | None, limit: int) -> dict[str, Any]:
        """Page every session bound to one owner, including deleted ones.

        The timeline listing hides archived, snoozed, hidden, and tombstoned
        sessions. A user deleting their history means those too, so this listing
        applies no user_state filter and pages forward by session id: each row
        is visited once even while rows change underneath the walk.
        """

        sessions = StorageSession.__table__
        tombstones = LiveSessionTombstone.__table__
        with _read_snapshot(self.engine) as connection:
            statement = select(
                sessions.c.session_id,
                sessions.c.tenant_id,
                sessions.c.owner_id,
            ).where(sessions.c.owner_id == owner_id)
            if after_session_id is not None:
                statement = statement.where(sessions.c.session_id > after_session_id)
            found = list(connection.execute(statement.order_by(sessions.c.session_id.asc()).limit(limit + 1)).mappings().all())
            has_more = len(found) > limit
            found = found[:limit]
            rows = []
            for row in found:
                session_key = str(row["session_id"])
                deleted = connection.execute(select(tombstones.c.session_id).where(tombstones.c.session_id == session_key)).first()
                rows.append(
                    {
                        "session_id": session_key,
                        "tenant_id": str(row["tenant_id"]),
                        "owner_id": str(row["owner_id"]),
                        "deleted": deleted is not None,
                    }
                )
            return {
                "sessions": rows,
                "has_more": has_more,
                "commit_seq": str(_current_commit_seq(connection)),
            }
