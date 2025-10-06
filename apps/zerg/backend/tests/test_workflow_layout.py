"""Tests for the new `/api/workflows/{id}/layout` persistence endpoints."""

from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_layout():
    return {
        "nodes": {
            "n1": {"x": 1.2, "y": 3.4},
            "n2": {"x": -5.0, "y": 0.0},
        },
        "viewport": {"x": 0.0, "y": 0.0, "zoom": 1.0},
    }


# ---------------------------------------------------------------------------
# Fixtures – pytest provides `client` from `tests/conftest.py`
# ---------------------------------------------------------------------------


def _create_workflow(client: TestClient):
    payload = {
        "name": "WF-Layout-Test",
        "description": "layout test wf",
        "canvas": {"nodes": [], "edges": []},
    }
    resp = client.post("/api/workflows/", json=payload)
    assert resp.status_code in (200, 201)
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_put_and_get_roundtrip(client: TestClient):
    """PUT should persist the payload so that a subsequent GET returns it."""

    workflow_id = _create_workflow(client)
    layout = _sample_layout()

    # Persist
    resp = client.put(f"/api/workflows/{workflow_id}/layout", json=layout)
    assert resp.status_code == 204

    # Retrieve
    resp = client.get(f"/api/workflows/{workflow_id}/layout")
    assert resp.status_code == 200

    body = resp.json()
    assert body["nodes"] == layout["nodes"]
    assert body["viewport"] == layout["viewport"]


def test_get_without_existing_layout_returns_204(client: TestClient):
    """When no layout is stored the endpoint should return HTTP 204."""

    workflow_id = _create_workflow(client)

    resp = client.get(f"/api/workflows/{workflow_id}/layout")
    assert resp.status_code == 204
