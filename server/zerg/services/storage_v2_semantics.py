"""Recover parser-owned semantic facts for legacy storage-v2 render objects."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from collections.abc import MutableMapping
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import UUID

from zerg.catalogd.client import CatalogClient
from zerg.services.provider_interaction_semantics import classify_provider_interaction
from zerg.services.provider_interaction_semantics import claude_sequence_dependent_control_candidate
from zerg.services.provider_interaction_semantics import seed_provider_interaction_sequence_context
from zerg.services.raw_object_workers import RawObjectWorkerPool
from zerg.storage_v2.raw_objects import RawObjectSpec
from zerg.storage_v2.render_objects import RenderObjectSpec
from zerg.storage_v2.render_objects import aggregate_render_object


class StorageV2SemanticRecoveryError(RuntimeError):
    """The raw companion needed to safely replay an old render object is absent."""

    retryable = True


class StorageV2SemanticRecoveryPermanentError(StorageV2SemanticRecoveryError):
    """The immutable evidence exceeds a safe replay bound and needs repair."""

    retryable = False


_SEMANTIC_REPAIR_MAX_SNAPSHOT_OBJECTS = 100_000
_MAX_RAW_MANIFESTS = 100_000
_MAX_SEQUENCE_REPLAY_RECORDS = 100_000
_MAX_SEQUENCE_REPLAY_SCAN_BYTES = 64 * 1024 * 1024
_MAX_SEQUENCE_REPLAY_VALUES = 8_192
_MAX_SEQUENCE_REPLAY_VALUE_BYTES = 16 * 1024 * 1024
_MAX_SEQUENCE_CAVEAT_RECORDS = 4_096
_MAX_SEQUENCE_CAVEAT_BYTES = 8 * 1024 * 1024


async def recover_render_interaction_kinds(
    *,
    catalog: CatalogClient,
    raw_workers: RawObjectWorkerPool | None,
    session_id: str,
    owner_id: str,
    provider: str,
    records: tuple[object, ...],
    source_envelope_id: str,
    manifest_cache: MutableMapping[str, dict[str, dict[str, object]]],
    sequence_context_cache: MutableMapping[tuple[str, str, str, str, str], dict[str, object]] | None = None,
    reclassify_sequence_controls: bool = False,
) -> dict[int, str]:
    """Return semantic kinds for render records from the immutable raw companion.

    Render format v2 predates ``interaction_kind``. Those objects remain valid
    immutable history, but replaying them without their raw companion would
    silently turn provider-local controls back into user messages. Production
    projectors pass the raw worker pool; tests and already-enriched objects take
    the zero-work path.
    """
    selected = {
        ordinal: record
        for ordinal, record in enumerate(records)
        if reclassify_sequence_controls or getattr(record, "interaction_kind", None) is None
    }
    if not selected:
        return {}
    if raw_workers is None:
        raise StorageV2SemanticRecoveryError("raw worker pool is required to recover legacy render semantics")

    manifests = manifest_cache.get(session_id)
    if manifests is None:
        manifests = await _load_raw_manifests(catalog, session_id=session_id, owner_id=owner_id)
        manifest_cache[session_id] = manifests
    manifest = manifests.get(source_envelope_id)
    if manifest is None:
        # A projector may observe the render object before the raw companion's
        # catalog row is visible. Do not pin that transient absence for the
        # lifetime of the worker; the next retry must reload the manifest.
        manifest_cache.pop(session_id, None)
        raise StorageV2SemanticRecoveryError(f"raw companion {source_envelope_id} is missing for storage session {session_id}")

    try:
        decoded = await raw_workers.read(
            str(manifest["object_path"]),
            str(manifest["object_hash"]),
            str(manifest["tenant_id"]),
        )
    except Exception as exc:  # worker errors are provider-independent recovery failures
        raise StorageV2SemanticRecoveryError(f"raw companion {source_envelope_id} could not be read") from exc

    if str(decoded.envelope_id) != source_envelope_id:
        raise StorageV2SemanticRecoveryError("raw companion envelope identity does not match render object")

    raw_records = decoded.spec.records
    if reclassify_sequence_controls:
        sequence_context = await _seed_sequence_context_from_all_raw(
            raw_workers=raw_workers,
            session_id=session_id,
            provider=provider,
            current_raw_spec=decoded.spec,
            current_envelope_id=source_envelope_id,
            manifests=manifests,
            sequence_context_cache=sequence_context_cache,
        )
    else:
        sequence_context = await _seed_sequence_context_from_prior_raw(
            catalog=catalog,
            raw_workers=raw_workers,
            session_id=session_id,
            owner_id=owner_id,
            provider=provider,
            current_raw_spec=decoded.spec,
            current_envelope_id=source_envelope_id,
            manifests=manifests,
        )
    return _classify_render_records_in_raw_order(
        provider=provider,
        raw_records=raw_records,
        records=records,
        selected=set(selected),
        sequence_context=sequence_context,
        source_surface="storage-v2-replay",
    )


async def enrich_render_interaction_kinds(
    *,
    catalog: CatalogClient,
    raw_workers: RawObjectWorkerPool | None,
    session_id: str,
    owner_id: str | None,
    raw_spec: RawObjectSpec,
    render_spec: RenderObjectSpec,
    manifest_cache: MutableMapping[str, dict[str, dict[str, object]]],
) -> RenderObjectSpec:
    """Resolve current render facts with raw sequence context before sealing.

    A storage-v2 envelope is independently admitted and may split one native
    provider interaction from its preceding evidence.  The raw object is the
    durable source of truth, so current render rows that depend on sequence
    evidence are deferred until the preceding raw objects can be replayed.
    """

    reclassify_claude = render_spec.provider.strip().lower() == "claude"
    candidates: set[int] = set()
    for ordinal, record in enumerate(render_spec.records):
        raw_ordinal = int(record.raw_record_ordinal)
        if raw_ordinal < 0 or raw_ordinal >= len(raw_spec.records):
            raise StorageV2SemanticRecoveryError("render record raw locator is outside its raw companion")
        raw_text = raw_spec.records[raw_ordinal].data.decode("utf-8", errors="replace")
        needs_claude_sequence_replay = reclassify_claude and claude_sequence_dependent_control_candidate(
            content_text=record.content_text,
            raw_json=raw_text,
        )
        if getattr(record, "interaction_kind", None) is None or needs_claude_sequence_replay:
            candidates.add(ordinal)
    if not candidates:
        return render_spec
    if raw_workers is None:
        raise StorageV2SemanticRecoveryError("raw worker pool is required to enrich render semantics")

    manifests = manifest_cache.get(session_id)
    if manifests is None:
        manifests = await _load_raw_manifests(catalog, session_id=session_id, owner_id=owner_id) if owner_id is not None else {}
        manifest_cache[session_id] = manifests
    sequence_context = await _seed_sequence_context_from_prior_raw(
        catalog=catalog,
        raw_workers=raw_workers,
        session_id=session_id,
        owner_id=owner_id,
        provider=render_spec.provider,
        current_raw_spec=raw_spec,
        current_envelope_id=render_spec.source_envelope_id,
        manifests=manifests,
    )
    recovered = _classify_render_records_in_raw_order(
        provider=render_spec.provider,
        raw_records=raw_spec.records,
        records=render_spec.records,
        selected=candidates,
        sequence_context=sequence_context,
        source_surface="storage-v2-ingest",
    )
    updated = list(render_spec.records)
    for ordinal, interaction_kind in recovered.items():
        updated[ordinal] = replace(updated[ordinal], interaction_kind=interaction_kind)
    return replace(render_spec, records=tuple(updated))


def _classify_render_records_in_raw_order(
    *,
    provider: str,
    raw_records: tuple[object, ...],
    records: tuple[object, ...],
    selected: set[int],
    sequence_context: MutableMapping[str, object],
    source_surface: str,
) -> dict[int, str]:
    """Replay raw and render rows together with complete-window evidence.

    Render objects are projections and may omit a provider-local row. Replay
    those raw-only rows at their actual ordinal, while classifying selected
    render rows at that same point. The complete raw window is pre-scanned for
    native caveats, so a command row remains recoverable even if an upstream
    envelope presents its caveat later than the command.
    """
    by_raw_ordinal: dict[int, list[tuple[int, object]]] = {}
    for ordinal, record in enumerate(records):
        raw_ordinal = int(getattr(record, "raw_record_ordinal", 0))
        if raw_ordinal < 0 or raw_ordinal >= len(raw_records):
            raise StorageV2SemanticRecoveryError("render record raw locator is outside its raw companion")
        by_raw_ordinal.setdefault(raw_ordinal, []).append((ordinal, record))

    raw_values: list[Any] = []
    for raw_record in raw_records:
        raw_text = raw_record.data.decode("utf-8", errors="replace")
        try:
            raw_values.append(json.loads(raw_text))
        except (TypeError, json.JSONDecodeError):
            raw_values.append(raw_text)

    seed_provider_interaction_sequence_context(provider, raw_values, sequence_context)
    recovered: dict[int, str] = {}
    for raw_ordinal, raw_value in enumerate(raw_values):
        selected_records = by_raw_ordinal.get(raw_ordinal, ())
        if selected_records:
            for ordinal, record in selected_records:
                classification = classify_provider_interaction(
                    provider,
                    role=getattr(record, "role", None),
                    content_text=getattr(record, "content_text", None),
                    raw_json=raw_value,
                    source_surface=source_surface,
                    sequence_context=sequence_context,
                )
                if ordinal in selected:
                    recovered[ordinal] = str(classification["interaction_kind"])
            continue
        role = _raw_role(raw_value)
        if role is not None:
            classify_provider_interaction(
                provider,
                role=role,
                content_text=None,
                raw_json=raw_value,
                source_surface=f"{source_surface}-raw-only",
                sequence_context=sequence_context,
            )
    return recovered


async def _seed_sequence_context_from_prior_raw(
    *,
    catalog: CatalogClient,
    raw_workers: RawObjectWorkerPool,
    session_id: str,
    owner_id: str | None,
    provider: str,
    current_raw_spec: RawObjectSpec,
    current_envelope_id: str,
    manifests: Mapping[str, dict[str, object]],
) -> dict[str, object]:
    if provider.strip().lower() != "claude" or owner_id is None:
        return {}

    prior = [
        item
        for envelope, item in manifests.items()
        if envelope != current_envelope_id
        and str(item.get("machine_id")) == current_raw_spec.machine_id
        and str(item.get("provider")).strip().lower() == provider.strip().lower()
        and str(item.get("opaque_source_id")) == current_raw_spec.opaque_source_id
        and str(item.get("source_epoch")) == str(current_raw_spec.source_epoch)
        and _manifest_range_end(item) <= current_raw_spec.range_start
    ]
    prior.sort(key=lambda item: (_manifest_range_start(item), str(item.get("envelope_id"))))
    # Sequence evidence is adjacent by construction. Bound replay so one old
    # session cannot make a live envelope read its entire archive.
    sequence_context: dict[str, object] = {}
    for item in prior[-64:]:
        envelope = str(item.get("envelope_id") or "")
        try:
            decoded = await raw_workers.read(
                str(item["object_path"]),
                str(item["object_hash"]),
                str(item["tenant_id"]),
            )
        except Exception as exc:  # provider-independent raw recovery failure
            raise StorageV2SemanticRecoveryError(f"raw companion {envelope} could not be read") from exc
        for raw_record in decoded.spec.records:
            raw_text = raw_record.data.decode("utf-8", errors="replace")
            try:
                raw_value: Any = json.loads(raw_text)
            except (TypeError, json.JSONDecodeError):
                raw_value = None
            role = _raw_role(raw_value)
            if role is None:
                continue
            classify_provider_interaction(
                provider,
                role=role,
                content_text=None,
                raw_json=raw_value,
                source_surface="storage-v2-sequence-seed",
                sequence_context=sequence_context,
            )
    return sequence_context


async def _seed_sequence_context_from_all_raw(
    *,
    raw_workers: RawObjectWorkerPool,
    session_id: str,
    provider: str,
    current_raw_spec: RawObjectSpec,
    current_envelope_id: str,
    manifests: Mapping[str, dict[str, object]],
    sequence_context_cache: MutableMapping[tuple[str, str, str, str, str], dict[str, object]] | None,
) -> dict[str, object]:
    """Seed Claude evidence from every immutable object in the source stream.

    A render object can be sealed before the provider emits the caveat that
    proves an earlier command row was local. Repair therefore needs a complete
    stream view, not just the preceding 64 envelopes. The cache is scoped to a
    projector/repair pass, so a newly claimed revision always gets a fresh
    source-manifest view.
    """

    if provider.strip().lower() != "claude":
        return {}
    machine_id = getattr(current_raw_spec, "machine_id", None)
    opaque_source_id = getattr(current_raw_spec, "opaque_source_id", None)
    source_epoch = getattr(current_raw_spec, "source_epoch", None)
    cache_key = (
        session_id,
        str(machine_id or ""),
        provider.strip().lower(),
        str(opaque_source_id or ""),
        str(source_epoch or ""),
    )
    if sequence_context_cache is not None and cache_key in sequence_context_cache:
        return sequence_context_cache[cache_key]

    compatibility_identity = machine_id is None or opaque_source_id is None or source_epoch is None
    if compatibility_identity:
        # A few legacy projector fixtures only model the raw record payload.
        # Production RawObjectSpec always carries the stream identity; in the
        # compatibility shape, replay the current companion rather than
        # guessing across unrelated provider streams.
        matching = [item for envelope, item in manifests.items() if envelope == current_envelope_id]
    else:
        matching = [
            item
            for item in manifests.values()
            if str(item.get("machine_id")) == str(machine_id)
            and str(item.get("provider")).strip().lower() == provider.strip().lower()
            and str(item.get("opaque_source_id")) == str(opaque_source_id)
            and str(item.get("source_epoch")) == str(source_epoch)
        ]
    if compatibility_identity:
        matching.sort(key=lambda item: str(item.get("envelope_id") or ""))
    else:
        matching.sort(key=lambda item: (_manifest_range_start(item), _manifest_range_end(item), str(item.get("envelope_id"))))
    # Retain only records that can establish Claude's native sequence context.
    # Caveats are pre-seeded globally, then command/output rows are replayed in
    # a fixpoint so a caveat -> command -> stdout UUID chain is independent of
    # object-id ordering. The scan itself is bounded as well as the retained
    # evidence: a bound on the retained list alone still permits an unbounded
    # read/decode of one pathological archive.
    replay_values: list[Any] = []
    replay_hashes: set[str] = set()
    replay_bytes = 0
    scanned_records = 0
    scanned_bytes = 0
    for item in matching:
        envelope_id = str(item.get("envelope_id") or "")
        try:
            decoded = await raw_workers.read(
                str(item["object_path"]),
                str(item["object_hash"]),
                str(item["tenant_id"]),
            )
        except Exception as exc:  # provider-independent raw recovery failure
            raise StorageV2SemanticRecoveryError(f"raw companion {envelope_id} could not be read") from exc
        if str(decoded.envelope_id) != envelope_id:
            raise StorageV2SemanticRecoveryError("raw companion envelope identity does not match manifest")
        for raw_record in decoded.spec.records:
            raw_text = raw_record.data.decode("utf-8", errors="replace")
            scanned_records += 1
            scanned_bytes += len(raw_record.data)
            if scanned_records > _MAX_SEQUENCE_REPLAY_RECORDS or scanned_bytes > _MAX_SEQUENCE_REPLAY_SCAN_BYTES:
                raise StorageV2SemanticRecoveryPermanentError("Claude semantic replay scan exceeds its safe evidence bound")
            try:
                raw_value: Any = json.loads(raw_text)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(raw_value, Mapping):
                continue
            is_caveat = (
                raw_value.get("isMeta") is True
                and raw_value.get("type") in {None, "user"}
                and (not isinstance(raw_value.get("message"), Mapping) or raw_value["message"].get("role") in {None, "user"})
                and "<local-command-caveat>" in raw_text
            )
            if not is_caveat and not claude_sequence_dependent_control_candidate(
                content_text=raw_text,
                raw_json=raw_value,
            ):
                continue
            digest = json.dumps(raw_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            digest_hash = hashlib.sha256(digest.encode("utf-8")).hexdigest()
            if digest_hash in replay_hashes:
                continue
            encoded_bytes = len(raw_text.encode("utf-8"))
            if len(replay_values) >= _MAX_SEQUENCE_REPLAY_VALUES or replay_bytes + encoded_bytes > _MAX_SEQUENCE_REPLAY_VALUE_BYTES:
                raise StorageV2SemanticRecoveryPermanentError("Claude semantic replay evidence window is too large")
            replay_hashes.add(digest_hash)
            replay_values.append(raw_value)
            replay_bytes += encoded_bytes

    sequence_context: dict[str, object] = {}
    seed_provider_interaction_sequence_context(provider, replay_values, sequence_context)
    if sequence_context_cache is not None:
        sequence_context_cache[cache_key] = sequence_context
    return sequence_context


def _raw_role(raw_value: Any) -> str | None:
    if not isinstance(raw_value, Mapping):
        return None
    message = raw_value.get("message")
    if isinstance(message, Mapping) and isinstance(message.get("role"), str):
        return str(message["role"])
    role = raw_value.get("role")
    if isinstance(role, str):
        return role
    if raw_value.get("type") in {"user", "assistant", "tool", "system"}:
        return str(raw_value["type"])
    return None


def _manifest_range_start(item: Mapping[str, object]) -> int:
    try:
        return int(item["range_start"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StorageV2SemanticRecoveryError("raw manifest range_start is invalid") from exc


def _manifest_range_end(item: Mapping[str, object]) -> int:
    try:
        return int(item["range_end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StorageV2SemanticRecoveryError("raw manifest range_end is invalid") from exc


async def _load_raw_manifests(
    catalog: CatalogClient,
    *,
    session_id: str,
    owner_id: str,
) -> dict[str, dict[str, object]]:
    manifests: dict[str, dict[str, object]] = {}
    after_source_key: str | None = None
    previous_source_key: str | None = None
    while True:
        page = await catalog.call(
            "storage.session.raw_manifest.v2",
            {
                "session_id": session_id,
                "owner_id": owner_id,
                "after_source_key": after_source_key,
                "limit": 1_000,
            },
        )
        if page.get("found") is not True:
            raise StorageV2SemanticRecoveryError("storage session raw manifest is unavailable")
        objects = page.get("objects")
        if not isinstance(objects, list):
            raise StorageV2SemanticRecoveryError("catalog returned an invalid raw manifest")
        for item in objects:
            if not isinstance(item, dict):
                raise StorageV2SemanticRecoveryError("catalog returned an invalid raw manifest row")
            envelope_id = item.get("envelope_id")
            if not isinstance(envelope_id, str) or not envelope_id:
                raise StorageV2SemanticRecoveryError("raw manifest row has no envelope id")
            manifests[envelope_id] = item
            if len(manifests) > _MAX_RAW_MANIFESTS:
                raise StorageV2SemanticRecoveryPermanentError("storage session raw manifest exceeds its safe replay bound")
        if page.get("objects_truncated") is not True:
            return manifests
        if not objects:
            raise StorageV2SemanticRecoveryError("catalog returned an empty truncated raw manifest page")
        last = objects[-1]
        after_source_key = json.dumps(
            [
                str(last["machine_id"]),
                str(last["provider"]),
                str(last["opaque_source_id"]),
                str(last["source_epoch"]),
                f"{int(last['range_start']):020d}",
                str(last["envelope_id"]),
            ],
            separators=(",", ":"),
        )
        if after_source_key == previous_source_key:
            raise StorageV2SemanticRecoveryError("raw manifest cursor did not advance")
        previous_source_key = after_source_key


def _raw_manifest_signature(manifests: Mapping[str, Mapping[str, object]]) -> tuple[tuple[str, ...], ...]:
    """Return the immutable source membership/fingerprint for a repair pass."""

    return tuple(
        sorted(
            (
                str(envelope_id),
                str(item.get("object_hash") or ""),
                str(item.get("object_path") or ""),
                str(item.get("tenant_id") or ""),
                str(item.get("machine_id") or ""),
                str(item.get("provider") or ""),
                str(item.get("opaque_source_id") or ""),
                str(item.get("source_epoch") or ""),
                str(item.get("range_start") or ""),
                str(item.get("range_end") or ""),
            )
            for envelope_id, item in manifests.items()
        )
    )


async def repair_storage_session_semantic_projection(
    *,
    catalog: CatalogClient,
    render_workers: Any,
    raw_workers: RawObjectWorkerPool | None,
    session_id: str,
    owner_id: str,
    generation_id: str,
) -> dict[str, Any]:
    """Replay legacy render objects and repair their catalog aggregates.

    The object bytes remain immutable. Only catalog-owned derived counts and
    previews are updated after the Runtime Host verifies each object against
    its raw companion.
    """

    session_read = await catalog.call("storage.session.read.v2", {"session_id": session_id})
    if session_read.get("found") is not True:
        raise StorageV2SemanticRecoveryError("storage session is unavailable for semantic repair")
    try:
        snapshot_revision = int(session_read["commit_seq"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StorageV2SemanticRecoveryError("catalog returned an invalid session revision") from exc

    raw_manifest_cache: dict[str, dict[str, dict[str, object]]] = {}
    sequence_context_cache: dict[tuple[str, str, str, str, str], dict[str, object]] = {}
    raw_manifest_snapshot: tuple[tuple[str, ...], ...] | None = None
    if raw_workers is not None:
        initial_raw_manifests = await _load_raw_manifests(
            catalog,
            session_id=session_id,
            owner_id=owner_id,
        )
        raw_manifest_cache[session_id] = initial_raw_manifests
        raw_manifest_snapshot = _raw_manifest_signature(initial_raw_manifests)
    pages = 0
    updated_object_count = 0
    repair_completions: list[bool] = []
    snapshot_object_count: int | None = None
    processed_object_count = 0

    # Materialize the immutable membership set before any repair write. The
    # catalog's object commit sequence is also the visibility boundary, so a
    # write between page reads would otherwise make a fixed snapshot appear to
    # lose its already-repaired objects. A concurrent new object is deliberately
    # excluded from this pass; the catalog's completion check will keep the
    # session incomplete until a later pass covers it.
    manifest_pages: list[tuple[dict[str, object], ...]] = []
    after_object_id: str | None = None
    while True:
        pages += 1
        if pages > 10_000:
            raise StorageV2SemanticRecoveryError("semantic repair exceeded its bounded object page limit")
        manifest = await catalog.call(
            "storage.session.render_objects.list.v2",
            {
                "session_id": session_id,
                "generation_id": generation_id,
                "snapshot_revision": snapshot_revision,
                "after_object_id": after_object_id,
                "limit": 1_000,
            },
        )
        if manifest.get("found") is not True or str(manifest.get("generation_id")) != generation_id:
            raise StorageV2SemanticRecoveryError("current render generation is unavailable for semantic repair")
        objects = manifest.get("objects")
        if not isinstance(objects, list):
            raise StorageV2SemanticRecoveryError("catalog returned an invalid render object page")
        page_object_count = int(manifest.get("snapshot_object_count") or 0)
        if snapshot_object_count is None:
            snapshot_object_count = page_object_count
            if snapshot_object_count > _SEMANTIC_REPAIR_MAX_SNAPSHOT_OBJECTS:
                raise StorageV2SemanticRecoveryPermanentError("semantic repair snapshot is too large")
        elif page_object_count != snapshot_object_count:
            raise StorageV2SemanticRecoveryError("render object snapshot changed during semantic repair")
        normalized_objects: list[dict[str, object]] = []
        for item in objects:
            if not isinstance(item, dict):
                raise StorageV2SemanticRecoveryError("catalog returned an invalid render manifest row")
            object_id = str(item.get("object_id") or "")
            if not object_id:
                raise StorageV2SemanticRecoveryError("render manifest row has no object id")
            normalized_objects.append(dict(item))
        manifest_pages.append(tuple(normalized_objects))
        if manifest.get("has_more") is not True:
            break
        if not objects:
            raise StorageV2SemanticRecoveryError("catalog returned an empty truncated render object page")
        last_object_id = objects[-1].get("object_id")
        if not isinstance(last_object_id, str) or last_object_id == after_object_id:
            raise StorageV2SemanticRecoveryError("semantic repair render cursor did not advance")
        after_object_id = last_object_id
        if len(manifest_pages) >= 10_000:
            raise StorageV2SemanticRecoveryError("semantic repair exceeded its bounded object page limit")

    for page_index, objects in enumerate(manifest_pages):
        corrections: list[dict[str, object]] = []
        for item in objects:
            object_id = str(item.get("object_id") or "")
            try:
                decoded = await render_workers.read(
                    str(item["object_path"]),
                    str(item["object_hash"]),
                    lane="background",
                )
            except Exception as exc:  # worker errors become a bounded retryable recovery failure
                raise StorageV2SemanticRecoveryError(f"render object {object_id} could not be read") from exc
            spec = decoded.spec
            if (
                spec.session_id != UUID(session_id)
                or spec.render_generation != UUID(generation_id)
                or spec.source_envelope_id != item.get("source_envelope_id")
                or decoded.object_hash != item.get("object_hash")
            ):
                raise StorageV2SemanticRecoveryError(f"render object {object_id} does not match its catalog manifest")
            recovered = await recover_render_interaction_kinds(
                catalog=catalog,
                raw_workers=raw_workers,
                session_id=session_id,
                owner_id=owner_id,
                provider=spec.provider,
                records=spec.records,
                source_envelope_id=spec.source_envelope_id,
                manifest_cache=raw_manifest_cache,
                sequence_context_cache=sequence_context_cache,
                reclassify_sequence_controls=spec.provider.strip().lower() == "claude",
            )
            updated_records = tuple(
                replace(
                    record, interaction_kind=_repaired_interaction_kind(getattr(record, "interaction_kind", None), recovered.get(index))
                )
                for index, record in enumerate(spec.records)
            )
            aggregate = aggregate_render_object(replace(spec, records=updated_records))
            corrections.append(
                {
                    "object_id": object_id,
                    "event_count": len(spec.records),
                    "user_messages": int(aggregate["user_messages"]),
                    "assistant_messages": int(aggregate["assistant_messages"]),
                    "tool_calls": int(aggregate["tool_calls"]),
                    "first_user_message_preview": aggregate["first_user_message_preview"],
                    "last_visible_text_preview": aggregate["last_visible_text_preview"],
                }
            )
        processed_object_count += len(objects)
        if corrections or (page_index == 0 and not objects):
            repaired = await catalog.call(
                "storage.session.semantic_projection.repair.v2",
                {
                    "session_id": session_id,
                    "owner_id": owner_id,
                    "generation_id": generation_id,
                    "objects": corrections,
                    "observed_at": datetime.now(UTC).isoformat(),
                },
            )
            repair_completions.append(repaired.get("complete") is True)
            updated_object_count += int(repaired.get("updated_object_count") or 0)
    if snapshot_object_count is None or processed_object_count != snapshot_object_count:
        raise StorageV2SemanticRecoveryError("semantic repair did not cover the immutable render object snapshot")
    if raw_manifest_snapshot is not None:
        final_raw_manifests = await _load_raw_manifests(
            catalog,
            session_id=session_id,
            owner_id=owner_id,
        )
        if _raw_manifest_signature(final_raw_manifests) != raw_manifest_snapshot:
            raise StorageV2SemanticRecoveryError("raw manifest changed during semantic repair")
    # The catalog response for the final object page is the completion
    # decision for the immutable snapshot. Earlier pages necessarily report
    # incomplete while later objects still need repair; folding those interim
    # responses with all() would leave every multi-page repair permanently
    # pending even after the final page completed.
    complete = bool(repair_completions) and repair_completions[-1]
    return {
        "complete": complete,
        "pages": pages,
        "updated_object_count": updated_object_count,
    }


def _repaired_interaction_kind(existing: str | None, recovered: str | None) -> str | None:
    """Prefer a recovered raw fact; preserve stored data only when recovery is pending."""

    return existing if recovered is None else recovered


__all__ = [
    "StorageV2SemanticRecoveryError",
    "StorageV2SemanticRecoveryPermanentError",
    "enrich_render_interaction_kinds",
    "repair_storage_session_semantic_projection",
    "recover_render_interaction_kinds",
]
