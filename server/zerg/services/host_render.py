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
from dataclasses import asdict
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
# Providers whose render the host builds from one envelope and its witness
# alone; the engine's HOST_RENDERS_ALONE stops shipping their render. Codex,
# Pi and OMP read their file header from earlier in the epoch, so ingest seals
# their laptop render and the sweep rebuilds them from the whole epoch.
HOST_RENDERS_ALONE = frozenset({"claude", "antigravity"})
INGEST_TIMEOUT_SECONDS = 10.0


class HostRenderSkipped(Exception):
    """The session cannot be rebuilt into a complete host generation now."""


class HostRenderUnavailable(Exception):
    """Host authority is on but the normalizer did not answer: the envelope is retried."""


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
    only_missing: bool = False,
    commit: bool = True,
) -> dict[str, Any]:
    """Rebuild one session into its host generation and commit it.

    Witnessed envelopes are rendered by the host; an envelope the parser holds
    back becomes an empty host object. When the host generation is not the
    current one, every other native envelope the current generation serves is
    carried forward, and so is any epoch the host could not rebuild (too large,
    media deleted, a normalizer error), so the generation always covers what
    is served and nothing new waits on a rebuild that cannot finish.

    With `only_missing`, an epoch whose witnessed envelopes are all already
    host-rendered into the host's generation is skipped; the rest are rebuilt
    (raw ingest could not render alone, a laptop render, an older generation).

    With `commit=False` nothing is sealed or committed: each host render is
    compared row by row with the render served for that envelope.
    """
    from zerg.services.render_shadow import _first_difference
    from zerg.services.render_shadow import _render_row
    from zerg.services.render_shadow import _sort_key

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
    served_objects = {row["source_envelope_id"]: row for row in inputs["render_objects"]}
    epochs: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in inputs["raw_objects"]:
        epochs[(row["machine_id"], row["provider"], row["opaque_source_id"], row["source_epoch"])].append(row)
    owner_id = str(inputs["owner_id"]) if inputs.get("owner_id") is not None else None
    manifest_cache: dict[str, dict[str, dict[str, object]]] = {}

    async def served_rows(envelope_id: str) -> list[dict[str, Any]]:
        served = served_objects.get(envelope_id)
        if served is None:
            return []
        decoded = await render_workers.read(
            served["object_path"],
            served["object_hash"],
            lane="background",
            queue_timeout_seconds=READ_QUEUE_SECONDS,
            operation_timeout_seconds=30.0,
        )
        return sorted((_render_row(asdict(record)) for record in decoded.spec.records), key=_sort_key)

    generation: dict[str, Any] | None = None
    # The generation this rebuild fills. An epoch already host-rendered into
    # it is skipped under `only_missing`; one served from an older generation
    # or by a laptop render sealed into this one is rebuilt.
    target = (await _name_generation(worker, inputs, session_id) or {}).get("generation_id") if only_missing else None
    host_rendered = {(envelope_id, generation_id) for envelope_id, generation_id in inputs.get("host_rendered") or []}
    objects: list[dict[str, Any]] = []
    compared: dict[str, Any] = {"compared": 0, "equal": 0, "differences": [], "unrebuilt_epochs": 0}
    host_envelopes: list[str] = []
    for (_machine, provider, _source, epoch), rows in epochs.items():
        rows.sort(key=lambda row: row["range_start"])
        witnessed = [row for row in rows if (epoch, row["range_start"]) in capture]
        if not witnessed or provider == "cursor":
            continue
        if only_missing and target is not None and all((row["envelope_id"], target) in host_rendered for row in witnessed):
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
            response = await worker.rebuild(request)
        except NormalizerUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - this epoch keeps its served render
            logger.info("host render session=%s epoch=%s not rebuilt: %s: %s", session_id, epoch, type(exc).__name__, exc)
            compared["unrebuilt_epochs"] += 1
            continue
        epoch_generation = response.get("generation")
        if not isinstance(epoch_generation, dict) or "error" in response:
            logger.info("host render session=%s epoch=%s not rebuilt: %s", session_id, epoch, response.get("error"))
            compared["unrebuilt_epochs"] += 1
            continue
        if generation is None:
            generation = epoch_generation
        elif generation["generation_id"] != epoch_generation["generation_id"]:
            raise HostRenderSkipped("session spans two host generations")
        answers = {int(answer.get("range_start", -1)): answer for answer in response.get("envelopes") or []}
        if any((answers.get(row["range_start"]) or {"error": "no answer"}).get("error") for row in witnessed):
            compared["unrebuilt_epochs"] += 1
            continue
        for row in witnessed:
            rebuilt = answers[row["range_start"]]["render"]
            # A range the parser holds back (an unfinished turn) is an empty
            # host object, so coverage holds and the laptop's is replaced.
            records = [] if rebuilt.get("withheld") else rebuilt["records"]
            spec = await host_render_spec(
                catalog=catalog,
                raw_workers=raw_workers,
                session_id=session_id,
                owner_id=owner_id,
                raw_spec=raw_specs[row["envelope_id"]],
                envelope_id=row["envelope_id"],
                records=records,
                generation=epoch_generation,
                manifest_cache=manifest_cache,
            )
            host_envelopes.append(row["envelope_id"])
            if not commit:
                host = sorted((_render_row(asdict(record)) for record in spec.records), key=_sort_key)
                difference = _first_difference(await served_rows(row["envelope_id"]), host)
                compared["compared"] += 1
                if difference is None:
                    compared["equal"] += 1
                elif len(compared["differences"]) < 5:
                    compared["differences"].append({"envelope_id": row["envelope_id"], "difference": difference})
                continue
            sealed = await render_workers.seal(spec, lane="repair")
            objects.append(
                {
                    **render_manifest(spec, sealed),
                    "source_envelope_id": row["envelope_id"],
                    "render_source": "host",
                    "object_parser_revision": epoch_generation["parser_revision"],
                }
            )
    if generation is None:
        generation = await _name_generation(worker, inputs, session_id)
    if generation is None:
        raise HostRenderSkipped("nothing to rebuild" if only_missing else "no generation to rebuild into")

    if not commit:
        return {**compared, "generation_id": generation["generation_id"], "current": str(current) == generation["generation_id"]}

    if current is not None and str(current) != generation["generation_id"]:
        # Carry every other native envelope the current generation serves.
        rendered = set(host_envelopes)
        for row in inputs["raw_objects"]:
            served = served_objects.get(row["envelope_id"])
            if (
                row["envelope_id"] in rendered
                or row.get("provenance_kind") != "native"
                or served is None
                or str(served.get("generation_id")) != str(current)
            ):
                continue
            decoded = await render_workers.read(
                served["object_path"],
                served["object_hash"],
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
                    # Keep who made it and with which parser: a carried object
                    # is not a host render.
                    "render_source": served.get("render_source") or "laptop",
                    "object_parser_revision": served.get("parser_revision"),
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
    if result["written"] and result["current"]:
        _wake_clients(session_id, provider=str(inputs.get("provider") or ""))
    logger.info(
        "host render session=%s generation=%s objects=%d written=%d current=%s unrebuilt_epochs=%d",
        session_id,
        generation["generation_id"],
        len(objects),
        result["written"],
        result["current"],
        compared["unrebuilt_epochs"],
    )
    return {
        **result,
        "generation_id": generation["generation_id"],
        "host_envelopes": host_envelopes,
        "unrebuilt_epochs": compared["unrebuilt_epochs"],
    }


def _wake_clients(session_id: str, *, provider: str) -> None:
    """Tell open timelines and detail pages that what this session serves changed."""
    from datetime import UTC
    from datetime import datetime

    from zerg.services.session_pubsub import TOPIC_TIMELINE
    from zerg.services.session_pubsub import get_pubsub
    from zerg.services.session_pubsub import topic_session

    payload = {
        "session_id": session_id,
        "kind": "ingest",
        "events_inserted": 0,
        "provider": provider,
        "source": "host_render",
        "server_fanout_at_ms": int(datetime.now(UTC).timestamp() * 1000),
    }
    bus = get_pubsub()
    bus.publish(topic_session(session_id), payload)
    bus.publish(TOPIC_TIMELINE, payload)


async def _name_generation(worker: NormalizerWorker, inputs: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    """The host generation of a session with nothing witnessed to rebuild, named by the normalizer."""
    for row in inputs["raw_objects"]:
        if row["provider"] == "cursor":
            continue
        answer = await worker.rebuild(
            {
                "id": "name",
                "provider": row["provider"],
                "session_id": session_id,
                "source_epoch": row["source_epoch"],
                "epoch_opened_at": inputs["epoch_opened_at"].get(row["source_epoch"], ""),
                "range_kind": row["range_kind"],
                "envelopes": [],
                "media": {},
            }
        )
        generation = answer.get("generation")
        return generation if isinstance(generation, dict) else None
    return None


_ingest_worker: NormalizerWorker | None = None
_scheduled: dict[str, asyncio.Task[None]] = {}
_rerun: set[str] = set()


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
    from zerg.services.render_shadow import rebuild_context

    worker = _ingest_normalizer()
    # Host authority needs the rebuild sweep running: a host generation that
    # waits for coverage is filled by it.
    if worker is None or rebuild_context() is None or raw_spec.provider == "cursor":
        # Cursor keeps the laptop's render authority for now: its store.db
        # render must replace a lossy agent-transcripts render whenever it
        # arrives, which a host generation's coverage rule would block.
        return None
    witness = [
        {key: fact[key] for key in ("kind", "at", "source_position", "payload")}
        for fact in parsed.get("provider_facts") or []
        if fact.get("kind") == CAPTURE_WITNESS and fact.get("source_position") == raw_spec.range_start
    ]
    host_renders = bool(witness) and raw_spec.range_kind == "byte_offset" and raw_spec.provider in HOST_RENDERS_ALONE
    refs: list[dict[str, Any]] = []
    media: dict[str, str] = {}
    if host_renders:
        try:
            refs, media = await _ingest_media(parsed.get("media_refs") or [], owner_id=owner_id)
        except Exception:  # noqa: BLE001 - the laptop render is sealed and the sweep rebuilds
            logger.info("host render at ingest: media unreadable for session %s", raw_spec.session_id, exc_info=True)
            host_renders = False
    envelope = {
        "range_start": raw_spec.range_start,
        "range_end": raw_spec.range_end,
        "records": [
            {"source_position": record.source_position, "data_b64": base64.b64encode(record.data).decode("ascii")}
            for record in raw_spec.records
        ],
        "facts": witness,
        "media": refs,
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
        "media": media,
    }
    try:
        # The whole call, lock wait included, so one slow render cannot hold
        # every queued live envelope.
        answer = await asyncio.wait_for(worker.rebuild(request), INGEST_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - the Machine Agent retries the envelope
        logger.warning("host render at ingest unavailable for session %s", raw_spec.session_id, exc_info=True)
        raise HostRenderUnavailable(str(exc)) from exc
    generation = answer.get("generation")
    if not isinstance(generation, dict):
        raise HostRenderUnavailable(f"normalizer named no generation: {answer.get('error')}")
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
        return {
            "generation": generation,
            "spec": spec,
            "render_source": "host",
            "object_parser_revision": generation["parser_revision"],
            "witnessed": True,
        }
    laptop = parsed.get("render_spec")
    if laptop is None:
        return {
            "generation": generation,
            "spec": None,
            "render_source": "laptop",
            "object_parser_revision": None,
            "witnessed": bool(witness),
        }
    retargeted = replace(
        laptop,
        render_generation=UUID(generation["generation_id"]),
        parser_revision=generation["parser_revision"],
        ordering_revision=generation["ordering_revision"],
    )
    return {
        "generation": generation,
        "spec": retargeted,
        "render_source": "laptop",
        "object_parser_revision": laptop.parser_revision,
        "witnessed": bool(witness),
    }


async def _ingest_media(refs: list[dict[str, Any]], *, owner_id: str | None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The envelope's media references and bytes, as the normalizer reads them.

    The Machine Agent uploads media before the envelope that names it, so the
    bytes are in the store by now.
    """
    if not refs:
        return [], {}
    if owner_id is None:
        raise LookupError("media needs an owner")
    from zerg.routers.agents_storage_v2 import read_storage_v2_media_bytes

    wire = []
    media: dict[str, str] = {}
    for ref in refs:
        wire.append(
            {
                "sha256": ref["media_hash"],
                "source_position": ref["source_position"],
                "ref_key": ref["ref_key"],
                "availability": ref["availability"],
            }
        )
        if ref["availability"] == "available" and ref["media_hash"] not in media:
            _hash, _manifest, data = await read_storage_v2_media_bytes(str(ref["media_hash"]), owner_id=int(owner_id))
            media[str(ref["media_hash"])] = base64.b64encode(data).decode("ascii")
    return wire, media


def schedule_host_rebuild(session_id: str) -> None:
    """Fill a host generation that is not served yet, once per session at a time."""
    if session_id in _scheduled:
        # Envelopes keep arriving while a rebuild runs; one more pass covers them.
        _rerun.add(session_id)
        return
    from zerg.services.render_shadow import _worker as sweep_worker
    from zerg.services.render_shadow import rebuild_context

    context = rebuild_context()
    if context is None or sweep_worker is None:
        # Ingest renders at the host only while the sweep runs, so this is a
        # worker that just died; the restarted sweep finds the session.
        logger.warning("host render session=%s not scheduled: normalizer sweep is not running", session_id)
        return

    async def run() -> None:
        try:
            while True:
                _rerun.discard(session_id)
                try:
                    await host_rebuild_session(worker=sweep_worker, session_id=session_id, only_missing=True, **context)
                except HostRenderSkipped as exc:
                    logger.info("host render session=%s skipped: %s", session_id, exc)
                if session_id not in _rerun:
                    break
        except Exception:  # noqa: BLE001 - the sweep retries it
            logger.exception("host render rebuild failed session=%s", session_id)
        finally:
            _scheduled.pop(session_id, None)
            _rerun.discard(session_id)

    _scheduled[session_id] = asyncio.create_task(run())
