"""Tests for embedding utilities: round-trip serialization, sanitization, and episode chunking."""

import json
from datetime import datetime
from datetime import timezone

import numpy as np

from zerg.services.session_processing.embeddings import bytes_to_embedding
from zerg.services.session_processing.embeddings import embedding_to_bytes
from zerg.services.session_processing.embeddings import prepare_turn_chunks
from zerg.services.session_processing.embeddings import sanitize_for_embedding


def test_embedding_roundtrip():
    """Serialize and deserialize a numpy array through bytes."""
    original = np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32)
    encoded = embedding_to_bytes(original)
    decoded = bytes_to_embedding(encoded, 4)
    np.testing.assert_array_almost_equal(original, decoded)


def test_claude_local_control_does_not_start_an_embedding_episode() -> None:
    caveat = "<local-command-caveat>native control</local-command-caveat>"
    command = "<command-name>/effort</command-name><command-args>high</command-args>"
    events = [
        {
            "id": 0,
            "role": "user",
            "content_text": caveat,
            "raw_json": json.dumps(
                {
                    "type": "user",
                    "isMeta": True,
                    "promptId": "legacy-effort",
                    "message": {"role": "user", "content": caveat},
                }
            ),
            "timestamp": 1,
        },
        {
            "id": 1,
            "role": "user",
            "content_text": command,
            "raw_json": json.dumps(
                {
                    "type": "user",
                    "promptId": "legacy-effort",
                    "message": {"role": "user", "content": command},
                }
            ),
            "timestamp": 2,
        },
        {
            "id": 2,
            "role": "user",
            "content_text": "<local-command-stdout>state updated</local-command-stdout>",
            "raw_json": json.dumps(
                {
                    "type": "user",
                    "promptId": "legacy-effort",
                    "message": {"role": "user", "content": "<local-command-stdout>state updated</local-command-stdout>"},
                }
            ),
            "timestamp": 3,
        },
        {"id": 3, "role": "user", "content_text": "Build the feature", "timestamp": 4},
        {"id": 4, "role": "assistant", "content_text": "Done", "timestamp": 5},
    ]

    chunks = prepare_turn_chunks(events, provider="claude")

    assert len(chunks) == 1
    assert chunks[0].event_index_start == 0
    assert command not in chunks[0].text
    assert "Build the feature" in chunks[0].text


def test_sanitize_for_embedding():
    """Noise and secrets are stripped from embedding input text."""
    text = "<system-reminder>ignored</system-reminder>Hello world sk-abc123456789012345678901234567890123456789012345"
    cleaned = sanitize_for_embedding(text)
    assert "<system-reminder>" not in cleaned
    assert "Hello world" in cleaned
    # The secret key should be redacted
    assert "sk-abc123456789012345678901234567890123456789012345" not in cleaned


def test_turn_chunks_event_indices(tmp_path):
    """Turn chunks track correct event start/end indices."""
    events = [
        {
            "role": "user",
            "content_text": "What is the capital of France?",
            "tool_name": None,
            "tool_input_json": None,
            "tool_output_text": None,
            "timestamp": datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            "session_id": "test",
        },
        {
            "role": "assistant",
            "content_text": "The capital of France is Paris.",
            "tool_name": None,
            "tool_input_json": None,
            "tool_output_text": None,
            "timestamp": datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
            "session_id": "test",
        },
        {
            "role": "user",
            "content_text": "What about Germany?",
            "tool_name": None,
            "tool_input_json": None,
            "tool_output_text": None,
            "timestamp": datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc),
            "session_id": "test",
        },
        {
            "role": "assistant",
            "content_text": "The capital of Germany is Berlin.",
            "tool_name": None,
            "tool_input_json": None,
            "tool_output_text": None,
            "timestamp": datetime(2026, 1, 1, 0, 3, tzinfo=timezone.utc),
            "session_id": "test",
        },
    ]

    chunks = prepare_turn_chunks(events)
    assert len(chunks) == 2

    # First turn: user(0) + assistant(1)
    assert chunks[0].chunk_index == 0
    assert chunks[0].event_index_start == 0
    assert chunks[0].event_index_end == 1

    # Second turn: user(2) + assistant(3)
    assert chunks[1].chunk_index == 1
    assert chunks[1].event_index_start == 2
    assert chunks[1].event_index_end == 3


def test_turn_chunks_span_full_episode_past_first_assistant_reply(tmp_path):
    """A chunk covers the whole episode, not just the first assistant turn.

    Regression guard: the previous pairing logic stopped a chunk at the first
    assistant reply, so a multi-round tool-call episode (assistant, tool,
    assistant again) silently dropped everything after that first reply from
    the embedded text -- most of what a coding agent actually does.
    """
    events = [
        {
            "role": "user",
            "content_text": "fix the failing test",
            "tool_name": None,
            "tool_input_json": None,
            "tool_output_text": None,
            "timestamp": datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            "session_id": "test",
        },
        {
            "role": "assistant",
            "content_text": "Let me look at the test file first.",
            "tool_name": None,
            "tool_input_json": None,
            "tool_output_text": None,
            "timestamp": datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
            "session_id": "test",
        },
        {
            "role": "assistant",
            "content_text": "Found it -- the fixture was stale, applying the fix now.",
            "tool_name": None,
            "tool_input_json": None,
            "tool_output_text": None,
            "timestamp": datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc),
            "session_id": "test",
        },
        {
            "role": "user",
            "content_text": "thanks, looks good",
            "tool_name": None,
            "tool_input_json": None,
            "tool_output_text": None,
            "timestamp": datetime(2026, 1, 1, 0, 3, tzinfo=timezone.utc),
            "session_id": "test",
        },
    ]

    chunks = prepare_turn_chunks(events)
    assert len(chunks) == 2

    first = chunks[0]
    assert first.event_index_start == 0
    assert first.event_index_end == 2
    assert "stale" in first.text.lower()
    assert "fix now" in first.text.lower() or "fix" in first.text.lower()

    second = chunks[1]
    assert second.event_index_start == 3
    assert second.event_index_end == 3
    assert "thanks" in second.text.lower()


def test_turn_chunks_break_equal_timestamps_by_event_id(tmp_path):
    """Equal event timestamps use the durable row id for stable transcript order."""
    events = [
        {
            "id": 2,
            "role": "assistant",
            "content_text": "Then the answer.",
            "tool_name": None,
            "tool_input_json": None,
            "tool_output_text": None,
            "timestamp": "2026-01-01T00:00:00Z",
            "session_id": "test",
        },
        {
            "id": 1,
            "role": "user",
            "content_text": "First the question.",
            "tool_name": None,
            "tool_input_json": None,
            "tool_output_text": None,
            "timestamp": datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            "session_id": "test",
        },
    ]

    chunks = prepare_turn_chunks(events)

    assert len(chunks) == 1
    assert chunks[0].text.index("First the question.") < chunks[0].text.index("Then the answer.")
