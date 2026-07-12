from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event as sqlalchemy_event

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.server import CatalogDaemon
from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import ArchiveChunk
from zerg.services.archive_primary import insert_archive_chunk_manifests
from zerg.services.archive_store import ArchiveRecord
from zerg.services.archive_store import FilesystemArchiveStore
from zerg.services.legacy_corpus_migration import STREAMING_EVENT_PAGE
from zerg.services.legacy_corpus_migration import LegacyCorpusConverter
from zerg.services.legacy_corpus_migration import _normalized_event_source
from zerg.services.legacy_corpus_migration import _source_batches
from zerg.services.legacy_corpus_migration import _SourceRecord
from zerg.services.legacy_corpus_migration import create_inventory_run
from zerg.services.legacy_corpus_migration import freeze_high_watermark
from zerg.services.legacy_corpus_migration import inventory_rows
from zerg.storage_v2.raw_objects import read_raw_object


class FakeCatalog:
    def __init__(self, *, source_epoch_found: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.source_epoch_found = source_epoch_found

    async def call(self, method, params=None, *, timeout_seconds=None):
        payload = dict(params or {})
        self.calls.append((method, payload))
        if method == "storage.raw_object.commit.v2":
            return {
                "receipt": {
                    "commit_seq": "7",
                    "envelope_id": payload["envelope_id"],
                    "render_state": payload["render_state"],
                }
            }
        if method == "auth.owner.get.v2":
            return {"found": True, "owner_id": 7}
        if method == "storage.source_epoch.manifest.v2":
            return {"found": self.source_epoch_found}
        return {"ok": True}


@pytest.fixture
def legacy_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    Base.metadata.create_all(engine)
    factory = make_sessionmaker(engine)
    yield factory
    engine.dispose()


def _session(*, provider: str = "codex") -> AgentSession:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    return AgentSession(
        id=uuid4(),
        provider=provider,
        environment="production",
        project="longhouse",
        device_id="cinder",
        cwd="/workspace/longhouse",
        started_at=now,
        last_activity_at=now,
    )


def _event(session_id, *, raw_json: str | None, source_path: str | None, source_offset: int | None) -> AgentEvent:
    return AgentEvent(
        session_id=session_id,
        role="user",
        content_text="migrate this",
        timestamp=datetime(2026, 7, 12, 0, 0, 1, tzinfo=UTC),
        source_path=source_path,
        source_offset=source_offset,
        event_hash=hashlib.sha256(b"event").hexdigest(),
        raw_json=raw_json,
        raw_json_codec=0,
    )


def test_source_batches_bound_dense_render_payloads():
    session_id = uuid4()
    records = [_SourceRecord(b"{}", "dense.jsonl", offset, 0, "legacy_source_lines") for offset in range(3)]
    groups = {}
    for offset in range(3):
        event = _event(session_id, raw_json="{}", source_path="dense.jsonl", source_offset=offset)
        event.content_text = "x" * 600_000
        groups[("dense.jsonl", offset)] = [event]

    batches = _source_batches(records, groups)

    assert [len(batch.records) for batch in batches] == [1, 1, 1]
    assert [batch.range_start for batch in batches] == [0, 1, 2]


@pytest.mark.asyncio
async def test_all_slim_high_row_session_streams_archive_and_events_in_bounded_batches(
    legacy_db,
    tmp_path: Path,
    monkeypatch,
):
    row_count = 12_000
    session = _session(provider="claude")
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    with legacy_db() as db:
        db.add(session)
        db.commit()

        def archive_records():
            for index in range(row_count):
                raw = f'{{"index":{index},"message":"archived"}}'.encode()
                yield ArchiveRecord(
                    tenant_id="tenant-a",
                    session_id=str(session.id),
                    stream="source_lines",
                    source_seq=index,
                    raw_bytes=raw,
                    provider="claude",
                    source_path="giant.jsonl",
                    source_offset=index * 64,
                )

        chunks = archive_store.write_record_chunks(archive_records(), target_uncompressed_bytes=128 * 1024)
        insert_archive_chunk_manifests(db, chunks)
        for start in range(0, row_count, 1_000):
            stop = min(row_count, start + 1_000)
            db.execute(
                AgentSourceLine.__table__.insert(),
                [
                    {
                        "session_id": session.id,
                        "source_path": "giant.jsonl",
                        "source_offset": index * 64,
                        "branch_id": 0,
                        "revision": 1,
                        "is_branch_copy": 0,
                        "raw_json": "",
                        "raw_json_z": None,
                        "raw_json_codec": 0,
                        "line_hash": hashlib.sha256(f'{{"index":{index},"message":"archived"}}'.encode()).hexdigest(),
                    }
                    for index in range(start, stop)
                ],
            )
            db.execute(
                AgentEvent.__table__.insert(),
                [
                    {
                        "session_id": session.id,
                        "role": "user" if index % 2 == 0 else "assistant",
                        "content_text": f"streamed event {index}",
                        "timestamp": datetime(2026, 7, 12, 0, 0, index % 60, tzinfo=UTC),
                        "source_path": "giant.jsonl",
                        "source_offset": index * 64,
                        "event_hash": hashlib.sha256(f"event:{index}".encode()).hexdigest(),
                        "raw_json": None,
                        "raw_json_codec": 0,
                    }
                    for index in range(start, stop)
                ],
            )
            db.commit()
        watermark = freeze_high_watermark(db)

    def forbidden_materializing_path(*_args, **_kwargs):
        raise AssertionError("giant all-slim conversion must not materialize legacy source/event rows")

    monkeypatch.setattr(LegacyCorpusConverter, "_load_sources", forbidden_materializing_path)
    loaded_event_high_watermark = 0

    def track_event_identity_map(_target, context):
        nonlocal loaded_event_high_watermark
        loaded = sum(isinstance(value, AgentEvent) for value in context.session.identity_map.values())
        loaded_event_high_watermark = max(loaded_event_high_watermark, loaded)

    sqlalchemy_event.listen(AgentEvent, "load", track_event_identity_map)
    catalog = FakeCatalog()
    converter = LegacyCorpusConverter(
        session_factory=legacy_db,
        catalog=catalog,
        object_root=tmp_path / "objects-v2",
        tenant_id="tenant-a",
        archive_store=archive_store,
    )
    try:
        with legacy_db() as db:
            result = await converter.convert_session(db, session.id, watermark)
    finally:
        sqlalchemy_event.remove(AgentEvent, "load", track_event_identity_map)

    commits = [payload for method, payload in catalog.calls if method == "storage.raw_object.commit.v2"]
    source_commits = [payload for payload in commits if payload["provenance_kind"] == "legacy_source_lines"]
    event_commits = [payload for payload in commits if payload["provenance_kind"] == "legacy_normalized_event"]
    assert len(source_commits) > 10
    assert len(event_commits) >= row_count // 500
    assert max(len(payload["record_hashes"]) for payload in commits) <= 1_000
    assert result.source_covered == row_count
    assert result.source_missing == 0
    assert result.parity_matches is True
    assert result.degradation_code is None
    assert result.envelope_ids == ()
    assert loaded_event_high_watermark <= STREAMING_EVENT_PAGE
    with legacy_db() as db:
        assert db.query(ArchiveChunk).count() == len(chunks)

    retry_catalog = FakeCatalog(source_epoch_found=True)
    retry_converter = LegacyCorpusConverter(
        session_factory=legacy_db,
        catalog=retry_catalog,
        object_root=tmp_path / "objects-v2",
        tenant_id="tenant-a",
        archive_store=archive_store,
    )
    with legacy_db() as db:
        retry = await retry_converter.convert_session(
            db,
            session.id,
            watermark,
            replace_existing_epochs=True,
        )
    retry_commits = [payload for method, payload in retry_catalog.calls if method == "storage.raw_object.commit.v2"]
    assert retry.source_covered == row_count
    assert retry.parity_matches is True
    assert retry_commits
    assert all(payload["predecessor_source_epoch"] is not None for payload in retry_commits)


@pytest.mark.asyncio
async def test_oversized_legacy_tool_output_keeps_raw_truth_and_terminally_degrades_render(legacy_db, tmp_path: Path):
    session = _session()
    raw = '{"type":"tool_result","content":"preserved in raw"}'
    with legacy_db() as db:
        db.add(session)
        db.flush()
        db.add(
            AgentSourceLine(
                session_id=session.id,
                source_path="oversized.jsonl",
                source_offset=0,
                branch_id=0,
                raw_json=raw,
                raw_json_codec=0,
                line_hash=hashlib.sha256(raw.encode()).hexdigest(),
            )
        )
        event = _event(session.id, raw_json=raw, source_path="oversized.jsonl", source_offset=0)
        event.role = "tool"
        event.tool_output_text = "x" * (2 * 1024 * 1024 + 1)
        db.add(event)
        db.commit()
        watermark = freeze_high_watermark(db)

    catalog = FakeCatalog()
    converter = LegacyCorpusConverter(
        session_factory=legacy_db,
        catalog=catalog,
        object_root=tmp_path / "objects-v2",
        tenant_id="tenant-a",
    )
    with legacy_db() as db:
        result = await converter.convert_session(db, session.id, watermark)

    commits = [payload for method, payload in catalog.calls if method == "storage.raw_object.commit.v2"]
    assert len(commits) == 1
    assert commits[0]["render_state"] == "failed"
    assert commits[0]["render_manifest"] is None
    assert result.degradation_code == "render_projection_failed"
    assert "tool_output_text" in (result.degradation_message or "")
    assert result.parity_matches is False
    await converter._complete(uuid4(), uuid4(), result)
    completion = [payload for method, payload in catalog.calls if method == "migration.session.complete.v2"]
    assert completion[0]["degradation_code"] == "render_projection_failed"


@pytest.mark.asyncio
async def test_oversized_unmatched_event_is_split_into_exact_bounded_raw_records(legacy_db, tmp_path: Path):
    session = _session()
    with legacy_db() as db:
        db.add(session)
        db.flush()
        event = _event(session.id, raw_json=None, source_path=None, source_offset=None)
        event.role = "tool"
        event.tool_output_text = "x" * (4 * 1024 * 1024 + 1)
        db.add(event)
        db.flush()
        expected = (_normalized_event_source((event,)) or "").encode("utf-8")
        db.commit()
        watermark = freeze_high_watermark(db)

    catalog = FakeCatalog()
    object_root = tmp_path / "objects-v2"
    converter = LegacyCorpusConverter(
        session_factory=legacy_db,
        catalog=catalog,
        object_root=object_root,
        tenant_id="tenant-a",
    )
    with legacy_db() as db:
        result = await converter.convert_session(db, session.id, watermark)

    commits = [payload for method, payload in catalog.calls if method == "storage.raw_object.commit.v2"]
    commits.sort(key=lambda payload: payload["range_start"])
    restored = b"".join(
        record.data
        for payload in commits
        for record in read_raw_object(
            object_root,
            payload["object_path"],
            expected_object_hash=payload["object_hash"],
        ).spec.records
    )
    assert len(commits) == 2
    assert restored == expected
    assert [payload["render_state"] for payload in commits] == ["failed", "ready"]
    assert result.degradation_code == "render_projection_failed"


@pytest.mark.asyncio
async def test_unmatched_events_are_preserved_in_bounded_synthetic_batches(legacy_db, tmp_path: Path):
    session = _session()
    raw = '{"type":"user","message":"matched"}'
    with legacy_db() as db:
        db.add(session)
        db.flush()
        db.add(
            AgentSourceLine(
                session_id=session.id,
                source_path="session.jsonl",
                source_offset=0,
                branch_id=0,
                raw_json=raw,
                raw_json_codec=0,
                line_hash=hashlib.sha256(raw.encode()).hexdigest(),
            )
        )
        db.add(_event(session.id, raw_json=raw, source_path="session.jsonl", source_offset=0))
        for index in range(3):
            event = _event(session.id, raw_json=None, source_path="unmatched.jsonl", source_offset=index)
            event.content_text = str(index) + "x" * 1_500_000
            event.event_hash = hashlib.sha256(f"unmatched-{index}".encode()).hexdigest()
            db.add(event)
        db.commit()
        watermark = freeze_high_watermark(db)

    catalog = FakeCatalog()
    converter = LegacyCorpusConverter(
        session_factory=legacy_db,
        catalog=catalog,
        object_root=tmp_path / "objects-v2",
        tenant_id="tenant-a",
    )
    with legacy_db() as db:
        result = await converter.convert_session(db, session.id, watermark)

    commits = [payload for method, payload in catalog.calls if method == "storage.raw_object.commit.v2"]
    synthetic = [payload for payload in commits if payload["provenance_kind"] == "legacy_normalized_event"]
    assert len(synthetic) == 3
    assert all(payload["render_manifest"]["event_count"] == 1 for payload in synthetic)
    assert result.parity_matches is True


@pytest.mark.asyncio
async def test_inventory_freezes_rowid_high_watermark_and_registers_exact_counts(legacy_db):
    first = _session()
    with legacy_db() as db:
        db.add(first)
        db.flush()
        raw = '{"type":"user","message":"first"}'
        db.add(
            AgentSourceLine(
                session_id=first.id,
                source_path="history.jsonl",
                source_offset=0,
                branch_id=0,
                raw_json=raw,
                raw_json_codec=0,
                line_hash=hashlib.sha256(raw.encode()).hexdigest(),
            )
        )
        db.commit()
        watermark = freeze_high_watermark(db)

        late = _session()
        db.add(late)
        db.commit()
        rows = inventory_rows(db, watermark)

    assert [(row.session_id, row.source_expected) for row in rows] == [(first.id, 1)]

    catalog = FakeCatalog()
    with legacy_db() as db:
        result = await create_inventory_run(db, catalog, run_id=uuid4())
    assert result["expected_session_count"] == 2
    assert [method for method, _ in catalog.calls] == [
        "migration.run.create.v2",
        "migration.session.register.batch.v2",
    ]


@pytest.mark.asyncio
async def test_converter_prefers_exact_source_lines_and_is_deterministic(legacy_db, tmp_path: Path):
    session = _session(provider="claude")
    session.git_branch = " "
    raw = '{"type":"user","message":"exact bytes"}'
    with legacy_db() as db:
        db.add(session)
        db.flush()
        db.add(
            AgentSourceLine(
                session_id=session.id,
                source_path="session.jsonl",
                source_offset=123,
                branch_id=0,
                raw_json=raw,
                raw_json_codec=0,
                line_hash=hashlib.sha256(raw.encode()).hexdigest(),
            )
        )
        db.add(_event(session.id, raw_json='{"different":"event raw"}', source_path="session.jsonl", source_offset=123))
        db.commit()
        watermark = freeze_high_watermark(db)

    catalog = FakeCatalog()
    converter = LegacyCorpusConverter(
        session_factory=legacy_db,
        catalog=catalog,
        object_root=tmp_path / "objects-v2",
        tenant_id="tenant-a",
    )
    with legacy_db() as db:
        first = await converter.convert_session(db, session.id, watermark)
    with legacy_db() as db:
        second = await converter.convert_session(db, session.id, watermark)

    commits = [payload for method, payload in catalog.calls if method == "storage.raw_object.commit.v2"]
    assert commits[0]["provenance_kind"] == "legacy_source_lines"
    assert commits[0]["owner_id"] == "7"
    assert commits[0]["session_facts"]["git_branch"] is None
    assert commits[0]["record_hashes"] == [hashlib.sha256(raw.encode()).hexdigest()]
    assert commits[0]["render_manifest"]["event_count"] == 1
    assert commits[0]["source_epoch"] == commits[1]["source_epoch"]
    assert commits[0]["envelope_id"] == commits[1]["envelope_id"]
    assert first.output_proof_hash == second.output_proof_hash
    assert first.parity_proof_hash == second.parity_proof_hash


@pytest.mark.asyncio
async def test_retry_replaces_partial_source_epoch_when_batch_layout_changed(legacy_db, tmp_path: Path):
    session = _session()
    raw = '{"type":"user","message":"retry"}'
    with legacy_db() as db:
        db.add(session)
        db.flush()
        db.add(
            AgentSourceLine(
                session_id=session.id,
                source_path="retry.jsonl",
                source_offset=0,
                branch_id=0,
                raw_json=raw,
                raw_json_codec=0,
                line_hash=hashlib.sha256(raw.encode()).hexdigest(),
            )
        )
        db.add(_event(session.id, raw_json=raw, source_path="retry.jsonl", source_offset=0))
        db.commit()
        watermark = freeze_high_watermark(db)

    catalog = FakeCatalog(source_epoch_found=True)
    converter = LegacyCorpusConverter(
        session_factory=legacy_db,
        catalog=catalog,
        object_root=tmp_path / "objects-v2",
        tenant_id="tenant-a",
    )
    with legacy_db() as db:
        await converter.convert_session(db, session.id, watermark, replace_existing_epochs=True)

    commit = next(payload for method, payload in catalog.calls if method == "storage.raw_object.commit.v2")
    manifest = next(payload for method, payload in catalog.calls if method == "storage.source_epoch.manifest.v2")
    assert commit["predecessor_source_epoch"] == manifest["source_epoch"]
    assert commit["source_epoch"] != manifest["source_epoch"]


@pytest.mark.asyncio
async def test_converter_uses_event_raw_only_when_source_lines_are_absent(legacy_db, tmp_path: Path):
    session = _session()
    raw = '{"type":"response_item","payload":"fallback"}'
    with legacy_db() as db:
        db.add(session)
        db.flush()
        db.add(_event(session.id, raw_json=raw, source_path="rollout.jsonl", source_offset=44))
        db.commit()
        watermark = freeze_high_watermark(db)

    catalog = FakeCatalog()
    converter = LegacyCorpusConverter(
        session_factory=legacy_db,
        catalog=catalog,
        object_root=tmp_path / "objects-v2",
        tenant_id="tenant-a",
    )
    with legacy_db() as db:
        result = await converter.convert_session(db, session.id, watermark)

    commit = next(payload for method, payload in catalog.calls if method == "storage.raw_object.commit.v2")
    assert commit["provenance_kind"] == "legacy_fallback"
    assert commit["record_hashes"] == [hashlib.sha256(raw.encode()).hexdigest()]
    assert result.source_covered == 1
    assert result.source_missing == 0


@pytest.mark.asyncio
async def test_missing_legacy_raw_and_media_finish_as_explicit_coverage_gaps(legacy_db, tmp_path: Path):
    session = _session()
    with legacy_db() as db:
        db.add(session)
        db.flush()
        db.add(_event(session.id, raw_json=None, source_path="missing.jsonl", source_offset=0))
        db.commit()
        watermark = freeze_high_watermark(db)

    catalog = FakeCatalog()
    converter = LegacyCorpusConverter(
        session_factory=legacy_db,
        catalog=catalog,
        object_root=tmp_path / "objects-v2",
        tenant_id="tenant-a",
    )
    with legacy_db() as db:
        result = await converter.convert_session(db, session.id, watermark)

    assert result.source_covered == 0
    assert result.source_missing == 1
    assert result.parity_matches is True
    assert len(result.output_proof_hash) == 64
    commit = next(payload for method, payload in catalog.calls if method == "storage.raw_object.commit.v2")
    assert commit["provenance_kind"] == "legacy_normalized_event"


@pytest.mark.asyncio
async def test_hash_mismatched_source_line_preserves_normalized_events_as_derived_evidence(legacy_db, tmp_path: Path):
    session = _session(provider="claude")
    raw = '{"type":"user","message":"corrupt hash"}'
    with legacy_db() as db:
        db.add(session)
        db.flush()
        db.add(
            AgentSourceLine(
                session_id=session.id,
                source_path="session.jsonl",
                source_offset=7,
                branch_id=0,
                raw_json=raw,
                raw_json_codec=0,
                line_hash=hashlib.sha256(b"different bytes").hexdigest(),
            )
        )
        db.add(_event(session.id, raw_json=None, source_path="session.jsonl", source_offset=7))
        db.commit()
        watermark = freeze_high_watermark(db)

    catalog = FakeCatalog()
    converter = LegacyCorpusConverter(
        session_factory=legacy_db,
        catalog=catalog,
        object_root=tmp_path / "objects-v2",
        tenant_id="tenant-a",
    )
    with legacy_db() as db:
        result = await converter.convert_session(db, session.id, watermark)

    assert result.source_covered == 0
    assert result.source_missing == 1
    assert result.parity_matches is True
    commit = next(payload for method, payload in catalog.calls if method == "storage.raw_object.commit.v2")
    assert commit["provenance_kind"] == "legacy_normalized_event"


@pytest.mark.asyncio
async def test_mixed_source_paths_get_separate_epochs_and_contiguous_ordinals(legacy_db, tmp_path: Path):
    session = _session()
    raws = {
        "a.jsonl": '{"type":"user","message":"a"}',
        "b.jsonl": '{"type":"assistant","message":"b"}',
    }
    with legacy_db() as db:
        db.add(session)
        db.flush()
        for path, raw in raws.items():
            db.add(
                AgentSourceLine(
                    session_id=session.id,
                    source_path=path,
                    source_offset=0,
                    branch_id=0,
                    raw_json=raw,
                    raw_json_codec=0,
                    line_hash=hashlib.sha256(raw.encode()).hexdigest(),
                )
            )
            db.add(_event(session.id, raw_json=raw, source_path=path, source_offset=0))
        db.commit()
        watermark = freeze_high_watermark(db)

    catalog = FakeCatalog()
    converter = LegacyCorpusConverter(
        session_factory=legacy_db,
        catalog=catalog,
        object_root=tmp_path / "objects-v2",
        tenant_id="tenant-a",
    )
    with legacy_db() as db:
        result = await converter.convert_session(db, session.id, watermark)

    commits = [payload for method, payload in catalog.calls if method == "storage.raw_object.commit.v2"]
    assert len(commits) == 2
    assert {commit["range_start"] for commit in commits} == {0}
    assert {commit["range_end"] for commit in commits} == {1}
    assert len({commit["source_epoch"] for commit in commits}) == 2
    assert len({commit["opaque_source_id"] for commit in commits}) == 2
    assert result.parity_matches is True


@pytest.mark.asyncio
async def test_branch_copy_duplicates_count_coverage_once_per_row_but_seal_one_record(legacy_db, tmp_path: Path):
    session = _session()
    raw = '{"type":"user","message":"branch copy"}'
    line_hash = hashlib.sha256(raw.encode()).hexdigest()
    with legacy_db() as db:
        db.add(session)
        db.flush()
        for branch_id, is_branch_copy in ((1, 0), (2, 1)):
            db.add(
                AgentSourceLine(
                    session_id=session.id,
                    source_path="branch.jsonl",
                    source_offset=55,
                    branch_id=branch_id,
                    is_branch_copy=is_branch_copy,
                    raw_json=raw,
                    raw_json_codec=0,
                    line_hash=line_hash,
                )
            )
        db.add(_event(session.id, raw_json=raw, source_path="branch.jsonl", source_offset=55))
        db.commit()
        watermark = freeze_high_watermark(db)

    catalog = FakeCatalog()
    converter = LegacyCorpusConverter(
        session_factory=legacy_db,
        catalog=catalog,
        object_root=tmp_path / "objects-v2",
        tenant_id="tenant-a",
    )
    with legacy_db() as db:
        result = await converter.convert_session(db, session.id, watermark)

    commits = [payload for method, payload in catalog.calls if method == "storage.raw_object.commit.v2"]
    assert len(commits) == 1
    assert commits[0]["range_end"] - commits[0]["range_start"] == 1
    assert commits[0]["record_hashes"] == [line_hash]
    assert result.source_covered == 2
    assert result.source_missing == 0


@pytest.mark.asyncio
async def test_missing_source_line_uses_matching_event_raw_as_legacy_fallback(legacy_db, tmp_path: Path):
    session = _session()
    raw = '{"type":"user","message":"recovered"}'
    with legacy_db() as db:
        db.add(session)
        db.flush()
        db.add(
            AgentSourceLine(
                session_id=session.id,
                source_path="slim.jsonl",
                source_offset=88,
                branch_id=0,
                raw_json="",
                raw_json_z=None,
                raw_json_codec=1,
                line_hash=hashlib.sha256(raw.encode()).hexdigest(),
            )
        )
        db.add(_event(session.id, raw_json=raw, source_path="slim.jsonl", source_offset=88))
        db.commit()
        watermark = freeze_high_watermark(db)

    catalog = FakeCatalog()
    converter = LegacyCorpusConverter(
        session_factory=legacy_db,
        catalog=catalog,
        object_root=tmp_path / "objects-v2",
        tenant_id="tenant-a",
    )
    with legacy_db() as db:
        result = await converter.convert_session(db, session.id, watermark)

    commit = next(payload for method, payload in catalog.calls if method == "storage.raw_object.commit.v2")
    assert commit["provenance_kind"] == "legacy_fallback"
    assert result.source_covered == 1
    assert result.source_missing == 0


@pytest.mark.asyncio
async def test_parity_mismatch_finishes_claim_as_explicit_degraded_failure(legacy_db, tmp_path: Path):
    session = _session()
    raw = '{"type":"user","message":"parity"}'
    with legacy_db() as db:
        db.add(session)
        db.flush()
        db.add(_event(session.id, raw_json=raw, source_path="parity.jsonl", source_offset=0))
        db.commit()
        watermark = freeze_high_watermark(db)

    catalog = FakeCatalog()
    converter = LegacyCorpusConverter(
        session_factory=legacy_db,
        catalog=catalog,
        object_root=tmp_path / "objects-v2",
        tenant_id="tenant-a",
    )
    with legacy_db() as db:
        result = await converter.convert_session(db, session.id, watermark)
    await converter._complete(uuid4(), uuid4(), replace(result, parity_matches=False))

    methods = [method for method, _ in catalog.calls]
    assert "migration.session.fail.v2" in methods
    assert "migration.session.complete.v2" not in methods
    failure = next(payload for method, payload in catalog.calls if method == "migration.session.fail.v2")
    assert failure["error_code"] == "parity_mismatch"


@pytest.mark.asyncio
async def test_converter_commits_through_real_catalog_contract(legacy_db, tmp_path: Path):
    session = _session()
    raw = '{"type":"user","message":"catalog contract"}'
    with legacy_db() as db:
        db.add(session)
        db.flush()
        db.add(_event(session.id, raw_json=raw, source_path="contract.jsonl", source_offset=0))
        db.commit()
        watermark = freeze_high_watermark(db)

    catalog_root = Path("/tmp") / f"lh-migrate-{uuid4().hex[:10]}"
    catalog_root.mkdir(mode=0o700)
    daemon = CatalogDaemon(database_path=catalog_root / "live.db", socket_path=catalog_root / "catalogd.sock")
    await daemon.start()
    client = CatalogClient(catalog_root / "catalogd.sock")
    try:
        await client.call(
            "auth.user.resolve_local.v2",
            {
                "email": "owner@example.com",
                "provider": "password",
                "provider_user_id": None,
                "role": "USER",
                "adopt_existing": True,
                "require_email_match": False,
                "max_users": None,
                "promote_role": False,
            },
        )
        converter = LegacyCorpusConverter(
            session_factory=legacy_db,
            catalog=client,
            object_root=tmp_path / "objects-v2",
            tenant_id="tenant-a",
        )
        with legacy_db() as db:
            result = await converter.convert_session(db, session.id, watermark)
        stored = await client.call("storage.session.read.v2", {"session_id": str(session.id)})
        assert result.source_covered == 1
        assert stored["found"] is True
        assert stored["session"]["current_render_generation"] is not None
    finally:
        await client.close()
        await daemon.close()
        for path in catalog_root.iterdir():
            path.unlink(missing_ok=True)
        catalog_root.rmdir()
