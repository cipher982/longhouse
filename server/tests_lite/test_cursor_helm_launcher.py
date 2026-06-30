"""Unit tests for the cursor Helm launcher's testable logic.

These cover the state-file round-trip, the socket command handler (send /
interrupt / terminate / ping / unknown), and the inject byte sequence. The
full PTY pass-through + live cursor-agent flow is exercised interactively
with David, not here.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time

from zerg.cli import cursor_helm


def _state_dir_for(monkeypatch, tmp_path) -> "object":
    monkeypatch.setenv("LONGHOUSE_HOME", str(tmp_path))
    return cursor_helm._state_dir()


def test_state_file_round_trip(monkeypatch, tmp_path):
    _state_dir_for(monkeypatch, tmp_path)
    session_id = "11111111-1111-4111-8111-111111111111"
    sock = cursor_helm._socket_path(session_id)
    cursor_helm._write_state(session_id, socket_path=sock, cursor_pid=123, cwd=tmp_path, ready=True)

    raw = cursor_helm._state_file_path(session_id).read_text()
    state = json.loads(raw)
    assert state["session_id"] == session_id
    assert state["provider"] == "cursor"
    assert state["control_plane"] == "cursor_helm"
    assert state["socket_path"] == str(sock)
    assert state["launcher_pid"] == os.getpid()
    assert state["cursor_pid"] == 123
    assert state["ready"] is True

    cursor_helm._remove_state(session_id, sock)
    assert not cursor_helm._state_file_path(session_id).exists()
    assert not sock.exists()


def test_handle_command_send_writes_text_escape_enter_sequence():
    read_fd, write_fd = os.pipe()
    lock = threading.Lock()
    stop = threading.Event()
    request = {"kind": "send", "text": "hello world"}
    reply = cursor_helm._handle_command(
        request, master_fd=write_fd, child_pid=999999, master_lock=lock, stop_event=stop
    )
    os.close(write_fd)
    written = os.read(read_fd, 4096)
    os.close(read_fd)
    assert reply == {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}
    # text, then Escape, then Enter — the Ink submit workaround.
    assert written == b"hello world\x1b\r"


def test_handle_command_send_missing_text_is_bad_request():
    read_fd, write_fd = os.pipe()
    try:
        reply = cursor_helm._handle_command(
            {"kind": "send"}, master_fd=write_fd, child_pid=1, master_lock=threading.Lock(),
            stop_event=threading.Event(),
        )
        assert reply["ok"] is False
        assert reply["error"]["code"] == "bad_request"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_handle_command_unknown_kind_is_bad_request():
    reply = cursor_helm._handle_command(
        {"kind": "nope"}, master_fd=1, child_pid=1, master_lock=threading.Lock(),
        stop_event=threading.Event(),
    )
    assert reply["ok"] is False
    assert reply["error"]["code"] == "bad_request"


def test_handle_command_ping_ok():
    reply = cursor_helm._handle_command(
        {"kind": "ping"}, master_fd=1, child_pid=1, master_lock=threading.Lock(),
        stop_event=threading.Event(),
    )
    assert reply["ok"] is True


def test_handle_command_interrupt_signals_child():
    child = subprocess.Popen(["sleep", "30"])
    stop = threading.Event()
    reply = cursor_helm._handle_command(
        {"kind": "interrupt"}, master_fd=1, child_pid=child.pid, master_lock=threading.Lock(),
        stop_event=stop,
    )
    assert reply["ok"] is True
    # SIGINT default-terminates `sleep`.
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
        raise AssertionError("interrupt did not terminate the sleep child")
    assert not stop.is_set(), "interrupt must not set the stop event (only terminate does)"


def test_handle_command_terminate_kills_child_and_sets_stop():
    child = subprocess.Popen(["sleep", "30"])
    stop = threading.Event()
    reply = cursor_helm._handle_command(
        {"kind": "terminate"}, master_fd=1, child_pid=child.pid, master_lock=threading.Lock(),
        stop_event=stop,
    )
    assert reply["ok"] is True
    assert stop.is_set(), "terminate must set the stop event so the launcher exits"
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        raise AssertionError("terminate did not kill the sleep child")


def test_handle_command_interrupt_dead_child_reports_not_attached():
    reply = cursor_helm._handle_command(
        {"kind": "interrupt"}, master_fd=1, child_pid=2_000_000, master_lock=threading.Lock(),
        stop_event=threading.Event(),
    )
    assert reply["ok"] is False
    assert reply["error"]["code"] == "session_not_attached"
