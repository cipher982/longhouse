"""Focused tests for optional Memory Files behavior."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.services.memory_paths import normalize_memory_path
from zerg.services.memory_paths import normalize_memory_prefix
from zerg.services.memory_summarizer import _should_skip_summary


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
