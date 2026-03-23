"""Tests for invoke_oikos() and create_oikos_run() — transport-agnostic entry points."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from zerg.models.enums import RunStatus
from zerg.surfaces.adapters.voice import VoiceSurfaceAdapter
from zerg.surfaces.base import SurfaceHandleResult
from zerg.surfaces.base import SurfaceHandleStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_db_session():
    """Return a context-manager factory that yields mock DB sessions."""
    from contextlib import contextmanager

    fiche = SimpleNamespace(id=1, model="test-model")
    thread = SimpleNamespace(id=10)
    run_id_counter = [100]

    @contextmanager
    def _ctx():
        db = MagicMock()

        def _add(obj):
            if hasattr(obj, "fiche_id"):
                obj.id = run_id_counter[0]
                run_id_counter[0] += 1

        db.add.side_effect = _add
        db.commit = MagicMock()
        db.refresh = MagicMock()

        # For finally block: simulate run still RUNNING
        mock_run = SimpleNamespace(
            id=100, status=RunStatus.RUNNING, finished_at=None
        )
        db.query.return_value.filter.return_value.first.return_value = mock_run

        yield db

    return _ctx


def _patch_dependencies(monkeypatch):
    """Patch db_session, OikosService, and event_bus for unit tests."""
    fake_ctx = _fake_db_session()
    monkeypatch.setattr("zerg.database.db_session", fake_ctx)

    fiche = SimpleNamespace(id=1, model="test-model")
    thread = SimpleNamespace(id=10)

    monkeypatch.setattr(
        "zerg.services.oikos_service.OikosService.get_or_create_oikos_fiche",
        lambda self, owner_id: fiche,
    )
    monkeypatch.setattr(
        "zerg.services.oikos_service.OikosService.get_or_create_oikos_thread",
        lambda self, owner_id, fiche: thread,
    )

    published = []

    async def _capture_publish(event_type, data):
        published.append((event_type, data))

    from zerg.events.event_bus import event_bus

    monkeypatch.setattr(event_bus, "publish", _capture_publish)

    return published


# ---------------------------------------------------------------------------
# create_oikos_run tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_oikos_run_returns_setup_without_execution(monkeypatch):
    """create_oikos_run() creates a Run record but does NOT start background execution."""
    published = _patch_dependencies(monkeypatch)

    from zerg.services.oikos_service import create_oikos_run

    setup = await create_oikos_run(owner_id=42, model="test-model")

    assert setup.run_id == 100
    assert setup.fiche_id == 1
    assert setup.thread_id == 10
    assert setup.trace_id is not None

    event_types = [et.value for et, _ in published]
    assert "run_created" in event_types
    assert "run_updated" in event_types


@pytest.mark.asyncio
async def test_create_oikos_run_does_not_call_surface_orchestrator(monkeypatch):
    """Verify create_oikos_run never touches SurfaceOrchestrator."""
    _patch_dependencies(monkeypatch)

    orchestrator_called = False

    def _spy_init(self):
        nonlocal orchestrator_called
        orchestrator_called = True

    monkeypatch.setattr(
        "zerg.surfaces.orchestrator.SurfaceOrchestrator.__init__",
        _spy_init,
    )

    from zerg.services.oikos_service import create_oikos_run

    await create_oikos_run(owner_id=42)
    await asyncio.sleep(0.05)

    assert not orchestrator_called, "create_oikos_run must NOT invoke SurfaceOrchestrator"


# ---------------------------------------------------------------------------
# invoke_oikos tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_oikos_starts_background_execution(monkeypatch):
    """invoke_oikos() should create a run AND start background execution."""
    _patch_dependencies(monkeypatch)

    orchestrator_called = asyncio.Event()

    async def _fake_handle_inbound(self, adapter, raw_input):
        orchestrator_called.set()
        from zerg.surfaces.base import SurfaceHandleResult
        from zerg.surfaces.base import SurfaceHandleStatus

        return SurfaceHandleResult(status=SurfaceHandleStatus.PROCESSED, data={})

    monkeypatch.setattr(
        "zerg.surfaces.orchestrator.SurfaceOrchestrator.handle_inbound",
        _fake_handle_inbound,
    )
    monkeypatch.setattr(
        "zerg.surfaces.adapters.web.WebSurfaceAdapter.__init__",
        lambda self, **kw: None,
    )

    from zerg.services.oikos_service import invoke_oikos

    run_id = await invoke_oikos(
        owner_id=42,
        message="Hello test",
        message_id="msg-123",
        source="web",
    )

    assert run_id == 100
    await asyncio.wait_for(orchestrator_called.wait(), timeout=2.0)
    assert orchestrator_called.is_set()


@pytest.mark.asyncio
async def test_invoke_oikos_accepts_explicit_surface_adapter_and_payload(monkeypatch):
    """invoke_oikos() should allow non-web callers to inject a surface adapter and raw payload."""
    _patch_dependencies(monkeypatch)

    orchestrator_called = asyncio.Event()
    captured: dict[str, object] = {}

    async def _fake_handle_inbound(self, adapter, raw_input):
        event = await adapter.normalize_inbound(raw_input)
        captured["adapter_surface_id"] = adapter.surface_id
        captured["event"] = event
        captured["raw_input"] = dict(raw_input)
        orchestrator_called.set()
        return SurfaceHandleResult(status=SurfaceHandleStatus.PROCESSED, surface_id=adapter.surface_id)

    monkeypatch.setattr(
        "zerg.surfaces.orchestrator.SurfaceOrchestrator.handle_inbound",
        _fake_handle_inbound,
    )

    from zerg.services.oikos_service import invoke_oikos

    voice_adapter = VoiceSurfaceAdapter(owner_id=42, conversation_id="voice:default")
    run_id = await invoke_oikos(
        owner_id=42,
        message="Fallback text",
        message_id="msg-voice",
        source="system",
        surface_adapter=voice_adapter,
        surface_payload={
            "transcript": "Investigate the latest active session.",
            "conversation_id": "voice:operator",
            "timeout": 45,
        },
    )

    assert run_id == 100
    await asyncio.wait_for(orchestrator_called.wait(), timeout=2.0)

    event = captured["event"]
    raw_input = captured["raw_input"]
    assert captured["adapter_surface_id"] == "voice"
    assert event.surface_id == "voice"
    assert event.conversation_id == "voice:operator"
    assert event.text == "Investigate the latest active session."
    assert event.source_message_id == "msg-voice"
    assert raw_input["owner_id"] == 42
    assert raw_input["message_id"] == "msg-voice"
    assert raw_input["run_id"] == 100
    assert raw_input["timeout"] == 45
    assert raw_input["message"] == "Fallback text"
    assert raw_input["trace_id"]


@pytest.mark.asyncio
async def test_invoke_oikos_marks_run_failed_on_crash(monkeypatch):
    """If _execute() crashes, the run should be marked FAILED (not stuck RUNNING)."""
    from contextlib import contextmanager

    finally_reached = asyncio.Event()
    status_updates = []
    call_count = [0]

    # Multi-use db_session: first call is for create_oikos_run (needs _add),
    # second call is the finally block (needs query + commit tracking).
    @contextmanager
    def _multi_ctx():
        call_count[0] += 1
        db = MagicMock()

        if call_count[0] <= 1:
            # Creation phase
            run_id_val = [100]

            def _add(obj):
                if hasattr(obj, "fiche_id"):
                    obj.id = run_id_val[0]

            db.add.side_effect = _add
        else:
            # Finally phase — simulate run still RUNNING
            mock_run = SimpleNamespace(
                id=100, status=RunStatus.RUNNING, finished_at=None
            )
            db.query.return_value.filter.return_value.first.return_value = mock_run

            def _commit():
                status_updates.append(mock_run.status)
                finally_reached.set()

            db.commit = _commit

        yield db

    monkeypatch.setattr("zerg.database.db_session", _multi_ctx)

    fiche = SimpleNamespace(id=1, model="test-model")
    thread = SimpleNamespace(id=10)
    monkeypatch.setattr(
        "zerg.services.oikos_service.OikosService.get_or_create_oikos_fiche",
        lambda self, owner_id: fiche,
    )
    monkeypatch.setattr(
        "zerg.services.oikos_service.OikosService.get_or_create_oikos_thread",
        lambda self, owner_id, fiche: thread,
    )

    published = []

    async def _capture_publish(event_type, data):
        published.append((event_type, data))

    from zerg.events.event_bus import event_bus

    monkeypatch.setattr(event_bus, "publish", _capture_publish)

    async def _crashing_handle_inbound(self, adapter, raw_input):
        raise RuntimeError("Simulated crash")

    monkeypatch.setattr(
        "zerg.surfaces.orchestrator.SurfaceOrchestrator.handle_inbound",
        _crashing_handle_inbound,
    )
    monkeypatch.setattr(
        "zerg.surfaces.adapters.web.WebSurfaceAdapter.__init__",
        lambda self, **kw: None,
    )

    from zerg.services.oikos_service import invoke_oikos

    await invoke_oikos(
        owner_id=42,
        message="This will crash",
        message_id="msg-crash",
    )

    await asyncio.wait_for(finally_reached.wait(), timeout=2.0)
    assert RunStatus.FAILED in status_updates, "Run should be marked FAILED after crash"


# ---------------------------------------------------------------------------
# Replay isolation test (source-level verification)
# ---------------------------------------------------------------------------


def test_task_registry_removed():
    """The process-local task registry should not exist — cancel is DB-only."""
    import zerg.routers.oikos_chat as chat_module

    assert not hasattr(chat_module, "_oikos_tasks"), "Task registry should be deleted"
    assert not hasattr(chat_module, "_register_oikos_task")
    assert not hasattr(chat_module, "_cancel_oikos_task")


def test_replay_branch_uses_create_oikos_run_not_invoke():
    """The replay branch in oikos_chat must use create_oikos_run, not invoke_oikos."""
    import inspect

    import zerg.routers.oikos_chat as chat_module

    source = inspect.getsource(chat_module.oikos_chat)
    lines = source.split("\n")

    # Extract the replay branch
    in_replay = False
    replay_lines = []
    for line in lines:
        if "replay_scenario" in line and "is_replay_enabled" in line:
            in_replay = True
        if in_replay:
            replay_lines.append(line)
            if "return EventSourceResponse" in line:
                break

    replay_block = "\n".join(replay_lines)
    assert "create_oikos_run" in replay_block, "Replay branch must call create_oikos_run"
    assert "invoke_oikos" not in replay_block, "Replay branch must NOT call invoke_oikos"
