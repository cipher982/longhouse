"""Tests for Codex CLI session provider."""

from __future__ import annotations

import json
from pathlib import Path

from zerg.services.shipper.providers.codex import CodexProvider

# --- Fixture helpers ---


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    """Write a list of dicts as JSONL to a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def _session_meta(
    sid: str = "019c2538-0c3d-7f23-8743-18c6fbf5dd9c",
    cwd: str = "/Users/test/project",
    version: str = "0.94.0",
    branch: str | None = None,
) -> dict:
    meta: dict = {
        "timestamp": "2026-02-03T15:35:56Z",
        "type": "session_meta",
        "payload": {
            "id": sid,
            "cwd": cwd,
            "cli_version": version,
            "model_provider": "openai",
        },
    }
    if branch:
        meta["payload"]["git"] = {"branch": branch, "commit_hash": "abc123"}
    return meta


def _user_message(text: str = "list files", ts: str = "2026-02-03T15:36:00Z") -> dict:
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _assistant_message(text: str = "Here are the files:", ts: str = "2026-02-03T15:36:05Z") -> dict:
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _developer_message(text: str = "You are a helpful assistant.", ts: str = "2026-02-03T15:35:57Z") -> dict:
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _function_call(
    name: str = "shell",
    arguments: str = '{"command": "ls -la"}',
    call_id: str = "call_abc",
    ts: str = "2026-02-03T15:36:10Z",
) -> dict:
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": name,
            "arguments": arguments,
            "call_id": call_id,
        },
    }


def _function_call_output(
    output: str = '{"output": "total 42\\ndrwxr-xr-x  5 user  staff  160 Feb  3 15:35 src"}',
    call_id: str = "call_abc",
    ts: str = "2026-02-03T15:36:15Z",
) -> dict:
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        },
    }


# --- Tests ---


class TestDiscoverFiles:
    def test_discover_files_empty(self, tmp_path: Path) -> None:
        """No sessions dir -> empty list."""
        provider = CodexProvider(config_dir=tmp_path / "nonexistent")
        assert provider.discover_files() == []

    def test_discover_files_finds_jsonl(self, tmp_path: Path) -> None:
        """JSONL files under sessions/ are discovered."""
        sessions = tmp_path / "sessions" / "2026" / "02" / "03"
        sessions.mkdir(parents=True)
        f1 = sessions / "rollout-2026-02-03T15-35-56-abc123.jsonl"
        f1.write_text("{}\n")

        provider = CodexProvider(config_dir=tmp_path)
        files = provider.discover_files()
        assert len(files) == 1
        assert files[0] == f1

    def test_discover_files_ignores_json(self, tmp_path: Path) -> None:
        """Old .json files are not discovered (JSONL only)."""
        sessions = tmp_path / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "rollout-old.json").write_text("{}\n")
        # Also create a JSONL file to confirm it IS found
        (sessions / "rollout-new.jsonl").write_text("{}\n")

        provider = CodexProvider(config_dir=tmp_path)
        files = provider.discover_files()
        assert len(files) == 1
        assert files[0].suffix == ".jsonl"


class TestParseFile:
    def test_parse_user_message(self, tmp_path: Path) -> None:
        """User message yields ParsedEvent with role=user."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_session_meta(), _user_message("list files in current dir")])

        provider = CodexProvider(config_dir=tmp_path)
        events = list(provider.parse_file(path))

        assert len(events) == 1
        evt = events[0]
        assert evt.role == "user"
        assert evt.content_text == "list files in current dir"
        assert evt.raw_type == "codex-user"

    def test_parse_assistant_message(self, tmp_path: Path) -> None:
        """Assistant message with output_text yields role=assistant."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_session_meta(), _assistant_message("Here are the files:")])

        provider = CodexProvider(config_dir=tmp_path)
        events = list(provider.parse_file(path))

        assert len(events) == 1
        evt = events[0]
        assert evt.role == "assistant"
        assert evt.content_text == "Here are the files:"
        assert evt.raw_type == "codex-assistant"

    def test_parse_skips_developer_role(self, tmp_path: Path) -> None:
        """Developer role (system instructions) is filtered out."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(
            path,
            [
                _session_meta(),
                _developer_message("You are a helpful assistant."),
                _user_message("hello"),
            ],
        )

        provider = CodexProvider(config_dir=tmp_path)
        events = list(provider.parse_file(path))

        # Only user message, no developer
        assert len(events) == 1
        assert events[0].role == "user"

    def test_parse_function_call(self, tmp_path: Path) -> None:
        """function_call yields tool_name + tool_input_json."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(
            path,
            [
                _session_meta(),
                _function_call(name="shell", arguments='{"command": "ls -la"}'),
            ],
        )

        provider = CodexProvider(config_dir=tmp_path)
        events = list(provider.parse_file(path))

        assert len(events) == 1
        evt = events[0]
        assert evt.role == "assistant"
        assert evt.tool_name == "shell"
        assert evt.tool_input_json == {"command": "ls -la"}
        assert evt.raw_type == "codex-function_call"

    def test_parse_function_call_output_double_json(self, tmp_path: Path) -> None:
        """Double-JSON-encoded tool output is parsed correctly."""
        # The output field is a JSON string containing {"output": "actual text"}
        double_encoded = json.dumps({"output": "total 42\ndrwxr-xr-x  5 user  staff  160 Feb  3 15:35 src"})
        path = tmp_path / "session.jsonl"
        _write_jsonl(
            path,
            [_session_meta(), _function_call_output(output=double_encoded)],
        )

        provider = CodexProvider(config_dir=tmp_path)
        events = list(provider.parse_file(path))

        assert len(events) == 1
        evt = events[0]
        assert evt.role == "tool"
        assert "total 42" in evt.tool_output_text
        assert "src" in evt.tool_output_text
        assert evt.raw_type == "codex-function_call_output"

    def test_parse_skips_session_meta(self, tmp_path: Path) -> None:
        """session_meta type doesn't yield events."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_session_meta()])

        provider = CodexProvider(config_dir=tmp_path)
        events = list(provider.parse_file(path))

        assert events == []


class TestExtractMetadata:
    def test_extract_metadata(self, tmp_path: Path) -> None:
        """session_meta fields are extracted correctly."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(
            path,
            [
                _session_meta(
                    sid="019c2538-0c3d-7f23-8743-18c6fbf5dd9c",
                    cwd="/Users/test/myproject",
                    version="0.94.0",
                ),
                _user_message(ts="2026-02-03T15:36:00Z"),
                _assistant_message(ts="2026-02-03T15:36:05Z"),
            ],
        )

        provider = CodexProvider(config_dir=tmp_path)
        meta = provider.extract_metadata(path)

        assert meta.session_id == "019c2538-0c3d-7f23-8743-18c6fbf5dd9c"
        assert meta.cwd == "/Users/test/myproject"
        assert meta.version == "0.94.0"
        assert meta.project == "myproject"
        assert meta.started_at is not None
        assert meta.ended_at is not None
        assert meta.started_at <= meta.ended_at

    def test_extract_metadata_with_git(self, tmp_path: Path) -> None:
        """git.branch is extracted from session_meta."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(
            path,
            [_session_meta(branch="feat/codex-support")],
        )

        provider = CodexProvider(config_dir=tmp_path)
        meta = provider.extract_metadata(path)

        assert meta.git_branch == "feat/codex-support"

    def test_session_id_from_session_meta(self, tmp_path: Path) -> None:
        """session_id comes from payload.id, not filename."""
        path = tmp_path / "rollout-2026-02-03T15-35-56-filename-uuid.jsonl"
        real_id = "019c2538-0c3d-7f23-8743-18c6fbf5dd9c"
        _write_jsonl(path, [_session_meta(sid=real_id), _user_message("hello")])

        provider = CodexProvider(config_dir=tmp_path)

        # extract_metadata should use payload.id
        meta = provider.extract_metadata(path)
        assert meta.session_id == real_id

        # parse_file should also use payload.id for event session_id
        events = list(provider.parse_file(path))
        assert len(events) == 1
        assert events[0].session_id == real_id
