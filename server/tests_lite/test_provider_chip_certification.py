"""The certified landing layer: chip proof edges joined to published proofs."""

from __future__ import annotations

import json
import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

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


def _write_controls(tmp_path: Path, controls: list[dict] | None, epoch_digest: str | None = None) -> None:
    if controls is None:
        return
    path = tmp_path / "negative-controls.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_kind": "provider_negative_control_snapshot",
                "epoch_digest": epoch_digest or _record().accepted_epoch_digest,
                "published_at": "2026-09-16T11:00:00Z",
                "controls": controls,
            }
        ),
        encoding="utf-8",
    )


def _payload(monkeypatch, tmp_path: Path, proofs, controls: list[dict] | None = None, *, control_epoch: str | None = None) -> dict:
    store = ProviderCapabilityProofStore(tmp_path / "proofs", require_authenticated_publication=True)
    monkeypatch.setattr(routes, "_proof_store", lambda: store)
    monkeypatch.setattr(routes, "_legacy_proof_store", lambda: ProviderCapabilityProofStore(tmp_path / "legacy"))
    for proof in proofs:
        _write_trusted(store, proof)
    _write_controls(tmp_path, [] if controls is None else controls, control_epoch)
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
    assert response.json()["artifact_kind"] == "provider_chip_certification"
    for forbidden in ("artifact_id", "run_reference", "raw_reference", "worker_id", "invocation_id"):
        assert forbidden not in body


def _steer_control(verdict: str) -> dict:
    return {"provider": "pi", "target_assertion": "pi_helm_steer_active", "fault": "pi_steer_as_follow_up", "verdict": verdict}


def test_a_passing_chip_certifies_and_reports_its_control_status(monkeypatch, tmp_path: Path) -> None:
    edge = _edge("pi", "steerMidTurn")
    proofs = [_proof(a, at=NOW - timedelta(hours=1)) for a in edge]
    # The control layer is reported, never enforced: a chip with passing proofs
    # certifies whatever the controls say, so the public claim matches its own
    # caption ("a current passing live test against the real binary"). A control
    # that never ran must not read as a product failure.
    for verdict, expected in (
        ("pass", "passed"),
        ("fail", "failed"),
        ("inconclusive", "failed"),
        ("not_recorded", "not_run"),
    ):
        chip = _chip(_payload(monkeypatch, tmp_path / verdict, proofs, [_steer_control(verdict)]), "pi", "steerMidTurn")
        assert chip["state"] == "certified", verdict
        assert chip["controls"] == expected, verdict
        assert "blocked_by" not in chip, verdict


def test_controls_from_another_epoch_are_reported_not_enforced(monkeypatch, tmp_path: Path) -> None:
    edge = _edge("pi", "steerMidTurn")
    proofs = [_proof(assertion, at=NOW - timedelta(hours=1)) for assertion in edge]
    payload = _payload(monkeypatch, tmp_path, proofs, [_steer_control("pass")], control_epoch="sha256:" + "f" * 64)
    chip = _chip(payload, "pi", "steerMidTurn")
    # A control judged another epoch is evidence about other code, so it is
    # reported as such -- and still does not withhold the chip's own proofs.
    assert chip["state"] == "certified"
    assert chip["controls"] == "epoch_mismatch"


def test_a_missing_control_snapshot_is_reported_not_enforced(monkeypatch, tmp_path: Path) -> None:
    edge = _edge("pi", "resume")
    store = ProviderCapabilityProofStore(tmp_path / "proofs", require_authenticated_publication=True)
    monkeypatch.setattr(routes, "_proof_store", lambda: store)
    monkeypatch.setattr(routes, "_legacy_proof_store", lambda: ProviderCapabilityProofStore(tmp_path / "legacy"))
    for proof in [_proof(a, at=NOW - timedelta(hours=1)) for a in edge]:
        _write_trusted(store, proof)
    chip = _chip(routes.build_chip_certification_payload(now=NOW), "pi", "resume")
    assert chip["state"] == "certified"
    assert chip["controls"] == "snapshot_missing"


def test_factory_publishes_the_negative_control_snapshot_with_its_token(monkeypatch, tmp_path: Path) -> None:
    store = ProviderCapabilityProofStore(tmp_path / "proofs", require_authenticated_publication=True)
    monkeypatch.setattr(routes, "_proof_store", lambda: store)
    monkeypatch.setattr(routes, "_legacy_proof_store", lambda: ProviderCapabilityProofStore(tmp_path / "legacy"))
    for assertion in _edge("pi", "steerMidTurn"):
        _write_trusted(store, _proof(assertion, at=NOW - timedelta(hours=1)))
    # Certification follows the proofs; the control layer is reported. The
    # snapshot's effect is therefore visible in `controls`, not in the state.
    before = _chip(routes.build_chip_certification_payload(now=NOW), "pi", "steerMidTurn")
    assert before["state"] == "certified"
    assert before["controls"] == "snapshot_missing"
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(provider_capability_factory_token="fixture-factory-token"))
    monkeypatch.setattr(routes, "_certification_cache", None)
    api_app.dependency_overrides.clear()
    client = TestClient(app, backend="asyncio")
    snapshot = {
        "schema_version": 1,
        "artifact_kind": "provider_negative_control_snapshot",
        "epoch_digest": _record().accepted_epoch_digest,
        "published_at": "2026-09-16T11:00:00Z",
        "controls": [_steer_control("pass")],
    }
    url = "/api/internal/provider-negative-controls"
    assert client.post(url, json=snapshot).status_code == 403
    assert (
        client.post(
            url,
            json={**snapshot, "controls": [{**_steer_control("pass"), "verdict": "maybe"}]},
            headers={"X-Provider-Capability-Factory-Token": "fixture-factory-token"},
        ).status_code
        == 422
    )
    response = client.post(url, json=snapshot, headers={"X-Provider-Capability-Factory-Token": "fixture-factory-token"})
    assert response.status_code == 201, response.text
    after = _chip(routes.build_chip_certification_payload(now=NOW), "pi", "steerMidTurn")
    assert after["state"] == "certified"
    assert after["controls"] == "passed"
