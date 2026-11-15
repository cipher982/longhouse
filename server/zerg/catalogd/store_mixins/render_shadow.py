"""Host render shadow: the inputs a rebuild needs and the differential it records.

See control-plane `docs/specs/host-normalized-transcripts.md`, Phase 3.
"""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import literal
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from zerg.catalogd.fact_reducer import _advance_commit_seq
from zerg.catalogd.models import MediaObject
from zerg.catalogd.models import ProjectorState
from zerg.catalogd.models import RawObject
from zerg.catalogd.models import RenderGeneration
from zerg.catalogd.models import RenderObject
from zerg.catalogd.models import RenderShadowEnvelope
from zerg.catalogd.models import SessionMediaRef
from zerg.catalogd.models import SessionProviderFact
from zerg.catalogd.models import SessionTombstone
from zerg.catalogd.models import SourceEpoch
from zerg.catalogd.models import StorageSession
from zerg.catalogd.store import _promote_generation_if_covering
from zerg.catalogd.store import _read_snapshot
from zerg.catalogd.store import _recompute_render_generation_projection
from zerg.catalogd.store import _render_order_columns
from zerg.catalogd.store import _write_transaction

# A session this large is rebuilt by Phase 4's incremental path, not sampled.
MAX_SHADOW_RAW_OBJECTS = 20_000
SHADOW_SESSION_COOLDOWN = timedelta(minutes=30)


def _rfc3339(value: object) -> str:
    """A stored time as the RFC 3339 text the engine parses.

    SQLite hands DateTime columns back as naive text ("2026-11-14 17:56:42.158704");
    the catalog writes UTC, so a missing offset means UTC.
    """
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return (moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)).isoformat()


SHADOW_OUTCOMES = (
    "match",
    "mismatch",
    "revision_skew",
    "host_rendered",
    "no_witness",
    "raw_only",
    "too_large",
    "media_missing",
    "error",
)
# Outcomes a newer normalizer revision must answer again. An error is retried
# after the cooldown whatever the revision. `revision_skew` means the laptop
# render was made by another parser revision than the host's, so the two are
# not comparable until one of them moves.
# `host_rendered`: the host replaced the laptop render with its own (Phase 4).
REVISION_BOUND_OUTCOMES = ("match", "mismatch", "revision_skew", "host_rendered")


class RenderShadowMixin:
    def read_render_rebuild_inputs(self, *, session_id: str) -> dict[str, Any]:
        """Everything a host rebuild of one session reads, in one snapshot."""
        raw = RawObject.__table__
        epochs = SourceEpoch.__table__
        facts = SessionProviderFact.__table__
        render = RenderObject.__table__
        generations = RenderGeneration.__table__
        refs = SessionMediaRef.__table__
        media = MediaObject.__table__
        sessions = StorageSession.__table__
        with _read_snapshot(self.engine) as connection:
            session = connection.execute(select(sessions).where(sessions.c.session_id == session_id)).mappings().first()
            if session is None:
                return {"found": False}
            shadow = RenderShadowEnvelope.__table__
            checked = {
                str(row["envelope_id"]): {"outcome": row["outcome"], "revision": row["normalizer_revision"]}
                for row in connection.execute(
                    select(shadow.c.envelope_id, shadow.c.outcome, shadow.c.normalizer_revision).where(shadow.c.session_id == session_id)
                ).mappings()
            }
            raw_count = connection.execute(
                select(func.count()).where(raw.c.session_id == session_id, raw.c.retired_at.is_(None))
            ).scalar_one()
            if raw_count > MAX_SHADOW_RAW_OBJECTS:
                envelopes = connection.execute(
                    select(raw.c.envelope_id, raw.c.provider, raw.c.source_epoch, raw.c.range_start).where(
                        raw.c.session_id == session_id, raw.c.retired_at.is_(None)
                    )
                ).mappings()
                return {
                    "found": True,
                    "too_large": True,
                    "checked": checked,
                    "raw_objects": [{**dict(row), "range_start": int(row["range_start"])} for row in envelopes],
                }
            raw_rows = (
                connection.execute(
                    select(
                        raw.c.envelope_id,
                        raw.c.tenant_id,
                        raw.c.provider,
                        raw.c.machine_id,
                        raw.c.opaque_source_id,
                        raw.c.source_epoch,
                        raw.c.range_kind,
                        raw.c.provenance_kind,
                        raw.c.range_start,
                        raw.c.range_end,
                        raw.c.object_path,
                        raw.c.object_hash,
                    )
                    .where(raw.c.session_id == session_id, raw.c.retired_at.is_(None))
                    .order_by(raw.c.machine_id, raw.c.provider, raw.c.opaque_source_id, raw.c.source_epoch, raw.c.range_start)
                )
                .mappings()
                .all()
            )
            epoch_ids = sorted({str(row["source_epoch"]) for row in raw_rows})
            opened = {
                str(row["source_epoch"]): row["opened_at"]
                for row in connection.execute(
                    select(epochs.c.source_epoch, epochs.c.opened_at).where(epochs.c.source_epoch.in_(epoch_ids))
                ).mappings()
            }
            fact_rows = (
                connection.execute(
                    select(facts.c.kind, facts.c.at, facts.c.source_epoch, facts.c.source_position, facts.c.payload_json)
                    .where(facts.c.session_id == session_id)
                    .order_by(facts.c.source_epoch, facts.c.source_position, facts.c.kind)
                )
                .mappings()
                .all()
            )
            generation_id = session["current_render_generation"]
            # Each envelope's laptop render from the current generation, or
            # from the newest generation holding one: a generation started by a
            # laptop parser bump need not re-render every older envelope.
            render_rows: dict[str, dict[str, Any]] = {}
            for row in connection.execute(
                select(
                    render.c.source_envelope_id,
                    render.c.object_path,
                    render.c.object_hash,
                    render.c.event_count,
                    generations.c.generation_id,
                    generations.c.parser_revision,
                    generations.c.commit_seq,
                )
                .select_from(render.join(generations, generations.c.generation_id == render.c.generation_id))
                .where(render.c.session_id == session_id, render.c.retired_at.is_(None))
            ).mappings():
                key = str(row["source_envelope_id"])
                rank = (row["generation_id"] == generation_id, int(row["commit_seq"]))
                held = render_rows.get(key)
                if held is None or rank > held["_rank"]:
                    render_rows[key] = {**dict(row), "_rank": rank}
            media_rows = (
                connection.execute(
                    select(refs.c.envelope_id, refs.c.media_hash, refs.c.ref_key, media.c.object_path, media.c.state)
                    .select_from(refs.join(media, media.c.media_hash == refs.c.media_hash))
                    .where(refs.c.session_id == session_id, refs.c.retired_at.is_(None))
                )
                .mappings()
                .all()
            )
        return {
            "found": True,
            "provider": session["provider"],
            "owner_id": session["owner_id"],
            "current_render_generation": generation_id,
            "raw_objects": [
                {key: (int(value) if key in {"range_start", "range_end"} else value) for key, value in row.items()} for row in raw_rows
            ],
            "epoch_opened_at": {key: _rfc3339(value) for key, value in opened.items()},
            "facts": [
                {
                    "kind": row["kind"],
                    "at": _rfc3339(row["at"]),
                    "source_epoch": row["source_epoch"],
                    "source_position": int(row["source_position"]),
                    "payload": json.loads(row["payload_json"]),
                }
                for row in fact_rows
            ],
            "render_objects": [
                {key: value for key, value in row.items() if key not in {"_rank", "commit_seq"}} for row in render_rows.values()
            ],
            "media": [dict(row) for row in media_rows],
            "checked": checked,
        }

    def commit_host_render_objects(
        self,
        *,
        session_id: str,
        generation_id: str,
        parser_revision: str,
        ordering_revision: str,
        objects: tuple[dict[str, Any], ...],
        promote: bool,
    ) -> dict[str, Any]:
        """Seal host-built render objects into a host generation, one per envelope.

        An envelope that already has an object in the generation keeps it when
        it is the same object and has it replaced (the old one retired) when the
        host rebuilt it differently. With `promote`, the generation becomes
        current once it covers the current one's native envelopes. Rebuilding a
        generation that is already current recomputes the session's heads and
        reopens its projectors.
        """
        sessions = StorageSession.__table__
        generations = RenderGeneration.__table__
        render = RenderObject.__table__
        raw = RawObject.__table__
        tombstones = SessionTombstone.__table__
        projectors = ProjectorState.__table__
        with _write_transaction(self.engine) as connection:
            if connection.execute(select(tombstones.c.session_id).where(tombstones.c.session_id == session_id)).first() is not None:
                return {"session_deleted": True}
            session = connection.execute(select(sessions).where(sessions.c.session_id == session_id)).mappings().first()
            if session is None:
                return {"not_found": True}
            generation = connection.execute(select(generations).where(generations.c.generation_id == generation_id)).mappings().first()
            if generation is not None and (
                generation["session_id"] != session_id
                or generation["parser_revision"] != parser_revision
                or generation["ordering_revision"] != ordering_revision
                or generation["state"] == "failed"
            ):
                return {"conflict": True, "reason": "generation belongs to another session or revision"}
            if generation is None:
                other = connection.execute(
                    select(generations.c.generation_id).where(
                        generations.c.session_id == session_id,
                        generations.c.parser_revision == parser_revision,
                        generations.c.ordering_revision == ordering_revision,
                    )
                ).first()
                if other is not None:
                    return {"conflict": True, "reason": "another generation holds this session and revision"}
            envelope_ids = {str(item["source_envelope_id"]) for item in objects}
            live = {
                str(value)
                for value in connection.execute(
                    select(raw.c.envelope_id).where(
                        raw.c.session_id == session_id, raw.c.retired_at.is_(None), raw.c.envelope_id.in_(envelope_ids)
                    )
                ).scalars()
            }
            held = {
                str(row["source_envelope_id"]): row
                for row in connection.execute(
                    select(render.c.object_id, render.c.source_envelope_id).where(
                        render.c.generation_id == generation_id,
                        render.c.retired_at.is_(None),
                        render.c.source_envelope_id.in_(envelope_ids),
                    )
                ).mappings()
            }
            commit_time = datetime.now(UTC)
            commit_seq = _advance_commit_seq(connection, commit_time)
            if generation is None:
                connection.execute(
                    generations.insert().values(
                        generation_id=generation_id,
                        session_id=session_id,
                        parser_revision=parser_revision,
                        ordering_revision=ordering_revision,
                        state="pending",
                        source_chain_hash="0" * 64,
                        object_count=0,
                        event_count=0,
                        commit_seq=commit_seq,
                        created_at=commit_time,
                        updated_at=commit_time,
                    )
                )
            written = 0
            for item in objects:
                envelope_id = str(item["source_envelope_id"])
                if envelope_id not in live:
                    continue
                previous = held.get(envelope_id)
                if previous is not None and str(previous["object_id"]) == item["object_id"]:
                    continue
                if previous is not None:
                    connection.execute(update(render).where(render.c.object_id == previous["object_id"]).values(retired_at=commit_time))
                if connection.execute(select(render.c.object_id).where(render.c.object_id == item["object_id"])).first() is not None:
                    # The same content already sealed for another generation:
                    # objects are content-addressed and carry their generation,
                    # so this cannot be the same object.
                    return {"conflict": True, "reason": "render object already belongs to another generation"}
                connection.execute(
                    render.insert().values(
                        object_id=item["object_id"],
                        generation_id=generation_id,
                        session_id=session_id,
                        source_envelope_id=envelope_id,
                        object_hash=item["object_hash"],
                        payload_hash=item["payload_hash"],
                        object_path=item["object_path"],
                        uncompressed_size=item["uncompressed_size"],
                        compressed_size=item["compressed_size"],
                        event_count=item["event_count"],
                        user_messages=item["user_messages"],
                        assistant_messages=item["assistant_messages"],
                        tool_calls=item["tool_calls"],
                        abandoned_events=item.get("abandoned_events"),
                        first_user_message_preview=item["first_user_message_preview"],
                        last_visible_text_preview=item["last_visible_text_preview"],
                        user_turns=item.get("user_turns"),
                        semantic_projection_version=item.get("semantic_projection_version", 0),
                        render_source=item["render_source"],
                        object_parser_revision=item.get("object_parser_revision") or parser_revision,
                        first_order_key=item["first_order_key"],
                        last_order_key=item["last_order_key"],
                        **_render_order_columns(item["first_order_key"], item["last_order_key"]),
                        commit_seq=commit_seq,
                        created_at=commit_time,
                    )
                )
                written += 1
            was_current = session["current_render_generation"] is not None and str(session["current_render_generation"]) == generation_id
            current = was_current
            if was_current and written:
                _recompute_render_generation_projection(
                    connection, session_id=session_id, generation_id=generation_id, commit_seq=commit_seq, commit_time=commit_time
                )
            elif promote and not was_current:
                current = _promote_generation_if_covering(
                    connection, session_id=session_id, candidate=generation_id, commit_seq=commit_seq, commit_time=commit_time
                )
            if current and (written or not was_current):
                connection.execute(
                    update(projectors)
                    .where(projectors.c.session_id == session_id)
                    .values(desired_revision=func.max(projectors.c.desired_revision, commit_seq), desired_at=commit_time)
                )
        return {"written": written, "current": current, "promoted": current and not was_current, "commit_seq": str(commit_seq)}

    def list_render_shadow_candidates(self, *, limit: int, revision: str | None = None) -> dict[str, Any]:
        """Sessions with envelopes the shadow has not answered yet, newest first.

        Unanswered means never compared, last compared with an error, or (when
        the caller names its normalizer `revision`) last matched or mismatched
        by another revision.

        A session compared in the last `SHADOW_SESSION_COOLDOWN` waits: each
        comparison rebuilds whole epochs, so rechecking a live session on every
        new envelope would cost the square of its length.
        """
        raw = RawObject.__table__
        shadow = RenderShadowEnvelope.__table__
        recent = select(shadow.c.session_id).where(shadow.c.checked_at > datetime.now(UTC) - SHADOW_SESSION_COOLDOWN)
        with _read_snapshot(self.engine) as connection:
            rows = connection.execute(
                select(raw.c.session_id, raw.c.provider, func.max(raw.c.commit_seq).label("newest"))
                .select_from(raw.outerjoin(shadow, shadow.c.envelope_id == raw.c.envelope_id))
                .where(
                    raw.c.retired_at.is_(None),
                    raw.c.session_id.not_in(recent),
                    or_(
                        shadow.c.envelope_id.is_(None),
                        shadow.c.outcome == "error",
                        and_(
                            literal(revision is not None),
                            shadow.c.outcome.in_(REVISION_BOUND_OUTCOMES),
                            shadow.c.normalizer_revision != (revision or ""),
                        ),
                    ),
                )
                .group_by(raw.c.session_id, raw.c.provider)
                .order_by(func.max(raw.c.commit_seq).desc())
                .limit(limit)
            ).all()
        return {"sessions": [{"session_id": row[0], "provider": row[1]} for row in rows]}

    def record_render_shadow(self, *, results: tuple[dict[str, Any], ...]) -> dict[str, Any]:
        if not results:
            return {"recorded": 0}
        table = RenderShadowEnvelope.__table__
        now = datetime.now(UTC)
        rows = [
            {
                "envelope_id": str(item["envelope_id"]),
                "session_id": str(item["session_id"]),
                "provider": str(item["provider"]),
                "source_epoch": str(item["source_epoch"]),
                "range_start": int(item["range_start"]),
                "outcome": str(item["outcome"]),
                "detail": (str(item["detail"])[:4_000] if item.get("detail") is not None else None),
                "normalizer_revision": str(item["normalizer_revision"]),
                "elapsed_us": int(item["elapsed_us"]) if item.get("elapsed_us") is not None else None,
                "checked_at": now,
            }
            for item in results
        ]
        statement = sqlite_insert(table)
        statement = statement.on_conflict_do_update(
            index_elements=["envelope_id"],
            set_={name: statement.excluded[name] for name in ("outcome", "detail", "normalizer_revision", "elapsed_us", "checked_at")},
        )
        tombstones = SessionTombstone.__table__
        with _write_transaction(self.engine) as connection:
            # A session deleted while the sweep compared it keeps no shadow rows:
            # their details quote transcript text.
            deleted = {
                str(value)
                for value in connection.execute(
                    select(tombstones.c.session_id).where(tombstones.c.session_id.in_({row["session_id"] for row in rows}))
                ).scalars()
            }
            rows = [row for row in rows if row["session_id"] not in deleted]
            if rows:
                connection.execute(statement, rows)
        return {"recorded": len(rows)}

    def render_shadow_summary(self, *, sample: int = 20) -> dict[str, Any]:
        shadow = RenderShadowEnvelope.__table__
        raw = RawObject.__table__
        # Only envelopes still stored: a replaced source epoch retires its raw
        # objects, and its old comparisons say nothing about what is served.
        table = (
            select(shadow)
            .where(shadow.c.envelope_id.in_(select(raw.c.envelope_id).where(raw.c.retired_at.is_(None))))
            .subquery("live_shadow")
        )
        with _read_snapshot(self.engine) as connection:
            counts = connection.execute(
                select(table.c.provider, table.c.outcome, func.count(), func.count(func.distinct(table.c.session_id)))
                .group_by(table.c.provider, table.c.outcome)
                .order_by(table.c.provider, table.c.outcome)
            ).all()
            elapsed = connection.execute(
                select(table.c.provider, func.max(table.c.elapsed_us), func.avg(table.c.elapsed_us)).group_by(table.c.provider)
            ).all()
            samples = (
                connection.execute(
                    select(table)
                    .where(table.c.outcome.in_(("mismatch", "revision_skew", "media_missing", "error")))
                    .order_by(table.c.checked_at.desc())
                    .limit(sample)
                )
                .mappings()
                .all()
            )
        return {
            "counts": [{"provider": row[0], "outcome": row[1], "envelopes": int(row[2]), "sessions": int(row[3])} for row in counts],
            "elapsed_us": [{"provider": row[0], "max": int(row[1] or 0), "mean": int(row[2] or 0)} for row in elapsed],
            "samples": [
                {
                    **{key: value for key, value in row.items() if key != "checked_at"},
                    "checked_at": _rfc3339(row["checked_at"]) if row["checked_at"] else None,
                }
                for row in samples
            ],
        }
