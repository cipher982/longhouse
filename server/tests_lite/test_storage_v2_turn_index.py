"""The session turn index lists exactly the user rows a full transcript read shows.

Each case seals real render objects for one provider, serves them through a
fake catalog that answers both the render manifest and the turn index, reads
the whole transcript through the real page reader, and compares.
"""

from __future__ import annotations

import json
import os
import re
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

import zerg.routers.agents_storage_v2 as storage_router
import zerg.services.storage_v2_turn_index as turn_index_module
from zerg.catalogd.server import _user_turns_json
from zerg.storage_v2.render_objects import RenderObjectSpec
from zerg.storage_v2.render_objects import RenderRecord
from zerg.storage_v2.render_objects import aggregate_render_object
from zerg.storage_v2.render_objects import encode_render_object

_FRAGMENT = re.compile(r"^(?:image|text)-\d+$")


def _record(position: int, role: str, text: str | None = None, *, event_id: str | None = None, **fields) -> RenderRecord:
    return RenderRecord(
        event_id=event_id or f"e{position}",
        order_time_us=1_700_000_000_000_000 + position * 1_000_000,
        source_position=position,
        event_subordinal=fields.pop("subordinal", 0),
        role=role,
        content_text=text,
        interaction_kind=fields.pop("interaction_kind", "durable_user_message" if role == "user" else "provider_system"),
        **fields,
    )


class _Session:
    """Render objects of one generation plus the catalog rows that describe them."""

    def __init__(
        self, provider: str, objects: list[list[RenderRecord]], *, pending: set[int] = frozenset(), legacy: set[int] = frozenset()
    ):
        self.session_id = uuid4()
        self.generation_id = uuid4()
        self.provider = provider
        epoch = uuid4()
        self.decoded = {}
        self.rows = []
        for index, records in enumerate(objects):
            spec = RenderObjectSpec(
                session_id=self.session_id,
                render_generation=self.generation_id,
                parser_revision="parser",
                ordering_revision="order",
                machine_id="m",
                provider=provider,
                opaque_source_id=f"{provider}/source",
                source_epoch=epoch,
                source_envelope_id=f"{index + 5000:064x}",
                records=tuple(records),
            )
            encode_render_object(spec)  # validates ordering like a real seal
            aggregate = aggregate_render_object(spec)
            object_hash = f"{index + 1:064x}"
            path = f"render/{object_hash}.zst"
            self.decoded[path] = SimpleNamespace(spec=spec, object_hash=object_hash)
            stored = _user_turns_json(list(aggregate["user_turns"]), aggregate["user_messages"], "test")
            self.rows.append(
                {
                    "object_id": object_hash,
                    "object_hash": object_hash,
                    "object_path": path,
                    "source_envelope_id": spec.source_envelope_id,
                    "provider": provider,
                    "first_order_key": aggregate["first_order_key"],
                    "last_order_key": aggregate["last_order_key"],
                    "user_messages": aggregate["user_messages"],
                    "semantic_projection_version": 0 if index in pending else 1,
                    "user_turns": None if index in legacy else stored,
                }
            )

    def catalog(self):
        session = self

        class Catalog:
            async def call(self, method, params, **_kwargs):
                if method == "storage.session.turn_index.v2":
                    objects = []
                    for row in session.rows:
                        settled = row["semantic_projection_version"] >= 1
                        if settled and row["user_turns"] == "[]":
                            continue
                        if settled and row["user_turns"] is None and row["user_messages"] == 0:
                            continue
                        objects.append(
                            {
                                **{key: row[key] for key in ("object_id", "object_hash", "object_path", "source_envelope_id", "provider")},
                                "user_turns": json.loads(row["user_turns"]) if settled and row["user_turns"] is not None else None,
                            }
                        )
                    return {"found": True, "deleted": False, "generation_id": str(session.generation_id), "objects": objects}
                assert method == "storage.session.render_manifest.v2"
                after = json.loads(params["after_order_key"]) if params["after_order_key"] else None
                selected = [row for row in session.rows if after is None or json.loads(row["last_order_key"]) > after]
                return {
                    "found": True,
                    "current_generation_id": str(session.generation_id),
                    "generation": {"generation_id": str(session.generation_id), "event_count": 1},
                    "abandoned_events": 0,
                    "objects": selected,
                    "objects_truncated": False,
                }

        return Catalog()

    def render_pool(self):
        session = self

        class Pool:
            async def read(self, object_path, *_args, **_kwargs):
                return session.decoded[object_path]

        return Pool()


async def _transcript_user_rows(session: _Session) -> list[str]:
    """User message rows of a full transcript read, merged the way the web timeline merges them."""

    cursor = None
    events: list[dict] = []
    while True:
        page = await storage_router.read_storage_v2_session_events_page(
            session_id=session.session_id, owner_id="42", cursor=cursor, anchor="start", limit=1000, branch_mode="head"
        )
        events.extend(page["events"])
        if not page.get("has_more"):
            break
        cursor = page["next_cursor"]
    rows: list[str] = []
    for event in events:
        if event["role"] != "user" or event.get("interaction_kind") in {
            "compaction_boundary",
            "provider_notification",
            "provider_reasoning",
        }:
            continue
        if rows and event["event_id"].startswith(f"{rows[-1]}-") and _FRAGMENT.match(event["event_id"][len(rows[-1]) + 1 :]):
            continue
        rows.append(event["event_id"])
    return rows


async def _turn_ids(session: _Session) -> tuple[list[str], dict]:
    result = await turn_index_module.read_session_turn_index(session_id=session.session_id, owner_id=42)
    return [turn["event_id"] for turn in result["turns"]], result


def _cases() -> dict[str, _Session]:
    return {
        "claude rewind and local command": _Session(
            "claude",
            [
                [_record(0, "user", "first ask", parent_uuid=None), _record(1, "assistant", "ok", parent_uuid="e0")],
                [
                    _record(2, "user", "tool-heavy ask", parent_uuid="e1"),
                    _record(3, "assistant", None, tool_name="Bash", parent_uuid="e2"),
                    _record(4, "tool", None, tool_output_text="done", parent_uuid="e3"),
                ],
                # A rewind: a new ask with the same parent as e2 replaces it.
                [_record(5, "user", "edited ask", parent_uuid="e1")],
                [_record(6, "user", "/clear", interaction_kind="local_control", parent_uuid="e5")],
                [_record(7, "user", "after the clear", parent_uuid="e6")],
            ],
        ),
        "codex with injected provider context": _Session(
            "codex",
            [
                [_record(0, "user", "context the provider injected", interaction_kind="provider_system")],
                [_record(1, "user", "real ask"), _record(2, "assistant", "answer")],
                [_record(3, "user", "second ask")],
            ],
        ),
        "cursor with an explicitly abandoned prompt": _Session(
            "cursor",
            [
                [_record(0, "user", "kept"), _record(1, "user", "dropped", branch_kind="abandoned")],
                [_record(2, "user", "after")],
            ],
        ),
        "pi parent links stay as shipped, content fragments merge": _Session(
            "pi",
            [
                [_record(0, "user", "root", parent_uuid=None), _record(1, "assistant", "a", parent_uuid="e0")],
                [_record(2, "user", "branch one", parent_uuid="e1"), _record(3, "user", "branch two", parent_uuid="e1")],
                [
                    _record(4, "user", "look at this", event_id="u4", parent_uuid="e3"),
                    _record(5, "user", "[image]", event_id="u4-image-1", parent_uuid="e3"),
                ],
            ],
        ),
        "pending repair and pre-index objects decode": _Session(
            "opencode",
            [
                [_record(0, "user", "old object")],
                [_record(1, "assistant", "no users here")],
                [_record(2, "user", "fresh object")],
            ],
            legacy={0},
            pending={2},
        ),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(_cases()))
async def test_turn_index_matches_full_transcript_read(monkeypatch, name):
    session = _cases()[name]
    for module in (storage_router, turn_index_module):
        monkeypatch.setattr(module, "get_catalogd_client", session.catalog)
        monkeypatch.setattr(module, "get_render_object_worker_pool", session.render_pool)
        monkeypatch.setattr(module, "get_raw_object_worker_pool", lambda: None)

    expected = await _transcript_user_rows(session)
    actual, result = await _turn_ids(session)

    assert actual == expected
    assert expected, "a parity case must contain turns"
    decodable = sum(
        1 for row in session.rows if row["semantic_projection_version"] < 1 or row["user_turns"] is None and row["user_messages"]
    )
    assert result["objects_decoded"] == decodable


@pytest.mark.asyncio
async def test_turn_index_rows_carry_time_and_preview(monkeypatch):
    session = _cases()["claude rewind and local command"]
    for module in (storage_router, turn_index_module):
        monkeypatch.setattr(module, "get_catalogd_client", session.catalog)
        monkeypatch.setattr(module, "get_render_object_worker_pool", session.render_pool)
        monkeypatch.setattr(module, "get_raw_object_worker_pool", lambda: None)

    result = await turn_index_module.read_session_turn_index(session_id=session.session_id, owner_id=42)

    assert [(turn["event_id"], turn["preview"]) for turn in result["turns"]] == [
        ("e0", "first ask"),
        ("e5", "edited ask"),
        ("e7", "after the clear"),
    ]
    assert result["turns"][0]["timestamp"] == "2023-11-14T22:13:20+00:00"
    assert result["objects_decoded"] == 0


def test_catalog_rejects_a_turn_list_that_disagrees_with_the_count():
    spec_turn = {
        "event_id": "e0",
        "order_key": [1, "m", "p", "s", "e", 0, 0],
        "parent_uuid": None,
        "branch_kind": None,
        "turn": True,
        "preview": "x",
    }
    assert json.loads(_user_turns_json([spec_turn], 1, "t"))[0]["event_id"] == "e0"
    with pytest.raises(ValueError):
        _user_turns_json([spec_turn], 2, "t")
    with pytest.raises(ValueError):
        _user_turns_json([{**spec_turn, "extra": 1}], 1, "t")
    assert _user_turns_json(None, 3, "t") is None


def test_owner_is_forwarded_as_text(monkeypatch):
    seen = {}

    class Catalog:
        async def call(self, method, params, **_kwargs):
            seen.update(params)
            return {"found": False}

    monkeypatch.setattr(turn_index_module, "get_catalogd_client", lambda: Catalog())
    monkeypatch.setattr(turn_index_module, "resolve_session_alias", lambda *_args, **_kwargs: {"found": False})
    import asyncio

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        asyncio.run(turn_index_module.read_session_turn_index(session_id=UUID(int=1), owner_id=7))
    assert caught.value.status_code == 404
    assert seen == {"session_id": str(UUID(int=1)), "owner_id": "7"}


def test_turn_preview_keeps_the_ask_typed_after_a_long_paste():
    from zerg.storage_v2.render_objects import _turn_preview

    preview = _turn_preview('<pasted_content id="a1">' + "log line\n" * 400 + "</pasted_content>\n\nwhy does this fail?")
    assert preview is not None and len(preview) <= 500
    assert preview.endswith("why does this fail?")
    assert preview.startswith('<pasted_content id="a1">log line')


def test_alias_id_reads_the_session_it_belongs_to(monkeypatch):
    import asyncio

    alias, canonical = UUID(int=2), UUID(int=3)
    asked = []

    class Catalog:
        async def call(self, method, params, **_kwargs):
            asked.append(params["session_id"])
            if params["session_id"] != str(canonical):
                return {"found": False}
            return {"found": True, "deleted": False, "generation_id": str(UUID(int=4)), "objects": []}

    monkeypatch.setattr(turn_index_module, "get_catalogd_client", lambda: Catalog())
    monkeypatch.setattr(turn_index_module, "resolve_session_alias", lambda value, **_kwargs: {"found": True, "session_id": str(canonical)})

    result = asyncio.run(turn_index_module.read_session_turn_index(session_id=alias, owner_id=7))

    assert asked == [str(alias), str(canonical)]
    assert result["session_id"] == str(canonical) and result["turns"] == []
