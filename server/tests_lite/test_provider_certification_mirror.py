"""The public demo host certifies landing chips from mirrored v4 factory proofs.

The factory mirrors each published proof to the demo Runtime Host. A v4 bundle
carries blob references, not bytes, so the demo host must resolve and verify
them at publication exactly as the primary host does; afterwards certification
reads only stored reference metadata. Being auth-disabled, the demo host must
never serve the records or the evidence it verified.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from tests_lite.test_provider_capability_proof_v4_routes import _client
from tests_lite.test_provider_capability_proof_v4_routes import _digest
from tests_lite.test_provider_capability_proof_v4_routes import _evidence_url
from tests_lite.test_provider_capability_proof_v4_routes import _MemoryResolver
from tests_lite.test_provider_capability_proof_v4_routes import _publish
from tests_lite.test_provider_capability_proof_v4_routes import _record
from tests_lite.test_provider_capability_proof_v4_routes import _v4_bundle
from zerg.routers import provider_capability_proofs as routes
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore
from zerg.services.provider_capability_schema import load_chip_edge_assertions

NOW = datetime(2026, 9, 17, 1, 0, tzinfo=UTC)


def _demo_client(monkeypatch, tmp_path: Path, resolver: _MemoryResolver):
    client = _client(monkeypatch, tmp_path, resolver)
    monkeypatch.setattr(
        routes,
        "get_settings",
        lambda: SimpleNamespace(provider_capability_factory_token="fixture-factory-token", demo_mode=True),
    )
    monkeypatch.setattr(routes, "_legacy_proof_store", lambda: ProviderCapabilityProofStore(tmp_path / "legacy"))
    return client


def test_mirrored_v4_proofs_certify_a_chip_without_exposing_evidence(monkeypatch, tmp_path: Path) -> None:
    edge = load_chip_edge_assertions()["pi"]["steerMidTurn"]
    assert edge, "pi steerMidTurn must carry a proof edge in the real schema"
    contents: dict[str, bytes] = {}
    bundles = []
    for index, assertion in enumerate(edge):
        record = _record(
            provider=assertion.provider,
            provider_version="0.85.1",
            scenario_id=assertion.scenario_id,
            scenario_revision=assertion.minimum_scenario_revision,
            assertion_id=assertion.assertion_id,
            assertion_variant=assertion.variant,
            evidence_class=EvidenceClass.LIVE_TOKEN,
            generated_at=(NOW - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            invocation_id=f"factory-run-{index}",
            run_reference=f"factory-test/run-{index}",
            longhouse_git_sha="b" * 40,
        )
        bundle, bundle_contents = _v4_bundle(record)
        contents.update(bundle_contents)
        bundles.append((record, bundle))
    resolver = _MemoryResolver(contents)

    with _demo_client(monkeypatch, tmp_path, resolver) as client:
        for _record_, bundle in bundles:
            response = _publish(client, bundle)
            assert response.status_code == 201, response.text
        assert resolver.calls, "publication must verify the referenced bytes"

        controls = {
            "schema_version": 1,
            "artifact_kind": "provider_negative_control_snapshot",
            "epoch_digest": bundles[0][0].accepted_epoch_digest,
            "published_at": "2026-09-17T00:55:00Z",
            "controls": [
                {"provider": "pi", "target_assertion": a.assertion_id, "fault": f"fault-{a.assertion_id}", "verdict": "pass"} for a in edge
            ],
        }
        published = client.post(
            "/api/internal/provider-negative-controls",
            headers={"X-Provider-Capability-Factory-Token": "fixture-factory-token"},
            json=controls,
        )
        assert published.status_code == 201, published.text

        reads_after_publication = list(resolver.calls)
        payload = routes.build_chip_certification_payload(now=NOW)
        chip = next(row for row in payload["providers"] if row["provider"] == "pi")["chips"]["steerMidTurn"]
        assert chip["state"] == "certified", chip
        assert {row["longhouse_git_sha"] for row in chip["requirements"]} == {"b" * 40}
        assert resolver.calls == reads_after_publication, "certification must not download evidence"

        assert client.get("/api/agents/provider-capability-proofs").status_code == 404
        assert client.get(_evidence_url(_digest("raw"))).status_code == 404


def test_demo_host_without_a_resolver_refuses_v4_publication(monkeypatch, tmp_path: Path) -> None:
    record = replace(_record(), invocation_id="factory-run-unresolved")
    bundle, contents = _v4_bundle(record)
    with _demo_client(monkeypatch, tmp_path, _MemoryResolver(contents)) as client:
        monkeypatch.setattr(routes, "_blob_resolver", lambda: None)
        response = _publish(client, bundle)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "provider_capability_blob_store_unavailable"


def test_demo_guard_admits_only_the_token_gated_factory_publication_writes() -> None:
    import asyncio

    from zerg.middleware.demo_guard import DemoGuardMiddleware

    reached: list[str] = []

    async def app(scope, receive, send):
        reached.append(scope["path"])

    async def send(message):
        pass

    guard = DemoGuardMiddleware(app)
    for path in (
        "/api/internal/provider-capability-proofs",
        "/api/internal/provider-negative-controls",
        "/api/agents/sessions",
    ):
        asyncio.run(guard({"type": "http", "method": "POST", "path": path}, None, send))
    assert reached == ["/api/internal/provider-capability-proofs", "/api/internal/provider-negative-controls"]
