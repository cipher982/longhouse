"""`POST /api/agents/sessions/{id}/coordination-token` had no tests at all.

It is the resume leg of the same authority the `longhouse cursor` outage was
about: the launcher calls it when reattaching, and every managed launcher bails
if it returns nothing. It also mints a session-scoped credential, so its
refusals matter as much as its successes.

Scope checks added 2026-07-31: device match alone is not provenance. Without
the managed and closed checks a device token could mint coordination authority
for a same-device Shadow session Longhouse never launched, or for one that
ended weeks ago.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import pytest
from fastapi import HTTPException

from zerg.routers.agents_sessions import _session_is_managed_for_coordination


def _snapshot(*, provider: str = "cursor", connections: list | None = None, closed_at=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        provider=provider,
        device_id="cinder",
        project="demo",
        closed_at=closed_at,
        catalog_facts={"connections": connections if connections is not None else [{"state": "detached"}]},
    )


def test_managed_session_with_a_connection_is_eligible() -> None:
    assert _session_is_managed_for_coordination(_snapshot()) is True


def test_shadow_session_is_not_eligible() -> None:
    """A Shadow session is discovered, never launched, so it has no connection."""

    assert _session_is_managed_for_coordination(_snapshot(connections=[])) is False


def test_session_without_catalog_facts_is_not_eligible() -> None:
    assert _session_is_managed_for_coordination(SimpleNamespace(catalog_facts=None)) is False
    assert _session_is_managed_for_coordination(SimpleNamespace()) is False


def test_malformed_connections_are_not_eligible() -> None:
    assert _session_is_managed_for_coordination(SimpleNamespace(catalog_facts={"connections": "nope"})) is False
    assert _session_is_managed_for_coordination(SimpleNamespace(catalog_facts={})) is False


@pytest.mark.parametrize("provider", ["claude", "codex", "opencode", "cursor"])
def test_coordination_providers_reach_the_provenance_check(provider: str) -> None:
    """Every launcher that hard-bails without a token must be able to get one.

    This is the resume-path counterpart to
    test_directed_input_envelope.test_declared_coordination_capabilities_are_backed_by_the_provider_set.
    """

    from zerg.services.directed_input_envelope import provider_supports_coordination_tools

    assert provider_supports_coordination_tools(provider) is True
    assert _session_is_managed_for_coordination(_snapshot(provider=provider)) is True


def test_antigravity_has_no_coordination_tools() -> None:
    from zerg.services.directed_input_envelope import provider_supports_coordination_tools

    assert provider_supports_coordination_tools("antigravity") is False


def test_endpoint_rejects_a_managed_session_token() -> None:
    """Only a durable device token may mint session-scoped authority."""

    import asyncio

    from zerg.auth.managed_session_tokens import ManagedSessionToken
    from zerg.routers.agents_sessions import issue_session_coordination_token

    auth = ManagedSessionToken(
        owner_id=1,
        session_id=str(uuid4()),
        project=None,
        device_id="cinder",
        scope="coordination",
    )
    with pytest.raises(HTTPException) as denied:
        asyncio.run(issue_session_coordination_token(UUID(int=1), _auth=auth, _single=None))
    assert denied.value.status_code == 403
    assert "device token" in str(denied.value.detail).lower()
