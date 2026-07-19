#!/usr/bin/env python3
"""Validate the Phase 7 fault inventory and its deterministic proof links."""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "schemas" / "session_state_fault_matrix.yml"
REQUIRED_FAULTS = {
    "network_loss_retry",
    "runtime_host_restart",
    "machine_agent_restart",
    "terminal_close",
    "tui_gone_app_server_survives",
    "bridge_death_execution_survives",
    "process_exit",
    "stale_pid_or_state_file",
    "transcript_lag",
    "mixed_current_and_residue",
    "cross_surface_contract_drift",
    "expired_control_revalidation",
}


def _test_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
    }


def validate(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return ["matrix must be a YAML object"]
    errors: list[str] = []
    if payload.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list):
        return [*errors, "scenarios must be a list"]
    ids: set[str] = set()
    faults: set[str] = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            errors.append("every scenario must be an object")
            continue
        scenario_id = str(scenario.get("id") or "")
        fault = str(scenario.get("fault") or "")
        if not scenario_id or scenario_id in ids:
            errors.append(f"duplicate or missing scenario id: {scenario_id!r}")
        ids.add(scenario_id)
        faults.add(fault)
        if scenario.get("live") not in {"required", "optional"}:
            errors.append(f"{scenario_id}: live must be required or optional")
        expected = scenario.get("expect")
        if not isinstance(expected, list) or not expected:
            errors.append(f"{scenario_id}: expect must be a non-empty list")
        deterministic = scenario.get("deterministic")
        if not isinstance(deterministic, list) or not deterministic:
            errors.append(f"{scenario_id}: deterministic proofs are required")
            continue
        for node_id in deterministic:
            if not isinstance(node_id, str) or "::" not in node_id:
                errors.append(f"{scenario_id}: invalid test node id {node_id!r}")
                continue
            relative, function = node_id.split("::", 1)
            path = ROOT / relative
            if not path.is_file():
                errors.append(f"{scenario_id}: missing test file {relative}")
                continue
            if function not in _test_functions(path):
                errors.append(f"{scenario_id}: missing test function {node_id}")
    missing = REQUIRED_FAULTS - faults
    if missing:
        errors.append(f"missing required faults: {', '.join(sorted(missing))}")
    gate = payload.get("deletion_gate")
    if not isinstance(gate, dict):
        errors.append("deletion_gate must be an object")
    else:
        live_ids = gate.get("exact_build_live_scenarios")
        if not isinstance(live_ids, list) or not live_ids:
            errors.append("deletion_gate exact_build_live_scenarios must be non-empty")
        elif unknown := set(live_ids) - ids:
            errors.append(f"deletion gate references unknown scenarios: {', '.join(sorted(unknown))}")
        if set(gate.get("required_providers") or []) != {"codex", "claude", "opencode", "cursor", "antigravity"}:
            errors.append("deletion gate must name all five managed providers")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--nodeids", action="store_true", help="Print deterministic pytest node ids.")
    parser.add_argument("--server-relative", action="store_true", help="Strip the server/ prefix from node ids.")
    args = parser.parse_args()
    if not args.check and not args.nodeids:
        parser.error("use --check or --nodeids")
    payload = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
    errors = validate(payload)
    if errors:
        print("session-state fault matrix invalid:", *[f"- {error}" for error in errors], sep="\n", file=sys.stderr)
        return 1
    if args.nodeids:
        node_ids = sorted(
            {
                node_id.removeprefix("server/") if args.server_relative else node_id
                for scenario in payload["scenarios"]
                for node_id in scenario["deterministic"]
            }
        )
        print(" ".join(node_ids))
    else:
        print("session-state fault matrix OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
