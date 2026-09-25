from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

pytest_plugins = ("tests_lite.live_catalog_harness",)

from zerg.catalogd.schema import create_catalog_engine  # noqa: E402
from zerg.catalogd.schema import initialize_catalog_schema  # noqa: E402
from zerg.catalogd.store import CatalogStore  # noqa: E402
from zerg.models.live_store import LiveSessionThread  # noqa: E402
from zerg.models.live_store import LiveUser  # noqa: E402
from zerg.services.console_turns import dispatch_catalog_claimed_turn  # noqa: E402
from zerg.services.console_turns import enqueue_catalog_console_turn  # noqa: E402


class _Registry:
    def __init__(self) -> None:
        self.commands: list[dict[str, object]] = []

    def supports(self, **_kwargs: object) -> bool:
        return True

    async def send_command(self, **kwargs: object) -> SimpleNamespace:
        self.commands.append(kwargs)
        return SimpleNamespace(transport_ok=True, message={"ok": True, "result": {}}, error=None)


class _UpdateCatalog:
    def __init__(self, turn: dict[str, object]) -> None:
        self.turn = turn

    async def call(self, method: str, _params: dict[str, object], **_kwargs: object) -> dict[str, object]:
        assert method == "session.console.turn.update.v2"
        return {"found": True, "applied": True, "turn": {**self.turn, "state": "active"}, "next_turn": None}


class _StoreCatalog:
    """RPC-shaped facade for the real CatalogStore used by the replay test."""

    def __init__(self, store: CatalogStore) -> None:
        self.store = store

    async def call(self, method: str, params: dict[str, object], **_kwargs: object) -> dict[str, object]:
        if method == "session.console.turn.enqueue.v2":
            data = dict(params["turn"])
            data["created_at"] = datetime.fromisoformat(str(data["created_at"]))
            return self.store.enqueue_console_turn(data=data)
        if method == "session.console.turn.current.v2":
            return self.store.read_current_console_turn(
                session_id=str(params["session_id"]),
                owner_id=int(params["owner_id"]),
            )
        if method == "session.console.turn.update.v2":
            data = dict(params["turn"])
            data["updated_at"] = datetime.fromisoformat(str(data["updated_at"]))
            return self.store.update_console_turn(data=data)
        raise AssertionError(f"unexpected RPC: {method}")


def _store(tmp_path, *, model: str | None = "model-B") -> tuple[object, CatalogStore, str, str]:
    engine = create_catalog_engine(tmp_path / "console-models.db")
    initialize_catalog_schema(engine)
    with Session(engine) as db:
        db.add(LiveUser(id=1, email="owner@example.com", is_active=True))
        db.commit()
    session_id = str(uuid4())
    thread_id = str(uuid4())
    provider_config = {"permission_mode": "bypass"}
    if model is not None:
        provider_config["model"] = model
    CatalogStore(engine).create_console_session(
        data={
            "session_id": session_id,
            "thread_id": thread_id,
            "owner_id": 1,
            "provider": "codex",
            "device_id": "cinder",
            "cwd": "/tmp/longhouse",
            "project": "longhouse",
            "provider_config": provider_config,
            "started_at": datetime.now(UTC),
        }
    )
    return engine, CatalogStore(engine), session_id, thread_id


def _enqueue(
    store: CatalogStore,
    *,
    session_id: str,
    message: str,
    client_request_id: str,
    model: object = ...,
) -> dict[str, object]:
    data: dict[str, object] = {
        "session_id": session_id,
        "owner_id": 1,
        "message": message,
        "client_request_id": client_request_id,
        "created_at": datetime.now(UTC),
    }
    if model is not ...:
        data["model"] = model
    return store.enqueue_console_turn(data=data)


@pytest.mark.asyncio
async def test_enqueued_model_is_frozen_in_dispatched_machine_payload(tmp_path):
    engine, store, session_id, thread_id = _store(tmp_path)
    try:
        _enqueue(store, session_id=session_id, message="first", client_request_id="frozen", model="model-A")
        with Session(engine) as db:
            thread = db.get(LiveSessionThread, thread_id)
            assert thread is not None
            thread.provider_config_json = json.dumps({"permission_mode": "bypass", "model": "model-B"})
            db.commit()

        current = store.read_current_console_turn(session_id=session_id, owner_id=1)
        assert current["turn"]["provider_config"]["model"] == "model-A"
        registry = _Registry()
        await dispatch_catalog_claimed_turn(
            owner_id=1,
            turn=current["turn"],
            client=_UpdateCatalog(current["turn"]),
            registry=registry,
        )
        assert registry.commands[0]["payload"]["model"] == "model-A"
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_recovered_starting_turn_replay_uses_turn_model(tmp_path, monkeypatch):
    engine, store, session_id, thread_id = _store(tmp_path)
    try:
        first = _enqueue(store, session_id=session_id, message="first", client_request_id="replay-first", model="model-A")
        second = _enqueue(store, session_id=session_id, message="second", client_request_id="replay-second")
        assert first["turn"]["state"] == "starting"
        assert second["turn"]["state"] == "queued"
        with Session(engine) as db:
            thread = db.get(LiveSessionThread, thread_id)
            assert thread is not None
            thread.provider_config_json = json.dumps({"permission_mode": "bypass", "model": "model-B"})
            db.commit()
        from zerg.services import catalogd_supervisor

        monkeypatch.setattr(catalogd_supervisor, "get_catalogd_client", lambda: _StoreCatalog(store))
        registry = _Registry()
        replayed = await enqueue_catalog_console_turn(
            owner_id=1,
            session_id=UUID(session_id),
            message="third",
            client_request_id="replay-third",
            registry=registry,
        )
        assert replayed.state == "queued"
        assert registry.commands[0]["payload"]["model"] == "model-A"
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_model_key_presence_controls_inheritance_and_dispatch_payload(tmp_path):
    engine, store, inherited_session_id, _thread_id = _store(tmp_path)
    try:
        inherited = _enqueue(
            store,
            session_id=inherited_session_id,
            message="inherits",
            client_request_id="inherits",
        )
        assert inherited["turn"]["provider_config"]["model"] == "model-B"

        explicit_none_session_id = str(uuid4())
        explicit_none_thread_id = str(uuid4())
        store.create_console_session(
            data={
                "session_id": explicit_none_session_id,
                "thread_id": explicit_none_thread_id,
                "owner_id": 1,
                "provider": "codex",
                "device_id": "cinder",
                "cwd": "/tmp/longhouse",
                "provider_config": {"permission_mode": "bypass", "model": "model-B"},
                "started_at": datetime.now(UTC),
            }
        )
        explicit_none = _enqueue(
            store,
            session_id=explicit_none_session_id,
            message="clears",
            client_request_id="clears",
            model=None,
        )
        assert "model" not in explicit_none["turn"]["provider_config"]
        registry = _Registry()
        await dispatch_catalog_claimed_turn(
            owner_id=1,
            turn=explicit_none["turn"],
            client=_UpdateCatalog(explicit_none["turn"]),
            registry=registry,
        )
        assert "model" not in registry.commands[0]["payload"]
    finally:
        engine.dispose()


def test_console_turn_idempotency_includes_model(tmp_path):
    engine, store, session_id, _thread_id = _store(tmp_path)
    try:
        first = _enqueue(store, session_id=session_id, message="same", client_request_id="same-id", model="model-A")
        replay = _enqueue(store, session_id=session_id, message="same", client_request_id="same-id", model="model-A")
        conflict = _enqueue(store, session_id=session_id, message="same", client_request_id="same-id", model="model-C")
        assert first["created"] is True
        assert replay["created"] is False
        assert replay["idempotency_conflict"] is False
        assert replay["turn"] == first["turn"]
        assert conflict["idempotency_conflict"] is True
    finally:
        engine.dispose()


def test_console_create_model_and_blank_model_are_served_on_session_detail(live_catalog, monkeypatch):
    from zerg.dependencies.browser_route_auth import get_current_browser_route_caller
    from zerg.routers import session_chat

    owner_id = live_catalog.create_user("model-detail@example.test")
    cookie = live_catalog.browser_cookie(owner_id=owner_id, email="model-detail@example.test")

    class Registry:
        @staticmethod
        def supports(**_kwargs: object) -> bool:
            return True

        @staticmethod
        def provider_readiness_state(**_kwargs: object) -> None:
            return None

    monkeypatch.setattr(session_chat, "get_machine_control_channel_registry", lambda: Registry())
    with live_catalog.http_client(extra_overrides={get_current_browser_route_caller: lambda: SimpleNamespace(id=owner_id)}) as client:
        created = client.post(
            "/sessions/console",
            json={"provider": "codex", "device_id": "cinder", "cwd": "/tmp/model-detail", "model": "model-A"},
            cookies={"longhouse_session": cookie},
        )
        assert created.status_code == 201, created.text
        created_session_id = created.json()["session_id"]
        detail = client.get(
            f"/timeline/sessions/{created_session_id}",
            cookies={"longhouse_session": cookie},
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["selected_model"] == "model-A"

        blank = client.post(
            "/sessions/console",
            json={"provider": "codex", "device_id": "cinder", "cwd": "/tmp/model-detail", "model": "   "},
            cookies={"longhouse_session": cookie},
        )
        assert blank.status_code == 201, blank.text
        blank_session_id = blank.json()["session_id"]
        blank_detail = client.get(
            f"/timeline/sessions/{blank_session_id}",
            cookies={"longhouse_session": cookie},
        )
        assert blank_detail.status_code == 200, blank_detail.text


def _create_recent_session(live_catalog, *, owner_id: int, model: str | None, launch_actor: str = "user") -> str:
    session_id = str(uuid4())
    thread_id = str(uuid4())
    provider_config = {"permission_mode": "bypass"}
    if model is not None:
        provider_config["model"] = model
    result = live_catalog.rpc(
        "session.console.create.v2",
        {
            "session": {
                "session_id": session_id,
                "thread_id": thread_id,
                "owner_id": owner_id,
                "provider": "codex",
                "device_id": "cinder",
                "cwd": "/tmp/recent-models",
                "project": "recent-models",
                "launch_actor": launch_actor,
                "provider_config": provider_config,
                "started_at": datetime.now(UTC).isoformat(),
            }
        },
    )
    assert result["created"] is True
    return session_id


def _insert_usage(live_catalog, *, session_id: str, source_epoch: str, model: str, position: int, at: datetime) -> None:
    result = live_catalog.rpc(
        "session.provider_facts.insert.v2",
        {
            "session_id": session_id,
            "source_epoch": source_epoch,
            "provider_facts": [
                {
                    "kind": "turn.usage",
                    "at": at.isoformat(),
                    "source_position": position,
                    "payload": {"model": model, "output_tokens": 1},
                }
            ],
        },
    )
    assert result["inserted"] == 1


def test_recent_models_dedupes_orders_and_serves_both_machine_routes(live_catalog):
    owner_id = live_catalog.create_user("recent-models@example.test")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="cinder")
    older = datetime.now(UTC) - timedelta(minutes=3)
    duplicate = _create_recent_session(live_catalog, owner_id=owner_id, model=None)
    newest = _create_recent_session(live_catalog, owner_id=owner_id, model=None)
    excluded = _create_recent_session(live_catalog, owner_id=owner_id, model=None, launch_actor="automation")
    no_usage = _create_recent_session(live_catalog, owner_id=owner_id, model=None)
    _insert_usage(live_catalog, session_id=duplicate, source_epoch=str(uuid4()), model="Model-A", position=1, at=older)
    _insert_usage(live_catalog, session_id=newest, source_epoch=str(uuid4()), model="model-a", position=1, at=older + timedelta(minutes=2))
    _insert_usage(live_catalog, session_id=newest, source_epoch=str(uuid4()), model="Model-B", position=2, at=older + timedelta(minutes=1))
    _insert_usage(live_catalog, session_id=excluded, source_epoch=str(uuid4()), model="Hidden", position=1, at=older + timedelta(minutes=4))
    assert no_usage

    direct = live_catalog.rpc(
        "machine.models.list.v2",
        {"owner_id": owner_id, "device_id": "cinder", "provider": "codex", "limit": 12, "days_back": 45},
    )
    assert [item["model"] for item in direct["models"]] == ["model-a", "Model-B"]
    assert all(item["last_used_at"] for item in direct["models"])

    with live_catalog.http_client() as client:
        agents = client.get(
            "/agents/machines/cinder/providers/codex/models",
            params={"limit": 12, "days_back": 45},
            headers={"X-Agents-Token": token},
        )
        assert agents.status_code == 200, agents.text
        assert [item["model"] for item in agents.json()["models"]] == ["model-a", "Model-B"]
        browser = client.get(
            "/timeline/machines/cinder/providers/codex/models",
            params={"limit": 12, "days_back": 45},
            cookies={"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email="recent-models@example.test")},
        )
        assert browser.status_code == 200, browser.text
        assert [item["model"] for item in browser.json()["models"]] == ["model-a", "Model-B"]
