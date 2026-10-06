"""Regression coverage for compacted machine-evidence heartbeats."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
from typing import Any

from tests_lite.test_heartbeat_endpoint import _catalog_rows  # noqa: F401
from tests_lite.test_heartbeat_endpoint import _headers
from tests_lite.test_heartbeat_endpoint import live_catalog  # noqa: F401
from tests_lite.test_heartbeat_endpoint import live_catalog_client  # noqa: F401
from zerg.catalogd.fact_reducer import reducer_facts_from_machine_evidence
from zerg.catalogd.models import FactHead
from zerg.machine_evidence import canonical_evidence_hash
from zerg.machine_evidence import validate_machine_evidence_identities
from zerg.routers.heartbeat import MachineEvidenceIn
from tests_lite.live_catalog_harness import provision_live_catalog

FAMILIES = ("run", "process", "activity", "control", "transcript", "readiness", "continuation")
FACT_COUNT_PER_FAMILY = 32
IDENTITY_FACT_INDEXES = (7, 23)
OBSERVED_AT = "2026-10-06T21:00:00Z"
BOOT_ID = "macos:runtime-test-boot:1"


def _machine_evidence_payload() -> dict[str, Any]:
    """Build realistic v3 typed rows with sparse identities across all families."""
    evidence: dict[str, Any] = {
        "schema_version": 3,
        "observed_at": OBSERVED_AT,
        "process_snapshot_scopes": [
            {
                "scope": "managed_state_files",
                "complete": True,
                "captured_at": OBSERVED_AT,
                "machine_boot_id": BOOT_ID,
                "source": "managed_provider_scan",
                "failure_reason": None,
            }
        ],
        "identities": [],
        **{family: [] for family in FAMILIES},
    }
    identities: list[dict[str, Any]] = evidence["identities"]

    for index in range(FACT_COUNT_PER_FAMILY):
        observed_at = f"2026-10-06T21:00:{index:02d}Z"
        session_id = f"session-{index:04d}"
        run_id = f"run-{index:04d}"
        provider_session_id = f"thread-{index:04d}"
        process_start_time = f"2026-10-06T20:59:{index:02d}Z"
        pid = 10_000 + index
        generation = hashlib.sha256(f"claude:{pid}:{process_start_time}".encode()).hexdigest()

        facts = {
            "run": {
                "authority_class": "exact_process_exit",
                "provider": "claude",
                "session_id": session_id,
                "run_id": run_id,
                "state": "ended",
                "end_reason": "process_gone",
                "process_role": "provider",
                "pid": pid,
                "process_start_time": process_start_time,
                "boot_id": BOOT_ID,
                "source": "claude_channel_scan",
                "observed_at": observed_at,
            },
            "process": {
                "authority_class": "exact_process_identity",
                "provider": "claude",
                "session_id": session_id,
                "provider_session_id": provider_session_id,
                "role": "provider",
                "pid": pid,
                "process_start_time": process_start_time,
                "boot_id": BOOT_ID,
                "cwd": f"/Users/runtime/worktrees/project-{index:04d}",
                "alive": True,
                "source": "claude_channel_scan",
                "observed_at": observed_at,
            },
            "activity": {
                "authority_class": "provider_runtime",
                "provider": "claude",
                "session_id": session_id,
                "run_id": run_id,
                "kind": "thinking",
                "raw_kind": "thinking",
                "tool_name": "Shell",
                "source": "phase_ledger",
                "observed_at": observed_at,
                "valid_until": "2026-10-06T21:30:00Z",
            },
            "control": {
                "authority_class": "provider_control",
                "provider": "claude",
                "session_id": session_id,
                "provider_session_id": provider_session_id,
                "connection_id": f"connection-{index:04d}",
                "lease_generation": f"generation-{index:04d}",
                "run_id": run_id,
                "granted_operations": ["interrupt", "send_input"],
                "ownership": "managed",
                "state": "attached",
                "terminal_attached": True,
                "lease_ttl_ms": 30_000,
                "source": "control_scan",
                "observed_at": observed_at,
            },
            "transcript": {
                "authority_class": "source_cursor",
                "provider": "claude",
                "session_id": session_id,
                "provider_session_id": provider_session_id,
                "source_path": f"/Users/runtime/.longhouse/sessions/{index:04d}.jsonl",
                "source_offset": 128_000 + index,
                "source_inode": 50_000 + index,
                "source_device": 1,
                "source_mtime": observed_at,
                "source": "claude_channel_scan",
                "observed_at": observed_at,
            },
            "readiness": {
                "authority_class": "operation_proof",
                "provider": "antigravity",
                "session_id": session_id,
                "operation": "send_input",
                "hook_installed": True,
                "recent_hook_observed": True,
                "claim_observed": True,
                "response_observed": True,
                "continuation_observed": False,
                "hook_event": "PreInvocation",
                "hook_observed_at": observed_at,
                "claim_message_id": f"claim-{index:04d}",
                "claimed_at": observed_at,
                "response_event": "PreInvocation",
                "response_at": observed_at,
                "response_status": "ok",
                "observed_at": observed_at,
                "valid_until": "2026-10-06T21:02:00Z",
                "source": "antigravity_hook_state",
                "raw_locator": f"/Users/runtime/.longhouse/antigravity/hooks/{index:04d}.json",
                "reason_codes": [],
            },
            "continuation": {
                "authority_class": "retained_launch_contract",
                "provider": "claude",
                "session_id": session_id,
                "provider_session_id": provider_session_id,
                "cwd": f"/Users/runtime/worktrees/project-{index:04d}",
                "contract_state": "valid",
                "unavailable_reason": None,
                "observed_at": observed_at,
                "valid_until": "2026-10-06T21:30:00Z",
                "source": "managed_resume_contract_scan",
                "raw_locator": f"claude/{session_id}/launch-contract.json",
            },
        }
        for family, fact in facts.items():
            evidence[family].append(fact)

        if index not in IDENTITY_FACT_INDEXES:
            continue
        for family, fact in facts.items():
            if family == "run":
                subject_key = f"run:{run_id}"
                source_epoch = run_id
            elif family == "process":
                boot_hash = hashlib.sha256(BOOT_ID.encode()).hexdigest()
                subject_key = f"process:{'0' * 64}:claude:{boot_hash}:{pid}:{generation}"
                source_epoch = generation
            elif family == "activity":
                subject_key = f"run:{run_id}"
                source_epoch = run_id
            elif family == "control":
                lease_generation = fact["lease_generation"]
                subject_key = f"connection:{fact['connection_id']}:{lease_generation}"
                source_epoch = lease_generation
            elif family == "transcript":
                thread_key = hashlib.sha256(provider_session_id.encode()).hexdigest()
                subject_key = f"thread:claude:{thread_key}"
                source_epoch = observed_at
            elif family == "readiness":
                subject_key = f"readiness:{session_id}:send_input"
                source_epoch = observed_at
            else:
                subject_key = f"resume:{session_id}"
                source_epoch = f"launch-{index:04d}"
            identities.append(
                {
                    "fact_family": family,
                    "fact_index": index,
                    "subject_key": subject_key,
                    "source": fact["source"],
                    "source_epoch": source_epoch,
                    "source_seq": None,
                    "sequenced": False,
                    "dedupe_key": hashlib.sha256(f"{family}:{index}".encode()).hexdigest(),
                    "evidence_hash": "",
                }
            )

    wire_evidence = MachineEvidenceIn.model_validate({**evidence, "identities": []}).model_dump(
        mode="json",
        exclude_none=True,
    )
    for identity in identities:
        fact = wire_evidence[identity["fact_family"]][identity["fact_index"]]
        identity["evidence_hash"] = canonical_evidence_hash(fact)
    wire_evidence["identities"] = identities
    return wire_evidence


def compact(evidence: dict[str, Any]) -> dict[str, Any]:
    """Mirror the engine's stable-order row filtering and per-family index remap."""
    compacted = copy.deepcopy(evidence)
    remapped: dict[str, dict[int, int]] = {}
    for family in FAMILIES:
        referenced = {identity["fact_index"] for identity in evidence["identities"] if identity["fact_family"] == family}
        remapped[family] = {}
        rows = []
        for old_index, fact in enumerate(evidence[family]):
            if old_index in referenced:
                remapped[family][old_index] = len(rows)
                rows.append(fact)
        compacted[family] = rows
    for identity in compacted["identities"]:
        identity["fact_index"] = remapped[identity["fact_family"]][identity["fact_index"]]
    return compacted


def _wire_sizes(evidence: dict[str, Any]) -> tuple[int, int]:
    serialized = json.dumps(evidence, separators=(",", ":")).encode("utf-8")
    return len(serialized), len(gzip.compress(serialized, compresslevel=5, mtime=0))


_FACT_HEAD_FIELDS = (
    "family",
    "subject_key",
    "source",
    "source_epoch",
    "session_id",
    "ordering_mode",
    "source_seq",
    "evidence_hash",
    "observed_at",
    "valid_until",
    "value_json",
    "raw_locator",
    "updated_commit_seq",
)


def _fact_heads() -> list[dict[str, Any]]:
    rows = _catalog_rows(FactHead.__table__)
    return sorted(
        ({field: row[field] for field in _FACT_HEAD_FIELDS} for row in rows),
        key=lambda row: (row["family"], row["subject_key"], row["source"], row["source_epoch"]),
    )


def _fact_head_keys(rows: list[dict[str, Any]]) -> set[tuple[str, str, str, str, str]]:
    return {(row["family"], row["subject_key"], row["source"], row["source_epoch"], row["evidence_hash"]) for row in rows}


def test_compacted_machine_evidence_preserves_facts_and_heartbeat_heads(live_catalog, live_catalog_client):
    full = _machine_evidence_payload()
    compacted = compact(full)

    assert all(len(full[family]) == FACT_COUNT_PER_FAMILY for family in FAMILIES)
    assert all(len(full[family]) > sum(identity["fact_family"] == family for identity in full["identities"]) for family in FAMILIES)
    assert all(identity["fact_index"] in (0, 1) for identity in compacted["identities"])
    assert len(validate_machine_evidence_identities(full)) == len(full["identities"])
    assert len(validate_machine_evidence_identities(compacted)) == len(compacted["identities"])
    full_facts = reducer_facts_from_machine_evidence(full)
    compacted_facts = reducer_facts_from_machine_evidence(compacted)
    assert full_facts == compacted_facts
    MachineEvidenceIn.model_validate(full)
    MachineEvidenceIn.model_validate(compacted)

    full_raw_size, full_gzip_size = _wire_sizes(full)
    compact_raw_size, compact_gzip_size = _wire_sizes(compacted)
    assert full_raw_size > compact_raw_size
    assert full_gzip_size > compact_gzip_size

    expected_head_keys = {(fact.family, fact.subject_key, fact.source, fact.source_epoch, fact.evidence_hash) for fact in full_facts}
    headers = _headers(live_catalog, "evidence-compaction-machine")
    full_response = live_catalog_client.post(
        "/agents/heartbeat",
        headers=headers,
        json={"version": "evidence-compaction-full", "daemon_pid": 42, "machine_evidence": full},
    )
    assert full_response.status_code == 204, full_response.text
    full_disposition = full_response.headers["x-longhouse-machine-evidence"]
    assert full_disposition == "applied"
    full_heads = _fact_heads()
    assert len(full_heads) == len(expected_head_keys)
    assert _fact_head_keys(full_heads) == expected_head_keys

    with provision_live_catalog() as compact_catalog:
        compact_headers = _headers(compact_catalog, "evidence-compaction-machine")
        with compact_catalog.http_client() as compact_client:
            compact_response = compact_client.post(
                "/agents/heartbeat",
                headers=compact_headers,
                json={"version": "evidence-compaction-compact", "daemon_pid": 42, "machine_evidence": compacted},
            )
        assert compact_response.status_code == 204, compact_response.text
        assert compact_response.headers["x-longhouse-machine-evidence"] == full_disposition
        compact_heads = _fact_heads()
        assert len(compact_heads) == len(expected_head_keys)
        assert _fact_head_keys(compact_heads) == expected_head_keys
        assert compact_heads == full_heads
