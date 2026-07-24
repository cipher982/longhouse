from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.catalogd.store import CatalogStore
from zerg.models.live_store import LiveSession
from zerg.models.live_store import LiveUser
from zerg.services.live_session_inputs import upsert_live_input_receipt


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-directed-inputs-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _seed_owner_sessions(database_path: Path) -> tuple[str, str, str]:
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    source_id = str(uuid4())
    target_id = str(uuid4())
    foreign_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            LiveUser.__table__.insert(),
            [
                {"id": 7, "email": "owner@example.com", "role": "ADMIN", "is_active": True},
                {"id": 8, "email": "other@example.com", "role": "ADMIN", "is_active": True},
            ],
        )
        connection.execute(
            LiveSession.__table__.insert(),
            [
                {
                    "session_id": source_id,
                    "owner_id": "7",
                    "provider": "codex",
                    "state": "idle",
                    "last_seen_at": now,
                    "updated_at": now,
                },
                {
                    "session_id": target_id,
                    "owner_id": "7",
                    "provider": "claude",
                    "state": "idle",
                    "last_seen_at": now,
                    "updated_at": now,
                },
                {
                    "session_id": foreign_id,
                    "owner_id": "8",
                    "provider": "codex",
                    "state": "idle",
                    "last_seen_at": now,
                    "updated_at": now,
                },
            ],
        )
    engine.dispose()
    return source_id, target_id, foreign_id


@pytest.mark.asyncio
async def test_directed_input_is_idempotent_and_cursor_ordered(daemon_paths):
    database_path, socket_path = daemon_paths
    source_id, target_id, _foreign_id = _seed_owner_sessions(database_path)
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    now = datetime.now(UTC).replace(microsecond=0)
    params = {
        "owner_id": 7,
        "source_session_id": source_id,
        "target_session_id": target_id,
        "text": "Check the migration result",
        "reply_to_id": None,
        "client_request_id": "request-1",
        "created_at": now.isoformat(),
    }
    try:
        created = await client.call("directed_input.create.v2", params)
        replay = await client.call("directed_input.create.v2", params)
        assert created["created"] is True
        assert created["directed_input"]["input_receipt"] is None
        assert replay["created"] is False
        assert replay["directed_input"] == created["directed_input"]

        second = await client.call(
            "directed_input.create.v2",
            {**params, "text": "Second", "client_request_id": "request-2"},
        )
        listed = await client.call(
            "directed_input.list.v2",
            {
                "owner_id": 7,
                "session_id": target_id,
                "direction": "inbound",
                "after_id": created["directed_input"]["id"],
                "limit": 50,
            },
        )
        assert listed["directed_inputs"] == [second["directed_input"]]
        assert listed["next_cursor"] == second["directed_input"]["id"]

        with pytest.raises(CatalogRemoteError) as conflict:
            await client.call("directed_input.create.v2", {**params, "text": "Changed"})
        assert conflict.value.code == "conflict"
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_directed_input_enforces_owner_and_reply_direction(daemon_paths):
    database_path, socket_path = daemon_paths
    source_id, target_id, foreign_id = _seed_owner_sessions(database_path)
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    try:
        with pytest.raises(CatalogRemoteError) as owner_error:
            await client.call(
                "directed_input.create.v2",
                {
                    "owner_id": 7,
                    "source_session_id": source_id,
                    "target_session_id": foreign_id,
                    "text": "cross-owner input",
                    "reply_to_id": None,
                    "client_request_id": "foreign",
                    "created_at": now,
                },
            )
        assert owner_error.value.code == "not_found"

        parent = await client.call(
            "directed_input.create.v2",
            {
                "owner_id": 7,
                "source_session_id": source_id,
                "target_session_id": target_id,
                "text": "parent",
                "reply_to_id": None,
                "client_request_id": "parent",
                "created_at": now,
            },
        )
        reply = await client.call(
            "directed_input.create.v2",
            {
                "owner_id": 7,
                "source_session_id": target_id,
                "target_session_id": source_id,
                "text": "reply",
                "reply_to_id": parent["directed_input"]["id"],
                "client_request_id": "reply",
                "created_at": now,
            },
        )
        assert reply["directed_input"]["reply_to_id"] == parent["directed_input"]["id"]

        with pytest.raises(CatalogRemoteError) as direction_error:
            await client.call(
                "directed_input.create.v2",
                {
                    "owner_id": 7,
                    "source_session_id": source_id,
                    "target_session_id": target_id,
                    "text": "not a reply",
                    "reply_to_id": parent["directed_input"]["id"],
                    "client_request_id": "bad-reply",
                    "created_at": now,
                },
            )
        assert direction_error.value.code == "invalid_request"
        assert direction_error.value.details == {"reason": "reply_direction"}
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.parametrize(("receipt_status", "error"), [("delivered", None), ("failed", "provider rejected input")])
def test_directed_input_projects_linked_receipt(receipt_status, error, daemon_paths):
    database_path, _socket_path = daemon_paths
    source_id, target_id, _foreign_id = _seed_owner_sessions(database_path)
    engine = create_catalog_engine(database_path)
    store = CatalogStore(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    created = store.create_directed_input(
        owner_id=7,
        source_session_id=source_id,
        target_session_id=target_id,
        text="linked queue input",
        reply_to_id=None,
        client_request_id="linked",
        created_at=now,
    )
    directed_input_id = created["directed_input"]["id"]
    with Session(engine) as db:
        receipt = upsert_live_input_receipt(
            db,
            owner_id=7,
            session_id=target_id,
            provider="claude",
            text="rendered linked queue input",
            intent="queue",
            status="delivering",
            client_request_id=f"directed-input-{directed_input_id}",
            delivery_request_id="delivery-1",
            now=now,
        )
        db.commit()
        receipt_id = str(receipt.id)

    store.link_directed_input_receipt(
        owner_id=7,
        directed_input_id=directed_input_id,
        input_receipt_id=receipt_id,
        observed_at=now,
    )
    store.finish_queued_input(
        receipt_id=receipt_id,
        delivery_request_id="delivery-1",
        status=receipt_status,
        error=error,
    )
    read = store.read_directed_input(owner_id=7, directed_input_id=directed_input_id)

    assert read["directed_input"]["input_receipt"]["status"] == receipt_status
    expected_error = json.dumps({"message": error}) if error else None
    assert read["directed_input"]["input_receipt"]["error_json"] == expected_error
    engine.dispose()
