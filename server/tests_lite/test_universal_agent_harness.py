from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

from zerg.qa import universal_agent_harness as uah

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_exe(path: Path, version: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""#!/usr/bin/env python3
import sys

if sys.argv[1:] == ["--version"]:
    print({version!r})
    raise SystemExit(0)

print("unexpected args", sys.argv[1:], file=sys.stderr)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fake_bins(tmp_path: Path) -> dict[str, Path]:
    return {
        "claude": _write_exe(tmp_path / "bin" / "claude", "2.9.9-fake (Claude Code)"),
        "codex": _write_exe(tmp_path / "bin" / "codex", "codex-cli 9.9.9"),
        "opencode": _write_exe(tmp_path / "bin" / "opencode", "opencode 9.9.9"),
        "antigravity": _write_exe(tmp_path / "bin" / "agy", "agy 9.9.9"),
    }


EXPECTED_ADAPTER_CLASS_BY_PROVIDER = {
    "claude": "ClaudeCodeHarnessAdapter",
    "codex": "CodexOpenAIHarnessAdapter",
    "opencode": "OpenCodeHarnessAdapter",
    "antigravity": "AntigravityHarnessAdapter",
}


def _fake_claude_provider_live(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        r"""#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("2.9.9-fake (Claude Code)")
    raise SystemExit(0)

if args == ["--help"]:
    if os.environ.get("FAKE_CLAUDE_MISSING_SESSION_ID") == "1":
        print("--resume --dangerously-skip-permissions --mcp-config --strict-mcp-config --permission-mode")
        raise SystemExit(0)
    print("--session-id --resume --dangerously-skip-permissions --mcp-config --strict-mcp-config --permission-mode")
    raise SystemExit(0)

if args == ["--dangerously-load-development-channels", "server:longhouse-channel", "--help"]:
    if os.environ.get("FAKE_CLAUDE_CHANNELS_MISSING") == "1":
        print("unknown option --dangerously-load-development-channels", file=sys.stderr)
        raise SystemExit(1)
    if os.environ.get("FAKE_CLAUDE_CHANNELS_UNCONFIRMED") == "1":
        print("--session-id --dangerously-skip-permissions --mcp-config --strict-mcp-config --permission-mode")
        raise SystemExit(0)
    print("--session-id --resume --dangerously-skip-permissions --mcp-config --strict-mcp-config --permission-mode")
    raise SystemExit(0)

print("unexpected fake claude args: " + json.dumps(args), file=sys.stderr)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fake_opencode_server(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        r"""#!/usr/bin/env python3
import base64
import http.server
import json
import os
import signal
import sys
from pathlib import Path
from urllib.parse import urlparse

args = sys.argv[1:]
if args == ["--version"]:
    print("opencode 9.9.9-e2e-fake")
    raise SystemExit(0)

if args == ["attach", "--help"]:
    print("opencode attach <url>")
    print("-s, --session session id")
    print("-p, --password defaults to OPENCODE_SERVER_PASSWORD")
    print("-u, --username defaults to OPENCODE_SERVER_USERNAME")
    raise SystemExit(0)

if not args or args[0] != "serve":
    print("unexpected fake opencode args: " + json.dumps(args), file=sys.stderr)
    raise SystemExit(2)

username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
password = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
provider_session_id = "ses_fake_universal_e2e"
state_path = Path.cwd() / ".fake-opencode-state.json"

def load_messages():
    try:
        payload = json.loads(state_path.read_text())
    except Exception:
        return []
    messages = payload.get("messages")
    return messages if isinstance(messages, list) else []

messages = load_messages()

def save_state():
    state_path.write_text(json.dumps({"messages": messages}))

def make_doc():
    return {
        "openapi": "3.1.0",
        "paths": {
            "/global/health": {"get": {"operationId": "global.health"}},
            "/session": {"post": {"operationId": "session.create"}},
            "/session/{sessionID}": {"get": {"operationId": "session.get"}},
            "/session/{sessionID}/message": {
                "get": {"operationId": "session.messages"},
                "post": {"operationId": "session.prompt"},
            },
            "/session/{sessionID}/prompt_async": {
                "post": {
                    "operationId": "session.prompt_async",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "noReply": {"type": "boolean"},
                                        "parts": {"type": "array"},
                                    },
                                }
                            }
                        }
                    },
                }
            },
            "/session/{sessionID}/abort": {"post": {"operationId": "session.abort"}},
        },
    }

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def _json(self, payload, status=200):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _empty(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authorized(self):
        expected = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        return self.headers.get("Authorization") == expected

    def do_GET(self):
        parsed = urlparse(self.path)
        if not self._authorized():
            self._json({"error": "forbidden"}, 403)
            return
        if self.path == "/global/health":
            self._json({"healthy": True})
            return
        if self.path == "/doc":
            self._json(make_doc())
            return
        if self.path == f"/session/{provider_session_id}":
            self._json({"id": provider_session_id})
            return
        if parsed.path == f"/session/{provider_session_id}/message":
            self._json(messages)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._authorized():
            self._json({"error": "forbidden"}, 403)
            return
        if parsed.path == "/session":
            self._json({
                "id": provider_session_id,
                "cost": 0,
                "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            })
            return
        if parsed.path == f"/session/{provider_session_id}/prompt_async":
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body.decode("utf-8") or "{}")
            if os.environ.get("FAKE_OPENCODE_DROP_PROMPT_ASYNC") != "1":
                messages.append({
                    "info": {"id": "msg_fake_user", "sessionID": provider_session_id, "role": "user"},
                    "parts": payload.get("parts") or [],
                })
                save_state()
            self._empty()
            return
        if parsed.path == f"/session/{provider_session_id}/abort":
            self._json(True)
            return
        self._json({"error": "not found"}, 404)

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
print(f"opencode server listening on http://127.0.0.1:{server.server_address[1]}", flush=True)
server.serve_forever()
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_adapter_registry_loads_all_four_provider_mvp_adapters(tmp_path: Path) -> None:
    registry = uah.adapter_registry(_fake_bins(tmp_path))

    assert tuple(registry) == uah.SUPPORTED_PROVIDERS
    for provider, adapter in registry.items():
        assert adapter.config.provider == provider
        assert set(uah.MVP_METHODS).issubset(set(adapter.config.methods))
        assert set(uah.MVP_CAPABILITIES).issubset(set(adapter.config.capabilities))


def test_probe_identity_runs_for_all_providers_through_shared_scenario(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("probe_identity",),
            evidence_root=tmp_path / "evidence",
            provider_bins=_fake_bins(tmp_path),
        )
    )

    assert payload["verdict"] == "green"
    assert {result["provider"] for result in payload["results"]} == set(uah.SUPPORTED_PROVIDERS)
    assert all(result["scenario"] == "probe_identity" for result in payload["results"])
    assert all(result["status"] == "pass" for result in payload["results"])
    for result in payload["results"]:
        probe = json.loads((Path(result["evidence_root"]) / "assertions" / "probe.json").read_text(encoding="utf-8"))
        assert probe["declared_capabilities"]
        assert probe["mvp_methods"] == list(uah.MVP_METHODS)
        assert probe["version"]


def test_adapter_registry_uses_concrete_provider_adapters(tmp_path: Path) -> None:
    registry = uah.adapter_registry(_fake_bins(tmp_path))

    assert set(registry) == set(uah.SUPPORTED_PROVIDERS)
    for provider, expected_class in EXPECTED_ADAPTER_CLASS_BY_PROVIDER.items():
        adapter = registry[provider]
        assert type(adapter).__name__ == expected_class
        assert adapter.config.provider == provider
        assert "action_result" in adapter.config.methods


def test_action_matrix_emits_same_longhouse_actions_for_all_providers(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("action_matrix",),
            evidence_root=tmp_path / "evidence",
            provider_bins=_fake_bins(tmp_path),
        )
    )

    assert payload["verdict"] == "yellow"
    assert {result["provider"] for result in payload["results"]} == set(uah.SUPPORTED_PROVIDERS)
    for result in payload["results"]:
        assert result["scenario"] == "action_matrix"
        assert result["status"] == "blocked"
        assert result["data"]["action_ids"] == list(uah.ACTIONS)
        assert result["data"]["action_count"] == len(uah.ACTIONS)
        actions = {row["action_id"]: row for row in result["data"]["actions"]}
        assert set(actions) == set(uah.ACTIONS)
        expected_class = EXPECTED_ADAPTER_CLASS_BY_PROVIDER[result["provider"]]
        assert {row["adapter_class"] for row in actions.values()} == {expected_class}
        assert {row["adapter_method"] for row in actions.values()} == {"action_result"}
        assert all(row["implementation_kind"] for row in actions.values())
        assert actions["send_message"]["category"] == "control"
        assert actions["steer_active_turn"]["category"] == "control"
        assert actions["pause_request_detect"]["category"] == "observe"
        assert actions["answer_pause_request"]["category"] == "control"
        assert actions["interrupt_cancel"]["contract_operation"] == "interrupt"
        assert actions["raw_evidence_capture"]["status"] == "pass"
        assert actions["parse_normalize"]["status"] == "pass"
        assert actions["db_ingest"]["status"] == "pass"
        assert actions["db_ingest"]["canary"] == "universal_db_ingest_project"
        assert actions["baseline_compare"]["status"] == "pass"
        assert actions["baseline_compare"]["canary"] == "provider_release_proof_baseline_diff"
        assert actions["old_new_release_diff"]["status"] == "pass"
        assert actions["old_new_release_diff"]["evidence_level"] == "artifact_diff"
        assert actions["old_new_release_diff"]["canary"] == "provider_release_proof_old_new_diff"
        assert Path(result["data"]["action_matrix_path"]).is_file()


def test_action_matrix_marks_provider_specific_unsupported_actions(tmp_path: Path) -> None:
    bins = _fake_bins(tmp_path)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("opencode", "antigravity"),
            scenarios=("action_matrix",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"opencode": bins["opencode"], "antigravity": bins["antigravity"]},
        )
    )

    by_provider = {
        result["provider"]: {row["action_id"]: row for row in result["data"]["actions"]}
        for result in payload["results"]
    }
    assert by_provider["opencode"]["steer_active_turn"]["status"] == "unsupported_gap"
    assert by_provider["opencode"]["answer_pause_request"]["status"] == "unsupported_gap"
    assert by_provider["antigravity"]["launch_remote"]["status"] == "unsupported_gap"
    assert by_provider["antigravity"]["interrupt_cancel"]["status"] == "unsupported_gap"
    assert by_provider["antigravity"]["send_message"]["status"] == "pass"
    assert by_provider["antigravity"]["send_message"]["evidence_level"] == "live_token"


def test_control_surface_emits_same_control_actions_for_all_providers(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("control_surface",),
            evidence_root=tmp_path / "evidence",
            provider_bins=_fake_bins(tmp_path),
        )
    )

    assert payload["verdict"] == "yellow"
    assert {result["provider"] for result in payload["results"]} == set(uah.SUPPORTED_PROVIDERS)
    for result in payload["results"]:
        assert result["scenario"] == "control_surface"
        assert result["status"] == "blocked"
        assert result["data"]["action_ids"] == list(uah.CONTROL_SURFACE_ACTION_IDS)
        assert result["data"]["action_count"] == len(uah.CONTROL_SURFACE_ACTION_IDS)
        assert Path(result["data"]["control_surface_path"]).is_file()
        actions = {row["action_id"]: row for row in result["data"]["actions"]}
        assert set(actions) == set(uah.CONTROL_SURFACE_ACTION_IDS)
        assert actions["send_message"]["category"] == "control"
        assert actions["tail_output"]["category"] == "observe"
        assert actions["tool_call_result"]["status"] == "pass"
        assert "baseline_compare" not in actions
        assert "db_ingest" not in actions


def test_control_surface_keeps_unsupported_and_live_token_rows_explicit(tmp_path: Path) -> None:
    bins = _fake_bins(tmp_path)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("opencode", "antigravity"),
            scenarios=("control_surface",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"opencode": bins["opencode"], "antigravity": bins["antigravity"]},
        )
    )

    by_provider = {
        result["provider"]: {row["action_id"]: row for row in result["data"]["actions"]}
        for result in payload["results"]
    }
    assert by_provider["opencode"]["steer_active_turn"]["status"] == "unsupported_gap"
    assert by_provider["opencode"]["resume_reattach"]["status"] == "pass"
    assert by_provider["opencode"]["resume_reattach"]["evidence_level"] == "live_no_token"
    assert by_provider["antigravity"]["interrupt_cancel"]["status"] == "unsupported_gap"
    assert by_provider["antigravity"]["send_message"]["status"] == "pass"
    assert by_provider["antigravity"]["send_message"]["required_evidence"] == "hermetic"
    assert by_provider["antigravity"]["send_message"]["evidence_level"] == "live_token"


def test_db_ingest_project_uses_real_longhouse_sqlite_for_all_providers(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("db_ingest_project",),
            evidence_root=tmp_path / "evidence",
        )
    )

    assert payload["verdict"] == "green"
    assert {result["provider"] for result in payload["results"]} == set(uah.SUPPORTED_PROVIDERS)
    for result in payload["results"]:
        assert result["status"] == "pass"
        assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"
        evidence_root = Path(result["evidence_root"])
        db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
        assert Path(db_snapshot["db_path"]).is_file()
        assert db_snapshot["ingest_result"]["events_inserted"] == 4
        assert db_snapshot["session_counts"]["user_messages"] == 1
        assert db_snapshot["session_counts"]["assistant_messages"] == 1
        assert db_snapshot["session_counts"]["tool_calls"] == 1
        assert db_snapshot["timeline"]["matched"] is True
        assert "universal db ingest hello" in db_snapshot["export_jsonl"]


def test_codex_run_prompt_once_writes_safe_projection(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("run_prompt_once",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": _fake_bins(tmp_path)["codex"]},
            prompt="hello",
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    evidence_root = Path(result["evidence_root"])
    assert (evidence_root / "input" / "prompt.txt").read_text(encoding="utf-8") == "hello"
    assert (evidence_root / "assertions" / "run_prompt.json").is_file()
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["has_user"] is True
    assert session["has_assistant"] is True
    assert session["operation_statuses"]["run_once"]["status"] == "pass"


def test_unsafe_run_prompt_once_is_typed_unsupported_gap(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude",),
            scenarios=("run_prompt_once",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"claude": _fake_bins(tmp_path)["claude"]},
            prompt="hello",
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "yellow"
    assert result["status"] == "unsupported_gap"
    assert result["failure_code"] == "run_prompt_once_not_safe_no_token"
    evidence_root = Path(result["evidence_root"])
    assert (evidence_root / "input" / "prompt.txt").read_text(encoding="utf-8") == "hello"
    assert (evidence_root / "assertions" / "run_prompt.json").is_file()


def test_managed_session_scenarios_pass_for_codex_and_opencode(tmp_path: Path) -> None:
    bins = _fake_bins(tmp_path)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex", "opencode"),
            scenarios=("launch_managed_session", "send_receive"),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": bins["codex"], "opencode": bins["opencode"]},
            prompt="ping",
        )
    )

    assert payload["verdict"] == "green"
    assert len(payload["results"]) == 4
    assert all(result["status"] == "pass" for result in payload["results"])
    for result in payload["results"]:
        evidence_root = Path(result["evidence_root"])
        session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
        assert session["provider"] == result["provider"]
        assert session["provider_session_id"].startswith(f"universal-{result['provider']}-")
        if result["scenario"] == "send_receive":
            assert session["has_user"] is True
            assert session["has_assistant"] is True
            assert session["operation_statuses"]["send_input"]["status"] == "pass"
        else:
            assert session["operation_statuses"]["launch_local"]["level"] == "live_no_token"


def test_managed_session_scenarios_are_typed_gaps_for_other_providers(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude", "antigravity"),
            scenarios=("launch_managed_session", "send_receive"),
            evidence_root=tmp_path / "evidence",
            provider_bins={
                "claude": _fake_bins(tmp_path)["claude"],
                "antigravity": _fake_bins(tmp_path)["antigravity"],
            },
            prompt="ping",
        )
    )

    assert payload["verdict"] == "yellow"
    assert len(payload["results"]) == 4
    assert {result["status"] for result in payload["results"]} == {"unsupported_gap"}
    assert {result["failure_code"] for result in payload["results"]} == {
        "managed_session_not_safe_no_token",
        "send_receive_not_safe_no_token",
    }


def test_opencode_managed_session_e2e_uses_real_provider_live_canary(tmp_path: Path) -> None:
    fake_opencode = _fake_opencode_server(tmp_path / "bin" / "opencode")
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("opencode",),
            scenarios=("managed_session_e2e",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"opencode": fake_opencode},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["source_artifact_kind"] == "provider_live_canary"
    assert result["data"]["synthetic"] is False
    assert result["data"]["longhouse_ingest"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    provider_live = json.loads((evidence_root / "raw" / "provider-live-canary.json").read_text(encoding="utf-8"))
    assert provider_live["verdict"] == "green"
    assert provider_live["canaries"]["prompt_async_no_reply_delivery"]["status"] == "pass"
    assert (evidence_root / "raw" / "provider-live-evidence" / "opencode-server.log").is_file()
    assert (evidence_root / "raw" / "provider-live-evidence" / "opencode-doc-paths.json").is_file()

    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    canonical_events = (evidence_root / "events" / "canonical-longhouse-events.jsonl").read_text(encoding="utf-8")
    assert "provider_live_canary" in raw_events
    assert '"synthetic": true' not in raw_events
    assert "prompt_async_no_reply_delivery" in canonical_events

    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["provider_session_id"] == "ses_fake_universal_e2e"
    assert session["longhouse_session_id"]
    assert session["operation_statuses"]["send_input"]["level"] == "live_no_token"
    assert session["operation_statuses"]["transcript_binding"]["canary"] == "opencode_prompt_async_no_reply_delivery"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 4
    assert db_snapshot["timeline"]["matched"] is True


def test_claude_managed_session_e2e_uses_provider_live_contract_canary(tmp_path: Path) -> None:
    fake_claude = _fake_claude_provider_live(tmp_path / "bin" / "claude")
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude",),
            scenarios=("managed_session_e2e",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"claude": fake_claude},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["source_artifact_kind"] == "provider_live_canary"
    assert result["data"]["synthetic"] is False
    assert result["data"]["longhouse_ingest"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["launch_local"]["level"] == "live_no_token"
    assert result["data"]["operation_evidence"]["external_event_channel"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["runtime_phase"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["send_input"]["status"] == "blocked"
    assert result["data"]["operation_evidence"]["send_input"]["level"] == "live_token_required"
    assert result["data"]["operation_evidence"]["steer_active_turn"]["status"] == "blocked"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    provider_live = json.loads((evidence_root / "raw" / "provider-live-canary.json").read_text(encoding="utf-8"))
    assert provider_live["verdict"] == "green"
    assert provider_live["canaries"]["command_shape"]["status"] == "pass"
    assert provider_live["canaries"]["channels_shape"]["status"] == "pass"
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    canonical_events = (evidence_root / "events" / "canonical-longhouse-events.jsonl").read_text(encoding="utf-8")
    assert "provider_live_canary" in raw_events
    assert "channels_shape" in canonical_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["provider"] == "claude"
    assert session["operation_statuses"]["send_input"]["failure_code"] == "claude_live_token_contract_not_run"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 4
    assert db_snapshot["timeline"]["matched"] is True


def test_claude_managed_session_e2e_fails_when_channel_contract_breaks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_CHANNELS_MISSING", "1")
    fake_claude = _fake_claude_provider_live(tmp_path / "bin" / "claude")
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude",),
            scenarios=("managed_session_e2e",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"claude": fake_claude},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "red"
    assert result["status"] == "fail"
    assert result["failure_code"] == "claude_development_channels_contract_missing"
    assert result["data"]["operation_evidence"]["external_event_channel"]["status"] == "fail"


def test_codex_managed_session_e2e_uses_provider_release_canary(tmp_path: Path, monkeypatch) -> None:
    from zerg.qa import codex_provider_release_canary

    calls: list[dict[str, object]] = []

    def fake_canary(args: dict[str, object]) -> dict[str, object]:
        calls.append(args)
        return {
            "artifact_kind": "provider_release_canary",
            "provider": "codex",
            "provider_version": "codex 9.9.9-e2e",
            "verdict": "green",
            "failure_code": None,
            "canaries": {
                "managed_tui_attach": {
                    "status": "pass",
                    "thread_id": "thread_codex_universal_e2e",
                    "state_file": "/tmp/codex-state.json",
                },
                "detached_ui": {
                    "status": "pass",
                    "thread_id": "thread_codex_universal_e2e",
                    "ipc_socket": "/tmp/codex-state.sock",
                },
            },
            "operation_evidence": {
                "launch_local": {
                    "status": "pass",
                    "level": "live_no_token",
                    "canary": "managed_tui_attach",
                },
                "launch_remote": {
                    "status": "pass",
                    "level": "live_no_token",
                    "canary": "detached_ui",
                },
                "reattach": {
                    "status": "pass",
                    "level": "live_no_token",
                    "canary": "managed_tui_attach",
                },
            },
        }

    monkeypatch.setattr(codex_provider_release_canary, "run_codex_provider_release_canary", fake_canary)
    fake_codex = _fake_bins(tmp_path)["codex"]
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("managed_session_e2e",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": fake_codex},
        )
    )

    assert calls
    assert calls[0]["codex_bin"] == str(fake_codex)
    assert calls[0]["run_managed_tui_attach"] is True
    assert calls[0]["run_detached_ui"] is True
    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["source_artifact_kind"] == "provider_release_canary"
    assert result["data"]["synthetic"] is False
    assert result["data"]["longhouse_ingest"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["launch_local"]["canary"] == "managed_tui_attach"
    assert result["data"]["operation_evidence"]["launch_remote"]["canary"] == "detached_ui"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    assert (evidence_root / "raw" / "codex-provider-release-canary.json").is_file()
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    assert "codex_provider_release_canary" in raw_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["provider_session_id"] == "thread_codex_universal_e2e"
    assert session["operation_statuses"]["launch_local"]["level"] == "live_no_token"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 2
    assert db_snapshot["timeline"]["matched"] is True


def test_codex_interrupt_cancel_uses_managed_live_interrupt_canary(tmp_path: Path, monkeypatch) -> None:
    from zerg.qa import codex_provider_release_canary

    calls: list[dict[str, object]] = []

    def fake_canary(args: dict[str, object]) -> dict[str, object]:
        calls.append(args)
        return {
            "artifact_kind": "codex_provider_release_canary",
            "provider": "codex",
            "codex_version": "codex-cli 9.9.9",
            "verdict": "green",
            "failure_code": None,
            "canaries": {
                "managed_live_interrupt": {
                    "status": "pass",
                    "thread_id": "codex-thread-interrupt",
                    "marker": "LONGHOUSE_CODEX_INTERRUPT_CANARY_fake",
                    "last_turn_status": "interrupted",
                    "state_file": str(tmp_path / "state.json"),
                }
            },
            "operation_evidence": {
                "interrupt": {
                    "status": "pass",
                    "level": "live_token",
                    "canary": "managed_live_interrupt",
                }
            },
        }

    monkeypatch.setattr(codex_provider_release_canary, "run_codex_provider_release_canary", fake_canary)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("interrupt_cancel",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": _fake_bins(tmp_path)["codex"]},
        )
    )

    assert calls
    assert calls[0]["run_managed_live_interrupt"] is True
    assert calls[0]["source_review_status"] == "pass"
    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["scenario"] == "interrupt_cancel"
    assert result["data"]["operation_evidence"]["interrupt"]["level"] == "live_token"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    assert "managed_live_interrupt" in raw_events
    assert "interrupted" in raw_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["provider_session_id"] == "codex-thread-interrupt"
    assert session["operation_statuses"]["interrupt"]["status"] == "pass"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 2
    assert db_snapshot["timeline"]["matched"] is True


def test_codex_interrupt_cancel_reports_runtime_host_credentials_gap(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("interrupt_cancel",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": _fake_bins(tmp_path)["codex"]},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "yellow"
    assert result["status"] == "unsupported_gap"
    assert result["failure_code"] == "codex_managed_bridge_credentials_missing"
    assert result["data"]["missing"] == ["--agents-token", "--api-url"]
    assert result["data"]["operation_evidence"]["interrupt"]["level"] == "live_token_required"


def test_codex_tool_call_result_uses_real_tool_canary(tmp_path: Path, monkeypatch) -> None:
    from zerg.qa import codex_provider_release_canary

    calls: list[dict[str, object]] = []

    def fake_canary(args: dict[str, object]) -> dict[str, object]:
        calls.append(args)
        return {
            "artifact_kind": "codex_provider_release_canary",
            "provider": "codex",
            "codex_version": "codex-cli 9.9.9",
            "verdict": "green",
            "failure_code": None,
            "canaries": {
                "codex_real_tool_result_shape": {
                    "status": "pass",
                    "marker": "LONGHOUSE_CODEX_REAL_TOOL_fake",
                    "command": "printf 'LONGHOUSE_CODEX_REAL_TOOL_fake\\n'",
                    "command_status": "completed",
                    "command_exit_code": 0,
                    "command_exact_match": True,
                    "output_exact_match": True,
                    "matching_command_event": {
                        "id": "call_fake_tool",
                        "type": "command_execution",
                        "status": "completed",
                        "exit_code": 0,
                        "command": "printf 'LONGHOUSE_CODEX_REAL_TOOL_fake\\n'",
                        "aggregated_output": "LONGHOUSE_CODEX_REAL_TOOL_fake\n",
                    },
                    "done_text_event": {"type": "agent_message", "text": "DONE"},
                }
            },
            "operation_evidence": {
                "run_once": {
                    "status": "pass",
                    "level": "live_token",
                    "canary": "codex_real_tool_result_shape",
                },
                "transcript_binding": {
                    "status": "pass",
                    "level": "live_token",
                    "canary": "codex_real_tool_result_shape",
                },
            },
        }

    monkeypatch.setattr(codex_provider_release_canary, "run_codex_provider_release_canary", fake_canary)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("tool_call_result",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": _fake_bins(tmp_path)["codex"]},
        )
    )

    assert calls
    assert calls[0]["run_real_tool"] is True
    assert calls[0]["source_review_status"] == "pass"
    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["scenario"] == "tool_call_result"
    assert result["data"]["operation_evidence"]["tool_call_result"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["tool_call_result"]["level"] == "live_token"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    assert "call_fake_tool" in raw_events
    assert "LONGHOUSE_CODEX_REAL_TOOL_fake" in raw_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["operation_statuses"]["tool_call_result"]["status"] == "pass"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 3
    assert db_snapshot["session_counts"]["tool_calls"] == 1
    assert db_snapshot["timeline"]["matched"] is True


def test_tool_call_result_is_typed_gap_for_unmigrated_providers(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude", "opencode", "antigravity"),
            scenarios=("tool_call_result",),
            evidence_root=tmp_path / "evidence",
            provider_bins=_fake_bins(tmp_path),
        )
    )

    assert payload["verdict"] == "yellow"
    assert {result["provider"] for result in payload["results"]} == {"claude", "opencode", "antigravity"}
    assert {result["status"] for result in payload["results"]} == {"unsupported_gap"}
    assert {result["failure_code"] for result in payload["results"]} == {"tool_call_result_adapter_missing"}


def test_codex_managed_session_e2e_reports_credentials_gap(tmp_path: Path, monkeypatch) -> None:
    from zerg.qa import codex_provider_release_canary

    def fake_canary(_args: dict[str, object]) -> dict[str, object]:
        return {
            "artifact_kind": "provider_release_canary",
            "provider": "codex",
            "provider_version": "codex 9.9.9-e2e",
            "verdict": "yellow",
            "failure_code": "insufficient_coverage",
            "canaries": {
                "managed_tui_attach": {
                    "status": "not_run",
                    "failure_code": "managed_bridge_credentials_missing",
                    "missing": ["--api-url", "--agents-token"],
                },
                "detached_ui": {
                    "status": "not_run",
                    "failure_code": "managed_bridge_credentials_missing",
                    "missing": ["--api-url", "--agents-token"],
                },
            },
            "operation_evidence": {
                "launch_local": {
                    "status": "not_run",
                    "level": "none",
                    "canary": "managed_tui_attach",
                    "failure_code": "managed_bridge_credentials_missing",
                },
                "launch_remote": {
                    "status": "not_run",
                    "level": "none",
                    "canary": "detached_ui",
                    "failure_code": "managed_bridge_credentials_missing",
                },
            },
        }

    monkeypatch.setattr(codex_provider_release_canary, "run_codex_provider_release_canary", fake_canary)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("managed_session_e2e",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": _fake_bins(tmp_path)["codex"]},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "yellow"
    assert result["status"] == "unsupported_gap"
    assert result["failure_code"] == "codex_managed_bridge_credentials_missing"
    assert result["data"]["missing"] == ["--agents-token", "--api-url"]


def test_antigravity_managed_session_e2e_uses_hook_inbox_canary(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_control_canary(*, provider: str, artifact_path: Path, evidence_root: Path) -> dict[str, object]:
        calls.append(
            {
                "provider": provider,
                "artifact_path": artifact_path,
                "evidence_root": evidence_root,
            }
        )
        return {
            "schema_version": 1,
            "provider": "antigravity",
            "verdict": "green",
            "failure_code": None,
            "canaries": {
                "antigravity": {
                    "status": "pass",
                    "session_id": "antigravity-canary-session",
                    "pre_injection": {"injectSteps": [{"userMessage": "pre invocation canary input"}]},
                    "post_injection": {
                        "injectSteps": [{"userMessage": "post invocation canary input"}],
                        "terminationBehavior": "force_continue",
                    },
                    "stop_decision": {"decision": "continue"},
                }
            },
        }

    monkeypatch.setattr(uah, "run_provider_control_e2e_canary", fake_control_canary)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("antigravity",),
            scenarios=("managed_session_e2e",),
            evidence_root=tmp_path / "evidence",
        )
    )

    assert calls
    assert calls[0]["provider"] == "antigravity"
    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["source_artifact_kind"] == "provider_control_e2e_canary"
    assert result["data"]["synthetic"] is False
    assert result["data"]["operation_evidence"]["external_event_channel"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["send_input"]["level"] == "hermetic"
    assert result["data"]["operation_evidence"]["runtime_phase"]["canary"] == (
        "provider_control_e2e_antigravity_hook_inbox"
    )
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    assert (evidence_root / "raw" / "provider-control-e2e.json").is_file()
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    assert "provider_control_e2e_canary" in raw_events
    assert "force_continue" in raw_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["provider_session_id"] == "antigravity-canary-session"
    assert session["operation_statuses"]["external_event_channel"]["status"] == "pass"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 4
    assert db_snapshot["timeline"]["matched"] is True


def test_antigravity_managed_session_e2e_fails_when_hook_inbox_canary_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_control_canary(*, provider: str, artifact_path: Path, evidence_root: Path) -> dict[str, object]:
        return {
            "schema_version": 1,
            "provider": provider,
            "verdict": "red",
            "failure_code": "antigravity_post_claim_failed",
            "canaries": {
                "antigravity": {
                    "status": "fail",
                    "failure_code": "antigravity_post_claim_failed",
                    "session_id": "antigravity-canary-session",
                }
            },
        }

    monkeypatch.setattr(uah, "run_provider_control_e2e_canary", fake_control_canary)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("antigravity",),
            scenarios=("managed_session_e2e",),
            evidence_root=tmp_path / "evidence",
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "red"
    assert result["status"] == "fail"
    assert result["failure_code"] == "antigravity_post_claim_failed"
    assert result["data"]["operation_evidence"]["external_event_channel"]["status"] == "fail"


def test_opencode_managed_session_e2e_fails_when_real_canary_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_OPENCODE_DROP_PROMPT_ASYNC", "1")
    fake_opencode = _fake_opencode_server(tmp_path / "bin" / "opencode")
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("opencode",),
            scenarios=("managed_session_e2e",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"opencode": fake_opencode},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "red"
    assert result["status"] == "fail"
    assert result["failure_code"] == "opencode_prompt_async_delivery_not_observed"
    evidence_root = Path(result["evidence_root"])
    provider_live = json.loads((evidence_root / "raw" / "provider-live-canary.json").read_text(encoding="utf-8"))
    assert provider_live["verdict"] == "red"
    assert provider_live["operation_evidence"]["send_input"]["status"] == "fail"


def test_collect_raw_evidence_runs_for_all_providers_without_launching(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("collect_raw_evidence",),
            evidence_root=tmp_path / "evidence",
        )
    )

    assert payload["verdict"] == "green"
    assert len(payload["results"]) == len(uah.SUPPORTED_PROVIDERS)
    for result in payload["results"]:
        assert result["status"] == "pass"
        evidence_root = Path(result["evidence_root"])
        assert (evidence_root / "manifest.json").is_file()
        assert (evidence_root / "assertions" / "collect_raw_evidence.json").is_file()


def test_probe_failure_writes_raw_and_assertion_evidence(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "codex"
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("probe_identity",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": missing},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "red"
    assert result["status"] == "fail"
    assert result["failure_code"] == "provider_binary_not_found"
    evidence_root = Path(result["evidence_root"])
    assert (evidence_root / "manifest.json").is_file()
    assert (evidence_root / "raw" / "version-command.json").is_file()
    assert (evidence_root / "assertions" / "probe.json").is_file()


def test_parse_ingest_project_replays_fixture_without_launching_provider(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.jsonl"
    fixture.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "text": "hello"}),
                json.dumps({"type": "assistant", "text": "world"}),
                json.dumps({"type": "unknown", "payload": {"new": True}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("opencode",),
            scenarios=("parse_ingest_project",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"opencode": tmp_path / "not-used"},
            fixture_path=fixture,
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    evidence_root = Path(result["evidence_root"])
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    timeline = json.loads((evidence_root / "longhouse" / "timeline-projection.json").read_text(encoding="utf-8"))
    unknown = (evidence_root / "events" / "unknown-provider-events.jsonl").read_text(encoding="utf-8")
    assert session["has_user"] is True
    assert session["has_assistant"] is True
    assert timeline["event_count"] == 3
    assert '"type": "unknown"' in unknown


def test_scenario_runner_does_not_branch_on_provider_names() -> None:
    sources = "\n".join(
        inspect.getsource(item)
        for item in (
            uah.run_scenario,
            uah.run_probe_identity,
            uah.run_collect_raw_evidence,
            uah.run_parse_ingest_project,
            uah.run_prompt_once,
            uah.run_launch_managed_session,
            uah.run_send_receive,
            uah.run_managed_session_e2e,
        )
    )

    for provider in uah.SUPPORTED_PROVIDERS:
        assert provider not in sources


def test_script_entrypoint_emits_normalized_artifact(tmp_path: Path) -> None:
    fake_bin = _fake_bins(tmp_path)["claude"]
    artifact_root = tmp_path / "cli-evidence"

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "qa" / "universal-agent-harness.py"),
            "--provider",
            "claude",
            "--scenario",
            "probe_identity",
            "--provider-bin",
            str(fake_bin),
            "--evidence-root",
            str(artifact_root),
            "--json",
        ],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["artifact_kind"] == uah.ARTIFACT_KIND
    assert payload["verdict"] == "green"
    assert (artifact_root / "universal-agent-harness.json").is_file()


def test_script_entrypoint_runs_all_provider_action_e2e(tmp_path: Path) -> None:
    fake_bins = _fake_bins(tmp_path)
    artifact_root = tmp_path / "all-provider-cli-evidence"
    provider_bin_args = [f"{provider}={path}" for provider, path in fake_bins.items()]

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "qa" / "universal-agent-harness.py"),
            "--scenario",
            "action_matrix",
            "--scenario",
            "control_surface",
            "--evidence-root",
            str(artifact_root),
            "--json",
            *[item for provider_bin in provider_bin_args for item in ("--provider-bin", provider_bin)],
        ],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["artifact_kind"] == uah.ARTIFACT_KIND
    assert payload["providers"] == list(uah.SUPPORTED_PROVIDERS)
    assert payload["scenarios"] == ["action_matrix", "control_surface"]
    assert payload["verdict"] == "yellow"
    assert len(payload["results"]) == len(uah.SUPPORTED_PROVIDERS) * 2
    assert (artifact_root / "universal-agent-harness.json").is_file()

    by_provider_scenario = {(item["provider"], item["scenario"]): item for item in payload["results"]}
    for provider in uah.SUPPORTED_PROVIDERS:
        action_matrix = by_provider_scenario[(provider, "action_matrix")]
        control_surface = by_provider_scenario[(provider, "control_surface")]
        assert action_matrix["status"] == "blocked"
        assert control_surface["status"] == "blocked"
        assert action_matrix["data"]["action_ids"] == list(uah.ACTIONS)
        assert control_surface["data"]["action_ids"] == list(uah.CONTROL_SURFACE_ACTION_IDS)
        assert set(control_surface["data"]["action_ids"]).issubset(set(action_matrix["data"]["action_ids"]))
        assert Path(action_matrix["data"]["action_matrix_path"]).is_file()
        assert Path(control_surface["data"]["control_surface_path"]).is_file()
        matrix_actions = {row["action_id"]: row for row in action_matrix["data"]["actions"]}
        surface_actions = {row["action_id"]: row for row in control_surface["data"]["actions"]}
        assert matrix_actions["raw_evidence_capture"]["status"] == "pass"
        assert matrix_actions["baseline_compare"]["status"] == "pass"
        assert matrix_actions["old_new_release_diff"]["status"] == "pass"
        assert matrix_actions["old_new_release_diff"]["evidence_level"] == "artifact_diff"
        assert surface_actions["send_message"]["category"] == "control"
        assert surface_actions["tail_output"]["category"] == "observe"
        assert "old_new_release_diff" not in surface_actions
