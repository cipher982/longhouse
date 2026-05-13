"""Engine ↔ server runtime-observation protocol contract test.

The codex_bridge Rust module emits runtime observations via the shape pinned in
tests_lite/fixtures/codex_bridge_runtime_events.json. If the server's
Pydantic RuntimeEventIngest model rejects that shape, the two sides have
drifted and a live managed session will silently drop events.

Freeze once, update both sides together when protocol changes.
"""

import json
from pathlib import Path

from zerg.services.session_runtime import RuntimeEventBatchIngest
from zerg.services.session_runtime import RuntimeEventIngest

FIXTURE = Path(__file__).parent / "fixtures" / "codex_bridge_runtime_events.json"


def test_codex_bridge_runtime_events_fixture_deserializes():
    data = json.loads(FIXTURE.read_text())
    assert "events" in data
    for raw in data["events"]:
        # Must validate one-by-one so a test failure names the offending event.
        RuntimeEventIngest(**raw)


def test_codex_bridge_runtime_events_fixture_is_a_valid_batch():
    data = json.loads(FIXTURE.read_text())
    batch = RuntimeEventBatchIngest(events=data["events"])
    assert len(batch.events) == len(data["events"])
    # Spot-check fields we rely on for realtime fan-out.
    first = batch.events[0]
    assert first.provider == "codex"
    assert first.source == "codex_bridge"
    assert first.session_id is not None
    assert first.runtime_key.startswith("codex:")


def test_canary_producer_events_match_runtime_event_schema():
    """Producer and server must agree on RuntimeEventIngest shape.

    Same drift trap as the codex_bridge fixture, but for the synthetic
    probe path. Catches regressions in scripts/canary/producer.py that
    would silently break always-on monitoring.
    """
    import importlib.util
    from datetime import datetime, timezone

    repo_root = Path(__file__).resolve().parents[2]
    producer_path = repo_root / "scripts" / "canary" / "producer.py"
    spec = importlib.util.spec_from_file_location("canary_producer", producer_path)
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)

    now = datetime.now(timezone.utc)
    session_id = "a776f692-7fb8-44a7-9574-e347fa29b88e"
    binding = producer._binding_event(session_id, "canary-host", now)
    progress = producer._runtime_event(session_id, 1, "canary-host", now)
    RuntimeEventIngest(**binding)
    RuntimeEventIngest(**progress)
    # Also validate as a batch (the real wire call).
    RuntimeEventBatchIngest(events=[binding, progress])
