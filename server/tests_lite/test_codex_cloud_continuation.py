"""Tests for Codex cloud continuation support in session_chat."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from zerg.services.session_continuity import prepare_codex_session_for_resume


@pytest.fixture
def codex_session_jsonl():
    """Minimal Codex session JSONL content."""
    lines = [
        json.dumps(
            {
                "timestamp": "2026-03-26T13:00:00.000Z",
                "type": "session_meta",
                "payload": {
                    "id": "019d2a51-8721-7653-ad24-c3a3dad5d04f",
                    "cwd": "/tmp",
                    "originator": "codex_exec",
                },
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": "hello"},
            }
        ),
    ]
    return "\n".join(lines).encode()


@pytest.mark.asyncio
async def test_prepare_codex_session_for_resume_places_file(tmp_path, codex_session_jsonl):
    """Codex session JSONL is placed in the expected directory structure."""
    codex_home = tmp_path / ".codex"

    with (
        patch(
            "zerg.services.session_continuity.fetch_session_from_zerg",
            new_callable=AsyncMock,
            return_value=(codex_session_jsonl, "/tmp", "019d2a51-8721-7653-ad24-c3a3dad5d04f"),
        ),
        patch("zerg.services.session_continuity.get_codex_config_dir", return_value=codex_home),
    ):
        provider_session_id = await prepare_codex_session_for_resume(session_id="test-session-uuid")

    assert provider_session_id == "019d2a51-8721-7653-ad24-c3a3dad5d04f"

    # Should be in sessions/YYYY/MM/DD/ directory
    session_files = list((codex_home / "sessions").rglob("rollout-*.jsonl"))
    assert len(session_files) == 1

    session_file = session_files[0]
    assert "019d2a51-8721-7653-ad24-c3a3dad5d04f" in session_file.name
    assert session_file.read_bytes() == codex_session_jsonl


def test_build_codex_resume_runtime_produces_correct_command():
    """Codex resume runtime builds correct codex exec resume command."""
    from zerg.routers.session_chat import _build_codex_resume_runtime

    with patch("zerg.routers.session_chat._check_codex_binary", return_value=True):
        runtime = _build_codex_resume_runtime(
            provider_session_id="019d2a51-8721-7653-ad24-c3a3dad5d04f",
            message="Continue the task",
        )

    assert runtime.cmd[0] == "codex"
    assert runtime.cmd[1] == "exec"
    assert runtime.cmd[2] == "resume"
    assert runtime.cmd[3] == "019d2a51-8721-7653-ad24-c3a3dad5d04f"
    assert runtime.cmd[4] == "Continue the task"
    assert "--json" in runtime.cmd
    assert "--full-auto" in runtime.cmd
    assert runtime.backend == "codex"


def test_build_codex_resume_runtime_includes_openai_api_key():
    """Codex resume runtime passes OPENAI_API_KEY from env."""
    from zerg.routers.session_chat import _build_codex_resume_runtime

    with (
        patch("zerg.routers.session_chat._check_codex_binary", return_value=True),
        patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key", "SESSION_CHAT_MODEL": ""}),
    ):
        runtime = _build_codex_resume_runtime(
            provider_session_id="test-id",
            message="test",
        )

    assert runtime.env_updates.get("OPENAI_API_KEY") == "sk-test-key"


def test_build_codex_resume_runtime_raises_without_binary():
    """Codex resume runtime raises if codex CLI not found."""
    from zerg.routers.session_chat import _build_codex_resume_runtime

    with (
        patch("zerg.routers.session_chat._check_codex_binary", return_value=False),
        pytest.raises(RuntimeError, match="codex"),
    ):
        _build_codex_resume_runtime(provider_session_id="test-id", message="test")


def test_build_codex_resume_runtime_includes_model_override():
    """Codex resume runtime adds -m flag when SESSION_CHAT_MODEL is set."""
    from zerg.routers.session_chat import _build_codex_resume_runtime

    with (
        patch("zerg.routers.session_chat._check_codex_binary", return_value=True),
        patch.dict("os.environ", {"SESSION_CHAT_MODEL": "o3", "OPENAI_API_KEY": ""}),
    ):
        runtime = _build_codex_resume_runtime(
            provider_session_id="test-id",
            message="test",
        )

    assert "-m" in runtime.cmd
    model_idx = runtime.cmd.index("-m")
    assert runtime.cmd[model_idx + 1] == "o3"


def test_find_latest_codex_session_file(tmp_path):
    """_find_latest_codex_session_file finds the most recent rollout file."""
    from zerg.services.session_continuity import _find_latest_codex_session_file

    # Create fake session files
    session_dir = tmp_path / ".codex" / "sessions" / "2026" / "03" / "26"
    session_dir.mkdir(parents=True)

    old_file = session_dir / "rollout-2026-03-26T10-00-00-old-session-id.jsonl"
    old_file.write_text("{}")

    import time

    time.sleep(0.01)  # ensure mtime differs

    new_file = session_dir / "rollout-2026-03-26T10-30-00-new-session-id.jsonl"
    new_file.write_text("{}")

    with patch("zerg.services.session_continuity.get_codex_config_dir", return_value=tmp_path / ".codex"):
        result = _find_latest_codex_session_file()

    assert result is not None
    assert result.name == new_file.name
