"""Tests for the cursor Helm launcher.

Unit tests cover the state-file round-trip, the socket command handler (send /
interrupt / terminate / ping / unknown), and the inject byte sequence.
Integration tests exercise the real pty.fork -> socket-server -> inject -> echo
path with `cat` as the child, plus the session_not_attached behavior when a
send lands after the child has exited. The full live cursor-agent TUI flow is
exercised interactively with David, not here.
"""

from __future__ import annotations

import json
import os
import pty
import select
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from zerg.cli import cursor_helm


def _state_dir_for(monkeypatch, tmp_path) -> "object":
    monkeypatch.setenv("LONGHOUSE_HOME", str(tmp_path))
    return cursor_helm._state_dir()


def test_state_file_round_trip(monkeypatch, tmp_path):
    _state_dir_for(monkeypatch, tmp_path)
    session_id = "11111111-1111-4111-8111-111111111111"
    sock = cursor_helm._socket_path(session_id)
    cursor_helm._write_state(
        session_id,
        socket_path=sock,
        cursor_pid=123,
        cwd=tmp_path,
        ready=True,
        registration="registered",
    )

    raw = cursor_helm._state_file_path(session_id).read_text()
    state = json.loads(raw)
    assert state["session_id"] == session_id
    assert state["provider"] == "cursor"
    assert state["control_plane"] == "cursor_helm"
    assert state["socket_path"] == str(sock)
    assert state["launcher_pid"] == os.getpid()
    assert state["cursor_pid"] == 123
    assert state["ready"] is True
    assert state["registration"] == "registered"

    cursor_helm._remove_state(session_id, sock)
    assert not cursor_helm._state_file_path(session_id).exists()
    assert not sock.exists()


def test_register_session_soft_fails_on_connect_error(monkeypatch, tmp_path):
    class BoomClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            raise cursor_helm.httpx.ConnectError("down")

    monkeypatch.setattr(cursor_helm.httpx, "Client", BoomClient)
    outcome = cursor_helm._register_session(
        url="https://example.invalid",
        token="tok",
        cwd=tmp_path,
        project="demo",
        name=None,
        loop_mode=cursor_helm.SessionLoopMode.ASSIST,
        machine_name="cinder",
        permission_mode="bypass",
        session_id="11111111-1111-4111-8111-111111111111",
    )
    assert outcome.registered is False
    assert outcome.session_id.endswith("1111")
    assert "connect failed" in (outcome.error or "")


def test_registration_worker_terminalizes_when_exit_races_success(monkeypatch, tmp_path):
    """If register commits after Helm exit, terminalize immediately."""
    _state_dir_for(monkeypatch, tmp_path)
    session_id = "22222222-2222-4222-8222-222222222222"
    sock = cursor_helm._socket_path(session_id)
    terminal_reasons: list[str] = []
    stop = threading.Event()

    def _register_then_mark_exit(**_kwargs):
        stop.set()
        return cursor_helm._RegistrationOutcome(
            session_id=session_id,
            registered=True,
            attach_command="",
        )

    monkeypatch.setattr(cursor_helm, "_register_session", _register_then_mark_exit)
    monkeypatch.setattr(
        cursor_helm,
        "_post_terminal_event",
        lambda _url, _token, _sid, reason: terminal_reasons.append(reason),
    )
    monkeypatch.setattr(cursor_helm, "_REGISTER_RETRY_DELAYS_SECONDS", (0.0,))

    outcome_box: list = []
    cursor_helm._registration_worker(
        url="https://example.invalid",
        token="tok",
        cwd=tmp_path,
        project="demo",
        name=None,
        loop_mode=cursor_helm.SessionLoopMode.ASSIST,
        machine_name="cinder",
        permission_mode="bypass",
        session_id=session_id,
        sock_path=sock,
        stop_event=stop,
        outcome_box=outcome_box,
        outcome_lock=threading.Lock(),
        verbose=False,
    )

    assert terminal_reasons == ["helm_exit_before_ready"]
    assert outcome_box == []


def test_reconcile_registration_on_exit_terminalizes_unknown_outcome(monkeypatch):
    terminal_reasons: list[str] = []
    monkeypatch.setattr(
        cursor_helm,
        "_post_terminal_event",
        lambda _url, _token, _sid, reason: terminal_reasons.append(reason),
    )

    finished = threading.Event()

    def _linger():
        finished.wait(timeout=0.05)

    thread = threading.Thread(target=_linger, daemon=True)
    thread.start()
    durable = cursor_helm._reconcile_registration_on_exit(
        url="https://example.invalid",
        token="tok",
        session_id="33333333-3333-4333-8333-333333333333",
        registration_thread=thread,
        registration_box=[],
        registration_lock=threading.Lock(),
        join_timeout=0.2,
    )
    assert durable is False
    assert terminal_reasons == ["helm_exit_before_ready"]


def test_reconcile_registration_on_exit_closes_registered_session(monkeypatch):
    terminal_reasons: list[str] = []
    monkeypatch.setattr(
        cursor_helm,
        "_post_terminal_event",
        lambda _url, _token, _sid, reason: terminal_reasons.append(reason),
    )
    thread = threading.Thread(target=lambda: None, daemon=True)
    thread.start()
    thread.join(timeout=1.0)
    box = [
        cursor_helm._RegistrationOutcome(
            session_id="44444444-4444-4444-8444-444444444444",
            registered=True,
            attach_command="",
        )
    ]
    durable = cursor_helm._reconcile_registration_on_exit(
        url="https://example.invalid",
        token="tok",
        session_id="44444444-4444-4444-8444-444444444444",
        registration_thread=thread,
        registration_box=box,
        registration_lock=threading.Lock(),
        join_timeout=0.1,
    )
    assert durable is True
    assert terminal_reasons == ["helm_exit"]


def test_register_session_soft_fails_on_401(monkeypatch, tmp_path):
    class AuthClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return cursor_helm.httpx.Response(401, request=cursor_helm.httpx.Request("POST", "https://x"))

    monkeypatch.setattr(cursor_helm.httpx, "Client", AuthClient)
    outcome = cursor_helm._register_session(
        url="https://example.invalid",
        token="tok",
        cwd=tmp_path,
        project="demo",
        name=None,
        loop_mode=cursor_helm.SessionLoopMode.ASSIST,
        machine_name="cinder",
        permission_mode="bypass",
        session_id="11111111-1111-4111-8111-111111111111",
    )
    assert outcome.registered is False
    assert "authentication failed" in (outcome.error or "")


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


def _start_helm_socket_server(tmp_path, master_fd, child_pid, stop_event):
    """Bind a temp Unix socket, start the launcher's socket server thread, return (sock_path, thread, server)."""
    # macOS sun_path is ~104 chars; tmp_path under /var/folders can exceed that,
    # so bind under /tmp with a short unique dir.
    short_dir = tempfile.mkdtemp(dir="/tmp", prefix="lh_helm_")
    sock_path = Path(short_dir) / "helm.sock"
    if sock_path.exists():
        sock_path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(4)
    os.chmod(sock_path, 0o600)
    lock = threading.Lock()
    thread = threading.Thread(
        target=cursor_helm._socket_server,
        args=(server,),
        kwargs={
            "master_fd": master_fd,
            "child_pid": child_pid,
            "master_lock": lock,
            "stop_event": stop_event,
        },
        daemon=True,
        name="cursor-helm-test-socket",
    )
    thread.start()
    return sock_path, thread, server, short_dir


def _send_command(sock_path, request) -> dict:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5.0)
    client.connect(str(sock_path))
    try:
        client.sendall((json.dumps(request) + "\n").encode())
        buf = bytearray()
        while b"\n" not in buf:
            chunk = client.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
        line, _, _ = bytes(buf).partition(b"\n")
        return json.loads(line.decode("utf-8", errors="replace"))
    finally:
        client.close()


def _read_pty_echo(master_fd: int, needle: bytes, timeout_s: float = 2.0) -> bytes:
    """Drain the PTY master for up to timeout_s looking for needle; return captured bytes."""
    deadline = time.time() + timeout_s
    captured = bytearray()
    while time.time() < deadline:
        ready, _, _ = select.select([master_fd], [], [], 0.1)
        if not ready:
            continue
        try:
            chunk = os.read(master_fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        captured.extend(chunk)
        if needle in captured:
            break
    return bytes(captured)


def test_socket_protocol_send_injects_into_real_pty_and_interrupt_exits_child(tmp_path):
    stop_event = threading.Event()
    pid, master_fd = pty.fork()
    if pid == 0:
        # Child: `cat` echoes stdin to stdout under the PTY slave.
        os.execvp("cat", ["cat"])
        os._exit(127)

    sock_path, thread, server, short_dir = _start_helm_socket_server(tmp_path, master_fd, pid, stop_event)
    try:
        # ping round-trips through the real socket server.
        assert _send_command(sock_path, {"kind": "ping"})["ok"] is True

        # send injects "hello" + Escape + Enter into the PTY master; cat echoes it.
        reply = _send_command(sock_path, {"kind": "send", "text": "hello"})
        assert reply["ok"] is True
        echo = _read_pty_echo(master_fd, b"hello")
        assert b"hello" in echo, f"expected 'hello' in PTY echo, got {echo!r}"

        # interrupt sends SIGINT to cat; cat exits.
        assert _send_command(sock_path, {"kind": "interrupt"})["ok"] is True
        try:
            _, status = os.waitpid(pid, 0)
            assert os.WIFSIGNALED(status) or os.WIFEXITED(status)
        except subprocess.TimeoutExpired:
            raise AssertionError("interrupt did not exit cat")
    finally:
        stop_event.set()
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            server.close()
        except OSError:
            pass
        thread.join(timeout=3.0)
        shutil.rmtree(short_dir, ignore_errors=True)


def test_socket_protocol_terminate_kills_real_child_and_sets_stop(tmp_path):
    stop_event = threading.Event()
    pid, master_fd = pty.fork()
    if pid == 0:
        os.execvp("cat", ["cat"])
        os._exit(127)

    sock_path, thread, server, short_dir = _start_helm_socket_server(tmp_path, master_fd, pid, stop_event)
    try:
        reply = _send_command(sock_path, {"kind": "terminate"})
        assert reply["ok"] is True
        # terminate must set the stop event so the launcher's main loop exits.
        assert stop_event.wait(timeout=3.0), "terminate did not set stop_event"
        try:
            _, status = os.waitpid(pid, 0)
            assert os.WIFSIGNALED(status) or os.WIFEXITED(status)
        except ChildProcessError:
            pass
    finally:
        stop_event.set()
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            server.close()
        except OSError:
            pass
        thread.join(timeout=3.0)
        shutil.rmtree(short_dir, ignore_errors=True)


def test_send_after_child_exit_reports_session_not_attached():
    """A remote send whose PTY master write fails must surface as
    session_not_attached, not a generic command_failed. The launcher's
    OSError handling is defense-in-depth for the narrow race between child
    exit and socket teardown; in the steady state the engine gets
    connection-refused (already mapped to not-attached) because the launcher
    unlinks the socket on exit."""
    # Use a read-only pipe end as master_fd: os.write on it raises OSError
    # (EBADF) without the fd-reuse race that a closed fd would hit.
    read_fd, write_fd = os.pipe()
    try:
        reply = cursor_helm._handle_command(
            {"kind": "send", "text": "hello"},
            master_fd=read_fd,
            child_pid=1,
            master_lock=threading.Lock(),
            stop_event=threading.Event(),
        )
        assert reply["ok"] is False
        assert reply["error"]["code"] == "session_not_attached"
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass
        try:
            os.close(write_fd)
        except OSError:
            pass


def test_set_pty_size_actually_sets_winsize():
    """Regression: _set_pty_size must really set the PTY winsize, not silently
    no-op. An earlier version called `termios.ioctl(...)` which does not exist
    on CPython (the ioctl *function* lives in `fcntl`); the call raised
    AttributeError and was swallowed by the try/except, so the PTY stayed at
    its default 0x0 winsize and cursor-agent wrapped every character to its
    own line. Verify via a real pty.openpty pair that the size round-trips."""
    import fcntl
    import struct
    import termios

    master, slave = pty.openpty()
    try:
        cursor_helm._set_pty_size(master, 40, 132)
        packed = fcntl.ioctl(slave, termios.TIOCGWINSZ, b"\x00" * 8)
        rows, cols, _, _ = struct.unpack("hhhh", packed)
        assert (rows, cols) == (40, 132), f"winsize did not round-trip: got {(rows, cols)}"
        # And _get_terminal_size reads it back from the master.
        assert cursor_helm._get_terminal_size(master) == (40, 132)
    finally:
        os.close(master)
        os.close(slave)


def test_infer_git_context_handles_none_git_output(monkeypatch):
    """git_output returns None when git fails or a value is unset (no origin
    remote, detached HEAD, not a repo). _infer_git_context must not call
    .strip() on None. Regression for the `lhcu` crash when launched from a
    non-git cwd. Monkeypatch git_output so the test does not depend on
    tmp_path being outside a repo (in CI tmp_path is nested inside the
    checkout, so git would otherwise resolve the parent origin URL)."""
    monkeypatch.setattr(cursor_helm, "git_output", lambda *_a, **_k: None)
    repo, branch = cursor_helm._infer_git_context(Path("/does/not/matter"))
    assert repo is None
    assert branch is None


def test_infer_git_context_normalizes_detached_head(monkeypatch):
    """A detached HEAD returns the literal string 'HEAD'; the launcher should
    normalize that to None so the session isn't tagged with branch 'HEAD'."""
    def fake_git(_cwd, *args):
        if args and args[0] == "config":
            return "https://github.com/example/repo"
        if args and args[0] == "rev-parse":
            return "HEAD"
        return None

    monkeypatch.setattr(cursor_helm, "git_output", fake_git)
    repo, branch = cursor_helm._infer_git_context(Path("/does/not/matter"))
    assert repo == "https://github.com/example/repo"
    assert branch is None
