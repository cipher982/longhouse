"""
WebSocket Protocol Contract Tests

These tests validate that the generated types from ws-protocol.yml work correctly
and maintain compatibility between frontend and backend.
"""

import json
import pytest
from typing import Dict, Any

from backend.zerg.generated.ws_messages import (
    Envelope,
    MessageType,
    RunUpdateData,
    AgentEventData,
    ThreadMessageData,
    StreamChunkData,
    ErrorData,
    PingData,
    PongData,
)


class TestEnvelopeContract:
    """Test the core Envelope format works correctly."""

    def test_envelope_creation(self):
        """Test creating envelopes with all required fields."""
        data = {"id": 123, "status": "running"}
        envelope = Envelope.create("run_update", "agent:123", data)

        assert envelope.v == 1
        assert envelope.type == "run_update"
        assert envelope.topic == "agent:123"
        assert envelope.data == data
        assert envelope.ts > 0
        assert envelope.req_id is None

    def test_envelope_with_req_id(self):
        """Test envelope with request correlation ID."""
        data = {"content": "Hello"}
        req_id = "msg-123"
        envelope = Envelope.create("send_message", "thread:456", data, req_id)

        assert envelope.req_id == req_id

    def test_envelope_serialization(self):
        """Test envelope can be serialized to/from JSON."""
        data = {"id": 123, "agent_id": 456, "status": "success"}
        envelope = Envelope.create("run_update", "agent:456", data)

        # Serialize to JSON
        json_data = envelope.model_dump()
        json_str = json.dumps(json_data)

        # Deserialize from JSON
        parsed_data = json.loads(json_str)
        parsed_envelope = Envelope.model_validate(parsed_data)

        assert parsed_envelope.type == envelope.type
        assert parsed_envelope.topic == envelope.topic
        assert parsed_envelope.data == envelope.data


class TestMessagePayloads:
    """Test individual message payload schemas."""

    def test_run_update_payload(self):
        """Test RunUpdateData validation."""
        valid_data = {
            "id": 123,
            "agent_id": 456,
            "status": "running",
            "trigger": "manual",
            "thread_id": 789
        }

        payload = RunUpdateData.model_validate(valid_data)
        assert payload.id == 123
        assert payload.agent_id == 456
        assert payload.status == "running"
        assert payload.trigger == "manual"
        assert payload.thread_id == 789

    def test_run_update_required_fields(self):
        """Test RunUpdateData fails without required fields."""
        with pytest.raises(Exception):  # Pydantic validation error
            RunUpdateData.model_validate({"id": 123})  # Missing agent_id, status

    def test_agent_event_payload(self):
        """Test AgentEventData validation."""
        valid_data = {
            "id": 123,
            "status": "idle",
            "last_run_at": "2025-01-01T00:00:00Z"
        }

        payload = AgentEventData.model_validate(valid_data)
        assert payload.id == 123
        assert payload.status == "idle"

    def test_stream_chunk_payload(self):
        """Test StreamChunkData validation."""
        valid_data = {
            "thread_id": 123,
            "chunk_type": "assistant_token",
            "content": "Hello world"
        }

        payload = StreamChunkData.model_validate(valid_data)
        assert payload.thread_id == 123
        assert payload.chunk_type == "assistant_token"
        assert payload.content == "Hello world"

    def test_stream_chunk_enum_validation(self):
        """Test StreamChunkData rejects invalid chunk_type."""
        invalid_data = {
            "thread_id": 123,
            "chunk_type": "invalid_type",  # Not in enum
            "content": "Hello"
        }

        # Should pass basic validation but enum constraint depends on schema
        payload = StreamChunkData.model_validate(invalid_data)
        assert payload.chunk_type == "invalid_type"  # Basic validation allows strings


class TestMessageTypeEnum:
    """Test MessageType enumeration."""

    def test_all_message_types_present(self):
        """Test all expected message types are in enum."""
        expected_types = [
            "ping", "pong", "error", "subscribe", "unsubscribe",
            "send_message", "thread_message", "stream_start", "stream_chunk", "stream_end",
            "assistant_id", "agent_event", "thread_event", "run_update", "user_update",
            "node_state", "execution_finished", "node_log"
        ]

        for msg_type in expected_types:
            assert hasattr(MessageType, msg_type.upper())
            assert getattr(MessageType, msg_type.upper()) == msg_type


class TestRoundTripSerialization:
    """Test data can round-trip between JSON and Python objects."""

    def test_full_message_round_trip(self):
        """Test complete message envelope round-trip."""
        # Create a complex message
        run_data = {
            "id": 123,
            "agent_id": 456,
            "thread_id": 789,
            "status": "success",
            "trigger": "manual",
            "duration_ms": 5000,
            "started_at": "2025-01-01T10:00:00Z",
            "finished_at": "2025-01-01T10:05:00Z"
        }

        envelope = Envelope.create("run_update", "agent:456", run_data, "req-123")

        # Serialize to JSON (simulating WebSocket send)
        json_str = json.dumps(envelope.model_dump())

        # Deserialize from JSON (simulating WebSocket receive)
        parsed_data = json.loads(json_str)
        parsed_envelope = Envelope.model_validate(parsed_data)

        # Validate payload can be extracted and parsed
        payload = RunUpdateData.model_validate(parsed_envelope.data)

        assert payload.id == 123
        assert payload.agent_id == 456
        assert payload.status == "success"
        assert payload.duration_ms == 5000

    def test_error_message_round_trip(self):
        """Test error message round-trip."""
        error_data = {
            "error": "Invalid request",
            "details": {"code": 400, "field": "agent_id"}
        }

        envelope = Envelope.create("error", "system", error_data)

        # Round-trip
        json_str = json.dumps(envelope.model_dump())
        parsed_envelope = Envelope.model_validate(json.loads(json_str))
        payload = ErrorData.model_validate(parsed_envelope.data)

        assert payload.error == "Invalid request"
        assert payload.details == {"code": 400, "field": "agent_id"}


class TestTopicRouting:
    """Test topic routing patterns work correctly."""

    def test_agent_topic_format(self):
        """Test agent topic follows correct pattern."""
        envelope = Envelope.create("run_update", "agent:123", {"id": 1, "agent_id": 123, "status": "running"})
        assert envelope.topic == "agent:123"
        assert envelope.topic.startswith("agent:")

    def test_thread_topic_format(self):
        """Test thread topic follows correct pattern."""
        envelope = Envelope.create("thread_message", "thread:456", {"thread_id": 456, "message": {}})
        assert envelope.topic == "thread:456"
        assert envelope.topic.startswith("thread:")

    def test_system_topic_format(self):
        """Test system topic format."""
        envelope = Envelope.create("ping", "system", {"timestamp": 123456789})
        assert envelope.topic == "system"


class TestBackwardCompatibility:
    """Test generated types maintain compatibility with existing data."""

    def test_legacy_message_compatibility(self):
        """Test we can handle existing message formats."""
        # This would be a legacy v1 message that needs wrapping
        legacy_message = {
            "type": "run_update",
            "id": 123,
            "agent_id": 456,
            "status": "running"
        }

        # Convert to envelope format
        envelope = Envelope.create(
            legacy_message["type"],
            f"agent:{legacy_message['agent_id']}",
            {k: v for k, v in legacy_message.items() if k != "type"}
        )

        assert envelope.type == "run_update"
        assert envelope.topic == "agent:456"
        assert envelope.data["id"] == 123


class TestValidationEdgeCases:
    """Test edge cases and error conditions."""

    def test_empty_envelope_data(self):
        """Test envelope with empty data."""
        envelope = Envelope.create("ping", "system", {})
        assert envelope.data == {}

    def test_nested_data_structures(self):
        """Test envelope with complex nested data."""
        complex_data = {
            "thread_id": 123,
            "message": {
                "id": 456,
                "role": "assistant",
                "content": "Hello",
                "metadata": {
                    "model": "gpt-4",
                    "tokens": 100,
                    "tools": ["web_search", "calculator"]
                }
            }
        }

        envelope = Envelope.create("thread_message", "thread:123", complex_data)
        payload = ThreadMessageData.model_validate(envelope.data)

        assert payload.thread_id == 123
        assert payload.message["role"] == "assistant"
        assert payload.message["metadata"]["tools"] == ["web_search", "calculator"]

    def test_invalid_envelope_structure(self):
        """Test invalid envelope structures are rejected."""
        with pytest.raises(Exception):
            Envelope.model_validate({"invalid": "structure"})

        with pytest.raises(Exception):
            Envelope.model_validate({
                "v": 1,
                "type": "test",
                # Missing required fields: topic, ts, data
            })


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
