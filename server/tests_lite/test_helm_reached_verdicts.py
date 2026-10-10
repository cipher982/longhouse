"""Errored Codex and OpenCode Helm lifecycles report verdicts only for steps they reached.

The factory files a false assertion as a product finding and a missing one as
"no verdict for assertion" (harness). An unreached step is absent, never False and
never True; a reached failing step stays a finding; and a run whose reached steps
all held is a typed harness failure with its evidence kept as partial_observation.
OMP has the same rule in test_omp_helm_reached_verdicts.py.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from zerg.qa import claude_helm_lifecycle as claude
from zerg.qa import codex_helm_lifecycle as codex
from zerg.qa import factory_registration
from zerg.qa import opencode_helm_lifecycle as opencode
from zerg.qa.failed_results import reached_only
from zerg.qa.failed_results import settle_failed_result

SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "managed_providers.yml"

# Per producer: an observation where send was reached (and failed or held), with
# the full map the producer computes, which reads unreached steps as False.
CASES: dict[str, dict[str, Any]] = {
    "claude": {
        "module": claude,
        "send_reached": {
            "lifecycle": {"launch_registration": {"passed": True}, "send_idle": {"passed": False}},
            "error": "late failure",
        },
        "send_failed_map": {
            "claude_helm_launch_registration": True,
            "claude_helm_send_idle": False,
            "claude_helm_steer_active": False,
            "claude_helm_abort_native": False,
            "claude_helm_terminate_owned": False,
        },
        "send_held_map": {
            "claude_helm_launch_registration": True,
            "claude_helm_send_idle": True,
            "claude_helm_steer_active": False,
            "claude_helm_abort_native": False,
            "claude_helm_terminate_owned": False,
        },
        "reached": {"claude_helm_launch_registration", "claude_helm_send_idle"},
        # Claude's phase records are lifecycle["<phase>"] = {..., "passed": ...}.
        "written_as": lambda marker: marker.removeprefix("lifecycle.").removesuffix(".passed"),
        "written_names": {"lifecycle"},
    },
    "codex": {
        "module": codex,
        "send_reached": {"send": {"turn_id": "t1"}, "terminate": {"stop_verification": {"verified": True}}},
        "send_failed_map": {
            codex.SEND_IDLE: False,
            codex.STEER_ACTIVE: False,
            codex.ABORT_NATIVE: False,
            codex.TERMINATE_OWNED: True,
        },
        "send_held_map": {
            codex.SEND_IDLE: True,
            codex.STEER_ACTIVE: False,
            codex.ABORT_NATIVE: False,
            codex.TERMINATE_OWNED: True,
        },
        "reached": {codex.SEND_IDLE, codex.TERMINATE_OWNED},
    },
    "opencode": {
        "module": opencode,
        "send_reached": {
            "provider": "opencode",
            "launch": {"session_id": "s", "runtime_input_accepted": True},
            "send": {"dispatch_accepted": True},
        },
        "send_failed_map": {
            "opencode_helm_launch_registration": True,
            "opencode_helm_send_idle": False,
            "opencode_helm_steer_active": False,
            "opencode_helm_abort_native": False,
            "opencode_helm_terminate_owned": False,
        },
        "send_held_map": {
            "opencode_helm_launch_registration": True,
            "opencode_helm_send_idle": True,
            "opencode_helm_steer_active": False,
            "opencode_helm_abort_native": False,
            "opencode_helm_terminate_owned": False,
        },
        "reached": {"opencode_helm_launch_registration", "opencode_helm_send_idle"},
    },
}


def _errored_result(module: ModuleType, observation: dict, full_map: dict[str, bool]) -> dict:
    """The failed result the producer's error path builds, settled the way it settles it."""

    registration = module.REGISTRATION.to_dict()
    result = {
        "schema_version": 1,
        "producer": registration,
        "provider": registration["providers"][0],
        "variant": None,
        "scenario_id": module.REGISTRATION.scenario_id,
        "scenario_revision": module.REGISTRATION.scenario_revision,
        "evidence_class": "live_token",
        "observation_scope": "scenario",
        "generated_at": "2026-10-10T00:00:00Z",
        "status": "fail",
        "failure_code": f"{registration['providers'][0]}_helm_lifecycle_failed",
        "error": "RuntimeError: late failure",
        "artifact_manifest": [],
    }
    return settle_failed_result(result, observation=observation, assertions=reached_only(full_map, observation, module.REACHED_MARKERS))


def _envelope_by_cell(module: ModuleType, result: dict) -> dict[str, list[str]]:
    commands = factory_registration.factory_commands(module.REGISTRATION.to_dict(), factory_registration.contract_rows(SCHEMA))
    assert {command["assertion_id"] for command in commands} == set(module.ASSERTIONS)
    return {command["assertion_id"]: factory_registration.result_envelope_failures(command, result) for command in commands}


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_reached_failing_step_is_a_finding_and_unreached_steps_have_no_verdict(name: str) -> None:
    case = CASES[name]
    result = _errored_result(case["module"], case["send_reached"], case["send_failed_map"])

    assert set(result["assertions"]) == case["reached"]
    assert False in result["assertions"].values()
    for assertion_id, failures in _envelope_by_cell(case["module"], result).items():
        if assertion_id in case["reached"]:
            assert failures == [], assertion_id
        else:
            assert failures == [f"no verdict for assertion {assertion_id!r}"], assertion_id


@pytest.mark.parametrize("name", sorted(CASES))
def test_an_error_after_only_passing_steps_is_a_typed_harness_failure(name: str) -> None:
    case = CASES[name]
    result = _errored_result(case["module"], case["send_reached"], case["send_held_map"])

    assert "observation" not in result and "assertions" not in result
    assert result["partial_observation"] == case["send_reached"]
    assert factory_registration.typed_harness_failure(result)
    assert all(failures == [] for failures in _envelope_by_cell(case["module"], result).values())


@pytest.mark.parametrize("name", sorted(CASES))
def test_an_error_before_any_step_is_a_typed_harness_failure(name: str) -> None:
    case = CASES[name]
    result = _errored_result(case["module"], {}, case["send_failed_map"])

    assert "observation" not in result and "assertions" not in result
    assert factory_registration.typed_harness_failure(result)
    assert all(failures == [] for failures in _envelope_by_cell(case["module"], result).values())


def _written_paths(tree: ast.AST, names: set[str]) -> set[str]:
    """Dotted paths a producer assigns on its observation dict (`observation["a"]["b"] = ...`)."""

    written: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            parts: list[str] = []
            while isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant):
                parts.insert(0, target.slice.value)
                target = target.value
            if parts and isinstance(target, ast.Name) and target.id in names:
                written.add(".".join(parts))
    return written


@pytest.mark.parametrize("name", sorted(CASES))
def test_reached_markers_cover_every_assertion_are_written_and_never_pre_initialized(name: str) -> None:
    module = CASES[name]["module"]
    assert set(module.REACHED_MARKERS) == set(module.ASSERTIONS)

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    written_as = CASES[name].get("written_as", lambda marker: marker)
    written = _written_paths(tree, CASES[name].get("written_names", {"observation", "observations"}))
    # Codex assigns each phase's record as observations[name] over its phases dict.
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "phases" for t in node.targets):
            if isinstance(node.value, ast.Dict):
                written |= {key.value for key in node.value.keys if isinstance(key, ast.Constant)}
    markers = {written_as(marker) for markers in module.REACHED_MARKERS.values() for marker in markers}
    assert markers <= written, f"markers the producer never writes: {sorted(markers - written)}"

    # The observation's initializer must not pre-set a marker's top-level record.
    initializers = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id in CASES[name].get("written_names", {"observation", "observations"})
        and isinstance(node.value, ast.Dict)
    ]
    assert initializers
    pre_set = {
        key.value
        for initializer in initializers
        for key in initializer.keys
        if isinstance(key, ast.Constant) and key.value in {marker.split(".")[0] for marker in markers}
    }
    assert not pre_set, f"markers initialized before any step runs: {pre_set}"


def test_reached_only_reads_nested_markers_and_treats_empty_as_unreached() -> None:
    markers = {"a": ("launch.accepted",), "b": ("send",), "c": ("abort",)}
    assertions = {"a": True, "b": False, "c": False}

    assert reached_only(assertions, {"launch": {"accepted": False}, "send": {"x": 1}, "abort": {}}, markers) == {
        "a": True,
        "b": False,
    }
    assert reached_only(assertions, {"launch": {}}, markers) == {}
    # An assertion with no marker entry is unreached, not a KeyError on the error path.
    assert reached_only({"new": False}, {"send": {"x": 1}}, markers) == {}
