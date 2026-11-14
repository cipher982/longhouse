"""Host render shadow: rebuild stored envelopes with the engine and compare.

Host-normalized transcripts, Phase 3 (control-plane
`docs/specs/host-normalized-transcripts.md`). The Runtime Host runs the
engine's normalizer (`longhouse-engine normalize-worker`, built into the image
at this commit) as one long-lived child process. A paced sweep takes sessions
with envelopes it has not compared yet, rebuilds each source epoch from stored
raw records plus the envelopes' capture witnesses, and records per envelope
whether the rebuild equals the render the laptop shipped. Nothing here changes
what is served: the laptop render stays current until Phase 4.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from typing import Any
from uuid import uuid4

from zerg.catalogd.client import CatalogClient
from zerg.services.raw_object_workers import RawObjectWorkerPool
from zerg.services.render_object_workers import RenderObjectWorkerPool

logger = logging.getLogger(__name__)

CAPTURE_WITNESS = "capture.witness"
DEFAULT_BINARY = "/usr/local/bin/longhouse-engine"
# One epoch's request and answer each fit in one line; a large epoch is many MiB.
MAX_LINE_BYTES = 512 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 300.0
# Raw plus media bytes one epoch request may carry; larger epochs are recorded
# as too_large. The request is about 4/3 of this after base64.
MAX_EPOCH_BYTES = 128 * 1024 * 1024
# Background reads wait behind user and live work instead of failing fast.
READ_QUEUE_SECONDS = 30.0
SWEEP_IDLE_SECONDS = 60.0
RECORD_CHUNK = 1_000
SWEEP_PACE_SECONDS = 2.0
RENDER_FIELDS = (
    "event_id",
    "parent_uuid",
    "order_time_us",
    "source_position",
    "event_subordinal",
    "role",
    "content_text",
    "tool_name",
    "tool_input_json",
    "tool_output_text",
    "tool_call_id",
    "thread_id",
    "branch_kind",
    "interaction_kind",
    "raw_record_ordinal",
)


class NormalizerUnavailable(RuntimeError):
    """The engine binary is not installed in this image."""


def normalizer_binary() -> str | None:
    configured = os.getenv("LONGHOUSE_NORMALIZER_BIN", "").strip()
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    # Only the engine built into this image at this commit; a laptop engine on
    # PATH is another revision and would compare the wrong normalizer.
    return DEFAULT_BINARY if os.access(DEFAULT_BINARY, os.X_OK) else None


class NormalizerWorker:
    """One `longhouse-engine normalize-worker` child; one request at a time."""

    def __init__(self, binary: str | None = None, *, timeout_seconds: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self.binary = binary or normalizer_binary()
        self.timeout_seconds = timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self.peak_rss_kib = 0
        # The engine's parser revision, learned from its first answer.
        self.revision: str | None = None

    async def _ensure(self) -> asyncio.subprocess.Process:
        if self.binary is None:
            raise NormalizerUnavailable("longhouse-engine is not installed; set LONGHOUSE_NORMALIZER_BIN")
        if self._process is None or self._process.returncode is not None:
            self._process = await asyncio.create_subprocess_exec(
                self.binary,
                "normalize-worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=MAX_LINE_BYTES,
            )
        return self._process

    def _sample_rss(self) -> None:
        if self._process is None:
            return
        try:
            with open(f"/proc/{self._process.pid}/status", encoding="ascii") as status:
                for line in status:
                    if line.startswith("VmHWM:"):
                        self.peak_rss_kib = max(self.peak_rss_kib, int(line.split()[1]))
        except OSError:
            return

    async def rebuild(self, request: dict[str, Any]) -> dict[str, Any]:
        line_out = await asyncio.to_thread(lambda: json.dumps(request, separators=(",", ":")).encode() + b"\n")
        async with self._lock:
            process = await self._ensure()
            assert process.stdin is not None and process.stdout is not None
            try:
                process.stdin.write(line_out)
                await process.stdin.drain()
                line = await asyncio.wait_for(process.stdout.readline(), self.timeout_seconds)
            except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError, asyncio.LimitOverrunError, ValueError):
                await self._kill()
                raise
            self._sample_rss()
            if not line:
                await self._kill()
                raise RuntimeError("normalizer exited without answering")
        answer = await asyncio.to_thread(json.loads, line)
        if isinstance(answer, dict) and answer.get("revision"):
            self.revision = str(answer["revision"])
        return answer

    async def _kill(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()

    async def close(self) -> None:
        async with self._lock:
            process, self._process = self._process, None
            if process is not None and process.returncode is None:
                assert process.stdin is not None
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()


def _render_row(record: dict[str, Any]) -> dict[str, Any]:
    return {field: record.get(field) for field in RENDER_FIELDS}


def _sort_key(row: dict[str, Any]) -> tuple:
    return (row["order_time_us"], row["source_position"], row["event_subordinal"], row["event_id"])


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _first_difference(laptop: list[dict[str, Any]], host: list[dict[str, Any]]) -> str | None:
    if len(laptop) != len(host):
        laptop_ids = [row["event_id"] for row in laptop]
        host_ids = [row["event_id"] for row in host]
        return f"rows laptop={len(laptop)} host={len(host)}; only laptop={sorted(set(laptop_ids) - set(host_ids))[:3]} only host={sorted(set(host_ids) - set(laptop_ids))[:3]}"
    for index, (left, right) in enumerate(zip(laptop, host, strict=True)):
        for field in RENDER_FIELDS:
            if left.get(field) != right.get(field):
                return f"row {index} ({left.get('event_id')}) {field}: laptop={json.dumps(left.get(field))[:300]} host={json.dumps(right.get(field))[:300]}"
    return None


async def _ingested_rows(
    *,
    catalog: CatalogClient,
    raw_workers: RawObjectWorkerPool,
    session_id: str,
    owner_id: object,
    raw_spec: Any,
    envelope_id: str,
    records: list[dict[str, Any]],
    revision: str,
    manifest_cache: dict[str, dict[str, dict[str, object]]],
) -> list[dict[str, Any]]:
    """The host render as ingest would store it.

    The stored laptop render passed through the wire parser and the ingest
    classifier, which still correct `interaction_kind` and some Claude text.
    Run the host rebuild through the same two steps so the comparison asks
    only whether the engines agree. Both steps go away when the host render
    becomes authoritative.
    """
    from zerg.services.storage_v2_semantics import enrich_render_interaction_kinds
    from zerg.storage_v2.envelope_wire import parse_render_spec

    spec = parse_render_spec(
        {"generation_id": str(uuid4()), "parser_revision": revision or "unknown", "ordering_revision": "host-shadow", "records": records},
        raw_spec=raw_spec,
        source_envelope_id=envelope_id,
    )
    assert spec is not None
    spec = await enrich_render_interaction_kinds(
        catalog=catalog,
        raw_workers=raw_workers,
        session_id=session_id,
        owner_id=str(owner_id) if owner_id is not None else None,
        raw_spec=raw_spec,
        render_spec=spec,
        manifest_cache=manifest_cache,
        lane="repair",
    )
    return sorted((_render_row(asdict(record)) for record in spec.records), key=_sort_key)


async def shadow_session(
    *,
    catalog: CatalogClient,
    raw_workers: RawObjectWorkerPool,
    render_workers: RenderObjectWorkerPool,
    worker: NormalizerWorker,
    session_id: str,
) -> list[dict[str, Any]]:
    """Compare every unanswered envelope of one session; return result rows.

    Every envelope it takes up gets exactly one row. A fault that is not the
    normalizer's answer (a busy or missing object, an unreadable blob, a
    catalog timeout) is an `error`, retried after the session cooldown; only a
    completed comparison is a `match` or `mismatch`.
    """
    inputs = await catalog.call("storage.session.rebuild_inputs.v2", {"session_id": session_id})
    if inputs.get("found") is not True:
        return []
    checked: dict[str, dict[str, Any]] = inputs.get("checked") or {}

    def unanswered(envelope_id: str) -> bool:
        previous = checked.get(envelope_id)
        if previous is None or previous["outcome"] == "error":
            return True
        return previous["outcome"] in ("match", "mismatch") and worker.revision is not None and previous["revision"] != worker.revision

    results: list[dict[str, Any]] = []

    def result(row: dict[str, Any], outcome: str, detail: str | None = None, elapsed_us: int | None = None) -> None:
        results.append(
            {
                "envelope_id": row["envelope_id"],
                "session_id": session_id,
                "provider": row["provider"],
                "source_epoch": row["source_epoch"],
                "range_start": row["range_start"],
                "outcome": outcome,
                "detail": detail[:2_000] if detail is not None else None,
                "normalizer_revision": worker.revision or "unknown",
                "elapsed_us": elapsed_us,
            }
        )

    if inputs.get("too_large") is True:
        for row in inputs["raw_objects"]:
            if row["envelope_id"] not in checked:
                result(row, "too_large", f"session has {len(inputs['raw_objects'])} raw objects")
        return results

    capture: dict[tuple[str, int], dict[str, Any]] = {}
    provider_facts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact in inputs["facts"]:
        if fact["kind"] == CAPTURE_WITNESS:
            capture[(fact["source_epoch"], fact["source_position"])] = fact
        elif not str(fact["kind"]).startswith("capture."):
            provider_facts[fact["source_epoch"]].append(fact)
    render_by_envelope = {row["source_envelope_id"]: row for row in inputs["render_objects"]}
    media_by_envelope: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in inputs["media"]:
        media_by_envelope[row["envelope_id"]].append(row)
    epochs: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in inputs["raw_objects"]:
        epochs[(row["machine_id"], row["provider"], row["opaque_source_id"], row["source_epoch"])].append(row)
    # One manifest snapshot per session for the ingest classifier's prior-raw context.
    manifest_cache: dict[str, dict[str, dict[str, object]]] = {}

    def classified(row: dict[str, Any]) -> str:
        return "raw_only" if row["envelope_id"] not in render_by_envelope else "no_witness"

    for (_machine, provider, _source, epoch), rows in epochs.items():
        rows.sort(key=lambda row: row["range_start"])
        pending = [row for row in rows if unanswered(row["envelope_id"])]
        if not pending:
            continue
        if not any((epoch, row["range_start"]) in capture for row in rows):
            # Shipped by an engine from before capture witnesses: nothing to rebuild from.
            for row in pending:
                result(row, classified(row))
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
        except _EpochTooLarge as exc:
            for row in pending:
                result(row, "too_large", str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 - retried after the cooldown
            for row in pending:
                result(row, "error", f"reading stored inputs: {type(exc).__name__}: {exc}")
            continue
        started = time.monotonic()
        try:
            response = await worker.rebuild(request)
        except NormalizerUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - recorded per envelope
            for row in pending:
                result(row, "error", f"normalizer: {type(exc).__name__}: {exc}")
            continue
        if "error" in response:
            for row in pending:
                result(row, "error", str(response["error"]))
            continue
        per_envelope_us = int((time.monotonic() - started) * 1_000_000 / max(1, len(rows)))
        answers = response.get("envelopes") or []
        answer_by_start = {int(answer.get("range_start", -1)): answer for answer in answers}
        for row in pending:
            answer = answer_by_start.get(row["range_start"])
            if (epoch, row["range_start"]) not in capture:
                result(row, classified(row))
                continue
            if answer is None:
                result(row, "error", f"normalizer answered {len(answers)} envelopes for {len(rows)}")
                continue
            if answer.get("error"):
                result(row, "error", str(answer["error"]), per_envelope_us)
                continue
            try:
                outcome, detail = await _compare(
                    catalog=catalog,
                    raw_workers=raw_workers,
                    render_workers=render_workers,
                    session_id=session_id,
                    owner_id=inputs.get("owner_id"),
                    row=row,
                    raw_spec=raw_specs[row["envelope_id"]],
                    rebuilt=answer["render"],
                    laptop_object=render_by_envelope.get(row["envelope_id"]),
                    stored_facts=[
                        fact for fact in provider_facts.get(epoch, []) if row["range_start"] <= fact["source_position"] < row["range_end"]
                    ],
                    manifest_cache=manifest_cache,
                    revision=worker.revision or "",
                )
            except Exception as exc:  # noqa: BLE001 - retried after the cooldown
                result(row, "error", f"comparing: {type(exc).__name__}: {exc}", per_envelope_us)
                continue
            result(row, outcome, detail, per_envelope_us)
    return results


class _EpochTooLarge(Exception):
    pass


async def _epoch_request(
    *,
    raw_workers: RawObjectWorkerPool,
    inputs: dict[str, Any],
    session_id: str,
    provider: str,
    epoch: str,
    rows: list[dict[str, Any]],
    capture: dict[tuple[str, int], dict[str, Any]],
    media_by_envelope: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    envelopes = []
    raw_specs: dict[str, Any] = {}
    media_bytes: dict[str, str] = {}
    total = 0
    for row in rows:
        decoded = await raw_workers.read(
            row["object_path"],
            row["object_hash"],
            row["tenant_id"],
            lane="repair",
            queue_timeout_seconds=READ_QUEUE_SECONDS,
            operation_timeout_seconds=30.0,
        )
        raw_specs[row["envelope_id"]] = decoded.spec
        total += sum(len(record.data) for record in decoded.spec.records)
        if total > MAX_EPOCH_BYTES:
            raise _EpochTooLarge(f"epoch holds more than {MAX_EPOCH_BYTES} raw bytes")
        facts = []
        witness = capture.get((epoch, row["range_start"]))
        if witness is not None:
            facts.append({key: witness[key] for key in ("kind", "at", "source_position", "payload")})
        refs = []
        for media in media_by_envelope.get(row["envelope_id"], []):
            refs.append(
                {
                    "sha256": media["media_hash"],
                    "source_position": row["range_start"],
                    "ref_key": media.get("ref_key") or "",
                    "availability": "available",
                }
            )
            if media["media_hash"] not in media_bytes:
                # A blob the host cannot read is an error, never a silent difference.
                blob = await raw_workers.read_media(
                    media["object_path"],
                    media["media_hash"],
                    lane="repair",
                    queue_timeout_seconds=READ_QUEUE_SECONDS,
                    operation_timeout_seconds=30.0,
                )
                total += len(blob.data)
                media_bytes[media["media_hash"]] = base64.b64encode(blob.data).decode("ascii")
        envelopes.append(
            {
                "range_start": row["range_start"],
                "range_end": row["range_end"],
                "records": [
                    {"source_position": record.source_position, "data_b64": base64.b64encode(record.data).decode("ascii")}
                    for record in decoded.spec.records
                ],
                "facts": facts,
                "media": refs,
            }
        )
    request = {
        "id": epoch,
        "provider": provider,
        "session_id": session_id,
        "source_epoch": epoch,
        "epoch_opened_at": inputs["epoch_opened_at"].get(epoch, ""),
        "range_kind": rows[0]["range_kind"],
        "envelopes": envelopes,
        "media": media_bytes,
    }
    return request, raw_specs


async def _compare(
    *,
    catalog: CatalogClient,
    raw_workers: RawObjectWorkerPool,
    render_workers: RenderObjectWorkerPool,
    session_id: str,
    owner_id: object,
    row: dict[str, Any],
    raw_spec: Any,
    rebuilt: dict[str, Any],
    laptop_object: dict[str, Any] | None,
    stored_facts: list[dict[str, Any]],
    manifest_cache: dict[str, dict[str, dict[str, object]]],
    revision: str,
) -> tuple[str, str | None]:
    host_rows: list[dict[str, Any]] = []
    if not rebuilt.get("withheld"):
        try:
            host_rows = await _ingested_rows(
                catalog=catalog,
                raw_workers=raw_workers,
                session_id=session_id,
                owner_id=owner_id,
                raw_spec=raw_spec,
                envelope_id=row["envelope_id"],
                records=rebuilt["records"],
                revision=revision,
                manifest_cache=manifest_cache,
            )
        except ValueError as exc:
            # The wire contract refused the host render: ingest would have rejected it.
            return "mismatch", f"host render fails ingest: {exc}"
    laptop_rows: list[dict[str, Any]] = []
    if laptop_object is not None:
        decoded_render = await render_workers.read(
            laptop_object["object_path"],
            laptop_object["object_hash"],
            lane="background",
            queue_timeout_seconds=READ_QUEUE_SECONDS,
            operation_timeout_seconds=30.0,
        )
        laptop_rows = sorted((_render_row(asdict(record)) for record in decoded_render.spec.records), key=_sort_key)
    if rebuilt.get("withheld") and laptop_rows:
        return "mismatch", "host withheld a render the laptop shipped"
    difference = _first_difference(laptop_rows, host_rows)
    if difference is None:
        stored = sorted(
            (fact["kind"], fact["source_position"], _at(fact["at"]), json.dumps(fact["payload"], sort_keys=True)) for fact in stored_facts
        )
        host_facts = sorted(
            (fact["kind"], fact["source_position"], _at(fact["at"]), json.dumps(fact["payload"], sort_keys=True))
            for fact in rebuilt.get("facts") or []
        )
        if stored != host_facts:
            difference = f"provider facts laptop={len(stored)} host={len(host_facts)}"
    return ("match", None) if difference is None else ("mismatch", difference)


_task: asyncio.Task[None] | None = None
_worker: NormalizerWorker | None = None


def render_shadow_enabled() -> bool:
    from zerg.config import get_settings

    default = "0" if get_settings().testing else "1"
    return os.getenv("LONGHOUSE_RENDER_SHADOW", default).strip().lower() not in {"0", "false", "no", "off"}


async def _sweep(
    catalog: CatalogClient, raw_workers: RawObjectWorkerPool, render_workers: RenderObjectWorkerPool, worker: NormalizerWorker
) -> None:
    while True:
        try:
            candidates = (await catalog.call("render.shadow.candidates.v2", {"limit": 4, "revision": worker.revision})).get(
                "sessions"
            ) or []
            for candidate in candidates:
                results = await shadow_session(
                    catalog=catalog,
                    raw_workers=raw_workers,
                    render_workers=render_workers,
                    worker=worker,
                    session_id=str(candidate["session_id"]),
                )
                for chunk in range(0, len(results), RECORD_CHUNK):
                    await catalog.call("render.shadow.record.v2", {"results": results[chunk : chunk + RECORD_CHUNK]})
                await asyncio.sleep(SWEEP_PACE_SECONDS)
            await asyncio.sleep(SWEEP_PACE_SECONDS if candidates else SWEEP_IDLE_SECONDS)
        except asyncio.CancelledError:
            raise
        except NormalizerUnavailable as exc:
            logger.error("Render shadow stopped: %s", exc)
            return
        except Exception:  # noqa: BLE001
            logger.exception("Render shadow sweep tick failed")
            await asyncio.sleep(SWEEP_IDLE_SECONDS)


def start_render_shadow() -> bool:
    """Start the paced shadow sweep when enabled and the engine is installed."""
    global _task, _worker
    if _task is not None and not _task.done():
        return True
    if not render_shadow_enabled():
        logger.info("Render shadow disabled by LONGHOUSE_RENDER_SHADOW")
        return False
    if normalizer_binary() is None:
        logger.error("Render shadow cannot start: longhouse-engine is not installed in this image")
        return False
    from zerg.services.catalogd_supervisor import get_catalogd_projector_client
    from zerg.services.raw_object_workers import get_raw_object_worker_pool
    from zerg.services.render_object_workers import get_render_object_worker_pool

    catalog = get_catalogd_projector_client()
    if catalog is None:
        return False
    _worker = NormalizerWorker()
    _task = asyncio.create_task(
        _sweep(catalog, get_raw_object_worker_pool(), get_render_object_worker_pool(), _worker), name="render-shadow"
    )
    return True


async def stop_render_shadow() -> None:
    global _task, _worker
    if _task is not None:
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
    if _worker is not None:
        await _worker.close()
    _task = None
    _worker = None


__all__ = [
    "NormalizerUnavailable",
    "NormalizerWorker",
    "render_shadow_enabled",
    "shadow_session",
    "start_render_shadow",
    "stop_render_shadow",
]
