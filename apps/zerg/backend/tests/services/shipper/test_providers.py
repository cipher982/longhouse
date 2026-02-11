"""Tests for multi-provider session parsing."""

import json

from zerg.services.shipper.providers import ProviderRegistry
from zerg.services.shipper.providers import SessionProvider
from zerg.services.shipper.providers import registry
from zerg.services.shipper.providers.claude import ClaudeProvider


class TestProviderRegistry:
    def test_register_and_get(self, tmp_path):
        reg = ProviderRegistry()
        p = ClaudeProvider(config_dir=tmp_path)
        reg.register(p)
        assert reg.get("claude") is p
        assert "claude" in reg.names()

    def test_get_missing_returns_none(self):
        reg = ProviderRegistry()
        assert reg.get("nonexistent") is None

    def test_all_providers(self, tmp_path):
        reg = ProviderRegistry()
        p = ClaudeProvider(config_dir=tmp_path)
        reg.register(p)
        assert len(reg.all()) == 1

    def test_global_registry_has_claude(self):
        # The global registry is populated when providers.claude is imported.
        # shipper.py imports it at module level, so it's always registered.
        assert registry.get("claude") is not None


class TestClaudeProvider:
    def test_name(self, tmp_path):
        p = ClaudeProvider(config_dir=tmp_path)
        assert p.name == "claude"

    def test_discover_files_empty(self, tmp_path):
        p = ClaudeProvider(config_dir=tmp_path)
        assert p.discover_files() == []

    def test_discover_files_finds_jsonl(self, tmp_path):
        projects = tmp_path / "projects" / "test-project"
        projects.mkdir(parents=True)
        f1 = projects / "session1.jsonl"
        f1.write_text('{"type": "user"}\n')
        p = ClaudeProvider(config_dir=tmp_path)
        files = p.discover_files()
        assert len(files) == 1
        assert files[0].name == "session1.jsonl"

    def test_parse_file_yields_events(self, tmp_path):
        projects = tmp_path / "projects"
        projects.mkdir()
        f = projects / "test-session.jsonl"
        event = {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "hello"},
        }
        f.write_text(json.dumps(event) + "\n")
        p = ClaudeProvider(config_dir=tmp_path)
        events = list(p.parse_file(f))
        assert len(events) == 1
        assert events[0].role == "user"
        assert events[0].content_text == "hello"

    def test_extract_metadata(self, tmp_path):
        f = tmp_path / "test-session.jsonl"
        lines = [
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-01-01T12:00:00Z",
                    "cwd": "/home/test/project",
                    "message": {"role": "user", "content": "hi"},
                }
            ),
        ]
        f.write_text("\n".join(lines) + "\n")
        p = ClaudeProvider(config_dir=tmp_path)
        meta = p.extract_metadata(f)
        assert meta.cwd == "/home/test/project"
        assert meta.project == "project"

    def test_implements_protocol(self, tmp_path):
        p = ClaudeProvider(config_dir=tmp_path)
        assert isinstance(p, SessionProvider)
