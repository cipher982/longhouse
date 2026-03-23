"""Tests for reflection job registration defaults."""

import importlib

import zerg.jobs.reflection as reflection_job_module
from zerg.jobs.registry import job_registry


def _reload_reflection_job(monkeypatch, enabled: str | None):
    if enabled is None:
        monkeypatch.delenv("REFLECTION_JOB_ENABLED", raising=False)
    else:
        monkeypatch.setenv("REFLECTION_JOB_ENABLED", enabled)

    job_registry.unregister("session-reflection")
    return importlib.reload(reflection_job_module)


def test_reflection_job_disabled_by_default(monkeypatch):
    module = _reload_reflection_job(monkeypatch, None)

    try:
        config = job_registry.get("session-reflection")
        assert config is not None
        assert config.enabled is False
    finally:
        job_registry.unregister("session-reflection")
        monkeypatch.delenv("REFLECTION_JOB_ENABLED", raising=False)
        importlib.reload(module)


def test_reflection_job_can_be_opted_back_in(monkeypatch):
    module = _reload_reflection_job(monkeypatch, "1")

    try:
        config = job_registry.get("session-reflection")
        assert config is not None
        assert config.enabled is True
    finally:
        job_registry.unregister("session-reflection")
        monkeypatch.delenv("REFLECTION_JOB_ENABLED", raising=False)
        importlib.reload(module)
