"""Focused tests for optional Memory Files behavior."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.services.memory_paths import normalize_memory_path
from zerg.services.memory_paths import normalize_memory_prefix
from zerg.services.memory_summarizer import _should_skip_summary
from zerg.tools.builtin.memory_tools import memory_read
from zerg.tools.builtin.memory_tools import memory_write


def test_memory_tools_fail_cleanly_when_disabled(monkeypatch):
    monkeypatch.delenv("MEMORY_FILES_ENABLED", raising=False)

    write_result = memory_write("notes/test.md", "hello")
    read_result = memory_read("notes/test.md")

    assert write_result["ok"] is False
    assert read_result["ok"] is False
    assert write_result["user_message"] == "Memory Files are disabled for this Longhouse instance."
    assert read_result["user_message"] == "Memory Files are disabled for this Longhouse instance."


def test_normalize_memory_path_rejects_absolute_and_traversal():
    assert normalize_memory_path("notes//project\\summary.md") == "notes/project/summary.md"

    for raw in ("/etc/passwd", "../secret.md", "notes/../../secret.md", "notes/."):
        try:
            normalize_memory_path(raw)
        except ValueError:
            pass
        else:  # pragma: no cover - assertion safety
            raise AssertionError(f"Expected invalid memory path: {raw}")


def test_normalize_memory_prefix_trims_trailing_separator():
    assert normalize_memory_prefix("episodes/2026-03-12/") == "episodes/2026-03-12"


def test_low_signal_summary_guard_skips_trivial_runs():
    assert _should_skip_summary("Live Voice Test Greeting", "Hello there")
    assert _should_skip_summary("Acknowledged Smoke Test Message", "ok")
    assert not _should_skip_summary("Investigate DNS outage", "Found likely Tailscale split-DNS regression")
