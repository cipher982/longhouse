"""Unit tests for the typed `Trigger.config_obj` accessor & mutator."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_config_obj_accessor_and_mutator(db_session):
    """Reading & writing via the typed helper should round-trip correctly."""

    # ------------------------------------------------------------------
    # Create an agent row – minimal required columns only
    # ------------------------------------------------------------------
    from zerg.crud import crud as _crud  # noqa: WPS433 – local import
    from zerg.models.models import Agent  # noqa: WPS433 – local import inside test
    from zerg.models.models import Trigger  # noqa: WPS433
    from zerg.models.trigger_config import TriggerConfig  # noqa: WPS433

    owner = _crud.get_user_by_email(db_session, "dev@local") or _crud.create_user(
        db_session, email="dev@local", provider=None, role="ADMIN"
    )

    agent = Agent(
        owner_id=owner.id,
        name="Cfg Test",
        system_instructions="sys",
        task_instructions="task",
        model="gpt-mock",
    )
    db_session.add(agent)
    db_session.commit()

    # ------------------------------------------------------------------
    # Insert trigger with *bare* config (gmail default)
    # ------------------------------------------------------------------
    trigger = Trigger(agent_id=agent.id, type="email", secret="dummy-secret")
    db_session.add(trigger)
    db_session.commit()

    # Accessor should return the default provider (gmail) even when the JSON is NULL
    cfg_obj = trigger.config_obj
    assert isinstance(cfg_obj, TriggerConfig)
    assert cfg_obj.provider == "gmail"

    # ------------------------------------------------------------------
    # Mutate via typed model and persist
    # ------------------------------------------------------------------
    updated_cfg = TriggerConfig(provider="gmail", history_id=123, watch_expiry=9999)
    trigger.set_config_obj(updated_cfg)
    db_session.add(trigger)
    db_session.commit()

    # Refresh from database and validate fields round-trip
    db_session.refresh(trigger)
    reloaded = trigger.config_obj
    assert reloaded.history_id == 123
    assert reloaded.watch_expiry == 9999
