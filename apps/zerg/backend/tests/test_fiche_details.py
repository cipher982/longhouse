"""Tests for the new /fiches/{id}/details endpoint."""

import pytest
from fastapi.testclient import TestClient

from zerg.models.models import Fiche

# Tests assume Python ≥3.12 – full union syntax available.


def _details_url(fiche_id: int, include=None) -> str:
    if include is None:
        return f"/api/fiches/{fiche_id}/details"
    return f"/api/fiches/{fiche_id}/details?include={include}"


def test_read_fiche_details_basic(client: TestClient, sample_fiche: Fiche):
    """Ensure the endpoint returns the expected wrapper with only `fiche`."""

    response = client.get(_details_url(sample_fiche.id))
    assert response.status_code == 200

    payload = response.json()

    # Should include exactly the `fiche` key and not the heavy sub-resources
    assert "fiche" in payload
    assert payload["fiche"]["id"] == sample_fiche.id

    # The optional keys should be absent when not requested
    assert "threads" not in payload
    assert "courses" not in payload
    assert "stats" not in payload


@pytest.mark.parametrize(
    "include_param, expected_keys",
    [
        ("threads", {"fiche", "threads"}),
        ("courses", {"fiche", "courses"}),
        ("stats", {"fiche", "stats"}),
        ("threads,courses", {"fiche", "threads", "courses"}),
    ],
)
def test_read_fiche_details_include_param(client: TestClient, sample_fiche: Fiche, include_param, expected_keys):
    """When include param is supplied, empty placeholders should be present."""

    response = client.get(_details_url(sample_fiche.id, include_param))
    assert response.status_code == 200

    payload = response.json()
    assert set(payload.keys()) == expected_keys

    # Fiche always present
    assert payload["fiche"]["id"] == sample_fiche.id
