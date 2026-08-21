"""Live Runtime Host assurance for Console workspace suggestions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from zerg.qa.resume_assurance import ProducerRegistration

ASSERTION_ID = "responsive_human_only_projection"
RUNTIME_API_URL_ENV = "LONGHOUSE_RUNTIME_API_URL"
RUNTIME_AGENTS_TOKEN_ENV = "LONGHOUSE_RUNTIME_AGENTS_TOKEN"
MAX_LIVE_LATENCY_SECONDS = 3.0
MAX_MACHINES = 3
_DISALLOWED_PATH_MARKERS = (
    "/canaries/provider-live/",
    "longhouse-provider-live-proof",
    "/provider-factory/artifacts/",
    "/tmp/provider-factory-",
    "/private/tmp/provider-factory-",
    "/tmp/live-cell-run-",
    "/private/tmp/live-cell-run-",
)

REGISTRATION = ProducerRegistration(
    producer_id="longhouse.workspace_suggestions_live.v1",
    producer_revision=2,
    scenario_id="workspace_suggestions_live",
    scenario_revision=1,
    assertion_cells=((ASSERTION_ID, None),),
    providers=(),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("runtime_host",),
    evidence_classes=("live_token",),
    observed_activity=(
        "runtime_host_machine_directory",
        "runtime_host_workspace_projection",
        "workspace_provenance_filter",
    ),
    acquisition_methods=(),
    credential_binding_ids=("runtime_host_control",),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=("live_runtime_observation", "runtime_request_receipt", "cleanup_receipt"),
    required_cleanup=("no_owned_processes",),
    implementation="server/zerg/qa/workspace_suggestions_live_producer.py",
    oracle_source="server/zerg/qa/workspace_suggestions_live_producer.py",
    oracle_entrypoint="run_live_workspace_suggestions_oracle",
    executable_module="zerg.qa.workspace_suggestions_live_producer",
    provider_artifact_required=False,
    subject_kind="longhouse_product",
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _artifact_manifest(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "result.json"
    ]


def _headers(token: str) -> dict[str, str]:
    return {"X-Agents-Token": token}


def _get_json(url: str, *, token: str) -> tuple[int, float, object]:
    started = time.monotonic()
    response = httpx.get(url, headers=_headers(token), timeout=10)
    latency = time.monotonic() - started
    payload: object
    try:
        payload = response.json()
    except ValueError:
        payload = {"body": response.text[:1_000]}
    return response.status_code, latency, payload


def _workspace_paths(payload: object) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("workspaces"), list):
        return []
    return [str(row.get("path") or "") for row in payload["workspaces"] if isinstance(row, dict)]


def run_live_workspace_suggestions_oracle(*, evidence_root: Path) -> dict[str, Any]:
    evidence_root.mkdir(parents=True, exist_ok=False)
    api_url = str(os.environ.get(RUNTIME_API_URL_ENV) or "").strip().rstrip("/")
    token = str(os.environ.get(RUNTIME_AGENTS_TOKEN_ENV) or "").strip()
    if not api_url or not token:
        raise ValueError("live workspace assurance requires Runtime Host API URL and token")

    machines_status, machines_latency, machines_payload = _get_json(
        f"{api_url}/api/agents/machines",
        token=token,
    )
    machines = machines_payload.get("machines") if isinstance(machines_payload, dict) else None
    if machines_status != 200 or not isinstance(machines, list):
        raise RuntimeError(f"machine directory failed status={machines_status}")
    connected = [
        row
        for row in machines
        if isinstance(row, dict) and row.get("control_channel_status") == "connected" and isinstance(row.get("device_id"), str)
    ]
    if not connected:
        raise RuntimeError("machine directory has no connected machine for workspace assurance")

    reads: list[dict[str, Any]] = []
    for machine in connected[:MAX_MACHINES]:
        device_id = str(machine["device_id"])
        status, latency, payload = _get_json(
            f"{api_url}/api/agents/machines/{quote(device_id, safe='')}/workspaces?limit=50&days_back=180",
            token=token,
        )
        paths = _workspace_paths(payload)
        reads.append(
            {
                "device_id": device_id,
                "status_code": status,
                "latency_seconds": round(latency, 6),
                "paths": paths,
                "response_shape_valid": isinstance(payload, dict) and isinstance(payload.get("workspaces"), list),
            }
        )

    all_paths = [path for read in reads for path in read["paths"]]
    leaking_paths = [path for path in all_paths if any(marker in path.replace("\\", "/").lower() for marker in _DISALLOWED_PATH_MARKERS)]
    observation = {
        "connected_machine_count": len(connected),
        "checked_machine_count": len(reads),
        "machine_directory_status": machines_status,
        "machine_directory_latency_seconds": round(machines_latency, 6),
        "all_reads_succeeded": all(read["status_code"] == 200 for read in reads),
        "all_reads_within_budget": all(read["latency_seconds"] <= MAX_LIVE_LATENCY_SECONDS for read in reads),
        "all_response_shapes_valid": all(read["response_shape_valid"] for read in reads),
        "at_least_one_human_workspace": bool(all_paths),
        "all_paths_absolute": all(path.startswith("/") for path in all_paths),
        "all_paths_unique_per_machine": all(len(read["paths"]) == len(set(read["paths"])) for read in reads),
        "proof_path_leak_count": len(leaking_paths),
        "leaking_paths": leaking_paths,
        "reads": reads,
    }
    passed = (
        observation["all_reads_succeeded"]
        and observation["all_reads_within_budget"]
        and observation["all_response_shapes_valid"]
        and observation["at_least_one_human_workspace"]
        and observation["all_paths_absolute"]
        and observation["all_paths_unique_per_machine"]
        and observation["proof_path_leak_count"] == 0
    )
    _write_json(evidence_root / "live-runtime-observation.json", observation)
    _write_json(
        evidence_root / "runtime-request-receipt.json",
        {
            "runtime_host_paths": [
                "/api/agents/machines",
                "/api/agents/machines/{device_id}/workspaces",
            ],
            "checked_device_ids": [read["device_id"] for read in reads],
            "direct_provider_paths": [],
            "credential_mutations": [],
        },
    )
    _write_json(
        evidence_root / "cleanup-receipt.json",
        {"status": "pass", "orphan_count": 0, "owned_process_count": 0},
    )
    return {"passed": bool(passed), "observation": observation}


def run(evidence_root: Path) -> dict[str, Any]:
    try:
        oracle = run_live_workspace_suggestions_oracle(evidence_root=evidence_root)
        result = {
            "schema_version": 1,
            "artifact_kind": "longhouse_workspace_suggestions_live_result",
            "producer": REGISTRATION.to_dict(),
            "provider": None,
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "status": "pass" if oracle["passed"] else "fail",
            "observation": oracle["observation"],
            "assertions": {ASSERTION_ID: bool(oracle["passed"])},
        }
    except Exception as exc:  # noqa: BLE001 - typed failure evidence is the producer contract
        evidence_root.mkdir(parents=True, exist_ok=True)
        if not (evidence_root / "cleanup-receipt.json").exists():
            _write_json(
                evidence_root / "cleanup-receipt.json",
                {"status": "fail", "orphan_count": 0, "owned_process_count": 0, "error_type": type(exc).__name__},
            )
        result = {
            "schema_version": 1,
            "artifact_kind": "longhouse_workspace_suggestions_live_result",
            "producer": REGISTRATION.to_dict(),
            "provider": None,
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "status": "fail",
            "failure_code": "workspace_suggestions_live_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "observation": {},
            "assertions": {ASSERTION_ID: False},
        }
    result["artifact_manifest"] = _artifact_manifest(evidence_root)
    _write_json(evidence_root / "result.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path)
    args = parser.parse_args(arguments)
    result = run(args.evidence_root)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
