"""Tests for the new /agents/{id}/details endpoint."""

import pytest
from fastapi.testclient import TestClient

from zerg.models.models import Agent

# Tests assume Python ≥3.12 – full union syntax available.


def _details_url(agent_id: int, include=None) -> str:
    if include is None:
        return f"/api/agents/{agent_id}/details"
    return f"/api/agents/{agent_id}/details?include={include}"


def test_read_agent_details_basic(client: TestClient, sample_agent: Agent):
    """Ensure the endpoint returns the expected wrapper with only `agent`."""

    response = client.get(_details_url(sample_agent.id))
    assert response.status_code == 200

    payload = response.json()

    # Should include exactly the `agent` key and not the heavy sub-resources
    assert "agent" in payload
    assert payload["agent"]["id"] == sample_agent.id

    # The optional keys should be absent when not requested
    assert "threads" not in payload
    assert "runs" not in payload
    assert "stats" not in payload


@pytest.mark.parametrize(
    "include_param, expected_keys",
    [
        ("threads", {"agent", "threads"}),
        ("runs", {"agent", "runs"}),
        ("stats", {"agent", "stats"}),
        ("threads,runs", {"agent", "threads", "runs"}),
    ],
)
def test_read_agent_details_include_param(client: TestClient, sample_agent: Agent, include_param, expected_keys):
    """When include param is supplied, empty placeholders should be present."""

    response = client.get(_details_url(sample_agent.id, include_param))
    assert response.status_code == 200

    payload = response.json()
    assert set(payload.keys()) == expected_keys

    # Agent always present
    assert payload["agent"]["id"] == sample_agent.id
