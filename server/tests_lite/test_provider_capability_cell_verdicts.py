"""Failed-execution verdicts: publication, the newest-wins store, strict reads."""

from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from tests_lite.test_provider_capability_proof_routes import _bundle
from tests_lite.test_provider_capability_proof_routes import _client
from tests_lite.test_provider_capability_proof_routes import _factory_headers
from tests_lite.test_provider_capability_proof_routes import _record
from zerg.main import api_app
from zerg.services.provider_capability_cell_verdicts import VERDICT_BUNDLE_KIND
from zerg.services.provider_capability_cell_verdicts import CellVerdict
from zerg.services.provider_capability_cell_verdicts import CellVerdictStore
from zerg.services.provider_capability_cell_verdicts import CellVerdictStoreError
from zerg.services.provider_capability_cell_verdicts import verdict_from_mapping

URL = "/api/internal/provider-capability-proofs"
T1 = datetime(2026, 9, 29, 6, 0, tzinfo=UTC)
CELL = ("cursor", "first_reply_received", "cursor_helm_launch_send", "default")


def _row(*, at: datetime = T1, failures: int = 2, outcome: str = "infrastructure_error", cell=CELL) -> dict:
    provider, assertion_id, scenario_id, variant = cell
    return {
        "provider": provider,
        "assertion_id": assertion_id,
        "scenario_id": scenario_id,
        "variant": variant,
        "outcome": outcome,
        "observed_at": at.isoformat().replace("+00:00", "Z"),
        "consecutive_failures": failures,
    }


def _bundle_of(*rows: dict) -> dict:
    return {"schema_version": 1, "artifact_kind": VERDICT_BUNDLE_KIND, "verdicts": list(rows)}


def _post(client, payload: dict, *, headers: dict[str, str] | None = None):
    return client.post(URL, headers=_factory_headers() if headers is None else headers, json=payload)


def _stored(tmp_path: Path) -> dict:
    return {key: verdict.serialize() for key, verdict in CellVerdictStore(tmp_path / "cell-verdicts").verdicts().items()}


def test_publication_needs_the_factory_token(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    try:
        wrong = _post(client, _bundle_of(_row()), headers={"X-Provider-Capability-Factory-Token": "wrong"})
        device_only = _post(client, _bundle_of(_row()), headers={"X-Agents-Token": "device-token"})
        missing = _post(client, _bundle_of(_row()), headers={})
    finally:
        api_app.dependency_overrides.clear()
    assert wrong.status_code == device_only.status_code == missing.status_code == 403
    assert _stored(tmp_path) == {}


def test_publish_stores_the_cell_and_reports_what_applied(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    try:
        response = _post(client, _bundle_of(_row(), _row(cell=("cursor", "other", "cursor_helm_launch_send", None))))
    finally:
        api_app.dependency_overrides.clear()
    assert response.status_code == 201
    assert response.json() == {"schema_version": 1, "accepted": 2, "applied": 2}
    stored = _stored(tmp_path)
    assert set(stored) == {CELL, ("cursor", "other", "cursor_helm_launch_send", None)}
    assert stored[CELL] == _row()


def test_older_observation_never_overwrites_a_newer_one(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    try:
        newer = _row(at=T1 + timedelta(hours=2), failures=3, outcome="semantic_fail")
        assert _post(client, _bundle_of(newer)).json()["applied"] == 1
        # A late mirror of an earlier tick, and a replay of the same instant.
        older = _post(client, _bundle_of(_row(at=T1, failures=1)))
        same = _post(client, _bundle_of(_row(at=T1 + timedelta(hours=2), failures=9, outcome="infrastructure_error")))
        assert older.status_code == same.status_code == 201
        assert older.json()["applied"] == same.json()["applied"] == 0
        assert _stored(tmp_path)[CELL] == newer
        # Newest wins inside one bundle regardless of order, and a strictly newer one replaces.
        latest = _row(at=T1 + timedelta(hours=3), failures=1)
        mixed = _post(client, _bundle_of(latest, _row(at=T1 + timedelta(hours=2, minutes=30), failures=5)))
        assert mixed.json() == {"schema_version": 1, "accepted": 2, "applied": 1}
        assert _stored(tmp_path)[CELL] == latest
    finally:
        api_app.dependency_overrides.clear()


def test_a_pass_is_a_proof_not_a_verdict(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    try:
        refused = _post(client, _bundle_of(_row(outcome="pass")))
        # Whole bundle or nothing: one bad row must not half-apply.
        half = _post(client, _bundle_of(_row(cell=("cursor", "x", "s", None)), _row(outcome="pass")))
    finally:
        api_app.dependency_overrides.clear()
    assert refused.status_code == half.status_code == 422
    assert "pass" in refused.json()["detail"]
    assert _stored(tmp_path) == {}


@pytest.mark.parametrize(
    "change",
    [
        {"outcome": "bogus"},
        {"consecutive_failures": 0},
        {"consecutive_failures": True},
        {"consecutive_failures": "2"},
        {"observed_at": "2026-09-29T06:00:00"},
        {"observed_at": "not a time"},
        {"observed_at": "2999-01-01T00:00:00Z"},
        {"provider": ""},
        {"variant": 3},
        {"extra": 1},
    ],
)
def test_malformed_verdicts_are_refused(monkeypatch, tmp_path: Path, change: dict) -> None:
    client = _client(monkeypatch, tmp_path)
    try:
        response = _post(client, _bundle_of({**_row(), **change}))
    finally:
        api_app.dependency_overrides.clear()
    assert response.status_code == 422
    assert _stored(tmp_path) == {}


def test_missing_field_and_bad_envelope_are_refused(monkeypatch, tmp_path: Path) -> None:
    incomplete = _row()
    del incomplete["scenario_id"]
    client = _client(monkeypatch, tmp_path)
    try:
        assert _post(client, _bundle_of(incomplete)).status_code == 422
        assert _post(client, _bundle_of()).status_code == 422
        assert _post(client, {**_bundle_of(_row()), "schema_version": 2}).status_code == 422
        assert _post(client, {**_bundle_of(_row()), "records": []}).status_code == 422
    finally:
        api_app.dependency_overrides.clear()
    assert _stored(tmp_path) == {}


def test_proof_bundles_still_reach_the_proof_path(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    try:
        response = _post(client, _bundle(_record()))
    finally:
        api_app.dependency_overrides.clear()
    assert response.status_code == 201
    assert response.json()["accepted"] == 1
    assert _stored(tmp_path) == {}


def test_a_corrupt_store_raises_instead_of_reading_as_empty(tmp_path: Path) -> None:
    root = tmp_path / "cell-verdicts"
    store = CellVerdictStore(root)
    store.publish([verdict_from_mapping(_row())], now=T1)
    (path,) = root.glob("*.json")

    path.write_text("{ not json")
    with pytest.raises(CellVerdictStoreError):
        store.verdicts()

    path.write_text(path.with_name("x").name)  # valid text, not a verdict
    with pytest.raises(CellVerdictStoreError):
        store.verdicts()

    # A well-formed verdict filed under another cell's name is corruption too.
    store2 = CellVerdictStore(tmp_path / "second")
    store2.publish([verdict_from_mapping(_row())], now=T1)
    (good,) = (tmp_path / "second").glob("*.json")
    good.rename(good.with_name("0" * 64 + ".json"))
    with pytest.raises(CellVerdictStoreError):
        store2.verdicts()
    # ...and publishing over a corrupt cell fails loudly rather than clobbering it.
    with pytest.raises(CellVerdictStoreError):
        store.publish([verdict_from_mapping(_row(at=T1 + timedelta(hours=1)))], now=T1 + timedelta(hours=1))


def test_missing_store_is_empty_not_an_error(tmp_path: Path) -> None:
    assert CellVerdictStore(tmp_path / "never-written").verdicts() == {}


def test_verdict_is_scoped_to_the_exact_cell() -> None:
    verdict = verdict_from_mapping(_row())
    assert isinstance(verdict, CellVerdict)
    assert verdict.key == CELL
    assert verdict.observed_at == T1
