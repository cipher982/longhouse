"""Tests for commis backend/model selection in CloudExecutor."""

import asyncio

import pytest

from zerg.services.cloud_executor import CloudExecutor


class _FakeProcess:
    def __init__(self, returncode: int = 0, stdout: bytes = b"ok", stderr: bytes = b""):
        self.returncode = returncode
        self.pid = 12345
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_run_commis_with_backend_and_model_passes_both_flags(tmp_path, monkeypatch):
    seen_cmds: list[list[str]] = []

    async def _fake_create_subprocess_exec(*cmd, **kwargs):
        seen_cmds.append([str(part) for part in cmd])
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    executor = CloudExecutor(hatch_path="hatch")
    result = await executor.run_commis(
        task="do work",
        workspace_path=tmp_path,
        backend="codex",
        model="gpt-5.2",
    )

    assert result.status == "success"
    assert len(seen_cmds) == 1
    cmd = seen_cmds[0]
    assert cmd[:5] == ["hatch", "-b", "codex", "--model", "gpt-5.2"]
    assert "-C" in cmd
    assert str(tmp_path) in cmd


@pytest.mark.asyncio
async def test_run_commis_with_backend_only_omits_model_flag(tmp_path, monkeypatch):
    seen_cmds: list[list[str]] = []

    async def _fake_create_subprocess_exec(*cmd, **kwargs):
        seen_cmds.append([str(part) for part in cmd])
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    executor = CloudExecutor(hatch_path="hatch")
    result = await executor.run_commis(
        task="do work",
        workspace_path=tmp_path,
        backend="gemini",
    )

    assert result.status == "success"
    assert len(seen_cmds) == 1
    cmd = seen_cmds[0]
    assert "-b" in cmd
    assert cmd[cmd.index("-b") + 1] == "gemini"
    assert "--model" not in cmd


@pytest.mark.asyncio
async def test_run_commis_unknown_model_errors_without_spawning(tmp_path, monkeypatch):
    calls = 0

    async def _fake_create_subprocess_exec(*cmd, **kwargs):
        nonlocal calls
        calls += 1
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    executor = CloudExecutor(hatch_path="hatch")
    result = await executor.run_commis(
        task="do work",
        workspace_path=tmp_path,
        model="unknown-model-x",
    )

    assert result.status == "failed"
    assert result.error is not None
    assert "Unknown model" in result.error
    assert calls == 0
