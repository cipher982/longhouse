from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
import typer
from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli import claude as claude_cli
from zerg.cli.main import app
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.session_loop_mode import SessionLoopMode


def _make_db(tmp_path):
    db_path = tmp_path / "test_claude_cli.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _seed_user(db, email: str) -> User:
    user = User(email=email, role=UserRole.USER.value)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_runner(db, *, owner_id: int, name: str, status: str = "online") -> Runner:
    runner = Runner(
        owner_id=owner_id,
        name=name,
        availability_policy="always_on",
        capabilities=["exec.full"],
        status=status,
        auth_secret_hash="secret-hash",
        runner_metadata={"install_mode": "desktop"},
    )
    db.add(runner)
    db.commit()
    db.refresh(runner)
    return runner


def test_resolve_runner_target_requires_owner_when_name_is_ambiguous(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user_a = _seed_user(db, "alpha@test.local")
        user_b = _seed_user(db, "beta@test.local")
        runner_a = _seed_runner(db, owner_id=user_a.id, name="cinder")
        runner_b = _seed_runner(db, owner_id=user_b.id, name="cinder")

        with pytest.raises(typer.BadParameter) as exc_info:
            claude_cli._resolve_runner_target(db, runner_target="cinder", owner_email=None)

        message = str(exc_info.value)
        assert "Runner name is ambiguous" in message
        assert f"runner:{runner_a.id} (alpha@test.local)" in message
        assert f"runner:{runner_b.id} (beta@test.local)" in message

        resolved = claude_cli._resolve_runner_target(
            db,
            runner_target="cinder",
            owner_email="beta@test.local",
        )
        assert resolved.owner_id == user_b.id
        assert resolved.owner_email == "beta@test.local"
        assert resolved.runner_target == "cinder"
        assert resolved.runner_name == "cinder"


def test_launch_managed_local_from_cli_maps_requested_options(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user = _seed_user(db, "owner@test.local")
        _seed_runner(db, owner_id=user.id, name="cinder")
        captured = {}

        async def _fake_launch(inner_db, params):
            captured["db"] = inner_db
            captured["params"] = params
            return SimpleNamespace(
                session=SimpleNamespace(id="session-123", provider_session_id="provider-123"),
                attach_command="zsh -lc 'exec tmux attach -t foo'",
            )

        monkeypatch.setattr(claude_cli, "launch_managed_local_session", _fake_launch)

        resolved, result = claude_cli._launch_managed_local_from_cli(
            db,
            runner_target="cinder",
            cwd="/tmp/project",
            project="ops",
            loop_mode=SessionLoopMode.ASSIST,
            name="Ops session",
            owner_email=None,
        )

        assert resolved.owner_id == user.id
        assert resolved.runner_target == "cinder"
        assert captured["db"] is db
        assert captured["params"].owner_id == user.id
        assert captured["params"].runner_target == "cinder"
        assert captured["params"].cwd == "/tmp/project"
        assert captured["params"].project == "ops"
        assert captured["params"].display_name == "Ops session"
        assert captured["params"].loop_mode == "assist"
        assert result.attach_command == "zsh -lc 'exec tmux attach -t foo'"


def test_claude_command_prints_attach_command_and_auto_attaches(monkeypatch, tmp_path):
    runner = CliRunner()
    db = SimpleNamespace(close=lambda: None, rollback=lambda: None)
    attach_calls: list[str] = []

    monkeypatch.setattr(claude_cli, "initialize_database", lambda: None)
    monkeypatch.setattr(claude_cli, "get_session_factory", lambda: (lambda: db))
    monkeypatch.setattr(claude_cli, "_interactive_stdio", lambda: True)
    monkeypatch.setattr(claude_cli, "_run_attach_command", lambda command: attach_calls.append(command) or 0)
    monkeypatch.setattr(
        claude_cli,
        "_launch_managed_local_from_cli",
        lambda *_args, **_kwargs: (
            claude_cli.ResolvedRunnerTarget(
                owner_id=1,
                owner_email="owner@test.local",
                runner_target="cinder",
                runner_name="cinder",
            ),
            SimpleNamespace(
                session=SimpleNamespace(
                    id="session-123",
                    provider_session_id="provider-123",
                ),
                attach_command="zsh -lc 'exec tmux attach -t lh-demo'",
            ),
        ),
    )

    result = runner.invoke(
        app,
        [
            "claude",
            "--runner",
            "cinder",
            "--cwd",
            str(tmp_path),
            "--project",
            "demo",
            "--loop-mode",
            "assist",
            "--name",
            "Demo session",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Managed local Claude session launched on cinder." in result.output
    assert "Session ID: session-123" in result.output
    assert "Provider session ID: provider-123" in result.output
    assert "Attach: zsh -lc 'exec tmux attach -t lh-demo'" in result.output
    assert "Attaching..." in result.output
    assert attach_calls == ["zsh -lc 'exec tmux attach -t lh-demo'"]
