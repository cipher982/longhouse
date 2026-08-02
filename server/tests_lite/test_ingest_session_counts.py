"""Tests that ingest_session correctly counts user/assistant/tool events.

Tool-call events (assistant role + tool_name set) must count toward tool_calls
only, not assistant_messages, so the UI shows accurate conversation turns.
"""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.routers.health import _session_enrichment_lag_check
from zerg.services.agents import AgentsStore
from zerg.services.agents import EventIngest
from zerg.services.agents import SessionIngest
from zerg.services.write_serializer import get_write_serializer


def _make_store(tmp_path):
    db_path = tmp_path / "counts.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    factory = sessionmaker(bind=engine)
    get_write_serializer().configure(factory)
    db = factory()
    return AgentsStore(db), db, factory


def _ts():
    return datetime(2026, 2, 22, tzinfo=timezone.utc)


def _claude_local_command_event(content: str, offset: int) -> EventIngest:
    return EventIngest(
        role="user",
        content_text=content,
        raw_json=json.dumps({"type": "user", "isMeta": True, "message": {"role": "user", "content": content}}),
        timestamp=_ts(),
        source_path="/claude/session.jsonl",
        source_offset=offset,
    )


def test_tool_call_events_count_as_tools_not_turns(tmp_path):
    """Assistant events with tool_name set should increment tool_calls, not assistant_messages."""
    store, db, _ = _make_store(tmp_path)
    ts = _ts()
    session_id = uuid4()

    result = store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=ts,
            events=[
                # 1 user turn
                EventIngest(role="user", content_text="hi", timestamp=ts, source_path="/s.jsonl", source_offset=0),
                # 1 assistant tool-call (should count as tool, not assistant turn)
                EventIngest(
                    role="assistant",
                    tool_name="Bash",
                    tool_input_json={"command": "ls"},
                    timestamp=ts,
                    source_path="/s.jsonl",
                    source_offset=1,
                ),
                # 1 tool result
                EventIngest(
                    role="tool",
                    tool_name="Bash",
                    tool_output_text="file.txt",
                    timestamp=ts,
                    source_path="/s.jsonl",
                    source_offset=2,
                ),
                # 1 assistant text response
                EventIngest(
                    role="assistant",
                    content_text="Done.",
                    timestamp=ts,
                    source_path="/s.jsonl",
                    source_offset=3,
                ),
            ],
        )
    )

    assert result.events_inserted == 4

    from zerg.models.agents import AgentSession
    from zerg.models.agents import TimelineCard

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    assert session is not None

    assert session.user_messages == 1, f"expected 1 user turn, got {session.user_messages}"
    assert session.assistant_messages == 1, f"expected 1 assistant turn, got {session.assistant_messages}"
    assert session.tool_calls == 1, f"expected 1 tool call, got {session.tool_calls}"
    assert session.first_user_message_preview == "hi"
    assert session.last_visible_text_preview == "Done."
    assert session.last_user_message_preview == "hi"
    assert session.last_assistant_message_preview == "Done."

    card = db.query(TimelineCard).filter(TimelineCard.session_id == session_id).one()
    assert card.last_user_message_preview == "hi"
    assert card.last_assistant_message_preview == "Done."


def test_first_user_event_triggers_initial_title_generation(tmp_path):
    store, _db, _ = _make_store(tmp_path)
    ts = _ts()
    session_id = uuid4()

    with patch("zerg.services.session_title_trigger.maybe_start_initial_title_generation") as trigger:
        result = store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="test",
                project="test",
                device_id="dev",
                cwd="/tmp",
                started_at=ts,
                events=[
                    EventIngest(role="user", content_text="name this session", timestamp=ts, source_path="/s.jsonl", source_offset=0),
                ],
            )
        )

    assert result.events_inserted == 1
    trigger.assert_called_once_with(str(session_id), reason="first_user_event")


def test_followup_user_event_does_not_retrigger_initial_title_generation(tmp_path):
    store, _db, _ = _make_store(tmp_path)
    ts = _ts()
    session_id = uuid4()

    with patch("zerg.services.session_title_trigger.maybe_start_initial_title_generation") as trigger:
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="test",
                project="test",
                device_id="dev",
                cwd="/tmp",
                started_at=ts,
                events=[
                    EventIngest(role="user", content_text="first prompt", timestamp=ts, source_path="/s.jsonl", source_offset=0),
                ],
            )
        )
        trigger.assert_called_once_with(str(session_id), reason="first_user_event")
        trigger.reset_mock()

        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="test",
                project="test",
                device_id="dev",
                cwd="/tmp",
                started_at=ts,
                events=[
                    EventIngest(role="user", content_text="second prompt", timestamp=ts, source_path="/s.jsonl", source_offset=1),
                ],
            )
        )

    trigger.assert_not_called()


def test_warmup_user_event_does_not_trigger_initial_title_generation(tmp_path):
    store, _db, _ = _make_store(tmp_path)
    ts = _ts()
    session_id = uuid4()

    with patch("zerg.services.session_title_trigger.maybe_start_initial_title_generation") as trigger:
        result = store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="test",
                project="test",
                device_id="dev",
                cwd="/tmp",
                started_at=ts,
                events=[
                    EventIngest(role="user", content_text="warmup", timestamp=ts, source_path="/s.jsonl", source_offset=0),
                ],
            )
        )

    assert result.events_inserted == 1
    trigger.assert_not_called()


def test_claude_local_command_does_not_count_or_trigger_title_generation(tmp_path):
    store, db, _ = _make_store(tmp_path)
    session_id = uuid4()
    command = "\n".join(
        (
            "<local-command-caveat>Caveat: /effort is a local command.</local-command-caveat>",
            "<command-name>/effort</command-name>",
            "<command-message>effort</command-message>",
            "<command-args>high</command-args>",
        )
    )
    output = "<local-command-stdout>Set effort level to high</local-command-stdout>"

    with patch("zerg.services.session_title_trigger.maybe_start_initial_title_generation") as trigger:
        result = store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="test",
                project="test",
                device_id="dev",
                cwd="/tmp",
                started_at=_ts(),
                events=[_claude_local_command_event(command, 0), _claude_local_command_event(output, 1)],
            )
        )

    session = db.query(AgentSession).filter_by(id=session_id).one()
    assert result.events_inserted == 2
    assert session.user_messages == 0
    assert session.first_user_message_preview is None
    assert session.last_visible_text_preview is None
    trigger.assert_not_called()


def test_claude_command_then_real_prompt_uses_prompt_for_counts_and_title_trigger(tmp_path):
    store, db, _ = _make_store(tmp_path)
    session_id = uuid4()
    command = "<command-name>/effort</command-name><command-args>high</command-args>"
    prompt = "Fix the session title pipeline"

    with patch("zerg.services.session_title_trigger.maybe_start_initial_title_generation") as trigger:
        result = store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="test",
                project="test",
                device_id="dev",
                cwd="/tmp",
                started_at=_ts(),
                events=[_claude_local_command_event(command, 0), _claude_local_command_event(prompt, 1)],
            )
        )

    session = db.query(AgentSession).filter_by(id=session_id).one()
    assert result.events_inserted == 2
    assert session.user_messages == 1
    assert session.first_user_message_preview == prompt
    assert session.last_visible_text_preview == prompt
    trigger.assert_called_once_with(str(session_id), reason="first_user_event")


def test_captured_claude_effort_fixture_uses_only_real_prompt_for_ingest_semantics(tmp_path):
    store, db, _ = _make_store(tmp_path)
    session_id = uuid4()
    fixture = Path(__file__).parent / "fixtures/provider_interactions/claude-2.1.219-effort.jsonl"
    rows = [json.loads(line) for line in fixture.read_text().splitlines()]
    events = [
        EventIngest(
            role=row["message"]["role"],
            content_text=row["message"]["content"],
            raw_json=json.dumps(row),
            timestamp=_ts(),
            source_path="/claude/session.jsonl",
            source_offset=index,
        )
        for index, row in enumerate(rows)
    ]

    with patch("zerg.services.session_title_trigger.maybe_start_initial_title_generation") as trigger:
        result = store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="test",
                project="test",
                device_id="dev",
                cwd="/tmp",
                started_at=_ts(),
                events=events,
            )
        )

    session = db.query(AgentSession).filter_by(id=session_id).one()
    stored_events = db.query(AgentEvent).filter_by(session_id=session_id).order_by(AgentEvent.id.asc()).all()
    assert result.events_inserted == 4
    assert [event.interaction_kind for event in stored_events] == [
        "local_control",
        "local_control",
        "local_control_output",
        "durable_user_message",
    ]
    assert session.user_messages == 1
    assert session.first_user_message_preview == rows[-1]["message"]["content"]
    assert session.last_visible_text_preview == rows[-1]["message"]["content"]
    trigger.assert_called_once_with(str(session_id), reason="first_user_event")


def test_captured_claude_effort_split_across_ingest_batches_keeps_context(tmp_path):
    store, db, _ = _make_store(tmp_path)
    session_id = uuid4()
    fixture = Path(__file__).parent / "fixtures/provider_interactions/claude-2.1.219-effort.jsonl"
    rows = [json.loads(line) for line in fixture.read_text().splitlines()]

    def event(row, offset):
        return EventIngest(
            role=row["message"]["role"],
            content_text=row["message"]["content"],
            raw_json=json.dumps(row),
            timestamp=_ts(),
            source_path="/claude/session.jsonl",
            source_offset=offset,
        )

    store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=_ts(),
            events=[event(rows[0], 0)],
        )
    )

    with patch("zerg.services.session_title_trigger.maybe_start_initial_title_generation") as trigger:
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="test",
                project="test",
                device_id="dev",
                cwd="/tmp",
                started_at=_ts(),
                events=[event(rows[1], 1), event(rows[2], 2), event(rows[3], 3)],
            )
        )

    session = db.query(AgentSession).filter_by(id=session_id).one()
    stored_events = db.query(AgentEvent).filter_by(session_id=session_id).order_by(AgentEvent.id.asc()).all()
    assert [event.interaction_kind for event in stored_events] == [
        "local_control",
        "local_control",
        "local_control_output",
        "durable_user_message",
    ]
    assert [event.interaction_context_key for event in stored_events[:3]] == [
        "prompt-effort-1",
        "prompt-effort-1",
        "prompt-effort-1",
    ]
    assert session.user_messages == 1
    assert session.first_user_message_preview == rows[-1]["message"]["content"]
    trigger.assert_called_once_with(str(session_id), reason="first_user_event")


def test_claude_command_before_caveat_in_one_ingest_batch_is_reclassified(tmp_path):
    store, db, _ = _make_store(tmp_path)
    session_id = uuid4()
    prompt_id = "prompt-effort-reversed"
    command = {
        "type": "user",
        "promptId": prompt_id,
        "message": {"role": "user", "content": "<command-name>/effort</command-name>"},
    }
    caveat = {
        "type": "user",
        "isMeta": True,
        "promptId": prompt_id,
        "message": {"role": "user", "content": "<local-command-caveat>native</local-command-caveat>"},
    }

    def event(row: dict[str, object], offset: int) -> EventIngest:
        content = row["message"]
        assert isinstance(content, dict)
        return EventIngest(
            role="user",
            content_text=str(content["content"]),
            raw_json=json.dumps(row),
            timestamp=_ts(),
            source_path="/claude/session.jsonl",
            source_offset=offset,
        )

    store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=_ts(),
            events=[event(command, 0), event(caveat, 1)],
        )
    )

    session = db.query(AgentSession).filter_by(id=session_id).one()
    stored = db.query(AgentEvent).filter_by(session_id=session_id).order_by(AgentEvent.id.asc()).all()
    assert [row.interaction_kind for row in stored] == ["local_control", "local_control"]
    assert session.user_messages == 0
    assert session.first_user_message_preview is None
    assert session.last_visible_text_preview is None


def test_claude_later_caveat_repairs_prior_ingest_batch(tmp_path):
    store, db, _ = _make_store(tmp_path)
    session_id = uuid4()
    prompt_id = "prompt-effort-cross-batch"
    command = {
        "type": "user",
        "promptId": prompt_id,
        "message": {"role": "user", "content": "<command-name>/effort</command-name>"},
    }
    caveat = {
        "type": "user",
        "isMeta": True,
        "promptId": prompt_id,
        "message": {"role": "user", "content": "<local-command-caveat>native</local-command-caveat>"},
    }

    def event(row: dict[str, object], offset: int) -> EventIngest:
        content = row["message"]
        assert isinstance(content, dict)
        return EventIngest(
            role="user",
            content_text=str(content["content"]),
            raw_json=json.dumps(row),
            timestamp=_ts(),
            source_path="/claude/session.jsonl",
            source_offset=offset,
        )

    store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=_ts(),
            events=[event(command, 0)],
        )
    )
    store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=_ts(),
            events=[event(caveat, 1)],
        )
    )

    session = db.query(AgentSession).filter_by(id=session_id).one()
    stored = db.query(AgentEvent).filter_by(session_id=session_id).order_by(AgentEvent.id.asc()).all()
    assert [row.interaction_kind for row in stored] == ["local_control", "local_control"]
    assert session.user_messages == 0
    assert session.first_user_message_preview is None
    assert session.last_visible_text_preview is None


@pytest.mark.parametrize(
    ("synchronous_projections", "synchronous_session_counts", "incremental_session_counts"),
    [
        (True, True, False),
        (False, False, True),
    ],
)
def test_archive_primary_late_claude_caveat_repairs_counts_without_legacy_raw(
    tmp_path,
    synchronous_projections,
    synchronous_session_counts,
    incremental_session_counts,
):
    store, db, _ = _make_store(tmp_path)
    session_id = uuid4()
    prompt_id = "prompt-effort-archive-primary"
    command = {
        "type": "user",
        "promptId": prompt_id,
        "message": {"role": "user", "content": "<command-name>/effort</command-name>"},
    }
    caveat = {
        "type": "user",
        "isMeta": True,
        "promptId": prompt_id,
        "message": {"role": "user", "content": "<local-command-caveat>native</local-command-caveat>"},
    }

    def event(row: dict[str, object], offset: int) -> EventIngest:
        content = row["message"]
        assert isinstance(content, dict)
        return EventIngest(
            role="user",
            content_text=str(content["content"]),
            raw_json=json.dumps(row),
            timestamp=_ts(),
            source_path="/claude/session.jsonl",
            source_offset=offset,
        )

    ingest_options = {
        "synchronous_projections": synchronous_projections,
        "synchronous_session_counts": synchronous_session_counts,
        "incremental_session_counts": incremental_session_counts,
        "write_legacy_raw": False,
        "trigger_initial_title_generation": False,
    }
    store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=_ts(),
            events=[event(command, 0)],
        ),
        **ingest_options,
    )
    first = db.query(AgentSession).filter_by(id=session_id).one()
    assert first.user_messages == 1
    assert first.first_user_message_preview == command["message"]["content"]

    store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=_ts(),
            events=[event(caveat, 1)],
        ),
        **ingest_options,
    )

    session = db.query(AgentSession).filter_by(id=session_id).one()
    stored = db.query(AgentEvent).filter_by(session_id=session_id).order_by(AgentEvent.id.asc()).all()
    assert [row.interaction_kind for row in stored] == ["local_control", "local_control"]
    assert [row.raw_json for row in stored] == [None, None]
    assert session.user_messages == 0
    assert session.first_user_message_preview is None
    assert session.last_visible_text_preview is None


def test_archive_primary_late_claude_caveat_repairs_uuid_lineage_without_prompt_id(tmp_path):
    store, db, _ = _make_store(tmp_path)
    session_id = uuid4()
    command = {
        "type": "user",
        "uuid": "command-archive-primary",
        "parentUuid": "caveat-archive-primary",
        "message": {"role": "user", "content": "<command-name>/effort</command-name>"},
    }
    caveat = {
        "type": "user",
        "isMeta": True,
        "uuid": "caveat-archive-primary",
        "message": {"role": "user", "content": "<local-command-caveat>native</local-command-caveat>"},
    }

    def event(row: dict[str, object], offset: int) -> EventIngest:
        content = row["message"]
        assert isinstance(content, dict)
        return EventIngest(
            role="user",
            content_text=str(content["content"]),
            raw_json=json.dumps(row),
            timestamp=_ts(),
            source_path="/claude/session.jsonl",
            source_offset=offset,
        )

    options = {
        "synchronous_projections": True,
        "synchronous_session_counts": True,
        "write_legacy_raw": False,
        "trigger_initial_title_generation": False,
    }
    store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=_ts(),
            events=[event(command, 0)],
        ),
        **options,
    )
    assert db.query(AgentSession).filter_by(id=session_id).one().user_messages == 1

    store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=_ts(),
            events=[event(caveat, 1)],
        ),
        **options,
    )

    session = db.query(AgentSession).filter_by(id=session_id).one()
    stored = db.query(AgentEvent).filter_by(session_id=session_id).order_by(AgentEvent.id.asc()).all()
    assert [row.interaction_kind for row in stored] == ["local_control", "local_control"]
    assert [row.interaction_context_key for row in stored] == [
        "uuid:command-archive-primary",
        "uuid:caveat-archive-primary",
    ]
    assert session.user_messages == 0
    assert session.first_user_message_preview is None
    assert session.last_visible_text_preview is None


def test_captured_claude_effort_without_prompt_id_split_across_batches_keeps_uuid_context(tmp_path):
    store, db, _ = _make_store(tmp_path)
    session_id = uuid4()
    fixture = Path(__file__).parent / "fixtures/provider_interactions/claude-2.1.92-effort-no-prompt-id.jsonl"
    rows = [json.loads(line) for line in fixture.read_text().splitlines()]

    def event(row, offset):
        return EventIngest(
            role=row["message"]["role"],
            content_text=row["message"]["content"],
            raw_json=json.dumps(row),
            timestamp=_ts(),
            source_path="/claude/session.jsonl",
            source_offset=offset,
        )

    store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=_ts(),
            events=[event(rows[0], 0)],
        )
    )

    with patch("zerg.services.session_title_trigger.maybe_start_initial_title_generation") as trigger:
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="test",
                project="test",
                device_id="dev",
                cwd="/tmp",
                started_at=_ts(),
                events=[event(rows[1], 1), event(rows[2], 2), event(rows[3], 3)],
            )
        )

    session = db.query(AgentSession).filter_by(id=session_id).one()
    stored_events = db.query(AgentEvent).filter_by(session_id=session_id).order_by(AgentEvent.id.asc()).all()
    assert [event.interaction_kind for event in stored_events] == [
        "local_control",
        "local_control",
        "local_control_output",
        "durable_user_message",
    ]
    assert [event.interaction_context_key for event in stored_events[:3]] == [
        "uuid:claude-caveat-1",
        "uuid:claude-command-1",
        "uuid:claude-output-1",
    ]
    assert session.user_messages == 1
    assert session.first_user_message_preview == rows[-1]["message"]["content"]
    trigger.assert_called_once_with(str(session_id), reason="first_user_event")


def test_oversized_claude_prompt_id_keeps_context_across_ingest_batches(tmp_path):
    store, db, _ = _make_store(tmp_path)
    session_id = uuid4()
    prompt_id = "p" * 512
    caveat = {
        "type": "user",
        "isMeta": True,
        "promptId": prompt_id,
        "message": {"role": "user", "content": "<local-command-caveat>native</local-command-caveat>"},
    }
    command = {
        "type": "user",
        "promptId": prompt_id,
        "message": {"role": "user", "content": "<command-name>/effort</command-name>"},
    }

    def event(row: dict[str, object], offset: int) -> EventIngest:
        message = row["message"]
        assert isinstance(message, dict)
        return EventIngest(
            role="user",
            content_text=str(message["content"]),
            raw_json=json.dumps(row),
            timestamp=_ts(),
            source_path="/claude/session.jsonl",
            source_offset=offset,
        )

    store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=_ts(),
            events=[event(caveat, 0)],
        )
    )
    store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=_ts(),
            events=[event(command, 1)],
        )
    )

    stored = db.query(AgentEvent).filter_by(session_id=session_id).order_by(AgentEvent.id.asc()).all()
    assert [event.interaction_kind for event in stored] == ["local_control", "local_control"]
    assert stored[0].interaction_context_key == stored[1].interaction_context_key
    assert len(stored[0].interaction_context_key.encode("utf-8")) <= 255


def test_incremental_count_ingest_triggers_initial_title_generation(tmp_path):
    store, _db, _ = _make_store(tmp_path)
    ts = _ts()
    session_id = uuid4()

    with patch("zerg.services.session_title_trigger.maybe_start_initial_title_generation") as trigger:
        result = store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="codex",
                environment="test",
                project="test",
                device_id="dev",
                cwd="/tmp",
                started_at=ts,
                events=[
                    EventIngest(role="user", content_text="incremental prompt", timestamp=ts, source_path="/s.jsonl", source_offset=0),
                ],
            ),
            synchronous_projections=False,
            synchronous_session_counts=False,
            incremental_session_counts=True,
        )

    assert result.events_inserted == 1
    trigger.assert_called_once_with(str(session_id), reason="first_user_event")


def test_multiple_tool_calls_per_turn(tmp_path):
    """Each assistant tool-call event increments tool_calls independently."""
    store, db, _ = _make_store(tmp_path)
    ts = _ts()
    session_id = uuid4()

    store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=ts,
            events=[
                EventIngest(role="user", content_text="do stuff", timestamp=ts, source_path="/s.jsonl", source_offset=0),
                # 3 tool calls
                EventIngest(
                    role="assistant",
                    tool_name="Glob",
                    tool_input_json={},
                    timestamp=ts,
                    source_path="/s.jsonl",
                    source_offset=1,
                ),
                EventIngest(
                    role="tool",
                    tool_name="Glob",
                    tool_output_text="a.py",
                    timestamp=ts,
                    source_path="/s.jsonl",
                    source_offset=2,
                ),
                EventIngest(
                    role="assistant",
                    tool_name="Read",
                    tool_input_json={},
                    timestamp=ts,
                    source_path="/s.jsonl",
                    source_offset=3,
                ),
                EventIngest(
                    role="tool",
                    tool_name="Read",
                    tool_output_text="...",
                    timestamp=ts,
                    source_path="/s.jsonl",
                    source_offset=4,
                ),
                EventIngest(
                    role="assistant",
                    tool_name="Edit",
                    tool_input_json={},
                    timestamp=ts,
                    source_path="/s.jsonl",
                    source_offset=5,
                ),
                EventIngest(
                    role="tool",
                    tool_name="Edit",
                    tool_output_text="done",
                    timestamp=ts,
                    source_path="/s.jsonl",
                    source_offset=6,
                ),
                # 1 final text
                EventIngest(role="assistant", content_text="All done.", timestamp=ts, source_path="/s.jsonl", source_offset=7),
            ],
        )
    )

    from zerg.models.agents import AgentSession

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()

    assert session.user_messages == 1
    assert session.assistant_messages == 1
    assert session.tool_calls == 3


def test_compaction_only_append_does_not_inflate_turn_counts(tmp_path):
    """Appending compaction metadata should not create fake user/assistant turns."""
    store, db, _ = _make_store(tmp_path)
    ts = _ts()
    session_id = uuid4()

    source_path = "/compaction/session.jsonl"
    store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=ts,
            events=[
                EventIngest(role="user", content_text="remember yellow", timestamp=ts, source_path=source_path, source_offset=0),
                EventIngest(role="assistant", content_text="noted", timestamp=ts, source_path=source_path, source_offset=1),
            ],
        )
    )

    append_result = store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=ts,
            ended_at=ts,
            events=[
                EventIngest(
                    role="system",
                    content_text="Session compacted to summary",
                    timestamp=ts,
                    source_path=source_path,
                    source_offset=2,
                    raw_json='{"type":"summary","summary":"Session compacted to summary","leafUuid":"leaf-1"}',
                ),
                EventIngest(
                    role="system",
                    content_text="Conversation compacted [trigger=auto]",
                    timestamp=ts,
                    source_path=source_path,
                    source_offset=3,
                    raw_json='{"type":"system","subtype":"compact_boundary","timestamp":"2026-02-22T00:00:00Z"}',
                ),
            ],
        )
    )
    assert append_result.events_inserted == 2

    from zerg.models.agents import AgentSession

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    assert session is not None
    assert session.user_messages == 1
    assert session.assistant_messages == 1
    assert session.tool_calls == 0


def test_session_enrichment_lag_surfaces_embedding_backlog(tmp_path):
    store, db, factory = _make_store(tmp_path)
    session_id = str(uuid4())
    ts = _ts().isoformat().replace("+00:00", "Z")

    result = store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="codex",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=ts,
            events=[
                EventIngest(
                    role="user",
                    content_text="needs enrichment",
                    timestamp=ts,
                    source_path="/archive.jsonl",
                    source_offset=0,
                )
            ],
        )
    )
    assert result.events_inserted == 1

    lag = _session_enrichment_lag_check(factory)
    assert lag["status"] == "warn"
    assert lag["pending_sessions"] == 1

    from zerg.models.agents import AgentSession

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    assert session is not None
    session.needs_embedding = 0
    db.commit()

    lag_after = _session_enrichment_lag_check(factory)
    assert lag_after["status"] == "pass"
    assert lag_after["pending_sessions"] == 0
