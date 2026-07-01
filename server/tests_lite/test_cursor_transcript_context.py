"""Regression tests for Cursor user-message classification.

Cursor injects a large environment-context block (user_info, rules,
agent_transcripts, system_reminders) as a `role="user"` turn the user never
typed, and wraps the real user input in <user_query>...</user_query>. These
tests pin the decoder's classification so the injection is re-roled to
`system` (hidden from the timeline by the existing system filter, raw_json
preserves Cursor's original role) and the real turn is unwrapped.
"""

from __future__ import annotations

import json
import os as _os
from datetime import datetime
from datetime import timezone

from cryptography.fernet import Fernet

_os.environ.setdefault("DATABASE_URL", "sqlite://")
_os.environ.setdefault("TESTING", "1")
_os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
_os.environ.setdefault("JWT_SECRET", "test-jwt-secret-long-enough")
_os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-long-enough")
_os.environ.setdefault("AUTH_DISABLED", "1")

from zerg.services.agents.models import EventIngest
from zerg.services.cursor_transcript import _map_message

_TS = datetime(2026, 7, 1, 16, 0, 0, tzinfo=timezone.utc)
_SRC = "synthetic"


def _map(msg: dict) -> list[EventIngest]:
    return _map_message(msg, _TS, _SRC, {}, EventIngest)


def test_context_injection_re_roled_to_system():
    msg = {
        "role": "user",
        "content": (
            "<user_info>\nOS Version: darwin 25.5.0\n\n"
            "<rules>\n<always_applied_workspace_rule>do thing</...>\n"
            "<agent_transcripts>past chats</agent_transcripts>\n"
            "<system_reminder>plan mode</system_reminder>"
        ),
    }
    events = _map(msg)
    assert len(events) == 1
    ev = events[0]
    assert ev.role == "system"
    assert "<user_info>" in ev.content_text
    # raw_json preserves Cursor's original role="user" (ground truth archive).
    raw = json.loads(ev.raw_json)
    assert raw["role"] == "user"


def test_user_query_unwrapped():
    msg = {"role": "user", "content": "<user_query>\nhello test, banana\n</user_query>"}
    events = _map(msg)
    assert len(events) == 1
    ev = events[0]
    assert ev.role == "user"
    assert ev.content_text == "hello test, banana"
    assert "<user_query>" not in ev.content_text


def test_plain_user_turn_kept_as_user():
    msg = {"role": "user", "content": "just a normal follow-up message"}
    events = _map(msg)
    assert len(events) == 1
    ev = events[0]
    assert ev.role == "user"
    assert ev.content_text == "just a normal follow-up message"


def test_combined_injection_plus_query_emits_user_text():
    msg = {
        "role": "user",
        "content": (
            "<user_info>\nOS Version: darwin\n\n"
            "<user_query>do the thing</user_query>"
        ),
    }
    events = _map(msg)
    assert len(events) == 1
    ev = events[0]
    # <user_query> wins: this is a real user turn; surrounding injection dropped.
    assert ev.role == "user"
    assert ev.content_text == "do the thing"


def test_system_prompt_still_system():
    msg = {
        "role": "system",
        "content": "You are an AI coding assistant, powered by Composer.",
    }
    events = _map(msg)
    assert len(events) == 1
    ev = events[0]
    assert ev.role == "system"
    assert ev.content_text.startswith("You are an AI coding assistant")


def test_user_text_block_list_with_query_unwrapped():
    msg = {
        "role": "user",
        "content": [{"type": "text", "text": "<user_query>list files</user_query>"}],
    }
    events = _map(msg)
    assert len(events) == 1
    ev = events[0]
    assert ev.role == "user"
    assert ev.content_text == "list files"


def test_user_list_with_image_falls_through_to_block_handling():
    # Mixed content (text + image) must not be collapsed; fall through to
    # default block-level handling so non-text blocks are not lost.
    msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "<user_query>look at this</user_query>"},
            {"type": "image", "image": "data:image/png;base64,xyz"},
        ],
    }
    events = _map(msg)
    # Default handling emits one event per block; image is an unknown block type
    # but still surfaces as an event (not dropped).
    assert len(events) >= 1
