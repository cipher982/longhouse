"""The shape catalog classifies every transcript line the goldens contain.

A shape nobody has classified is a signal nobody has looked at. The census
tool makes that a failing check instead of a thing the human notices later.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "scripts" / "qa" / "transcript_census.py"


def _check(provider: str, *paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), "check", "--provider", provider, *map(str, paths)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_golden_fixtures_contain_only_classified_shapes():
    for provider in ("claude", "codex"):
        result = _check(provider, REPO_ROOT / "engine" / "tests" / "fixtures" / "golden" / provider)
        assert result.returncode == 0, result.stdout + result.stderr


def test_every_implemented_claude_signal_has_a_classified_shape():
    catalog = json.loads((REPO_ROOT / "schemas" / "transcript_shapes" / "claude.json").read_text())
    shapes = catalog["shapes"]
    for raw_source, signal in {
        "system/turn_duration": "turn.duration",
        "system/away_summary": "session.recap",
        "ai-title": "session.title",
        "system/api_error": "turn.api_error",
        "system/compact_boundary": "context.compaction",
    }.items():
        assert shapes[raw_source]["classification"] == f"signal:{signal}", raw_source
        assert shapes[raw_source]["first_seen_version"], f"{raw_source} carries no version history"
    # The catalog is a ledger: turn_duration is an interactive-only signal.
    assert shapes["system/turn_duration"]["entrypoints"] == ["cli"]


def test_every_implemented_codex_signal_has_a_classified_shape():
    catalog = json.loads((REPO_ROOT / "schemas" / "transcript_shapes" / "codex.json").read_text())
    shapes = catalog["shapes"]
    for raw_source, signal in {
        "event_msg/task_complete": "turn.duration",
        "event_msg/turn_aborted": "turn.duration",
        "event_msg/token_count": "turn.usage",
        "event_msg/error": "turn.api_error",
        "event_msg/stream_error": "turn.api_error",
        "compacted": "context.compaction",
    }.items():
        assert shapes[raw_source]["classification"] == f"signal:{signal}", raw_source
        assert shapes[raw_source]["first_seen_version"], f"{raw_source} carries no version history"
    # Unlike Claude's turn_duration, Codex closes headless turns too.
    assert "codex_exec" in shapes["event_msg/task_complete"]["entrypoints"]
    assert "longhouse_console" in shapes["event_msg/task_complete"]["entrypoints"]


def test_unclassified_shape_fails_the_check(tmp_path: Path):
    novel = tmp_path / "novel.jsonl"
    novel.write_text('{"type":"system","subtype":"brand_new_thing","timestamp":"2026-09-03T00:00:00Z","version":"9.9.9"}\n')
    result = _check("claude", novel)
    assert result.returncode == 1
    assert "system/brand_new_thing" in result.stdout
