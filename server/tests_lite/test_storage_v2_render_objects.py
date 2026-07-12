from __future__ import annotations

from uuid import UUID

import pytest

from zerg.storage_v2.render_objects import RenderObjectSpec
from zerg.storage_v2.render_objects import RenderObjectValidationError
from zerg.storage_v2.render_objects import RenderRecord
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
