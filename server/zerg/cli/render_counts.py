"""Explicit, render-only preparation and application of historical branch counts."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections import defaultdict
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import typer


def _snapshot(database: Path, limit: int | None = None, *, missing_only: bool = False) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    deadline = time.monotonic() + 120
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(render_objects)")}
        if missing_only and "abandoned_events" not in columns:
            raise ValueError("Deploy the count-fact catalog writer before applying the repair")
        query = """SELECT r.object_id, r.session_id, r.generation_id, r.object_path,
            r.object_hash, r.event_count, r.source_envelope_id, s.owner_id
            FROM render_objects r JOIN sessions s ON s.current_render_generation=r.generation_id
            WHERE r.retired_at IS NULL AND r.event_count>0"""
        if missing_only:
            query += " AND r.abandoned_events IS NULL"
        query += " ORDER BY r.object_id"
        if limit is not None:
            query += " LIMIT ?"
        return [dict(row) for row in connection.execute(query, (limit,) if limit is not None else ())]
    finally:
        connection.close()


def _cached_patch(manifest: dict[str, Any], receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    if not receipt or any(
        receipt.get(key) != manifest[key] for key in ("object_hash", "generation_id", "event_count", "session_id", "owner_id")
    ):
        return None
    count = receipt.get("abandoned_events")
    if type(count) is not int or not 0 <= count <= manifest["event_count"]:
        return None
    return {"object_id": manifest["object_id"], "event_count": manifest["event_count"], "abandoned_events": count}


async def repair_counts(
    *, database: Path, cache: Path, socket_path: Path | None, object_root: Path | None, apply: bool, limit: int | None
) -> dict[str, Any]:
    from zerg.catalogd.client import CatalogClient
    from zerg.services.provider_interaction_semantics import semantic_event_included
    from zerg.services.raw_object_workers import storage_v2_root
    from zerg.services.render_object_workers import RenderObjectWorkerPool

    if apply and limit is not None:
        raise ValueError("--limit is for preflight only; applying must verify the complete current scope")
    cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    summary = cache.with_suffix(".summary.json")
    summary.write_text(json.dumps({"status": "running", "phase": "snapshot"}) + "\n")
    receipts = {}
    if cache.exists():
        for line in cache.read_text().splitlines():
            receipt = json.loads(line)
            receipts[receipt["object_id"]] = receipt
    manifests = _snapshot(database, limit, missing_only=apply)
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    for manifest in manifests:
        if _cached_patch(manifest, receipts.get(manifest["object_id"])) is None:
            queue.put_nowait(manifest)
    result: dict[str, Any] = {
        "status": "running",
        "objects": len(manifests),
        "cached": len(manifests) - queue.qsize(),
        "computed": 0,
        "errors": [],
    }
    summary.write_text(json.dumps(result, indent=2) + "\n")
    typer.echo(json.dumps(result))
    started = time.monotonic()
    pool = RenderObjectWorkerPool(object_root or storage_v2_root(), repair_workers=2)
    try:
        with cache.open("a") as output:
            cache.chmod(0o600)

            async def worker() -> None:
                while not queue.empty() and len(result["errors"]) < 10:
                    manifest = queue.get_nowait()
                    try:
                        decoded = await pool.read(
                            manifest["object_path"],
                            manifest["object_hash"],
                            lane="background",
                            queue_timeout_seconds=5,
                            operation_timeout_seconds=30,
                        )
                        spec = decoded.spec
                        if (
                            str(spec.session_id) != manifest["session_id"]
                            or str(spec.render_generation) != manifest["generation_id"]
                            or str(spec.source_envelope_id) != str(manifest["source_envelope_id"])
                            or decoded.object_hash != manifest["object_hash"]
                            or len(spec.records) != manifest["event_count"]
                        ):
                            raise ValueError("immutable object identity/count mismatch")
                        count = sum(
                            record.branch_kind == "abandoned"
                            and semantic_event_included(
                                spec.provider, role=record.role, content_text=record.content_text, interaction_kind=record.interaction_kind
                            )
                            for record in spec.records
                        )
                        receipt = {
                            key: manifest[key]
                            for key in ("object_id", "object_hash", "generation_id", "session_id", "owner_id", "event_count")
                        }
                        receipt["abandoned_events"] = count
                        output.write(json.dumps(receipt, separators=(",", ":")) + "\n")
                        receipts[manifest["object_id"]] = receipt
                        result["computed"] += 1
                        if result["computed"] % 1000 == 0:
                            output.flush()
                            typer.echo(json.dumps({"computed": result["computed"], "remaining": queue.qsize()}))
                    except Exception as error:
                        # Decoder errors can contain source text; retain identity and type only.
                        result["errors"].append({"object_id": manifest["object_id"], "error_type": type(error).__name__})
                    finally:
                        queue.task_done()

            await asyncio.gather(worker(), worker())
        result["remaining_to_compute"] = queue.qsize()
        if not result["errors"] and queue.empty() and apply:
            # Re-read current membership after preparation; stale cache never grants authority.
            groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
            for manifest in _snapshot(database, missing_only=True):
                patch = _cached_patch(manifest, receipts.get(manifest["object_id"]))
                if patch is not None:
                    groups[(manifest["session_id"], manifest["generation_id"], manifest["owner_id"])].append(patch)
            catalog = CatalogClient(socket_path or database.parent / ".catalogd/catalogd.sock", default_timeout_seconds=10)
            result["updated_objects"] = 0
            result["stale_groups"] = 0
            try:
                for index, ((session_id, generation_id, owner_id), patches) in enumerate(groups.items(), 1):
                    for offset in range(0, len(patches), 1000):
                        repaired = await catalog.call(
                            "storage.session.semantic_projection.repair.v2",
                            {
                                "session_id": session_id,
                                "owner_id": owner_id,
                                "generation_id": generation_id,
                                "objects": patches[offset : offset + 1000],
                                "observed_at": datetime.now(UTC).isoformat(),
                            },
                        )
                        if repaired.get("conflict") or repaired.get("not_found"):
                            result["stale_groups"] += 1
                            break
                        result["updated_objects"] += int(repaired.get("updated_object_count") or 0)
                    if index % 1000 == 0:
                        typer.echo(json.dumps({"sessions_applied": index, "updated_objects": result["updated_objects"]}))
            finally:
                await catalog.close()
            result["missing_objects"] = len(_snapshot(database, missing_only=True))
        result["status"] = "pass" if not result["errors"] and not queue.qsize() and not result.get("missing_objects") else "fail"
        return result
    except BaseException:
        result["status"] = "interrupted"
        raise
    finally:
        await pool.close()
        result["elapsed_s"] = round(time.monotonic() - started, 2)
        summary.write_text(json.dumps(result, indent=2) + "\n")
        typer.echo(json.dumps(result))


def repair_render_counts(
    database: Path = typer.Option(..., "--database", exists=True, dir_okay=False, help="Authoritative catalog SQLite file."),
    cache: Path = typer.Option(..., "--cache", help="Resumable, private verified-count receipt file."),
    socket_path: Path | None = typer.Option(None, "--socket", help="Catalog writer socket; defaults beside the database."),
    object_root: Path | None = typer.Option(None, "--object-root", help="Immutable render root; defaults to Runtime Host settings."),
    apply: bool = typer.Option(False, "--apply", help="Apply verified counts through catalogd, then require zero missing current facts."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Bound a read-only preflight sample; cannot be combined with --apply."),
) -> None:
    """Prepare immutable render counts before upgrade; apply them with the updated writer."""
    result = asyncio.run(
        repair_counts(database=database, cache=cache, socket_path=socket_path, object_root=object_root, apply=apply, limit=limit)
    )
    if result["status"] != "pass":
        raise typer.Exit(code=1)


if __name__ == "__main__":
    typer.run(repair_render_counts)
