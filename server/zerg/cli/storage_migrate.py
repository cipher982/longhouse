"""Operator CLI for the one-time legacy corpus to storage-v2 conversion."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import UUID

import typer

from zerg.catalogd.client import CatalogClient
from zerg.config import get_settings
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.services.catalogd_supervisor import catalogd_paths
from zerg.services.legacy_corpus_migration import LegacyCorpusConverter
from zerg.services.legacy_corpus_migration import create_inventory_run
from zerg.services.legacy_corpus_migration import freeze_high_watermark
from zerg.services.legacy_corpus_migration import inventory_rows
from zerg.services.raw_object_workers import storage_v2_root

app = typer.Typer(help="Convert the frozen legacy SQLite corpus to storage-v2.")


def _context(database_url: str | None) -> tuple:
    settings = get_settings()
    engine = make_engine(database_url or settings.database_url)
    factory = make_sessionmaker(engine)
    _, socket_path = catalogd_paths()
    return settings, engine, factory, CatalogClient(socket_path)


def _print(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


@app.command("dry-run")
def dry_run(
    database_url: str | None = typer.Option(None, "--database-url"),
) -> None:
    """Read the bounded inventory without writing objects or ledger rows."""

    _, engine, factory, _ = _context(database_url)
    try:
        with factory() as db:
            watermark = freeze_high_watermark(db)
            rows = inventory_rows(db, watermark)
        _print(
            {
                "dry_run": True,
                "legacy_high_watermark": watermark.encode(),
                "expected_session_count": len(rows),
                "source_expected": sum(row.source_expected for row in rows),
                "media_expected": sum(row.media_expected for row in rows),
            }
        )
    finally:
        engine.dispose()


@app.command("inventory")
def inventory(
    run_id: UUID | None = typer.Option(None, "--run-id"),
    database_url: str | None = typer.Option(None, "--database-url"),
) -> None:
    """Freeze a high-watermark and register its exact session inventory."""

    _, engine, factory, catalog = _context(database_url)

    async def execute() -> dict:
        try:
            with factory() as db:
                return await create_inventory_run(db, catalog, run_id=run_id)
        finally:
            await catalog.close()

    try:
        _print(asyncio.run(execute()))
    finally:
        engine.dispose()


@app.command("run")
def run(
    run_id: UUID = typer.Option(..., "--run-id"),
    workers: int = typer.Option(2, "--workers", min=1, max=32),
    database_url: str | None = typer.Option(None, "--database-url"),
    object_root: Path | None = typer.Option(None, "--object-root"),
) -> None:
    """Resume claimed sessions until no eligible ledger rows remain."""

    settings, engine, factory, catalog = _context(database_url)

    async def execute() -> dict:
        converter = LegacyCorpusConverter(
            session_factory=factory,
            catalog=catalog,
            object_root=object_root or storage_v2_root(),
            tenant_id=settings.archive_primary_tenant_id,
        )
        try:
            return await converter.migrate_run(run_id, workers=workers)
        finally:
            await catalog.close()

    try:
        _print(asyncio.run(execute()))
    finally:
        engine.dispose()


@app.command("status")
def status(
    run_id: UUID = typer.Option(..., "--run-id"),
    database_url: str | None = typer.Option(None, "--database-url"),
) -> None:
    """Return the durable migration coverage summary as JSON."""

    _, engine, _, catalog = _context(database_url)

    async def execute() -> dict:
        try:
            return await catalog.call("migration.run.summary.v2", {"run_id": str(run_id)}, timeout_seconds=5.0)
        finally:
            await catalog.close()

    try:
        _print(asyncio.run(execute()))
    finally:
        engine.dispose()


@app.command("reconcile")
def reconcile(
    run_id: UUID = typer.Option(..., "--run-id"),
    release_claims: bool = typer.Option(False, "--release-claims", help="Requeue all claims after stopping every converter."),
    database_url: str | None = typer.Option(None, "--database-url"),
) -> None:
    """Classify terminal gaps and optionally release stopped-worker claims."""

    _, engine, _, catalog = _context(database_url)

    async def execute() -> dict:
        try:
            return await catalog.call(
                "migration.run.reconcile.v2",
                {
                    "run_id": str(run_id),
                    "observed_at": datetime.now(UTC).isoformat(),
                    "release_claims": release_claims,
                },
                timeout_seconds=30.0,
            )
        finally:
            await catalog.close()

    try:
        _print(asyncio.run(execute()))
    finally:
        engine.dispose()


__all__ = ["app"]
