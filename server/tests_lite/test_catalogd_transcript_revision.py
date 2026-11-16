"""storage_sessions.transcript_revision rises with every write a transcript row shows.

Spec: control-plane/docs/specs/transcript-revision-sync.md. Clients read the
transcript again only when this revision is newer than the one they hold, so a
writer that changes a row (or its decoration) without advancing it leaves open
pages stale until some unrelated write happens to move it.
"""

from __future__ import annotations

import hashlib
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from tests_lite.test_session_input_links import _candidate
from tests_lite.test_session_input_links import _seed_delivered_receipt
from zerg.catalogd.models import StorageSession
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.store import CatalogStore
from zerg.catalogd.store import _advance_transcript_revision

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def _store(tmp_path):
    engine = create_catalog_engine(tmp_path / "revision.db")
    initialize_catalog_schema(engine)
    return engine, CatalogStore(engine)


def _seed_storage_session(engine, session_id: str, *, transcript_revision: int = 1) -> None:
    with engine.begin() as connection:
        connection.execute(
            StorageSession.__table__.insert().values(
                session_id=session_id,
                tenant_id="default",
                owner_id="42",
                provider="claude",
                environment="test",
                machine_id="cinder",
                project="longhouse",
                cwd=None,
                git_repo=None,
                git_branch=None,
                started_at=NOW - timedelta(hours=1),
                last_activity_at=NOW,
                ended_at=None,
                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
                summary_title=None,
                first_user_message_preview="hello",
                last_visible_text_preview=None,
                transcript_revision=transcript_revision,
                current_render_generation=str(uuid4()),
                raw_state="durable",
                render_state="ready",
                media_state="complete",
                origin_kind="test_or_canary",
                hidden_from_default_timeline=1,
                commit_seq=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )


def _revision(engine, session_id: str) -> int:
    with engine.connect() as connection:
        table = StorageSession.__table__
        return int(connection.execute(select(table.c.transcript_revision).where(table.c.session_id == session_id)).scalar_one())


def test_advance_only_ever_raises_the_revision(tmp_path):
    engine, _store_ = _store(tmp_path)
    session_id = str(uuid4())
    _seed_storage_session(engine, session_id, transcript_revision=5)

    with engine.begin() as connection:
        _advance_transcript_revision(connection, session_ids=(session_id,), commit_seq=9)
    assert _revision(engine, session_id) == 9

    with engine.begin() as connection:
        _advance_transcript_revision(connection, session_ids=(session_id,), commit_seq=7)
    assert _revision(engine, session_id) == 9


def test_backfilled_provider_facts_advance_the_revision(tmp_path):
    engine, store = _store(tmp_path)
    session_id = str(uuid4())
    _seed_storage_session(engine, session_id, transcript_revision=0)
    fact = {"kind": "turn.duration", "at": NOW, "source_position": 12, "payload": {"duration_ms": 1200}}

    first = store.insert_session_provider_facts(session_id=session_id, source_epoch=str(uuid4()), provider_facts=(fact,))
    assert first["inserted"] == 1
    after_insert = _revision(engine, session_id)
    assert after_insert > 0

    epoch = str(uuid4())
    store.insert_session_provider_facts(session_id=session_id, source_epoch=epoch, provider_facts=(fact,))
    settled = _revision(engine, session_id)
    # An exact repeat inserts nothing and changes no row.
    store.insert_session_provider_facts(session_id=session_id, source_epoch=epoch, provider_facts=(fact,))
    assert _revision(engine, session_id) == settled


def test_linking_a_send_to_its_row_advances_the_revision(tmp_path):
    engine, store = _store(tmp_path)
    session_id = uuid4()
    _seed_storage_session(engine, str(session_id))
    _seed_delivered_receipt(engine, session_id=session_id, text="ship it", client_request_id="req-1", created_at=NOW)

    result = store.link_input_receipts_to_events(
        session_id=str(session_id),
        candidates=[_candidate("echo-1", "ship it", NOW + timedelta(seconds=2))],
        observed_at=NOW + timedelta(seconds=3),
    )

    assert [entry["durable_event_id"] for entry in result["linked"]] == ["echo-1"]
    assert _revision(engine, str(session_id)) == int(result["commit_seq"])

    unchanged = _revision(engine, str(session_id))
    store.link_input_receipts_to_events(
        session_id=str(session_id),
        candidates=[_candidate("echo-2", "ship it", NOW + timedelta(seconds=4))],
        observed_at=NOW + timedelta(seconds=5),
    )
    assert _revision(engine, str(session_id)) == unchanged


def test_an_image_arriving_after_its_rows_advances_every_session_showing_it(tmp_path):
    engine, store = _store(tmp_path)
    shown_in = [str(uuid4()), str(uuid4())]
    elsewhere = str(uuid4())
    for session_id in [*shown_in, elsewhere]:
        _seed_storage_session(engine, session_id)
    media_hash = hashlib.sha256(b"screenshot").hexdigest()

    store.commit_media_object(
        media_hash=media_hash,
        state="missing",
        mime_type="image/png",
        byte_size=None,
        object_path=None,
        session_refs=tuple({"session_id": session_id, "envelope_id": None, "ref_key": "0"} for session_id in shown_in),
        observed_at=NOW,
    )
    before = {session_id: _revision(engine, session_id) for session_id in [*shown_in, elsewhere]}

    landed = store.commit_media_object(
        media_hash=media_hash,
        state="present",
        mime_type="image/png",
        byte_size=10,
        object_path=f"media/{media_hash}",
        session_refs=(),
        observed_at=NOW + timedelta(seconds=1),
    )

    assert landed["changed"] is True
    assert sorted(landed["affected_session_ids"]) == sorted(shown_in)
    for session_id in shown_in:
        assert _revision(engine, session_id) == int(landed["commit_seq"]) > before[session_id]
    assert _revision(engine, elsewhere) == before[elsewhere]
