"""The shape catalog classifies every transcript line the goldens contain.

A shape nobody has classified is a signal nobody has looked at. The census
tool makes that a failing check instead of a thing the human notices later.
"""

from __future__ import annotations

import json
import sqlite3
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
    golden = REPO_ROOT / "engine" / "tests" / "fixtures" / "golden"
    checks = {
        "claude": (golden / "claude",),
        "codex": (golden / "codex",),
        "cursor": (golden / "cursor",),
        "antigravity": (golden / "antigravity", golden / "antigravity_legacy_json" / "basic.json"),
        "opencode": (golden / "opencode",),
        "pi": (golden / "pi",),
    }
    for provider, paths in checks.items():
        result = _check(provider, *paths)
        assert result.returncode == 0, result.stdout + result.stderr


def test_opencode_sqlite_census_reads_message_and_part_shapes(tmp_path: Path):
    database = tmp_path / "opencode.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE message (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE part (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                time_created INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            INSERT INTO message (id, data) VALUES ('message-1', '{"role":"assistant"}');
            INSERT INTO part (id, message_id, time_created, data)
            VALUES ('part-1', 'message-1', 1, '{"type":"future_text","text":"future"}');
            """
        )

    result = _check("opencode", database)
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
        # Protocol-backed shapes may start with no corpus version, but the current
        # checked-in rollout fixture has now observed stream_error in 0.151.0.
        assert shapes[raw_source]["first_seen_version"] or shapes[raw_source].get("evidence", "").startswith("protocol:"), (
            f"{raw_source} carries neither version history nor protocol evidence"
        )
    assert shapes["event_msg/stream_error"]["first_seen_version"] == "0.151.0"
    # Unlike Claude's turn_duration, Codex closes headless turns too.
    assert "codex_exec" in shapes["event_msg/task_complete"]["entrypoints"]
    assert "longhouse_console" in shapes["event_msg/task_complete"]["entrypoints"]


def test_unclassified_shape_fails_the_check(tmp_path: Path):
    novel = tmp_path / "novel.jsonl"
    novel.write_text('{"type":"system","subtype":"brand_new_thing","timestamp":"2026-09-03T00:00:00Z","version":"9.9.9"}\n')
    result = _check("claude", novel)
    assert result.returncode == 1
    assert "system/brand_new_thing" in result.stdout


def test_a_dropped_required_key_is_drift_not_a_clean_census(tmp_path: Path):
    """A provider that renames durationMs keeps the discriminator; only the key set can say the fact will stop."""
    renamed = tmp_path / "renamed.jsonl"
    renamed.write_text(
        '{"type":"system","subtype":"turn_duration","duration":129299,"messageCount":898,'
        '"timestamp":"2026-09-03T14:20:39.100Z","version":"9.9.9","uuid":"u","sessionId":"s","cwd":"/","gitBranch":"main"}\n'
    )
    result = _check("claude", renamed)
    assert result.returncode == 1
    assert "system/turn_duration" in result.stdout
    assert "durationMs absent in 1 of 1 lines" in result.stdout
    assert "+duration" in result.stdout, "the new name is reported as a key the catalog has never seen"


def test_a_new_key_on_a_known_shape_is_reported_until_seeded(tmp_path: Path):
    """A field the provider added is something to classify, not something to ignore."""
    widened = tmp_path / "widened.jsonl"
    widened.write_text(
        '{"timestamp":"2026-09-03T11:25:31.445Z","type":"event_msg","payload":{"type":"task_complete",'
        '"turn_id":"t-1","last_agent_message":"ok","started_at":1,"completed_at":2,"duration_ms":18391,'
        '"time_to_first_token_ms":8662,"brand_new_field":true}}\n'
    )
    result = _check("codex", widened)
    assert result.returncode == 1
    assert "event_msg/task_complete  +brand_new_field" in result.stdout


def test_required_keys_name_what_the_parser_reads():
    for provider, expected in {
        "claude": {"system/turn_duration": ["durationMs"], "ai-title": ["aiTitle"], "system/away_summary": ["content"]},
        "codex": {
            "event_msg/task_complete": ["duration_ms", "turn_id"],
            "event_msg/token_count": [
                "info",
                "info.last_token_usage",
                "info.last_token_usage.total_tokens",
                "info.last_token_usage.output_tokens",
                "info.model_context_window",
            ],
        },
    }.items():
        shapes = json.loads((REPO_ROOT / "schemas" / "transcript_shapes" / f"{provider}.json").read_text())["shapes"]
        for shape, keys in expected.items():
            assert shapes[shape]["required_keys"] == keys, (provider, shape)
            assert set(keys) <= set(shapes[shape]["keys"]), "a required key must be one the provider has actually written"


def test_every_launch_provider_has_boundary_and_drift_catalog_rows():
    expected = {
        "claude": ("user/origin_task_notification+string", "provider_notification:task_notification"),
        "codex": ("response_item/message/user/provider_system", "provider_system:context_injection"),
        "cursor": ("user/provider_context+text", "provider_injection:context"),
        "antigravity": ("UI/USER_INPUT", "transcript:user"),
        "opencode": ("part/future_text/assistant", "provider_drift:unknown_text"),
        "pi": ("message/user/text", "transcript:user"),
    }
    for provider, (shape, classification) in expected.items():
        catalog = json.loads((REPO_ROOT / "schemas" / "transcript_shapes" / f"{provider}.json").read_text())
        assert catalog["shapes"][shape]["classification"] == classification
        assert catalog["shapes"][shape]["required_keys"]
