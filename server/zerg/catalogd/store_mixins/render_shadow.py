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
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from zerg.catalogd.models import MediaObject
from zerg.catalogd.models import RawObject
from zerg.catalogd.models import RenderGeneration
from zerg.catalogd.models import RenderObject
from zerg.catalogd.models import RenderShadowEnvelope
from zerg.catalogd.models import SessionMediaRef
from zerg.catalogd.models import SessionProviderFact
from zerg.catalogd.models import SessionTombstone
from zerg.catalogd.models import SourceEpoch
from zerg.catalogd.models import StorageSession
from zerg.catalogd.store import _read_snapshot
from zerg.catalogd.store import _write_transaction

# A session this large is rebuilt by Phase 4's incremental path, not sampled.
MAX_SHADOW_RAW_OBJECTS = 20_000
SHADOW_SESSION_COOLDOWN = timedelta(minutes=30)
SHADOW_OUTCOMES = ("match", "mismatch", "no_witness", "raw_only", "too_large", "error")
# Outcomes a newer normalizer revision must answer again. An error is retried
# after the cooldown whatever the revision.
REVISION_BOUND_OUTCOMES = ("match", "mismatch")


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
            generation = None
            render_rows: list[Any] = []
            if generation_id is not None:
                generation = (
                    connection.execute(
                        select(generations.c.parser_revision, generations.c.ordering_revision).where(
                            generations.c.generation_id == generation_id
                        )
                    )
                    .mappings()
                    .first()
                )
                render_rows = (
                    connection.execute(
                        select(render.c.source_envelope_id, render.c.object_path, render.c.object_hash, render.c.event_count).where(
                            render.c.generation_id == generation_id, render.c.retired_at.is_(None)
                        )
                    )
                    .mappings()
                    .all()
                )
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
            "parser_revision": generation["parser_revision"] if generation else None,
            "raw_objects": [
                {key: (int(value) if key in {"range_start", "range_end"} else value) for key, value in row.items()} for row in raw_rows
            ],
            "epoch_opened_at": {key: value.isoformat() if hasattr(value, "isoformat") else str(value) for key, value in opened.items()},
            "facts": [
                {
                    "kind": row["kind"],
                    "at": row["at"].isoformat() if hasattr(row["at"], "isoformat") else str(row["at"]),
                    "source_epoch": row["source_epoch"],
                    "source_position": int(row["source_position"]),
                    "payload": json.loads(row["payload_json"]),
                }
                for row in fact_rows
            ],
            "render_objects": [dict(row) for row in render_rows],
            "media": [dict(row) for row in media_rows],
            "checked": checked,
        }

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
        table = RenderShadowEnvelope.__table__
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
                    select(table).where(table.c.outcome.in_(("mismatch", "error"))).order_by(table.c.checked_at.desc()).limit(sample)
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
                    "checked_at": row["checked_at"].isoformat() if row["checked_at"] else None,
                }
                for row in samples
            ],
        }
