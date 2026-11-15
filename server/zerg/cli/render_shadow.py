"""Read the host render shadow: how often the host rebuild equals the laptop render."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer


def render_shadow_summary(
    socket_path: Path | None = typer.Option(None, "--socket", help="Catalog socket; defaults to this Runtime Host's."),
) -> None:
    """Print per-provider outcome counts, rebuild latency and recent differences as JSON."""
    from zerg.catalogd.client import CatalogClient
    from zerg.services.catalogd_supervisor import catalogd_paths

    catalog = CatalogClient(socket_path or catalogd_paths()[1], default_timeout_seconds=30)
    summary = asyncio.run(catalog.call("render.shadow.summary.v2", {}))
    typer.echo(json.dumps(summary, indent=2, sort_keys=True))


def host_rebuild(
    session: list[str] = typer.Option([], "--session", help="Session to rebuild; repeatable. Default: every witnessed session."),
    apply: bool = typer.Option(False, "--apply", help="Seal and commit the host generation. Default compares only."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="At most this many sessions."),
    machine: str | None = typer.Option(None, "--machine", help="Only sessions shipped by this machine id."),
    socket_path: Path | None = typer.Option(None, "--socket", help="Catalog socket; defaults to this Runtime Host's."),
) -> None:
    """Rebuild sessions from raw with this host's normalizer (host-normalized-transcripts Phase 4).

    Without --apply nothing is written: each witnessed envelope's host render
    is built in memory and its event count compared with the render served.
    """
    asyncio.run(_host_rebuild(session, apply=apply, limit=limit, machine=machine, socket_path=socket_path))


async def _host_rebuild(sessions: list[str], *, apply: bool, limit: int | None, machine: str | None, socket_path: Path | None) -> None:
    import sqlite3

    from zerg.catalogd.client import CatalogClient
    from zerg.services.catalogd_supervisor import catalogd_paths
    from zerg.services.host_render import HostRenderSkipped
    from zerg.services.host_render import host_rebuild_session
    from zerg.services.raw_object_workers import RawObjectWorkerPool
    from zerg.services.raw_object_workers import storage_v2_root
    from zerg.services.render_object_workers import RenderObjectWorkerPool
    from zerg.services.render_shadow import NormalizerWorker

    database, default_socket = catalogd_paths()
    if not sessions:
        connection = sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True, timeout=5)
        try:
            query = (
                "SELECT DISTINCT f.session_id FROM session_provider_facts f"
                + (" JOIN raw_objects r ON r.session_id = f.session_id AND r.machine_id = ?" if machine else "")
                + " WHERE f.kind = 'capture.witness' ORDER BY f.session_id"
            )
            sessions = [row[0] for row in connection.execute(query, (machine,) if machine else ())]
        finally:
            connection.close()
    if limit is not None:
        sessions = sessions[:limit]
    catalog = CatalogClient(socket_path or default_socket, default_timeout_seconds=60)
    raw_workers = RawObjectWorkerPool(storage_v2_root())
    render_workers = RenderObjectWorkerPool(storage_v2_root(), repair_workers=1)
    worker = NormalizerWorker()
    totals = {"sessions": len(sessions), "compared": 0, "equal": 0, "skipped": 0, "current": 0, "differences": []}
    try:
        for session_id in sessions:
            try:
                result = await host_rebuild_session(
                    catalog=catalog,
                    raw_workers=raw_workers,
                    render_workers=render_workers,
                    worker=worker,
                    session_id=session_id,
                    commit=apply,
                )
            except HostRenderSkipped as exc:
                totals["skipped"] += 1
                typer.echo(json.dumps({"session_id": session_id, "skipped": str(exc)}))
                continue
            totals["current"] += int(bool(result.get("current")))
            if not apply:
                totals["compared"] += result["compared"]
                totals["equal"] += result["equal"]
                for difference in result["differences"]:
                    if len(totals["differences"]) < 20:
                        totals["differences"].append({"session_id": session_id, **difference})
    finally:
        await worker.close()
        await render_workers.close()
        await raw_workers.close()
    typer.echo(json.dumps(totals, indent=2, sort_keys=True))
    if not apply and totals["compared"] != totals["equal"]:
        raise typer.Exit(1)
