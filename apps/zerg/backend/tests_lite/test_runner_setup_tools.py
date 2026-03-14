from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.tools.builtin import runner_setup_tools
from zerg.utils.time import utc_now_naive


def _make_db(tmp_path: Path):
    db_path = tmp_path / "runner-setup-tools.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = make_sessionmaker(engine)
    return SessionLocal()


@contextmanager
def _db_session(db):
    try:
        yield db
    finally:
        pass


def test_runner_list_suggests_doctor_when_all_runners_are_offline(tmp_path: Path):
    db = _make_db(tmp_path)
    try:
        user = User(email="owner@test.local", role="ADMIN")
        db.add(user)
        db.commit()
        db.refresh(user)

        runner = Runner(
            owner_id=user.id,
            name="cube",
            auth_secret_hash="hash",
            capabilities=["exec.full"],
            status="offline",
            last_seen_at=utc_now_naive() - timedelta(minutes=10),
            runner_metadata={"capabilities": ["exec.full"], "heartbeat_interval_ms": 30000},
        )
        db.add(runner)
        db.commit()

        with (
            patch("zerg.tools.builtin.runner_setup_tools.get_credential_resolver", return_value=SimpleNamespace(owner_id=user.id)),
            patch("zerg.tools.builtin.runner_setup_tools.db_session", return_value=_db_session(db)),
            patch(
                "zerg.tools.builtin.runner_setup_tools.get_runner_connection_manager",
                return_value=SimpleNamespace(is_online=lambda owner_id, runner_id: False),
            ),
        ):
            result = runner_setup_tools.runner_list()

        assert result["ok"] is True
        assert result["data"]["summary"] == "0/1 runners online"
        assert "runner_doctor" in result["data"]["suggested_next_step"]
    finally:
        db.close()


def test_runner_doctor_returns_reason_coded_diagnosis(tmp_path: Path):
    db = _make_db(tmp_path)
    try:
        user = User(email="owner@test.local", role="ADMIN")
        db.add(user)
        db.commit()
        db.refresh(user)

        runner = Runner(
            owner_id=user.id,
            name="zerg",
            auth_secret_hash="hash",
            capabilities=["exec.full"],
            status="online",
            last_seen_at=utc_now_naive(),
            runner_metadata={
                "hostname": "zerg",
                "platform": "linux",
                "runner_version": "0.1.0",
                "install_mode": "server",
                "capabilities": ["exec.full"],
            },
        )
        db.add(runner)
        db.commit()

        with (
            patch("zerg.tools.builtin.runner_setup_tools.get_credential_resolver", return_value=SimpleNamespace(owner_id=user.id)),
            patch("zerg.tools.builtin.runner_setup_tools.db_session", return_value=_db_session(db)),
            patch(
                "zerg.tools.builtin.runner_setup_tools.get_runner_connection_manager",
                return_value=SimpleNamespace(is_online=lambda owner_id, runner_id: True),
            ),
        ):
            result = runner_setup_tools.runner_doctor("zerg")

        assert result["ok"] is True
        diagnosis = result["data"]["diagnosis"]
        assert diagnosis["reason_code"] == "runner_version_outdated"
        assert diagnosis["repair_supported"] is True
    finally:
        db.close()
