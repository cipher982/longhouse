"""Unit coverage for scripts/ops/remote-launch-smoke.py.

The live workflow owns the end-to-end assertion. These tests pin the parts of
the harness that can regress quietly: exact build matching, machine selection,
and requiring an assistant-role event rather than a user echo.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from types import SimpleNamespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ops" / "remote-launch-smoke.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location("remote_launch_smoke", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


smoke = _load_smoke_module()


def test_commit_match_accepts_full_or_short_sha() -> None:
    full = "ca73cc1f0123456789abcdef0123456789abcdef"

    assert smoke._commit_matches(full, full)
    assert smoke._commit_matches(full, "ca73cc1f")
    assert smoke._commit_matches(full, "live")
    assert smoke._commit_matches(full, "any")
    assert not smoke._commit_matches("ca73cc1f", full)
    assert not smoke._commit_matches(full, "40a262b2")


def test_assistant_events_contain_ignores_user_echo() -> None:
    payload = {
        "recent_events": [
            {"role": "user", "text": "LH_REMOTE_LAUNCH_SMOKE_123"},
            {"role": "tool", "text": "LH_REMOTE_LAUNCH_SMOKE_123"},
        ]
    }

    assert not smoke.assistant_events_contain(payload, "LH_REMOTE_LAUNCH_SMOKE_123")

    payload["recent_events"].insert(0, {"role": "assistant", "text": "LH_REMOTE_LAUNCH_SMOKE_123"})
    assert smoke.assistant_events_contain(payload, "LH_REMOTE_LAUNCH_SMOKE_123")


def test_discover_machine_picks_online_codex_capable_machine(monkeypatch) -> None:
    def fake_http_json(method, url, **kwargs):
        assert method == "GET"
        assert url == "https://demo.longhouse.ai/api/timeline/machines"
        return smoke.HttpResult(
            200,
            "{}",
            {
                "machines": [
                    {
                        "device_id": "offline",
                        "online": False,
                        "supports": ["codex.launch"],
                        "can_launch_codex": True,
                        "launch": {"providers": [], "blocked_by": "control_down"},
                    },
                    {
                        "device_id": "claude-only",
                        "online": True,
                        "supports": ["claude.launch"],
                        "can_launch_codex": False,
                        "launch": {
                            "providers": [{"provider": "claude", "execution_lifetimes": ["live_control"]}],
                            "blocked_by": None,
                        },
                    },
                    {
                        "device_id": "demo-machine",
                        "machine_name": "demo-machine",
                        "online": True,
                        "supports": ["codex.launch"],
                        "can_launch_codex": True,
                        "launch": {
                            "providers": [{"provider": "codex", "execution_lifetimes": ["live_control"]}],
                            "blocked_by": None,
                        },
                    },
                ]
            },
        )

    monkeypatch.setattr(smoke, "_http_json", fake_http_json)

    machine = smoke.discover_machine("https://demo.longhouse.ai", "cookie")

    assert machine["device_id"] == "demo-machine"


def test_discover_machine_can_require_run_once_capability(monkeypatch) -> None:
    def fake_http_json(method, url, **kwargs):
        return smoke.HttpResult(
            200,
            "{}",
            {
                "machines": [
                    {
                        "device_id": "live-only",
                        "online": True,
                        "supports": ["codex.launch"],
                        "can_launch_codex": True,
                        "launch": {
                            "providers": [{"provider": "codex", "execution_lifetimes": ["live_control"]}],
                            "blocked_by": None,
                        },
                    },
                    {
                        "device_id": "run-once",
                        "online": True,
                        "supports": ["codex.run_once"],
                        "can_launch_codex": False,
                        "launch": {
                            "providers": [{"provider": "codex", "execution_lifetimes": ["one_shot"]}],
                            "blocked_by": None,
                        },
                    },
                ]
            },
        )

    monkeypatch.setattr(smoke, "_http_json", fake_http_json)

    machine = smoke.discover_machine(
        "https://demo.longhouse.ai",
        "cookie",
        required_capability="codex.run_once",
    )

    assert machine["device_id"] == "run-once"


def test_parse_last_json_line_skips_logs() -> None:
    parsed = smoke._parse_last_json_line("startup log\nnot json\n{\"ok\": true}\n")

    assert parsed == {"ok": True}


def test_http_json_uses_browser_like_user_agent(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        captured["user_agent"] = request.get_header("User-agent")
        captured["accept_language"] = request.get_header("Accept-language")
        return FakeResponse()

    monkeypatch.setattr(smoke, "urlopen", fake_urlopen)

    result = smoke._http_json("GET", "https://demo.longhouse.ai/api/health")

    assert result.status == 200
    assert "Mozilla/5.0" in captured["user_agent"]
    assert captured["accept_language"] == "en-US,en;q=0.9"


def test_http_json_can_send_device_token_bearer(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["cookie"] = request.get_header("Cookie")
        return FakeResponse()

    monkeypatch.setattr(smoke, "urlopen", fake_urlopen)

    result = smoke._http_json(
        "GET",
        "https://demo.longhouse.ai/api/timeline/machines",
        bearer_token="zdt_test",
    )

    assert result.status == 200
    assert captured["authorization"] == "Bearer zdt_test"
    assert captured["cookie"] is None


def test_remote_python_ssh_accepts_new_host_keys(monkeypatch) -> None:
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return smoke.subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(smoke.subprocess, "run", fake_run)

    smoke._run_remote_python("zerg", container="longhouse-demo", script="print('ok')")

    assert captured["command"][:5] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]


def test_mint_device_token_redacts_plain_token_on_failure(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return smoke.subprocess.CompletedProcess(
            args,
            1,
            stdout='{"token_id": "token-1", "token": "zdt_SECRET_VALUE"}\n',
            stderr="",
        )

    monkeypatch.setattr(smoke, "_run_remote_python", fake_run)

    try:
        smoke.mint_device_token(
            ssh_target="zerg",
            container="longhouse-demo",
            device_id="remote-launch-smoke-unit",
        )
    except smoke.SmokeError as exc:
        message = str(exc)
        assert "zdt_SECRET_VALUE" not in message
        assert "zdt_[redacted]" in message
    else:
        raise AssertionError("mint_device_token should fail when remote command exits nonzero")


def test_launch_session_rejects_non_live_state(monkeypatch) -> None:
    def fake_http_json(method, url, **kwargs):
        assert method == "POST"
        return smoke.HttpResult(
            200,
            "{}",
            {
                "session_id": "sess-1",
                "launch_state": "launching_unknown",
                "launch_error_code": None,
                "launch_error_message": "transport timed out",
            },
        )

    monkeypatch.setattr(smoke, "_http_json", fake_http_json)

    try:
        smoke.launch_session(
            "https://demo.longhouse.ai",
            "cookie",
            device_id="demo-machine",
            cwd="/Users/example/git/zerg/longhouse",
            project="zerg",
            display_name="smoke",
            client_request_id="rid",
            execution_lifetime="live_control",
        )
    except smoke.SmokeError as exc:
        assert "launching_unknown" in str(exc)
    else:
        raise AssertionError("launch_session should reject launching_unknown")


def test_launch_session_sends_one_shot_prompt_payload(monkeypatch) -> None:
    captured = {}

    def fake_http_json(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["body"] = kwargs["body"]
        return smoke.HttpResult(
            200,
            "{}",
            {
                "session_id": "sess-1",
                "launch_state": "live",
                "execution_lifetime": "one_shot",
            },
        )

    monkeypatch.setattr(smoke, "_http_json", fake_http_json)

    result = smoke.launch_session(
        "https://demo.longhouse.ai",
        "cookie",
        device_id="demo-machine",
        cwd="/Users/example/git/zerg/longhouse",
        project="zerg",
        display_name="smoke",
        client_request_id="rid",
        execution_lifetime="one_shot",
        initial_prompt="reply with TOKEN",
    )

    assert result["session_id"] == "sess-1"
    assert captured["body"]["execution_lifetime"] == "one_shot"
    assert captured["body"]["initial_prompt"] == "reply with TOKEN"


def test_launch_session_sends_explicit_live_control_payload(monkeypatch) -> None:
    captured = {}

    def fake_http_json(method, url, **kwargs):
        captured["body"] = kwargs["body"]
        return smoke.HttpResult(
            200,
            "{}",
            {
                "session_id": "sess-1",
                "launch_state": "live",
                "execution_lifetime": "live_control",
            },
        )

    monkeypatch.setattr(smoke, "_http_json", fake_http_json)

    smoke.launch_session(
        "https://demo.longhouse.ai",
        "cookie",
        device_id="demo-machine",
        cwd="/Users/example/git/zerg/longhouse",
        project="zerg",
        display_name="smoke",
        client_request_id="rid",
        execution_lifetime="live_control",
    )

    assert captured["body"]["execution_lifetime"] == "live_control"
    assert "initial_prompt" not in captured["body"]


def test_run_one_shot_wires_run_once_capability_and_nonce_prompt(monkeypatch) -> None:
    captured = {}

    monkeypatch.setenv("GITHUB_RUN_ID", "unit")
    monkeypatch.setattr(smoke, "wait_for_health_commit", lambda *args, **kwargs: {"status": "ok", "build": {"commit": "live"}})
    monkeypatch.setattr(
        smoke,
        "mint_device_token",
        lambda **kwargs: smoke.DeviceTokenAuth(token_id="token-1", token="zdt_test"),
    )
    monkeypatch.setattr(smoke, "revoke_device_token", lambda *args, **kwargs: {"ok": True, "token_id": "token-1"})
    monkeypatch.setattr(smoke, "hosted_session_debug", lambda **kwargs: {"assistant_events": []})
    monkeypatch.setattr(smoke, "stop_codex_bridge", lambda *args, **kwargs: {"ok": True})

    def fake_discover_machine(base_url, **kwargs):
        captured["discover_bearer_token"] = kwargs["bearer_token"]
        captured["required_capability"] = kwargs["required_capability"]
        return {
            "device_id": "demo-machine",
            "machine_name": "demo-machine",
            "engine_build": "test",
            "supports": ["codex.run_once", "codex.resume_run_once"],
        }

    def fake_launch_session(*args, **kwargs):
        captured["launch_bearer_token"] = kwargs["bearer_token"]
        captured["execution_lifetime"] = kwargs["execution_lifetime"]
        captured["initial_prompt"] = kwargs["initial_prompt"]
        for token in str(kwargs["initial_prompt"]).replace(".", " ").split():
            if token.startswith("LH_REMOTE_CONTEXT_"):
                captured["context_secret"] = token
                break
        return {
            "session_id": "sess-1",
            "launch_state": "live",
            "execution_lifetime": kwargs["execution_lifetime"],
        }

    def fake_poll_for_assistant_nonce(**kwargs):
        captured.setdefault("polled_nonces", []).append(kwargs["nonce"])
        text = kwargs["nonce"]
        if kwargs["nonce"].startswith("LH_REMOTE_CONTINUE_SMOKE_"):
            text = f"{kwargs['nonce']} {captured['context_secret']}"
        return {"assistant_events": [{"role": "assistant", "text": text}]}

    def fake_continue_session(*args, **kwargs):
        captured["continue_bearer_token"] = kwargs["bearer_token"]
        captured["continue_message"] = kwargs["message"]
        captured["continue_lifetime"] = kwargs["execution_lifetime"]
        return {
            "session_id": kwargs["session_id"],
            "launch_state": "live",
            "execution_lifetime": kwargs["execution_lifetime"],
        }

    def unexpected_send(*args, **kwargs):
        raise AssertionError("one-shot smoke should not use the live-control input endpoint")

    monkeypatch.setattr(smoke, "discover_machine", fake_discover_machine)
    monkeypatch.setattr(smoke, "launch_session", fake_launch_session)
    monkeypatch.setattr(smoke, "continue_session", fake_continue_session)
    monkeypatch.setattr(smoke, "poll_for_assistant_nonce", fake_poll_for_assistant_nonce)
    monkeypatch.setattr(smoke, "send_nonce_prompt", unexpected_send)
    monkeypatch.setattr(smoke, "send_second_input_probe", unexpected_send)

    result = smoke.run(
        SimpleNamespace(
            base_url="https://demo.longhouse.ai",
            subdomain="demo",
            container="longhouse-demo",
            ssh_target="runtime-host",
            bridge_stop_ssh_target=None,
            device_id=None,
            cwd="/Users/example/git/zerg/longhouse",
            project="zerg",
            expected_commit="live",
            execution_lifetime="one_shot",
            initial_prompt=None,
            health_timeout_secs=1,
            wait_after_launch_secs=0,
            assistant_timeout_secs=1,
            poll_interval_secs=0,
            output_json=None,
            skip_stop=True,
        )
    )

    assert result["ok"] is True
    assert "zdt_" not in json.dumps(result)
    assert result["auth"]["device_token_id"] == "token-1"
    assert result["auth_cleanup"]["ok"] is True
    assert captured["discover_bearer_token"] == "zdt_test"
    assert captured["launch_bearer_token"] == "zdt_test"
    assert captured["required_capability"] == "codex.run_once"
    assert captured["execution_lifetime"] == "one_shot"
    assert result["nonce"] in captured["initial_prompt"]
    assert result["context_secret"] in captured["initial_prompt"]
    assert captured["continue_bearer_token"] == "zdt_test"
    assert captured["continue_lifetime"] == "one_shot"
    assert result["continue_nonce"] in captured["continue_message"]
    assert result["context_secret"] not in captured["continue_message"]
    assert captured["polled_nonces"] == [result["nonce"], result["continue_nonce"]]
    assert result["cleanup"]["reason"] == "one_shot_has_no_bridge_to_stop"
