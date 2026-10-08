"""The 2026-07 legacy conversion ledger stays readable after the converter's retirement."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import insert

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.models import LegacyMigrationRun
from zerg.catalogd.models import LegacyMigrationSession
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon


@pytest.fixture
def paths(tmp_path):
    socket_dir = Path("/tmp") / f"lhcd-record-{uuid4().hex[:10]}"
    socket_dir.mkdir(mode=0o700)
    yield tmp_path / "live.db", socket_dir / "catalogd.sock"
    for path in socket_dir.iterdir():
        path.unlink(missing_ok=True)
    socket_dir.rmdir()


@pytest.mark.asyncio
async def test_conversion_summary_reads_the_recorded_ledger(paths):
    database, socket = paths
    run_id = uuid4()
    now = datetime(2026, 7, 13, 4, 28, tzinfo=UTC)
    engine = create_catalog_engine(database)
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(LegacyMigrationRun.__table__).values(
                run_id=str(run_id),
                legacy_high_watermark="{}",
                expected_session_count=2,
                state="degraded",
                commit_seq=1,
                created_at=now,
                updated_at=now,
                completed_at=now,
            )
        )
        for state, expected, covered in (("verified", 10, 10), ("degraded", 8, 5)):
            connection.execute(
                insert(LegacyMigrationSession.__table__).values(
                    run_id=str(run_id),
                    session_id=str(uuid4()),
                    state=state,
                    source_expected=expected,
                    source_covered=covered,
                    source_missing=expected - covered,
                    media_expected=0,
                    media_covered=0,
                    media_missing=0,
                    attempts=1,
                    commit_seq=1,
                    created_at=now,
                    updated_at=now,
                )
            )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database, socket_path=socket)
    await daemon.start()
    client = CatalogClient(socket)
    try:
        summary = await client.call("migration.run.summary.v2", {"run_id": str(run_id)})
        assert summary["run"]["state"] == "degraded"
        assert summary["summary"]["state_counts"] == {"pending": 0, "migrating": 0, "verified": 1, "degraded": 1}
        assert summary["summary"]["source_missing"] == 3
        with pytest.raises(CatalogRemoteError) as missing:
            await client.call("migration.run.summary.v2", {"run_id": str(uuid4())})
        assert missing.value.code == "not_found"
    finally:
        await client.close()
        await daemon.close()
