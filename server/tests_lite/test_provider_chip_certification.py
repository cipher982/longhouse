"""The certified landing layer: chip proof edges joined to published proofs."""

from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
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
from zerg.services.provider_capability_cell_verdicts import CellVerdict
from zerg.services.provider_capability_cell_verdicts import CellVerdictStore
from zerg.services.provider_capability_cell_verdicts import CellVerdictStoreError
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


def _verdicts(edge, *, at: datetime, failures: int, outcome: str = "infrastructure_error"):
    return [
        CellVerdict(
            provider=a.provider,
            assertion_id=a.assertion_id,
            scenario_id=a.scenario_id,
            variant=a.variant,
            outcome=outcome,
            observed_at=at,
            consecutive_failures=failures,
        )
        for a in edge
    ]


def _payload_with_verdicts(monkeypatch, tmp_path: Path, proofs, verdicts) -> dict:
    store = CellVerdictStore(tmp_path / "verdicts")
    store.publish(list(verdicts), now=NOW)
    monkeypatch.setattr(routes, "_cell_verdict_store", lambda: store)
    return _payload(monkeypatch, tmp_path, proofs)


def test_two_consecutive_failures_after_a_pass_unlight_the_chip_as_unverified(monkeypatch, tmp_path: Path) -> None:
    edge = _edge("pi", "steerMidTurn")
    passed = [_proof(a, at=NOW - timedelta(hours=2)) for a in edge]
    failed_twice = _verdicts(edge, at=NOW - timedelta(minutes=5), failures=2)
    chip = _chip(_payload_with_verdicts(monkeypatch, tmp_path, passed, failed_twice), "pi", "steerMidTurn")
    # Not certified, and an infrastructure failure never reads as a product failure.
    assert chip["state"] == "unverified"
    for row in chip["requirements"]:
        assert row["proof_status"] == "infrastructure_error"
        assert row["latest_outcome"] == "infrastructure_error"
        assert row["proven_at"] is None
        # The row still names what the last pass ran against.
        assert row["longhouse_git_sha"] == "a" * 40


def test_one_failure_after_a_pass_leaves_the_chip_certified(monkeypatch, tmp_path: Path) -> None:
    edge = _edge("pi", "steerMidTurn")
    passed = [_proof(a, at=NOW - timedelta(hours=2)) for a in edge]
    failed_once = _verdicts(edge, at=NOW - timedelta(minutes=5), failures=1)
    chip = _chip(_payload_with_verdicts(monkeypatch, tmp_path, passed, failed_once), "pi", "steerMidTurn")
    assert chip["state"] == "certified"
    # The failure stays visible without changing the claim.
    assert {row["latest_outcome"] for row in chip["requirements"]} == {"infrastructure_error"}
    assert all(row["proof_status"] == "pass" and row["proven_at"] for row in chip["requirements"])


def test_a_lone_failed_proof_record_is_one_failure_and_does_not_revoke(monkeypatch, tmp_path: Path) -> None:
    edge = _edge("pi", "steerMidTurn")
    recent_pass = [_proof(a, at=NOW - timedelta(hours=2), suffix="pass") for a in edge]
    newer_fail = [_proof(a, outcome=AssertionOutcome.SEMANTIC_FAIL, at=NOW - timedelta(minutes=5), suffix="fail") for a in edge]
    assert _chip(_payload(monkeypatch, tmp_path, recent_pass + newer_fail), "pi", "steerMidTurn")["state"] == "certified"


def test_a_failure_older_than_the_pass_is_ignored_because_the_cell_passed_again(monkeypatch, tmp_path: Path) -> None:
    edge = _edge("pi", "steerMidTurn")
    passed = [_proof(a, at=NOW - timedelta(hours=1)) for a in edge]
    earlier = _verdicts(edge, at=NOW - timedelta(hours=3), failures=5)
    assert _chip(_payload_with_verdicts(monkeypatch, tmp_path, passed, earlier), "pi", "steerMidTurn")["state"] == "certified"


def test_one_revoked_requirement_unlights_the_whole_chip(monkeypatch, tmp_path: Path) -> None:
    edge = _edge("pi", "steerMidTurn")
    passed = [_proof(a, at=NOW - timedelta(hours=2)) for a in edge]
    chip = _chip(
        _payload_with_verdicts(monkeypatch, tmp_path, passed, _verdicts(edge[:1], at=NOW - timedelta(minutes=5), failures=2)),
        "pi",
        "steerMidTurn",
    )
    assert chip["state"] == "unverified"
    assert sorted(row["proof_status"] for row in chip["requirements"]) == sorted(["infrastructure_error"] + ["pass"] * (len(edge) - 1))


def test_a_semantic_verdict_reads_failing_and_only_a_semantic_one(monkeypatch, tmp_path: Path) -> None:
    edge = _edge("pi", "steerMidTurn")
    passed = [_proof(a, at=NOW - timedelta(hours=2)) for a in edge]
    verdicts = _verdicts(edge, at=NOW - timedelta(minutes=5), failures=2, outcome="semantic_fail")
    assert _chip(_payload_with_verdicts(monkeypatch, tmp_path, passed, verdicts), "pi", "steerMidTurn")["state"] == "failing"


def test_a_pass_that_aged_out_is_stale_and_a_failure_cannot_revive_it(monkeypatch, tmp_path: Path) -> None:
    edge = _edge("pi", "steerMidTurn")
    expired = NOW - timedelta(seconds=max(a.max_age_seconds for a in edge) + 60)
    old_pass = [_proof(a, at=expired, suffix="old") for a in edge]
    newer_fail = [_proof(a, outcome=AssertionOutcome.SEMANTIC_FAIL, at=NOW - timedelta(minutes=5), suffix="fail") for a in edge]
    assert _chip(_payload(monkeypatch, tmp_path / "a", old_pass), "pi", "steerMidTurn")["state"] == "stale"
    assert _chip(_payload(monkeypatch, tmp_path / "b", old_pass + newer_fail), "pi", "steerMidTurn")["state"] == "failing"


def test_an_unreadable_verdict_store_fails_the_read_instead_of_certifying(monkeypatch, tmp_path: Path) -> None:
    edge = _edge("pi", "steerMidTurn")
    passed = [_proof(a, at=NOW - timedelta(hours=2)) for a in edge]
    store = CellVerdictStore(tmp_path / "verdicts")
    store.publish(_verdicts(edge[:1], at=NOW - timedelta(minutes=5), failures=2), now=NOW)
    (verdict_file,) = (tmp_path / "verdicts").glob("*.json")
    verdict_file.write_text("{ corrupt")
    monkeypatch.setattr(routes, "_cell_verdict_store", lambda: store)
    with pytest.raises(CellVerdictStoreError):
        _payload(monkeypatch, tmp_path, passed)
    # The public route then answers 5xx, which the landing page renders as
    # "unavailable"; it never caches or serves a chart missing a fact.
    monkeypatch.setattr(routes, "_certification_cache", None)
    response = TestClient(app, backend="asyncio", raise_server_exceptions=False).get("/api/public/provider-certification")
    assert response.status_code == 500
    assert routes._certification_cache is None


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
