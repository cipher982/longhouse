"""Tests for user-backed Oikos operator-mode policy helpers."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.user import User
from zerg.services.oikos_operator_policy import get_operator_policy
from zerg.services.oikos_operator_policy import policy_from_user_context


def _make_db(tmp_path, name: str = "operator_policy.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return engine, make_sessionmaker(engine)


def test_policy_defaults_to_master_switch(monkeypatch):
    monkeypatch.delenv("OIKOS_OPERATOR_MODE_ENABLED", raising=False)
    disabled = policy_from_user_context(None)
    assert disabled.enabled is False
    assert disabled.shadow_mode is True
    assert disabled.allow_continue is False
    assert disabled.allow_notify is True
    assert disabled.allow_small_repairs is False

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    enabled = policy_from_user_context(None)
    assert enabled.enabled is True
    assert enabled.shadow_mode is True
    assert enabled.allow_continue is False
    assert enabled.allow_notify is True
    assert enabled.allow_small_repairs is False


def test_policy_reads_nested_operator_mode_preferences(monkeypatch):
    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    policy = policy_from_user_context(
        {
            "preferences": {
                "operator_mode": {
                    "enabled": False,
                    "shadow_mode": False,
                    "allow_continue": True,
                    "allow_notify": False,
                    "allow_small_repairs": True,
                }
            }
        }
    )

    assert policy.enabled is False
    assert policy.shadow_mode is False
    assert policy.allow_continue is True
    assert policy.allow_notify is False
    assert policy.allow_small_repairs is True


def test_get_operator_policy_loads_user_context_from_db(monkeypatch, tmp_path):
    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    engine, SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        db.add(
            User(
                id=7,
                email="owner@test.local",
                role="ADMIN",
                context={
                    "preferences": {
                        "operator_mode": {
                            "enabled": True,
                            "allow_continue": True,
                        }
                    }
                },
            )
        )
        db.commit()

    with SessionLocal() as db:
        policy = get_operator_policy(db, 7)

    engine.dispose()

    assert policy.enabled is True
    assert policy.allow_continue is True
    assert policy.shadow_mode is True
