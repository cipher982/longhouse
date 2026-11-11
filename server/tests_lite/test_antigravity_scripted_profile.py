"""The Antigravity scripted profile cannot reach Google, and the Gemini path streams a paced multi-frame reply."""

from __future__ import annotations

import http.client
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests_lite.test_scripted_model_server import _running_server
from zerg.qa import scripted_model_profiles as profiles
from zerg.qa.scripted_model_server import paced_text
from zerg.qa.scripted_model_server import parse_pacing

ENDPOINT = "http://127.0.0.1:50123"
AMBIENT = {
    "LONGHOUSE_ANTIGRAVITY_BIN": "/Users/someone/.local/bin/agy-dangerously-skip-permissions",
    "LONGHOUSE_HOOK_URL": "https://hosted.example",
    "LONGHOUSE_HOOK_TOKEN": "zst_ambient",
    "LONGHOUSE_MANAGED_SESSION_ID": "ambient-session",
    "LONGHOUSE_PROVIDER_SESSION_ID": "ambient-provider-session",
    "GOOGLE_API_KEY": "ambient-google-key",
    "HTTPS_PROXY": "http://corp-proxy:3128",
}


def test_profile_points_agy_at_the_loopback_fake_and_blocks_every_other_https_route(tmp_path: Path) -> None:
    environment = {**AMBIENT, profiles.SCRIPTED_MODEL_BASE_URL_ENV: ENDPOINT}
    profiles.configure_scripted_model_profile("antigravity", tmp_path, environment)

    assert environment["HOME"] == str(tmp_path)
    assert environment["GOOGLE_GEMINI_BASE_URL"] == ENDPOINT
    assert environment["GEMINI_API_KEY"] == "longhouse-mock-key"
    # A discard-port proxy refuses any non-loopback HTTPS request locally; loopback bypasses it.
    assert environment["HTTPS_PROXY"] == environment["https_proxy"] == "http://127.0.0.1:9"
    assert environment["NO_PROXY"] == "127.0.0.1,localhost"
    for name in AMBIENT:
        if name not in {"HTTPS_PROXY"}:
            assert name not in environment, name
    cli = tmp_path / ".gemini" / "antigravity-cli"
    assert json.loads((cli / "settings.json").read_text())["modelProvider"] == "gemini"
    assert json.loads((cli / "cache" / "onboarding.json").read_text())["onboardingComplete"] is True


@pytest.mark.parametrize("endpoint", ["https://generativelanguage.googleapis.com", "http://example.com:80", "http://10.0.0.5:8080"])
def test_profile_refuses_a_non_loopback_endpoint(tmp_path: Path, endpoint: str) -> None:
    environment = {profiles.SCRIPTED_MODEL_BASE_URL_ENV: endpoint}
    with pytest.raises(RuntimeError, match="loopback"):
        profiles.configure_scripted_model_profile("antigravity", tmp_path, environment)
    assert not (tmp_path / ".gemini").exists()


def test_gemini_paced_reply_is_many_frames_matched_through_an_earlier_user_message(tmp_path: Path) -> None:
    pace = {"tokens": 12, "rate": 40.0, "initial_delay": 0.2, "marker_every": 4, "marker_prefix": "AGYPACE"}
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"turns": [{"id": "paced", "match": "AGYTRUTH", "match_scope": "any_user", "pace": pace}]}))
    expected = paced_text(parse_pacing(pace)).split(" ")
    body = {
        # agy appends context as the last user message, so the marker is only in an earlier one.
        "contents": [
            {"role": "user", "parts": [{"text": "AGYTRUTH say something"}]},
            {"role": "user", "parts": [{"text": "<context>no marker here</context>"}]},
        ]
    }
    with _running_server(tmp_path, script_path=script) as (endpoint, _receipt):
        parsed = urlsplit(endpoint)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
        frames: list[tuple[float, dict]] = []
        try:
            connection.request(
                "POST",
                "/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent?alt=sse",
                json.dumps(body),
                {"Content-Type": "application/json"},
            )
            started = time.monotonic()
            response = connection.getresponse()
            assert response.status == 200
            while line := response.fp.readline():
                text = line.decode().strip()
                if text.startswith("data: "):
                    frames.append((time.monotonic() - started, json.loads(text[6:])))
        finally:
            connection.close()

    texts = ["".join(part["text"] for part in frame["candidates"][0]["content"]["parts"]) for _, frame in frames]
    assert len(frames) == len(expected) == 12
    assert "".join(texts).split() == expected
    assert [frame["candidates"][0].get("finishReason") for _, frame in frames] == [None] * 11 + ["STOP"]
    arrivals = [at for at, _ in frames]
    assert arrivals == sorted(arrivals)
    assert arrivals[-1] - arrivals[0] > 0.2  # 11 gaps at 40/s: a real stream, not one write


def test_timing_sidecar_with_a_line_still_being_written_loads_the_complete_chunks(tmp_path: Path) -> None:
    from zerg.qa.pty_reader import load_timed_chunks

    recording, timing = tmp_path / "t.raw", tmp_path / "t.timing"
    recording.write_bytes(b"hello world")
    timing.write_text('{"clock":"monotonic","start_offset":0}\n{"t":1.0,"o":0,"n":5}\n{"t":2.0,"o":5,"n":6}\n{"t":3.0,"o":11')
    assert [(chunk.t, chunk.data) for chunk in load_timed_chunks(recording, timing)] == [(1.0, b"hello"), (2.0, b" world")]
    timing.write_text('{"clock":"monotonic","start_offset":0}\n{"t":1.0,"o":0,"n":50}\n')
    with pytest.raises(ValueError, match="runs past the recording"):
        load_timed_chunks(recording, timing)


def test_helm_hook_is_the_shipped_script_written_into_the_isolated_home_only(tmp_path: Path) -> None:
    from zerg.qa.terminal_truth import _install_antigravity_hook

    home = tmp_path / "h"
    home.mkdir()
    _install_antigravity_hook(home, {"HOME": str(home)})
    script = home / ".longhouse/managed-local/antigravity/plugins/longhouse-runtime/longhouse-antigravity-hook.sh"
    text = script.read_text()
    assert "__LONGHOUSE_HOME__" not in text and str(home / ".longhouse") in text
    assert "wake" in text  # the shipped script wakes the transcript shipper on Stop; a stub would not
    assert script.stat().st_mode & 0o111
    hooks = json.loads((home / ".gemini/config/hooks.json").read_text())
    assert set(hooks["longhouse-runtime"]) >= {"PreInvocation", "PostInvocation", "Stop"}
    assert str(script).replace("'", "") in json.dumps(hooks)
    with pytest.raises(RuntimeError, match="isolated HOME"):
        _install_antigravity_hook(tmp_path / "other", {"HOME": str(home)})
