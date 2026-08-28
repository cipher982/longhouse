"""Semantic storage convergence runs from the durable projector ledger."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from uuid import uuid4

import pytest

import zerg.services.semantic_v2_projector as semantic_projector


@pytest.mark.asyncio
async def test_claude_semantic_repair_runs_from_claim_and_completes(monkeypatch):
    session_id = str(uuid4())
    generation_id = str(uuid4())
    calls: list[tuple[str, dict]] = []

    class Catalog:
        async def call(self, method, params=None, **_kwargs):
            params = params or {}
            calls.append((method, params))
            if method == "projector.state.claim.v2":
                return {
                    "claimed": [
                        {
                            "session_id": session_id,
                            "claimed_revision": "12",
                            "failure_count": 0,
                        }
                    ]
                }
            if method == "storage.session.projector.read.v2":
                return {
                    "found": True,
                    "session": {
                        "session_id": session_id,
                        "provider": "claude",
                        "owner_id": "42",
                        "semantic_projection_version": 0,
                        "current_render_generation": generation_id,
                    },
                }
            if method == "projector.state.complete.v2":
                return {"changed": True}
            raise AssertionError(f"unexpected catalog call: {method}")

    repairs: list[dict] = []

    async def repair(**kwargs):
        repairs.append(kwargs)
        return {"complete": True, "updated_object_count": 1}

    monkeypatch.setattr(semantic_projector, "repair_storage_session_semantic_projection", repair)
    catalog = Catalog()
    projector = semantic_projector.SemanticV2Projector(
        catalog=catalog,
        render_workers=object(),
        raw_workers=object(),
        worker_id="semantic-v2:test",
    )

    claimed = await projector.run_once(limit=1, now=datetime(2026, 8, 20, tzinfo=UTC))

    assert claimed == 1
    assert len(repairs) == 1
    assert repairs[0]["session_id"] == session_id
    assert repairs[0]["generation_id"] == generation_id
    assert [method for method, _params in calls] == [
        "projector.state.claim.v2",
        "storage.session.projector.read.v2",
        "projector.state.complete.v2",
    ]
    assert calls[-1][1]["completed_revision"] == 12
