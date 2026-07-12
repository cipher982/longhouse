from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.server import CatalogDaemon
from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSourceLine
from zerg.services.legacy_corpus_migration import LegacyCorpusConverter
from zerg.services.legacy_corpus_migration import create_inventory_run
from zerg.services.legacy_corpus_migration import freeze_high_watermark
from zerg.services.legacy_corpus_migration import inventory_rows


class FakeCatalog:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

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
    assert commits[0]["record_hashes"] == [hashlib.sha256(raw.encode()).hexdigest()]
    assert commits[0]["render_manifest"]["event_count"] == 1
    assert commits[0]["source_epoch"] == commits[1]["source_epoch"]
    assert commits[0]["envelope_id"] == commits[1]["envelope_id"]
    assert first.output_proof_hash == second.output_proof_hash
    assert first.parity_proof_hash == second.parity_proof_hash


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
    assert result.parity_matches is False
    assert len(result.output_proof_hash) == 64
    assert not any(method == "storage.raw_object.commit.v2" for method, _ in catalog.calls)


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
