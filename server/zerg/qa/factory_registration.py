"""What the private provider factory requires of a public producer, checked where it is written.

The factory (control-plane ``provider_factory/assurance.py``) refuses two kinds of public change
that nothing in this repository used to notice:

* A ``release_gate`` row in ``schemas/managed_providers.yml`` whose producer is not in the
  factory's ``PRODUCER_MODULES``: ``accept-epoch`` fails at every later head ("release-gate
  contract rows with no registered producer"), and the only fix is a factory deploy. This
  happened for the OMP and the Claude background producers on 2026-10-01.
* A producer result whose envelope differs from the command it ran for (a renamed key, or a
  ``variant`` that is the execution key instead of the authored variant): every cell of that
  producer fails as ``malformed result``.

This repository cannot read the control plane, so the ids the factory knows are mirrored in
``schemas/factory_producers.yml`` and control-plane CI compares the mirror with
``PRODUCER_MODULES`` at the accepted pin. ``check`` (``make validate-factory-registration``)
fails when a release-gate row has no listed producer. ``result_envelope_failures`` restates the
envelope the factory compares, so a producer's own tests can hold its result to it; a
control-plane test runs this function and the factory's validator side by side.

Importing this module touches no ``zerg`` package, so the control plane can load the file alone.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

MANIFEST_PATH = "schemas/factory_producers.yml"
SCHEMA_PATH = "schemas/managed_providers.yml"
MANIFEST_SCHEMA_VERSION = 1
PROVIDER_RELEASE = "provider_release"

REMEDY = (
    "A release-gate row is only real once the factory runs a producer for it. Register the producer in "
    "control-plane provider_factory/assurance.py PRODUCER_MODULES and list it in schemas/factory_producers.yml "
    "in the same unit of work (control-plane CI compares the two at the accepted epoch's pin); author the row "
    "`assurance_priority: ordinary_ci` or `sampled` until both have landed."
)


def module_path(module: str) -> str:
    """The repo-relative file a ``zerg.qa`` producer module lives in."""

    return "server/" + module.replace(".", "/") + ".py"


def load_manifest(path: Path) -> list[dict[str, str]]:
    import yaml

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{MANIFEST_PATH} is unreadable: {exc}") from exc
    entries = payload.get("producers") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("schema_version") != MANIFEST_SCHEMA_VERSION or not isinstance(entries, list):
        raise ValueError(f"{MANIFEST_PATH} must be a mapping with schema_version {MANIFEST_SCHEMA_VERSION} and a producers list")
    return [dict(entry) if isinstance(entry, dict) else {} for entry in entries]


def contract_rows(schema_path: Path) -> list[dict[str, Any]]:
    """Every provider row of the contract, shaped as accept-epoch reads it.

    Mirrors control-plane ``_contract_shape(schema, capability=None)``; a control-plane test
    compares the two at the accepted pin.
    """

    import yaml

    payload = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    providers = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(providers, list):
        raise ValueError(f"{SCHEMA_PATH} has no providers")
    rows: list[dict[str, Any]] = []
    for entry in providers:
        provider = entry.get("provider") if isinstance(entry, dict) else None
        capabilities = entry.get("capabilities") if isinstance(entry, dict) else None
        if not isinstance(provider, str) or not isinstance(capabilities, dict):
            continue
        for capability in sorted(key for key in capabilities if isinstance(key, str)):
            node = capabilities[capability]
            assertions = node.get("required_assertions") if isinstance(node, dict) else None
            if not isinstance(assertions, list):
                continue
            for assertion in assertions:
                rows.append(
                    {
                        "provider": provider,
                        "capability": capability,
                        "assertion_id": assertion["id"],
                        "variant": assertion.get("variant"),
                        "scenario_id": assertion["scenario_id"],
                        "minimum_scenario_revision": int(assertion["minimum_scenario_revision"]),
                        "acceptable_evidence": sorted(assertion.get("acceptable_evidence") or []),
                        "assurance_priority": str(assertion.get("assurance_priority") or "release_gate"),
                    }
                )
    return rows


def release_gate_rows(schema_path: Path) -> list[dict[str, Any]]:
    """The rows whose producer the factory must run (``assurance_priority: release_gate``)."""

    return [row for row in contract_rows(schema_path) if row["assurance_priority"] == "release_gate"]


def _cell_label(row: Mapping[str, Any]) -> str:
    return f"{row.get('provider')}:{row['assertion_id']}:{row.get('variant')}"


def registry_failures(
    manifest: Sequence[Mapping[str, Any]],
    registrations: Mapping[str, Mapping[str, Any] | None],
    rows: Sequence[Mapping[str, Any]],
    *,
    unlisted: Sequence[Mapping[str, Any]] = (),
) -> list[str]:
    """Why the factory could not run every release-gate row, empty when it can.

    ``registrations`` maps each listed module to its registration (``None`` when the module
    exports none); ``unlisted`` are registrations of public producers the manifest omits, used
    only to name the producer that declares an uncovered row.
    """

    failures: list[str] = []
    seen_ids: set[str] = set()
    seen_modules: set[str] = set()
    listed: list[Mapping[str, Any]] = []
    for entry in manifest:
        producer_id, module = entry.get("producer_id"), entry.get("module")
        if not isinstance(producer_id, str) or not producer_id or not isinstance(module, str) or not module.startswith("zerg.qa."):
            failures.append(f"manifest entry needs a producer_id and a zerg.qa module: {dict(entry)}")
            continue
        if producer_id in seen_ids or module in seen_modules:
            failures.append(f"manifest lists {producer_id} ({module}) twice")
        seen_ids.add(producer_id)
        seen_modules.add(module)
        registration = registrations.get(module)
        if registration is None:
            failures.append(f"{module} is listed for {producer_id} but exports no REGISTRATION")
        elif registration.get("producer_id") != producer_id:
            failures.append(f"{module} registers {registration.get('producer_id')!r}, the manifest lists {producer_id!r}")
        elif registration.get("executable_module") != module:
            failures.append(f"{producer_id} executes {registration.get('executable_module')!r}, the manifest lists {module!r}")
        else:
            listed.append(registration)

    def providers_of(registration: Mapping[str, Any]) -> list[str]:
        return list(registration.get("providers") or []) if registration.get("subject_kind") in (None, PROVIDER_RELEASE) else []

    def cells_of(registration: Mapping[str, Any]) -> set[tuple[str, str, Any]]:
        return {
            (provider, str(cell.get("assertion_id")), cell.get("variant"))
            for provider in providers_of(registration)
            for cell in registration.get("assertion_cells") or []
            if isinstance(cell, Mapping)
        }

    covering: dict[tuple[str, str, Any], list[Mapping[str, Any]]] = {}
    for registration in listed:
        for cell in cells_of(registration):
            covering.setdefault(cell, []).append(registration)
    for row in rows:
        key = (str(row.get("provider")), str(row["assertion_id"]), row.get("variant"))
        producers = covering.get(key)
        if not producers:
            hint = ""
            declared_by = [item["producer_id"] for item in unlisted if key in cells_of(item)]
            if declared_by:
                hint = f" (declared by {', '.join(declared_by)}, which {MANIFEST_PATH} does not list)"
            failures.append(f"release-gate row {_cell_label(row)} has no registered producer{hint}")
            continue
        if not any(_supports(registration, row) for registration in producers):
            failures.append(
                f"release-gate row {_cell_label(row)}: {producers[0]['producer_id']} registers scenario "
                f"{producers[0].get('scenario_id')!r} revision {producers[0].get('scenario_revision')!r} for "
                f"evidence {producers[0].get('evidence_classes')!r}, the row needs scenario {row['scenario_id']!r} "
                f"revision >= {row['minimum_scenario_revision']} evidence {row['acceptable_evidence']!r}"
            )
    return failures


def _supports(registration: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    """The part of the compiler's ``_producer_supports_cell`` that depends only on the repository."""

    scenarios = registration.get("scenario_ids") or [registration.get("scenario_id")]
    revision = registration.get("scenario_revision")
    return (
        row["scenario_id"] in scenarios
        and isinstance(revision, int)
        and revision >= row["minimum_scenario_revision"]
        and bool(set(registration.get("evidence_classes") or []) & set(row["acceptable_evidence"]))
    )


def check_tree(root: Path) -> list[str]:
    """Run the registry check against the checkout at ``root`` (its ``server/`` must be importable)."""

    root = root.resolve()
    manifest = load_manifest(root / MANIFEST_PATH)
    rows = release_gate_rows(root / SCHEMA_PATH)
    registrations: dict[str, Mapping[str, Any] | None] = {}
    problems: list[str] = []

    def registration_of(module: str) -> Mapping[str, Any] | None:
        loaded = importlib.import_module(module)
        if not Path(loaded.__file__ or "").resolve().is_relative_to(root):
            raise ValueError(f"{module} imports from {loaded.__file__}, not from {root}: run with PYTHONPATH={root / 'server'}")
        registration = getattr(loaded, "REGISTRATION", None)
        return registration.to_dict() if registration is not None else None

    for entry in manifest:
        module = entry.get("module")
        if isinstance(module, str) and module.startswith("zerg.qa."):
            try:
                registrations[module] = registration_of(module)
            except ImportError as exc:
                problems.append(f"{module} cannot be imported: {exc}")
    # A producer the manifest omits can only be named, never trusted, so it is read separately.
    from zerg.qa.provider_factory_model import PRODUCER_MODULES

    listed = {entry.get("module") for entry in manifest}
    unlisted = []
    for module in PRODUCER_MODULES:
        if module in listed:
            continue
        try:
            registration = registration_of(module)
        except ImportError as exc:
            problems.append(f"{module} cannot be imported: {exc}")
        else:
            if registration is not None:
                unlisted.append(registration)
    for module in sorted(str(item) for item in listed if isinstance(item, str) and item not in PRODUCER_MODULES):
        problems.append(f"{module} is listed but is not in provider_factory_model.PRODUCER_MODULES")
    return problems + registry_failures(manifest, registrations, rows, unlisted=unlisted)


# ---------------------------------------------------------------------------------------------
# The result envelope the factory compares against the command it ran.
# ---------------------------------------------------------------------------------------------


def factory_commands(registration: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The facts of each command the compiler builds for the rows ``registration`` covers.

    Only what a result is compared with: producer, scenario, authored variant and its execution
    key, evidence class, provider. ``rows`` are ``release_gate_rows`` (or any row of that shape).
    """

    from zerg.qa.resume_assurance import execution_variant_key

    commands = []
    for row in rows:
        cells = {(str(cell.get("assertion_id")), cell.get("variant")) for cell in registration.get("assertion_cells") or []}
        if (row["assertion_id"], row.get("variant")) not in cells or row.get("provider") not in (registration.get("providers") or []):
            continue
        evidence = sorted(set(registration.get("evidence_classes") or []) & set(row["acceptable_evidence"]))
        commands.append(
            {
                "producer_id": registration["producer_id"],
                "provider": row["provider"],
                "assertion_id": row["assertion_id"],
                "variant": row.get("variant"),
                "execution_variant": execution_variant_key(
                    provider=row["provider"],
                    assertion_id=row["assertion_id"],
                    scenario_id=row["scenario_id"],
                    variant=row.get("variant"),
                ),
                "scenario_id": row["scenario_id"],
                "scenario_revision": registration["scenario_revision"],
                "evidence_class": evidence[0] if evidence else None,
            }
        )
    return commands


def typed_harness_failure(result: Mapping[str, Any]) -> bool:
    """A producer that could not reach its observation boundary and says so, which the factory records as its cause.

    Mirrors the branch at the top of control-plane ``_validate_execution_outcome``: a failing result with a
    ``failure_code`` and an ``error`` and neither an ``observation`` nor an ``assertions`` object.
    """

    return (
        result.get("status") == "fail"
        and isinstance(result.get("failure_code"), str)
        and isinstance(result.get("error"), str)
        and not isinstance(result.get("observation"), Mapping)
        and not isinstance(result.get("assertions"), Mapping)
    )


def result_envelope_failures(command: Mapping[str, Any], result: Mapping[str, Any]) -> list[str]:
    """Why the factory would refuse ``result`` as the answer to ``command``, empty when it would not.

    Mirrors control-plane ``_validate_execution_outcome`` and the shape and verdict checks at the top of
    ``_validate_execution_result`` (the evidence-tree checks after them need real files and stay private). A
    typed harness failure is accepted: the factory reports its cause instead of calling it malformed. A
    control-plane test compares this function with the factory's validator on a corpus that includes the
    2026-10-01 ``variant`` regression.
    """

    if typed_harness_failure(result):
        return []
    failures: list[str] = []
    try:
        generated_at = datetime.fromisoformat(str(result.get("generated_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return ["invalid generated_at"]
    if generated_at.tzinfo is None:
        return ["timezone-naive generated_at"]
    producer = result.get("producer")
    if not isinstance(producer, Mapping):
        failures.append("producer is not an object")
    elif producer.get("producer_id") != command.get("producer_id"):
        failures.append(f"producer_id is {producer.get('producer_id')!r}, expected {command.get('producer_id')!r}")
    if not isinstance(result.get("observation"), Mapping):
        failures.append("observation is not an object")
    if not isinstance(result.get("artifact_manifest"), list):
        failures.append("artifact_manifest is not a list")
    assertions = result.get("assertions")
    if not isinstance(assertions, Mapping):
        failures.append("assertions is not an object")
    for field in ("provider", "variant", "scenario_id", "scenario_revision", "evidence_class"):
        if result.get(field) != command.get(field):
            failures.append(f"{field} is {result.get(field)!r}, expected {command.get(field)!r}")
    if command.get("vehicle_provider") is not None:
        for field in ("vehicle_provider", "vehicle_qualification_model"):
            if result.get(field) != command.get(field):
                failures.append(f"{field} is {result.get(field)!r}, expected {command.get(field)!r}")
    if failures or not isinstance(assertions, Mapping):
        return failures
    assertion_id = command.get("assertion_id")
    if assertion_id not in assertions:
        return [f"no verdict for assertion {assertion_id!r}"]
    if not assertions or any(type(value) is not bool for value in assertions.values()):
        return ["non-boolean assertion verdict"]
    expected_status = "pass" if all(assertions.values()) else "fail"
    if result.get("status") != expected_status:
        return [f"status {result.get('status')!r} contradicts its assertion map ({expected_status!r})"]
    return []


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("check",))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    args = parser.parse_args(argv)
    try:
        failures = check_tree(args.root)
    except ValueError as exc:
        print(f"factory registration: {exc}", file=sys.stderr)
        return 2
    if failures:
        print("factory registration: " + str(len(failures)) + " problem(s)", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        print(f"\n{REMEDY}", file=sys.stderr)
        return 1
    rows = release_gate_rows(args.root / SCHEMA_PATH)
    print(f"factory registration: {len(load_manifest(args.root / MANIFEST_PATH))} producers cover {len(rows)} release-gate rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
