"""Hermetic assurance for iOS launch-workspace cache reconciliation.

The provider factory runs on Linux and deliberately does not carry Xcode.  This
producer therefore checks the pinned production Swift source itself.  It uses a
small lexical scope reader rather than test names or global substring matches:
the selection policy and its call sites must occur in the live declarations at
the expected brace depth.  A stale unit test cannot satisfy this assertion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Callable

from zerg.qa.resume_assurance import ProducerRegistration

SCENARIO_ID = "ios_workspace_selection_source_contract"
ASSERTION_ID = "ios_fresh_ranking_replaces_implicit_cache"
SOURCE_PATH = Path("ios/Sources/LonghouseApp/LaunchSessionSheet.swift")

REGISTRATION = ProducerRegistration(
    producer_id="longhouse.ios_workspace_selection_source.v1",
    producer_revision=1,
    scenario_id=SCENARIO_ID,
    scenario_revision=1,
    assertion_cells=((ASSERTION_ID, None),),
    providers=(),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("console",),
    evidence_classes=("hermetic",),
    observed_activity=(
        "production_swift_source_read",
        "implicit_cached_default_reconciled_from_fresh_ranking",
        "explicit_absolute_choice_preserved",
        "picker_marks_explicit_choice",
        "machine_change_resets_implicit_default",
        "unlaunchable_machine_clears_implicit_default",
        "legacy_cache_generation_invalidated",
    ),
    acquisition_methods=("hermetic_source_under_test",),
    credential_binding_ids=(),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=("selection_contract_observation", "cleanup_receipt"),
    required_cleanup=("no_owned_processes",),
    implementation="server/zerg/qa/ios_workspace_selection_source_producer.py",
    oracle_source="server/zerg/qa/ios_workspace_selection_source_producer.py",
    oracle_entrypoint="run_ios_workspace_selection_source_oracle",
    executable_module="zerg.qa.ios_workspace_selection_source_producer",
    provider_artifact_required=False,
    subject_kind="longhouse_product",
)


class SourceContractError(ValueError):
    """The pinned Swift source does not have one unambiguous live scope."""


@dataclass(frozen=True)
class _Scope:
    text: str
    lexical: str
    code: str


def _masks(source: str) -> tuple[str, str]:
    """Return comment-free and code-only same-length views of Swift source."""

    lexical = list(source)
    code = list(source)
    index = 0
    block_depth = 0
    mode = "code"
    quote_width = 0
    while index < len(source):
        if mode == "line_comment":
            if source[index] == "\n":
                mode = "code"
            else:
                lexical[index] = code[index] = " "
            index += 1
            continue
        if mode == "block_comment":
            if source.startswith("/*", index):
                lexical[index : index + 2] = code[index : index + 2] = [" ", " "]
                block_depth += 1
                index += 2
            elif source.startswith("*/", index):
                lexical[index : index + 2] = code[index : index + 2] = [" ", " "]
                block_depth -= 1
                index += 2
                if block_depth == 0:
                    mode = "code"
            else:
                if source[index] != "\n":
                    lexical[index] = code[index] = " "
                index += 1
            continue
        if mode == "string":
            delimiter = '"' * quote_width
            if source.startswith(delimiter, index) and (quote_width == 3 or index == 0 or source[index - 1] != "\\"):
                for offset in range(quote_width):
                    code[index + offset] = " "
                index += quote_width
                mode = "code"
            else:
                if source[index] != "\n":
                    code[index] = " "
                if quote_width == 1 and source[index] == "\\" and index + 1 < len(source):
                    code[index + 1] = " "
                    index += 2
                else:
                    index += 1
            continue

        if source.startswith("//", index):
            lexical[index : index + 2] = code[index : index + 2] = [" ", " "]
            mode = "line_comment"
            index += 2
        elif source.startswith("/*", index):
            lexical[index : index + 2] = code[index : index + 2] = [" ", " "]
            mode = "block_comment"
            block_depth = 1
            index += 2
        elif source.startswith('"""', index):
            code[index : index + 3] = [" ", " ", " "]
            mode = "string"
            quote_width = 3
            index += 3
        elif source[index] == '"':
            code[index] = " "
            mode = "string"
            quote_width = 1
            index += 1
        else:
            index += 1
    return "".join(lexical), "".join(code)


def _scope(source: str, declaration: str) -> _Scope:
    lexical, code = _masks(source)
    starts = [match.start() for match in re.finditer(re.escape(declaration), code)]
    if len(starts) != 1:
        raise SourceContractError(f"expected exactly one live declaration: {declaration}")
    opening = code.find("{", starts[0] + len(declaration))
    if opening < 0:
        raise SourceContractError(f"declaration has no body: {declaration}")
    depth = 0
    closing = -1
    for index in range(opening, len(code)):
        if code[index] == "{":
            depth += 1
        elif code[index] == "}":
            depth -= 1
            if depth == 0:
                closing = index
                break
    if closing < 0:
        raise SourceContractError(f"declaration has an unbalanced body: {declaration}")
    return _Scope(
        text=source[opening + 1 : closing],
        lexical=lexical[opening + 1 : closing],
        code=code[opening + 1 : closing],
    )


def _matches(scope: _Scope, pattern: str) -> list[re.Match[str]]:
    matches = []
    for match in re.finditer(pattern, scope.lexical, flags=re.DOTALL):
        first_token = re.search(r"[A-Za-z_]", match.group(0))
        if first_token is None:
            continue
        position = match.start() + first_token.start()
        if scope.code[position] != " ":
            matches.append(match)
    return matches


def _one(scope: _Scope, pattern: str, label: str) -> re.Match[str]:
    matches = _matches(scope, pattern)
    if len(matches) != 1:
        raise SourceContractError(f"expected exactly one live {label}")
    return matches[0]


def _depth_at(scope: _Scope, position: int) -> int:
    return scope.code[:position].count("{") - scope.code[:position].count("}")


def _check_selection_policy(source: str) -> dict[str, bool]:
    scope = _scope(source, "func resolveFreshWorkspaceSelection(")
    explicit_guard = _one(
        scope,
        r'if\s+source\s*==\s*\.explicitUserChoice\s*,\s*normalized\.starts\(with:\s*"/"\)\s*\{',
        "explicit-choice guard",
    )
    explicit_return = _one(
        scope,
        r"return\s+WorkspaceSelectionResolution\(\s*path:\s*normalized\s*,\s*source:\s*\.explicitUserChoice\s*\)",
        "explicit-choice return",
    )
    implicit_return = _one(
        scope,
        r'return\s+WorkspaceSelectionResolution\(\s*path:\s*suggestions\.first\?\.path\s*\?\?\s*""\s*,\s*source:\s*\.implicitDefault\s*\)',
        "fresh implicit-default return",
    )
    ordered = explicit_guard.start() < explicit_return.start() < implicit_return.start()
    return {
        "explicit_absolute_choice_preserved": ordered
        and _depth_at(scope, explicit_guard.start()) == 0
        and _depth_at(scope, explicit_return.start()) == 1,
        "implicit_policy_uses_fresh_first_or_empty": ordered and _depth_at(scope, implicit_return.start()) == 0,
    }


def _check_fresh_response_wiring(source: str) -> bool:
    scope = _scope(source, "private func loadWorkspaceSuggestions(")
    fetch = _one(
        scope,
        r"let\s+suggestions\s*=\s*try\s+await\s+api\.workspaceSuggestions\(deviceId:\s*deviceId\)",
        "fresh workspace request",
    )
    resolution = _one(
        scope,
        r"let\s+selection\s*=\s*resolveFreshWorkspaceSelection\(\s*currentPath:\s*normalizedCwd\s*,\s*source:\s*workspaceSelectionSource\s*,\s*suggestions:\s*suggestions\s*\)",
        "fresh selection resolution",
    )
    path_assignment = _one(scope, r"cwd\s*=\s*selection\.path", "fresh path assignment")
    source_assignment = _one(
        scope,
        r"workspaceSelectionSource\s*=\s*selection\.source",
        "fresh source assignment",
    )
    cache_save = _one(
        scope,
        r"WorkspaceSuggestionsCacheStore\.save\(workspaces:\s*suggestions",
        "fresh cache save",
    )
    positions = [fetch.start(), resolution.start(), path_assignment.start(), source_assignment.start(), cache_save.start()]
    if positions != sorted(positions):
        return False
    depths = {_depth_at(scope, position) for position in positions}
    if len(depths) != 1:
        return False
    legacy_guards = _matches(
        _Scope(
            text=scope.text[fetch.end() : cache_save.start()],
            lexical=scope.lexical[fetch.end() : cache_save.start()],
            code=scope.code[fetch.end() : cache_save.start()],
        ),
        r"if\s+normalizedCwd\.isEmpty",
    )
    return not legacy_guards


def _check_picker_wiring(source: str) -> bool:
    scope = _scope(source, "private var formView: some View")
    callback = _one(scope, r"\)\s*\{\s*path\s+in", "workspace picker callback")
    path_assignment = _one(scope, r"cwd\s*=\s*path", "explicit picker path assignment")
    source_assignment = _one(
        scope,
        r"workspaceSelectionSource\s*=\s*\.explicitUserChoice",
        "explicit picker source assignment",
    )
    return callback.start() < path_assignment.start() < source_assignment.start() and _depth_at(
        scope, path_assignment.start()
    ) == _depth_at(scope, source_assignment.start())


def _has_reset(scope: _Scope) -> bool:
    path_reset = _one(scope, r'cwd\s*=\s*""', "workspace path reset")
    source_reset = _one(
        scope,
        r"workspaceSelectionSource\s*=\s*\.implicitDefault",
        "workspace source reset",
    )
    return path_reset.start() < source_reset.start() and _depth_at(scope, path_reset.start()) == _depth_at(scope, source_reset.start())


def _check_unlaunchable_reset(source: str) -> bool:
    scope = _scope(source, "private func loadWorkspaceSuggestions(")
    guard = _one(
        scope,
        r"guard\s+Self\.canStartInteractiveSession\(",
        "unlaunchable-machine guard",
    )
    cache_start = _one(scope, r"let\s+startedAt\s*=\s*Date\(\)", "workspace request start")
    if guard.start() >= cache_start.start():
        return False
    guard_scope = _Scope(
        text=scope.text[guard.start() : cache_start.start()],
        lexical=scope.lexical[guard.start() : cache_start.start()],
        code=scope.code[guard.start() : cache_start.start()],
    )
    return _has_reset(guard_scope) and bool(_matches(guard_scope, r"\breturn\b"))


def _check_cache_generation(source: str) -> bool:
    scope = _scope(source, "enum WorkspaceSuggestionsCacheStore")
    cache_key = _one(
        scope,
        r'private\s+static\s+let\s+cacheKey\s*=\s*"longhouse\.launch\.workspaces\.cache\.v2"',
        "v2 workspace cache key",
    )
    version = _one(scope, r"private\s+static\s+let\s+version\s*=\s*2\b", "v2 workspace cache version")
    return _depth_at(scope, cache_key.start()) == 0 and _depth_at(scope, version.start()) == 0


def evaluate_launch_workspace_selection_source(source: str) -> dict[str, Any]:
    try:
        policy = _check_selection_policy(source)
    except SourceContractError:
        policy = {
            "explicit_absolute_choice_preserved": False,
            "implicit_policy_uses_fresh_first_or_empty": False,
        }

    def checked(function: Callable[[], bool]) -> bool:
        try:
            return bool(function())
        except SourceContractError:
            return False

    facts = {
        "production_swift_source_read": True,
        "implicit_cached_default_reconciled_from_fresh_ranking": (
            policy["implicit_policy_uses_fresh_first_or_empty"] and checked(lambda: _check_fresh_response_wiring(source))
        ),
        "explicit_absolute_choice_preserved": policy["explicit_absolute_choice_preserved"],
        "picker_marks_explicit_choice": checked(lambda: _check_picker_wiring(source)),
        "machine_change_resets_implicit_default": checked(
            lambda: _has_reset(_scope(source, "private func selectMachine(")) and _has_reset(_scope(source, "private func loadMachines()"))
        ),
        "unlaunchable_machine_clears_implicit_default": checked(lambda: _check_unlaunchable_reset(source)),
        "legacy_cache_generation_invalidated": checked(lambda: _check_cache_generation(source)),
    }
    return {"passed": all(facts.values()), **facts}


def _repo_root_from_cwd() -> Path:
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / SOURCE_PATH).is_file() and (candidate / "schemas/product_assurance.yml").is_file():
            return candidate
    raise SourceContractError("could not locate the pinned Longhouse source checkout from cwd")


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


def run_ios_workspace_selection_source_oracle(*, evidence_root: Path, repo_root: Path | None = None) -> dict[str, Any]:
    evidence_root.mkdir(parents=True, exist_ok=False)
    root = (repo_root or _repo_root_from_cwd()).resolve()
    source_path = root / SOURCE_PATH
    source = source_path.read_text(encoding="utf-8")
    observation = evaluate_launch_workspace_selection_source(source)
    observation.update(
        {
            "source_path": SOURCE_PATH.as_posix(),
            "source_sha256": f"sha256:{hashlib.sha256(source.encode('utf-8')).hexdigest()}",
            "source_size": len(source.encode("utf-8")),
        }
    )
    _write_json(evidence_root / "selection-contract-observation.json", observation)
    _write_json(
        evidence_root / "cleanup-receipt.json",
        {"status": "pass", "owned_process_count": 0, "orphan_count": 0},
    )
    return observation


def run(evidence_root: Path, *, repo_root: Path | None = None) -> dict[str, Any]:
    try:
        observation = run_ios_workspace_selection_source_oracle(
            evidence_root=evidence_root,
            repo_root=repo_root,
        )
        passed = bool(observation["passed"])
        result = {
            "schema_version": 1,
            "artifact_kind": "longhouse_ios_workspace_selection_source_result",
            "producer": REGISTRATION.to_dict(),
            "provider": None,
            "variant": None,
            "scenario_id": SCENARIO_ID,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "hermetic",
            "status": "pass" if passed else "fail",
            "observation": observation,
            "assertions": {ASSERTION_ID: passed},
        }
    except Exception as exc:  # noqa: BLE001 - typed failure evidence is the producer contract
        evidence_root.mkdir(parents=True, exist_ok=True)
        if not (evidence_root / "cleanup-receipt.json").exists():
            _write_json(
                evidence_root / "cleanup-receipt.json",
                {
                    "status": "pass",
                    "owned_process_count": 0,
                    "orphan_count": 0,
                    "error_type": type(exc).__name__,
                },
            )
        result = {
            "schema_version": 1,
            "artifact_kind": "longhouse_ios_workspace_selection_source_result",
            "producer": REGISTRATION.to_dict(),
            "provider": None,
            "variant": None,
            "scenario_id": SCENARIO_ID,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "hermetic",
            "status": "fail",
            "failure_code": "ios_workspace_selection_source_contract_failed",
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
