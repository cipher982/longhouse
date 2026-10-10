"""A public change must not be able to make the provider factory refuse an epoch or a result.

Replays the two 2026-10-01 incidents against this tree: release-gate rows whose producer the
factory had not registered (public 13edac91d, 43d053f81 and 912b6e979), and a producer result
whose ``variant`` was the execution key (912b6e979). See ``zerg.qa.factory_registration``.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from zerg.qa import factory_registration as factory
from zerg.qa.provider_factory_model import load_capability_assertions

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / factory.MANIFEST_PATH
SCHEMA = ROOT / factory.SCHEMA_PATH

OMP_BACKGROUND = "omp.background_jobs.v1"
CLAUDE_BACKGROUND = "claude.background_jobs.v1"
OMP_BACKGROUND_ROWS = {
    "omp:omp_background_registry_owner_scoped:None",
    "omp:omp_background_partial_progress_child_scoped:None",
    "omp:omp_background_terminal_status_preserved:None",
}
CLAUDE_BACKGROUND_ROWS = {
    "claude:claude_background_native_source:None",
    "claude:claude_background_parent_turn_boundary:None",
    "claude:claude_background_registry_served:None",
    "claude:claude_background_callbacks_scoped:None",
    "claude:claude_background_explicit_empty:None",
}


def _registrations() -> dict[str, dict[str, Any]]:
    """Every public producer's registration, keyed by module."""

    from zerg.qa.provider_factory_model import PRODUCER_MODULES

    return {module: importlib.import_module(module).REGISTRATION.to_dict() for module in PRODUCER_MODULES}


def _failures(manifest: list[dict[str, str]], rows: list[dict[str, Any]] | None = None) -> list[str]:
    registrations = _registrations()
    listed = {entry["module"] for entry in manifest}
    return factory.registry_failures(
        manifest,
        {module: registration for module, registration in registrations.items() if module in listed},
        factory.release_gate_rows(SCHEMA) if rows is None else rows,
        unlisted=[registration for module, registration in registrations.items() if module not in listed],
    )


def _without(*producer_ids: str) -> list[dict[str, str]]:
    return [entry for entry in factory.load_manifest(MANIFEST) if entry["producer_id"] not in producer_ids]


def test_every_release_gate_row_has_a_listed_producer_that_can_run_it() -> None:
    assert factory.check_tree(ROOT) == []


def test_the_checked_in_check_passes_and_names_what_it_covered(capsys: pytest.CaptureFixture[str]) -> None:
    assert factory.main(["check", "--root", str(ROOT)]) == 0
    assert "producers cover" in capsys.readouterr().out


def test_an_unreadable_manifest_is_refused_not_a_traceback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert factory.main(["check", "--root", str(tmp_path)]) == 2
    assert "schemas/factory_producers.yml is unreadable" in capsys.readouterr().err


def test_the_omp_background_rows_without_a_listed_producer_are_refused_as_at_13edac91d() -> None:
    """13edac91d authored three omp_background_* release-gate rows; their producer was not registered."""

    failures = _failures(_without(OMP_BACKGROUND))

    assert {
        failure.split(" has no registered producer")[0].removeprefix("release-gate row ") for failure in failures
    } == OMP_BACKGROUND_ROWS
    assert all(f"declared by {OMP_BACKGROUND}, which schemas/factory_producers.yml does not list" in failure for failure in failures)


def test_the_claude_background_rows_without_a_listed_producer_are_refused_as_at_912b6e979() -> None:
    """912b6e979 authored five claude_background_* release-gate rows; their producer was not registered."""

    failures = _failures(_without(CLAUDE_BACKGROUND))

    assert {
        failure.split(" has no registered producer")[0].removeprefix("release-gate row ") for failure in failures
    } == CLAUDE_BACKGROUND_ROWS


def test_both_unregistered_background_producers_are_refused_together() -> None:
    assert len(_failures(_without(OMP_BACKGROUND, CLAUDE_BACKGROUND))) == len(OMP_BACKGROUND_ROWS) + len(CLAUDE_BACKGROUND_ROWS)


def test_a_new_row_for_a_listed_producer_that_does_not_declare_it_is_refused() -> None:
    row = {
        "provider": "omp",
        "capability": "session.delegation.background",
        "assertion_id": "omp_background_new_assertion",
        "variant": None,
        "scenario_id": "omp_background_jobs",
        "minimum_scenario_revision": 1,
        "acceptable_evidence": ["live_token"],
    }

    assert _failures(factory.load_manifest(MANIFEST), [row]) == [
        "release-gate row omp:omp_background_new_assertion:None has no registered producer"
    ]


def test_a_row_whose_revision_scenario_or_evidence_the_registration_cannot_meet_is_refused() -> None:
    rows = [row for row in factory.release_gate_rows(SCHEMA) if row["assertion_id"] == "omp_background_registry_owner_scoped"]
    assert len(rows) == 1
    manifest = factory.load_manifest(MANIFEST)

    assert _failures(manifest, rows) == []
    assert len(_failures(manifest, [{**rows[0], "minimum_scenario_revision": 99}])) == 1
    assert len(_failures(manifest, [{**rows[0], "scenario_id": "another_scenario"}])) == 1
    assert len(_failures(manifest, [{**rows[0], "acceptable_evidence": ["hermetic"]}])) == 1


def test_a_listed_producer_must_export_the_registration_it_is_listed_for() -> None:
    manifest = factory.load_manifest(MANIFEST)
    registrations = {entry["module"]: _registrations()[entry["module"]] for entry in manifest}
    entry = manifest[0]

    assert factory.registry_failures(manifest, {**registrations, entry["module"]: None}, []) == [
        f"{entry['module']} is listed for {entry['producer_id']} but exports no REGISTRATION"
    ]
    renamed = [{**entry, "producer_id": "codex.renamed.v1"}, *manifest[1:]]
    assert factory.registry_failures(renamed, registrations, []) == [
        f"{entry['module']} registers {entry['producer_id']!r}, the manifest lists 'codex.renamed.v1'"
    ]
    assert factory.registry_failures([*manifest, entry], registrations, []) == [
        f"manifest lists {entry['producer_id']} ({entry['module']}) twice"
    ]
    assert factory.registry_failures([{"producer_id": "x.v1", "module": "somewhere.else"}], {}, []) != []


def test_every_listed_producer_is_a_producer_module_the_directory_registry_knows() -> None:
    from zerg.qa.provider_factory_model import PRODUCER_MODULES

    assert {entry["module"] for entry in factory.load_manifest(MANIFEST)} <= set(PRODUCER_MODULES)
    for entry in factory.load_manifest(MANIFEST):
        assert (ROOT / factory.module_path(entry["module"])).is_file()


def test_the_contract_rows_agree_with_the_public_capability_loader() -> None:
    """``contract_rows`` ports the factory's reader; the two public readers must not disagree about the schema."""

    ported = {
        (r["provider"], r["assertion_id"], r["variant"], r["scenario_id"], r["assurance_priority"]) for r in factory.contract_rows(SCHEMA)
    }
    loaded = {(a.provider, a.assertion_id, a.variant, a.scenario_id, a.assurance_priority) for a in load_capability_assertions()}
    assert ported == loaded


def test_the_manifest_is_valid_yaml_with_the_documented_shape() -> None:
    payload = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    assert payload["schema_version"] == factory.MANIFEST_SCHEMA_VERSION
    assert all(set(entry) == {"producer_id", "module"} for entry in payload["producers"])


# --- the result envelope ---------------------------------------------------------------------


def _claude_helm_command() -> dict[str, Any]:
    from zerg.qa import claude_helm_lifecycle

    commands = factory.factory_commands(claude_helm_lifecycle.REGISTRATION.to_dict(), factory.release_gate_rows(SCHEMA))
    return next(command for command in commands if command["assertion_id"] == "claude_helm_abort_native")


def _answer(command: dict[str, Any], **changes: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "producer": {"producer_id": command["producer_id"]},
        "provider": command["provider"],
        "variant": command["variant"],
        "scenario_id": command["scenario_id"],
        "scenario_revision": command["scenario_revision"],
        "evidence_class": command["evidence_class"],
        "observation_scope": command["observation_scope"],
        "generated_at": "2026-10-01T07:00:00Z",
        "status": "pass",
        "observation": {},
        "assertions": {command["assertion_id"]: True},
        "artifact_manifest": [],
    }
    result.update(changes)
    return result


def test_a_complete_answer_to_the_command_is_accepted() -> None:
    command = _claude_helm_command()

    assert command["variant"] is None
    assert command["execution_variant"] == "cell:claude:claude_helm_abort_native:claude_helm_lifecycle"
    assert factory.result_envelope_failures(command, _answer(command)) == []


def test_a_result_reporting_the_execution_key_as_its_variant_is_refused_as_at_912b6e979() -> None:
    """912b6e979 reported getattr(args, "variant", None): the factory's own message for the five claude_helm_* cells."""

    command = _claude_helm_command()
    result = _answer(command, variant=command["execution_variant"])

    assert factory.result_envelope_failures(command, result) == [
        "variant is 'cell:claude:claude_helm_abort_native:claude_helm_lifecycle', expected None"
    ]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"artifact_manifest": None}, "artifact_manifest is not a list"),
        ({"observation": None}, "observation is not an object"),
        ({"assertions": None}, "assertions is not an object"),
        ({"producer": {"producer_id": "other.v1"}}, "producer_id is 'other.v1', expected 'claude.helm_lifecycle.v1'"),
        ({"scenario_revision": 1}, "scenario_revision is 1, expected 7"),
        ({"evidence_class": "hermetic"}, "evidence_class is 'hermetic', expected 'live_token'"),
        ({"provider": "codex"}, "provider is 'codex', expected 'claude'"),
        ({"generated_at": "yesterday"}, "invalid generated_at"),
        ({"generated_at": "2026-10-01T07:00:00"}, "timezone-naive generated_at"),
        ({"assertions": {"other": True}}, "no verdict for assertion 'claude_helm_abort_native'"),
        ({"assertions": {"claude_helm_abort_native": "yes"}}, "non-boolean assertion verdict"),
        ({"status": "pass", "assertions": {"claude_helm_abort_native": False}}, "status 'pass' contradicts its assertion map ('fail')"),
    ],
)
def test_each_envelope_field_the_factory_compares_is_held_to(change: dict[str, Any], expected: str) -> None:
    command = _claude_helm_command()
    assert factory.result_envelope_failures(command, _answer(command, **change)) == [expected]


def test_a_renamed_envelope_key_is_a_refusal() -> None:
    command = _claude_helm_command()
    result = _answer(command)
    result["artifacts"] = result.pop("artifact_manifest")

    assert factory.result_envelope_failures(command, result) == ["artifact_manifest is not a list"]


def test_a_typed_harness_failure_is_accepted_because_the_factory_reports_its_cause() -> None:
    command = _claude_helm_command()
    failure = {
        "status": "fail",
        "failure_code": "claude_helm_lifecycle_failed",
        "error": "RuntimeError: x",
        "observation_scope": "scenario",
    }

    assert factory.typed_harness_failure(failure)
    assert factory.result_envelope_failures(command, failure) == []
    # The factory checks the scope before the outcome: a scenario producer's typed failure without it
    # was reported as "returned a cell-specific result" and its cause never reached a case.
    unscoped = {key: value for key, value in failure.items() if key != "observation_scope"}
    assert factory.result_envelope_failures(command, unscoped) == [
        "scenario-scoped producer returned a cell-specific result (observation_scope is None)"
    ]
    # Keeping an observation or an assertion map makes it an ordinary result, held to the whole envelope.
    assert not factory.typed_harness_failure({**failure, "assertions": {}})
    assert factory.result_envelope_failures(command, {**failure, "assertions": {}}) == ["invalid generated_at"]


# --- no producer reports the invocation's variant -------------------------------------------


def _reads_the_invocation_variant(value: ast.AST) -> bool:
    for node in ast.walk(value):
        if isinstance(node, ast.Attribute) and node.attr == "variant":
            return True
        if isinstance(node, ast.Name) and node.id == "variant":
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "variant"
        ):
            return True
    return False


def _invocation_variant_reports(source: str, *, mixed: bool = False) -> list[int]:
    """Lines of dict literals whose "variant" value is read from the invocation (``args.variant``, ``getattr``).

    ``mixed`` is a producer with authored and unauthored cells: ``None if steer else variant`` is how it reports each
    cell's own variant, and any other read of the invocation's variant would be wrong for the unauthored cells.
    """

    def reads(value: ast.AST) -> bool:
        if (
            mixed
            and isinstance(value, ast.IfExp)
            and any(isinstance(branch, ast.Constant) and branch.value is None for branch in (value.body, value.orelse))
        ):
            return False
        return _reads_the_invocation_variant(value)

    return [
        key.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values)
        if isinstance(key, ast.Constant) and key.value == "variant" and reads(value)
    ]


def test_a_producer_never_reports_the_invocation_variant_for_a_cell_that_authors_none() -> None:
    """The invocation's ``--variant`` is the execution key unless a cell authors a variant (Resume's clean_exit and so on).

    912b6e979 wrote ``"variant": getattr(args, "variant", None)`` in a producer whose cells author none, on every path
    of the Claude Helm result; a path no fixture drives is still caught here. A backstop, not an exhaustive proof: it
    reads direct and ``getattr`` uses, so an aliased read passes, and the producers' conformance tests run the result.
    """

    reported = {}
    for entry in factory.load_manifest(MANIFEST):
        module = importlib.import_module(entry["module"])
        authored = [variant for _assertion, variant in module.REGISTRATION.assertion_cells]
        if all(authored):
            continue
        if lines := _invocation_variant_reports(Path(module.__file__).read_text(encoding="utf-8"), mixed=any(authored)):
            reported[entry["module"]] = lines
    assert reported == {}


def test_the_static_check_sees_the_912b6e979_literal_and_leaves_authored_variants_alone() -> None:
    assert _invocation_variant_reports('result = {"variant": getattr(args, "variant", None), "status": "pass"}') == [1]
    assert _invocation_variant_reports('result = {"variant": args.variant}') == [1]
    assert _invocation_variant_reports('result = {"variant": None, "execution_variant": args.variant}') == []
    assert _invocation_variant_reports('result = {"variant": "interrupt_supported"}') == []
    # A producer with authored and unauthored cells may choose per cell, but may not read the invocation's variant plainly.
    assert _invocation_variant_reports('result = {"variant": None if steer else variant}', mixed=True) == []
    assert _invocation_variant_reports('result = {"variant": None if steer else variant}') == [1]
    assert _invocation_variant_reports('result = {"variant": args.variant}', mixed=True) == [1]


def test_settle_failed_result_keeps_a_verdict_only_with_evidence() -> None:
    """Evidence carries its verdict; no observation means a typed harness failure.

    settle_failed_result (the producers' leaf helper) and typed_harness_failure
    (the factory's mirror of control-plane validation) live in different
    modules, so this also pins that they agree.
    """

    from zerg.qa.failed_results import settle_failed_result

    base = {"status": "fail", "failure_code": "x_failed", "error": "RuntimeError: bridge never came up"}

    with_evidence = settle_failed_result(dict(base), observation={"seen": True}, assertions={"a": False})
    assert with_evidence["observation"] == {"seen": True}
    assert with_evidence["assertions"] == {"a": False}
    assert not factory.typed_harness_failure(with_evidence)

    all_held = settle_failed_result(dict(base), observation={"seen": True}, assertions={"a": True})
    assert "observation" not in all_held and "assertions" not in all_held
    assert all_held["partial_observation"] == {"seen": True}
    assert factory.typed_harness_failure(all_held)

    empty_observation = settle_failed_result(dict(base), observation={}, assertions={"a": False})
    assert factory.typed_harness_failure(empty_observation)

    stale = {**base, "observation": {}, "assertions": {"a": False}}
    no_verdict = settle_failed_result(stale, observation=None, assertions={"a": False})
    assert "observation" not in no_verdict and "assertions" not in no_verdict
    assert factory.typed_harness_failure(no_verdict)


def test_failed_results_is_a_leaf_and_producers_never_import_factory_registration() -> None:
    """The factory pins every module a producer imports (its verifier closure).

    failed_results must import nothing from Longhouse, and producers take the
    failure helpers from it, never from factory_registration, whose closure is
    most of the server: one such import made every Longhouse commit read as a
    verifier change and blocked an epoch accept (2026-10-10).
    """

    qa = Path(factory.__file__).resolve().parent
    leaf = ast.parse((qa / "failed_results.py").read_text(encoding="utf-8"))
    leaf_imports = {node.module or "" for node in ast.walk(leaf) if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in ast.walk(leaf) if isinstance(node, ast.Import) for alias in node.names
    }
    assert not {name for name in leaf_imports if name.startswith("zerg")}, leaf_imports

    # Producers are registered in several shapes (a script's main(), a wrapper
    # around a shared runner), so the rule is simply: nothing under zerg imports
    # factory_registration. Today nothing does; the tests and the control plane
    # are its only callers.
    offenders = []
    for path in sorted(qa.parent.rglob("*.py")):
        if path == Path(factory.__file__).resolve():
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                # Resolve relative imports against the importing module's
                # package, so `from .factory_registration import x` and
                # `from . import factory_registration` are caught too.
                package = ".".join(("zerg", *path.relative_to(qa.parent).parent.parts))
                base = node.module or ""
                if node.level:
                    anchor = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                    base = ".".join([*anchor, *([base] if base else [])])
                imported = [base] + [f"{base}.{alias.name}" for alias in node.names]
            elif isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            else:
                continue
            if "zerg.qa.factory_registration" in imported:
                offenders.append(str(path.relative_to(qa.parent)))
    assert not offenders, f"modules importing factory_registration: {offenders}"
