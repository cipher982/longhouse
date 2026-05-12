import importlib.util
import json
import sys
from pathlib import Path


def _load_probe_module():
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "scripts" / "ops" / "probe-managed-claude-truth.py"
    spec = importlib.util.spec_from_file_location("probe_managed_claude_truth", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_managed_claude_truth_probe_redacts_channel_token():
    probe = _load_probe_module()

    assert probe.redact(
        {
            "auth_token": "secret",
            "X-Longhouse-Channel-Token": "header-secret",
            "nested": {"LONGHOUSE_HOOK_TOKEN": "hook-secret"},
            "safe": "value",
        }
    ) == {
        "auth_token": "<redacted>",
        "X-Longhouse-Channel-Token": "<redacted>",
        "nested": {"LONGHOUSE_HOOK_TOKEN": "<redacted>"},
        "safe": "value",
    }


def test_managed_claude_truth_probe_summarizes_channel_process_truth(monkeypatch):
    probe = _load_probe_module()
    monkeypatch.setattr(probe, "pid_alive", lambda pid: int(pid) in {101, 202})

    summary = probe.summarize_probe(
        session_id="session-1",
        local_health={
            "managed_sessions": [
                {
                    "session_id": "session-1",
                    "provider": "claude",
                    "control_path": "managed",
                    "state": "attached",
                    "raw_phase": "idle",
                }
            ],
            "engine_status": {
                "payload": {
                    "phase_ledger": [
                        {
                            "session_id": "session-1",
                            "phase": "thinking",
                            "source": "claude_hook",
                            "observed_at": "2026-05-12T18:00:00+00:00",
                        }
                    ]
                }
            },
        },
        channel_state={"ready": True, "claude_pid": 101, "bridge_pid": 202},
        channel_health={"ready": True},
        hook_outbox={
            "entries": [
                {
                    "mtime": "2026-05-12T18:00:00+00:00",
                    "payload": {"state": "thinking"},
                }
            ]
        },
        hosted={
            "database": {
                "event_stats": {
                    "assistant_events": 1,
                    "count": 2,
                    "tool_events": 0,
                },
                "recent_runtime_events": [
                    {"kind": "terminal_signal", "source": "claude_channel_scan"},
                    {"kind": "terminal_signal", "source": "claude_channel_wrapper"},
                    {"kind": "phase_signal", "source": "claude_hook"},
                ],
                "runtime_event_stats": {"count": 8},
                "session": {"id": "session-1"},
                "runtime_state": {
                    "phase": "idle",
                    "phase_source": "semantic",
                    "terminal_reason": "process_gone",
                    "terminal_source": "engine_attached_lease",
                    "terminal_state": "process_gone",
                },
            }
        },
    )

    assert summary["local_health_has_managed_claude"] is True
    assert summary["local_health_state"] == "attached"
    assert summary["local_phase_ledger_entries"] == 1
    assert summary["latest_phase_ledger_phase"] == "thinking"
    assert summary["latest_phase_ledger_source"] == "claude_hook"
    assert summary["channel_ready"] is True
    assert summary["claude_pid_alive"] is True
    assert summary["bridge_pid_alive"] is True
    assert summary["hook_outbox_entries"] == 1
    assert summary["latest_hook_state"] == "thinking"
    assert summary["hosted_runtime_phase"] == "idle"
    assert summary["hosted_phase_source"] == "semantic"
    assert summary["hosted_terminal_state"] == "process_gone"
    assert summary["hosted_terminal_reason"] == "process_gone"
    assert summary["hosted_terminal_source"] == "engine_attached_lease"
    assert summary["hosted_archive_event_count"] == 2
    assert summary["hosted_archive_assistant_events"] == 1
    assert summary["hosted_runtime_event_count"] == 8
    assert summary["hosted_terminal_event_count"] == 2
    assert summary["hosted_terminal_event_sources"] == ["claude_channel_scan", "claude_channel_wrapper"]


def test_managed_claude_truth_probe_records_profile_metadata(tmp_path):
    probe = _load_probe_module()
    path = tmp_path / "observations.jsonl"
    recorder = probe.Recorder(path, "run-1", "managed_claude_warm_live_graceful_close", "warm_realtime")

    recorder.write(source="local_health", event="snapshot", session_id="session-1", payload={"auth_token": "secret"})

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["schema"] == "managed_claude_truth_probe.v1"
    assert row["harness_version"] == 1
    assert row["case_id"] == "managed_claude_warm_live_graceful_close"
    assert row["profile_class"] == "warm_realtime"
    assert row["provider"] == "claude"
    assert row["ownership"] == "managed"
    assert row["payload"] == {"auth_token": "<redacted>"}


def test_managed_claude_truth_probe_strips_channel_token_before_recording(tmp_path):
    probe = _load_probe_module()
    state_path = tmp_path / "session-1.json"
    state_path.write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "provider_session_id": "provider-session-1",
                "auth_token": "secret-token",
                "port": 1234,
            }
        ),
        encoding="utf-8",
    )

    assert probe.read_json_file(state_path) == {
        "session_id": "session-1",
        "provider_session_id": "provider-session-1",
        "port": 1234,
    }


def test_managed_claude_truth_probe_collects_matching_hook_outbox(tmp_path):
    probe = _load_probe_module()
    outbox = tmp_path / "agent" / "outbox"
    outbox.mkdir(parents=True)
    (outbox / "prs.match.json").write_text(
        json.dumps({"session_id": "session-1", "provider": "claude", "state": "thinking"}),
        encoding="utf-8",
    )
    (outbox / "prs.other.json").write_text(
        json.dumps({"session_id": "session-2", "provider": "claude", "state": "thinking"}),
        encoding="utf-8",
    )

    snapshot = probe.collect_hook_outbox(longhouse_home=tmp_path, session_id="session-1")

    assert snapshot["exists"] is True
    assert len(snapshot["entries"]) == 1
    assert snapshot["entries"][0]["payload"]["session_id"] == "session-1"
    assert snapshot["entries"][0]["payload"]["state"] == "thinking"
