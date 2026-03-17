import pytest

from zerg.websocket.manager import TopicConnectionManager


@pytest.mark.asyncio
async def test_automation_lifecycle_events_broadcast_to_canonical_and_legacy_topics():
    manager = object.__new__(TopicConnectionManager)
    broadcasts: list[tuple[str, dict]] = []

    async def _fake_broadcast(topic: str, message: dict) -> None:
        broadcasts.append((topic, message))

    manager.broadcast_to_topic = _fake_broadcast  # type: ignore[attr-defined]

    await TopicConnectionManager._handle_automation_event(
        manager,
        {
            "event_type": "automation_updated",
            "id": 42,
            "status": "running",
            "name": "Nightly sync",
        },
    )

    assert [topic for topic, _ in broadcasts] == ["automation:42", "fiche:42"]

    automation_message = broadcasts[0][1]
    legacy_message = broadcasts[1][1]

    assert automation_message["type"] == "automation_updated"
    assert automation_message["topic"] == "automation:42"
    assert automation_message["data"] == {"id": 42, "status": "running", "name": "Nightly sync"}

    assert legacy_message["type"] == "fiche_updated"
    assert legacy_message["topic"] == "fiche:42"
    assert legacy_message["data"] == automation_message["data"]


@pytest.mark.asyncio
async def test_run_updates_broadcast_to_canonical_and_legacy_topics():
    manager = object.__new__(TopicConnectionManager)
    broadcasts: list[tuple[str, dict]] = []

    async def _fake_broadcast(topic: str, message: dict) -> None:
        broadcasts.append((topic, message))

    manager.broadcast_to_topic = _fake_broadcast  # type: ignore[attr-defined]

    await TopicConnectionManager._handle_run_event(
        manager,
        {
            "event_type": "run_updated",
            "run_id": 7,
            "fiche_id": 42,
            "thread_id": 123,
            "status": "success",
            "started_at": "2026-03-17T10:00:00Z",
            "duration_ms": 1200,
        },
    )

    assert [topic for topic, _ in broadcasts] == ["automation:42", "fiche:42"]

    automation_message = broadcasts[0][1]
    legacy_message = broadcasts[1][1]

    assert automation_message["type"] == "run_update"
    assert automation_message["topic"] == "automation:42"
    assert automation_message["data"]["id"] == 7
    assert automation_message["data"]["thread_id"] == 123
    assert "run_id" not in automation_message["data"]

    assert legacy_message["type"] == "run_update"
    assert legacy_message["topic"] == "fiche:42"
    assert legacy_message["data"] == automation_message["data"]
