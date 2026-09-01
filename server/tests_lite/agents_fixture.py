"""Direct legacy-schema fixtures for tests of current services.

This is deliberately not an ingest implementation. Production transcript writes
go through storage-v2; a few focused tests still need rows in the migration-era
ORM tables that current control and archive services read.
"""

import hashlib
from types import SimpleNamespace

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSessionBranch
from zerg.models.agents import AgentSourceLine
from zerg.services.agents.kernel_writes import ensure_primary_thread
from zerg.services.agents.kernel_writes import record_thread_alias


class SessionFixtureStore:
    def __init__(self, db):
        self.db = db

    def get_session(self, session_id):
        return self.db.query(AgentSession).filter(AgentSession.id == session_id).first()

    def ingest_session(self, data, **options):
        session = self.get_session(data.id) if data.id is not None else None
        created = session is None
        if session is None:
            session = AgentSession(
                id=data.id,
                provider=data.provider,
                environment=data.environment,
                project=data.project,
                device_id=data.device_id,
                device_name=data.device_name,
                cwd=data.cwd,
                git_repo=data.git_repo,
                git_branch=data.git_branch,
                started_at=data.started_at,
                ended_at=None,
                last_activity_at=data.started_at,
                user_messages=0,
                assistant_messages=0,
                tool_calls=0,
                launch_actor=data.launch_actor,
                launch_surface=data.launch_surface,
            )
            self.db.add(session)
            self.db.flush()
        thread = ensure_primary_thread(self.db, session)
        record_thread_alias(
            self.db,
            thread=thread,
            provider=session.provider,
            alias_kind="longhouse_session_id",
            alias_value=str(session.id),
        )
        if data.provider_session_id:
            record_thread_alias(
                self.db,
                thread=thread,
                provider=session.provider,
                alias_kind="provider_session_id",
                alias_value=data.provider_session_id,
            )
        branch = (
            self.db.query(AgentSessionBranch)
            .filter(AgentSessionBranch.session_id == session.id, AgentSessionBranch.is_head == 1)
            .first()
        )
        if branch is None:
            branch = AgentSessionBranch(session_id=session.id, branch_reason="root", is_head=1)
            self.db.add(branch)
            self.db.flush()
        inserted = []
        for event in data.events:
            row = AgentEvent(
                session_id=session.id,
                thread_id=thread.id,
                branch_id=branch.id,
                role=event.role,
                content_text=event.content_text,
                tool_name=event.tool_name,
                tool_input_json=event.tool_input_json,
                tool_output_text=event.tool_output_text,
                tool_call_id=event.tool_call_id,
                timestamp=event.timestamp,
                source_path=event.source_path,
                source_offset=event.source_offset,
                raw_json=event.raw_json,
            )
            self.db.add(row)
            inserted.append(row)
        write_legacy_raw = bool(options.get("write_legacy_raw", True))
        for source_line in data.source_lines:
            encoded = source_line.raw_json.encode("utf-8")
            prior = (
                self.db.query(AgentSourceLine.revision)
                .filter(
                    AgentSourceLine.session_id == session.id,
                    AgentSourceLine.branch_id == branch.id,
                    AgentSourceLine.source_path == source_line.source_path,
                    AgentSourceLine.source_offset == source_line.source_offset,
                )
                .order_by(AgentSourceLine.revision.desc())
                .first()
            )
            self.db.add(
                AgentSourceLine(
                    session_id=session.id,
                    thread_id=thread.id,
                    branch_id=branch.id,
                    source_path=source_line.source_path,
                    source_offset=source_line.source_offset,
                    revision=int(prior[0]) + 1 if prior else 1,
                    is_branch_copy=0,
                    raw_json=source_line.raw_json if write_legacy_raw else "",
                    raw_json_z=None,
                    raw_json_codec=0,
                    line_hash=hashlib.sha256(encoded).hexdigest(),
                )
            )
        if data.events:
            session.last_activity_at = max(event.timestamp for event in data.events)
            session.user_messages += sum(event.role == "user" for event in data.events)
            session.assistant_messages += sum(event.role == "assistant" and not event.tool_name for event in data.events)
            session.tool_calls += sum(bool(event.tool_name) for event in data.events)
        self.db.commit()
        return SimpleNamespace(
            session_id=session.id,
            session_created=created,
            events_inserted=len(inserted),
            events_skipped=0,
            latest_inserted_event_id=max((row.id for row in inserted), default=None),
            source_lines_inserted=len(data.source_lines),
        )
