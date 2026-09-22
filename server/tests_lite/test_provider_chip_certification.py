"""The certified landing layer: chip proof edges joined to published proofs."""

from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from tests_lite.test_provider_capability_proof_routes import _record
from tests_lite.test_provider_capability_proof_routes import _write_trusted
from zerg.main import api_app
from zerg.main import app
from zerg.routers import provider_capability_proofs as routes
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore
from zerg.services.provider_capability_schema import load_chip_edge_assertions
from zerg.services.provider_chip_edges import rollup_state

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def test_rollup_keeps_red_candidates_from_revoking_and_names_every_state() -> None:
    assert rollup_state(["pass", "pass"]) == "certified"
    assert rollup_state(["pass", "semantic_fail"]) == "failing"
    assert rollup_state(["pass", "stale"]) == "stale"
    assert rollup_state(["semantic_fail", "stale"]) == "failing"
    for status in ("never_proven", "infrastructure_error", "blocked", "skipped", "unacceptable_evidence"):
        assert rollup_state(["pass", status]) == "unverified"
    assert rollup_state([]) == "unverified"


def _edge(provider: str, chip: str):
    assertions = load_chip_edge_assertions()[provider][chip]
    assert assertions, f"{provider}.{chip} has no proof edge in the real schema"
    return assertions


def _proof(assertion, *, outcome=AssertionOutcome.PASS, at: datetime, sha: str = "a" * 40, suffix: str = ""):
    return _record(
        provider=assertion.provider,
        provider_version="1.2.3",
        scenario_id=assertion.scenario_id,
        scenario_revision=assertion.minimum_scenario_revision,
        assertion_id=assertion.assertion_id,
        assertion_variant=assertion.variant,
        outcome=outcome,
        evidence_class=EvidenceClass.LIVE_TOKEN,
        generated_at=at.isoformat().replace("+00:00", "Z"),
        invocation_id=f"run-{assertion.assertion_id}-{assertion.variant}-{suffix or at.timestamp()}",
        longhouse_git_sha=sha,
    )


def _payload(monkeypatch, tmp_path: Path, proofs) -> dict:
    store = ProviderCapabilityProofStore(tmp_path / "proofs", require_authenticated_publication=True)
    monkeypatch.setattr(routes, "_proof_store", lambda: store)
    monkeypatch.setattr(routes, "_legacy_proof_store", lambda: ProviderCapabilityProofStore(tmp_path / "legacy"))
    for proof in proofs:
        _write_trusted(store, proof)
    return routes.build_chip_certification_payload(now=NOW)


def _chip(payload: dict, provider: str, chip: str) -> dict:
    return next(row for row in payload["providers"] if row["provider"] == provider)["chips"][chip]


def test_every_edge_passing_certifies_and_rows_scope_the_claim(monkeypatch, tmp_path: Path) -> None:
    edge = _edge("pi", "steerMidTurn")
    payload = _payload(monkeypatch, tmp_path, [_proof(a, at=NOW - timedelta(hours=1)) for a in edge])
    chip = _chip(payload, "pi", "steerMidTurn")
    assert chip["state"] == "certified"
    assert {row["assertion_id"] for row in chip["requirements"]} == {a.assertion_id for a in edge}
    assert all(row["longhouse_git_sha"] == "a" * 40 and row["provider_version"] == "1.2.3" for row in chip["requirements"])
    # Nothing else was published, so every other covered chip is unverified,
    # and a chip with no edge at all is unproven -- never silently lit.
    antigravity = next(row for row in payload["providers"] if row["provider"] == "antigravity")["chips"]
    assert antigravity["steerMidTurn"] == {"state": "unproven", "requirements": []}
    assert _chip(payload, "pi", "resume")["state"] == "unverified"


def test_newer_failure_does_not_revoke_an_admissible_pass_until_it_ages_out(monkeypatch, tmp_path: Path) -> None:
    edge = _edge("pi", "steerMidTurn")
    recent_pass = [_proof(a, at=NOW - timedelta(hours=2), suffix="pass") for a in edge]
    newer_fail = [_proof(a, outcome=AssertionOutcome.SEMANTIC_FAIL, at=NOW - timedelta(minutes=5), suffix="fail") for a in edge]
    assert _chip(_payload(monkeypatch, tmp_path / "a", recent_pass + newer_fail), "pi", "steerMidTurn")["state"] == "certified"

    expired = NOW - timedelta(seconds=max(a.max_age_seconds for a in edge) + 60)
    old_pass = [_proof(a, at=expired, suffix="old") for a in edge]
    assert _chip(_payload(monkeypatch, tmp_path / "b", old_pass), "pi", "steerMidTurn")["state"] == "stale"
    assert _chip(_payload(monkeypatch, tmp_path / "c", old_pass + newer_fail), "pi", "steerMidTurn")["state"] == "failing"


def test_infrastructure_error_is_unknown_not_failure(monkeypatch, tmp_path: Path) -> None:
    edge = _edge("pi", "steerMidTurn")
    proofs = [_proof(a, outcome=AssertionOutcome.INFRASTRUCTURE_ERROR, at=NOW - timedelta(minutes=5)) for a in edge]
    assert _chip(_payload(monkeypatch, tmp_path, proofs), "pi", "steerMidTurn")["state"] == "unverified"


def test_public_route_needs_no_auth_and_leaks_no_evidence_locations(monkeypatch, tmp_path: Path) -> None:
    store = ProviderCapabilityProofStore(tmp_path / "proofs", require_authenticated_publication=True)
    monkeypatch.setattr(routes, "_proof_store", lambda: store)
    monkeypatch.setattr(routes, "_legacy_proof_store", lambda: ProviderCapabilityProofStore(tmp_path / "legacy"))
    monkeypatch.setattr(routes, "_certification_cache", None)
    api_app.dependency_overrides.clear()
    response = TestClient(app, backend="asyncio").get("/api/public/provider-certification")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=60"
    body = response.text
    payload = response.json()
    assert payload["artifact_kind"] == "provider_chip_certification"
    for provider in payload["providers"]:
        for chip in provider["chips"].values():
            assert "controls" not in chip
            assert all("negative_controls" not in requirement for requirement in chip["requirements"])
    for forbidden in ("artifact_id", "run_reference", "raw_reference", "worker_id", "invocation_id"):
        assert forbidden not in body
