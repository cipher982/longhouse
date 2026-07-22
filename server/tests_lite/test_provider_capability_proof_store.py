from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore


def _record(**changes) -> ProviderCapabilityProofRecord:
    record = ProviderCapabilityProofRecord(
        provider="claude",
        provider_version="2.1.0",
        provider_executable_identity="sha256:provider",
        provider_contract_digest="sha256:contract",
        adapter_digest="sha256:adapter",
        scenario_id="coordination_awareness_create",
        scenario_revision=1,
        oracle_digest="sha256:oracle",
        assertion_id="peer_tool_visible",
        outcome=AssertionOutcome.PASS,
        evidence_class=EvidenceClass.HERMETIC,
        generated_at="2026-07-22T16:00:00Z",
        producer_class="local_machine",
        producer_version="2",
        invocation_id="run-123",
    )
    return replace(record, **changes)


def test_store_is_append_only_and_idempotent(tmp_path: Path) -> None:
    store = ProviderCapabilityProofStore(tmp_path)
    first = _record()
    second = _record(
        generated_at="2026-07-22T16:01:00Z",
        outcome=AssertionOutcome.INFRASTRUCTURE_ERROR,
        invocation_id="run-456",
    )

    first_path = store.write(first)
    assert store.write(first) == first_path
    store.write(second)

    assert store.records("claude") == (first, second)
    assert first_path.name == f"{first.artifact_id}.json"
    index = json.loads((tmp_path / "claude" / "index.json").read_text())
    assert index["artifact_ids"] == [first.artifact_id, second.artifact_id]


def test_store_reads_records_without_trusting_index(tmp_path: Path) -> None:
    store = ProviderCapabilityProofStore(tmp_path)
    record = _record()
    store.write(record)
    (tmp_path / "claude" / "index.json").write_text('{"artifact_ids": []}\n')

    assert store.records("claude") == (record,)
    store.rebuild_index("claude")
    index = json.loads((tmp_path / "claude" / "index.json").read_text())
    assert index["artifact_ids"] == [record.artifact_id]


def test_concurrent_identical_writers_are_idempotent(tmp_path: Path) -> None:
    store = ProviderCapabilityProofStore(tmp_path)
    record = _record()

    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda _: store.write(record), range(16)))

    assert len(set(paths)) == 1
    assert store.records("claude") == (record,)


def test_store_rejects_tampered_record(tmp_path: Path) -> None:
    store = ProviderCapabilityProofStore(tmp_path)
    path = store.write(_record())
    payload = json.loads(path.read_text())
    payload["provider_version"] = "tampered"
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="artifact_id does not match"):
        store.records("claude")


@pytest.mark.parametrize("provider", ["../claude", "Claude", "", "a/b"])
def test_store_rejects_unsafe_provider_paths(tmp_path: Path, provider: str) -> None:
    store = ProviderCapabilityProofStore(tmp_path)

    with pytest.raises(ValueError, match="invalid provider"):
        store.records(provider)
