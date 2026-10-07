"""The deploy-window monitor must see a blocked event loop and then stop."""

import asyncio
import json
import logging
import time

import pytest


@pytest.mark.asyncio
async def test_monitor_records_a_blocked_loop_and_ends_after_reopen(monkeypatch, caplog):
    from zerg.services import event_loop_lag

    monkeypatch.setattr(event_loop_lag, "_AFTER_REOPEN_SECONDS", 0.2)
    monitor = event_loop_lag.LoopLagMonitor()
    opened = {"value": False}
    task = asyncio.create_task(monitor.run(lambda: opened["value"]))
    await asyncio.sleep(0.06)
    time.sleep(0.25)  # what a blocking catalog call on the loop does
    await asyncio.sleep(0.06)
    assert monitor.running
    assert monitor.snapshot()["stalls_over_100ms"] == 1
    opened["value"] = True
    with caplog.at_level(logging.INFO, logger=event_loop_lag.__name__):
        await asyncio.wait_for(task, timeout=2.0)
    assert not monitor.running
    line = next(record.getMessage() for record in caplog.records if "deploy_window_loop_lag" in record.getMessage())
    summary = json.loads(line.split(" ", 1)[1])
    assert summary["stalls_over_100ms"] == 1
    assert summary["max_ms"] >= 200
    assert summary["reopened_at_ms"] > 0
