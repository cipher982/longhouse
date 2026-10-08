"""Operator CLI for storage-v2 repairs and the record of the 2026-07 legacy conversion."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import UUID

import typer

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.config import get_settings
from zerg.services.catalogd_supervisor import catalogd_paths
from zerg.services.legacy_twins import DEFAULT_WINDOW_SECONDS
from zerg.services.legacy_twins import find_legacy_twins
from zerg.services.raw_object_workers import storage_v2_root

app = typer.Typer(help="Storage-v2 repair and legacy-conversion records: status, twin and relink reconciles, generation restore.")


def _print(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


@app.command("status")
def status(
    run_id: UUID = typer.Option(..., "--run-id"),
) -> None:
    """Return the 2026-07 conversion ledger's coverage summary as JSON."""

    _, socket_path = catalogd_paths()
    catalog = CatalogClient(socket_path)

    async def execute() -> dict:
        try:
            return await catalog.call("migration.run.summary.v2", {"run_id": str(run_id)}, timeout_seconds=5.0)
        finally:
            await catalog.close()

    _print(asyncio.run(execute()))


@app.command("reconcile-relinked-legacy")
def reconcile_relinked_legacy(
    session_ids: list[UUID] = typer.Option(..., "--session-id"),
) -> None:
    """Sequentially retire sessions proven duplicated by a relinked native source."""

    if not 1 <= len(set(session_ids)) <= 100:
        raise typer.BadParameter("provide 1 to 100 unique --session-id values")
    _, socket_path = catalogd_paths()
    catalog = CatalogClient(socket_path)

    async def execute() -> dict:
        results = []
        try:
            for session_id in session_ids:
                try:
                    result = await catalog.call(
                        "storage.session.relinked_legacy.reconcile.v2",
                        {
                            "session_id": str(session_id),
                            "observed_at": datetime.now(UTC).isoformat(),
                        },
                        timeout_seconds=30.0,
                    )
                    results.append({"session_id": str(session_id), "ok": True, "result": result})
                except CatalogRemoteError as exc:
                    results.append(
                        {
                            "session_id": str(session_id),
                            "ok": False,
                            "error": {"code": exc.code, "message": str(exc), "details": exc.details},
                        }
                    )
            failed = sum(result["ok"] is False for result in results)
            return {"reconciled": len(results) - failed, "failed": failed, "results": results}
        finally:
            await catalog.close()

    output = asyncio.run(execute())
    _print(output)
    if output["failed"]:
        raise typer.Exit(code=1)


@app.command("reconcile-legacy-twins")
def reconcile_legacy_twins(
    session_ids: list[UUID] = typer.Option([], "--session-id", help="Limit to these legacy sessions (default: all)."),
    window_seconds: int = typer.Option(DEFAULT_WINDOW_SECONDS, "--window-seconds", min=1, max=600),
    apply: bool = typer.Option(False, "--apply", help="Retire every qualifying legacy copy; without it, only report."),
) -> None:
    """Report, and with --apply retire, legacy copies of conversations a native session already holds."""

    settings = get_settings()
    live_database, socket_path = catalogd_paths()
    evidence = find_legacy_twins(
        live_database=live_database,
        object_root=storage_v2_root(),
        archive_root=Path(settings.archive_root),
        session_ids=[str(value) for value in session_ids] or None,
        window_seconds=window_seconds,
    )
    qualifying = [item for item in evidence if item.qualifies]
    output: dict = {
        "rule": {"window_seconds": window_seconds},
        "pairs": [item.as_dict() for item in evidence],
        "qualifying": len(qualifying),
        "applied": [],
    }
    if apply and qualifying:
        catalog = CatalogClient(socket_path)

        async def execute() -> list[dict]:
            results = []
            try:
                for item in qualifying:
                    try:
                        result = await catalog.call(
                            "storage.session.legacy_twin.retire.v2",
                            {
                                "session_id": item.legacy_session_id,
                                "twin_session_id": item.twin_session_id,
                                "window_seconds": window_seconds,
                                "observed_at": datetime.now(UTC).isoformat(),
                            },
                            timeout_seconds=30.0,
                        )
                        results.append({"session_id": item.legacy_session_id, "ok": True, "result": result})
                    except CatalogRemoteError as exc:
                        results.append(
                            {
                                "session_id": item.legacy_session_id,
                                "ok": False,
                                "error": {"code": exc.code, "message": str(exc), "details": exc.details},
                            }
                        )
            finally:
                await catalog.close()
            return results

        output["applied"] = asyncio.run(execute())
    _print(output)
    if any(result["ok"] is False for result in output["applied"]):
        raise typer.Exit(code=1)


@app.command("restore-generation")
def restore_generation(
    session_id: UUID = typer.Option(..., "--session-id"),
    generation_id: UUID = typer.Option(..., "--generation-id"),
) -> None:
    """Restore a complete generation when the selected generation lost its raw source."""

    _, socket_path = catalogd_paths()
    catalog = CatalogClient(socket_path)

    async def execute() -> dict:
        try:
            return await catalog.call(
                "storage.session.render_generation.restore.v2",
                {
                    "session_id": str(session_id),
                    "generation_id": str(generation_id),
                    "observed_at": datetime.now(UTC).isoformat(),
                },
                timeout_seconds=30.0,
            )
        finally:
            await catalog.close()

    _print(asyncio.run(execute()))


__all__ = ["app"]
