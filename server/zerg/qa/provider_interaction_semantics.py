"""Shared oracle and hermetic observations for provider-native controls."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from zerg.services.managed_provider_contracts import contract_for_provider
from zerg.services.provider_interaction_semantics import INTERACTION_LOCAL_CONTROL_OUTPUT
from zerg.services.provider_interaction_semantics import classify_provider_interaction
from zerg.services.provider_interaction_semantics import interaction_contract_snapshot

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_BLOCKED = "blocked"


def _claude_command_content(command: str) -> str:
    command_name, _, args = command.partition(" ")
    return "\n".join(
        (
            f"<local-command-caveat>Caveat: {command_name} is a local command.</local-command-caveat>",
            f"<command-name>{command_name}</command-name>",
            f"<command-message>{command_name.removeprefix('/')}</command-message>",
            f"<command-args>{args}</command-args>",
        )
    )


def _synthetic_raw_event(provider: str, probe: Mapping[str, Any], *, output: bool = False) -> dict[str, Any]:
    command = str((probe.get("input_sequence") or [""])[0])
    kind = str(probe.get("expected_interaction_kind") or "local_control")
    if provider == "claude":
        content = (
            "<local-command-stdout>Set the requested local state.</local-command-stdout>" if output else _claude_command_content(command)
        )
        return {
            "type": "user",
            "message": {"role": "user", "content": content},
            "content_text": content,
            "isMeta": True,
            "longhouse_interaction_kind": "local_control_output" if output else kind,
            "longhouse_changes_provider_state": False if output else probe.get("changes_provider_state"),
        }
    markers = probe.get("raw_output_markers") if output else probe.get("raw_markers")
    marker_text = " ".join(str(marker) for marker in markers or ()).strip()
    return {
        "type": "user",
        "role": "user",
        "content_text": marker_text or command or "provider interaction acknowledgement",
        "longhouse_interaction_kind": INTERACTION_LOCAL_CONTROL_OUTPUT if output else kind,
        "longhouse_changes_provider_state": False if output else probe.get("changes_provider_state"),
        "provider_probe_id": probe.get("probe_id"),
    }


def generated_fake_observation(provider: str) -> dict[str, Any]:
    """Build a provider-shaped, no-token observation for CI and local tests."""

    probes = interaction_contract_snapshot(provider)
    raw_events: list[dict[str, Any]] = []
    probe_observations: list[dict[str, Any]] = []
    for probe in probes:
        disposition = str(probe.get("disposition") or "")
        if disposition in {"policy_disabled", "upstream_absent"}:
            probe_observations.append(
                {
                    "probe_id": probe["probe_id"],
                    "disposition": disposition,
                    "status": STATUS_NOT_APPLICABLE,
                    "raw_events": [],
                }
            )
            continue
        control = _synthetic_raw_event(provider, probe)
        output = _synthetic_raw_event(provider, probe, output=True)
        raw_events.extend((control, output))
        probe_observations.append(
            {
                "probe_id": probe["probe_id"],
                "disposition": disposition,
                "status": "observed",
                "raw_events": [control, output],
            }
        )

    marker = f"LONGHOUSE_INTERACTION_SEMANTICS_{provider.upper()}_MARKER"
    ordinary_prompt = {
        "type": "user",
        "role": "user",
        "content_text": f"Reply with exactly {marker}",
        "provider_probe_id": "ordinary_marker_prompt",
    }
    unknown_slash = {
        "type": "user",
        "role": "user",
        "content_text": "/custom-command-that-provider-may-send-to-model",
        "provider_probe_id": "unknown_slash_prompt",
    }
    raw_events.extend((ordinary_prompt, unknown_slash))
    return {
        "schema_version": 1,
        "artifact_kind": "provider_interaction_semantics_observation",
        "provider": provider,
        "evidence_class": "hermetic",
        "synthetic": True,
        "probes": probe_observations,
        "raw_events": raw_events,
        "ordinary_marker": marker,
        "unknown_slash_probe": unknown_slash["content_text"],
    }


def _event_semantics(provider: str, event: Mapping[str, Any], *, source_surface: str = "helm_tui") -> dict[str, Any]:
    return classify_provider_interaction(
        provider,
        role=str(event.get("role") or event.get("type") or ""),
        content_text=str(event.get("content_text") or event.get("text") or ""),
        raw_json=event.get("raw_json") or event,
        source_surface=source_surface,
    )


def _event_evidence_text(event: Mapping[str, Any]) -> str:
    """Serialize one raw event for literal marker assertions."""

    return json.dumps(dict(event), ensure_ascii=False, sort_keys=True, default=str)


def evaluate_observation(provider: str, observation: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate raw probe evidence without asking an LLM to classify it."""

    contract = contract_for_provider(provider)
    if contract is None:
        return {"status": STATUS_FAIL, "failure_code": "provider_contract_missing"}
    declared = {probe.probe_id: probe for probe in contract.interaction_probes}
    observed = observation.get("probes")
    if not isinstance(observed, list):
        return {
            "status": STATUS_BLOCKED,
            "failure_code": "interaction_probe_observations_missing",
            "message": "The provider artifact did not contain per-probe raw observations.",
        }

    assertion_rows: list[dict[str, Any]] = []
    observed_by_id: dict[str, Mapping[str, Any]] = {}
    for row in observed:
        if not isinstance(row, Mapping):
            assertion_rows.append({"status": STATUS_FAIL, "failure_code": "interaction_probe_row_invalid"})
            continue
        probe_id = str(row.get("probe_id") or "")
        if probe_id in observed_by_id:
            assertion_rows.append({"probe_id": probe_id, "status": STATUS_FAIL, "failure_code": "interaction_probe_duplicate"})
            continue
        observed_by_id[probe_id] = row
        probe = declared.get(probe_id)
        if probe is None:
            assertion_rows.append({"probe_id": probe_id, "status": STATUS_FAIL, "failure_code": "interaction_probe_not_declared"})
            continue
        if probe.disposition in {"policy_disabled", "upstream_absent"}:
            assertion_rows.append(
                {
                    "probe_id": probe_id,
                    "status": STATUS_NOT_APPLICABLE,
                    "disposition": probe.disposition,
                }
            )
            continue
        events = row.get("raw_events")
        if not isinstance(events, list) or not events:
            assertion_rows.append(
                {
                    "probe_id": probe_id,
                    "status": STATUS_BLOCKED,
                    "failure_code": str(row.get("failure_code") or "interaction_raw_evidence_missing"),
                }
            )
            continue
        semantic_rows = [_event_semantics(provider, event) for event in events if isinstance(event, Mapping)]
        event_rows = [event for event in events if isinstance(event, Mapping)]
        first = semantic_rows[0] if semantic_rows else {}
        evidence_text = "\n".join(_event_evidence_text(event) for event in event_rows)
        output_text = "\n".join(
            _event_evidence_text(event)
            for event, semantics in zip(event_rows, semantic_rows, strict=False)
            if semantics.get("interaction_kind") == INTERACTION_LOCAL_CONTROL_OUTPUT
        )
        assertions = {
            "expected_kind": first.get("interaction_kind") == probe.expected_interaction_kind,
            "control_not_title_eligible": first.get("title_eligible") is probe.expected_title_eligibility,
            "control_not_user_message": first.get("counts_as_user_message") is False,
            "expected_model_turn": first.get("starts_model_turn") is probe.expected_model_turn,
            "expected_state_change": first.get("changes_provider_state") is probe.changes_provider_state
            if probe.changes_provider_state is not None
            else True,
            "raw_markers_present": all(marker in evidence_text for marker in probe.raw_markers),
            "raw_output_markers_present": all(marker in output_text for marker in probe.raw_output_markers),
        }
        status = STATUS_PASS if all(assertions.values()) else STATUS_FAIL
        assertion_rows.append(
            {
                "probe_id": probe_id,
                "status": status,
                "disposition": probe.disposition,
                "assertions": assertions,
                "semantic_events": semantic_rows,
            }
        )

    for probe in contract.interaction_probes:
        if probe.probe_id in observed_by_id:
            continue
        if probe.disposition in {"policy_disabled", "upstream_absent"}:
            assertion_rows.append(
                {
                    "probe_id": probe.probe_id,
                    "status": STATUS_NOT_APPLICABLE,
                    "disposition": probe.disposition,
                }
            )
        else:
            assertion_rows.append(
                {
                    "probe_id": probe.probe_id,
                    "status": STATUS_BLOCKED,
                    "disposition": probe.disposition,
                    "failure_code": "interaction_probe_not_observed",
                }
            )

    raw_events = observation.get("raw_events")
    raw_events = raw_events if isinstance(raw_events, list) else []
    marker = str(observation.get("ordinary_marker") or "")
    marker_event = next(
        (
            event
            for event in raw_events
            if isinstance(event, Mapping) and marker and marker in str(event.get("content_text") or event.get("text") or "")
        ),
        None,
    )
    marker_semantics = _event_semantics(provider, marker_event) if isinstance(marker_event, Mapping) else {}
    unknown_slash = str(observation.get("unknown_slash_probe") or "")
    unknown_event = next(
        (
            event
            for event in raw_events
            if isinstance(event, Mapping) and unknown_slash and unknown_slash == str(event.get("content_text") or event.get("text") or "")
        ),
        None,
    )
    unknown_semantics = _event_semantics(provider, unknown_event) if isinstance(unknown_event, Mapping) else {}
    boundary_assertions = {
        "ordinary_marker_is_title_eligible": marker_semantics.get("title_eligible") is True,
        "ordinary_marker_is_user_message": marker_semantics.get("counts_as_user_message") is True,
        "unknown_slash_remains_eligible": unknown_semantics.get("title_eligible") is True,
    }
    boundary_observed = bool(marker_semantics and unknown_semantics)
    assertion_rows.append(
        {
            "probe_id": "shared_title_boundary",
            "status": (STATUS_PASS if all(boundary_assertions.values()) else STATUS_FAIL if boundary_observed else STATUS_BLOCKED),
            "failure_code": None if boundary_observed else "interaction_title_boundary_observation_missing",
            "assertions": boundary_assertions,
            "semantic_events": [marker_semantics, unknown_semantics],
        }
    )
    statuses = [str(row.get("status") or STATUS_FAIL) for row in assertion_rows]
    if any(status == STATUS_FAIL for status in statuses):
        status = STATUS_FAIL
    elif any(status == STATUS_BLOCKED for status in statuses):
        status = STATUS_BLOCKED
    elif all(status == STATUS_NOT_APPLICABLE for status in statuses):
        status = STATUS_NOT_APPLICABLE
    else:
        status = STATUS_PASS
    return {
        "status": status,
        "provider": provider,
        "probe_count": len(declared),
        "assertions": assertion_rows,
        "raw_event_count": len(raw_events),
        "semantic_projection": [
            {"event": event, "semantics": _event_semantics(provider, event)} for event in raw_events if isinstance(event, Mapping)
        ],
        "failure_code": None if status in {STATUS_PASS, STATUS_NOT_APPLICABLE} else "interaction_semantics_assertion_failed",
    }


def jsonl_events(observation: Mapping[str, Any]) -> str:
    rows = observation.get("raw_events")
    if not isinstance(rows, list):
        return ""
    return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows if isinstance(row, Mapping))


__all__ = ["evaluate_observation", "generated_fake_observation", "jsonl_events"]
