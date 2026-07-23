from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from io import BytesIO
from pathlib import Path

from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_remote_proof import load_cached_provider_capability_proofs
from zerg.services.provider_capability_remote_proof import refresh_cached_provider_capability_proofs


def _record(**changes) -> ProviderCapabilityProofRecord:
    return replace(
        ProviderCapabilityProofRecord(
            provider="codex",
            provider_version="0.145.0",
            provider_executable_identity="sha256:provider",
            provider_contract_digest="sha256:contract",
            adapter_digest="sha256:adapter",
            scenario_id="codex_helm_interrupt",
            scenario_revision=1,
            oracle_digest="sha256:oracle",
            assertion_id="interrupt_acknowledged",
            outcome=AssertionOutcome.PASS,
            evidence_class=EvidenceClass.LIVE_TOKEN,
            generated_at="2026-07-22T18:00:00Z",
            producer_class="release_factory",
            producer_version="1",
            invocation_id="factory-run-123",
            run_reference="factory://run-123",
            raw_reference_digests=("sha256:raw",),
        ),
        **changes,
    )


def _bundle(*records: ProviderCapabilityProofRecord, trusted_ids: list[str] | None = None) -> dict:
    return {
        "schema_version": 1,
        "artifact_kind": "trusted_provider_capability_proof_bundle",
        "records": [record.serialize() for record in records],
        "trusted_artifact_ids": trusted_ids if trusted_ids is not None else [record.artifact_id for record in records],
    }


class Response:
    def __init__(self, payload: bytes, *, content_length: str | None = None) -> None:
        self._stream = BytesIO(payload)
        self.status = 200
        self.headers = {
            "Content-Length": content_length if content_length is not None else str(len(payload)),
            "Content-Encoding": "identity",
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


def test_fetch_uses_machine_auth_and_atomically_caches_server_trust(tmp_path: Path) -> None:
    trusted = _record()
    untrusted = _record(assertion_id="other", invocation_id="factory-run-456")
    encoded = json.dumps(_bundle(trusted, untrusted, trusted_ids=[trusted.artifact_id])).encode()
    captured = {}

    def opener(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response(encoded)

    fetched = refresh_cached_provider_capability_proofs(
        tmp_path,
        runtime_url="https://runtime.example",
        token="zdt_secret",
        opener=opener,
        now=lambda: datetime(2026, 7, 22, 19, 0, tzinfo=UTC),
    )

    assert captured["request"].full_url == "https://runtime.example/api/agents/provider-capability-proofs"
    assert captured["request"].get_header("X-agents-token") == "zdt_secret"
    assert fetched.records_by_provider == {"codex": (trusted,)}
    assert fetched.trusted_artifact_ids == frozenset({trusted.artifact_id})
    assert fetched.summary["refresh_state"] == "refreshed"
    cache_path = tmp_path / "provider-capability-proofs" / "trusted-runtime-cache.json"
    assert cache_path.stat().st_mode & 0o777 == 0o600
    loaded = load_cached_provider_capability_proofs(tmp_path, runtime_url="https://runtime.example")
    assert loaded.records_by_provider == {"codex": (trusted,)}


def test_nonlocal_http_is_rejected_without_network_or_cache_replacement(tmp_path: Path) -> None:
    calls = []
    result = refresh_cached_provider_capability_proofs(
        tmp_path,
        runtime_url="http://runtime.example",
        token="zdt_secret",
        opener=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert calls == []
    assert result.summary["refresh_state"] == "invalid_runtime_url"
    assert result.records_by_provider == {}
    assert not (tmp_path / "provider-capability-proofs" / "trusted-runtime-cache.json").exists()


def test_invalid_or_offline_refresh_preserves_last_valid_cache(tmp_path: Path) -> None:
    record = _record()
    valid = json.dumps(_bundle(record)).encode()
    refresh_cached_provider_capability_proofs(
        tmp_path,
        runtime_url="http://127.0.0.1:8080",
        token="zdt_secret",
        opener=lambda *_args, **_kwargs: Response(valid),
    )
    cache_path = tmp_path / "provider-capability-proofs" / "trusted-runtime-cache.json"
    before = cache_path.read_bytes()

    invalid = refresh_cached_provider_capability_proofs(
        tmp_path,
        runtime_url="http://127.0.0.1:8080",
        token="zdt_secret",
        opener=lambda *_args, **_kwargs: Response(b'{"schema_version": 99}'),
    )

    assert invalid.records_by_provider == {"codex": (record,)}
    assert invalid.summary["refresh_state"] == "failed"
    assert cache_path.read_bytes() == before


def test_cache_is_bound_to_runtime_origin(tmp_path: Path) -> None:
    record = _record()
    encoded = json.dumps(_bundle(record)).encode()
    refresh_cached_provider_capability_proofs(
        tmp_path,
        runtime_url="https://one.example",
        token="zdt_secret",
        opener=lambda *_args, **_kwargs: Response(encoded),
    )

    mismatched = load_cached_provider_capability_proofs(tmp_path, runtime_url="https://two.example")

    assert mismatched.records_by_provider == {}
    assert mismatched.trusted_artifact_ids == frozenset()
    assert mismatched.summary["cache_state"] == "origin_mismatch"


def test_oversized_response_is_rejected_before_reading_body(tmp_path: Path) -> None:
    result = refresh_cached_provider_capability_proofs(
        tmp_path,
        runtime_url="https://runtime.example",
        token="zdt_secret",
        opener=lambda *_args, **_kwargs: Response(b"{}", content_length=str(3 * 1024 * 1024)),
    )

    assert result.records_by_provider == {}
    assert result.summary["refresh_state"] == "failed"
    assert "size limit" in result.summary["error"]
