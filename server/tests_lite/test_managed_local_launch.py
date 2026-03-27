from __future__ import annotations

import os
import shlex
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.auth.managed_local_hook_tokens import validate_managed_local_hook_token
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPresence
from zerg.models.agents import SessionRuntimeState
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.services.managed_local_launcher import _build_entry_command
from zerg.services.managed_local_launcher import _build_hooks_ensure_command
from zerg.services.managed_local_launcher import _build_preflight_command
from zerg.services.managed_local_tmux import MANAGED_LOCAL_TMUX_SERVER_LABEL
from zerg.services.managed_local_tmux import MANAGED_LOCAL_TMUX_HISTORY_LIMIT


def _inner_command(command: str) -> str:
    parts = shlex.split(command)
    if parts[:2] == ["zsh", "-lc"]:
        return parts[2]
    return command


def _make_db(tmp_path):
    db_path = tmp_path / "test_managed_local_launch.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _make_client(db_session, current_user):
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    def override_current_user():
        return current_user

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[get_current_oikos_user] = override_current_user
    return TestClient(app, backend="asyncio"), api_app


def _make_device_client(db_session, device_token):
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    def override_device_token():
        return device_token

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = override_device_token
    return TestClient(app, backend="asyncio"), api_app


def _seed_user_and_runner(db):
    user = User(email="managed-local@test.local", role=UserRole.USER.value)
    db.add(user)
    db.commit()
    db.refresh(user)

    runner = Runner(
        owner_id=user.id,
        name="cinder",
        availability_policy="always_on",
        capabilities=["exec.full"],
        status="online",
        auth_secret_hash="secret-hash",
        runner_metadata={"install_mode": "desktop"},
    )
    db.add(runner)
    db.commit()
    db.refresh(runner)
    return user, runner


class _FakeDispatcher:
    def __init__(
        self,
        verify_exit_code: int = 0,
        *,
        preflight_tmux_tmpdir: str | None = None,
        pane_command: str = "claude",
        capture_stdout: str = "Claude ready",
        hook_install_exit_code: int = 0,
        hook_install_stdout: str = "",
        hook_install_stderr: str = "",
    ):
        self.calls: list[dict] = []
        self.verify_exit_code = verify_exit_code
        self.preflight_tmux_tmpdir = preflight_tmux_tmpdir
        self.pane_command = pane_command
        self.capture_stdout = capture_stdout
        self.hook_install_exit_code = hook_install_exit_code
        self.hook_install_stdout = hook_install_stdout
        self.hook_install_stderr = hook_install_stderr

    async def dispatch_job(self, *, db, owner_id, runner_id, command, timeout_secs, commis_id, run_id):
        self.calls.append(
            {
                "owner_id": owner_id,
                "runner_id": runner_id,
                "command": command,
                "timeout_secs": timeout_secs,
            }
        )
        inner = _inner_command(command)
        if "__LONGHOUSE_TMUX_TMPDIR__=" in inner:
            return {
                "ok": True,
                "data": {
                    "exit_code": 0,
                    "stdout": f"__LONGHOUSE_TMUX_TMPDIR__={self.preflight_tmux_tmpdir or ''}\n",
                    "stderr": "",
                },
            }
        if "longhouse connect --hooks-only" in inner:
            return {
                "ok": True,
                "data": {
                    "exit_code": self.hook_install_exit_code,
                    "stdout": self.hook_install_stdout,
                    "stderr": self.hook_install_stderr,
                },
            }
        if f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} has-session" in inner:
            return {
                "ok": True,
                "data": {
                    "exit_code": self.verify_exit_code,
                    "stdout": "",
                    "stderr": "" if self.verify_exit_code == 0 else "failed to find session",
                },
            }
        if f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} display-message" in inner:
            return {
                "ok": True,
                "data": {
                    "exit_code": 0,
                    "stdout": self.pane_command,
                    "stderr": "",
                },
            }
        if f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} capture-pane" in inner:
            return {
                "ok": True,
                "data": {
                    "exit_code": 0,
                    "stdout": self.capture_stdout,
                    "stderr": "",
                },
            }
        return {
            "ok": True,
            "data": {
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
            },
        }


def test_build_entry_command_claude_includes_session_id():
    cmd = _build_entry_command(provider="claude", provider_session_id="abc-123", display_name=None)
    inner = _inner_command(cmd)
    assert "export LONGHOUSE_SESSION_ID=abc-123" in inner
    assert 'export PATH="$HOME/.local/bin:$HOME/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin:$PATH"' in inner
    assert "if ! command -v claude-code >/dev/null 2>&1; then source ~/.zshrc >/dev/null 2>&1 || true; fi" in inner
    assert "claude-code --session-id abc-123" in inner
    assert "codex" not in inner


def test_build_entry_command_claude_includes_hook_target_overrides():
    cmd = _build_entry_command(
        provider="claude",
        provider_session_id="abc-123",
        display_name=None,
        hook_url="https://david010.longhouse.ai",
        hook_token="zdt_live_token",
    )
    inner = _inner_command(cmd)
    assert "export LONGHOUSE_SESSION_ID=abc-123" in inner
    assert "export LONGHOUSE_HOOK_URL=https://david010.longhouse.ai" in inner
    assert "export LONGHOUSE_HOOK_TOKEN=zdt_live_token" in inner
    assert "claude-code --session-id abc-123" in inner


def test_build_entry_command_codex_injects_longhouse_session_id():
    cmd = _build_entry_command(
        provider="codex",
        provider_session_id="abc-123",
        display_name=None,
        managed_session_name="lh-zerg-codex",
    )
    inner = _inner_command(cmd)
    assert inner.endswith("exec codex --enable codex_hooks --no-alt-screen")
    assert "claude-code" not in inner
    assert "--session-id" not in inner
    assert "export LONGHOUSE_SESSION_ID=" in inner
    assert 'export PATH="$HOME/.local/bin:$HOME/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin:$PATH"' in inner
    assert "if ! command -v codex >/dev/null 2>&1; then source ~/.zshrc >/dev/null 2>&1 || true; fi" in inner
    assert "abc-123" in inner
    assert "codex app-server" not in inner
    assert "--remote" not in inner


def test_build_entry_command_codex_does_not_depend_on_remote_tui():
    cmd = _build_entry_command(
        provider="codex",
        provider_session_id="abc-123",
        display_name=None,
        managed_session_name="lh-zerg-codex",
    )
    inner = _inner_command(cmd)
    assert "tui_app_server" not in inner
    assert "APP_SERVER_" not in inner
    assert "curl -fsS" not in inner


def test_build_preflight_command_claude_checks_claude_code():
    cmd = _build_preflight_command(provider="claude", cwd="/tmp/test")
    inner = _inner_command(cmd)
    assert 'export PATH="$HOME/.local/bin:$HOME/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin:$PATH"' in inner
    assert (
        "if ! command -v claude-code >/dev/null 2>&1 || ! command -v tmux >/dev/null 2>&1; "
        "then source ~/.zshrc >/dev/null 2>&1 || true; fi"
    ) in inner
    assert "command -v claude-code" in inner
    assert "command -v codex" not in inner


def test_build_preflight_command_codex_checks_codex():
    cmd = _build_preflight_command(provider="codex", cwd="/tmp/test")
    inner = _inner_command(cmd)
    assert (
        "if ! command -v codex >/dev/null 2>&1 || ! command -v tmux >/dev/null 2>&1; "
        "then source ~/.zshrc >/dev/null 2>&1 || true; fi"
    ) in inner
    assert "command -v codex" in inner
    assert "command -v claude-code" not in inner


def test_build_hooks_ensure_command_installs_longhouse_hooks_for_codex():
    cmd = _build_hooks_ensure_command(provider="codex")
    inner = _inner_command(cmd)
    assert 'test -x "${HOME}/.codex/hooks/longhouse-codex-hook.sh"' in inner
    assert 'test -f "${HOME}/.codex/hooks.json"' in inner
    assert 'export PATH="$HOME/.local/bin:$HOME/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin:$PATH"' in inner
    assert "if ! command -v longhouse >/dev/null 2>&1; then source ~/.zshrc >/dev/null 2>&1 || true; fi" in inner
    assert "command -v longhouse" in inner
    assert "longhouse connect --hooks-only" in inner
    assert "${HOME}/.codex/hooks/longhouse-codex-hook.sh" in inner
    assert "${HOME}/.codex/hooks.json" in inner
    assert "longhouse-codex-hook.sh" in inner


def test_launch_managed_local_session_creates_session_and_dispatches_tmux(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        client, api_app_ref = _make_client(db, user)
        dispatcher = _FakeDispatcher(preflight_tmux_tmpdir="/tmp/lh-managed-launch")

        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda owner_id, runner_id: True),
        )
        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_job_dispatcher",
            lambda: dispatcher,
        )

        try:
            response = client.post(
                "/api/sessions/managed-local",
                json={
                    "runner_target": runner.name,
                    "cwd": "/Users/davidrose/git/zeta/hiring",
                    "project": "hiring",
                    "display_name": "Hiring session",
                    "loop_mode": "assist",
                },
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["execution_home"] == "managed_local"
            assert payload["managed_transport"] == "tmux"
            assert payload["loop_mode"] == "assist"
            assert payload["source_runner_name"] == "cinder"
            attach_inner = _inner_command(payload["attach_command"])
            assert "export TMUX_TMPDIR=/tmp/lh-managed-launch" in attach_inner
            assert attach_inner.endswith(
                f"exec tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} attach -t {payload['managed_session_name']}"
            )

            session = db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).one()
            assert session.execution_home == "managed_local"
            assert session.managed_transport == "tmux"
            assert session.source_runner_id == runner.id
            assert session.source_runner_name == runner.name
            assert session.provider_session_id == payload["provider_session_id"]
            assert session.managed_session_name == payload["managed_session_name"]
            assert session.managed_tmux_tmpdir == "/tmp/lh-managed-launch"
            assert session.continuation_kind == "local"
            assert session.origin_label == runner.name

            runtime_state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session.id).one()
            assert runtime_state.phase == "idle"
            assert runtime_state.phase_source == "semantic"
            assert runtime_state.last_runtime_signal_at is not None
            assert runtime_state.freshness_expires_at is not None

            presence = db.query(SessionPresence).filter(SessionPresence.session_id == str(session.id)).one()
            assert presence.state == "idle"
            assert presence.provider == "claude"
            assert presence.cwd == session.cwd
            assert presence.project == session.project

            preflight_inner = _inner_command(dispatcher.calls[0]["command"])
            hooks_inner = _inner_command(dispatcher.calls[1]["command"])
            launch_inner = _inner_command(dispatcher.calls[2]["command"])
            has_session_inner = _inner_command(dispatcher.calls[3]["command"])
            display_inner = _inner_command(dispatcher.calls[4]["command"])

            assert len(dispatcher.calls) == 5
            assert dispatcher.calls[0]["runner_id"] == runner.id
            assert 'export PATH="$HOME/.local/bin:$HOME/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin:$PATH"' in preflight_inner
            assert "command -v tmux" in preflight_inner
            assert "command -v claude-code" in preflight_inner
            assert "printf '__LONGHOUSE_TMUX_TMPDIR__=%s\\n' \"${TMUX_TMPDIR:-}\"" in preflight_inner
            assert "if ! command -v longhouse >/dev/null 2>&1; then source ~/.zshrc >/dev/null 2>&1 || true; fi" in hooks_inner
            assert "longhouse connect --hooks-only" in hooks_inner
            assert "${HOME}/.claude/hooks/longhouse-hook.sh" in hooks_inner
            assert "${HOME}/.claude/settings.json" in hooks_inner
            assert "cat > /tmp/longhouse-managed-" in launch_inner
            assert "__LONGHOUSE_MANAGED_LOCAL__" in launch_inner
            assert "export LONGHOUSE_HOOK_URL=http://testserver" in launch_inner
            assert "if ! command -v claude-code >/dev/null 2>&1; then source ~/.zshrc >/dev/null 2>&1 || true; fi" in launch_inner
            token_fragment = launch_inner.split("export LONGHOUSE_HOOK_TOKEN=", 1)[1].split(";", 1)[0].strip()
            hook_token = shlex.split(token_fragment)[0]
            auth = validate_managed_local_hook_token(hook_token)
            assert auth is not None
            assert auth.owner_id == user.id
            assert auth.session_id == payload["session_id"]
            assert auth.project == "hiring"
            assert auth.device_id == runner.name
            assert (
                f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} start-server \\; "
                "set-option -s escape-time 0 \\; "
                "set-option -g status off \\; "
                "set-option -g mouse on \\; "
                f"set-option -g history-limit {MANAGED_LOCAL_TMUX_HISTORY_LIMIT} \\; "
                "set-option -g remain-on-exit failed \\; "
                f"new-session -d -s"
            ) in launch_inner
            assert "claude-code --session-id" in launch_inner
            assert (
                f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} has-session -t {session.managed_session_name}"
                in has_session_inner
            )
            assert (
                f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} display-message -p -t "
                f"{session.managed_session_name} '#{{pane_current_command}}'"
                in display_inner
            )
        finally:
            api_app_ref.dependency_overrides = {}


def test_launch_managed_local_session_rejects_unimplemented_transport(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        client, api_app_ref = _make_client(db, user)

        try:
            response = client.post(
                "/api/sessions/managed-local",
                json={
                    "runner_target": runner.name,
                    "cwd": "/Users/davidrose/git/zerg",
                    "provider": "codex",
                    "managed_transport": "codex_app_server",
                },
            )
            assert response.status_code == 501, response.text
            assert response.json()["detail"] == "Managed local transport 'codex_app_server' is not implemented yet"
        finally:
            api_app_ref.dependency_overrides = {}


def test_launch_managed_local_session_accepts_shell_wrapper_when_capture_has_output(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        client, api_app_ref = _make_client(db, user)
        dispatcher = _FakeDispatcher(pane_command="zsh", capture_stdout="Welcome to Claude Code")

        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda owner_id, runner_id: True),
        )
        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_job_dispatcher",
            lambda: dispatcher,
        )

        try:
            response = client.post(
                "/api/sessions/managed-local",
                json={
                    "runner_target": runner.name,
                    "cwd": "/Users/davidrose/git/zeta/hiring",
                    "project": "hiring",
                },
            )
            assert response.status_code == 200, response.text
            assert len(dispatcher.calls) == 6
            assert "capture-pane" in _inner_command(dispatcher.calls[5]["command"])
        finally:
            api_app_ref.dependency_overrides = {}


def test_launch_managed_local_session_rejects_shell_wrapper_startup_error(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        client, api_app_ref = _make_client(db, user)
        dispatcher = _FakeDispatcher(pane_command="zsh", capture_stdout="zsh: command not found: claude-code")

        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda owner_id, runner_id: True),
        )
        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_job_dispatcher",
            lambda: dispatcher,
        )

        try:
            response = client.post(
                "/api/sessions/managed-local",
                json={
                    "runner_target": runner.name,
                    "cwd": "/Users/davidrose/git/zeta/hiring",
                },
            )
            assert response.status_code == 424, response.text
            assert "failed to start Claude" in response.json()["detail"]
        finally:
            api_app_ref.dependency_overrides = {}


def test_launch_managed_local_session_rolls_back_when_tmux_verify_fails(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        client, api_app_ref = _make_client(db, user)
        dispatcher = _FakeDispatcher(verify_exit_code=1)

        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda owner_id, runner_id: True),
        )
        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_job_dispatcher",
            lambda: dispatcher,
        )

        try:
            response = client.post(
                "/api/sessions/managed-local",
                json={
                    "runner_target": runner.name,
                    "cwd": "/Users/davidrose/git/zeta/hiring",
                },
            )
            assert response.status_code == 424, response.text
            assert "failed to find session" in response.json()["detail"]
            assert db.query(AgentSession).count() == 0
            assert len(dispatcher.calls) == 5
            assert dispatcher.calls[-1]["command"].startswith(
                "zsh -lc "
            )
            assert (
                f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} kill-session -t lh-hiring-"
                in _inner_command(dispatcher.calls[-1]["command"])
            )
        finally:
            api_app_ref.dependency_overrides = {}


def test_launch_managed_local_this_device_uses_machine_name_override(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        runner.name = "work-laptop"
        db.commit()
        db.refresh(runner)

        device_token = SimpleNamespace(owner_id=user.id, device_id="host-123", id="token-1")
        client, api_app_ref = _make_device_client(db, device_token)
        dispatcher = _FakeDispatcher(preflight_tmux_tmpdir="/tmp/lh-managed-launch")

        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda owner_id, runner_id: True),
        )
        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_job_dispatcher",
            lambda: dispatcher,
        )

        try:
            response = client.post(
                "/api/sessions/managed-local/this-device",
                headers={"X-Agents-Token": "zdt_test_token"},
                json={
                    "machine_name": "work-laptop",
                    "cwd": "/Users/davidrose/git/zeta/hiring",
                    "project": "hiring",
                    "display_name": "Hiring session",
                    "loop_mode": "assist",
                },
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["source_runner_name"] == "work-laptop"

            session = db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).one()
            assert session.source_runner_id == runner.id
            assert session.source_runner_name == "work-laptop"
            assert dispatcher.calls[0]["owner_id"] == user.id
            assert dispatcher.calls[0]["runner_id"] == runner.id
            launch_inner = _inner_command(dispatcher.calls[2]["command"])
            assert "export LONGHOUSE_HOOK_URL=http://testserver" in launch_inner
            token_fragment = launch_inner.split("export LONGHOUSE_HOOK_TOKEN=", 1)[1].split(";", 1)[0].strip()
            hook_token = shlex.split(token_fragment)[0]
            auth = validate_managed_local_hook_token(hook_token)
            assert auth is not None
            assert auth.owner_id == user.id
            assert auth.session_id == payload["session_id"]
            assert auth.project == "hiring"
            assert auth.device_id == "work-laptop"
        finally:
            api_app_ref.dependency_overrides = {}


def test_launch_managed_local_this_device_prefers_forwarded_https_hook_url(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        runner.name = "cinder"
        db.commit()
        db.refresh(runner)

        device_token = SimpleNamespace(owner_id=user.id, device_id="cinder", id="token-1")
        client, api_app_ref = _make_device_client(db, device_token)
        dispatcher = _FakeDispatcher(preflight_tmux_tmpdir="/tmp/lh-managed-launch")

        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda owner_id, runner_id: True),
        )
        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_job_dispatcher",
            lambda: dispatcher,
        )

        try:
            response = client.post(
                "/api/sessions/managed-local/this-device",
                headers={
                    "X-Agents-Token": "zdt_test_token",
                    "host": "david010.longhouse.ai",
                    "x-forwarded-proto": "https",
                },
                json={
                    "machine_name": "cinder",
                    "cwd": "/Users/davidrose/git/zeta/hiring",
                    "project": "hiring",
                    "display_name": "Hiring session",
                    "loop_mode": "assist",
                },
            )
            assert response.status_code == 200, response.text
            launch_inner = _inner_command(dispatcher.calls[2]["command"])
            assert "export LONGHOUSE_HOOK_URL=https://david010.longhouse.ai" in launch_inner
        finally:
            api_app_ref.dependency_overrides = {}


def test_launch_managed_local_codex_session(monkeypatch, tmp_path):
    """Launching with provider=codex creates a codex session with codex-specific preflight."""
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        client, api_app_ref = _make_client(db, user)
        dispatcher = _FakeDispatcher(pane_command="codex", capture_stdout="Codex ready")

        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda owner_id, runner_id: True),
        )
        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_job_dispatcher",
            lambda: dispatcher,
        )

        try:
            response = client.post(
                "/api/sessions/managed-local",
                json={
                    "runner_target": runner.name,
                    "cwd": "/Users/davidrose/git/zerg",
                    "project": "zerg",
                    "provider": "codex",
                },
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["provider"] == "codex"
            assert payload["execution_home"] == "managed_local"

            session = db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).one()
            assert session.provider == "codex"
            assert session.execution_home == "managed_local"

            presence = db.query(SessionPresence).filter(SessionPresence.session_id == str(session.id)).one()
            assert presence.state == "idle"
            assert presence.provider == "codex"
            assert presence.cwd == session.cwd
            assert presence.project == session.project

            preflight_inner = _inner_command(dispatcher.calls[0]["command"])
            assert "command -v codex" in preflight_inner
            assert "command -v claude-code" not in preflight_inner

            hooks_inner = _inner_command(dispatcher.calls[1]["command"])
            assert "longhouse connect --hooks-only" in hooks_inner
            assert "${HOME}/.codex/hooks/longhouse-codex-hook.sh" in hooks_inner
            assert "${HOME}/.codex/hooks.json" in hooks_inner

            launch_inner = _inner_command(dispatcher.calls[2]["command"])
            assert "exec codex --enable codex_hooks --no-alt-screen" in launch_inner
            assert "claude-code" not in launch_inner

            # Must inject LONGHOUSE_SESSION_ID so hook routes presence to Longhouse's UUID
            assert "LONGHOUSE_SESSION_ID" in launch_inner
            assert payload["provider_session_id"] in launch_inner
            assert "codex app-server" not in launch_inner
            assert "--remote" not in launch_inner
            assert len(dispatcher.calls) == 4
            assert "has-session" in _inner_command(dispatcher.calls[3]["command"])
        finally:
            api_app_ref.dependency_overrides = {}


def test_launch_managed_local_codex_fails_when_hook_install_does_not_produce_hooks(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        client, api_app_ref = _make_client(db, user)
        dispatcher = _FakeDispatcher(
            hook_install_exit_code=18,
            hook_install_stderr="Longhouse hook config is missing the expected hook entry",
        )

        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda owner_id, runner_id: True),
        )
        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_job_dispatcher",
            lambda: dispatcher,
        )

        try:
            response = client.post(
                "/api/sessions/managed-local",
                json={
                    "runner_target": runner.name,
                    "cwd": "/Users/davidrose/git/zerg",
                    "provider": "codex",
                },
            )
            assert response.status_code == 424, response.text
            assert response.json()["detail"] == "Longhouse hook config is missing the expected hook entry"
            assert db.query(AgentSession).count() == 0
            assert len(dispatcher.calls) == 2
            assert "longhouse connect --hooks-only" in _inner_command(dispatcher.calls[1]["command"])
        finally:
            api_app_ref.dependency_overrides = {}


def test_launch_managed_local_rejects_invalid_provider(monkeypatch, tmp_path):
    """Launching with an unsupported provider returns 400."""
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        client, api_app_ref = _make_client(db, user)
        dispatcher = _FakeDispatcher()

        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda owner_id, runner_id: True),
        )
        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_job_dispatcher",
            lambda: dispatcher,
        )

        try:
            response = client.post(
                "/api/sessions/managed-local",
                json={
                    "runner_target": runner.name,
                    "cwd": "/Users/davidrose/git/zerg",
                    "provider": "gemini",
                },
            )
            assert response.status_code == 400, response.text
            assert "Unsupported provider" in response.json()["detail"]
        finally:
            api_app_ref.dependency_overrides = {}


def test_launch_managed_local_codex_returns_without_waiting_for_ui_boot(monkeypatch, tmp_path):
    """Managed-local Codex should return once tmux is live, not after TUI readiness."""
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        client, api_app_ref = _make_client(db, user)
        dispatcher = _FakeDispatcher(pane_command="zsh", capture_stdout="zsh: command not found: codex")

        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda owner_id, runner_id: True),
        )
        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_job_dispatcher",
            lambda: dispatcher,
        )

        try:
            response = client.post(
                "/api/sessions/managed-local",
                json={
                    "runner_target": runner.name,
                    "cwd": "/Users/davidrose/git/zerg",
                    "provider": "codex",
                },
            )
            assert response.status_code == 200, response.text
            assert len(dispatcher.calls) == 4
            assert all("display-message" not in _inner_command(call["command"]) for call in dispatcher.calls)
            assert all("capture-pane" not in _inner_command(call["command"]) for call in dispatcher.calls)
        finally:
            api_app_ref.dependency_overrides = {}


def test_launch_managed_local_codex_ignores_bootstrap_script_probe_on_launch(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        client, api_app_ref = _make_client(db, user)
        dispatcher = _FakeDispatcher(
            pane_command="longhouse-managed-lh-Codex-CLI-smoke-3649a5df.zsh",
            capture_stdout="",
        )

        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda owner_id, runner_id: True),
        )
        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_job_dispatcher",
            lambda: dispatcher,
        )

        try:
            response = client.post(
                "/api/sessions/managed-local",
                json={
                    "runner_target": runner.name,
                    "cwd": "/Users/davidrose/git/zerg",
                    "project": "zerg",
                    "provider": "codex",
                },
            )
            assert response.status_code == 200, response.text
            assert len(dispatcher.calls) == 4
            assert all("display-message" not in _inner_command(call["command"]) for call in dispatcher.calls)
        finally:
            api_app_ref.dependency_overrides = {}


def test_launch_managed_local_this_device_falls_back_from_stale_token_owner(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        device_token = SimpleNamespace(owner_id=user.id + 999, device_id="cinder", id="token-stale-owner")
        client, api_app_ref = _make_device_client(db, device_token)
        dispatcher = _FakeDispatcher(preflight_tmux_tmpdir="/tmp/lh-managed-launch")

        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda owner_id, runner_id: True),
        )
        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_job_dispatcher",
            lambda: dispatcher,
        )

        try:
            response = client.post(
                "/api/sessions/managed-local/this-device",
                headers={"X-Agents-Token": "zdt_test_token"},
                json={
                    "cwd": "/Users/davidrose/git/zeta/hiring",
                    "project": "hiring",
                    "display_name": "Hiring session",
                    "loop_mode": "assist",
                    "machine_name": "cinder",
                },
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["source_runner_name"] == "cinder"

            session = db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).one()
            assert session.source_runner_id == runner.id
            assert dispatcher.calls[0]["owner_id"] == user.id
            assert dispatcher.calls[0]["runner_id"] == runner.id
        finally:
            api_app_ref.dependency_overrides = {}
