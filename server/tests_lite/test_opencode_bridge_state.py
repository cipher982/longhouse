from __future__ import annotations

import json
import os

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-value")

from zerg.services import opencode_bridge_state as bs


def test_parse_listen_line_extracts_url():
    assert bs.parse_listen_line("opencode server listening on http://127.0.0.1:4096") == "http://127.0.0.1:4096"
    assert (
        bs.parse_listen_line("[ts] opencode server listening on http://0.0.0.0:54321/")
        == "http://0.0.0.0:54321"
    )


def test_parse_listen_line_returns_none_for_unrelated():
    assert bs.parse_listen_line("starting server...") is None
    assert bs.parse_listen_line("") is None
    assert bs.parse_listen_line("opencode server listening on garbage") is None


def test_generate_server_password_is_unique_and_nonempty():
    a = bs.generate_server_password()
    b = bs.generate_server_password()
    assert a and b and a != b


def test_state_round_trip(tmp_path):
    state_root = tmp_path / "bridge"
    written = bs.write_opencode_bridge_state(
        session_id="sess-1",
        server_url="http://127.0.0.1:9000/",
        server_password="hunter2",
        cwd=str(tmp_path),
        opencode_pid=4242,
        opencode_session_id="oc-session-abc",
        state_root=state_root,
    )
    assert written.exists()
    assert oct(written.stat().st_mode & 0o777) == "0o600"

    loaded = bs.read_opencode_bridge_state(session_id="sess-1", state_root=state_root)
    assert loaded["server_url"] == "http://127.0.0.1:9000"
    assert loaded["server_password"] == "hunter2"
    assert loaded["server_username"] == "opencode"
    assert loaded["opencode_pid"] == 4242
    assert loaded["opencode_session_id"] == "oc-session-abc"
    assert loaded["ready"] is True


def test_write_state_rejects_bad_inputs(tmp_path):
    with pytest.raises(ValueError):
        bs.write_opencode_bridge_state(
            session_id="",
            server_url="http://127.0.0.1",
            server_password="x",
            cwd=str(tmp_path),
            opencode_pid=1,
            state_root=tmp_path,
        )
    with pytest.raises(ValueError):
        bs.write_opencode_bridge_state(
            session_id="s",
            server_url="ftp://wrong",
            server_password="x",
            cwd=str(tmp_path),
            opencode_pid=1,
            state_root=tmp_path,
        )
    with pytest.raises(ValueError):
        bs.write_opencode_bridge_state(
            session_id="s",
            server_url="http://127.0.0.1",
            server_password="",
            cwd=str(tmp_path),
            opencode_pid=1,
            state_root=tmp_path,
        )


def test_remove_state_is_idempotent(tmp_path):
    bs.remove_opencode_bridge_state(session_id="not-there", state_root=tmp_path)  # no error

    bs.write_opencode_bridge_state(
        session_id="sess",
        server_url="http://127.0.0.1:1",
        server_password="p",
        cwd=str(tmp_path),
        opencode_pid=1,
        state_root=tmp_path,
    )
    bs.remove_opencode_bridge_state(session_id="sess", state_root=tmp_path)
    state_path = bs.build_opencode_bridge_state_file(session_id="sess", state_root=tmp_path)
    assert not state_path.exists()


def test_wait_for_state_returns_when_ready(tmp_path):
    bs.write_opencode_bridge_state(
        session_id="sess",
        server_url="http://127.0.0.1:1",
        server_password="p",
        cwd=str(tmp_path),
        opencode_pid=1,
        state_root=tmp_path,
    )
    state = bs.wait_for_opencode_bridge_state(session_id="sess", timeout_secs=1.0, state_root=tmp_path)
    assert state["ready"] is True


def test_wait_for_state_times_out(tmp_path):
    with pytest.raises(FileNotFoundError):
        bs.wait_for_opencode_bridge_state(session_id="missing", timeout_secs=0.2, state_root=tmp_path)


def test_write_state_uses_atomic_rename(tmp_path):
    """A failed write must not leave a half-written state file in place."""

    bs.write_opencode_bridge_state(
        session_id="sess",
        server_url="http://127.0.0.1:1",
        server_password="first-password",
        cwd=str(tmp_path),
        opencode_pid=1,
        state_root=tmp_path,
    )
    state_path = bs.build_opencode_bridge_state_file(session_id="sess", state_root=tmp_path)
    original_text = state_path.read_text()

    # Force fdopen.write to fail mid-publish: this simulates an interrupted
    # second writer. The original file must remain readable and intact.
    real_fdopen = os.fdopen

    def boom(*args, **kwargs):
        fh = real_fdopen(*args, **kwargs)

        class _BoomWriter:
            def __getattr__(self, name):
                return getattr(fh, name)

            def write(self, *_a, **_kw):
                raise OSError("disk full")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                fh.close()
                return False

        return _BoomWriter()

    import zerg.services.opencode_bridge_state as svc

    svc.os.fdopen = boom  # type: ignore[attr-defined]
    try:
        with pytest.raises(OSError):
            bs.write_opencode_bridge_state(
                session_id="sess",
                server_url="http://127.0.0.1:1",
                server_password="second-password",
                cwd=str(tmp_path),
                opencode_pid=2,
                state_root=tmp_path,
            )
    finally:
        svc.os.fdopen = real_fdopen  # type: ignore[attr-defined]

    # Original payload survived.
    assert state_path.read_text() == original_text
    # No leftover temp files.
    leftovers = [p for p in state_path.parent.iterdir() if ".tmp." in p.name]
    assert leftovers == []
