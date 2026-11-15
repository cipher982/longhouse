"""Host render authority: the Runtime Host builds the served render from raw.

control-plane `docs/specs/host-normalized-transcripts.md`, Phase 4.

The engine names the generation a host render belongs to (the same identity
the Machine Agent's shipper mints at that parser revision), so a host render
and a laptop render at one revision share a generation and nothing moves. When
they differ (a host revision bump, a laptop render that disagrees, a witnessed
envelope the laptop never rendered), `host_rebuild_session` fills the host
generation from raw: witnessed envelopes are rendered by the host, other native
envelopes carry their laptop render forward re-sealed into the generation, and
the catalog makes it current only once it covers what is served now.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections import defaultdict
from dataclasses import replace
from typing import Any
from uuid import UUID

from zerg.catalogd.client import CatalogClient
from zerg.services.raw_object_workers import RawObjectWorkerPool
from zerg.services.render_object_workers import RenderObjectWorkerPool
from zerg.services.render_shadow import CAPTURE_WITNESS
from zerg.services.render_shadow import READ_QUEUE_SECONDS
from zerg.services.render_shadow import NormalizerUnavailable
from zerg.services.render_shadow import NormalizerWorker
from zerg.services.render_shadow import _epoch_request
from zerg.services.render_shadow import host_render_spec
from zerg.storage_v2.render_objects import SEMANTIC_PROJECTION_VERSION

logger = logging.getLogger(__name__)

COMMIT_CHUNK = 500
INGEST_TIMEOUT_SECONDS = 10.0


class HostRenderSkipped(Exception):
    """The session cannot be rebuilt into a complete host generation now."""


def render_manifest(spec: Any, sealed: Any) -> dict[str, Any]:
    """The catalog manifest of one sealed render object."""
    return {
        "generation_id": str(spec.render_generation),
        "parser_revision": spec.parser_revision,
        "ordering_revision": spec.ordering_revision,
        "object_id": sealed.object_id,
        "object_hash": sealed.object_hash,
        "payload_hash": sealed.payload_hash,
        "object_path": sealed.object_path,
        "uncompressed_size": sealed.uncompressed_size,
        "compressed_size": sealed.compressed_size,
        "event_count": sealed.event_count,
        "first_order_key": sealed.first_order_key,
        "last_order_key": sealed.last_order_key,
        "user_messages": sealed.user_messages,
        "assistant_messages": sealed.assistant_messages,
        "tool_calls": sealed.tool_calls,
        "abandoned_events": sealed.abandoned_events,
        "first_user_message_preview": sealed.first_user_message_preview,
        "last_visible_text_preview": sealed.last_visible_text_preview,
        "user_turns": list(sealed.user_turns),
        # Claude local-command evidence can arrive in a later raw envelope, so
        # Claude projections stay pending until the semantic projector replays.
        "semantic_projection_version": 0 if spec.provider.strip().lower() == "claude" else SEMANTIC_PROJECTION_VERSION,
    }


async def host_rebuild_session(
    *,
    catalog: CatalogClient,
    raw_workers: RawObjectWorkerPool,
    render_workers: RenderObjectWorkerPool,
    worker: NormalizerWorker,
    session_id: str,
) -> dict[str, Any]:
    """Rebuild one session into its host generation and commit it.

    Returns the host-rendered envelope ids and whether the host generation is
    now current. Raises `HostRenderSkipped` when a complete generation cannot
    be built (an epoch too large, deleted media, a normalizer error), leaving
    what is served untouched.
    """
    inputs = await catalog.call("storage.session.rebuild_inputs.v2", {"session_id": session_id})
    if inputs.get("found") is not True:
        raise HostRenderSkipped("session not found")
    if inputs.get("too_large") is True:
        raise HostRenderSkipped("session too large to rebuild")
    current = inputs.get("current_render_generation")
    capture = {(fact["source_epoch"], fact["source_position"]): fact for fact in inputs["facts"] if fact["kind"] == CAPTURE_WITNESS}
    media_by_envelope: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in inputs["media"]:
        media_by_envelope[row["envelope_id"]].append(row)
    laptop_objects = {row["source_envelope_id"]: row for row in inputs["render_objects"]}
    epochs: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in inputs["raw_objects"]:
        epochs[(row["machine_id"], row["provider"], row["opaque_source_id"], row["source_epoch"])].append(row)
    manifest_cache: dict[str, dict[str, dict[str, object]]] = {}

    generation: dict[str, Any] | None = None
    objects: list[dict[str, Any]] = []
    host_envelopes: list[str] = []
    for (_machine, provider, _source, epoch), rows in epochs.items():
        rows.sort(key=lambda row: row["range_start"])
        if not any((epoch, row["range_start"]) in capture for row in rows):
            continue
        try:
            request, raw_specs = await _epoch_request(
                raw_workers=raw_workers,
                inputs=inputs,
                session_id=session_id,
                provider=provider,
                epoch=epoch,
                rows=rows,
                capture=capture,
                media_by_envelope=media_by_envelope,
            )
        except NormalizerUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - nothing partial is committed
            raise HostRenderSkipped(f"reading epoch {epoch}: {type(exc).__name__}: {exc}") from exc
        response = await worker.rebuild(request)
        if "error" in response or not isinstance(response.get("generation"), dict):
            raise HostRenderSkipped(f"normalizer: {response.get('error') or 'no generation named'}")
        epoch_generation = response["generation"]
        if generation is None:
            generation = epoch_generation
        elif generation["generation_id"] != epoch_generation["generation_id"]:
            raise HostRenderSkipped("session spans two host generations")
        answers = {int(answer.get("range_start", -1)): answer for answer in response.get("envelopes") or []}
        for row in rows:
            if (epoch, row["range_start"]) not in capture:
                continue
            answer = answers.get(row["range_start"])
            if answer is None or answer.get("error"):
                raise HostRenderSkipped(f"normalizer: {answer.get('error') if answer else 'no answer'} for {row['envelope_id']}")
            rebuilt = answer["render"]
            if rebuilt.get("withheld"):
                # The parser holds this range back (an unfinished turn): no object.
                host_envelopes.append(row["envelope_id"])
                continue
            spec = await host_render_spec(
                catalog=catalog,
                raw_workers=raw_workers,
                session_id=session_id,
                owner_id=str(inputs["owner_id"]) if inputs.get("owner_id") is not None else None,
                raw_spec=raw_specs[row["envelope_id"]],
                envelope_id=row["envelope_id"],
                records=rebuilt["records"],
                generation=epoch_generation,
                manifest_cache=manifest_cache,
            )
            sealed = await render_workers.seal(spec, lane="repair")
            objects.append(
                {
                    **render_manifest(spec, sealed),
                    "source_envelope_id": row["envelope_id"],
                    "render_source": "host",
                    "object_parser_revision": epoch_generation["parser_revision"],
                }
            )
            host_envelopes.append(row["envelope_id"])
    if generation is None:
        raise HostRenderSkipped("no witnessed envelope")

    if current is not None and str(current) != generation["generation_id"]:
        # Carry every other native envelope the current generation serves.
        rendered = set(host_envelopes)
        for row in inputs["raw_objects"]:
            laptop = laptop_objects.get(row["envelope_id"])
            if (
                row["envelope_id"] in rendered
                or row.get("provenance_kind") != "native"
                or laptop is None
                or str(laptop.get("generation_id")) != str(current)
            ):
                continue
            decoded = await render_workers.read(
                laptop["object_path"],
                laptop["object_hash"],
                lane="background",
                queue_timeout_seconds=READ_QUEUE_SECONDS,
                operation_timeout_seconds=30.0,
            )
            spec = replace(
                decoded.spec,
                render_generation=UUID(generation["generation_id"]),
                parser_revision=generation["parser_revision"],
                ordering_revision=generation["ordering_revision"],
            )
            sealed = await render_workers.seal(spec, lane="repair")
            objects.append(
                {
                    **render_manifest(spec, sealed),
                    "source_envelope_id": row["envelope_id"],
                    "render_source": "laptop",
                    "object_parser_revision": laptop.get("parser_revision"),
                }
            )

    result: dict[str, Any] = {"current": False, "written": 0}
    for start in range(0, max(len(objects), 1), COMMIT_CHUNK):
        chunk = objects[start : start + COMMIT_CHUNK]
        answer = await catalog.call(
            "storage.render.host_commit.v2",
            {
                "session_id": session_id,
                "generation_id": generation["generation_id"],
                "parser_revision": generation["parser_revision"],
                "ordering_revision": generation["ordering_revision"],
                "objects": [
                    {key: value for key, value in item.items() if key not in {"generation_id", "parser_revision", "ordering_revision"}}
                    for item in chunk
                ],
                "promote": start + COMMIT_CHUNK >= len(objects),
            },
        )
        result = {"current": answer.get("current"), "written": result["written"] + int(answer.get("written") or 0)}
    logger.info(
        "host render session=%s generation=%s objects=%d written=%d current=%s",
        session_id,
        generation["generation_id"],
        len(objects),
        result["written"],
        result["current"],
    )
    return {**result, "generation_id": generation["generation_id"], "host_envelopes": host_envelopes}


_ingest_worker: NormalizerWorker | None = None
_scheduled: dict[str, asyncio.Task[None]] = {}


def _ingest_normalizer() -> NormalizerWorker | None:
    """A normalizer for ingest, separate from the sweep's so a long epoch rebuild never queues a live envelope."""
    global _ingest_worker
    from zerg.services.render_shadow import host_authority_enabled
    from zerg.services.render_shadow import normalizer_binary

    if not host_authority_enabled() or normalizer_binary() is None:
        return None
    if _ingest_worker is None:
        # A live envelope never waits long on the host: past this, the laptop
        # render is sealed and the sweep rebuilds it.
        _ingest_worker = NormalizerWorker(timeout_seconds=INGEST_TIMEOUT_SECONDS)
    return _ingest_worker


async def close_ingest_normalizer() -> None:
    global _ingest_worker
    worker, _ingest_worker = _ingest_worker, None
    for task in list(_scheduled.values()):
        task.cancel()
    _scheduled.clear()
    if worker is not None:
        await worker.close()


async def render_at_ingest(
    *,
    catalog: CatalogClient,
    raw_workers: RawObjectWorkerPool,
    raw_spec: Any,
    parsed: dict[str, Any],
    owner_id: str | None,
    lane: str,
) -> dict[str, Any] | None:
    """The render ingest seals for one envelope, owned by the host.

    Returns None when host authority is off (the laptop render is sealed as
    before). Otherwise names the host generation and the render to seal in
    it: the host's own for a witnessed byte-offset envelope without media,
    else the laptop's re-targeted into the host generation (the sweep
    rebuilds it from raw when it differs). `spec` is None when there is no
    render to seal.
    """
    worker = _ingest_normalizer()
    if worker is None:
        return None
    witness = [
        {key: fact[key] for key in ("kind", "at", "source_position", "payload")}
        for fact in parsed.get("provider_facts") or []
        if fact.get("kind") == CAPTURE_WITNESS and fact.get("source_position") == raw_spec.range_start
    ]
    host_renders = bool(witness) and raw_spec.range_kind == "byte_offset" and not parsed.get("media_refs")
    envelope = {
        "range_start": raw_spec.range_start,
        "range_end": raw_spec.range_end,
        "records": [
            {"source_position": record.source_position, "data_b64": base64.b64encode(record.data).decode("ascii")}
            for record in raw_spec.records
        ],
        "facts": witness,
        "media": [],
    }
    request = {
        "id": "ingest",
        "provider": raw_spec.provider,
        "session_id": str(raw_spec.session_id),
        "source_epoch": str(raw_spec.source_epoch),
        "epoch_opened_at": parsed["opened_at"].isoformat(),
        "range_kind": raw_spec.range_kind,
        # With no envelope the worker still names the generation.
        "envelopes": [envelope] if host_renders else [],
        "media": {},
    }
    try:
        answer = await worker.rebuild(request)
    except Exception:  # noqa: BLE001 - the laptop render is sealed as before
        logger.warning("host render at ingest unavailable for session %s", raw_spec.session_id, exc_info=True)
        return None
    generation = answer.get("generation")
    if not isinstance(generation, dict):
        return None
    rendered = (answer.get("envelopes") or [None])[0] if host_renders and "error" not in answer else None
    if rendered is not None and rendered.get("render") is not None and not rendered.get("error"):
        if rendered["render"].get("withheld"):
            return {
                "generation": generation,
                "spec": None,
                "render_source": "host",
                "object_parser_revision": generation["parser_revision"],
            }
        spec = await host_render_spec(
            catalog=catalog,
            raw_workers=raw_workers,
            session_id=str(raw_spec.session_id),
            owner_id=owner_id,
            raw_spec=raw_spec,
            envelope_id=parsed["expected_envelope_id"],
            records=rendered["render"]["records"],
            generation=generation,
            manifest_cache={},
            lane=lane,
            projector_read=False,
        )
        return {"generation": generation, "spec": spec, "render_source": "host", "object_parser_revision": generation["parser_revision"]}
    laptop = parsed.get("render_spec")
    if laptop is None:
        return {"generation": generation, "spec": None, "render_source": "laptop", "object_parser_revision": None}
    retargeted = replace(
        laptop,
        render_generation=UUID(generation["generation_id"]),
        parser_revision=generation["parser_revision"],
        ordering_revision=generation["ordering_revision"],
    )
    return {"generation": generation, "spec": retargeted, "render_source": "laptop", "object_parser_revision": laptop.parser_revision}


def schedule_host_rebuild(session_id: str) -> None:
    """Fill a host generation that is not served yet, once per session at a time."""
    if session_id in _scheduled:
        return
    from zerg.services.render_shadow import _worker as sweep_worker
    from zerg.services.render_shadow import rebuild_context

    context = rebuild_context()
    if context is None or sweep_worker is None:
        return

    async def run() -> None:
        try:
            await host_rebuild_session(worker=sweep_worker, session_id=session_id, **context)
        except HostRenderSkipped as exc:
            logger.info("host render session=%s skipped: %s", session_id, exc)
        except Exception:  # noqa: BLE001 - the sweep retries it
            logger.exception("host render rebuild failed session=%s", session_id)
        finally:
            _scheduled.pop(session_id, None)

    _scheduled[session_id] = asyncio.create_task(run())
