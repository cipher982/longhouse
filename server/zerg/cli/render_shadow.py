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
    totals = {
        "sessions": len(sessions),
        "compared": 0,
        "equal": 0,
        "skipped": 0,
        "current": 0,
        "unrebuilt_epochs": 0,
        "lossy": 0,
        "differences": [],
        "failed": [],
    }
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
            except Exception as exc:  # noqa: BLE001 - one session's failure is reported, not fatal
                totals["failed"].append({"session_id": session_id, "error": f"{type(exc).__name__}: {exc}"})
                typer.echo(json.dumps({"session_id": session_id, "failed": f"{type(exc).__name__}: {exc}"}))
                continue
            totals["current"] += int(bool(result.get("current")))
            totals["unrebuilt_epochs"] += int(result.get("unrebuilt_epochs") or 0)
            if not apply:
                totals["compared"] += result["compared"]
                totals["equal"] += result["equal"]
                totals["lossy"] += result["lossy"]
                for difference in result["differences"]:
                    if difference["lossy"] or len(totals["differences"]) < 20:
                        totals["differences"].append({"session_id": session_id, **difference})
    finally:
        await worker.close()
        await render_workers.close()
        await raw_workers.close()
    typer.echo(json.dumps(totals, indent=2, sort_keys=True))
    # A check that compared nothing, or left an epoch unrebuilt, proves nothing.
    # A new normalizer revision may add rows and item facts; it may not lose
    # or change a row the host serves.
    if totals["failed"] or totals["unrebuilt_epochs"] or (not apply and (totals["compared"] == 0 or totals["lossy"])):
        raise typer.Exit(1)


def semantic_backfill(
    session: list[str] = typer.Option(
        [], "--session", help="Session to backfill; repeatable. Default: every session with an older object."
    ),
    apply: bool = typer.Option(False, "--apply", help="Re-seal and mark the objects. Default reports only."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="At most this many sessions."),
    socket_path: Path | None = typer.Option(None, "--socket", help="Catalog socket; defaults to this Runtime Host's."),
) -> None:
    """Make every current render object's classification final (host-normalized-transcripts Phase 6).

    An object below semantic projection version 2 is replayed once from raw.
    Without --apply nothing is written: the report counts the objects and how
    many would change. With --apply an object whose kinds change is re-sealed
    and replaced in its generation (the old object retired, not deleted), and
    every replayed object is marked final, so reads never open raw for it.
    """
    asyncio.run(_semantic_backfill(session, apply=apply, limit=limit, socket_path=socket_path))


async def _semantic_backfill(sessions: list[str], *, apply: bool, limit: int | None, socket_path: Path | None) -> None:
    import sqlite3

    from zerg.catalogd.client import CatalogClient
    from zerg.services.catalogd_supervisor import catalogd_paths
    from zerg.services.raw_object_workers import RawObjectWorkerPool
    from zerg.services.raw_object_workers import storage_v2_root
    from zerg.services.render_object_workers import RenderObjectWorkerPool
    from zerg.services.storage_v2_semantics import recover_render_spec
    from zerg.services.storage_v2_semantics import repair_storage_session_semantic_projection
    from zerg.storage_v2.render_objects import SEMANTIC_PROJECTION_VERSION

    database, default_socket = catalogd_paths()
    connection = sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True, timeout=5)
    try:
        query = (
            "SELECT s.session_id, s.owner_id, s.current_render_generation, o.object_id, o.object_path, o.object_hash"
            " FROM sessions s JOIN render_objects o ON o.generation_id = s.current_render_generation"
            " WHERE o.retired_at IS NULL AND o.semantic_projection_version < ?"
            + (f" AND s.session_id IN ({','.join('?' * len(sessions))})" if sessions else "")
            + " ORDER BY s.session_id"
        )
        pending: dict[str, dict[str, object]] = {}
        for session_id, owner_id, generation_id, object_id, object_path, object_hash in connection.execute(
            query, (SEMANTIC_PROJECTION_VERSION, *sessions)
        ):
            entry = pending.setdefault(str(session_id), {"owner_id": owner_id, "generation_id": str(generation_id), "objects": []})
            entry["objects"].append((object_id, object_path, object_hash))  # type: ignore[union-attr]
    finally:
        connection.close()
    selected = list(pending.items())[:limit] if limit is not None else list(pending.items())
    catalog = CatalogClient(socket_path or default_socket, default_timeout_seconds=60)
    raw_workers = RawObjectWorkerPool(storage_v2_root())
    render_workers = RenderObjectWorkerPool(storage_v2_root(), repair_workers=1)
    totals = {
        "sessions_below_final": len(pending),
        "sessions": len(selected),
        "objects": 0,
        "would_change": 0,
        "records_changed": 0,
        "failed": 0,
    }
    try:
        for session_id, entry in selected:
            objects = entry["objects"]
            totals["objects"] += len(objects)  # type: ignore[arg-type]
            try:
                if apply:
                    result = await repair_storage_session_semantic_projection(
                        catalog=catalog,
                        render_workers=render_workers,
                        raw_workers=raw_workers,
                        session_id=session_id,
                        owner_id=str(entry["owner_id"]),
                        generation_id=str(entry["generation_id"]),
                        replay_below=SEMANTIC_PROJECTION_VERSION,
                    )
                    if result.get("complete") is not True:
                        raise RuntimeError(f"repair did not complete: {result}")
                    continue
                cache: dict[str, dict[str, dict[str, object]]] = {}
                for _object_id, object_path, object_hash in objects:  # type: ignore[union-attr]
                    decoded = await render_workers.read(str(object_path), str(object_hash), lane="background")
                    recovered = await recover_render_spec(
                        catalog=catalog,
                        raw_workers=raw_workers,
                        session_id=session_id,
                        owner_id=str(entry["owner_id"]),
                        spec=decoded.spec,
                        manifest_cache=cache,
                    )
                    changed = sum(
                        old.interaction_kind != new.interaction_kind
                        for old, new in zip(decoded.spec.records, recovered.records, strict=True)
                    )
                    totals["would_change"] += int(changed > 0)
                    totals["records_changed"] += changed
            except Exception as exc:  # noqa: BLE001 - one session's failure is reported, not fatal
                totals["failed"] += 1
                typer.echo(json.dumps({"session_id": session_id, "failed": f"{type(exc).__name__}: {exc}"[:300]}))
    finally:
        await render_workers.close()
        await raw_workers.close()
    typer.echo(json.dumps(totals, indent=2, sort_keys=True))
    if totals["failed"]:
        raise typer.Exit(1)
