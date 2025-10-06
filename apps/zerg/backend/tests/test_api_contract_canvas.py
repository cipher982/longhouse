"""
API Contract Tests for Canvas Endpoints
These tests ensure that the expected API routes exist and respond correctly,
preventing frontend-backend endpoint mismatches that cause runtime 404 errors.
"""

from zerg.main import app


class TestCanvasAPIContract:
    """Test the canvas API endpoints to prevent 404 contract violations."""

    def test_current_workflow_canvas_endpoint_exists(self, client, auth_headers):
        """Test that PATCH /api/workflows/current/canvas exists (not /canvas-data)."""

        # Valid canvas data payload matching backend schema
        payload = {
            "canvas": {
                "nodes": [
                    {
                        "id": "test_node_1",
                        "type": "trigger",
                        "position": {"x": 100.0, "y": 100.0},
                        "config": {
                            "width": 200.0,
                            "height": 80.0,
                            "text": "Test Node",
                            "color": "#10b981",
                            "trigger": {"type": "manual", "config": {"enabled": True, "params": {}, "filters": []}},
                        },
                    }
                ],
                "edges": [],
            }
        }

        # This should NOT return 404
        response = client.patch("/api/workflows/current/canvas", json=payload, headers=auth_headers)
        assert response.status_code != 404, "Canvas endpoint returned 404 - API contract broken!"

        # Should be either success or validation error, but not 404
        assert response.status_code in [200, 201, 400, 422], f"Unexpected status: {response.status_code}"

    def test_wrong_canvas_endpoint_returns_404(self, client, auth_headers):
        """Test that the old incorrect endpoint /canvas-data returns 404."""

        payload = {"canvas_data": {"nodes": [], "edges": []}}

        # This SHOULD return 404 to confirm the wrong endpoint doesn't exist
        response = client.patch("/api/workflows/current/canvas-data", json=payload, headers=auth_headers)
        assert response.status_code == 404, "Incorrect endpoint should return 404"

    def test_canvas_data_validation(self, client, auth_headers):
        """Test that canvas data validation works properly."""

        # Test with malformed data
        malformed_payload = {
            "canvas": {
                "nodes": [
                    {
                        # Missing required fields
                        "node_id": "malformed"
                    }
                ],
                "edges": "not_an_array",  # Should be array
            }
        }

        response = client.patch("/api/workflows/current/canvas", json=malformed_payload, headers=auth_headers)

        # Should be validation error, not 404
        assert response.status_code in [400, 422], f"Expected validation error, got {response.status_code}"
        assert response.status_code != 404, "Validation should not return 404"

    def test_current_workflow_get_endpoint_exists(self, client, auth_headers):
        """Test that GET /api/workflows/current exists."""

        response = client.get("/api/workflows/current", headers=auth_headers)
        assert response.status_code != 404, "Current workflow GET endpoint missing!"
        assert response.status_code in [200, 201], f"Unexpected status: {response.status_code}"

    def test_canvas_persistence_roundtrip(self, client, auth_headers):
        """Test complete canvas save/load cycle."""

        # Save canvas data
        payload = {
            "canvas": {
                "nodes": [
                    {
                        "id": "persist_test_node",
                        "type": "trigger",
                        "position": {"x": 200.0, "y": 150.0},
                        "config": {
                            "width": 200.0,
                            "height": 80.0,
                            "text": "Persistence Test",
                            "color": "#10b981",
                            "trigger": {"type": "manual", "config": {"enabled": True, "params": {}, "filters": []}},
                        },
                    }
                ],
                "edges": [],
            }
        }

        # Save
        save_response = client.patch("/api/workflows/current/canvas", json=payload, headers=auth_headers)
        if save_response.status_code not in [200, 201]:
            print(f"Save response: {save_response.status_code} - {save_response.text}")
        assert save_response.status_code in [200, 201], f"Save failed: {save_response.status_code}"

        # Load
        load_response = client.get("/api/workflows/current", headers=auth_headers)
        assert load_response.status_code == 200, f"Load failed: {load_response.status_code}"

        workflow_data = load_response.json()
        assert "canvas" in workflow_data, "Canvas data not in workflow response"
        assert "nodes" in workflow_data["canvas"], "Nodes not in canvas data"
        assert len(workflow_data["canvas"]["nodes"]) == 1, "Node not persisted"
        assert workflow_data["canvas"]["nodes"][0]["id"] == "persist_test_node", "Wrong node persisted"


def test_api_routes_comprehensive_check():
    """Comprehensive check of all expected canvas-related routes."""
    from fastapi.routing import APIRoute

    # Get all routes from the app
    routes = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            routes.append({"path": route.path, "methods": route.methods, "name": route.name})

    # Expected canvas routes that must exist
    expected_routes = [
        {"path": "/api/workflows/current", "methods": {"GET"}},
        {"path": "/api/workflows/current/canvas", "methods": {"PATCH"}},
        {"path": "/api/workflows/", "methods": {"POST"}},
        {"path": "/api/workflows/", "methods": {"GET"}},
    ]

    # Check each expected route exists
    for expected in expected_routes:
        matching_routes = [
            r for r in routes if r["path"] == expected["path"] and expected["methods"].issubset(r["methods"])
        ]
        assert len(matching_routes) > 0, f"Missing route: {expected['path']} with methods {expected['methods']}"

    # Ensure the incorrect route does NOT exist
    incorrect_routes = [r for r in routes if r["path"] == "/api/workflows/current/canvas-data"]
    assert len(incorrect_routes) == 0, "Incorrect /canvas-data endpoint exists - should be /canvas"
