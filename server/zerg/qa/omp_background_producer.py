#!/usr/bin/env python3
"""Live OMP async-job/background evidence through the existing Helm channel."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from zerg.qa import omp_helm_lifecycle as helm
from zerg.qa import provider_release_identity as identity
from zerg.qa import provider_semantic_qualification as semantic
from zerg.qa.provider_factory_invocation import add_factory_provider_arguments
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key

SCENARIO_ID = "omp_background_jobs"
PROFILE = "omp_background_v1"
ASSERTIONS = (
    "omp_background_registry_owner_scoped",
    "omp_background_partial_progress_child_scoped",
    "omp_background_terminal_status_preserved",
)
FAULT = "omp_background_writer_disabled"
NEGATIVE_CONTROLS = {FAULT: ("omp_background_registry_owner_scoped",)}
_VARIANTS = tuple(execution_variant_key(provider="omp", assertion_id=item, scenario_id=SCENARIO_ID, variant=None) for item in ASSERTIONS)


def _requested_assertion_id(variant: object) -> str | None:
    if variant is None:
        return None
    for assertion in ASSERTIONS:
        if (
            execution_variant_key(
                provider="omp",
                assertion_id=assertion,
                scenario_id=SCENARIO_ID,
                variant=None,
            )
            == variant
        ):
            return assertion
    raise ValueError(f"unrecognized OMP background execution variant: {variant!r}")


REGISTRATION = ProducerRegistration(
    producer_id="omp.background_jobs.v1",
    producer_revision=1,
    scenario_id=SCENARIO_ID,
    scenario_revision=1,
    assertion_cells=tuple((item, None) for item in ASSERTIONS),
    providers=("omp",),
    platforms=("linux", "darwin"),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "omp_async_job_manager_snapshot",
        "omp_task_progress_source",
        "omp_background_owner_fence",
        "omp_background_terminal_status",
        "omp_native_archive_bound",
        "omp_transcript_shipper_started",
        "omp_owned_processes_dead",
    ),
    acquisition_methods=("staged_release",),
    credential_binding_ids=("omp_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "omp_helm_receipt",
        "omp_background_receipt",
        "transcript_flush_receipt",
        "transcript_shipper_receipt",
        "provider_source_retention",
        "cleanup_receipt",
    ),
    required_cleanup=(
        "provider_process_dead",
        "process_group_dead",
        "no_orphan_provider_processes",
        "canary_session_hidden",
        "isolation_removed",
    ),
    implementation="server/zerg/qa/omp_background_producer.py",
    oracle_source="server/zerg/qa/omp_background_producer.py",
    oracle_entrypoint="omp_background_assertions",
    executable_module="zerg.qa.omp_background_producer",
    required_executables=("longhouse", "longhouse-engine"),
    provider_artifact_required=True,
    observation_scope="scenario",
)


def _jsonl_paths(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in {".jsonl", ".ndjson"}))


def _rows_from_path(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _walk_jsonl(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in _jsonl_paths(root):
        rows.extend(_rows_from_path(path))
    return rows


def _retain_native_extension_artifacts(root: Path) -> list[dict[str, Any]]:
    raw_root = root / "raw"
    raw_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    retained: list[dict[str, Any]] = []
    index = 0
    for source in _jsonl_paths(root):
        if raw_root in source.parents:
            continue
        rows = _rows_from_path(source)
        if not any(
            isinstance(row.get("event"), Mapping) and ("async_jobs" in row["event"] or "task_progress" in row["event"]) for row in rows
        ):
            continue
        try:
            source_bytes = source.read_bytes()
        except OSError:
            continue
        destination = raw_root / f"omp-extension-{index}{source.suffix.lower()}"
        index += 1
        try:
            shutil.copyfile(source, destination)
        except OSError:
            continue
        retained.append(
            {
                "source_path": str(source),
                "retained_path": str(destination),
                "sha256": hashlib.sha256(source_bytes).hexdigest(),
                "bytes": len(source_bytes),
                "row_count": len(rows),
            }
        )
    (root / "omp-background-raw-artifacts.json").write_text(
        json.dumps({"schema_version": 1, "artifacts": retained}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return retained


def _managed_omp_identity(root: Path, result: Mapping[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(result, Mapping) or result.get("provider") != "omp":
        return None
    receipt_path = root / "omp-background-served-receipt.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(receipt, Mapping) or receipt.get("managed_transport") != "omp_helm_channel":
        return None
    control = receipt.get("control_identity")
    state = receipt.get("state")
    if not helm._control_identity_receipt_is_bound(control):
        return None
    if not isinstance(state, Mapping) or state.get("provider") != "omp":
        return None
    if control.get("session_id") != state.get("session_id") or control["owner_identity"][:3] != [
        state.get("session_id"),
        state.get("native_session_id"),
        state.get("session_file"),
    ]:
        return None
    fields = (
        "machine_name",
        "session_id",
        "run_id",
        "native_session_id",
        "session_file",
        "connection_id",
        "lease_generation",
    )
    if any(not isinstance(state.get(field), str) or not state[field] for field in fields):
        return None
    return {field: str(state[field]) for field in fields}


def _served_terminal_pairs(root: Path, identity: Mapping[str, str] | None) -> set[tuple[str, str]]:
    if identity is None:
        return set()
    try:
        receipt = json.loads((root / "omp-background-served-receipt.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return set()
    detail = receipt.get("detail") or {}
    if detail.get("id") != identity["session_id"]:
        return set()
    delegation = (detail.get("session_state") or {}).get("delegation") or {}
    recent = delegation.get("recent_items") or []
    return {
        (str(row["id"]), str(row["status"]))
        for row in recent
        if isinstance(row, Mapping)
        and isinstance(row.get("id"), str)
        and row.get("status") in {"completed", "failed", "cancelled", "aborted"}
    }


def omp_background_assertions(root: Path, result: Mapping[str, Any] | None = None) -> dict[str, bool]:
    """Judge retained native frames against the actual managed OMP Helm receipt."""

    frames = _walk_jsonl(root)
    identity = _managed_omp_identity(root, result)

    def bound(frame: Mapping[str, Any]) -> bool:
        if identity is None:
            return False
        for field in ("session_id", "native_session_id", "session_file", "connection_id", "lease_generation"):
            if frame.get(field) != identity[field]:
                return False
        frame_machine = frame.get("machine_name")
        if frame_machine is not None and frame_machine != identity["machine_name"]:
            return False
        frame_run_id = frame.get("run_id")
        return frame_run_id is None or frame_run_id == identity["run_id"]

    async_frames = [
        frame
        for frame in frames
        if isinstance(frame.get("event"), Mapping)
        and ("async_jobs" in frame["event"] or "task_progress" in frame["event"])
        and bound(frame)
    ]
    complete = [
        frame
        for frame in async_frames
        if frame["event"].get("async_running_complete") is True
        and isinstance(frame["event"].get("async_jobs"), list)
        and frame["event"]["async_jobs"]
    ]
    progress = [
        frame for frame in async_frames if isinstance(frame["event"].get("task_progress"), list) and frame["event"]["task_progress"]
    ]
    running_ids = {
        str(row["id"])
        for frame in async_frames
        for row in frame["event"].get("async_jobs", [])
        if isinstance(row, Mapping) and isinstance(row.get("id"), str) and row["id"] and row.get("status") == "running"
    }
    owner_scoped = any(
        isinstance(row, Mapping) and row.get("status") == "running" for frame in complete for row in frame["event"]["async_jobs"]
    )
    progress_rows = [row for frame in progress for row in frame["event"]["task_progress"] if isinstance(row, Mapping)]
    progress_scoped = bool(progress_rows) and all(
        isinstance(row.get("agent_id"), str) and bool(row["agent_id"]) and (not row.get("job_id") or row["job_id"] in running_ids)
        for row in progress_rows
    )
    terminal_pairs = {
        (str(row["id"]), str(row["status"]))
        for frame in async_frames
        for row in frame["event"].get("async_jobs", [])
        if isinstance(row, Mapping)
        and isinstance(row.get("id"), str)
        and row["id"] in running_ids
        and row.get("status") in {"completed", "failed", "cancelled", "aborted"}
    }
    terminal_pairs.update(
        (str(row["job_id"]), str(row["status"]))
        for frame in async_frames
        for row in frame["event"].get("task_progress", [])
        if isinstance(row, Mapping)
        and isinstance(row.get("job_id"), str)
        and row["job_id"] in running_ids
        and row.get("status") in {"completed", "failed", "cancelled", "aborted"}
    )
    terminal = bool(terminal_pairs & _served_terminal_pairs(root, identity))
    return {
        "omp_background_registry_owner_scoped": owner_scoped,
        "omp_background_partial_progress_child_scoped": progress_scoped,
        "omp_background_terminal_status_preserved": terminal,
    }


def run_omp_background(args: argparse.Namespace) -> dict[str, Any]:
    # Reuse the existing Helm PTY, extension channel, shipper and retirement
    # mechanics. The prompt asks the real OMP task facility for one bounded job;
    # absent native async evidence remains a semantic failure, never green.
    args.background_prompt = (
        "Use the native background task/job facility exactly once. Start one bounded "
        "task that reports progress, then wait for its terminal completed or failed "
        "status. Reply with the requested marker only after the job terminal event."
    )
    requested_assertion = _requested_assertion_id(getattr(args, "variant", None))
    fault = getattr(args, "negative_control", None)
    if fault:
        os.environ["LONGHOUSE_QA_FAULT"] = fault
        os.environ["LONGHOUSE_QA_FAULT_RECEIPT"] = str(args.evidence_root.resolve() / "qa-fault-receipt.jsonl")
        args.negative_control = None
    result = helm.run_omp_helm(args)
    args.negative_control = fault
    root = args.evidence_root.resolve()
    raw_artifacts = _retain_native_extension_artifacts(root)
    managed_identity = _managed_omp_identity(root, result)
    assertions = omp_background_assertions(root, result)
    if not raw_artifacts:
        assertions = {name: False for name in assertions}
    receipt = {
        "schema_version": 1,
        "scenario": SCENARIO_ID,
        "producer": REGISTRATION.to_dict(),
        "provider": "omp",
        "requested_assertion": requested_assertion,
        "assertions": assertions,
        "managed_identity": managed_identity,
        "raw_artifacts": raw_artifacts,
        "source": "retained OMP Helm extension JSONL/NDJSON frames",
        "fault": getattr(args, "negative_control", None),
    }
    (root / "omp-background-receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result = dict(result)
    result.update(
        {
            "scenario_id": SCENARIO_ID,
            "producer": REGISTRATION.to_dict(),
            "requested_assertion": requested_assertion,
            "assertions": assertions,
            "managed_identity": managed_identity,
            "raw_artifacts": raw_artifacts,
        }
    )
    fault = getattr(args, "negative_control", None)
    if fault:
        (target,) = NEGATIVE_CONTROLS[fault]
        receipt_path = root / "qa-fault-receipt.jsonl"
        fault_receipts = []
        if receipt_path.is_file():
            for line in receipt_path.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict) and value.get("fault") == fault:
                    fault_receipts.append(value)
        target_rejected = not assertions.get(target, False)
        healthy_control_assertions = all(value for name, value in assertions.items() if name != target)
        result["negative_control"] = {
            "fault": fault,
            "target_assertion": target,
            "fault_fired": bool(fault_receipts),
            "fault_receipts": fault_receipts,
            "target_rejected": target_rejected,
            "observed_code": ("background_registry_missing" if target_rejected else "background_registry_present"),
            "expected_code": "background_registry_missing",
            "healthy_preconditions_failed": [],
            "managed_identity": managed_identity,
            "status": (
                "pass"
                if (fault_receipts and managed_identity and raw_artifacts and target_rejected and healthy_control_assertions)
                else "inconclusive"
            ),
        }
        result["status"] = "pass" if all(assertions.values()) else "fail"
    else:
        result["status"] = "pass" if all(assertions.values()) else "fail"
    return result


def _request_args(
    request_path: Path, output_root: Path, *, request: Mapping[str, Any] | None = None, agents_token: str | None = None
) -> argparse.Namespace:
    request = dict(request) if request is not None else json.loads(request_path.read_text(encoding="utf-8"))
    return argparse.Namespace(
        evidence_root=output_root,
        repo_root=Path(__file__).resolve().parents[3],
        provider_bin=Path(str(request["provider_bin"])),
        provider_version=str(request["expected_provider_version"]),
        engine=Path(os.environ.get("LONGHOUSE_ENGINE_BIN") or ""),
        longhouse_cli=Path(os.environ.get("LONGHOUSE_CLI_BIN") or "longhouse"),
        api_url=os.environ.get("LONGHOUSE_RUNTIME_API_URL"),
        agents_token=agents_token or os.environ.get("LONGHOUSE_RUNTIME_AGENTS_TOKEN"),
        model=os.environ.get("LONGHOUSE_OMP_QUALIFICATION_MODEL", ""),
        variant=None,
        negative_control=None,
    )


def run(request_path: Path, output_root: Path) -> dict[str, object]:
    request = identity.load_request(
        request_path, provider="omp", profile=PROFILE, version_grammar=identity.semver_version_line(version_prefix=r"omp/")
    )

    def execute(binary: Path, evidence_root: Path):
        args = _request_args(request_path, evidence_root, request=request)
        args.provider_bin = binary
        result = run_omp_background(args)
        assertions = result["assertions"]
        return (
            result,
            tuple(
                semantic.SemanticAssertion(
                    name,
                    semantic.AssertionOutcome.PASS if value else semantic.AssertionOutcome.SEMANTIC_FAIL,
                    semantic.EvidenceClass.LIVE_TOKEN,
                )
                for name, value in assertions.items()
            ),
            (),
        )

    return semantic.run_semantic_profile(
        request_path,
        output_root,
        profile=identity.IdentityProfile(
            provider="omp",
            profile=PROFILE,
            scenario_id=SCENARIO_ID,
            version_line=identity.semver_version_line(version_prefix=r"omp/"),
            oracle_source=Path(__file__),
        ),
        assertion_ids=ASSERTIONS,
        executor=execute,
        oracle_source=Path(__file__),
        scenario_revision=1,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_factory_provider_arguments(parser, variants=_VARIANTS)
    parser.add_argument("--negative-control", choices=(FAULT,))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    if args.negative_control:
        os.environ["LONGHOUSE_QA_FAULT"] = FAULT
    result = run_omp_background(args)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
