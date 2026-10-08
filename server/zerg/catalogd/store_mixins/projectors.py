"""CatalogStore: projector state, lag claims, coverage and store bindings."""

from __future__ import annotations

import logging
import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import literal
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from zerg.catalogd.fact_reducer import _advance_commit_seq
from zerg.catalogd.fact_reducer import _current_commit_seq
from zerg.catalogd.models import ProjectorState
from zerg.catalogd.models import ProjectorStoreBinding
from zerg.catalogd.models import SessionTombstone as LiveSessionTombstone
from zerg.catalogd.models import StorageSession

# store.py imports this module after defining these, at its end.
from zerg.catalogd.store import _PROJECTOR_REPAIR_BATCH
from zerg.catalogd.store import _PROJECTOR_ROWS_BY_CLAIM_TOKEN
from zerg.catalogd.store import _SEARCH_CLAIM_NEWEST_FIRST_WALK
from zerg.catalogd.store import ACTIVE_PROJECTORS
from zerg.catalogd.store import KNOWN_PROJECTORS
from zerg.catalogd.store import PERMANENT_FAILURE_RETRY_INTERVAL
from zerg.catalogd.store import SEARCH_CLAIM_WALK_BACKLOG
from zerg.catalogd.store import SEARCH_CLAIM_WALK_RESTART_SECONDS
from zerg.catalogd.store import SEMANTIC_PROJECTOR_ID
from zerg.catalogd.store import ProjectorRepairPlan
from zerg.catalogd.store import _as_aware_utc
from zerg.catalogd.store import _encode_datetime
from zerg.catalogd.store import _projector_repair_predicates
from zerg.catalogd.store import _projector_state_dto
from zerg.catalogd.store import _read_snapshot
from zerg.catalogd.store import _write_transaction
from zerg.embedding_space import EMBEDDING_PROJECTOR_ID


class ProjectorsMixin:
    def refresh_projector_statistics(self) -> dict[str, int]:
        """Give the planner projector_state's real shape once per catalogd start.

        Nothing else analyzes the catalog, and the production statistics were
        taken at 196 rows: claim lookups scanned the projector (28 ms on the
        frozen owner catalog, 0.14 ms after this). A full ANALYZE, because a
        sampled one misjudges the mostly-NULL claim_token index the same way.
        """

        with _write_transaction(self.engine) as connection:
            connection.exec_driver_sql("ANALYZE projector_state")
            rows = connection.execute(select(func.count()).select_from(ProjectorState.__table__)).scalar_one()
        return {"rows": int(rows)}

    def scan_projector_repairs(self) -> ProjectorRepairPlan:
        """Find the sessions ``ensure_known_projector_states`` must repair.

        These scans visit every eligible session and every search-v2 row: 2.1 s
        on the 39,300-session owner catalog, all of it once held under
        catalogd's write lock before the socket was published. A read snapshot
        does not block the WAL writer, so catalogd runs this while serving.
        """

        p = _projector_repair_predicates()
        sessions, states = p.sessions, p.states
        with _read_snapshot(self.engine) as connection:

            def session_ids(statement) -> tuple[str, ...]:
                return tuple(str(session_id) for session_id in connection.execute(statement).scalars())

            def lacks(projector: str, session_id):
                return ~select(states.c.session_id).where(states.c.projector == projector, states.c.session_id == session_id).exists()

            return ProjectorRepairPlan(
                eligible_sessions=int(connection.execute(select(func.count()).select_from(sessions).where(*p.eligible)).scalar_one()),
                irrelevant_semantic=session_ids(select(states.c.session_id).where(*p.irrelevant_semantic)),
                missing_semantic=session_ids(
                    select(sessions.c.session_id).where(*p.semantic_candidates, lacks(SEMANTIC_PROJECTOR_ID, sessions.c.session_id))
                ),
                missing_search=session_ids(select(sessions.c.session_id).where(*p.eligible, lacks("search-v2", sessions.c.session_id))),
                missing_embeddings=session_ids(
                    select(p.search_rows.c.session_id).where(
                        *p.embedding_seed,
                        lacks(EMBEDDING_PROJECTOR_ID, p.search_rows.c.session_id),
                    )
                ),
                stale_render=session_ids(select(states.c.session_id).where(*p.stale_render)),
                misaligned_embeddings=session_ids(select(states.c.session_id).where(*p.alignment)),
                retired=session_ids(select(states.c.session_id).where(*p.retired).distinct()),
            )

    def ensure_known_projector_states(self, plan: ProjectorRepairPlan) -> dict[str, int]:
        """Backfill projector identities and preserve the search-derived chain.

        Embeddings consume searchd's published render, so every search-v2
        ledger row needs a matching active embedding row even when the catalog
        session has since moved from ``ready`` to ``pending`` for semantic
        repair. Restricting a newly bumped embedding identity to currently
        ready sessions left already-served search rows outside dense coverage.

        Only the sessions ``plan`` names are touched, and every statement
        re-checks the predicate that found them, so a session repaired or
        changed since the scan is left as its writer committed it. The write
        lasts as long as the repair is large, not as long as the catalog.
        """

        p = _projector_repair_predicates()
        sessions, states, search_rows = p.sessions, p.states, p.search_rows
        counts = {
            "inserted": 0,
            "eligible_sessions": plan.eligible_sessions,
            "reaped_irrelevant_semantic": 0,
            "aligned_embeddings": 0,
            "advanced_retired": 0,
            "advanced_render_consumers": 0,
        }
        if plan.is_empty():
            return counts
        insert_columns = (
            "projector",
            "session_id",
            "desired_revision",
            "desired_at",
            "completed_revision",
            "status",
            "failure_count",
            "commit_seq",
            "created_at",
            "updated_at",
        )
        # A search row this write inserts starts at the session revision, which
        # its current render can be past; the render scan never saw it. Raising
        # a search target must then realign its embedding row, and an inserted
        # search row can meet an embedding row seeded at another revision; the
        # alignment scan saw neither.
        search_touched = {*plan.stale_render, *plan.missing_search}
        with _write_transaction(self.engine) as connection:
            now = datetime.now(UTC)
            commit_seq = _current_commit_seq(connection)

            def per_batch(session_ids, statement_for) -> int:
                ordered = sorted(set(session_ids))
                changed = 0
                for start in range(0, len(ordered), _PROJECTOR_REPAIR_BATCH):
                    changed += int(connection.execute(statement_for(ordered[start : start + _PROJECTOR_REPAIR_BATCH])).rowcount or 0)
                return changed

            def insert_missing(projector, session_id, desired_revision, *where):
                seed = select(
                    literal(projector),
                    session_id,
                    desired_revision,
                    literal(now),
                    literal(0),
                    literal("idle"),
                    literal(0),
                    literal(commit_seq),
                    literal(now),
                    literal(now),
                ).where(*where)
                return (
                    sqlite_insert(states)
                    .from_select(insert_columns, seed)
                    .on_conflict_do_nothing(index_elements=[states.c.projector, states.c.session_id])
                )

            released_claim = {
                "desired_at": now,
                "claimed_revision": None,
                "claim_token": None,
                "worker_id": None,
                "claim_expires_at": None,
                "status": "idle",
                "retry_at": None,
                "commit_seq": commit_seq,
                "updated_at": now,
            }
            cleared_failure = {"failure_count": 0, "last_error_code": None, "last_error_message": None}
            counts["reaped_irrelevant_semantic"] = per_batch(
                plan.irrelevant_semantic,
                lambda batch: delete(states).where(*p.irrelevant_semantic, states.c.session_id.in_(batch)),
            )
            counts["inserted"] = per_batch(
                plan.missing_semantic,
                lambda batch: insert_missing(
                    SEMANTIC_PROJECTOR_ID,
                    sessions.c.session_id,
                    sessions.c.commit_seq,
                    *p.semantic_candidates,
                    sessions.c.session_id.in_(batch),
                ),
            )
            counts["inserted"] += per_batch(
                plan.missing_search,
                lambda batch: insert_missing(
                    "search-v2",
                    sessions.c.session_id,
                    sessions.c.commit_seq,
                    *p.eligible,
                    sessions.c.session_id.in_(batch),
                ),
            )
            # A search row is the durable certificate that this session has
            # entered the served corpus. Mirror that ledger directly instead
            # of materializing both whole tables into Python dictionaries.
            counts["inserted"] += per_batch(
                {*plan.missing_embeddings, *plan.missing_search},
                lambda batch: insert_missing(
                    EMBEDDING_PROJECTOR_ID,
                    search_rows.c.session_id,
                    search_rows.c.desired_revision,
                    *p.embedding_seed,
                    search_rows.c.session_id.in_(batch),
                ),
            )
            counts["advanced_render_consumers"] = per_batch(
                search_touched,
                lambda batch: update(states)
                .where(*p.stale_render, states.c.session_id.in_(batch))
                .values(desired_revision=p.session_revision, **released_claim, **cleared_failure),
            )
            counts["aligned_embeddings"] = per_batch(
                {*plan.misaligned_embeddings, *search_touched},
                lambda batch: update(states)
                .where(*p.alignment, states.c.session_id.in_(batch))
                .values(desired_revision=p.search_revision, **released_claim, **cleared_failure),
            )
            counts["advanced_retired"] = per_batch(
                plan.retired,
                lambda batch: update(states)
                .where(*p.retired, states.c.session_id.in_(batch))
                .values(desired_revision=p.retired_revision, **released_claim),
            )
        return counts

    def advance_projector_state(
        self,
        *,
        projector: str,
        session_id: UUID,
        desired_revision: int,
        observed_at: datetime,
    ) -> dict[str, Any]:
        table = ProjectorState.__table__
        tombstones = LiveSessionTombstone.__table__
        session_key = str(session_id)
        with _write_transaction(self.engine) as connection:
            deletion_revision = connection.execute(
                select(tombstones.c.deletion_revision).where(tombstones.c.session_id == session_key)
            ).scalar_one_or_none()
            if deletion_revision is not None:
                return {
                    "session_deleted": True,
                    "deletion_revision": str(deletion_revision),
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            row = (
                connection.execute(select(table).where(table.c.projector == projector, table.c.session_id == session_key))
                .mappings()
                .first()
            )
            if row is not None and int(row["desired_revision"]) >= desired_revision:
                return {
                    "changed": False,
                    "state": _projector_state_dto(row),
                    "commit_seq": str(row["commit_seq"]),
                }
            commit_time = datetime.now(UTC)
            commit_seq = _advance_commit_seq(connection, commit_time)
            if row is None:
                connection.execute(
                    insert(table).values(
                        projector=projector,
                        session_id=session_key,
                        desired_revision=desired_revision,
                        desired_at=commit_time,
                        completed_revision=0,
                        status="idle",
                        failure_count=0,
                        commit_seq=commit_seq,
                        created_at=commit_time,
                        updated_at=commit_time,
                    )
                )
            else:
                # New desired data invalidates the previous failure verdict:
                # it was reached against input that has since changed, so the
                # row should be retried now rather than waiting out a backoff
                # earned by a different revision. This previously keyed on
                # "quarantined" alone, which is no longer written, so it would
                # have stopped resetting anything at all.
                reset_failure = row["status"] in ("failed", "quarantined")
                values = {
                    "desired_revision": desired_revision,
                    "desired_at": commit_time,
                    "commit_seq": commit_seq,
                    "updated_at": commit_time,
                }
                if reset_failure:
                    values.update(
                        {
                            "status": "idle",
                            "failure_count": 0,
                            "last_error_code": None,
                            "last_error_message": None,
                            "retry_at": None,
                        }
                    )
                connection.execute(update(table).where(table.c.projector == projector, table.c.session_id == session_key).values(**values))
            updated = (
                connection.execute(select(table).where(table.c.projector == projector, table.c.session_id == session_key)).mappings().one()
            )
            return {
                "changed": True,
                "state": _projector_state_dto(updated),
                "commit_seq": str(commit_seq),
            }

    def claim_projector_lag(
        self,
        *,
        projector: str,
        worker_id: str,
        claim_token: str,
        now: datetime,
        lease_seconds: int,
        limit: int,
    ) -> dict[str, Any]:
        table = ProjectorState.__table__
        tombstones = LiveSessionTombstone.__table__
        eligible_predicates = [
            table.c.projector == projector,
            table.c.desired_revision > table.c.completed_revision,
            # No status exclusion. "quarantined" is no longer written (see
            # fail_projector_claim); rows still carrying it from before that
            # change have retry_at=NULL, so dropping the exclusion makes
            # them immediately claimable and they heal on the next tick
            # rather than needing an operator to call
            # projector.state.requeue.v2 by hand.
            or_(table.c.claim_expires_at.is_(None), table.c.claim_expires_at <= now),
            or_(table.c.retry_at.is_(None), table.c.retry_at <= now),
        ]
        if projector != "search-v2":
            eligible_predicates.append(~select(tombstones.c.session_id).where(tombstones.c.session_id == table.c.session_id).exists())

        def replay_result(connection) -> dict[str, Any] | None:
            replay_rows = (
                connection.execute(_PROJECTOR_ROWS_BY_CLAIM_TOKEN, {"projector": projector, "claim_token": claim_token}).mappings().all()
            )
            if replay_rows:
                if any(row["worker_id"] != worker_id for row in replay_rows):
                    return {"claim_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                return {
                    "claimed": [_projector_state_dto(row) for row in replay_rows],
                    "exact_replay": True,
                    "commit_seq": str(replay_rows[0]["commit_seq"]),
                }
            # Keep these as two point lookups. SQLite otherwise chooses the
            # broad projector index for the OR expression and scans the full
            # projector history instead of using the token indexes.
            terminal_replay = None
            for terminal_token in (table.c.last_completion_token, table.c.last_failure_token):
                terminal_replay = connection.execute(
                    select(table.c.commit_seq).where(
                        table.c.projector == projector,
                        terminal_token == claim_token,
                    )
                ).first()
                if terminal_replay is not None:
                    break
            if terminal_replay is not None:
                return {
                    "claimed": [],
                    "exact_replay": True,
                    "commit_seq": str(terminal_replay[0]),
                }
            return None

        def eligible_rows(connection, row_limit: int, newest_first: bool = False):
            if projector == EMBEDDING_PROJECTOR_ID:
                # Embeddings read episode text from searchd's *published* render
                # generation, so a session is only workable up to the revision
                # search-v2 has actually completed. This used to require
                # search-v2 to have reached the embedding row's desired revision.
                # Both rows take desired_revision from the same catalog
                # watermark, so that reduced to "search-v2 has zero lag on this
                # session" -- and a session lags embeddings precisely when it
                # also lags search-v2. The claim could only fire in the instant
                # between search-v2 completing and the next write arriving, so
                # any session under active projection was never claimable and
                # the embedding projector inherited search-v2's entire backlog.
                #
                # Claim at what search-v2 has published instead. The row stays
                # `desired > completed` and is re-claimed as search-v2 advances,
                # so the projector always makes progress on the newest text that
                # actually exists to embed.
                search_state = table.alias("search_state")
                return (
                    connection.execute(
                        select(table, search_state.c.completed_revision.label("search_completed_revision"))
                        .join(
                            search_state,
                            and_(
                                search_state.c.projector == "search-v2",
                                search_state.c.session_id == table.c.session_id,
                                search_state.c.completed_revision > table.c.completed_revision,
                            ),
                        )
                        .where(*eligible_predicates)
                        .order_by(table.c.updated_at.asc(), table.c.session_id.asc())
                        .limit(row_limit)
                    )
                    .mappings()
                    .all()
                )
            statement = select(table).where(*eligible_predicates)
            if newest_first:
                backlog = connection.execute(
                    select(func.count()).select_from(
                        select(table.c.session_id).where(*eligible_predicates).limit(SEARCH_CLAIM_WALK_BACKLOG).subquery()
                    )
                ).scalar_one()
                if backlog >= SEARCH_CLAIM_WALK_BACKLOG:
                    if time.monotonic() - self._search_walk_started >= SEARCH_CLAIM_WALK_RESTART_SECONDS:
                        self._search_walk_floor = None
                        self._search_walk_started = time.monotonic()
                    rows = (
                        connection.execute(
                            _SEARCH_CLAIM_NEWEST_FIRST_WALK,
                            {"now": now, "row_limit": row_limit, "floor": self._search_walk_floor},
                        )
                        .mappings()
                        .all()
                    )
                    if len(rows) < row_limit:
                        # Fell off the bottom: rescan from the newest session next time.
                        self._search_walk_started = 0.0
                    elif rows:
                        self._search_walk_floor = rows[0]["walk_activity_at"]
                    return rows
                sessions = StorageSession.__table__
                # Rebuilds should make recent history playable first. The
                # activity tie-break preserves deterministic progress among
                # sessions with the same latest event time.
                statement = statement.outerjoin(sessions, sessions.c.session_id == table.c.session_id).order_by(
                    sessions.c.last_activity_at.desc(), table.c.updated_at.asc(), table.c.session_id.asc()
                )
            else:
                statement = statement.order_by(table.c.updated_at.asc(), table.c.session_id.asc())
            return connection.execute(statement.limit(row_limit)).mappings().all()

        # Idle pollers dominate the steady state. Do the exact eligibility and
        # replay checks on a read connection first so a poll with nothing to do
        # never enters SQLite's single-writer lane. A newly eligible row that
        # arrives after this check is picked up on the next bounded poll.
        with self.engine.connect() as connection:
            replay = replay_result(connection)
            if replay is not None:
                return replay
            if not eligible_rows(connection, 1):
                return {
                    "claimed": [],
                    "exact_replay": False,
                    "commit_seq": str(_current_commit_seq(connection)),
                }

        # Eligibility can change before the reservation is acquired, so repeat
        # every decision that matters under the write transaction.
        with _write_transaction(self.engine) as connection:
            replay = replay_result(connection)
            if replay is not None:
                return replay
            # search-v2 alternates oldest-lag-first with newest-activity-first:
            # recent history becomes playable early in a rebuild, and a session
            # that keeps revising (live work during an import) takes at most
            # every other claim instead of starving the backlog.
            newest_first = projector == "search-v2" and self._search_claim_turn % 2 == 1
            eligible = eligible_rows(connection, limit, newest_first)
            if projector == "search-v2" and eligible:
                self._search_claim_turn += 1
            if not eligible:
                return {
                    "claimed": [],
                    "exact_replay": False,
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            commit_time = datetime.now(UTC)
            commit_seq = _advance_commit_seq(connection, commit_time)
            expires_at = now + timedelta(seconds=lease_seconds)
            for row in eligible:
                session_key = row["session_id"]
                # For embeddings this is search-v2's published revision, which is
                # the newest text searchd can actually serve for this session.
                # Every other projector claims its own desired revision. Bounded
                # by desired_revision because `complete_projector_claim` rejects
                # a completion past it, and the two projectors' desired
                # revisions are not guaranteed to advance in lockstep.
                search_completed = row.get("search_completed_revision")
                claimed_revision = min(int(search_completed), int(row["desired_revision"])) if search_completed else row["desired_revision"]
                values = {
                    "claimed_revision": claimed_revision,
                    "claim_token": claim_token,
                    "worker_id": worker_id,
                    "claim_expires_at": expires_at,
                    "status": "claimed",
                    "retry_at": None,
                    "commit_seq": commit_seq,
                    "updated_at": commit_time,
                }
                if row["status"] == "claimed":
                    # Taking over a lease that ran out. The previous pass neither
                    # completed nor failed, so nothing else records that it was
                    # abandoned -- and a pass that is too slow to finish inside
                    # its lease will be abandoned again on the next tick, forever,
                    # while every observable field says "idle, zero failures".
                    values["failure_count"] = int(row["failure_count"] or 0) + 1
                    values["last_error_code"] = "claim_lease_expired"
                    values["last_error_message"] = f"a pass held by {row['worker_id']} did not finish inside its lease"
                    logging.getLogger(__name__).warning(
                        "Reclaiming expired projector lease projector=%s session=%s previous_worker=%s failures=%d",
                        projector,
                        session_key,
                        row["worker_id"],
                        values["failure_count"],
                    )
                connection.execute(update(table).where(table.c.projector == projector, table.c.session_id == session_key).values(**values))
            claimed_rows = (
                connection.execute(_PROJECTOR_ROWS_BY_CLAIM_TOKEN, {"projector": projector, "claim_token": claim_token}).mappings().all()
            )
            claimed_by_session = {str(row["session_id"]): row for row in claimed_rows}
            claimed = [claimed_by_session[str(row["session_id"])] for row in eligible]
            return {
                "claimed": [_projector_state_dto(row) for row in claimed],
                "exact_replay": False,
                "commit_seq": str(commit_seq),
            }

    def release_projector_claims_on_startup(
        self,
        *,
        active_worker_ids: tuple[str, ...],
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Release leases whose workers cannot prove ownership across this daemon generation."""

        table = ProjectorState.__table__
        stale_claim = and_(table.c.status == "claimed", table.c.projector.in_(KNOWN_PROJECTORS))
        if active_worker_ids:
            stale_claim = and_(stale_claim, or_(table.c.worker_id.is_(None), ~table.c.worker_id.in_(active_worker_ids)))
        with _write_transaction(self.engine) as connection:
            claimed_count = int(connection.execute(select(func.count()).where(stale_claim)).scalar_one())
            if claimed_count == 0:
                return {"released": 0, "commit_seq": str(_current_commit_seq(connection))}
            commit_seq = _advance_commit_seq(connection, observed_at)
            connection.execute(
                update(table)
                .where(stale_claim)
                .values(
                    claimed_revision=None,
                    claim_token=None,
                    worker_id=None,
                    claim_expires_at=None,
                    status="idle",
                    # Say why. A released claim used to reset to a plain idle row
                    # with failure_count=0 and no error code, which is
                    # indistinguishable from a session that was never attempted.
                    # A projector that had been abandoning the same session for
                    # hours therefore left no trace anywhere, and finding it took
                    # a live query against the projector table.
                    last_error_code="claim_released_worker_gone",
                    last_error_message="the claiming worker did not survive this daemon generation",
                    commit_seq=commit_seq,
                    updated_at=observed_at,
                )
            )
            logging.getLogger(__name__).warning("Released %d projector claim(s) whose worker did not survive restart", claimed_count)
            return {"released": claimed_count, "commit_seq": str(commit_seq)}

    def complete_projector_claim(
        self,
        *,
        projector: str,
        session_id: UUID,
        claim_token: str,
        completed_revision: int,
        completed_at: datetime,
    ) -> dict[str, Any]:
        table = ProjectorState.__table__
        session_key = str(session_id)
        with _write_transaction(self.engine) as connection:
            row = (
                connection.execute(select(table).where(table.c.projector == projector, table.c.session_id == session_key))
                .mappings()
                .first()
            )
            if row is None:
                return {"claim_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
            if row["last_completion_token"] == claim_token and int(row["completed_revision"]) >= completed_revision:
                return {
                    "changed": False,
                    "exact_replay": True,
                    "state": _projector_state_dto(row),
                    "commit_seq": str(row["commit_seq"]),
                }
            if (
                row["claim_token"] != claim_token
                or row["claimed_revision"] is None
                or int(row["claimed_revision"]) != completed_revision
                or completed_revision > int(row["desired_revision"])
            ):
                return {"claim_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
            commit_time = datetime.now(UTC)
            commit_seq = _advance_commit_seq(connection, commit_time)
            connection.execute(
                update(table)
                .where(table.c.projector == projector, table.c.session_id == session_key)
                .values(
                    completed_revision=completed_revision,
                    claimed_revision=None,
                    claim_token=None,
                    worker_id=None,
                    claim_expires_at=None,
                    status="idle",
                    failure_count=0,
                    last_error_code=None,
                    last_error_message=None,
                    retry_at=None,
                    last_completion_token=claim_token,
                    commit_seq=commit_seq,
                    updated_at=commit_time,
                )
            )
            updated = (
                connection.execute(select(table).where(table.c.projector == projector, table.c.session_id == session_key)).mappings().one()
            )
            return {
                "changed": True,
                "exact_replay": False,
                "state": _projector_state_dto(updated),
                "commit_seq": str(commit_seq),
            }

    def fail_projector_claim(
        self,
        *,
        projector: str,
        session_id: UUID,
        claim_token: str,
        error_code: str,
        error_message: str | None,
        failed_at: datetime,
        retry_at: datetime,
    ) -> dict[str, Any]:
        table = ProjectorState.__table__
        session_key = str(session_id)
        with _write_transaction(self.engine) as connection:
            row = (
                connection.execute(select(table).where(table.c.projector == projector, table.c.session_id == session_key))
                .mappings()
                .first()
            )
            if row is None:
                return {"claim_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
            if row["last_failure_token"] == claim_token:
                return {
                    "changed": False,
                    "exact_replay": True,
                    "state": _projector_state_dto(row),
                    "commit_seq": str(row["commit_seq"]),
                }
            if row["claim_token"] != claim_token or row["claimed_revision"] is None:
                return {"claim_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
            commit_time = datetime.now(UTC)
            commit_seq = _advance_commit_seq(connection, commit_time)
            # A projector row is derived state, so no failure is terminal. A
            # "permanent" error means *this build* cannot project *this input*
            # -- a statement about code, not about data. Quarantining the row
            # made that verdict outlive the build that issued it: the row was
            # dropped from claim eligibility with retry_at=NULL, so the deploy
            # that fixed the parser never re-examined it and no counter showed
            # it. One 95 MB archive sat quarantined for six days that way, and
            # dragged the whole coverage watermark back with it.
            #
            # Permanent errors now take a long backoff instead. The next
            # deploy re-attempts them for free, a genuinely unprojectable row
            # costs one claim per PERMANENT_FAILURE_RETRY_INTERVAL, and
            # "stuck forever" stops being a reachable state.
            permanent = error_code.endswith("_permanent")
            effective_retry_at = commit_time + PERMANENT_FAILURE_RETRY_INTERVAL if permanent else retry_at
            connection.execute(
                update(table)
                .where(table.c.projector == projector, table.c.session_id == session_key)
                .values(
                    claimed_revision=None,
                    claim_token=None,
                    worker_id=None,
                    claim_expires_at=None,
                    status="failed",
                    failure_count=int(row["failure_count"] or 0) + 1,
                    last_error_code=error_code,
                    last_error_message=error_message,
                    retry_at=effective_retry_at,
                    last_failure_token=claim_token,
                    commit_seq=commit_seq,
                    updated_at=commit_time,
                )
            )
            updated = (
                connection.execute(select(table).where(table.c.projector == projector, table.c.session_id == session_key)).mappings().one()
            )
            return {
                "changed": True,
                "exact_replay": False,
                "state": _projector_state_dto(updated),
                "commit_seq": str(commit_seq),
            }

    def list_projector_lag(
        self,
        *,
        projector: str,
        after_session_id: str | None,
        limit: int,
    ) -> dict[str, Any]:
        table = ProjectorState.__table__
        tombstones = LiveSessionTombstone.__table__
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            lag_predicate = [
                table.c.projector == projector,
                table.c.desired_revision > table.c.completed_revision,
            ]
            if projector != "search-v2":
                lag_predicate.append(~select(tombstones.c.session_id).where(tombstones.c.session_id == table.c.session_id).exists())
            statement = select(table).where(*lag_predicate)
            if after_session_id is not None:
                statement = statement.where(table.c.session_id > after_session_id)
            rows = connection.execute(statement.order_by(table.c.session_id.asc()).limit(limit)).mappings().all()
            lag_count, first_lag_revision = connection.execute(
                select(func.count(), func.min(table.c.desired_revision)).where(*lag_predicate)
            ).one()
            commit_seq = _current_commit_seq(connection)
            return {
                "states": [_projector_state_dto(row) for row in rows],
                "lag_count": int(lag_count),
                "indexed_through": (str(int(first_lag_revision) - 1) if first_lag_revision is not None else str(commit_seq)),
                "commit_seq": str(commit_seq),
                "observed_at": observed_at.isoformat(),
            }

    def read_projector_coverage(self, *, projector: str) -> dict[str, Any]:
        """Read cutover proof and the mutable head in one catalog snapshot."""

        states = ProjectorState.__table__
        bindings = ProjectorStoreBinding.__table__
        tombstones = LiveSessionTombstone.__table__
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            lag_predicate = [
                states.c.projector == projector,
                states.c.desired_revision > states.c.completed_revision,
            ]
            if projector != "search-v2":
                lag_predicate.append(~select(tombstones.c.session_id).where(tombstones.c.session_id == states.c.session_id).exists())
            lag_count, first_lag_revision, oldest_lag_at = connection.execute(
                select(
                    func.count(),
                    func.min(states.c.desired_revision),
                    func.min(func.coalesce(states.c.desired_at, states.c.created_at)),
                ).where(*lag_predicate)
            ).one()
            binding = connection.execute(select(bindings).where(bindings.c.projector == projector)).mappings().first()
            commit_seq = _current_commit_seq(connection)
            oldest = _as_aware_utc(oldest_lag_at)
            return {
                "projector": projector,
                "store_binding": (
                    {
                        "store_id": str(binding["store_id"]),
                        "schema_generation": str(binding["schema_generation"]),
                        "commit_seq": str(binding["commit_seq"]),
                    }
                    if binding is not None
                    else None
                ),
                "lag_count": int(lag_count),
                "indexed_through": (str(int(first_lag_revision) - 1) if first_lag_revision is not None else str(commit_seq)),
                "oldest_lag_at": _encode_datetime(oldest),
                "oldest_lag_seconds": (max(0.0, (observed_at - oldest).total_seconds()) if oldest is not None else None),
                "commit_seq": str(commit_seq),
                "observed_at": observed_at.isoformat(),
            }

    def reap_retired_projector_states(self) -> dict[str, Any]:
        """Delete projector rows whose projector name no longer exists.

        EMBEDDING_PROJECTOR_ID embeds the embedding artifact revision and a
        partition suffix, so changing either renames the projector. Every row
        under the old name is then stranded: no worker polls that name again,
        the rows keep whatever status they last had, and nothing reports them.
        Three retired generations were holding ~69k rows this way, two of them
        `failed` since a fix sixteen days earlier.

        Projector state is derived from the catalog and is rebuilt by claiming
        the row, so deleting a retired generation loses nothing. Rows for a
        live projector are never touched.
        """

        table = ProjectorState.__table__
        observed_at = datetime.now(UTC)
        # Runs on every catalogd start, and almost every start has nothing to
        # reap, so look first in a read snapshot. Taking the writer just to
        # discover "no retired generations" would put a lock acquisition on the
        # startup path of every daemon for no benefit.
        with _read_snapshot(self.engine) as connection:
            retired = [
                str(row.projector)
                for row in connection.execute(select(table.c.projector).distinct().where(table.c.projector.notin_(ACTIVE_PROJECTORS))).all()
            ]
            if not retired:
                return {
                    "reaped_projectors": [],
                    "reaped_rows": 0,
                    "commit_seq": str(_current_commit_seq(connection)),
                    "observed_at": observed_at.isoformat(),
                }
        with _write_transaction(self.engine) as connection:
            result = connection.execute(delete(table).where(table.c.projector.in_(retired)))
            commit_time = datetime.now(UTC)
            commit_seq = _advance_commit_seq(connection, commit_time)
            return {
                "reaped_projectors": sorted(retired),
                "reaped_rows": int(result.rowcount or 0),
                "commit_seq": str(commit_seq),
                "observed_at": observed_at.isoformat(),
            }

    def requeue_projector_states(
        self,
        *,
        projector: str,
        session_ids: list[UUID],
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Requeue explicitly named states against a fresh catalog snapshot."""

        table = ProjectorState.__table__
        session_keys = sorted(str(session_id) for session_id in session_ids)
        with _write_transaction(self.engine) as connection:
            rows = (
                connection.execute(select(table).where(table.c.projector == projector, table.c.session_id.in_(session_keys)))
                .mappings()
                .all()
            )
            by_session = {str(row["session_id"]): row for row in rows}
            missing = [session_id for session_id in session_keys if session_id not in by_session]
            if missing:
                return {
                    "changed": False,
                    "requeued_session_ids": [],
                    "missing_session_ids": missing,
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            snapshot_revision = _current_commit_seq(connection)
            changed = [
                session_id
                for session_id in session_keys
                if int(by_session[session_id]["completed_revision"]) != 0
                or int(by_session[session_id]["desired_revision"]) < snapshot_revision
                or by_session[session_id]["claimed_revision"] is not None
                or by_session[session_id]["status"] != "idle"
                or int(by_session[session_id]["failure_count"] or 0) != 0
                or by_session[session_id]["last_error_code"] is not None
                or by_session[session_id]["last_error_message"] is not None
                or by_session[session_id]["retry_at"] is not None
            ]
            if not changed:
                return {
                    "changed": False,
                    "requeued_session_ids": [],
                    "missing_session_ids": [],
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            commit_seq = _advance_commit_seq(connection, observed_at)
            connection.execute(
                update(table)
                .where(table.c.projector == projector, table.c.session_id.in_(changed))
                .values(
                    desired_revision=func.max(table.c.desired_revision, commit_seq),
                    desired_at=observed_at,
                    completed_revision=0,
                    claimed_revision=None,
                    claim_token=None,
                    worker_id=None,
                    claim_expires_at=None,
                    status="idle",
                    failure_count=0,
                    last_error_code=None,
                    last_error_message=None,
                    retry_at=None,
                    last_completion_token=None,
                    last_failure_token=None,
                    commit_seq=commit_seq,
                    updated_at=observed_at,
                )
            )
            if projector == "search-v2":
                # Search publication fences embeddings to the same revision.
                # Keep existing vectors/completions, but make their publication
                # eligible again once search has completed this fresh snapshot.
                connection.execute(
                    update(table)
                    .where(table.c.projector == EMBEDDING_PROJECTOR_ID, table.c.session_id.in_(changed))
                    .values(
                        desired_revision=func.max(table.c.desired_revision, commit_seq),
                        desired_at=observed_at,
                        commit_seq=commit_seq,
                        updated_at=observed_at,
                    )
                )
            return {
                "changed": True,
                "requeued_session_ids": changed,
                "missing_session_ids": [],
                "commit_seq": str(commit_seq),
            }

    def bind_projector_store(
        self,
        *,
        projector: str,
        store_id: UUID,
        schema_generation: str,
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Invalidate completed rows exactly once when a disposable store is replaced."""

        bindings = ProjectorStoreBinding.__table__
        states = ProjectorState.__table__
        store_key = str(store_id)
        with _write_transaction(self.engine) as connection:
            existing = connection.execute(select(bindings).where(bindings.c.projector == projector)).mappings().first()
            if existing is not None and str(existing["store_id"]) == store_key and str(existing["schema_generation"]) == schema_generation:
                return {
                    "changed": False,
                    "invalidated_states": 0,
                    "store_id": store_key,
                    "schema_generation": schema_generation,
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            commit_seq = _advance_commit_seq(connection, observed_at)
            invalidated = connection.execute(
                update(states)
                .where(states.c.projector == projector)
                .values(
                    completed_revision=0,
                    claimed_revision=None,
                    claim_token=None,
                    worker_id=None,
                    claim_expires_at=None,
                    status="idle",
                    failure_count=0,
                    last_error_code=None,
                    last_error_message=None,
                    retry_at=None,
                    last_completion_token=None,
                    last_failure_token=None,
                    commit_seq=commit_seq,
                    updated_at=observed_at,
                )
            ).rowcount
            if existing is None:
                connection.execute(
                    insert(bindings).values(
                        projector=projector,
                        store_id=store_key,
                        schema_generation=schema_generation,
                        commit_seq=commit_seq,
                        created_at=observed_at,
                        updated_at=observed_at,
                    )
                )
            else:
                connection.execute(
                    update(bindings)
                    .where(bindings.c.projector == projector)
                    .values(
                        store_id=store_key,
                        schema_generation=schema_generation,
                        commit_seq=commit_seq,
                        updated_at=observed_at,
                    )
                )
            return {
                "changed": True,
                "invalidated_states": int(invalidated or 0),
                "store_id": store_key,
                "schema_generation": schema_generation,
                "commit_seq": str(commit_seq),
            }
