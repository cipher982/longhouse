"""A page that holds its transcript refreshes the workspace header only.

Spec: control-plane/docs/specs/catalog-read-path.md. The header-only read
skips the transcript page (its manifest and render object reads) and still
reports the server's transcript revision at the top level.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

import zerg.dependencies.auth as _auth_deps  # noqa: F401 - triggers settings init
import zerg.services.storage_v2_workspace as workspace_module


def _session(session_id, render_revision):
    return SimpleNamespace(
        provider="claude",
        runtime_display=SimpleNamespace(lifecycle="open"),
        capabilities=SimpleNamespace(live_control_available=True, can_start_turn=True),
        session_state=SimpleNamespace(transcript=SimpleNamespace(transcript_revision=render_revision)),
        model_dump=lambda **_kwargs: {"id": str(session_id), "lifecycle": "open", "capabilities": {}},
    )


class _Catalog:
    async def call(self, method, params, *, timeout_seconds=None):
        return {
            "found": True,
            "commit_seq": "8",
            "session": {"owner_id": "42", "updated_at": "2026-11-16T12:00:00Z"},
            "provider_facts": [],
            "input_receipts": [],
            "media_refs": [],
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", ["full", "lite"])
async def test_a_header_only_workspace_skips_the_transcript_and_reports_the_revision(monkeypatch, detail):
    session_id = uuid4()

    async def read_page(**_kwargs):
        raise AssertionError("a header-only workspace must not read the transcript page")

    monkeypatch.setattr(workspace_module, "get_catalogd_client", lambda: _Catalog())
    monkeypatch.setattr(workspace_module, "read_live_catalog_session", lambda *_a, **_k: (_session(session_id, 412), None, "7"))
    monkeypatch.setattr(workspace_module, "read_storage_v2_session_events_page", read_page)

    workspace = await workspace_module.build_storage_v2_workspace(
        session_id=session_id, owner_id=42, branch_mode="head", limit=200, detail=detail, include_projection=False
    )

    assert workspace is not None
    assert workspace["transcript_revision"] == 412
    assert workspace["session"]["id"] == str(session_id)
    assert workspace["projection"]["items"] == []
    assert workspace["control_only"] is False


@pytest.mark.asyncio
async def test_a_full_workspace_reports_the_same_revision_on_top_and_on_the_projection(monkeypatch):
    session_id = uuid4()

    async def read_page(**_kwargs):
        return {"generation_id": str(uuid4()), "events": [], "next_cursor": None, "has_more": False, "total": 0}

    monkeypatch.setattr(workspace_module, "get_catalogd_client", lambda: _Catalog())
    monkeypatch.setattr(workspace_module, "read_live_catalog_session", lambda *_a, **_k: (_session(session_id, 412), None, "7"))
    monkeypatch.setattr(workspace_module, "read_storage_v2_session_events_page", read_page)

    workspace = await workspace_module.build_storage_v2_workspace(session_id=session_id, owner_id=42, branch_mode="head", limit=200)

    assert workspace is not None
    assert workspace["transcript_revision"] == workspace["projection"]["transcript_revision"] == 412
