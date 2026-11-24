"""The served transcript revision rides every projection page and stream frame.

Spec: control-plane/docs/specs/transcript-revision-sync.md. A client holds the
revision of the rows it read and reads again only when the server reports a
newer one, so a page must say what it saw and the stream must say where the
transcript is.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

import zerg.dependencies.auth as _auth_deps  # noqa: F401 - triggers settings init
import zerg.routers.timeline as timeline_mod
import zerg.services.storage_v2_workspace as workspace_module
from zerg.services.storage_v2_workspace import transcript_revision


def _session(session_id, render_revision):
    return SimpleNamespace(
        provider="claude",
        runtime_display=SimpleNamespace(lifecycle="open"),
        capabilities=SimpleNamespace(live_control_available=True, can_start_turn=True),
        session_state=SimpleNamespace(transcript=SimpleNamespace(transcript_revision=render_revision)),
        model_dump=lambda **_kwargs: {"id": str(session_id), "lifecycle": "open", "capabilities": {}},
    )


class _Catalog:
    async def call(self, method, params, *, timeout_seconds=None):
        return {"found": True, "commit_seq": "8", "session": {"owner_id": "42", "updated_at": "2026-11-15T12:00:00Z"}}


class _DisconnectAfterNCycles:
    def __init__(self, n: int) -> None:
        self._checks = 0
        self._n = n

    async def is_disconnected(self) -> bool:
        self._checks += 1
        return self._checks > self._n


def test_transcript_revision_reads_the_served_transcript_revision():
    assert transcript_revision(_session(uuid4(), 901)) == 901
    # No transcript write yet is still an answer: nothing to read.
    assert transcript_revision(_session(uuid4(), 0)) == 0
    assert transcript_revision(SimpleNamespace()) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", ["full", "lite"])
async def test_projection_page_carries_the_revision_of_the_read_before_it(monkeypatch, detail):
    session_id = uuid4()
    order: list[str] = []

    def read_session(_session_id, **_kwargs):
        order.append("session")
        return _session(session_id, 901), None, "7"

    async def read_page(**_kwargs):
        order.append("page")
        return {
            "generation_id": str(uuid4()),
            "events": [
                {
                    "event_id": "event-1",
                    "cursor": "cursor-1",
                    "timestamp": "2026-11-15T12:00:00+00:00",
                    "role": "user",
                    "content_text": "hello",
                    "tool_name": None,
                    "tool_input_json": None,
                    "tool_output_text": None,
                    "tool_call_id": None,
                    "branch_kind": None,
                }
            ],
            "next_cursor": None,
            "has_more": False,
            "total": 1,
        }

    monkeypatch.setattr(workspace_module, "get_catalogd_client", lambda: _Catalog())
    monkeypatch.setattr(workspace_module, "read_live_catalog_session", read_session)
    monkeypatch.setattr(workspace_module, "read_storage_v2_session_events_page", read_page)

    workspace = await workspace_module.build_storage_v2_workspace(
        session_id=session_id, owner_id=42, branch_mode="head", limit=50, detail=detail
    )

    assert workspace is not None
    assert workspace["projection"]["transcript_revision"] == 901
    # The revision is a lower bound on the rows only if it was read first.
    assert order == ["session", "page"]


def test_pushed_delta_keeps_the_page_revision():
    delta = timeline_mod._projection_delta_from_workspace(
        {
            "projection": {
                "generation_id": "g-1",
                "transcript_revision": 77,
                "items": [
                    {"kind": "event", "event": {"id": 1, "cursor": "c-1"}},
                    {"kind": "event", "event": {"id": 2, "cursor": "c-2"}},
                ],
            }
        }
    )
    assert delta is not None
    assert delta["transcript_revision"] == 77
    assert delta["anchor_cursor"] == "c-1"


def _run_stream(session_id, *, cycles, transcript_revision=None, owner_id=None, on_initial=None):
    async def _run():
        frames: list[tuple[str, dict]] = []
        async for event in timeline_mod._live_catalog_workspace_stream(
            _DisconnectAfterNCycles(cycles),
            session_id=session_id,
            skip_initial=True,
            last_event_id=None,
            owner_id=owner_id,
            transcript_revision=transcript_revision,
        ):
            data = json.loads(event["data"])
            frames.append((event["event"], data))
            if event["event"] == "workspace_changed" and data.get("pubsub_seq") == 0 and on_initial is not None:
                on_initial()
            elif event["event"] == "workspace_changed" and data.get("pubsub_seq"):
                break
        return frames

    # A cycle with nothing published waits out the stream's change wait (5 s); a short one runs the same cycles,
    # as test_workspace_projection_delta.py does.
    with patch.object(timeline_mod, "WORKSPACE_STREAM_CHANGE_WAIT_SECONDS", 0.05):
        return asyncio.run(_run())


def test_attach_frames_carry_the_revision_read_at_stream_open():
    from zerg.services.session_pubsub import reset_pubsub_for_test

    reset_pubsub_for_test()
    frames = _run_stream(uuid4(), cycles=2, transcript_revision=15998783)

    connected = next(data for name, data in frames if name == "connected")
    initial = next(data for name, data in frames if name == "workspace_changed")
    assert connected["transcript_revision"] == 15998783
    # The reattach frame is the one the web client used to read as header-only.
    assert initial["latest_event_id"] == 0
    assert initial["transcript_revision"] == 15998783


def test_ingest_wake_carries_the_revision_from_its_catalog_read():
    from zerg.services.session_pubsub import get_pubsub
    from zerg.services.session_pubsub import reset_pubsub_for_test
    from zerg.services.session_pubsub import topic_session

    reset_pubsub_for_test()
    session_id = uuid4()

    def publish_ingest():
        get_pubsub().publish(topic_session(str(session_id)), {"kind": "ingest", "events_inserted": 2})

    with patch.object(timeline_mod, "read_live_catalog_session", lambda *_a, **_k: (_session(session_id, 120), None, "130")):
        frames = _run_stream(session_id, cycles=20, transcript_revision=100, owner_id=7, on_initial=publish_ingest)

    wake = [data for name, data in frames if name == "workspace_changed" and data.get("pubsub_seq")][0]
    assert wake["change_kind"] == "ingest"
    assert wake["transcript_revision"] == 120
    assert wake["catalog_commit_seq"] == 130


def test_preview_wake_carries_no_revision():
    from zerg.services.session_pubsub import publish_session_transcript_preview_update
    from zerg.services.session_pubsub import reset_pubsub_for_test

    reset_pubsub_for_test()
    session_id = uuid4()

    def publish_preview():
        publish_session_transcript_preview_update(
            session_id=str(session_id),
            provider="codex",
            source="codex_bridge_live",
            transcript_preview={"text": "hello", "role": "assistant", "event_id": 1},
        )

    frames = _run_stream(session_id, cycles=20, transcript_revision=100, owner_id=7, on_initial=publish_preview)

    wake = [data for name, data in frames if name == "workspace_changed" and data.get("pubsub_seq")][0]
    assert wake["change_kind"] == "transcript_preview"
    assert "transcript_revision" not in wake
