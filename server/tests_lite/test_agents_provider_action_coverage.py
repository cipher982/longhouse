from __future__ import annotations

import os
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())


def _make_client():
    from zerg.dependencies.agents_auth import require_single_tenant
    from zerg.dependencies.agents_auth import verify_agents_token
    from zerg.main import api_app
    from zerg.main import app

    def override_verify_agents_token():
        return SimpleNamespace(device_id="testclient", id="token-1")

    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(app, backend="asyncio"), api_app


def test_agents_provider_action_coverage_route_exposes_derived_product_actions():
    client, api_app = _make_client()
    try:
        response = client.get("/api/agents/providers/action-coverage?provider=opencode")
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "zerg.services.provider_action_coverage"
    assert payload["states"] == ["supported", "read_only", "unknown", "unsupported"]

    opencode = payload["providers"]["opencode"]
    assert opencode["actions"]["send_prompt"]["state"] == "supported"
    assert opencode["actions"]["send_prompt"]["reason_code"] == "contract_proven"
    assert opencode["actions"]["classify_subagents"]["state"] == "unknown"
    assert opencode["actions"]["classify_subagents"]["proof_refs"] == [
        {
            "scenario": "opencode_orchestration_projection",
            "assertion": "task_child_attached_to_primary_parent",
        },
        {
            "scenario": "opencode_orchestration_projection",
            "assertion": "nested_subagent_attached_to_subagent_parent",
        },
    ]
    assert opencode["summary"]["supported"] >= 1
