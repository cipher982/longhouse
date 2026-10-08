"""Per-provider-version evidence facts for agents choosing provider pins."""

from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi import status

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from tests_lite.test_provider_capability_proof_routes import _client
from tests_lite.test_provider_capability_proof_routes import _record
from tests_lite.test_provider_capability_proof_routes import _write_trusted
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.routers import provider_capability_proofs as routes
from zerg.services.provider_capability_cell_verdicts import CellVerdict
from zerg.services.provider_capability_cell_verdicts import CellVerdictStore
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore

URL = "/api/agents/provider-version-evidence"


def _verdict(provider: str, assertion_id: str, failures: int) -> CellVerdict:
    return CellVerdict(
        provider=provider,
        assertion_id=assertion_id,
        scenario_id="helm_scenario",
        variant="default",
        outcome="infrastructure_error",
        observed_at=datetime.now(UTC),
        consecutive_failures=failures,
    )


def _seed(store: ProviderCapabilityProofStore) -> None:
    _write_trusted(store, _record(provider="codex", provider_version="0.145.0", invocation_id="a", generated_at="2026-10-01T10:00:00Z"))
    _write_trusted(store, _record(provider="codex", provider_version="0.145.0", invocation_id="b", generated_at="2026-10-02T10:00:00Z"))
    _write_trusted(store, _record(provider="codex", provider_version="0.146.0", invocation_id="c", generated_at="2026-10-03T10:00:00Z"))
    _write_trusted(store, _record(provider="claude", provider_version="0.145.0", invocation_id="d", generated_at="2026-10-03T10:00:00Z"))


def test_version_evidence_returns_only_matching_records_newest_first(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    _seed(ProviderCapabilityProofStore(tmp_path / "proofs", require_authenticated_publication=True))
    try:
        response = client.get(URL, params={"provider": "codex", "version": "0.145.0"})
    finally:
        api_app.dependency_overrides.clear()

    payload = response.json()
    assert response.status_code == 200
    assert payload["provider"] == "codex"
    assert payload["version"] == "0.145.0"
    assert [record["generated_at"] for record in payload["records"]] == ["2026-10-02T10:00:00Z", "2026-10-01T10:00:00Z"]
    assert all(
        set(record) == {"assertion_id", "scenario_id", "variant", "outcome", "evidence_class", "longhouse_git_sha", "generated_at"}
        for record in payload["records"]
    )
    assert all(record["outcome"] == "pass" for record in payload["records"])
    assert payload["truncated"] is False
    assert isinstance(payload["required_assertions"], list)
    assert payload["required_assertions"] == sorted(set(payload["required_assertions"]))
    assert payload["required_assertions"]


def test_failing_verdicts_need_two_consecutive_failures_for_this_provider(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    cells = CellVerdictStore(tmp_path / "cell-verdicts")
    cells.publish(
        [
            _verdict("codex", "reconnect_survives", 2),
            _verdict("codex", "single_flake", 1),
            _verdict("claude", "other_provider", 5),
        ]
    )
    try:
        response = client.get(URL, params={"provider": "codex", "version": "0.145.0"})
    finally:
        api_app.dependency_overrides.clear()

    payload = response.json()
    assert response.status_code == 200
    assert [verdict["assertion_id"] for verdict in payload["failing_verdicts"]] == ["reconnect_survives"]
    verdict = payload["failing_verdicts"][0]
    assert verdict["consecutive_failures"] == 2
    assert verdict["outcome"] == "infrastructure_error"
    assert verdict["provider"] == "codex"
    assert "observed_at" in verdict


def test_unknown_provider_or_version_returns_empty_facts(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    _seed(ProviderCapabilityProofStore(tmp_path / "proofs", require_authenticated_publication=True))
    try:
        unknown_provider = client.get(URL, params={"provider": "no-such-cli", "version": "1.0.0"})
        unknown_version = client.get(URL, params={"provider": "codex", "version": "9.9.9"})
    finally:
        api_app.dependency_overrides.clear()

    for response in (unknown_provider, unknown_version):
        assert response.status_code == 200
        assert response.json()["records"] == []
        assert response.json()["failing_verdicts"] == []
    assert unknown_provider.json()["required_assertions"] == []


def test_version_evidence_requires_agents_auth(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)

    def reject_machine():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing machine token")

    api_app.dependency_overrides[verify_agents_token] = reject_machine
    try:
        response = client.get(URL, params={"provider": "codex", "version": "0.145.0"})
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 401


def test_version_evidence_is_refused_on_public_demo(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(demo_mode=True, provider_capability_factory_token=None))
    try:
        response = client.get(URL, params={"provider": "codex", "version": "0.145.0"})
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 404


def test_version_evidence_requires_both_query_params(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    try:
        response = client.get(URL, params={"provider": "codex"})
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 422
