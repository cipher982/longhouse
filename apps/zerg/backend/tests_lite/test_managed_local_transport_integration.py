from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
import textwrap
import time
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionRuntimeState
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.services.managed_local_control import send_text_to_managed_local_session
from zerg.services.managed_local_tmux import build_tmux_capture_command
from zerg.services.managed_local_tmux import build_tmux_kill_session_command


def _make_db(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test_managed_local_transport.db'}")
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


def _seed_user_and_runner(db):
    user = User(email="managed-local-transport@test.local", role=UserRole.USER.value)
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


class _LocalExecDispatcher:
    """Executes runner commands locally so tmux transport is real in this test.

    This still mocks the remote runner boundary. It intentionally does not mock:
    - tmux session launch
    - tmux send-keys input
    - the process running inside tmux
    """

    def __init__(self, *, env: dict[str, str]) -> None:
        self.env = env
        self.calls: list[dict[str, object]] = []

    async def dispatch_job(self, *, db, owner_id, runner_id, command, timeout_secs, commis_id, run_id):
        self.calls.append(
            {
                "owner_id": owner_id,
                "runner_id": runner_id,
                "command": command,
                "timeout_secs": timeout_secs,
                "commis_id": commis_id,
                "run_id": run_id,
            }
        )

        if " display-message " in command:
            return await asyncio.to_thread(
                self._run_with_settle_poll,
                command=command,
                timeout_secs=timeout_secs,
            )

        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                shell=True,
                executable="/bin/zsh",
                capture_output=True,
                text=True,
                env=self.env,
                timeout=timeout_secs,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": {"message": f"Timed out after {timeout_secs}s"},
            }

        return {
            "ok": True,
            "data": {
                "exit_code": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            },
        }

    def _run_with_settle_poll(self, *, command: str, timeout_secs: int) -> dict[str, object]:
        deadline = time.monotonic() + timeout_secs
        latest: subprocess.CompletedProcess[str] | None = None
        while time.monotonic() < deadline:
            latest = subprocess.run(
                command,
                shell=True,
                executable="/bin/zsh",
                capture_output=True,
                text=True,
                env=self.env,
                timeout=timeout_secs,
            )
            pane_command = (latest.stdout or "").strip().lower()
            if pane_command not in {"", "bash", "sh", "zsh", "fish"}:
                break
            time.sleep(0.1)

        if latest is None:
            return {
                "ok": False,
                "error": {"message": f"Timed out after {timeout_secs}s"},
            }

        return {
            "ok": True,
            "data": {
                "exit_code": latest.returncode,
                "stdout": latest.stdout,
                "stderr": latest.stderr,
            },
        }


def _make_fake_claude_home(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    home = tmp_path / "fake-home"
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / "fake-claude.log"
    tmux_tmpdir = Path("/tmp") / f"lh-tmux-{uuid4().hex[:8]}"
    tmux_tmpdir.mkdir(parents=True, exist_ok=True)

    node_impl = textwrap.dedent(
        """\
        import fs from "node:fs";

        const args = process.argv.slice(2);
        let sessionId = null;
        let displayName = null;

        for (let i = 0; i < args.length; i += 1) {
          if (args[i] === "--session-id" && i + 1 < args.length) {
            sessionId = args[i + 1];
          }
          if (args[i] === "-n" && i + 1 < args.length) {
            displayName = args[i + 1];
          }
        }

        const logPath = process.env.FAKE_CLAUDE_LOG;
        if (logPath) {
          fs.appendFileSync(logPath, `START session=${sessionId} name=${displayName}\\n`, "utf8");
        }

        console.log(`FAKE_CLAUDE_START session=${sessionId} name=${displayName}`);

        process.stdin.setEncoding("utf8");
        process.stdin.on("data", (chunk) => {
          for (const rawLine of chunk.split(/\\r?\\n/)) {
            const line = rawLine.trimEnd();
            if (!line) continue;
            if (logPath) {
              fs.appendFileSync(logPath, `USER:${line}\\n`, "utf8");
            }
            console.log(`USER:${line}`);
            if (line.toLowerCase().includes("continue")) {
              console.log("ASSISTANT: continuing exact managed local session");
            } else {
              console.log(`ASSISTANT: received ${line}`);
            }
          }
        });
        """
    )
    impl_path = bin_dir / "fake-claude.mjs"
    impl_path.write_text(node_impl, encoding="utf-8")

    launcher = textwrap.dedent(
        f"""\
        #!/bin/zsh
        exec node {impl_path} "$@"
        """
    )

    claude_path = bin_dir / "claude-code"
    claude_path.write_text(launcher, encoding="utf-8")
    claude_path.chmod(claude_path.stat().st_mode | stat.S_IXUSR)

    (home / ".zshrc").write_text(
        f'export PATH="{bin_dir}:$PATH"\nexport FAKE_CLAUDE_LOG="{log_path}"\n',
        encoding="utf-8",
    )

    env = {
        "HOME": str(home),
        "ZDOTDIR": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TMUX_TMPDIR": str(tmux_tmpdir),
    }

    return home, log_path, env


async def _capture_tmux_output(dispatcher: _LocalExecDispatcher, *, session_name: str) -> str:
    result = await dispatcher.dispatch_job(
        db=None,
        owner_id=0,
        runner_id=0,
        command=build_tmux_capture_command(session_name=session_name, lines=80),
        timeout_secs=5,
        commis_id=None,
        run_id=None,
    )
    assert result["ok"] is True
    return str(result["data"]["stdout"])


async def _wait_for_tmux_text(
    dispatcher: _LocalExecDispatcher,
    *,
    session_name: str,
    needle: str,
    timeout_secs: float = 5.0,
) -> str:
    deadline = time.monotonic() + timeout_secs
    latest = ""
    while time.monotonic() < deadline:
        latest = await _capture_tmux_output(dispatcher, session_name=session_name)
        if needle in latest:
            return latest
        await asyncio.sleep(0.1)
    raise AssertionError(f"Did not observe {needle!r} in tmux pane output.\nLatest output:\n{latest}")


@pytest.mark.skipif(
    shutil.which("tmux") is None or shutil.which("zsh") is None,
    reason="Requires local tmux + zsh for the managed-local transport canary.",
)
def test_managed_local_launch_and_send_text_use_real_tmux_transport(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    _home, log_path, launcher_env = _make_fake_claude_home(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    dispatcher = _LocalExecDispatcher(env={**os.environ, **launcher_env})
    managed_session_name: str | None = None

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        client, api_app_ref = _make_client(db, user)

        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_connection_manager",
            lambda: type("Conn", (), {"is_online": staticmethod(lambda owner_id, runner_id: True)})(),
        )
        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_job_dispatcher",
            lambda: dispatcher,
        )
        monkeypatch.setattr(
            "zerg.services.managed_local_control.get_runner_job_dispatcher",
            lambda: dispatcher,
        )

        try:
            response = client.post(
                "/api/sessions/managed-local",
                json={
                    "runner_target": runner.name,
                    "cwd": str(workspace),
                    "project": "managed-local-proof",
                    "display_name": "Managed Local Proof",
                    "loop_mode": "assist",
                },
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            managed_session_name = payload["managed_session_name"]

            session = db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).one()
            assert session.execution_home == "managed_local"
            assert session.source_runner_name == "cinder"

            startup_output = asyncio.run(
                _wait_for_tmux_text(
                    dispatcher,
                    session_name=managed_session_name,
                    needle="FAKE_CLAUDE_START",
                )
            )
            normalized_startup = startup_output.replace("\n", "")
            assert f"session={payload['provider_session_id']}" in normalized_startup
            assert "name=Managed Local Proof" in normalized_startup

            send_result = asyncio.run(
                send_text_to_managed_local_session(
                    db=db,
                    owner_id=user.id,
                    session=session,
                    text="Continue from Loop now",
                    commis_id="managed-local-transport-canary",
                )
            )
            assert send_result.ok is True

            turn_output = asyncio.run(
                _wait_for_tmux_text(
                    dispatcher,
                    session_name=managed_session_name,
                    needle="ASSISTANT: continuing exact managed local session",
                )
            )
            assert "USER:Continue from Loop now" in turn_output

            runtime_state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session.id).one()
            assert runtime_state.phase == "thinking"
            assert runtime_state.phase_source == "semantic"

            log_text = log_path.read_text(encoding="utf-8")
            assert "START session=" in log_text
            assert "USER:Continue from Loop now" in log_text
        finally:
            if managed_session_name:
                asyncio.run(
                    dispatcher.dispatch_job(
                        db=db,
                        owner_id=user.id,
                        runner_id=runner.id,
                        command=build_tmux_kill_session_command(session_name=managed_session_name),
                        timeout_secs=5,
                        commis_id=None,
                        run_id=None,
                    )
                )
            api_app_ref.dependency_overrides = {}
