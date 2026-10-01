from __future__ import annotations

import argparse
import http.server
import json
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import zerg.qa.codex_helm_launch_visibility as launch


def test_helm_launch_bridge_socket_fits_linux_unix_path_budget():
    for prefix in ("lch-", "lca-"):
        isolation_root = Path("/tmp") / f"{prefix}{'x' * 8}"
        socket_path = launch.bridge_canary._bridge_state_root(isolation_root) / f"{'s' * 36}.sock"
        assert len(os.fsencode(socket_path)) < 108


def test_recording_proxy_reports_wrapper_exit_before_registration():
    proxy = launch.RuntimeHostRecordingProxy("https://runtime.invalid")
    process = subprocess.Popen(["/usr/bin/true"], stdout=subprocess.PIPE)
    process.wait(timeout=5)
    try:
        with pytest.raises(RuntimeError, match="exited before registration"):
            proxy.wait_registration(after=0, timeout=5, process=process)
    finally:
        proxy.server.server_close()


def test_recording_proxy_retains_registration_identity_without_authority_tokens():
    proxy = launch.RuntimeHostRecordingProxy("https://runtime.invalid")
    try:
        proxy._capture_registration(  # noqa: SLF001 - prove the evidence redaction boundary
            json.dumps(
                {
                    "provider": "codex",
                    "launch_actor": "human_shell",
                    "launch_surface": "terminal",
                    "session_id": "session-1",
                }
            ).encode(),
            json.dumps(
                {
                    "session_id": "session-1",
                    "run_id": "run-1",
                    "coordination_authority": {"token": "must-not-survive"},
                }
            ).encode(),
            200,
        )

        record = proxy.wait_registration(after=0, timeout=0.1)
        assert record["response"] == {"session_id": "session-1", "run_id": "run-1"}
        assert "must-not-survive" not in json.dumps(record)
    finally:
        proxy.server.server_close()


def test_recording_proxy_forwards_and_retains_managed_launch_identity():
    observed = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib callback name
            observed["user_agent"] = self.headers.get("User-Agent")
            body = json.dumps({"session_id": "session-1", "run_id": "run-1"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    host, port = upstream.server_address[:2]
    proxy = launch.RuntimeHostRecordingProxy(f"http://{host}:{port}")
    proxy.start()
    try:
        request = urllib.request.Request(
            f"{proxy.url}/api/sessions/managed-local/this-device",
            headers={"User-Agent": "longhouse-engine/test-version"},
            data=json.dumps({"provider": "codex", "session_id": "session-1"}).encode(),
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
        assert observed["user_agent"] == "longhouse-engine/test-version"
        assert proxy.wait_registration(after=0, timeout=0.1)["response"] == {
            "session_id": "session-1",
            "run_id": "run-1",
        }
    finally:
        proxy.close()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)


def test_wait_canonical_launch_requires_exact_run_open_and_default_visibility(monkeypatch):
    registration = {
        "request": {"session_id": "session-1"},
        "response": {"session_id": "session-1", "run_id": "run-1"},
    }

    def request(_args, path: str, _method: str, _body):
        assert path == "sessions/session-1/state-diagnostics"
        return {
            "catalog_commit_seq": 9,
            "shadow": {"mode": "helm", "control": {"connection": "connected"}, "control_run_id": "run-1"},
            "explain": {
                "working_set": "open",
                "launch_actor": "human_shell",
                "launch_surface": "terminal",
                "origin_kind": "managed_local",
                "fact_sources": {"control": {"source": "provider_control"}},
            },
        }

    monkeypatch.setattr(launch, "_runtime_request", request)
    monkeypatch.setattr(launch, "_session_visible", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        launch,
        "_browser_workspace",
        lambda *_args, **_kwargs: {
            "session": {
                "capabilities": {
                    "live_control_available": True,
                    "input_mode": "live",
                    "can_send_input": True,
                    "composer_enabled": True,
                    "composer_disabled_reason": None,
                    "control_label": "live",
                }
            }
        },
    )
    result = launch._wait_canonical_launch(  # noqa: SLF001 - pure product-proof seam
        argparse.Namespace(wait_ready_secs=0.1),
        registration=registration,
        project="proof",
        device_id="machine",
        expected_actor="human_shell",
        expected_surface="terminal",
        expect_visible=False,
        expect_open=True,
        launched_at=time.monotonic(),
    )

    assert result["working_set"] == "open"
    assert result["control_run_id"] == "run-1"
    assert result["default_timeline_visible"] is False


def test_infrastructure_failure_retains_cause_without_claiming_a_product_verdict(tmp_path, monkeypatch):
    codex_bin = tmp_path / "codex"
    codex_bin.write_bytes(b"fixture")

    class Proxy:
        def __init__(self, _target):
            pass

        def start(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(launch, "RuntimeHostRecordingProxy", Proxy)
    monkeypatch.setattr(launch.bridge_canary, "_run", lambda *_args, **_kwargs: SimpleNamespace(stdout="codex 1.0"))
    monkeypatch.setattr(
        launch,
        "_human_launch_sequence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('Runtime Host HTTP 503: {"detail":{"code":"resource_exhausted"}}')),
    )

    result = launch.run_scenario(
        argparse.Namespace(
            api_url="https://runtime.invalid",
            codex_bin=codex_bin,
            evidence_root=tmp_path / "evidence",
        )
    )

    assert result["status"] == "fail"
    assert result["failure_code"] == "codex_helm_launch_visibility_failed"
    assert "Runtime Host HTTP 503" in result["error"]
    assert "observation" not in result
    assert "assertions" not in result
    assert {item["path"] for item in result["artifact_manifest"]} == {"provider-binary-receipt.json"}


@pytest.mark.parametrize("bridge_receipt", [{}, {"verification": {"verified": False, "alive_pids": [42]}}])
def test_stop_launch_refuses_unverified_bridge_and_closes_wrapper(tmp_path, monkeypatch, bridge_receipt):
    process = subprocess.Popen(["/bin/sleep", "60"])

    def close():
        process.terminate()
        process.wait(timeout=5)

    tui = SimpleNamespace(process=process, close=close)
    monkeypatch.setattr(launch.bridge_canary, "_stop_bridge", lambda *_args: bridge_receipt)
    try:
        with pytest.raises(RuntimeError, match="bridge cleanup"):
            launch._stop_launch(
                argparse.Namespace(),
                tui=tui,
                session_id="owned-session",
                isolation_root=tmp_path,
            )
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            close()


def _queue_runtime_event(directory, name, dedupe_key):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps({"kind": "terminal_signal", "dedupe_key": dedupe_key}), encoding="utf-8")


def test_stop_launch_waits_for_the_shipper_to_deliver_the_terminal_event(tmp_path, monkeypatch):
    """Stopping the shipper before delivery left the served run `running`."""

    outbox = tmp_path / "longhouse" / "agent" / "runtime-events-outbox"
    key = "bridge:terminal:owned-session:run-1"
    _queue_runtime_event(outbox, "queued.json", key)
    _queue_runtime_event(outbox, "other.json", "bridge:terminal:other-session:run-2")
    process = subprocess.Popen(["/bin/sleep", "60"])

    def close():
        process.terminate()
        process.wait(timeout=5)

    receipt = {"verification": {"verified": True, "state": {"terminal_dedupe_key": key, "terminal_published": True}}}
    monkeypatch.setattr(launch.bridge_canary, "_stop_bridge", lambda *_args: receipt)
    # The shipper delivers (removes) the event while the producer waits.
    threading.Timer(0.6, lambda: (outbox / "queued.json").unlink()).start()
    try:
        stop = launch._stop_launch(
            argparse.Namespace(),
            tui=SimpleNamespace(process=process, close=close),
            session_id="owned-session",
            isolation_root=tmp_path,
        )
    finally:
        if process.poll() is None:
            close()
    delivery = stop["terminal_event_delivery"]
    assert delivery["delivered"] is True and delivery["dedupe_key"] == key
    assert delivery["wait_seconds"] >= 0.5


def test_terminal_event_wait_fails_typed_when_undelivered_or_dead_lettered(tmp_path):
    outbox = tmp_path / "longhouse" / "agent" / "runtime-events-outbox"
    key = "bridge:terminal:owned-session:run-1"
    _queue_runtime_event(outbox, "queued.json", key)
    with pytest.raises(RuntimeError, match="still queued"):
        launch._wait_terminal_event_delivered(tmp_path, key, timeout=0.3)
    (outbox / "queued.json").unlink()
    _queue_runtime_event(outbox / "dead-letter", "rejected.json", key)
    with pytest.raises(RuntimeError, match="dead-lettered"):
        launch._wait_terminal_event_delivered(tmp_path, key, timeout=5)


def test_stop_launch_refuses_a_stop_without_a_published_terminal_event(tmp_path, monkeypatch):
    process = subprocess.Popen(["/bin/sleep", "60"])

    def close():
        process.terminate()
        process.wait(timeout=5)

    receipt = {"verification": {"verified": True, "state": {"terminal_published": False}}}
    monkeypatch.setattr(launch.bridge_canary, "_stop_bridge", lambda *_args: receipt)
    try:
        with pytest.raises(RuntimeError, match="did not publish a terminal event"):
            launch._stop_launch(
                argparse.Namespace(),
                tui=SimpleNamespace(process=process, close=close),
                session_id="owned-session",
                isolation_root=tmp_path,
            )
    finally:
        if process.poll() is None:
            close()


# The picker as the terminal log carries it (cursor movement eats the spaces), captured from a real
# Codex 0.159.3 first start on 2026-10-01 under a PTY with the factory's model.
_MIGRATION_PICKER = (
    b"\x1b[2J\x1b[H>_ OpenAI Codex (v0.159.3)\x1b[3;1H\xe2\x80\xba Ask Codex to do anything\x1b[5;1H"
    b"Meet GPT-6 Luna\x1b[6;1HOur latest Luna is significantly more efficient\x1b[8;1H"
    b"\xe2\x80\xba 1. Try new model\x1b[9;1H  2.\x1b[1CUse\x1b[1Cexisting\x1b[1Cmodel\x1b[10;1Henter/esc confirm"
)
_COMPOSER = b"\x1b[2J\x1b[H>_ OpenAI Codex (v0.159.3)\x1b[3;1H\xe2\x80\xba Ask Codex to do anything\x1b[9;1HGPT-5.6-Luna default"


class _FakeCodexTui:
    """An owned PTY stand-in: the terminal log is a file, a key press can append a reply frame."""

    def __init__(self, tmp_path: Path, *, initial: bytes, replies: dict[bytes, bytes] | None = None, reply_delay: float = 0.0) -> None:
        self.reply_delay = reply_delay
        self.terminal_path = tmp_path / "terminal.log"
        self.terminal_path.write_bytes(initial)
        self.process = SimpleNamespace(returncode=None, pid=1)
        self.replies = replies or {}
        self.events: list[tuple[str, bytes | str]] = []

    def alive(self) -> bool:
        return True

    def write(self, value: bytes) -> None:
        self.events.append(("write", value))
        reply = self.replies.get(value, b"")

        def append() -> None:
            with self.terminal_path.open("ab") as handle:
                handle.write(reply)

        if self.reply_delay:
            threading.Timer(self.reply_delay, append).start()
        else:
            append()

    def submit_line(self, text: str) -> None:
        self.events.append(("submit", text))


def test_the_model_migration_picker_is_answered_before_the_seed_is_typed(tmp_path):
    tui = _FakeCodexTui(tmp_path, initial=_MIGRATION_PICKER, replies={b"2": _COMPOSER})

    answered = launch._answer_startup_dialogs(tui, timeout=10)

    assert tui.events == [("write", b"2")]
    assert [item["dialog"] for item in answered] == ["model_migration"]
    assert answered[0]["answer"] == "use_existing_model"


def test_a_codex_without_the_picker_is_left_alone_as_soon_as_its_composer_is_ready(tmp_path):
    tui = _FakeCodexTui(tmp_path, initial=_COMPOSER)

    started = time.monotonic()
    assert launch._answer_startup_dialogs(tui, timeout=10) == []

    assert tui.events == []
    assert time.monotonic() - started < 2.0, "a ready composer must not wait out the picker's window"


def test_a_terminal_that_never_says_anything_is_waited_on_once_and_not_failed(tmp_path):
    """The seed goes in as it always did when neither the picker nor the status line ever shows."""

    tui = _FakeCodexTui(tmp_path, initial=b"\x1b[2J starting")

    assert launch._answer_startup_dialogs(tui, timeout=0.6) == []
    assert tui.events == []


def test_the_picker_arrives_after_a_quiet_start_and_is_still_answered(tmp_path):
    """Observed on Codex 0.159.3: 2.4 s with no output, then the picker at 3.0 s."""

    tui = _FakeCodexTui(tmp_path, initial=b"\x1b[2J loading", replies={b"2": _COMPOSER})
    threading.Timer(1.0, lambda: tui.terminal_path.open("ab").write(_MIGRATION_PICKER)).start()

    answered = launch._answer_startup_dialogs(tui, timeout=10)

    assert len(answered) == 1
    assert answered[0]["after_seconds"] >= 1.0


def test_a_second_picker_drawn_after_the_first_answer_is_answered_too(tmp_path):
    tui = _FakeCodexTui(tmp_path, initial=_MIGRATION_PICKER, reply_delay=1.3)
    original_write = tui.write
    calls = {"count": 0}

    def write(value: bytes) -> None:
        calls["count"] += 1
        tui.replies = {b"2": _MIGRATION_PICKER if calls["count"] == 1 else _COMPOSER}
        original_write(value)

    tui.write = write

    answered = launch._answer_startup_dialogs(tui, timeout=20)

    assert len(answered) == 2
    assert [event for event in tui.events if event[0] == "write"] == [("write", b"2")] * 2


def test_an_answered_picker_frame_is_not_read_again(tmp_path):
    """The reply frame can still hold the picker's text; the composer after it decides."""

    tui = _FakeCodexTui(tmp_path, initial=_MIGRATION_PICKER, replies={b"2": _MIGRATION_PICKER + _COMPOSER})

    answered = launch._answer_startup_dialogs(tui, timeout=10)

    assert len(answered) == 1
    assert tui.events == [("write", b"2")]


def test_the_seed_goes_in_only_after_the_picker_is_answered(tmp_path, monkeypatch):
    tui = _FakeCodexTui(tmp_path, initial=_MIGRATION_PICKER, replies={b"2": _COMPOSER})
    monkeypatch.setattr(launch.bridge_canary, "_assistant_transcript_contains", lambda *_a, **_k: True)
    codex_home = tmp_path / "codex-home"
    rollout = codex_home / "sessions" / "2026" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("{}\n")

    receipt = launch._seed_codex_rollout(tui, codex_home=codex_home, marker="MARK", timeout=5)

    assert [event[0] for event in tui.events] == ["write", "submit"]
    assert tui.events[1] == ("submit", "Reply with exactly MARK")
    assert receipt["status"] == "pass"
    assert [item["dialog"] for item in receipt["startup_dialogs"]] == ["model_migration"]
