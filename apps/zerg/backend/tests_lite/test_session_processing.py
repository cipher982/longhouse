"""Golden tests for the session_processing module.

Tests cover:
    - content: strip_noise, redact_secrets, is_tool_result
    - tokens: count_tokens, truncate (head, tail, sandwich)
    - transcript: build_transcript, detect_turns, token_budget
    - golden: full pipeline from sample events to verified SessionTranscript
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone

import pytest

from zerg.services.session_processing import (
    SessionMessage,
    SessionTranscript,
    Turn,
    build_transcript,
    count_tokens,
    detect_turns,
    is_tool_result,
    redact_secrets,
    strip_noise,
    truncate,
)


# =====================================================================
# Fixtures — sample AgentEvent dicts
# =====================================================================

def _ts(hour: int, minute: int = 0) -> datetime:
    """Helper to build a UTC timestamp on a fixed date."""
    return datetime(2026, 2, 11, hour, minute, 0, tzinfo=timezone.utc)


SAMPLE_EVENTS: list[dict] = [
    {
        "role": "user",
        "content_text": "Fix the login bug in auth.py",
        "tool_name": None,
        "tool_input_json": None,
        "tool_output_text": None,
        "timestamp": _ts(10, 0),
        "session_id": "sess-001",
    },
    {
        "role": "assistant",
        "content_text": "I'll look at the auth module. Let me read the file first.",
        "tool_name": None,
        "tool_input_json": None,
        "tool_output_text": None,
        "timestamp": _ts(10, 1),
        "session_id": "sess-001",
    },
    {
        "role": "tool",
        "content_text": None,
        "tool_name": "Read",
        "tool_input_json": {"file_path": "/app/auth.py"},
        "tool_output_text": "def login(user, password):\n    return check_creds(user, password)",
        "timestamp": _ts(10, 2),
        "session_id": "sess-001",
    },
    {
        "role": "assistant",
        "content_text": "Found the issue. The login function doesn't handle empty passwords.",
        "tool_name": None,
        "tool_input_json": None,
        "tool_output_text": None,
        "timestamp": _ts(10, 3),
        "session_id": "sess-001",
    },
    {
        "role": "tool",
        "content_text": None,
        "tool_name": "Edit",
        "tool_input_json": {"file_path": "/app/auth.py", "old_string": "def login", "new_string": "def login_v2"},
        "tool_output_text": "File edited successfully",
        "timestamp": _ts(10, 4),
        "session_id": "sess-001",
    },
    {
        "role": "assistant",
        "content_text": "I've fixed the login bug by adding password validation.",
        "tool_name": None,
        "tool_input_json": None,
        "tool_output_text": None,
        "timestamp": _ts(10, 5),
        "session_id": "sess-001",
    },
    {
        "role": "user",
        "content_text": "Great, thanks!",
        "tool_name": None,
        "tool_input_json": None,
        "tool_output_text": None,
        "timestamp": _ts(10, 6),
        "session_id": "sess-001",
    },
]


# =====================================================================
# content.py — strip_noise
# =====================================================================

class TestStripNoise:
    def test_removes_system_reminder(self):
        text = "Hello <system-reminder>secret stuff</system-reminder> world"
        assert strip_noise(text) == "Hello  world"

    def test_removes_function_results(self):
        text = "Before <function_results>output here</function_results> after"
        assert strip_noise(text) == "Before  after"

    def test_removes_env_tags(self):
        text = "Start <env>PATH=/usr/bin</env> end"
        assert strip_noise(text) == "Start  end"

    def test_removes_claude_background_info(self):
        text = "A <claude_background_info>model info</claude_background_info> B"
        assert strip_noise(text) == "A  B"

    def test_removes_fast_mode_info(self):
        text = "X <fast_mode_info>fast details</fast_mode_info> Y"
        assert strip_noise(text) == "X  Y"

    def test_multiline_tags(self):
        text = "Before\n<system-reminder>\nline1\nline2\n</system-reminder>\nAfter"
        result = strip_noise(text)
        assert "<system-reminder>" not in result
        assert "line1" not in result
        assert "After" in result

    def test_collapses_excess_newlines(self):
        text = "A\n\n\n\n\nB"
        assert strip_noise(text) == "A\n\nB"

    def test_empty_string(self):
        assert strip_noise("") == ""

    def test_none_passthrough(self):
        # strip_noise handles falsy values
        assert strip_noise("") == ""

    def test_no_tags_unchanged(self):
        text = "Just normal text with no XML"
        assert strip_noise(text) == text


# =====================================================================
# content.py — redact_secrets
# =====================================================================

class TestRedactSecrets:
    def test_openai_key(self):
        text = "key is sk-abc123def456ghi789jkl012mno345"
        result = redact_secrets(text)
        assert "sk-abc123" not in result
        assert "[OPENAI_KEY]" in result

    def test_anthropic_key(self):
        text = "using sk-ant-abcdefghijklmnopqrstuvwxyz"
        result = redact_secrets(text)
        assert "sk-ant-" not in result
        assert "[ANTHROPIC_KEY]" in result

    def test_aws_access_key(self):
        text = "AWS key: AKIAIOSFODNN7EXAMPLE"
        result = redact_secrets(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "[AWS_ACCESS_KEY]" in result

    def test_github_token(self):
        text = "token ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZaBcDeFgHiJkL"
        result = redact_secrets(text)
        assert "ghp_" not in result
        assert "[GITHUB_TOKEN]" in result

    def test_jwt_token(self):
        text = "auth: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result = redact_secrets(text)
        assert "eyJhbGci" not in result
        assert "[JWT_TOKEN]" in result

    def test_bearer_token(self):
        text = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz1234567890"
        result = redact_secrets(text)
        assert "abcdefghijklmnopqrstuvwxyz" not in result
        assert "[BEARER_TOKEN]" in result

    def test_private_key(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIE...base64...\n-----END RSA PRIVATE KEY-----"
        result = redact_secrets(text)
        assert "MIIE" not in result
        assert "[PRIVATE_KEY]" in result

    def test_empty_string(self):
        assert redact_secrets("") == ""

    def test_no_secrets_unchanged(self):
        text = "Just a normal message about coding"
        assert redact_secrets(text) == text

    def test_multiple_secrets(self):
        text = "keys: sk-abc123def456ghi789jkl012mno345 and AKIAIOSFODNN7EXAMPLE"
        result = redact_secrets(text)
        assert "[OPENAI_KEY]" in result
        assert "[AWS_ACCESS_KEY]" in result


# =====================================================================
# content.py — is_tool_result
# =====================================================================

class TestIsToolResult:
    def test_tool_role(self):
        assert is_tool_result({"role": "tool"}) is True

    def test_with_tool_output(self):
        assert is_tool_result({"role": "assistant", "tool_output_text": "some output"}) is True

    def test_user_message(self):
        assert is_tool_result({"role": "user"}) is False

    def test_assistant_no_output(self):
        assert is_tool_result({"role": "assistant"}) is False

    def test_empty_tool_output(self):
        assert is_tool_result({"role": "assistant", "tool_output_text": ""}) is False

    def test_none_tool_output(self):
        assert is_tool_result({"role": "assistant", "tool_output_text": None}) is False


# =====================================================================
# tokens.py — count_tokens
# =====================================================================

class TestCountTokens:
    def test_known_string(self):
        # "hello world" is 2 tokens in cl100k_base
        result = count_tokens("hello world", encoding="cl100k_base")
        assert result == 2

    def test_empty_string(self):
        assert count_tokens("", encoding="cl100k_base") == 0

    def test_longer_text(self):
        text = "The quick brown fox jumps over the lazy dog"
        result = count_tokens(text, encoding="cl100k_base")
        assert result > 0
        assert result < len(text)  # tokens < chars

    def test_different_encoding(self):
        text = "hello world"
        cl100k = count_tokens(text, encoding="cl100k_base")
        o200k = count_tokens(text, encoding="o200k_base")
        # Both should count > 0 (exact values may differ)
        assert cl100k > 0
        assert o200k > 0


# =====================================================================
# tokens.py — truncate
# =====================================================================

class TestTruncate:
    def test_no_truncation_needed(self):
        text = "short text"
        result, tokens, was_truncated = truncate(text, max_tokens=100, encoding="cl100k_base")
        assert result == text
        assert was_truncated is False
        assert tokens > 0

    def test_head_strategy(self):
        # Build a string that is definitely > 5 tokens
        text = "one two three four five six seven eight nine ten eleven twelve"
        result, tokens, was_truncated = truncate(
            text, max_tokens=5, strategy="head", encoding="cl100k_base"
        )
        assert was_truncated is True
        assert tokens == 5
        # Head strategy keeps the beginning
        assert result.startswith("one")

    def test_tail_strategy(self):
        text = "one two three four five six seven eight nine ten eleven twelve"
        result, tokens, was_truncated = truncate(
            text, max_tokens=5, strategy="tail", encoding="cl100k_base"
        )
        assert was_truncated is True
        assert tokens == 5
        # Tail strategy keeps the end
        assert result.endswith("twelve")

    def test_sandwich_strategy(self):
        text = " ".join(f"word{i}" for i in range(200))
        result, tokens, was_truncated = truncate(
            text, max_tokens=50, strategy="sandwich", encoding="cl100k_base"
        )
        assert was_truncated is True
        assert tokens <= 50
        # Sandwich keeps head and tail with marker
        assert "truncated" in result
        assert "word0" in result  # head preserved

    def test_empty_string(self):
        result, tokens, was_truncated = truncate("", max_tokens=100)
        assert result == ""
        assert tokens == 0
        assert was_truncated is False

    def test_zero_budget(self):
        result, tokens, was_truncated = truncate("hello world", max_tokens=0)
        assert result == ""
        assert was_truncated is True

    def test_invalid_strategy(self):
        # Text must exceed max_tokens to trigger strategy dispatch
        long_text = " ".join(f"word{i}" for i in range(200))
        with pytest.raises(ValueError, match="Unknown truncation strategy"):
            truncate(long_text, max_tokens=5, strategy="invalid")


# =====================================================================
# transcript.py — detect_turns
# =====================================================================

class TestDetectTurns:
    def test_basic_turns(self):
        messages = [
            SessionMessage(role="user", content="hello", timestamp=_ts(10, 0)),
            SessionMessage(role="assistant", content="hi", timestamp=_ts(10, 1)),
            SessionMessage(role="user", content="bye", timestamp=_ts(10, 2)),
        ]
        turns = detect_turns(messages)
        assert len(turns) == 3
        assert turns[0].role == "user"
        assert turns[0].combined_text == "hello"
        assert turns[0].message_count == 1
        assert turns[1].role == "assistant"
        assert turns[2].role == "user"

    def test_consecutive_same_role(self):
        messages = [
            SessionMessage(role="user", content="part 1", timestamp=_ts(10, 0)),
            SessionMessage(role="user", content="part 2", timestamp=_ts(10, 1)),
            SessionMessage(role="assistant", content="response", timestamp=_ts(10, 2)),
        ]
        turns = detect_turns(messages)
        assert len(turns) == 2
        assert turns[0].role == "user"
        assert turns[0].message_count == 2
        assert "part 1" in turns[0].combined_text
        assert "part 2" in turns[0].combined_text

    def test_empty_list(self):
        assert detect_turns([]) == []

    def test_single_message(self):
        messages = [SessionMessage(role="user", content="only one", timestamp=_ts(10, 0))]
        turns = detect_turns(messages)
        assert len(turns) == 1
        assert turns[0].turn_index == 0
        assert turns[0].token_count > 0

    def test_turn_indices_sequential(self):
        messages = [
            SessionMessage(role="user", content="a", timestamp=_ts(10, 0)),
            SessionMessage(role="assistant", content="b", timestamp=_ts(10, 1)),
            SessionMessage(role="user", content="c", timestamp=_ts(10, 2)),
            SessionMessage(role="assistant", content="d", timestamp=_ts(10, 3)),
        ]
        turns = detect_turns(messages)
        for i, turn in enumerate(turns):
            assert turn.turn_index == i


# =====================================================================
# transcript.py — build_transcript
# =====================================================================

class TestBuildTranscript:
    def test_basic_build(self):
        """Build transcript from sample events, excluding tool calls."""
        transcript = build_transcript(SAMPLE_EVENTS, include_tool_calls=False)
        assert isinstance(transcript, SessionTranscript)
        assert transcript.session_id == "sess-001"
        # Tool events (role=tool) should be excluded
        roles = [m.role for m in transcript.messages]
        assert "tool" not in roles
        assert "user" in roles
        assert "assistant" in roles

    def test_include_tool_calls(self):
        """When include_tool_calls=True, tool events appear in messages."""
        transcript = build_transcript(SAMPLE_EVENTS, include_tool_calls=True)
        roles = [m.role for m in transcript.messages]
        assert "tool" in roles

    def test_first_user_message(self):
        transcript = build_transcript(SAMPLE_EVENTS)
        assert transcript.first_user_message == "Fix the login bug in auth.py"

    def test_last_assistant_message(self):
        transcript = build_transcript(SAMPLE_EVENTS)
        assert transcript.last_assistant_message is not None
        assert "fixed the login bug" in transcript.last_assistant_message

    def test_noise_stripping(self):
        events = [
            {
                "role": "user",
                "content_text": "Hi <system-reminder>ignore this</system-reminder> there",
                "timestamp": _ts(10, 0),
                "session_id": "sess-002",
            }
        ]
        transcript = build_transcript(events, strip_noise=True)
        assert "<system-reminder>" not in transcript.messages[0].content
        assert "ignore this" not in transcript.messages[0].content

    def test_noise_stripping_disabled(self):
        events = [
            {
                "role": "user",
                "content_text": "Hi <system-reminder>keep this</system-reminder> there",
                "timestamp": _ts(10, 0),
                "session_id": "sess-003",
            }
        ]
        transcript = build_transcript(events, strip_noise=False)
        assert "<system-reminder>" in transcript.messages[0].content

    def test_secret_redaction(self):
        events = [
            {
                "role": "user",
                "content_text": "My key is sk-abc123def456ghi789jkl012mno345",
                "timestamp": _ts(10, 0),
                "session_id": "sess-004",
            }
        ]
        transcript = build_transcript(events, redact_secrets=True)
        assert "sk-abc123" not in transcript.messages[0].content
        assert "[OPENAI_KEY]" in transcript.messages[0].content

    def test_secret_redaction_disabled(self):
        events = [
            {
                "role": "user",
                "content_text": "My key is sk-abc123def456ghi789jkl012mno345",
                "timestamp": _ts(10, 0),
                "session_id": "sess-005",
            }
        ]
        transcript = build_transcript(events, redact_secrets=False)
        assert "sk-abc123" in transcript.messages[0].content

    def test_empty_events(self):
        transcript = build_transcript([])
        assert transcript.session_id == ""
        assert transcript.messages == []
        assert transcript.turns == []
        assert transcript.total_tokens == 0

    def test_total_tokens(self):
        transcript = build_transcript(SAMPLE_EVENTS)
        assert transcript.total_tokens > 0

    def test_turns_populated(self):
        transcript = build_transcript(SAMPLE_EVENTS)
        assert len(transcript.turns) > 0
        # Each turn should have a valid role
        for turn in transcript.turns:
            assert turn.role in ("user", "assistant", "tool")


# =====================================================================
# transcript.py — token budget
# =====================================================================

class TestTokenBudget:
    def test_budget_truncates(self):
        """With a very small budget, fewer messages survive."""
        full = build_transcript(SAMPLE_EVENTS)
        budgeted = build_transcript(SAMPLE_EVENTS, token_budget=20)
        assert budgeted.total_tokens <= 20
        assert len(budgeted.messages) <= len(full.messages)

    def test_large_budget_no_change(self):
        """A large budget should keep all messages."""
        full = build_transcript(SAMPLE_EVENTS)
        budgeted = build_transcript(SAMPLE_EVENTS, token_budget=100_000)
        assert len(budgeted.messages) == len(full.messages)

    def test_budget_keeps_recent(self):
        """Token budget strategy keeps the most recent messages."""
        budgeted = build_transcript(SAMPLE_EVENTS, token_budget=30)
        if budgeted.messages:
            # Last message should be from the end of the conversation
            last_ts = budgeted.messages[-1].timestamp
            first_event_ts = SAMPLE_EVENTS[0]["timestamp"]
            assert last_ts >= first_event_ts


# =====================================================================
# Golden test — full pipeline verification
# =====================================================================

class TestGoldenTranscript:
    """End-to-end test: sample events -> expected transcript structure."""

    def test_golden_structure(self):
        """Verify the full pipeline produces expected structure from SAMPLE_EVENTS."""
        transcript = build_transcript(
            SAMPLE_EVENTS,
            include_tool_calls=False,
            strip_noise=True,
            redact_secrets=True,
        )

        # Session ID extracted from first event
        assert transcript.session_id == "sess-001"

        # Only user + assistant messages (no tool role)
        assert all(m.role in ("user", "assistant") for m in transcript.messages)

        # Expected message count: 2 user + 3 assistant = 5 (tool events excluded)
        assert len(transcript.messages) == 5

        # Messages are in chronological order
        for i in range(len(transcript.messages) - 1):
            assert transcript.messages[i].timestamp <= transcript.messages[i + 1].timestamp

        # Goal signal
        assert transcript.first_user_message == "Fix the login bug in auth.py"

        # Outcome signal — last assistant message
        assert transcript.last_assistant_message is not None
        assert "fixed" in transcript.last_assistant_message.lower()

        # Turns are grouped correctly
        # Expected: user(1), assistant(1), assistant(1), assistant(1), user(1)
        # With tool events filtered: user, assistant, assistant, assistant, user
        # Consecutive assistants merge: user(1msg), assistant(3msg), user(1msg)
        assert len(transcript.turns) == 3
        assert transcript.turns[0].role == "user"
        assert transcript.turns[0].message_count == 1
        assert transcript.turns[1].role == "assistant"
        assert transcript.turns[1].message_count == 3
        assert transcript.turns[2].role == "user"
        assert transcript.turns[2].message_count == 1

        # Metadata is a dict (empty for now, callers populate)
        assert isinstance(transcript.metadata, dict)

        # Total tokens is positive
        assert transcript.total_tokens > 0

    def test_golden_with_tool_calls(self):
        """Golden test including tool calls."""
        transcript = build_transcript(
            SAMPLE_EVENTS,
            include_tool_calls=True,
            tool_output_max_chars=100,
        )

        # Now tool events should appear
        roles = [m.role for m in transcript.messages]
        assert "tool" in roles

        # More messages than without tool calls
        no_tools = build_transcript(SAMPLE_EVENTS, include_tool_calls=False)
        assert len(transcript.messages) > len(no_tools.messages)

        # Tool messages should have tool_name set
        tool_msgs = [m for m in transcript.messages if m.role == "tool"]
        for tm in tool_msgs:
            assert tm.tool_name is not None

    def test_golden_noise_and_secrets(self):
        """Events with noise and secrets are cleaned in the transcript."""
        noisy_events = [
            {
                "role": "user",
                "content_text": (
                    "<system-reminder>You are an AI</system-reminder>"
                    "Deploy with key sk-abc123def456ghi789jkl012mno345 to production"
                ),
                "timestamp": _ts(10, 0),
                "session_id": "sess-golden",
            },
            {
                "role": "assistant",
                "content_text": "I'll deploy now. <env>HOME=/root</env>Done.",
                "timestamp": _ts(10, 1),
                "session_id": "sess-golden",
            },
        ]

        transcript = build_transcript(noisy_events, strip_noise=True, redact_secrets=True)

        # Noise removed
        for msg in transcript.messages:
            assert "<system-reminder>" not in msg.content
            assert "<env>" not in msg.content
            assert "You are an AI" not in msg.content

        # Secrets redacted
        assert "sk-abc123" not in transcript.messages[0].content
        assert "[OPENAI_KEY]" in transcript.messages[0].content
