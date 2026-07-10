"""Cross-child-session archive coverage for relinked workflow subagents.

Workflow relink rewrites a subagent's source_lines/events.session_id to the
PARENT but leaves its archive chunks keyed by the ORIGINAL child session id (and
deletes the child AgentSession). The original id survives as a
``longhouse_session_id`` alias on the child subagent thread. The reclaim
verifier + transcript reader must resolve a parent's archive coverage across
those child ids — otherwise relinked-subagent rows look (falsely) uncovered and
the reclaim would either refuse or drop bytes it can't see.
"""

import os
from uuid import UUID

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentSourceLine
from zerg.services.agents.models import EventIngest
from zerg.services.agents.models import SessionIngest
from zerg.services.agents.store import AgentsStore
from zerg.services.archive_reclaim_verifier import verify_session_archive_coverage
from zerg.services.archive_store import ArchiveRecord
from zerg.services.archive_store import FilesystemArchiveStore
from zerg.services.archive_transcript import archive_owning_session_ids

PARENT_ID = UUID("11111111-0000-0000-0000-0000000000aa")
AGENT_ID = UUID("22222222-0000-0000-0000-0000000000bb")
RUN = "wf_testrun01"
PROJ = "/Users/davidrose/git/g55"
AGENT_PATH = f"{PROJ}/{PARENT_ID}/subagents/workflows/{RUN}/agent-aaa111.jsonl"
AGENT_RAW = '{"type":"user","isSidechain":true,"agentId":"aaa111","message":{"content":"x"}}'


def _factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'relink_cov.db'}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _agent_payload():
    return SessionIngest(
        id=AGENT_ID, provider="claude", environment="production", project="g55",
        started_at="2026-01-01T00:00:00Z", provider_session_id=str(AGENT_ID),
        is_sidechain=True, parent_provider_session_id=str(PARENT_ID),
        subagent_id="aaa111", workflow_run_id=RUN, attribution_agent="workflow-subagent",
        events=[EventIngest(role="user", content_text="x", timestamp="2026-01-01T00:00:01Z",
                            source_path=AGENT_PATH, source_offset=0, raw_json=AGENT_RAW)],
    )


def _parent_payload():
    return SessionIngest(
        id=PARENT_ID, provider="claude", environment="production", project="g55",
        started_at="2026-01-01T00:00:00Z", provider_session_id=str(PARENT_ID),
        events=[EventIngest(role="user", content_text="root", timestamp="2026-01-01T00:00:00Z",
                            source_path=f"{PROJ}/{PARENT_ID}.jsonl", source_offset=0,
                            raw_json='{"type":"user","uuid":"root","message":{"content":"root"}}')],
    )


def test_relinked_subagent_coverage_resolves_across_child_id(tmp_path, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_ROOT", str(tmp_path / "archive"))
    factory = _factory(tmp_path)

    # 1. Agent ships before parent -> standalone orphan under AGENT_ID.
    with factory() as db:
        r = AgentsStore(db).ingest_session(_agent_payload())
        db.commit()
        assert r.session_id == AGENT_ID

    # 2. Seed the archive chunk for the agent's source line UNDER ITS OWN ID
    #    (this is where archive-primary wrote it at ingest time).
    store = FilesystemArchiveStore(tmp_path / "archive")
    from zerg.services.archive_primary import insert_archive_chunk_manifests
    chunks = store.write_record_chunks([
        ArchiveRecord(tenant_id="default", session_id=str(AGENT_ID), stream="source_lines",
                      source_seq=1, raw_bytes=AGENT_RAW.encode(), source_path=AGENT_PATH, source_offset=0)
    ], target_uncompressed_bytes=1 << 20)
    with factory() as db:
        insert_archive_chunk_manifests(db, chunks)
        db.commit()

    # 3. Parent ships -> relink moves the agent's source_lines to PARENT_ID,
    #    deletes the child AgentSession, leaves the archive chunk under AGENT_ID.
    with factory() as db:
        AgentsStore(db).ingest_session(_parent_payload())
        db.commit()

    with factory() as db:
        # The source line now lives under the parent...
        moved = db.query(AgentSourceLine).filter(
            AgentSourceLine.session_id == PARENT_ID, AgentSourceLine.source_path == AGENT_PATH
        ).all()
        assert moved, "subagent source line should be relinked under the parent"
        # ...and the original child id survives as a longhouse_session_id alias.
        owners = archive_owning_session_ids(db, PARENT_ID)
        assert str(AGENT_ID) in owners, f"child id must be in owners, got {owners}"

        # The reclaim verifier must now find the agent row covered (via child id),
        # not report it missing.
        rep = verify_session_archive_coverage(db, PARENT_ID, archive_store=store)
        missing_agent = [m for m in rep.missing if m[0] == AGENT_PATH]
        assert not missing_agent, f"relinked subagent row should be archive-covered, missing={rep.missing}"
