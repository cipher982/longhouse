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
from zerg.services.managed_local_tmux import build_tmux_has_session_command
from zerg.services.managed_local_tmux import build_tmux_kill_session_command
from zerg.services.managed_local_tmux import build_tmux_launch_command


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
    bin_dir = home / ".managed-local-shell-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / "fake-claude.log"
    tmux_tmpdir = Path("/tmp") / f"lh-tmux-{uuid4().hex[:8]}"
    tmux_tmpdir.mkdir(parents=True, exist_ok=True)
    tmux_bin = shutil.which("tmux")
    node_bin = shutil.which("node")
    if tmux_bin is None or node_bin is None:
        raise RuntimeError("tmux and node must be available for managed-local transport integration tests")

    node_impl = textwrap.dedent(
        """\
        import fs from "node:fs";

        const args = process.argv.slice(2);
        let sessionId = null;
        let displayName = null;
        let dangerousSkipPermissions = false;
        const bedrockEnabled = process.env.CLAUDE_CODE_USE_BEDROCK || "";
        const awsProfile = process.env.AWS_PROFILE || "";
        const awsRegion = process.env.AWS_REGION || "";
        const anthropicModel = process.env.ANTHROPIC_MODEL || "";
        const awsSessionToken = process.env.AWS_SESSION_TOKEN || "";

        for (let i = 0; i < args.length; i += 1) {
          if (args[i] === "--session-id" && i + 1 < args.length) {
            sessionId = args[i + 1];
          }
          if (args[i] === "-n" && i + 1 < args.length) {
            displayName = args[i + 1];
          }
          if (args[i] === "--dangerously-skip-permissions") {
            dangerousSkipPermissions = true;
          }
        }

        const logPath = process.env.FAKE_CLAUDE_LOG;
        let turnCounter = 0;
        if (logPath) {
          fs.appendFileSync(
            logPath,
            `START session=${sessionId} name=${displayName} dangerousSkipPermissions=${dangerousSkipPermissions} `
              + `bedrock=${bedrockEnabled} awsProfile=${awsProfile} awsRegion=${awsRegion} `
              + `anthropicModel=${anthropicModel} awsSessionToken=${awsSessionToken}\\n`,
            "utf8"
          );
        }

        console.log(
          `FAKE_CLAUDE_START session=${sessionId} name=${displayName} dangerousSkipPermissions=${dangerousSkipPermissions} `
            + `bedrock=${bedrockEnabled} awsProfile=${awsProfile} awsRegion=${awsRegion} `
            + `anthropicModel=${anthropicModel} awsSessionToken=${awsSessionToken}`
        );

        process.stdin.setEncoding("utf8");
        process.stdin.on("data", (chunk) => {
          for (const rawLine of chunk.split(/\\r?\\n/)) {
            const line = rawLine.trimEnd();
            if (!line) continue;
            if (!dangerousSkipPermissions) {
              if (logPath) {
                fs.appendFileSync(logPath, `PERMISSION_BLOCKED:${line}\\n`, "utf8");
              }
              console.log(`PERMISSION_BLOCKED:${line}`);
              continue;
            }
            turnCounter += 1;
            if (logPath) {
              fs.appendFileSync(logPath, `USER:${line}\\n`, "utf8");
              fs.appendFileSync(logPath, `TURN_STARTED:${turnCounter}:${line}\\n`, "utf8");
            }
            console.log(`USER:${line}`);
            console.log(`TURN_STARTED:${turnCounter}:${line}`);
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

    claude_path = bin_dir / "claude"
    claude_path.write_text(launcher, encoding="utf-8")
    claude_path.chmod(claude_path.stat().st_mode | stat.S_IXUSR)

    fake_longhouse = textwrap.dedent(
        """\
        #!/bin/zsh
        if [[ "$1" == "connect" && "$2" == "--hooks-only" ]]; then
          mkdir -p "$HOME/.claude/hooks"
          printf '#!/bin/zsh\nexit 0\n' > "$HOME/.claude/hooks/longhouse-hook.sh"
          chmod +x "$HOME/.claude/hooks/longhouse-hook.sh"
          printf '{\n  "hooks": {\n    "Stop": [\n      {\n        "hooks": [\n          {\n            "type": "command",\n            "command": "longhouse-hook.sh"\n          }\n        ]\n      }\n    ]\n  }\n}\n' > "$HOME/.claude/settings.json"
          exit 0
        fi

        echo "unsupported fake longhouse args: $*" >&2
        exit 2
        """
    )
    longhouse_path = bin_dir / "longhouse"
    longhouse_path.write_text(fake_longhouse, encoding="utf-8")
    longhouse_path.chmod(longhouse_path.stat().st_mode | stat.S_IXUSR)

    shell_init_path = ":".join(
        [
            str(bin_dir),
            str(Path(tmux_bin).parent),
            str(Path(node_bin).parent),
        ]
    )
    (home / ".zshrc").write_text(
        f'export PATH="{shell_init_path}:$PATH"\nexport FAKE_CLAUDE_LOG="{log_path}"\n',
        encoding="utf-8",
    )

    env = {
        "HOME": str(home),
        "ZDOTDIR": str(home),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "TMUX_TMPDIR": str(tmux_tmpdir),
    }

    return home, log_path, env


async def _capture_tmux_output(
    dispatcher: _LocalExecDispatcher,
    *,
    session_name: str,
    tmux_tmpdir: str | None = None,
) -> str:
    result = await dispatcher.dispatch_job(
        db=None,
        owner_id=0,
        runner_id=0,
        command=build_tmux_capture_command(
            session_name=session_name,
            lines=80,
            tmux_tmpdir=tmux_tmpdir,
        ),
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
    tmux_tmpdir: str | None = None,
    timeout_secs: float = 5.0,
) -> str:
    deadline = time.monotonic() + timeout_secs
    latest = ""
    while time.monotonic() < deadline:
        latest = await _capture_tmux_output(
            dispatcher,
            session_name=session_name,
            tmux_tmpdir=tmux_tmpdir,
        )
        if needle in latest:
            return latest
        await asyncio.sleep(0.1)
    raise AssertionError(f"Did not observe {needle!r} in tmux pane output.\nLatest output:\n{latest}")


def _wait_for_log_line(log_path: Path, needle: str, *, timeout_secs: float = 5.0) -> str:
    deadline = time.monotonic() + timeout_secs
    latest = ""
    while time.monotonic() < deadline:
        latest = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        if needle in latest:
            return latest
        time.sleep(0.1)
    raise AssertionError(f"Did not observe {needle!r} in fake Claude log.\nLatest output:\n{latest}")


@pytest.mark.skipif(
    shutil.which("tmux") is None or shutil.which("zsh") is None or shutil.which("node") is None,
    reason="Requires local tmux, zsh, and node for the managed-local transport canary.",
)
def test_managed_local_launch_and_send_text_use_real_tmux_transport_with_shell_init_path(monkeypatch, tmp_path):
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
            assert payload["managed_launch_profile"]["argv"][:3] == [
                "claude",
                "--dangerously-skip-permissions",
                "--session-id",
            ]

            session = db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).one()
            assert session.execution_home == "managed_local"
            assert session.source_runner_name == "cinder"
            assert session.managed_tmux_tmpdir == launcher_env["TMUX_TMPDIR"]
            assert session.managed_launch_profile["argv"][:3] == [
                "claude",
                "--dangerously-skip-permissions",
                "--session-id",
            ]
            baseline_runtime_state = (
                db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session.id).one_or_none()
            )
            baseline_phase = baseline_runtime_state.phase if baseline_runtime_state is not None else None

            startup_output = asyncio.run(
                _wait_for_tmux_text(
                    dispatcher,
                    session_name=managed_session_name,
                    needle="FAKE_CLAUDE_START",
                    tmux_tmpdir=session.managed_tmux_tmpdir,
                )
            )
            normalized_startup = startup_output.replace("\n", "")
            assert f"session={payload['provider_session_id']}" in normalized_startup
            assert "name=Managed Local Proof" in normalized_startup
            assert "dangerousSkipPermissions=true" in normalized_startup

            wrong_tmux_tmpdir = tmp_path / "wrong-tmux"
            wrong_tmux_tmpdir.mkdir()
            dispatcher.env["TMUX_TMPDIR"] = str(wrong_tmux_tmpdir)

            send_result = asyncio.run(
                send_text_to_managed_local_session(
                    db=db,
                    owner_id=user.id,
                    session=session,
                    text="Enter",
                    commis_id="managed-local-transport-canary",
                )
            )
            assert send_result.ok is True

            turn_output = asyncio.run(
                _wait_for_tmux_text(
                    dispatcher,
                    session_name=managed_session_name,
                    needle="TURN_STARTED:1:Enter",
                    tmux_tmpdir=session.managed_tmux_tmpdir,
                )
            )
            assert "USER:Enter" in turn_output
            assert "ASSISTANT: received Enter" in turn_output
            assert "PERMISSION_BLOCKED:Enter" not in turn_output

            runtime_state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session.id).one_or_none()
            assert (runtime_state.phase if runtime_state is not None else None) == baseline_phase
            assert (runtime_state.phase if runtime_state is not None else None) != "thinking"

            log_text = log_path.read_text(encoding="utf-8")
            assert "START session=" in log_text
            assert "dangerousSkipPermissions=true" in log_text
            assert "PERMISSION_BLOCKED:Enter" not in log_text
            assert "USER:Enter" in log_text
            assert "TURN_STARTED:1:Enter" in log_text
        finally:
            if managed_session_name:
                asyncio.run(
                    dispatcher.dispatch_job(
                        db=db,
                        owner_id=user.id,
                        runner_id=runner.id,
                        command=build_tmux_kill_session_command(
                            session_name=managed_session_name,
                            tmux_tmpdir=(
                                session.managed_tmux_tmpdir
                                if "session" in locals()
                                else launcher_env["TMUX_TMPDIR"]
                            ),
                        ),
                        timeout_secs=5,
                        commis_id=None,
                        run_id=None,
                    )
                )
            api_app_ref.dependency_overrides = {}


@pytest.mark.skipif(
    shutil.which("tmux") is None or shutil.which("zsh") is None or shutil.which("node") is None,
    reason="Requires local tmux, zsh, and node for the managed-local transport canary.",
)
def test_managed_local_claude_tmux_launch_allowlisted_bedrock_env_reaches_runtime(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    _home, log_path, launcher_env = _make_fake_claude_home(tmp_path)
    workspace = tmp_path / "bedrock-workspace"
    workspace.mkdir()

    dispatcher_env = {**os.environ, **launcher_env}
    for key in ("CLAUDE_CODE_USE_BEDROCK", "AWS_PROFILE", "AWS_REGION", "ANTHROPIC_MODEL", "AWS_SESSION_TOKEN"):
        dispatcher_env.pop(key, None)

    dispatcher = _LocalExecDispatcher(env=dispatcher_env)
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

        try:
            response = client.post(
                "/api/sessions/managed-local",
                json={
                    "runner_target": runner.name,
                    "cwd": str(workspace),
                    "project": "managed-local-bedrock",
                    "display_name": "Managed Local Bedrock",
                    "claude_launch_env": {
                        "CLAUDE_CODE_USE_BEDROCK": "1",
                        "AWS_PROFILE": "zh-qa-engineer",
                        "AWS_REGION": "us-east-1",
                        "ANTHROPIC_MODEL": "us.anthropic.claude-sonnet-4-6",
                        "AWS_SESSION_TOKEN": "should-not-pass",
                    },
                },
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            managed_session_name = payload["managed_session_name"]
            assert payload["managed_launch_profile"]["exported_env_keys"] == [
                "LONGHOUSE_MANAGED_SESSION_ID",
                "LONGHOUSE_HOOK_URL",
                "LONGHOUSE_HOOK_TOKEN",
                "CLAUDE_CODE_USE_BEDROCK",
                "AWS_PROFILE",
                "AWS_REGION",
                "ANTHROPIC_MODEL",
            ]

            session = db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).one()
            startup_output = asyncio.run(
                _wait_for_tmux_text(
                    dispatcher,
                    session_name=managed_session_name,
                    needle="FAKE_CLAUDE_START",
                    tmux_tmpdir=session.managed_tmux_tmpdir,
                )
            ).replace("\n", "")

            assert "bedrock=1" in startup_output
            assert "awsProfile=zh-qa-engineer" in startup_output
            assert "awsRegion=us-east-1" in startup_output
            assert "anthropicModel=us.anthropic.claude-sonnet-4-6" in startup_output
            assert "awsSessionToken=" in startup_output
            assert "awsSessionToken=should-not-pass" not in startup_output

            log_text = log_path.read_text(encoding="utf-8")
            assert "bedrock=1" in log_text
            assert "awsProfile=zh-qa-engineer" in log_text
            assert "awsRegion=us-east-1" in log_text
            assert "anthropicModel=us.anthropic.claude-sonnet-4-6" in log_text
            assert "awsSessionToken=should-not-pass" not in log_text
        finally:
            if managed_session_name:
                asyncio.run(
                    dispatcher.dispatch_job(
                        db=db,
                        owner_id=user.id,
                        runner_id=runner.id,
                        command=build_tmux_kill_session_command(
                            session_name=managed_session_name,
                            tmux_tmpdir=(
                                session.managed_tmux_tmpdir
                                if "session" in locals()
                                else launcher_env["TMUX_TMPDIR"]
                            ),
                        ),
                        timeout_secs=5,
                        commis_id=None,
                        run_id=None,
                    )
                )
            api_app_ref.dependency_overrides = {}


@pytest.mark.skipif(
    shutil.which("tmux") is None or shutil.which("zsh") is None or shutil.which("node") is None,
    reason="Requires local tmux, zsh, and node for the managed-local transport canary.",
)
def test_managed_local_send_text_repeated_turns_do_not_drop_or_duplicate_inputs(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    _home, log_path, launcher_env = _make_fake_claude_home(tmp_path)
    workspace = tmp_path / "stress-workspace"
    workspace.mkdir()

    dispatcher = _LocalExecDispatcher(env={**os.environ, **launcher_env})
    managed_session_name: str | None = None
    prompts = [
        "continue alpha",
        "status? [ok]",
        "Enter",
        "final /done",
    ]

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
                    "project": "managed-local-stress",
                    "display_name": "Managed Local Stress",
                    "loop_mode": "assist",
                },
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            managed_session_name = payload["managed_session_name"]

            session = db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).one()
            assert session.managed_tmux_tmpdir == launcher_env["TMUX_TMPDIR"]
            baseline_runtime_state = (
                db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session.id).one_or_none()
            )
            baseline_phase = baseline_runtime_state.phase if baseline_runtime_state is not None else None

            asyncio.run(
                _wait_for_tmux_text(
                    dispatcher,
                    session_name=managed_session_name,
                    needle="FAKE_CLAUDE_START",
                    tmux_tmpdir=session.managed_tmux_tmpdir,
                )
            )

            for idx, prompt in enumerate(prompts, start=1):
                send_result = asyncio.run(
                    send_text_to_managed_local_session(
                        db=db,
                        owner_id=user.id,
                        session=session,
                        text=prompt,
                        commis_id=f"managed-local-stress-{idx}",
                    )
                )
                assert send_result.ok is True

            latest_log = _wait_for_log_line(
                log_path,
                f"TURN_STARTED:{len(prompts)}:{prompts[-1]}",
            )

            for idx, prompt in enumerate(prompts, start=1):
                assert latest_log.count(f"USER:{prompt}") == 1
                assert latest_log.count(f"TURN_STARTED:{idx}:{prompt}") == 1

            latest_pane = asyncio.run(
                _wait_for_tmux_text(
                    dispatcher,
                    session_name=managed_session_name,
                    needle=f"TURN_STARTED:{len(prompts)}:{prompts[-1]}",
                    tmux_tmpdir=session.managed_tmux_tmpdir,
                )
            )
            for prompt in prompts:
                assert f"USER:{prompt}" in latest_pane

            runtime_state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session.id).one_or_none()
            assert (runtime_state.phase if runtime_state is not None else None) == baseline_phase
            assert (runtime_state.phase if runtime_state is not None else None) != "thinking"
        finally:
            if managed_session_name:
                asyncio.run(
                    dispatcher.dispatch_job(
                        db=db,
                        owner_id=user.id,
                        runner_id=runner.id,
                        command=build_tmux_kill_session_command(
                            session_name=managed_session_name,
                            tmux_tmpdir=(
                                session.managed_tmux_tmpdir
                                if "session" in locals()
                                else launcher_env["TMUX_TMPDIR"]
                            ),
                        ),
                        timeout_secs=5,
                        commis_id=None,
                        run_id=None,
                    )
                )
            api_app_ref.dependency_overrides = {}


@pytest.mark.skipif(
    shutil.which("tmux") is None or shutil.which("zsh") is None or shutil.which("node") is None,
    reason="Requires local tmux, zsh, and node for the managed-local transport canary.",
)
def test_managed_local_repeated_send_text_uses_single_live_tmux_session(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    _home, log_path, launcher_env = _make_fake_claude_home(tmp_path)
    workspace = tmp_path / "repeated-send-workspace"
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
                    "project": "managed-local-repeat",
                    "display_name": "Managed Local Repeat",
                    "loop_mode": "assist",
                },
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            managed_session_name = payload["managed_session_name"]

            session = db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).one()
            assert session.execution_home == "managed_local"
            baseline_runtime_state = (
                db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session.id).one_or_none()
            )
            baseline_phase = baseline_runtime_state.phase if baseline_runtime_state is not None else None

            asyncio.run(
                _wait_for_tmux_text(
                    dispatcher,
                    session_name=managed_session_name,
                    needle="FAKE_CLAUDE_START",
                    tmux_tmpdir=session.managed_tmux_tmpdir,
                )
            )

            messages = [
                f"alpha-{uuid4().hex[:6]}",
                "continue",
                f"omega-{uuid4().hex[:6]}",
            ]
            expected_assistant_needles = [
                f"ASSISTANT: received {messages[0]}",
                "ASSISTANT: continuing exact managed local session",
                f"ASSISTANT: received {messages[2]}",
            ]

            for index, message in enumerate(messages):
                send_result = asyncio.run(
                    send_text_to_managed_local_session(
                        db=db,
                        owner_id=user.id,
                        session=session,
                        text=message,
                        commis_id=f"managed-local-repeat-{index}",
                    )
                )
                assert send_result.ok is True
                assert send_result.baseline_event_id is not None
                asyncio.run(
                    _wait_for_tmux_text(
                        dispatcher,
                        session_name=managed_session_name,
                        needle=expected_assistant_needles[index],
                        tmux_tmpdir=session.managed_tmux_tmpdir,
                    )
                )

            pane_output = asyncio.run(
                _capture_tmux_output(
                    dispatcher,
                    session_name=managed_session_name,
                    tmux_tmpdir=session.managed_tmux_tmpdir,
                )
            )
            for message in messages:
                assert f"USER:{message}" in pane_output

            log_text = log_path.read_text(encoding="utf-8")
            for message in messages:
                assert log_text.count(f"USER:{message}\n") == 1

            runtime_state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session.id).one_or_none()
            assert (runtime_state.phase if runtime_state is not None else None) == baseline_phase
            assert (runtime_state.phase if runtime_state is not None else None) != "thinking"
        finally:
            if managed_session_name:
                asyncio.run(
                    dispatcher.dispatch_job(
                        db=db,
                        owner_id=user.id,
                        runner_id=runner.id,
                        command=build_tmux_kill_session_command(
                            session_name=managed_session_name,
                            tmux_tmpdir=(
                                session.managed_tmux_tmpdir
                                if "session" in locals()
                                else launcher_env["TMUX_TMPDIR"]
                            ),
                        ),
                        timeout_secs=5,
                        commis_id=None,
                        run_id=None,
                    )
                )
            api_app_ref.dependency_overrides = {}


@pytest.mark.skipif(
    shutil.which("tmux") is None or shutil.which("zsh") is None or shutil.which("node") is None,
    reason="Requires local tmux, zsh, and node for the managed-local transport canary.",
)
def test_tmux_launch_command_retains_fast_failing_pane(tmp_path):
    _home, _log_path, launcher_env = _make_fake_claude_home(tmp_path)
    dispatcher = _LocalExecDispatcher(env={**os.environ, **launcher_env})
    workspace = tmp_path / "fast-fail-workspace"
    workspace.mkdir()
    session_name = f"lh-fast-fail-{uuid4().hex[:8]}"

    try:
        launch_result = asyncio.run(
            dispatcher.dispatch_job(
                db=None,
                owner_id=0,
                runner_id=0,
                command=build_tmux_launch_command(
                    session_name=session_name,
                    cwd=str(workspace),
                    launch_command="zsh -lc 'echo FAST_FAIL; exit 23'",
                    tmux_tmpdir=launcher_env["TMUX_TMPDIR"],
                ),
                timeout_secs=5,
                commis_id=None,
                run_id=None,
            )
        )
        assert launch_result["ok"] is True
        assert int(launch_result["data"]["exit_code"]) == 0

        has_session_result = asyncio.run(
            dispatcher.dispatch_job(
                db=None,
                owner_id=0,
                runner_id=0,
                command=build_tmux_has_session_command(
                    session_name=session_name,
                    tmux_tmpdir=launcher_env["TMUX_TMPDIR"],
                ),
                timeout_secs=5,
                commis_id=None,
                run_id=None,
            )
        )
        assert has_session_result["ok"] is True
        assert int(has_session_result["data"]["exit_code"]) == 0

        captured = asyncio.run(
            _wait_for_tmux_text(
                dispatcher,
                session_name=session_name,
                needle="FAST_FAIL",
                tmux_tmpdir=launcher_env["TMUX_TMPDIR"],
            )
        )
        assert "FAST_FAIL" in captured
    finally:
        asyncio.run(
            dispatcher.dispatch_job(
                db=None,
                owner_id=0,
                runner_id=0,
                command=build_tmux_kill_session_command(
                    session_name=session_name,
                    tmux_tmpdir=launcher_env["TMUX_TMPDIR"],
                ),
                timeout_secs=5,
                commis_id=None,
                run_id=None,
            )
        )
