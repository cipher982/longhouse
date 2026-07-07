"""Tests for the fast-path initial title trigger."""

from __future__ import annotations

import asyncio
import os
import threading

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.services.session_title_trigger import maybe_start_initial_title_generation
from zerg.services.session_title_trigger import reset_initial_title_trigger_for_test


@pytest.mark.asyncio
async def test_initial_title_trigger_dedupes_in_flight(monkeypatch):
    reset_initial_title_trigger_for_test()
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def _fake_generate_initial_title(session_id: str) -> bool:
        calls.append(session_id)
        started.set()
        await release.wait()
        return True

    monkeypatch.setattr(
        "zerg.services.session_summaries.generate_initial_title_impl",
        _fake_generate_initial_title,
    )

    assert maybe_start_initial_title_generation("session-1", reason="test") is True
    assert maybe_start_initial_title_generation("session-1", reason="test") is False

    await asyncio.wait_for(started.wait(), timeout=0.5)
    assert calls == ["session-1"]

    release.set()
    await asyncio.sleep(0)
    reset_initial_title_trigger_for_test()


@pytest.mark.asyncio
async def test_initial_title_trigger_clears_in_flight_after_failure(monkeypatch):
    reset_initial_title_trigger_for_test()
    calls = 0
    first_failed = asyncio.Event()

    async def _fake_generate_initial_title(_session_id: str) -> bool:
        nonlocal calls
        calls += 1
        first_failed.set()
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "zerg.services.session_summaries.generate_initial_title_impl",
        _fake_generate_initial_title,
    )

    assert maybe_start_initial_title_generation("session-fails", reason="test") is True
    await asyncio.wait_for(first_failed.wait(), timeout=0.5)
    await asyncio.sleep(0)

    assert maybe_start_initial_title_generation("session-fails", reason="test") is True
    await asyncio.sleep(0)
    assert calls == 2
    reset_initial_title_trigger_for_test()


def test_initial_title_trigger_runs_without_running_loop(monkeypatch):
    reset_initial_title_trigger_for_test()
    called = threading.Event()
    calls: list[str] = []

    async def _fake_generate_initial_title(session_id: str) -> bool:
        calls.append(session_id)
        called.set()
        return True

    monkeypatch.setattr(
        "zerg.services.session_summaries.generate_initial_title_impl",
        _fake_generate_initial_title,
    )

    assert maybe_start_initial_title_generation("session-sync", reason="test") is True
    assert called.wait(timeout=1.0)
    assert calls == ["session-sync"]
    reset_initial_title_trigger_for_test()
