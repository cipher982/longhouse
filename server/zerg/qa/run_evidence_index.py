"""The versioned run-evidence index.

Phase 2 step 1 of docs/specs/provider-factory-coherence.md ("The run-evidence
index"): the format a converged harness-as-orchestrator run will write, and a
pure qualification oracle will read, once the orchestrator actually produces
one (Phase 2 steps 2-4 — not yet wired to any live code path). Defined now,
ahead of that wiring, because the oracle-extraction work in this phase needs
a concrete target shape to extract *toward*; without it "pure oracle" has no
fixed input type to be pure over.

Fields, per the spec: the serialized plan; an intervention log with runtime
bindings (an intervention cannot be named in a static plan — the turn id it
bound to is only known once the executor actually sends it); raw artifacts
with checksums; build provenance; the sandbox receipt; and the deployed
Longhouse/control-plane SHA pair.
"""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class InterventionLogEntry:
    """A perturbation the orchestrator performed mid-run and what it bound to.

    Covers the two deployed assertion families the spec calls out as
    timing-coupled rather than passive observation: codex_helm_interrupt
    (action="interrupt", bound_turn_id=the turn it targeted) and
    process_restart_reattach_preserved (action="restart_server",
    bound_turn_id=None).
    """

    action: str
    monotonic_timestamp: float
    bound_turn_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RawArtifact:
    path: str
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunEvidenceIndex:
    schema_version: int
    plan_cell: dict[str, Any]
    intervention_log: tuple[InterventionLogEntry, ...]
    raw_artifacts: tuple[RawArtifact, ...]
    build_provenance: str
    sandbox_receipt: dict[str, Any] | None
    longhouse_git_sha: str
    control_plane_git_sha: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported run-evidence index schema_version: {self.schema_version}")
        if not self.longhouse_git_sha:
            raise ValueError("run-evidence index requires longhouse_git_sha")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_cell": self.plan_cell,
            "intervention_log": [entry.to_dict() for entry in self.intervention_log],
            "raw_artifacts": [artifact.to_dict() for artifact in self.raw_artifacts],
            "build_provenance": self.build_provenance,
            "sandbox_receipt": self.sandbox_receipt,
            "longhouse_git_sha": self.longhouse_git_sha,
            "control_plane_git_sha": self.control_plane_git_sha,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RunEvidenceIndex:
        return cls(
            schema_version=payload["schema_version"],
            plan_cell=dict(payload["plan_cell"]),
            intervention_log=tuple(InterventionLogEntry(**entry) for entry in payload.get("intervention_log") or ()),
            raw_artifacts=tuple(RawArtifact(**artifact) for artifact in payload.get("raw_artifacts") or ()),
            build_provenance=payload["build_provenance"],
            sandbox_receipt=payload.get("sandbox_receipt"),
            longhouse_git_sha=payload["longhouse_git_sha"],
            control_plane_git_sha=payload.get("control_plane_git_sha"),
        )
