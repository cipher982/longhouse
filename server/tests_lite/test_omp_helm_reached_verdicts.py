"""An errored OMP Helm lifecycle reports a verdict only for the steps it reached.

The factory files a false assertion as a product finding and a missing one as
"no verdict for assertion" (harness). So an unreached step must be absent, never
False and never True; a reached failing step stays a finding; and a run whose
reached steps all held is a typed harness failure with its evidence kept as
partial_observation.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from zerg.qa import factory_registration
from zerg.qa import omp_helm_lifecycle as producer

SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "managed_providers.yml"

_SEND_REACHED = {
    "send_idle": True,
    "send_evidence": {"native_source_bound": True, "marker_count": 1, "channel_ack_bound": True},
}
_FOLLOW_UP_FAILED = {
    "follow_up_native": False,
    "follow_up_evidence": {"native_source_bound": True, "marker_count": 0, "channel_ack_bound": True},
}


def _run_main_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observation: dict | None) -> tuple[dict, str]:
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    if observation is not None:
        (evidence / "partial-observation.json").write_text(
            json.dumps({"schema_version": 1, "status": "fail", "error": "x", "observation": observation})
        )

    def boom(_args):
        raise RuntimeError("late OMP failure")

    monkeypatch.setattr(producer, "run_omp_helm", boom)
    variant = producer._VARIANTS[0]
    binary = tmp_path / "omp"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    producer.main(
        [
            "--variant",
            variant,
            "--evidence-root",
            str(evidence),
            "--repo-root",
            str(tmp_path),
            "--engine",
            str(binary),
            "--provider-bin",
            str(binary),
            "--api-url",
            "http://127.0.0.1:1",
            "--agents-token",
            "token",
        ]
    )
    return json.loads((evidence / "result.json").read_text()), variant


def _envelope_by_cell(result: dict, variant: str) -> dict[str, list[str]]:
    registration = producer.REGISTRATION.to_dict()
    # One scenario run answers every cell; each cell's command is checked against it.
    commands = factory_registration.factory_commands(registration, factory_registration.contract_rows(SCHEMA))
    assert {command["assertion_id"] for command in commands} == set(producer.ASSERTIONS)
    return {command["assertion_id"]: factory_registration.result_envelope_failures(command, result) for command in commands}


def test_a_reached_failing_step_is_a_finding_and_unreached_steps_have_no_verdict(tmp_path, monkeypatch) -> None:
    result, variant = _run_main_with(tmp_path, monkeypatch, {"observation_scope": "scenario", **_SEND_REACHED, **_FOLLOW_UP_FAILED})

    assert result["status"] == "fail"
    assert result["assertions"] == {"omp_helm_send_idle": True, "omp_helm_follow_up_native": False}
    by_cell = _envelope_by_cell(result, variant)
    # Reached cells are answered: send passes, follow-up is a product finding.
    assert by_cell["omp_helm_send_idle"] == [] and by_cell["omp_helm_follow_up_native"] == []
    # Unreached cells are absent, which the factory files as harness.
    for assertion_id, failures in by_cell.items():
        if assertion_id not in result["assertions"]:
            assert failures == [f"no verdict for assertion {assertion_id!r}"], assertion_id


def test_an_error_after_only_passing_steps_is_a_typed_harness_failure(tmp_path, monkeypatch) -> None:
    result, variant = _run_main_with(tmp_path, monkeypatch, {"observation_scope": "scenario", **_SEND_REACHED})

    assert "observation" not in result and "assertions" not in result
    assert result["partial_observation"]["send_idle"] is True
    assert factory_registration.typed_harness_failure(result)
    assert all(failures == [] for failures in _envelope_by_cell(result, variant).values())


def test_an_error_before_any_step_is_a_typed_harness_failure(tmp_path, monkeypatch) -> None:
    result, variant = _run_main_with(tmp_path, monkeypatch, None)

    assert "observation" not in result and "assertions" not in result
    assert factory_registration.typed_harness_failure(result)
    assert all(failures == [] for failures in _envelope_by_cell(result, variant).values())


def test_reached_markers_cover_every_assertion_and_none_is_pre_initialized() -> None:
    """A marker only proves a step ran if the run does not set it up front.

    run_omp_helm initializes every step flag to False and settlement/cleanup to {}
    before any step runs; a marker initialized to anything non-empty would mark an
    unreached step as reached.
    """

    function_names = {"omp_helm_lifecycle_assertions"}
    tree = ast.parse(Path(producer.__file__).read_text(encoding="utf-8"))
    assertions_fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in function_names)
    returned = next(node for node in assertions_fn.body if isinstance(node, ast.Return)).value
    assert isinstance(returned, ast.Dict)
    assert set(producer.REACHED_MARKERS) == {key.value for key in returned.keys}

    run_fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run_omp_helm")
    initializer = next(
        node.value
        for node in ast.walk(run_fn)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "observation"
    )
    assert isinstance(initializer, ast.Dict)
    initial = {key.value: ast.literal_eval(value) for key, value in zip(initializer.keys, initializer.values, strict=True)}
    markers = {key for keys in producer.REACHED_MARKERS.values() for key in keys}
    pre_set = {key: initial[key] for key in markers if key in initial and initial[key] not in ({}, [], None, "")}
    assert not pre_set, f"markers initialized before any step runs: {pre_set}"
