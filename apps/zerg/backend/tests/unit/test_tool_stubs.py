"""Unit tests for the tool stubbing mechanism."""

import json
import os
import tempfile
from unittest.mock import patch

import pytest

from zerg.testing.tool_stubs import (
    _matches,
    clear_stubs_cache,
    get_tool_stubs,
    match_stub,
)


@pytest.fixture(autouse=True)
def reset_stubs_cache():
    """Clear the stubs cache before each test."""
    clear_stubs_cache()
    yield
    clear_stubs_cache()


class TestMatchRules:
    """Test the _matches function for various match rules."""

    def test_empty_rules_always_match(self):
        assert _matches({}, {"host": "cube", "command": "df -h"})

    def test_match_all_always_matches(self):
        assert _matches({"match_all": True}, {"anything": "goes"})

    def test_host_exact_match(self):
        rules = {"host": "cube"}
        assert _matches(rules, {"host": "cube", "command": "df -h"})
        assert not _matches(rules, {"host": "clifford", "command": "df -h"})

    def test_command_contains(self):
        rules = {"command_contains": "df"}
        assert _matches(rules, {"host": "cube", "command": "df -h"})
        assert _matches(rules, {"host": "cube", "command": "df --human-readable"})
        assert not _matches(rules, {"host": "cube", "command": "ls -la"})

    def test_combined_rules(self):
        rules = {"host": "cube", "command_contains": "df"}
        assert _matches(rules, {"host": "cube", "command": "df -h"})
        assert not _matches(rules, {"host": "clifford", "command": "df -h"})
        assert not _matches(rules, {"host": "cube", "command": "ls -la"})

    def test_generic_exact_suffix(self):
        rules = {"device_id_exact": "1"}
        assert _matches(rules, {"device_id": "1", "other": "stuff"})
        assert not _matches(rules, {"device_id": "2"})

    def test_generic_contains_suffix(self):
        rules = {"path_contains": "/home"}
        assert _matches(rules, {"path": "/home/user/docs"})
        assert not _matches(rules, {"path": "/var/log"})


class TestGetToolStubs:
    """Test loading stubs from file."""

    def test_no_env_var_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            clear_stubs_cache()
            assert get_tool_stubs() is None

    def test_nonexistent_file_returns_none(self):
        with patch.dict(os.environ, {"ZERG_TOOL_STUBS_PATH": "/nonexistent/path.json"}):
            clear_stubs_cache()
            assert get_tool_stubs() is None

    def test_valid_file_loads_stubs(self):
        stubs = {
            "ssh_exec": [
                {"match": {"host": "cube"}, "result": {"ok": True, "data": {"stdout": "test"}}}
            ]
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(stubs, f)
            f.flush()
            with patch.dict(os.environ, {"ZERG_TOOL_STUBS_PATH": f.name}):
                clear_stubs_cache()
                loaded = get_tool_stubs()
                assert loaded is not None
                assert "ssh_exec" in loaded
            os.unlink(f.name)

    def test_invalid_json_returns_none(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{")
            f.flush()
            with patch.dict(os.environ, {"ZERG_TOOL_STUBS_PATH": f.name}):
                clear_stubs_cache()
                assert get_tool_stubs() is None
            os.unlink(f.name)


class TestMatchStub:
    """Test the match_stub function."""

    def test_no_stubs_enabled_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            clear_stubs_cache()
            assert match_stub("ssh_exec", {"host": "cube"}) is None

    def test_matching_stub_returns_result(self):
        stubs = {
            "ssh_exec": [
                {
                    "match": {"host": "cube", "command_contains": "df"},
                    "result": {"ok": True, "data": {"stdout": "45% used", "exit_code": 0}},
                }
            ]
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(stubs, f)
            f.flush()
            with patch.dict(os.environ, {"ZERG_TOOL_STUBS_PATH": f.name}):
                clear_stubs_cache()
                result = match_stub("ssh_exec", {"host": "cube", "command": "df -h"})
                assert result is not None
                assert result["ok"] is True
                assert result["data"]["stdout"] == "45% used"
            os.unlink(f.name)

    def test_no_matching_stub_returns_none(self):
        stubs = {"ssh_exec": [{"match": {"host": "cube"}, "result": {"ok": True}}]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(stubs, f)
            f.flush()
            with patch.dict(os.environ, {"ZERG_TOOL_STUBS_PATH": f.name}):
                clear_stubs_cache()
                result = match_stub("ssh_exec", {"host": "clifford", "command": "df"})
                assert result is None
            os.unlink(f.name)

    def test_first_matching_stub_wins(self):
        stubs = {
            "ssh_exec": [
                {"match": {"host": "cube"}, "result": {"ok": True, "data": {"stdout": "first"}}},
                {"match": {"host": "cube"}, "result": {"ok": True, "data": {"stdout": "second"}}},
            ]
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(stubs, f)
            f.flush()
            with patch.dict(os.environ, {"ZERG_TOOL_STUBS_PATH": f.name}):
                clear_stubs_cache()
                result = match_stub("ssh_exec", {"host": "cube", "command": "any"})
                assert result["data"]["stdout"] == "first"
            os.unlink(f.name)


class TestStubbingEnabled:
    """Test stubbing enablement via env var."""

    def test_not_enabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            clear_stubs_cache()
            assert get_tool_stubs() is None

    def test_enabled_when_env_var_set(self):
        stubs = {
            "ssh_exec": [
                {"match": {"host": "cube"}, "result": {"ok": True, "data": {"stdout": "test"}}}
            ]
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(stubs, f)
            f.flush()
            with patch.dict(os.environ, {"ZERG_TOOL_STUBS_PATH": f.name}):
                clear_stubs_cache()
                assert get_tool_stubs() is not None
            os.unlink(f.name)
