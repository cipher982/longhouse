"""Provider-neutral conversation-reset observations and deterministic oracles."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

RESET_SCHEMA_VERSION = 1
RESET_SCENARIO = "conversation_reset"
RESET_RESUME_SCENARIO = "conversation_reset_resume"
IDENTITY_TRANSITIONS = ("rotated", "reused", "unobserved")
IDENTITY_ALLOCATIONS = ("eager", "lazy", "not_applicable", "unobserved")


def marker_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
            "provider_alias_ids": [before_id],
            "timeline_session_ids": [f"longhouse-{provider}-managed"],
        },
    }


def evaluate_reset_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    before = observation.get("before") if isinstance(observation.get("before"), Mapping) else {}
    after = observation.get("after") if isinstance(observation.get("after"), Mapping) else {}
    provider = observation.get("provider_transition") if isinstance(observation.get("provider_transition"), Mapping) else {}
    archive = observation.get("archive") if isinstance(observation.get("archive"), Mapping) else {}
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
    }
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


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _tail_order_conserved(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != 3:
        return False
    return bool(value[0] and value[1] == "reset" and value[2] and value[0] != value[2])
