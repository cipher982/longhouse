"""Startup behavior a first-time self-hoster meets (2026-09-30 cold-install proof).

- `serve` after `serve --stop` reported "Port already in use" for a port nothing listened on;
- a public bind with nothing configured crashed with a 30-line traceback;
- `serve --daemon` returned success while the daemon had already died.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from types import ModuleType
from unittest import mock

from typer.testing import CliRunner

from zerg.cli import serve as serve_cli


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_port_probe_sees_a_listener_but_not_time_wait():
    port = _free_port()
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(1)
    try:
        assert serve_cli._port_is_free("127.0.0.1", port) is False

        # The server closes first, as it does on `serve --stop`, leaving TIME_WAIT on its port.
        client = socket.create_connection(("127.0.0.1", port))
        accepted, _ = listener.accept()
        accepted.close()
        client.close()
    finally:
        listener.close()

    plain = socket.socket()  # what the probe used to be: no SO_REUSEADDR
    try:
        try:
            plain.bind(("127.0.0.1", port))
        except OSError:
            pass  # the TIME_WAIT the old probe tripped over
        else:  # platform without TIME_WAIT on this path: nothing to prove
            return
    finally:
        plain.close()
    assert serve_cli._port_is_free("127.0.0.1", port) is True


def test_port_probe_never_raises_for_a_host_it_cannot_resolve():
    assert serve_cli._port_is_free("no-such-host.invalid", _free_port()) in (True, False)


def test_port_probe_handles_an_ipv6_literal():
    try:
        listener = socket.socket(socket.AF_INET6)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("::1", 0))
    except OSError:
        return  # no IPv6 loopback here
    port = listener.getsockname()[1]
    listener.listen(1)
    try:
        assert serve_cli._port_is_free("::1", port) is False
    finally:
        listener.close()
    assert serve_cli._port_is_free("::1", port) is True


def test_daemon_health_probe_ignores_proxy_settings(monkeypatch):
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing listens: a proxied probe would fail
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    server = HTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ok, _ = serve_cli._await_daemon_ready("127.0.0.1", server.server_port, timeout=5)
    finally:
        server.shutdown()
        server.server_close()
    assert ok is True


def test_pid_file_being_written_is_not_a_dead_server(tmp_path, monkeypatch):
    pid_file = tmp_path / "server.pid"
    monkeypatch.setattr(serve_cli, "_get_pid_file", lambda: pid_file)

    pid_file.write_text("")  # mid-write
    assert serve_cli._is_server_running() == (False, None)
    assert pid_file.exists()  # not deleted out from under the daemon writing it

    pid_file.write_text(str(os.getpid()))
    assert serve_cli._is_server_running() == (True, os.getpid())

    dead = os.fork()
    if dead == 0:
        os._exit(0)
    os.waitpid(dead, 0)
    pid_file.write_text(str(dead))
    assert serve_cli._is_server_running() == (False, None)
    assert not pid_file.exists()  # a genuinely dead pid is still cleaned up


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):  # silence
        return


def test_daemon_ready_when_it_answers():
    server = HTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ok, detail = serve_cli._await_daemon_ready("127.0.0.1", server.server_port, timeout=5)
    finally:
        server.shutdown()
        server.server_close()
    assert (ok, detail) == (True, "")


def test_daemon_not_ready_when_its_process_is_gone(tmp_path, monkeypatch):
    pid_file = tmp_path / "server.pid"
    dead = os.fork()
    if dead == 0:
        os._exit(0)
    os.waitpid(dead, 0)
    pid_file.write_text(str(dead))
    monkeypatch.setattr(serve_cli, "_get_pid_file", lambda: pid_file)

    ok, detail = serve_cli._await_daemon_ready("127.0.0.1", _free_port(), timeout=5)

    assert ok is False
    assert "exited during startup" in detail


def test_daemon_wait_survives_a_half_written_pid_file(tmp_path, monkeypatch):
    pid_file = tmp_path / "server.pid"
    pid_file.write_text("")  # the daemon has created the file but not written its pid yet
    monkeypatch.setattr(serve_cli, "_get_pid_file", lambda: pid_file)
    port = _free_port()

    def start_late():
        threading.Event().wait(0.8)
        server = HTTPServer(("127.0.0.1", port), _Health)
        started.append(server)
        server.serve_forever()

    started: list[HTTPServer] = []
    threading.Thread(target=start_late, daemon=True).start()
    try:
        ok, detail = serve_cli._await_daemon_ready("127.0.0.1", port, timeout=10)
    finally:
        for server in started:
            server.shutdown()
            server.server_close()
    assert (ok, detail) == (True, "")


class _RejectingMain(ModuleType):
    """zerg.main as it behaves when the environment fails validation on import."""

    def __getattr__(self, name):
        raise RuntimeError(
            "CRITICAL: Missing required environment variables: JWT_SECRET (must be >=16 chars, not 'dev-secret')\n"
            "Set these in your .env file or deployment environment.\n"
            "Current DATABASE_URL: 'sqlite:///x.db'\n"
            "LLM available: False\n"
            "Deployment will fail without these variables."
        )


def test_public_bind_with_nothing_configured_explains_instead_of_crashing(monkeypatch):
    monkeypatch.setitem(sys.modules, "zerg.main", _RejectingMain("zerg.main"))
    env = {"DATABASE_URL": "sqlite:///:memory:"}
    with (
        mock.patch.dict(os.environ, env, clear=False),
        mock.patch("uvicorn.run") as uvicorn_run,
        mock.patch("zerg.cli.serve._get_lan_ip", return_value=None),
        mock.patch("zerg.cli.acquisition.emit_acquisition_event_once"),
    ):
        for var in ("AUTH_DISABLED", "TESTING", "DEMO_MODE", "APP_MODE"):
            os.environ.pop(var, None)
        result = CliRunner().invoke(serve_cli.app, ["serve", "--host", "0.0.0.0", "--port", str(_free_port())])

    assert result.exit_code == 1
    assert not uvicorn_run.called
    assert "Traceback" not in result.output
    assert "JWT_SECRET" in result.output
    assert 'LONGHOUSE_PASSWORD_HASH="$(longhouse-server hash-password)"' in result.output
