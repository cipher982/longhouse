"""Tests for the `/api/graph/layout` persistence endpoints (Phase-B)."""

from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_layout():
    """Return a representative layout payload."""

    return {
        "nodes": {
            "node-a": {"x": 42.0, "y": 84.0},
            "node-b": {"x": -10.5, "y": 0.0},
        },
        "viewport": {"x": 5.0, "y": 10.0, "zoom": 1.5},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_patch_and_get_roundtrip(client: TestClient):
    """PATCH should persist the payload so that a subsequent GET returns it."""

    payload = _sample_layout()

    # 1. Persist
    resp = client.patch("/api/graph/layout", json=payload)
    assert resp.status_code == 204

    # 2. Retrieve
    resp = client.get("/api/graph/layout")
    assert resp.status_code == 200

    body = resp.json()

    assert body["nodes"] == payload["nodes"]
    assert body["viewport"] == payload["viewport"]


def test_get_without_existing_layout_returns_204(client: TestClient):
    """When the user has no saved layout the endpoint should return 204."""

    resp = client.get("/api/graph/layout")
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Validation edge-cases
# ---------------------------------------------------------------------------


def test_patch_rejects_payload_with_too_many_nodes(client: TestClient):
    """Server must return 422 when more than 5 000 nodes are supplied."""

    big_nodes = {f"n{i}": {"x": 0, "y": 0} for i in range(5001)}
    payload = {"nodes": big_nodes, "viewport": {"x": 0, "y": 0, "zoom": 1.0}}

    resp = client.patch("/api/graph/layout", json=payload)
    assert resp.status_code == 422


def test_patch_rejects_invalid_zoom(client: TestClient):
    """Zoom outside the allowed range (0.1–10.0) should fail with 422."""

    payload = {
        "nodes": {"a": {"x": 1, "y": 2}},
        "viewport": {"x": 0, "y": 0, "zoom": 0.05},  # too small
    }

    resp = client.patch("/api/graph/layout", json=payload)
    assert resp.status_code == 422
