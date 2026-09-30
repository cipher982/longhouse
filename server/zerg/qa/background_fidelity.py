"""Executable background-source replay helpers.

A background proof starts with retained provider bytes.  This module only owns
that first executable boundary: it invokes the pinned Longhouse engine parser,
retains the exact command/output, and returns the parser's emitted provider
facts.  Catalog and served-state assertions remain in the existing
``delegation_projection`` / HTTP lifecycle seams; callers must combine those
real results rather than certifying copied dictionaries.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCENARIO_ID = "background_fidelity_replay"
SCHEMA_VERSION = 1
STATUS_PASS = "pass"
STATUS_BLOCKED = "blocked"
STATUS_FAIL = "fail"


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _digest_matches(expected: object, observed: str) -> bool:
    if expected is None:
        return True
    return str(expected).lower().removeprefix("sha256:") == observed


def _source_section(capture: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(capture.get("source_capture") or capture.get("source"))


def _source_path(capture: Mapping[str, Any]) -> Path | None:
    source = _source_section(capture)
    direct = source.get("path") or capture.get("source_path")
    for candidate in (direct, source.get("retained_path"), source.get("capture_path")):
        if isinstance(candidate, str) and Path(candidate).is_file():
            return Path(candidate)
    artifacts = _rows(source.get("artifacts"))
    for artifact in artifacts:
        original = artifact.get("path")
        candidate = artifact.get("retained_path") or artifact.get("capture_path")
        if (
            isinstance(direct, str)
            and isinstance(original, str)
            and original == direct
            and isinstance(candidate, str)
            and Path(candidate).is_file()
        ):
            return Path(candidate)
        if artifact.get("is_source") is True and isinstance(candidate, str) and Path(candidate).is_file():
            return Path(candidate)
    return None


def _stage_source_bundle(package: Any, capture: Mapping[str, Any]) -> tuple[Path | None, dict[str, Any]]:
    """Restore retained source/sidecars under one explicit replay directory.

    Claude's parser discovers ``agent-*.meta.json`` beside a transcript by
    basename.  Native captures can retain those bytes after the original
    isolated profile has been removed, so replay must copy the exact bytes to a
    declared location rather than silently rewrite paths or invent IDs.
    """

    source = _source_section(capture)
    direct = source.get("path") or capture.get("source_path")
    artifacts = _rows(source.get("artifacts"))
    source_path = _source_path(capture)
    if not artifacts:
        return source_path, {"relocated": False, "entries": []}
    replay_root = package.path("raw", "background-source")
    replay_root.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    source_replay: Path | None = None
    source_original = str(direct) if isinstance(direct, str) else None
    source_expected = source.get("sha256")
    if source_expected is None and source_original is not None:
        source_expected = next(
            (artifact.get("sha256") or artifact.get("digest") for artifact in artifacts if artifact.get("path") == source_original),
            None,
        )
    for index, artifact in enumerate(artifacts):
        original = artifact.get("path")
        retained = artifact.get("retained_path") or artifact.get("capture_path") or original
        if not isinstance(retained, str) or not Path(retained).is_file():
            continue
        original_name = Path(str(original or retained)).name
        destination = replay_root / original_name
        observed = hashlib.sha256(Path(retained).read_bytes()).hexdigest()
        expected = artifact.get("sha256") or artifact.get("digest")
        if expected is not None and not _digest_matches(expected, observed):
            return None, {
                "relocated": True,
                "error": "background_sidecar_digest_mismatch",
                "entries": entries,
                "failed_entry": {
                    "original_path": original,
                    "retained_path": retained,
                    "sha256": observed,
                    "expected_sha256": expected,
                },
            }
        if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != observed:
            return None, {
                "relocated": True,
                "error": "background_sidecar_name_collision",
                "entries": entries,
                "failed_entry": {"original_path": original, "retained_path": retained},
            }
        if not destination.exists():
            shutil.copyfile(retained, destination)
        row = {
            "index": index,
            "original_path": original,
            "retained_path": retained,
            "replay_path": str(destination),
            "sha256": observed,
        }
        entries.append(row)
        if (
            (isinstance(original, str) and original == source_original)
            or (source_path is not None and Path(retained).resolve() == source_path.resolve())
            or (source_replay is None and artifact.get("is_source") is True)
        ):
            source_replay = destination
    if source_replay is None:
        if source_path is not None and source_path.is_file():
            source_replay = replay_root / source_path.name
            if not source_replay.exists():
                shutil.copyfile(source_path, source_replay)
        else:
            return None, {"relocated": True, "error": "background_source_missing", "entries": entries}
    receipt = {
        "relocated": True,
        "replay_root": str(replay_root),
        "source_original_path": source_original,
        "source_expected_sha256": source_expected,
        "source_replay_path": str(source_replay),
        "entries": entries,
    }
    package.write_json("raw/background-source-relocation.json", receipt)
    return source_replay, receipt


def _source_digest(
    capture: Mapping[str, Any],
    source_path: Path,
    *,
    expected_sha256: object = None,
) -> dict[str, Any]:
    source = _source_section(capture)
    expected: str | None = str(expected_sha256) if expected_sha256 is not None else None
    if expected is None and isinstance(source.get("sha256"), str):
        expected = source["sha256"]
    for artifact in _rows(source.get("artifacts")):
        if artifact.get("path") == str(source_path):
            expected = artifact.get("sha256") or artifact.get("digest")
            break
    observed = hashlib.sha256(source_path.read_bytes()).hexdigest()
    return {
        "path": str(source_path),
        "sha256": observed,
        "expected_sha256": expected,
        "matches": _digest_matches(expected, observed),
    }


def _parse_json_lines(stdout: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            rows.append(value)
    return rows


def run_engine_fact_replay(
    package: Any,
    *,
    capture: Mapping[str, Any],
    engine_binary: Path | None = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Run the real engine parser against a retained provider source file.

    The engine's ``--dump-facts`` stream is the authority for this stage.  A
    missing source, missing engine, parser crash, timeout, or digest mismatch
    is blocked/fail evidence and never a provider pass.
    """

    try:
        source_path, relocation = _stage_source_bundle(package, capture)
    except (OSError, ValueError) as exc:
        source_path = None
        relocation = {
            "relocated": False,
            "error": f"background_source_relocation_failed:{type(exc).__name__}",
        }
    binary = engine_binary
    if binary is None:
        configured = os.environ.get("LONGHOUSE_ENGINE_BIN")
        binary = Path(configured) if configured else None
    if source_path is None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "scenario": SCENARIO_ID,
            "status": STATUS_BLOCKED,
            "failure_code": str(relocation.get("error") or "background_source_missing"),
            "source_relocation": relocation,
            "assertions": {"retained_source_available": False, "engine_facts_produced": False},
        }
        package.write_json("assertions/background-engine-replay.json", payload)
        return payload
    try:
        digest = _source_digest(
            capture,
            source_path,
            expected_sha256=relocation.get("source_expected_sha256"),
        )
    except OSError as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "scenario": SCENARIO_ID,
            "status": STATUS_BLOCKED,
            "failure_code": "background_source_unreadable",
            "source": {"path": str(source_path)},
            "source_relocation": relocation,
            "message": f"{type(exc).__name__}: {exc}",
            "assertions": {"retained_source_available": False, "engine_facts_produced": False},
        }
        package.write_json("assertions/background-engine-replay.json", payload)
        return payload
    if not digest["matches"]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "scenario": SCENARIO_ID,
            "status": STATUS_FAIL,
            "failure_code": "background_source_digest_mismatch",
            "source": digest,
            "source_relocation": relocation,
            "assertions": {"retained_source_available": False, "engine_facts_produced": False},
        }
        package.write_json("assertions/background-engine-replay.json", payload)
        return payload
    if binary is None or not binary.is_file() or not os.access(binary, os.X_OK):
        payload = {
            "schema_version": SCHEMA_VERSION,
            "scenario": SCENARIO_ID,
            "status": STATUS_BLOCKED,
            "failure_code": "background_engine_missing",
            "source": digest,
            "source_relocation": relocation,
            "assertions": {"retained_source_available": True, "engine_facts_produced": False},
        }
        package.write_json("assertions/background-engine-replay.json", payload)
        return payload

    argv = [str(binary), "parse", "--dump-facts", str(source_path)]
    try:
        completed = subprocess.run(argv, text=True, capture_output=True, check=False, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "scenario": SCENARIO_ID,
            "status": STATUS_BLOCKED,
            "failure_code": "background_engine_timeout",
            "source": digest,
            "source_relocation": relocation,
            "command": {"argv": argv, "timeout_seconds": timeout_seconds, "stdout": exc.stdout or "", "stderr": exc.stderr or ""},
            "assertions": {"retained_source_available": True, "engine_facts_produced": False},
        }
        package.write_json("assertions/background-engine-replay.json", payload)
        return payload
    except OSError as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "scenario": SCENARIO_ID,
            "status": STATUS_BLOCKED,
            "failure_code": "background_engine_start_failed",
            "source": digest,
            "source_relocation": relocation,
            "command": {"argv": argv},
            "message": f"{type(exc).__name__}: {exc}",
            "assertions": {"retained_source_available": True, "engine_facts_produced": False},
        }
        package.write_json("assertions/background-engine-replay.json", payload)
        return payload
    package.write_text("events/engine-provider-facts.jsonl", completed.stdout)
    command = {
        "argv": argv,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    package.write_json("raw/engine-fact-command.json", command)
    if completed.returncode != 0:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "scenario": SCENARIO_ID,
            "status": STATUS_BLOCKED,
            "failure_code": "background_engine_parse_failed",
            "source": digest,
            "source_relocation": relocation,
            "command": command,
            "assertions": {"retained_source_available": True, "engine_facts_produced": False},
        }
        package.write_json("assertions/background-engine-replay.json", payload)
        return payload
    try:
        facts = _parse_json_lines(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "scenario": SCENARIO_ID,
            "status": STATUS_FAIL,
            "failure_code": "background_engine_fact_stream_invalid",
            "source": digest,
            "source_relocation": relocation,
            "command": command,
            "message": f"{type(exc).__name__}: {exc}",
            "assertions": {"retained_source_available": True, "engine_facts_produced": False},
        }
        package.write_json("assertions/background-engine-replay.json", payload)
        return payload
    expected_kinds = {str(item) for item in capture.get("expected_engine_fact_kinds", ()) if isinstance(item, str) and item}
    actual_kinds = {str(row.get("kind")) for row in facts if row.get("kind")}
    facts_ok = bool(facts) and (not expected_kinds or expected_kinds <= actual_kinds)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "scenario": SCENARIO_ID,
        "status": STATUS_PASS if facts_ok else STATUS_FAIL,
        "failure_code": None if facts_ok else "background_engine_expected_facts_missing",
        "source": digest,
        "source_relocation": relocation,
        "command": command,
        "fact_count": len(facts),
        "fact_kinds": sorted(actual_kinds),
        "expected_fact_kinds": sorted(expected_kinds),
        # The engine's serialized facts are the only input to the subsequent
        # catalog adapter. Keep them in the result as source-derived evidence;
        # callers must not synthesize a registry when this list is empty.
        "facts": facts,
        "assertions": {
            "retained_source_available": True,
            "engine_facts_produced": facts_ok,
        },
    }
    package.write_json("assertions/background-engine-replay.json", payload)
    return payload


def run_claude_lifecycle_hook_replay(
    package: Any,
    *,
    capture: Mapping[str, Any],
    engine_binary: Path | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Run retained Claude hook stdin through the native engine entrypoint.

    The hook is local-only: its observable output is the exact outbox payload
    under an isolated LONGHOUSE_HOME. No copied ``delegation`` object is
    accepted as proof; callers must post the emitted payload through normal
    runtime ingress before serving it.
    """

    try:
        source_path, relocation = _stage_source_bundle(package, capture)
    except (OSError, ValueError) as exc:
        source_path = None
        relocation = {
            "relocated": False,
            "error": f"background_source_relocation_failed:{type(exc).__name__}",
        }
    if source_path is None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "scenario": SCENARIO_ID,
            "status": STATUS_BLOCKED,
            "failure_code": str(relocation.get("error") or "background_source_missing"),
            "source_relocation": relocation,
            "assertions": {"retained_source_available": False, "hook_outbox_produced": False},
        }
        package.write_json("assertions/background-hook-replay.json", payload)
        return payload
    try:
        source_bytes = source_path.read_bytes()
    except OSError as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "scenario": SCENARIO_ID,
            "status": STATUS_BLOCKED,
            "failure_code": "background_source_unreadable",
            "source_relocation": relocation,
            "message": f"{type(exc).__name__}: {exc}",
            "assertions": {"retained_source_available": False, "hook_outbox_produced": False},
        }
        package.write_json("assertions/background-hook-replay.json", payload)
        return payload
    digest = _source_digest(
        capture,
        source_path,
        expected_sha256=relocation.get("source_expected_sha256"),
    )
    if not digest["matches"]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "scenario": SCENARIO_ID,
            "status": STATUS_FAIL,
            "failure_code": "background_source_digest_mismatch",
            "source": digest,
            "source_relocation": relocation,
            "assertions": {"retained_source_available": False, "hook_outbox_produced": False},
        }
        package.write_json("assertions/background-hook-replay.json", payload)
        return payload
    binary = engine_binary
    if binary is None:
        configured = os.environ.get("LONGHOUSE_ENGINE_BIN")
        binary = Path(configured) if configured else None
    if binary is None or not binary.is_file() or not os.access(binary, os.X_OK):
        payload = {
            "schema_version": SCHEMA_VERSION,
            "scenario": SCENARIO_ID,
            "status": STATUS_BLOCKED,
            "failure_code": "background_engine_missing",
            "source": digest,
            "source_relocation": relocation,
            "assertions": {"retained_source_available": True, "hook_outbox_produced": False},
        }
        package.write_json("assertions/background-hook-replay.json", payload)
        return payload

    home = package.path("raw", "claude-hook-home")
    home.mkdir(parents=True, exist_ok=True)
    argv = [str(binary), "claude-lifecycle-hook"]
    env = {**os.environ, "LONGHOUSE_HOME": str(home)}
    try:
        completed = subprocess.run(
            argv,
            input=source_bytes,
            capture_output=True,
            env=env,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "scenario": SCENARIO_ID,
            "status": STATUS_BLOCKED,
            "failure_code": "background_hook_execution_failed",
            "source": digest,
            "source_relocation": relocation,
            "command": {"argv": argv, "returncode": None},
            "message": f"{type(exc).__name__}: {exc}",
            "assertions": {"retained_source_available": True, "hook_outbox_produced": False},
        }
        package.write_json("assertions/background-hook-replay.json", payload)
        return payload

    outbox_root = home / "agent" / "outbox"
    outbox = sorted(outbox_root.glob("*.json")) if outbox_root.is_dir() else []
    emitted: list[dict[str, Any]] = []
    for path in outbox:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, Mapping):
            emitted.append(dict(value))
    command = {
        "argv": argv,
        "returncode": completed.returncode,
        "stdout": completed.stdout.decode(errors="replace") if isinstance(completed.stdout, bytes) else completed.stdout,
        "stderr": completed.stderr.decode(errors="replace") if isinstance(completed.stderr, bytes) else completed.stderr,
    }
    package.write_json("raw/background-hook-command.json", command)
    package.write_json("events/claude-hook-outbox.json", {"events": emitted})
    passed = completed.returncode == 0 and bool(emitted)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "scenario": SCENARIO_ID,
        "status": STATUS_PASS if passed else STATUS_FAIL,
        "failure_code": None if passed else "background_hook_outbox_missing",
        "source": digest,
        "source_relocation": relocation,
        "command": command,
        "outbox_count": len(emitted),
        "outbox_events": emitted,
        "assertions": {
            "retained_source_available": True,
            "hook_outbox_produced": passed,
        },
    }
    package.write_json("assertions/background-hook-replay.json", payload)
    return payload


def combine_real_replay_results(
    *,
    parser_result: Mapping[str, Any],
    catalog_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Join independent parser and catalog/projector receipts without copying proof.

    ``catalog_result`` must come from the existing real delegation runner or
    HTTP lifecycle path.  This function only combines their already-executed
    statuses; it never accepts a caller-provided ``served`` object as proof.
    """

    parser_status = str(parser_result.get("status") or "")
    catalog_status = str(catalog_result.get("status") or "")
    parser_passed = parser_status == STATUS_PASS
    catalog_passed = catalog_status == STATUS_PASS
    assertions = {
        "engine_facts_produced": parser_passed and parser_result.get("assertions", {}).get("engine_facts_produced") is True,
        "catalog_served_path_passed": catalog_passed,
    }
    passed = all(assertions.values())
    failed = parser_status == STATUS_FAIL or catalog_status == STATUS_FAIL
    status = STATUS_FAIL if failed else STATUS_PASS if passed else STATUS_BLOCKED
    failure_code = None
    if status != STATUS_PASS:
        failure_code = str(parser_result.get("failure_code") or catalog_result.get("failure_code") or "background_real_replay_unproven")
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "provider_background_fidelity_result",
        "scenario": SCENARIO_ID,
        "status": status,
        "failure_code": failure_code,
        "assertions": assertions,
        "parser": dict(parser_result),
        "catalog": dict(catalog_result),
        "operation_evidence": {
            "background_fidelity": {
                "status": status,
                "level": "hermetic" if status == STATUS_PASS else "none",
                "canary": SCENARIO_ID,
                "failure_code": failure_code,
            }
        },
    }


__all__ = [
    "SCENARIO_ID",
    "combine_real_replay_results",
    "run_engine_fact_replay",
    "run_claude_lifecycle_hook_replay",
]
