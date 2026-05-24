"""Unit coverage for scripts/ops/remote-launch-smoke.py.

The live workflow owns the end-to-end assertion. These tests pin the parts of
the harness that can regress quietly: exact build matching, machine selection,
and requiring an assistant-role event rather than a user echo.
"""

from __future__ import annotations

import importlib.util
import sys
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
        assert url == "https://david010.longhouse.ai/api/timeline/machines"
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
                    },
                    {
                        "device_id": "claude-only",
                        "online": True,
                        "supports": ["claude.launch"],
                        "can_launch_codex": False,
                    },
                    {
                        "device_id": "cube",
                        "machine_name": "cube",
                        "online": True,
                        "supports": ["codex.launch"],
                        "can_launch_codex": True,
                    },
                ]
            },
        )

    monkeypatch.setattr(smoke, "_http_json", fake_http_json)

    machine = smoke.discover_machine("https://david010.longhouse.ai", "cookie")

    assert machine["device_id"] == "cube"


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

    result = smoke._http_json("GET", "https://david010.longhouse.ai/api/health")

    assert result.status == 200
    assert "Mozilla/5.0" in captured["user_agent"]
    assert captured["accept_language"] == "en-US,en;q=0.9"


def test_remote_python_ssh_accepts_new_host_keys(monkeypatch) -> None:
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return smoke.subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(smoke.subprocess, "run", fake_run)

    smoke._run_remote_python("zerg", container="longhouse-david010", script="print('ok')")

    assert captured["command"][:5] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]


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
            "https://david010.longhouse.ai",
            "cookie",
            device_id="cube",
            cwd="/Users/davidrose/git/zerg/longhouse",
            project="zerg",
            display_name="smoke",
            client_request_id="rid",
        )
    except smoke.SmokeError as exc:
        assert "launching_unknown" in str(exc)
    else:
        raise AssertionError("launch_session should reject launching_unknown")
