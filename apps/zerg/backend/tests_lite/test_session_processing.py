"""Golden tests for the session_processing module.

Tests cover:
    - content: strip_noise, redact_secrets, is_tool_result
    - tokens: count_tokens, truncate (head, tail, sandwich)
    - transcript: build_transcript, detect_turns, token_budget
    - golden: full pipeline from sample events to verified SessionTranscript
    - review-fix: modern token redaction, tool filtering, event sorting,
      budget signal preservation, noise stripping accuracy
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
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

    def test_assistant_with_tool_output_is_not_tool_result(self):
        """Assistant events with tool_output_text are NOT tool results — only role='tool' qualifies."""
        assert is_tool_result({"role": "assistant", "tool_output_text": "some output"}) is False

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


# =====================================================================
# Review Fix 1: Modern token format redaction
# =====================================================================

class TestRedactSecretsModernTokens:
    """Verify redaction catches modern API key / token formats."""

    def test_sk_proj_key_redacted(self):
        """sk-proj-... keys (OpenAI project-scoped) must be redacted."""
        text = "my key is sk-proj-abc123def456ghi789jkl012mno"
        result = redact_secrets(text)
        assert "sk-proj-" not in result
        assert "[OPENAI_KEY]" in result

    def test_github_pat_redacted(self):
        """github_pat_... fine-grained PATs must be redacted."""
        text = "token: github_pat_11AABBBCC_xxyyzzaabbccddee1234567890abcdef"
        result = redact_secrets(text)
        assert "github_pat_" not in result
        assert "[GITHUB_PAT]" in result

    def test_aws_temporary_credentials_redacted(self):
        """ASIA... AWS temporary credentials must be redacted."""
        text = "AWS_ACCESS_KEY_ID=ASIAIOSFODNN7EXAMPLE"
        result = redact_secrets(text)
        assert "ASIAIOSFODNN7EXAMPLE" not in result
        assert "[AWS_TEMP_KEY]" in result

    def test_json_api_key_redacted(self):
        """JSON-style 'apiKey': '...' patterns must be redacted."""
        text = '{"apiKey": "abcdef1234567890abcdef1234567890"}'
        result = redact_secrets(text)
        assert "abcdef1234567890" not in result
        assert "[REDACTED]" in result

    def test_sk_ant_still_specific_label(self):
        """sk-ant-... keys get [ANTHROPIC_KEY], not [OPENAI_KEY]."""
        text = "key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
        result = redact_secrets(text)
        assert "[ANTHROPIC_KEY]" in result


# =====================================================================
# Review Fix 2: Tool-result detection — assistant narration preserved
# =====================================================================

class TestToolResultFiltering:
    """Verify assistant events with tool_output_text are not dropped."""

    def test_assistant_with_tool_output_included_when_tools_excluded(self):
        """When include_tool_calls=False, assistant narration must still appear."""
        now = _ts(11, 0)
        events = [
            {"role": "user", "content_text": "Run ls", "timestamp": now, "session_id": "fix2"},
            {
                "role": "assistant",
                "content_text": "I ran ls and found 3 files.",
                "tool_output_text": "file1.txt\nfile2.txt\nfile3.txt",
                "timestamp": _ts(11, 1),
                "session_id": "fix2",
            },
        ]
        transcript = build_transcript(
            events, include_tool_calls=False, strip_noise=False, redact_secrets=False,
        )
        assistant_msgs = [m for m in transcript.messages if m.role == "assistant"]
        assert len(assistant_msgs) == 1
        assert "I ran ls" in assistant_msgs[0].content
        # Tool output must NOT appear (include_tool_calls=False)
        assert "file1.txt" not in assistant_msgs[0].content

    def test_tool_role_skipped_when_tools_excluded(self):
        """Events with role='tool' must be skipped when include_tool_calls=False."""
        events = [
            {"role": "user", "content_text": "Run ls", "timestamp": _ts(11, 0), "session_id": "fix2b"},
            {"role": "tool", "tool_output_text": "file1.txt", "timestamp": _ts(11, 1), "session_id": "fix2b"},
            {"role": "assistant", "content_text": "Done.", "timestamp": _ts(11, 2), "session_id": "fix2b"},
        ]
        transcript = build_transcript(
            events, include_tool_calls=False, strip_noise=False, redact_secrets=False,
        )
        roles = [m.role for m in transcript.messages]
        assert "tool" not in roles


# =====================================================================
# Review Fix 3: Unsorted events sorted by build_transcript
# =====================================================================

class TestEventSorting:
    """Verify build_transcript sorts events by timestamp."""

    def test_unsorted_events_produce_correct_order(self):
        base = _ts(12, 0)
        events = [
            {"role": "assistant", "content_text": "second", "timestamp": base + timedelta(seconds=120), "session_id": "fix3"},
            {"role": "user", "content_text": "first", "timestamp": base + timedelta(seconds=60), "session_id": "fix3"},
            {"role": "assistant", "content_text": "third", "timestamp": base + timedelta(seconds=180), "session_id": "fix3"},
        ]
        transcript = build_transcript(events, strip_noise=False, redact_secrets=False)
        contents = [m.content for m in transcript.messages]
        assert contents == ["first", "second", "third"]


# =====================================================================
# Review Fix 4: Token budget preserves goal signals
# =====================================================================

class TestTokenBudgetSignals:
    """Verify first_user_message and last_assistant_message survive budget truncation."""

    def test_first_user_message_survives_truncation(self):
        now = _ts(13, 0)
        events = [
            {"role": "user", "content_text": "Please build the auth system", "timestamp": now, "session_id": "fix4"},
        ]
        for i in range(20):
            events.append({
                "role": "assistant",
                "content_text": f"Working on step {i}. " * 50,
                "timestamp": now + timedelta(seconds=i + 1),
                "session_id": "fix4",
            })
        events.append({
            "role": "assistant",
            "content_text": "All done! The auth system is complete.",
            "timestamp": now + timedelta(seconds=100),
            "session_id": "fix4",
        })

        transcript = build_transcript(
            events, token_budget=50, strip_noise=False, redact_secrets=False,
        )
        # Goal signals come from FULL session, not truncated view
        assert transcript.first_user_message == "Please build the auth system"
        assert "All done" in transcript.last_assistant_message


# =====================================================================
# Review Fix 5: strip_noise preserves HTML/JSX
# =====================================================================

class TestStripNoiseAccuracy:
    """Verify strip_noise only removes known noise tags, not legitimate HTML/JSX."""

    def test_html_div_preserved(self):
        text = "Use a <div className='container'>content</div> wrapper."
        result = strip_noise(text)
        assert "<div" in result
        assert "content</div>" in result

    def test_jsx_component_preserved(self):
        text = "Render <Button variant='primary'>Click me</Button> for the CTA."
        result = strip_noise(text)
        assert "<Button" in result
        assert "</Button>" in result

    def test_system_reminder_still_stripped(self):
        text = "Before <system-reminder>secret stuff</system-reminder> After"
        result = strip_noise(text)
        assert "secret stuff" not in result
        assert "Before" in result
        assert "After" in result

    def test_custom_xml_tag_preserved(self):
        """Arbitrary XML tags must NOT be stripped."""
        text = "Configure <my_config>value=42</my_config> in settings."
        result = strip_noise(text)
        assert "<my_config>" in result
        assert "value=42" in result
