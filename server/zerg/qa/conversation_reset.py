"""Provider-neutral conversation-reset observations and deterministic oracles."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from zerg.services.longhouse_paths import resolve_longhouse_home

RESET_SCHEMA_VERSION = 1
RESET_SCENARIO = "conversation_reset"
RESET_RESUME_SCENARIO = "conversation_reset_resume"
IDENTITY_TRANSITIONS = ("rotated", "reused", "unobserved")
IDENTITY_ALLOCATIONS = ("eager", "lazy", "not_applicable", "unobserved")


def reset_artifact_env(provider: str) -> str:
    return f"LONGHOUSE_{provider.upper()}_CONVERSATION_RESET_ARTIFACT"


def produce_live_reset_artifact(provider: str, binary: Path, artifact_root: Path) -> Path | None:
    """Run a provider-owned canary and return its neutral observation artifact."""

    modules = {
        "claude": "zerg.qa.claude_conversation_reset",
        "codex": "zerg.qa.codex_conversation_reset",
        "opencode": "zerg.qa.opencode_conversation_reset",
        "antigravity": "zerg.qa.antigravity_conversation_reset",
        "cursor": "zerg.qa.cursor_helm_gate0",
    }
    try:
        module = modules[provider]
    except KeyError:
        return None
    command = [sys.executable, "-m", module, "--artifact-root", str(artifact_root)]
    if provider == "cursor":
        command.extend(("--cursor-bin", str(binary), "--conversation-reset-only"))
    else:
        command.extend(("--provider-bin", str(binary)))
    if provider == "antigravity":
        session_id = str(os.environ.get("LONGHOUSE_ANTIGRAVITY_RESET_SESSION_ID") or "").strip()
        if not session_id:
            return None
        command.extend(("--longhouse-session-id", session_id))
    artifact_root.mkdir(parents=True, exist_ok=True)
    existing = set(artifact_root.glob("*/conversation-reset-observation.json"))
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    (artifact_root / "producer.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (artifact_root / "producer.stderr.log").write_text(completed.stderr, encoding="utf-8")
    observations = sorted(set(artifact_root.glob("*/conversation-reset-observation.json")) - existing)
    return observations[-1].resolve() if observations else None


def marker_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _shipper_database_path(shipper_db: Path | None) -> Path:
    if shipper_db is not None:
        return shipper_db.expanduser()
    return resolve_longhouse_home() / "agent" / "longhouse-shipper.db"


def longhouse_source_binding(
    provider: str,
    provider_session_id: str,
    *,
    shipper_db: Path | None = None,
) -> str | None:
    shipper_db = _shipper_database_path(shipper_db)
    if not shipper_db.is_file():
        return None
    try:
        with closing(sqlite3.connect(shipper_db)) as conn:
            row = conn.execute(
                """
                SELECT bound_session_id
                FROM source_epoch_registry
                WHERE provider = ? AND provider_session_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (provider, provider_session_id),
            ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]).strip() if row and row[0] else None


def longhouse_provider_aliases(
    provider: str,
    session_id: str,
    *,
    shipper_db: Path | None = None,
) -> tuple[str, ...]:
    """Return provider identities durably bound to one local Longhouse session."""

    shipper_db = _shipper_database_path(shipper_db)
    if not shipper_db.is_file():
        return ()
    try:
        with closing(sqlite3.connect(shipper_db)) as conn:
            rows = conn.execute(
                """
                SELECT provider_session_id
                FROM source_epoch_registry
                WHERE provider = ?
                  AND bound_session_id = ?
                  AND provider_session_id IS NOT NULL
                  AND provider_session_id != ''
                ORDER BY created_at, source_epoch
                """,
                (provider, session_id),
            ).fetchall()
    except sqlite3.Error:
        return ()
    return tuple(dict.fromkeys(str(row[0]).strip() for row in rows if row and str(row[0]).strip()))


def tail_sequence(payload: Mapping[str, Any], marker_a: str, reset_command: str, marker_b: str) -> dict[str, Any]:
    """Measure marker/reset order from individual archived events.

    Event-by-event matching avoids accidental ordering from JSON object metadata.
    """

    events = payload.get("events")
    rows = events if isinstance(events, list) else []
    rendered = [json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows]

    def first(value: str, *, after: int = -1) -> int:
        return next((index for index, row in enumerate(rendered) if index > after and value in row), -1)

    marker_a_index = first(marker_a)
    reset_index = next(
        (
            index
            for index, row in enumerate(rows)
            if index > marker_a_index
            and (
                reset_command in rendered[index]
                or (isinstance(row, Mapping) and row.get("role") == "system" and row.get("content") == "Conversation reset")
            )
        ),
        -1,
    )
    marker_b_index = first(marker_b, after=max(marker_a_index, reset_index))
    ordered = 0 <= marker_a_index < reset_index < marker_b_index
    return {
        "pre_reset_raw_preserved": marker_a_index >= 0,
        "post_reset_raw_preserved": marker_b_index >= 0,
        "reset_boundary_observable": reset_index >= 0,
        "tail_marker_order": [marker_digest(marker_a), "reset", marker_digest(marker_b)] if ordered else [],
        "event_indices": {
            "marker_a": marker_a_index,
            "reset": reset_index,
            "marker_b": marker_b_index,
        },
    }


def execution_summary(observation: Mapping[str, Any] | None, *, observation_path: Path, terminal_path: Path) -> dict[str, Any]:
    evaluation = evaluate_reset_observation(observation) if observation else None
    return {
        "status": "completed" if observation else "failed",
        "execution_status": "completed" if observation else "failed",
        "semantic_status": evaluation.get("status") if evaluation else "not_evaluated",
        "failed_assertions": evaluation.get("failed_assertions", []) if evaluation else [],
        "observation_path": str(observation_path),
        "terminal_path": str(terminal_path),
    }


def observation_exit_code(observation: Mapping[str, Any]) -> int:
    """Make direct canary targets fail when their semantic assertions fail."""

    return 0 if evaluate_reset_observation(observation)["status"] == "pass" else 1


def classify_identity_transition(before: str | None, after: str | None) -> str:
    if not before or not after:
        return "unobserved"
    return "reused" if before == after else "rotated"


def generated_fake_observation(
    provider: str,
    *,
    allocation: str = "eager",
    transition: str = "rotated",
) -> dict[str, Any]:
    """Build comparable reset evidence for the verified generated-fake lane."""

    if allocation not in IDENTITY_ALLOCATIONS:
        raise ValueError(f"unsupported identity allocation: {allocation}")
    if transition not in IDENTITY_TRANSITIONS:
        raise ValueError(f"unsupported identity transition: {transition}")
    before_id = f"{provider}-conversation-before"
    after_id = None
    if transition == "rotated":
        after_id = f"{provider}-conversation-after"
    elif transition == "reused":
        after_id = before_id
    marker_a = f"LONGHOUSE_RESET_{provider.upper()}_A"
    marker_b = f"LONGHOUSE_RESET_{provider.upper()}_B"
    before_source = f"fake://{provider}/{before_id}"
    after_source = before_source if transition == "reused" else f"fake://{provider}/{after_id or 'unobserved'}"
    return {
        "schema_version": RESET_SCHEMA_VERSION,
        "scenario": RESET_SCENARIO,
        "provider": provider,
        "evidence_class": "hermetic",
        "reset_command": "/new" if provider == "opencode" else "/clear",
        "reset_command_accepted": True,
        "identity_transition": transition,
        "identity_allocation": allocation,
        "before": {
            "provider_session_id": before_id,
            "longhouse_session_id": f"longhouse-{provider}-managed",
            "provider_process_id": f"fake-{provider}-pid",
            "run_id": f"fake-{provider}-run",
            "raw_source_ids": [before_source],
            "marker_digest": marker_digest(marker_a),
        },
        "after": {
            "provider_session_id": after_id,
            "longhouse_session_id": f"longhouse-{provider}-managed",
            "provider_process_id": f"fake-{provider}-pid",
            "run_id": f"fake-{provider}-run",
            "raw_source_ids": [after_source],
            "marker_digest": marker_digest(marker_b),
        },
        "provider_transition": {
            "pre_reset_history_retained": True,
            "post_reset_turn_bound_to_active_identity": True,
            "pre_reset_messages_not_copied": True,
        },
        "archive": {
            "pre_reset_raw_preserved": True,
            "post_reset_raw_preserved": True,
            "reset_boundary_observable": True,
            "tail_marker_order": [marker_digest(marker_a), "reset", marker_digest(marker_b)],
            "source_identity_preserved": True,
        },
        "longhouse": {
            "provider_alias_ids": [after_id or before_id],
            "timeline_session_ids": [f"longhouse-{provider}-managed"],
            "provider_alias_matches_before": transition == "reused",
            "provider_alias_matches_after": True,
        },
    }


def evaluate_reset_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    before = observation.get("before") if isinstance(observation.get("before"), Mapping) else {}
    after = observation.get("after") if isinstance(observation.get("after"), Mapping) else {}
    provider = observation.get("provider_transition") if isinstance(observation.get("provider_transition"), Mapping) else {}
    archive = observation.get("archive") if isinstance(observation.get("archive"), Mapping) else {}
    longhouse = observation.get("longhouse") if isinstance(observation.get("longhouse"), Mapping) else {}
    observed_transition = classify_identity_transition(
        _optional_text(before.get("provider_session_id")),
        _optional_text(after.get("provider_session_id")),
    )
    declared_transition = str(observation.get("identity_transition") or observed_transition)
    transition_consistent = declared_transition == observed_transition
    process_before = _optional_text(before.get("provider_process_id"))
    process_after = _optional_text(after.get("provider_process_id"))
    assertions = {
        "reset_command_accepted_quiescent": observation.get("reset_command_accepted") is True,
        "provider_identity_transition_observed": observed_transition in {"rotated", "reused"} and transition_consistent,
        "pre_reset_history_retained": provider.get("pre_reset_history_retained") is True,
        "post_reset_turn_bound_to_active_identity": provider.get("post_reset_turn_bound_to_active_identity") is True,
        "pre_reset_messages_not_copied_to_new_history": provider.get("pre_reset_messages_not_copied") is True,
        "reset_kept_cli_invocation": bool(process_before and process_after and process_before == process_after),
        "pre_reset_raw_preserved": archive.get("pre_reset_raw_preserved") is True,
        "post_reset_raw_preserved": archive.get("post_reset_raw_preserved") is True,
        "reset_boundary_observable": archive.get("reset_boundary_observable") is True,
        "tail_order_conserved": _tail_order_conserved(archive.get("tail_marker_order")),
        "source_identity_preserved": archive.get("source_identity_preserved") is True,
        "longhouse_alias_targets_active_identity": bool(
            _optional_text(after.get("provider_session_id"))
            and _optional_text(after.get("provider_session_id")) in (longhouse.get("provider_alias_ids") or [])
            and longhouse.get("provider_alias_matches_after") is True
        ),
    }
    if "source_binding_matches" in longhouse:
        assertions["longhouse_source_bound_to_managed_session"] = longhouse.get("source_binding_matches") is True
    failed = sorted(name for name, passed in assertions.items() if not passed)
    return {
        "status": "pass" if not failed else "fail",
        "failure_code": None if not failed else "conversation_reset_assertion_failed",
        "assertions": assertions,
        "failed_assertions": failed,
        "observed_identity_transition": observed_transition,
        "declared_identity_transition": declared_transition,
    }


def evaluate_resume_observation(
    reset_observation: Mapping[str, Any],
    resume_observation: Mapping[str, Any],
) -> dict[str, Any]:
    after = reset_observation.get("after") if isinstance(reset_observation.get("after"), Mapping) else {}
    expected_id = _optional_text(after.get("provider_session_id"))
    requested_id = _optional_text(resume_observation.get("longhouse_requested_provider_id"))
    opened_id = _optional_text(resume_observation.get("provider_opened_id"))
    assertions = {
        "resume_target_observed": bool(requested_id and opened_id),
        "resume_requested_post_reset_identity": bool(expected_id and requested_id == expected_id),
        "resume_opened_post_reset_identity": bool(expected_id and opened_id == expected_id),
    }
    failed = sorted(name for name, passed in assertions.items() if not passed)
    return {
        "status": "pass" if not failed else "fail",
        "failure_code": None if not failed else "conversation_reset_resume_assertion_failed",
        "assertions": assertions,
        "failed_assertions": failed,
        "expected_provider_id": expected_id,
        "requested_provider_id": requested_id,
        "opened_provider_id": opened_id,
    }


def load_reset_observation(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != RESET_SCHEMA_VERSION:
        raise ValueError(f"unsupported conversation-reset observation: {path}")
    return payload


def consume_live_reset_artifact(
    adapter: Any,
    package: Any,
    *,
    provider: str,
) -> dict[str, Any] | None:
    """Verify and project a provider-owned live reset observation.

    Live producers remain provider-specific. This consumer is deliberately
    shared so exact-binary binding, the neutral oracle, and Longhouse ingest
    assertions cannot drift between adapter columns.
    """

    from zerg.qa.universal_agent_harness import STATUS_FAIL
    from zerg.qa.universal_agent_harness import STATUS_PASS

    artifact_value = str(os.environ.get(reset_artifact_env(provider)) or "").strip()
    if not artifact_value:
        return None
    artifact_path = Path(artifact_value).expanduser()
    try:
        observation = load_reset_observation(artifact_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        payload = {
            "status": STATUS_FAIL,
            "scenario": RESET_SCENARIO,
            "failure_code": "conversation_reset_artifact_invalid",
            "message": f"{type(exc).__name__}: {exc}",
        }
        package.write_json("assertions/conversation_reset.json", payload)
        return payload

    if observation.get("provider") != provider or observation.get("evidence_class") != "live_token":
        payload = {
            "status": STATUS_FAIL,
            "scenario": RESET_SCENARIO,
            "failure_code": "conversation_reset_artifact_invalid",
            "message": f"Expected provider={provider} live_token conversation-reset evidence.",
        }
        package.write_json("assertions/conversation_reset.json", payload)
        return payload

    binary, binary_error = adapter._require_binary(package, RESET_SCENARIO)
    if binary_error is not None:
        return binary_error
    assert binary is not None
    resolved_binary = binary.expanduser().resolve(strict=True)
    executable_identity = f"sha256:{_file_digest(resolved_binary)}"
    probe = adapter.probe(package)
    probe_version = str(probe.get("version") or "").strip()
    observed_version = str(observation.get("provider_version") or "").strip()
    observed_identity = str(observation.get("provider_executable_identity") or "").strip()
    identity_failure: tuple[str, str] | None = None
    if probe.get("status") != STATUS_PASS or not probe_version:
        identity_failure = ("conversation_reset_binary_probe_failed", "Provider binary probe failed.")
    elif probe_version != observed_version and not probe_version.startswith(f"{observed_version} "):
        identity_failure = (
            "conversation_reset_version_mismatch",
            f"Reset artifact proved {observed_version!r}; declared binary reports {probe_version!r}.",
        )
    elif executable_identity != observed_identity:
        identity_failure = (
            "conversation_reset_identity_mismatch",
            "Reset artifact did not prove the exact declared provider executable.",
        )

    evaluation = evaluate_reset_observation(observation)
    passed = identity_failure is None and evaluation["status"] == STATUS_PASS
    before = observation.get("before") if isinstance(observation.get("before"), Mapping) else {}
    after = observation.get("after") if isinstance(observation.get("after"), Mapping) else {}
    longhouse_session_id = str(after.get("longhouse_session_id") or before.get("longhouse_session_id") or adapter._session_id(package))
    evidence = {
        RESET_SCENARIO: {
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "level": "live_token",
            "canary": f"{provider}_conversation_reset",
            "failure_code": identity_failure[0] if identity_failure else evaluation.get("failure_code"),
        }
    }
    raw_events = [
        {
            "type": "conversation_reset",
            "role": "system",
            "text": f"{provider} conversation reset observation",
            "provider_session_id": str(after.get("provider_session_id") or before.get("provider_session_id") or ""),
            "longhouse_session_id": longhouse_session_id,
            "before_provider_session_id": before.get("provider_session_id"),
            "after_provider_session_id": after.get("provider_session_id"),
            "source_canary": f"{provider}_conversation_reset",
            "evidence_origin": "provider_live_canary",
        }
    ]
    projection, evidence, db_ingest = adapter._project_ingest_and_merge(
        package,
        operation_evidence=evidence,
        raw_events=raw_events,
        provider_session_id=longhouse_session_id,
    )
    if db_ingest.get("status") != STATUS_PASS:
        passed = False
    copied_observation = package.write_json("observations/conversation_reset.json", observation)
    payload = {
        **projection,
        **evaluation,
        "status": STATUS_PASS if passed else STATUS_FAIL,
        "scenario": RESET_SCENARIO,
        "artifact_path": str(artifact_path.resolve(strict=False)),
        "observation_path": str(copied_observation),
        "identity_transition": observation.get("identity_transition"),
        "identity_allocation": observation.get("identity_allocation"),
        "synthetic": False,
        "operation_evidence": evidence,
        "longhouse_ingest": adapter._longhouse_ingest_block(db_ingest),
    }
    if identity_failure:
        payload["failure_code"], payload["message"] = identity_failure
    elif db_ingest.get("status") != STATUS_PASS:
        payload["failure_code"] = db_ingest.get("failure_code") or "conversation_reset_db_ingest_failed"
        payload["message"] = "Conversation-reset evidence did not pass Longhouse DB ingest assertions."
    package.write_json("assertions/conversation_reset.json", payload)
    return payload


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _tail_order_conserved(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != 3:
        return False
    return bool(value[0] and value[1] == "reset" and value[2] and value[0] != value[2])
