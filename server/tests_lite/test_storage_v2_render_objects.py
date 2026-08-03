from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest

from zerg.routers.agents_storage_v2 import _parse_render_spec
from zerg.services.render_object_workers import RenderObjectWorkerPool
from zerg.services.storage_v2_semantics import StorageV2SemanticRecoveryError
from zerg.services.storage_v2_semantics import _repaired_interaction_kind
from zerg.services.storage_v2_semantics import enrich_render_interaction_kinds
from zerg.services.storage_v2_semantics import recover_render_interaction_kinds
from zerg.services.storage_v2_semantics import repair_storage_session_semantic_projection
from zerg.storage_v2.raw_objects import RawObjectSpec
from zerg.storage_v2.raw_objects import RawRecord
from zerg.storage_v2.raw_objects import read_raw_object
from zerg.storage_v2.raw_objects import seal_raw_object
from zerg.storage_v2.render_objects import RenderObjectSpec
from zerg.storage_v2.render_objects import RenderObjectValidationError
from zerg.storage_v2.render_objects import RenderRecord
from zerg.storage_v2.render_objects import decode_render_object
from zerg.storage_v2.render_objects import encode_render_object
from zerg.storage_v2.render_objects import read_render_object
from zerg.storage_v2.render_objects import seal_render_object


def _spec() -> RenderObjectSpec:
    return RenderObjectSpec(
        session_id=UUID("018f0c3a-7b2d-7f10-8a11-123456789abc"),
        render_generation=UUID("018f0c3a-7b2d-7f10-8a11-223456789abc"),
        parser_revision="engine-parser-v2",
        ordering_revision="semantic-order-v2",
        machine_id="cinder",
        provider="codex",
        opaque_source_id="history.jsonl",
        source_epoch=UUID("018f0c3a-7b2d-7f10-8a11-323456789abc"),
        source_envelope_id="a" * 64,
        records=(
            RenderRecord(
                event_id="user-1",
                order_time_us=1_700_000_000_000_000,
                source_position=0,
                event_subordinal=0,
                role="user",
                content_text="Build it",
            ),
            RenderRecord(
                event_id="tool-1",
                order_time_us=1_700_000_001_000_000,
                source_position=10,
                event_subordinal=0,
                role="assistant",
                tool_name="apply_patch",
                tool_input_json={"patch": "*** Begin Patch"},
                tool_call_id="call-1",
                raw_record_ordinal=1,
            ),
        ),
    )


def test_render_object_is_deterministic_verified_and_summarized(tmp_path):
    spec = _spec()
    sealed = seal_render_object(tmp_path, spec)
    replay = seal_render_object(tmp_path, spec)
    assert replay.object_hash == sealed.object_hash
    assert replay.reused is True
    assert sealed.event_count == 2
    assert sealed.user_messages == 1
    assert sealed.tool_calls == 1
    assert sealed.first_user_message_preview == "Build it"
    decoded = read_render_object(tmp_path, sealed.object_path, expected_object_hash=sealed.object_hash)
    assert decoded.spec == spec


def test_render_aggregate_keeps_claude_control_raw_but_excludes_it_from_semantics(tmp_path):
    spec = _spec()
    claude = replace(
        spec,
        provider="claude",
        records=(
            RenderRecord(
                event_id="command",
                order_time_us=1_700_000_000_000_000,
                source_position=0,
                event_subordinal=0,
                role="user",
                content_text="<command-name>/effort</command-name><command-args>high</command-args>",
                interaction_kind="local_control",
            ),
            RenderRecord(
                event_id="prompt",
                order_time_us=1_700_000_000_000_001,
                source_position=1,
                event_subordinal=0,
                role="user",
                content_text="Build the feature",
            ),
        ),
    )

    sealed = seal_render_object(tmp_path, claude)

    assert sealed.event_count == 2
    assert sealed.user_messages == 1
    assert sealed.first_user_message_preview == "Build the feature"
    assert sealed.last_visible_text_preview == "Build the feature"


def test_storage_wire_derives_semantics_from_raw_when_engine_omits_field():
    raw = RawObjectSpec(
        tenant_id="tenant-a",
        machine_id="cinder",
        session_id=UUID("018f0c3a-7b2d-7f10-8a11-123456789abc"),
        provider="claude",
        opaque_source_id="history.jsonl",
        source_epoch=UUID("018f0c3a-7b2d-7f10-8a11-323456789abc"),
        range_kind="record_ordinal",
        range_start=0,
        range_end=2,
        records=(
            RawRecord(
                source_position=0,
                data=b'{"type":"user","isMeta":true,"message":{"role":"user","content":"<command-name>/effort</command-name>"}}',
            ),
            RawRecord(
                source_position=1,
                data=b'{"type":"user","message":{"role":"user","content":"Build the feature"}}',
            ),
        ),
    )
    parsed = _parse_render_spec(
        {
            "generation_id": "018f0c3a-7b2d-7f10-8a11-423456789abc",
            "parser_revision": "engine-parser-v2",
            "ordering_revision": "semantic-order-v2",
            "records": [
                {
                    "event_id": "command",
                    "order_time_us": 1,
                    "source_position": 0,
                    "event_subordinal": 0,
                    "role": "user",
                    "content_text": "<command-name>/effort</command-name>",
                    "tool_name": None,
                    "tool_input_json": None,
                    "tool_output_text": None,
                    "tool_call_id": None,
                    "thread_id": None,
                    "branch_kind": None,
                    "raw_record_ordinal": 0,
                },
                {
                    "event_id": "prompt",
                    "order_time_us": 2,
                    "source_position": 1,
                    "event_subordinal": 0,
                    "role": "user",
                    "content_text": "Build the feature",
                    "tool_name": None,
                    "tool_input_json": None,
                    "tool_output_text": None,
                    "tool_call_id": None,
                    "thread_id": None,
                    "branch_kind": None,
                    "raw_record_ordinal": 1,
                },
            ],
        },
        raw_spec=raw,
        source_envelope_id="a" * 64,
    )

    assert parsed is not None
    assert [record.interaction_kind for record in parsed.records] == ["local_control", "durable_user_message"]


def test_storage_wire_uses_complete_raw_window_for_command_before_caveat():
    raw = RawObjectSpec(
        tenant_id="tenant-a",
        machine_id="cinder",
        session_id=UUID("018f0c3a-7b2d-7f10-8a11-123456789abc"),
        provider="claude",
        opaque_source_id="history.jsonl",
        source_epoch=UUID("018f0c3a-7b2d-7f10-8a11-323456789abc"),
        range_kind="record_ordinal",
        range_start=0,
        range_end=2,
        records=(
            RawRecord(
                source_position=0,
                data=b'{"type":"user","promptId":"prompt-effort-1","message":{"role":"user","content":"<command-name>/effort</command-name>"}}',
            ),
            RawRecord(
                source_position=1,
                data=b'{"type":"user","isMeta":true,"promptId":"prompt-effort-1","message":{"role":"user","content":"<local-command-caveat>later</local-command-caveat>"}}',
            ),
        ),
    )
    parsed = _parse_render_spec(
        {
            "generation_id": "018f0c3a-7b2d-7f10-8a11-423456789abc",
            "parser_revision": "engine-parser-v2",
            "ordering_revision": "semantic-order-v2",
            "records": [
                {
                    "event_id": "command",
                    "order_time_us": 1,
                    "source_position": 0,
                    "event_subordinal": 0,
                    "role": "user",
                    "content_text": "<command-name>/effort</command-name>",
                    "tool_name": None,
                    "tool_input_json": None,
                    "tool_output_text": None,
                    "tool_call_id": None,
                    "thread_id": None,
                    "branch_kind": None,
                    "raw_record_ordinal": 0,
                },
            ],
        },
        raw_spec=raw,
        source_envelope_id="a" * 64,
    )

    assert parsed is not None
    assert parsed.records[0].interaction_kind == "local_control"


def test_storage_wire_preserves_non_claude_parser_semantic_kind():
    raw = RawObjectSpec(
        tenant_id="tenant-a",
        machine_id="cinder",
        session_id=UUID("018f0c3a-7b2d-7f10-8a11-123456789abc"),
        provider="codex",
        opaque_source_id="history.jsonl",
        source_epoch=UUID("018f0c3a-7b2d-7f10-8a11-323456789abc"),
        range_kind="record_ordinal",
        range_start=0,
        range_end=1,
        records=(RawRecord(source_position=0, data=b'{"type":"user","message":{"role":"user","content":"Build it"}}'),),
    )
    parsed = _parse_render_spec(
        {
            "generation_id": "018f0c3a-7b2d-7f10-8a11-423456789abc",
            "parser_revision": "engine-parser-v2",
            "ordering_revision": "semantic-order-v2",
            "records": [
                {
                    "event_id": "prompt",
                    "order_time_us": 1,
                    "source_position": 0,
                    "event_subordinal": 0,
                    "role": "user",
                    "content_text": "Build it",
                    "tool_name": None,
                    "tool_input_json": None,
                    "tool_output_text": None,
                    "tool_call_id": None,
                    "thread_id": None,
                    "branch_kind": None,
                    "raw_record_ordinal": 0,
                    "interaction_kind": "local_control",
                },
            ],
        },
        raw_spec=raw,
        source_envelope_id="a" * 64,
    )

    assert parsed is not None
    assert parsed.records[0].interaction_kind == "local_control"


@pytest.mark.asyncio
async def test_multi_page_semantic_repair_uses_final_catalog_completion(monkeypatch) -> None:
    first = _spec()
    second = replace(first, source_envelope_id="b" * 64)
    objects = [
        {
            "object_id": "object-1",
            "object_path": "object-1",
            "object_hash": "hash-1",
            "source_envelope_id": first.source_envelope_id,
        },
        {
            "object_id": "object-2",
            "object_path": "object-2",
            "object_hash": "hash-2",
            "source_envelope_id": second.source_envelope_id,
        },
    ]

    class Catalog:
        def __init__(self):
            self.repairs: list[dict[str, object]] = []

        async def call(self, method, params):
            if method == "storage.session.read.v2":
                return {"found": True, "commit_seq": 9}
            if method == "storage.session.render_objects.list.v2":
                page = objects[0:1] if params["after_object_id"] is None else objects[1:]
                return {
                    "found": True,
                    "generation_id": str(first.render_generation),
                    "snapshot_object_count": 2,
                    "objects": page,
                    "has_more": params["after_object_id"] is None,
                }
            if method == "storage.session.semantic_projection.repair.v2":
                self.repairs.append(params)
                return {
                    "complete": len(self.repairs) == 2,
                    "updated_object_count": len(params["objects"]),
                }
            raise AssertionError(method)

    class RenderWorkers:
        async def read(self, path, expected_hash, *, lane):
            assert lane == "background"
            spec = first if path == "object-1" else second
            return SimpleNamespace(spec=spec, object_hash=expected_hash)

    async def no_recovery(**_kwargs):
        return {}

    catalog = Catalog()
    monkeypatch.setattr(
        "zerg.services.storage_v2_semantics._load_raw_manifests",
        lambda *_args, **_kwargs: _empty_async_mapping(),
    )
    monkeypatch.setattr(
        "zerg.services.storage_v2_semantics.recover_render_interaction_kinds",
        no_recovery,
    )

    result = await repair_storage_session_semantic_projection(
        catalog=catalog,
        render_workers=RenderWorkers(),
        raw_workers=object(),
        session_id=str(first.session_id),
        owner_id="42",
        generation_id=str(first.render_generation),
    )

    assert result["pages"] == 2
    assert result["complete"] is True
    assert len(catalog.repairs) == 2


async def _empty_async_mapping():
    return {}


def test_render_object_rejects_unstable_semantic_order(tmp_path):
    spec = _spec()
    reversed_spec = RenderObjectSpec(
        session_id=spec.session_id,
        render_generation=spec.render_generation,
        parser_revision=spec.parser_revision,
        ordering_revision=spec.ordering_revision,
        machine_id=spec.machine_id,
        provider=spec.provider,
        opaque_source_id=spec.opaque_source_id,
        source_epoch=spec.source_epoch,
        source_envelope_id=spec.source_envelope_id,
        records=tuple(reversed(spec.records)),
    )
    with pytest.raises(RenderObjectValidationError, match="strictly ordered"):
        seal_render_object(tmp_path, reversed_spec)


def test_render_object_rejects_oversized_interaction_kind(tmp_path):
    spec = _spec()
    malformed = replace(
        spec,
        records=(replace(spec.records[0], interaction_kind="x" * 65), *spec.records[1:]),
    )

    with pytest.raises(RenderObjectValidationError, match="interaction_kind"):
        seal_render_object(tmp_path, malformed)


def test_render_object_reader_accepts_v2_without_semantic_fields():
    payload = json.loads(encode_render_object(_spec()))
    payload["format_version"] = 2
    for record in payload["records"]:
        record.pop("interaction_kind", None)

    decoded = decode_render_object(json.dumps(payload).encode("utf-8"))

    assert decoded.records[0].interaction_kind is None
    assert decoded.records[0].content_text == "Build it"


def test_storage_repair_uses_raw_durable_fact_over_stale_local_fact():
    assert _repaired_interaction_kind("local_control", "durable_user_message") == "durable_user_message"
    assert _repaired_interaction_kind("local_control", None) == "local_control"


@pytest.mark.asyncio
async def test_storage_semantics_seed_from_prior_raw_envelope(tmp_path):
    session_id = UUID("018f0c3a-7b2d-7f10-8a11-123456789abc")
    source_epoch = UUID("018f0c3a-7b2d-7f10-8a11-323456789abc")
    caveat = (
        b'{"type":"user","isMeta":true,"promptId":"prompt-effort-1",'
        b'"message":{"role":"user","content":"<local-command-caveat>native</local-command-caveat>"}}'
    )
    command = (
        b'{"type":"user","promptId":"prompt-effort-1",'
        b'"message":{"role":"user","content":"<command-name>/effort</command-name>"}}'
    )
    prior_raw = RawObjectSpec(
        tenant_id="tenant-a",
        machine_id="cinder",
        session_id=session_id,
        provider="claude",
        opaque_source_id="history.jsonl",
        source_epoch=source_epoch,
        range_kind="record_ordinal",
        range_start=0,
        range_end=1,
        records=(RawRecord(source_position=0, data=caveat),),
    )
    current_raw = RawObjectSpec(
        tenant_id="tenant-a",
        machine_id="cinder",
        session_id=session_id,
        provider="claude",
        opaque_source_id="history.jsonl",
        source_epoch=source_epoch,
        range_kind="record_ordinal",
        range_start=1,
        range_end=2,
        records=(RawRecord(source_position=1, data=command),),
    )
    prior_sealed = seal_raw_object(tmp_path, prior_raw)
    current_sealed = seal_raw_object(tmp_path, current_raw)
    render = RenderObjectSpec(
        session_id=session_id,
        render_generation=UUID("018f0c3a-7b2d-7f10-8a11-223456789abc"),
        parser_revision="engine-parser-v2",
        ordering_revision="semantic-order-v2",
        machine_id="cinder",
        provider="claude",
        opaque_source_id="history.jsonl",
        source_epoch=source_epoch,
        source_envelope_id=current_sealed.envelope_id,
        records=(
            RenderRecord(
                event_id="command",
                order_time_us=2,
                source_position=1,
                event_subordinal=0,
                role="user",
                content_text="<command-name>/effort</command-name>",
                raw_record_ordinal=0,
            ),
        ),
    )

    class RawReader:
        async def read(self, object_path, object_hash, tenant_id):
            return read_raw_object(tmp_path, object_path, expected_object_hash=object_hash)

    class Catalog:
        async def call(self, method, params):
            assert method == "storage.session.raw_manifest.v2"
            assert params["session_id"] == str(session_id)
            return {
                "found": True,
                "objects": [
                    {
                        "envelope_id": prior_sealed.envelope_id,
                        "machine_id": "cinder",
                        "provider": "claude",
                        "opaque_source_id": "history.jsonl",
                        "source_epoch": str(source_epoch),
                        "range_start": 0,
                        "range_end": 1,
                        "object_path": prior_sealed.object_path,
                        "object_hash": prior_sealed.object_hash,
                        "tenant_id": "tenant-a",
                    }
                ],
                "objects_truncated": False,
            }

    enriched = await enrich_render_interaction_kinds(
        catalog=Catalog(),
        raw_workers=RawReader(),
        session_id=str(session_id),
        owner_id="42",
        raw_spec=current_raw,
        render_spec=render,
        manifest_cache={},
    )

    assert enriched.records[0].interaction_kind == "local_control"


@pytest.mark.asyncio
async def test_storage_semantic_recovery_reloads_manifest_after_transient_absence(tmp_path):
    session_id = UUID("018f0c3a-7b2d-7f10-8a11-123456789abc")
    source_epoch = UUID("018f0c3a-7b2d-7f10-8a11-323456789abc")
    caveat = b'{"type":"user","isMeta":true,"uuid":"caveat-1","message":{"role":"user","content":"<local-command-caveat>native</local-command-caveat>"}}'
    command = b'{"type":"user","uuid":"command-1","parentUuid":"caveat-1","message":{"role":"user","content":"<command-name>/effort</command-name>"}}'
    raw = RawObjectSpec(
        tenant_id="tenant-a",
        machine_id="cinder",
        session_id=session_id,
        provider="claude",
        opaque_source_id="history.jsonl",
        source_epoch=source_epoch,
        range_kind="record_ordinal",
        range_start=0,
        range_end=2,
        records=(RawRecord(source_position=0, data=caveat), RawRecord(source_position=1, data=command)),
    )
    sealed = seal_raw_object(tmp_path, raw)
    render = RenderObjectSpec(
        session_id=session_id,
        render_generation=UUID("018f0c3a-7b2d-7f10-8a11-223456789abc"),
        parser_revision="engine-parser-v2",
        ordering_revision="semantic-order-v2",
        machine_id="cinder",
        provider="claude",
        opaque_source_id="history.jsonl",
        source_epoch=source_epoch,
        source_envelope_id=sealed.envelope_id,
        records=(
            RenderRecord(
                event_id="command",
                order_time_us=2,
                source_position=1,
                event_subordinal=0,
                role="user",
                content_text="<command-name>/effort</command-name>",
                raw_record_ordinal=1,
            ),
        ),
    )
    cache: dict[str, dict[str, dict[str, object]]] = {}

    class Catalog:
        calls = 0

        async def call(self, method, params):
            assert method == "storage.session.raw_manifest.v2"
            self.calls += 1
            if self.calls == 1:
                return {"found": True, "objects": [], "objects_truncated": False}
            return {
                "found": True,
                "objects": [
                    {
                        "envelope_id": sealed.envelope_id,
                        "machine_id": "cinder",
                        "provider": "claude",
                        "opaque_source_id": "history.jsonl",
                        "source_epoch": str(source_epoch),
                        "range_start": 0,
                        "range_end": 2,
                        "object_path": sealed.object_path,
                        "object_hash": sealed.object_hash,
                        "tenant_id": "tenant-a",
                    }
                ],
                "objects_truncated": False,
            }

    class RawReader:
        async def read(self, object_path, object_hash, tenant_id):
            return read_raw_object(tmp_path, object_path, expected_object_hash=object_hash)

    catalog = Catalog()
    with pytest.raises(StorageV2SemanticRecoveryError, match="raw companion"):
        await recover_render_interaction_kinds(
            catalog=catalog,
            raw_workers=RawReader(),
            session_id=str(session_id),
            owner_id="42",
            provider="claude",
            records=render.records,
            source_envelope_id=sealed.envelope_id,
            manifest_cache=cache,
        )

    recovered = await recover_render_interaction_kinds(
        catalog=catalog,
        raw_workers=RawReader(),
        session_id=str(session_id),
        owner_id="42",
        provider="claude",
        records=render.records,
        source_envelope_id=sealed.envelope_id,
        manifest_cache=cache,
    )

    assert catalog.calls == 2
    assert recovered == {0: "local_control"}


@pytest.mark.asyncio
@pytest.mark.parametrize("command_first", (False, True))
async def test_storage_semantics_replays_current_raw_in_order(tmp_path, command_first):
    session_id = UUID("018f0c3a-7b2d-7f10-8a11-123456789abc")
    source_epoch = UUID("018f0c3a-7b2d-7f10-8a11-323456789abc")
    caveat = (
        b'{"type":"user","isMeta":true,"promptId":"prompt-effort-1",'
        b'"message":{"role":"user","content":"<local-command-caveat>native</local-command-caveat>"}}'
    )
    command = (
        b'{"type":"user","promptId":"prompt-effort-1",'
        b'"message":{"role":"user","content":"<command-name>/effort</command-name>"}}'
    )
    current_raw = RawObjectSpec(
        tenant_id="tenant-a",
        machine_id="cinder",
        session_id=session_id,
        provider="claude",
        opaque_source_id="history.jsonl",
        source_epoch=source_epoch,
        range_kind="record_ordinal",
        range_start=0,
        range_end=2,
        records=(
            (RawRecord(source_position=0, data=command), RawRecord(source_position=1, data=caveat))
            if command_first
            else (RawRecord(source_position=0, data=caveat), RawRecord(source_position=1, data=command))
        ),
    )
    sealed = seal_raw_object(tmp_path, current_raw)
    render = RenderObjectSpec(
        session_id=session_id,
        render_generation=UUID("018f0c3a-7b2d-7f10-8a11-223456789abc"),
        parser_revision="engine-parser-v2",
        ordering_revision="semantic-order-v2",
        machine_id="cinder",
        provider="claude",
        opaque_source_id="history.jsonl",
        source_epoch=source_epoch,
        source_envelope_id=sealed.envelope_id,
        records=(
            RenderRecord(
                event_id="command",
                order_time_us=2,
                source_position=0 if command_first else 1,
                event_subordinal=0,
                role="user",
                content_text="<command-name>/effort</command-name>",
                raw_record_ordinal=0 if command_first else 1,
            ),
        ),
    )

    class RawReader:
        async def read(self, object_path, object_hash, tenant_id):
            return read_raw_object(tmp_path, object_path, expected_object_hash=object_hash)

    class Catalog:
        async def call(self, method, params):
            assert method == "storage.session.raw_manifest.v2"
            return {"found": True, "objects": [], "objects_truncated": False}

    enriched = await enrich_render_interaction_kinds(
        catalog=Catalog(),
        raw_workers=RawReader(),
        session_id=str(session_id),
        owner_id="42",
        raw_spec=current_raw,
        render_spec=render,
        manifest_cache={},
    )

    assert enriched.records[0].interaction_kind == "local_control"


@pytest.mark.asyncio
async def test_storage_semantic_recovery_skips_full_stream_scan_without_sequence_candidates(tmp_path):
    session_id = UUID("018f0c3a-7b2d-7f10-8a11-123456789abc")
    source_epoch = UUID("018f0c3a-7b2d-7f10-8a11-323456789abc")
    current = RawObjectSpec(
        tenant_id="tenant-a",
        machine_id="cinder",
        session_id=session_id,
        provider="claude",
        opaque_source_id="history.jsonl",
        source_epoch=source_epoch,
        range_kind="record_ordinal",
        range_start=0,
        range_end=1,
        records=(RawRecord(source_position=0, data=b'{"type":"user","message":{"role":"user","content":"hello"}}'),),
    )
    unrelated = replace(
        current,
        range_start=1,
        range_end=2,
        records=(
            RawRecord(
                source_position=1,
                data=b'{"type":"user","isMeta":true,"message":{"role":"user","content":"<local-command-caveat>x</local-command-caveat>"}}',
            ),
        ),
    )
    current_sealed = seal_raw_object(tmp_path, current)
    unrelated_sealed = seal_raw_object(tmp_path, unrelated)
    render = RenderObjectSpec(
        session_id=session_id,
        render_generation=UUID("018f0c3a-7b2d-7f10-8a11-223456789abc"),
        parser_revision="engine-parser-v2",
        ordering_revision="semantic-order-v2",
        machine_id="cinder",
        provider="claude",
        opaque_source_id="history.jsonl",
        source_epoch=source_epoch,
        source_envelope_id=current_sealed.envelope_id,
        records=(
            RenderRecord(
                event_id="message",
                order_time_us=1,
                source_position=0,
                event_subordinal=0,
                role="user",
                content_text="hello",
                interaction_kind="durable_user_message",
                raw_record_ordinal=0,
            ),
        ),
    )

    class Catalog:
        async def call(self, method, params):
            assert method == "storage.session.raw_manifest.v2"
            return {
                "found": True,
                "objects": [
                    {
                        "envelope_id": sealed.envelope_id,
                        "machine_id": "cinder",
                        "provider": "claude",
                        "opaque_source_id": "history.jsonl",
                        "source_epoch": str(source_epoch),
                        "range_start": start,
                        "range_end": end,
                        "object_path": sealed.object_path,
                        "object_hash": sealed.object_hash,
                        "tenant_id": "tenant-a",
                    }
                    for sealed, start, end in ((current_sealed, 0, 1), (unrelated_sealed, 1, 2))
                ],
                "objects_truncated": False,
            }

    class RawReader:
        reads = 0

        async def read(self, object_path, object_hash, tenant_id):
            self.reads += 1
            assert object_path == current_sealed.object_path
            return read_raw_object(tmp_path, object_path, expected_object_hash=object_hash)

    reader = RawReader()
    recovered = await recover_render_interaction_kinds(
        catalog=Catalog(),
        raw_workers=reader,
        session_id=str(session_id),
        owner_id="42",
        provider="claude",
        records=render.records,
        source_envelope_id=current_sealed.envelope_id,
        manifest_cache={},
        sequence_context_cache={},
        reclassify_sequence_controls=True,
    )

    assert reader.reads == 1
    assert recovered == {0: "durable_user_message"}


@pytest.mark.asyncio
async def test_storage_semantic_recovery_reclassifies_legacy_command_when_caveat_is_in_later_envelope(tmp_path):
    """A later immutable caveat must repair an earlier durable-looking command."""

    session_id = UUID("018f0c3a-7b2d-7f10-8a11-123456789abc")
    source_epoch = UUID("018f0c3a-7b2d-7f10-8a11-323456789abc")
    prompt_id = "prompt-effort-1"
    command = RawObjectSpec(
        tenant_id="tenant-a",
        machine_id="cinder",
        session_id=session_id,
        provider="claude",
        opaque_source_id="history.jsonl",
        source_epoch=source_epoch,
        range_kind="record_ordinal",
        range_start=0,
        range_end=1,
        records=(
            RawRecord(
                source_position=0,
                data=(
                    f'{{"type":"user","promptId":"{prompt_id}",'
                    '"message":{"role":"user","content":"<command-name>/effort</command-name>"}}'
                ).encode(),
            ),
        ),
    )
    later_caveat = RawObjectSpec(
        tenant_id="tenant-a",
        machine_id="cinder",
        session_id=session_id,
        provider="claude",
        opaque_source_id="history.jsonl",
        source_epoch=source_epoch,
        range_kind="record_ordinal",
        range_start=1,
        range_end=2,
        records=(
            RawRecord(
                source_position=1,
                data=(
                    f'{{"type":"user","isMeta":true,"promptId":"{prompt_id}",'
                    '"message":{"role":"user","content":"<local-command-caveat>native</local-command-caveat>"}}'
                ).encode(),
            ),
        ),
    )
    command_sealed = seal_raw_object(tmp_path, command)
    caveat_sealed = seal_raw_object(tmp_path, later_caveat)
    render = RenderObjectSpec(
        session_id=session_id,
        render_generation=UUID("018f0c3a-7b2d-7f10-8a11-223456789abc"),
        parser_revision="engine-parser-v2",
        ordering_revision="semantic-order-v2",
        machine_id="cinder",
        provider="claude",
        opaque_source_id="history.jsonl",
        source_epoch=source_epoch,
        source_envelope_id=command_sealed.envelope_id,
        records=(
            RenderRecord(
                event_id="command",
                order_time_us=1,
                source_position=0,
                event_subordinal=0,
                role="user",
                content_text="<command-name>/effort</command-name>",
                interaction_kind="durable_user_message",
                raw_record_ordinal=0,
            ),
        ),
    )

    class RawReader:
        async def read(self, object_path, object_hash, tenant_id):
            return read_raw_object(tmp_path, object_path, expected_object_hash=object_hash)

    class Catalog:
        async def call(self, method, params):
            assert method == "storage.session.raw_manifest.v2"
            return {
                "found": True,
                "objects": [
                    {
                        "envelope_id": command_sealed.envelope_id,
                        "machine_id": "cinder",
                        "provider": "claude",
                        "opaque_source_id": "history.jsonl",
                        "source_epoch": str(source_epoch),
                        "range_start": 0,
                        "range_end": 1,
                        "object_path": command_sealed.object_path,
                        "object_hash": command_sealed.object_hash,
                        "tenant_id": "tenant-a",
                    },
                    {
                        "envelope_id": caveat_sealed.envelope_id,
                        "machine_id": "cinder",
                        "provider": "claude",
                        "opaque_source_id": "history.jsonl",
                        "source_epoch": str(source_epoch),
                        "range_start": 1,
                        "range_end": 2,
                        "object_path": caveat_sealed.object_path,
                        "object_hash": caveat_sealed.object_hash,
                        "tenant_id": "tenant-a",
                    },
                ],
                "objects_truncated": False,
            }

    recovered = await recover_render_interaction_kinds(
        catalog=Catalog(),
        raw_workers=RawReader(),
        session_id=str(session_id),
        owner_id="42",
        provider="claude",
        records=render.records,
        source_envelope_id=command_sealed.envelope_id,
        manifest_cache={},
        sequence_context_cache={},
        reclassify_sequence_controls=True,
    )

    assert recovered == {0: "local_control"}


@pytest.mark.asyncio
async def test_user_read_lane_decodes_in_persistent_process(tmp_path):
    sealed = seal_render_object(tmp_path, _spec())
    pool = RenderObjectWorkerPool(
        tmp_path,
        live_workers=1,
        repair_workers=1,
        user_read_workers=1,
        queue_multiplier=1,
    )
    try:
        decoded = await pool.read(sealed.object_path, sealed.object_hash, lane="user")
        assert decoded.spec == _spec()
    finally:
        await pool.close()
