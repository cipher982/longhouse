#!/usr/bin/env python3
"""Bounded OMP Helm qualification using the native extension channel."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

from zerg.qa import provider_console_lifecycle as lifecycle
from zerg.qa import provider_release_identity as identity
from zerg.qa import provider_semantic_qualification as semantic
from zerg.qa.console_served_state_core import assistant_marker_events
from zerg.qa.console_served_state_core import event_text
from zerg.qa.live_session_toolkit import new_qualification_isolation_root
from zerg.qa.live_session_toolkit import redact_state_for_evidence
from zerg.qa.live_session_toolkit import retire_qualification_session
from zerg.qa.live_session_toolkit import start_transcript_shipper
from zerg.qa.omp_console_producer import omp_native_model_evidence
from zerg.qa.provider_factory_invocation import add_factory_provider_arguments
from zerg.qa.provider_release_identity import artifact_manifest
from zerg.qa.provider_release_identity import now
from zerg.qa.provider_release_identity import sha256_file
from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_interaction_semantics import omp_agent_end_is_terminal

SCENARIO_ID = "omp_helm_lifecycle"
ASSERTIONS = (
    "omp_helm_launch_registration",
    "omp_helm_send_idle",
    "omp_helm_follow_up_native",
    "omp_helm_steer_active",
    "omp_helm_abort_native",
    "omp_helm_terminate_owned",
    "omp_helm_cold_resume_exact_file",
    "omp_helm_stale_owner_refused",
    "omp_helm_native_replacement_bound",
)
_VARIANTS = tuple(
    execution_variant_key(
        provider="omp",
        assertion_id=assertion,
        scenario_id=SCENARIO_ID,
        variant=None,
    )
    for assertion in ASSERTIONS
)
_CELL_BY_VARIANT = {
    execution_variant_key(
        provider="omp",
        assertion_id=assertion,
        scenario_id=SCENARIO_ID,
        variant=None,
    ): assertion
    for assertion in ASSERTIONS
}

REGISTRATION = ProducerRegistration(
    producer_id="omp.helm_lifecycle.v1",
    producer_revision=8,
    scenario_id=SCENARIO_ID,
    scenario_revision=8,
    assertion_cells=tuple((assertion, None) for assertion in ASSERTIONS),
    providers=("omp",),
    platforms=("linux", "darwin"),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "omp_native_extension_channel_bound",
        "omp_agent_end_settlement_observed",
        "omp_native_archive_bound",
        "omp_transcript_shipper_started",
        "omp_transcript_flush_completed",
        "omp_runtime_transcript_converged",
        "omp_owned_processes_dead",
    ),
    acquisition_methods=("staged_release",),
    credential_binding_ids=("omp_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "omp_helm_receipt",
        "omp_native_settlement_receipt",
        "transcript_flush_receipt",
        "transcript_shipper_receipt",
        "runtime_convergence_receipt",
        "provider_source_retention",
        "cleanup_receipt",
    ),
    required_cleanup=(
        "provider_process_dead",
        "process_group_dead",
        "no_orphan_provider_processes",
        "canary_session_hidden",
        "isolation_removed",
    ),
    implementation="server/zerg/qa/omp_helm_lifecycle.py",
    oracle_source="server/zerg/qa/omp_helm_lifecycle.py",
    oracle_entrypoint="omp_helm_lifecycle_assertions",
    executable_module="zerg.qa.omp_helm_lifecycle",
    required_executables=("longhouse", "longhouse-engine"),
    observation_scope="scenario",
)
PROFILE = "omp_helm_v1"
_PROFILE = identity.IdentityProfile(
    provider="omp",
    profile=PROFILE,
    scenario_id=SCENARIO_ID,
    version_line=identity.semver_version_line(version_prefix=r"omp/"),
    oracle_source=Path(__file__),
)


def _requested_assertion_id(variant: object) -> str | None:
    return _CELL_BY_VARIANT.get(str(variant)) if isinstance(variant, str) else None


def _assertion_result_status(assertions: Mapping[str, bool], variant: object) -> str:
    requested = _requested_assertion_id(variant)
    passed = assertions.get(requested) is True if requested is not None else all(assertions.values())
    return "pass" if passed else "fail"


def _helm_result_status(
    assertions: Mapping[str, bool],
    variant: object,
    *,
    cleanup_ready: bool,
    manifest_stable: bool,
) -> str:
    # The result envelope is the scenario-level status consumed by the private
    # verifier. Per-cell selection happens in semantic qualification; a
    # passing selected assertion must not hide failed siblings here.
    _ = variant
    return "pass" if cleanup_ready and manifest_stable and bool(assertions) and all(assertions.values()) else "fail"


def omp_helm_lifecycle_assertions(observation: Mapping[str, object]) -> dict[str, bool]:
    cleanup = observation.get("cleanup")
    cleanup = cleanup if isinstance(cleanup, Mapping) else {}
    cleanup_ok = _helm_cleanup_ready(cleanup)
    channel = observation.get("channel_binding")
    channel = channel if isinstance(channel, Mapping) else {}
    send = observation.get("send_evidence")
    send = send if isinstance(send, Mapping) else {}
    follow_up = observation.get("follow_up_evidence")
    follow_up = follow_up if isinstance(follow_up, Mapping) else {}
    steer = observation.get("steer_evidence")
    steer = steer if isinstance(steer, Mapping) else {}
    abort = observation.get("abort_evidence")
    abort = abort if isinstance(abort, Mapping) else {}
    resume = observation.get("cold_resume_evidence")
    resume = resume if isinstance(resume, Mapping) else {}
    replacement = observation.get("replacement_evidence")
    replacement = replacement if isinstance(replacement, Mapping) else {}
    stale = observation.get("stale_owner_evidence")
    stale = stale if isinstance(stale, Mapping) else {}
    settlement = observation.get("settlement")
    settlement = settlement if isinstance(settlement, Mapping) else {}
    settlement_ok = (
        settlement.get("status") == "pass"
        and settlement.get("agent_end_terminal") is True
        and settlement.get("agent_end_evidence_shape") is True
        and settlement.get("native_archive_bound") is True
        and settlement.get("native_session_header_count") == 1
        and settlement.get("malformed_source") is False
        and settlement.get("agent_settled_is_not_completion_contract") is True
    )
    return {
        "omp_helm_launch_registration": (
            observation.get("observation_scope") == "scenario"
            and channel.get("ready") is True
            and channel.get("session_id_present") is True
            and channel.get("native_session_id_present") is True
            and channel.get("connection_id_present") is True
            and channel.get("lease_generation_present") is True
            and channel.get("session_file_present") is True
            and observation.get("omp_transcript_shipper_started") is True
            and observation.get("omp_transcript_flush_completed") is True
            and observation.get("omp_runtime_transcript_converged") is True
            and _runtime_control_identity_is_complete(observation.get("runtime_control_identity"))
            and observation.get("runtime_agents_api_controls") is True
            and settlement_ok
        ),
        "omp_helm_send_idle": (
            observation.get("send_idle") is True
            and send.get("native_source_bound") is True
            and send.get("marker_count") == 1
            and send.get("channel_ack_bound") is True
        ),
        "omp_helm_follow_up_native": (
            observation.get("follow_up_native") is True
            and follow_up.get("native_source_bound") is True
            and follow_up.get("marker_count") == 1
            and follow_up.get("channel_ack_bound") is True
        ),
        "omp_helm_steer_active": (
            observation.get("steer_active") is True
            and steer.get("native_source_bound") is True
            and steer.get("marker_count") == 1
            and steer.get("channel_ack_bound") is True
            and steer.get("active_command_bound") is True
            and steer.get("steer_command_bound") is True
            and isinstance(steer.get("active_state"), Mapping)
            and steer["active_state"].get("phase") in {"running", "thinking"}
            and steer.get("native_session_id") == steer["active_state"].get("native_session_id")
        ),
        "omp_helm_abort_native": (
            observation.get("abort_native") is True
            and abort.get("channel_source_bound") is True
            and abort.get("terminal") is True
            and abort.get("channel_ack_bound") is True
        ),
        "omp_helm_terminate_owned": observation.get("terminate_owned") is True and cleanup_ok and settlement_ok,
        "omp_helm_cold_resume_exact_file": (
            observation.get("cold_resume_exact_file") is True
            and resume.get("native_source_bound") is True
            and resume.get("marker_count") == 1
            and resume.get("context_recalled") is True
            and resume.get("context_marker_count") == 1
            and resume.get("channel_terminal_bound") is True
            and resume.get("terminal") is True
            and resume.get("exact_file") is True
        ),
        "omp_helm_stale_owner_refused": (observation.get("stale_owner_refused") is True and stale.get("error_code") == "stale_channel"),
        "omp_helm_native_replacement_bound": (
            observation.get("native_replacement_bound") is True
            and replacement.get("native_source_bound") is True
            and replacement.get("marker_count") == 1
            and replacement.get("channel_ack_bound") is True
        ),
    }


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _wait(
    observe: Any,
    *,
    timeout: float,
    description: str,
) -> Any:
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        last = observe()
        if last is not None:
            return last
        time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {description}: {last!r}")


_RUNTIME_HOST_RETRY_STATUSES = frozenset({404, 429, 500, 502, 503, 504})

_RUNTIME_EVENTS_PAGE_LIMIT = 500
_RUNTIME_EVENTS_MAX_PAGES = 32


def _runtime_get(api_url: str, token: str, path: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}{path}",
        headers={
            "X-Agents-Token": token,
            "Accept": "application/json",
            "User-Agent": "LonghouseProviderFactory/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in _RUNTIME_HOST_RETRY_STATUSES:
            raise RuntimeError(f"Runtime Host HTTP {exc.code}") from exc
        raise
    if not isinstance(payload, dict):
        raise RuntimeError("Runtime Host returned a non-object response")
    return payload


def _runtime_events_snapshot(api_url: str, token: str, session_id: str) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    first_page: dict[str, Any] | None = None
    generation_id: str | None = None
    branch_mode: str | None = None
    cursor: str | None = None

    for pages_read in range(1, _RUNTIME_EVENTS_MAX_PAGES + 1):
        query = f"/api/agents/sessions/{session_id}/events?anchor=start&branch_mode=head&limit={_RUNTIME_EVENTS_PAGE_LIMIT}"
        if cursor is not None:
            query += f"&cursor={quote(cursor, safe='')}"
        page = _runtime_get(api_url, token, query)
        if first_page is None:
            first_page = dict(page)
        page_session_id = page.get("session_id")
        if page_session_id is not None and page_session_id != session_id:
            raise RuntimeError("Runtime Host returned events for the wrong session")
        page_generation = page.get("generation_id")
        if not isinstance(page_generation, str) or not page_generation:
            raise RuntimeError("Runtime Host returned events without a generation identity")
        if generation_id is None:
            generation_id = page_generation
            branch_mode = page.get("branch_mode")
        elif page_generation != generation_id or page.get("branch_mode") != branch_mode:
            raise RuntimeError("Runtime Host changed the events generation during pagination")
        raw_events = page.get("events")
        if not isinstance(raw_events, list) or any(not isinstance(event, Mapping) for event in raw_events):
            raise RuntimeError("Runtime Host returned an invalid events page")
        events.extend(dict(event) for event in raw_events)
        has_more = page.get("has_more")
        next_cursor = page.get("next_cursor")
        if has_more is False and next_cursor is None:
            result = dict(first_page or {})
            result.update(
                {
                    "events": events,
                    "generation_id": generation_id,
                    "branch_mode": branch_mode,
                    "has_more": False,
                    "next_cursor": None,
                    "pagination": {
                        "pages_read": pages_read,
                        "exhausted": True,
                        "generation_id": generation_id,
                    },
                }
            )
            return result
        if has_more is not True or not isinstance(next_cursor, str) or not next_cursor:
            raise RuntimeError("Runtime Host returned a non-exhaustible events page")
        cursor = next_cursor
    raise RuntimeError(f"Runtime Host events pagination exceeded {_RUNTIME_EVENTS_MAX_PAGES} pages")


def _runtime_snapshot(api_url: str, token: str, session_id: str) -> dict[str, Any]:
    return {
        "detail": _runtime_get(api_url, token, f"/api/agents/sessions/{session_id}"),
        "thread": _runtime_get(api_url, token, f"/api/agents/sessions/{session_id}/thread"),
        "events": _runtime_events_snapshot(api_url, token, session_id),
        "diagnostic": _runtime_get(api_url, token, f"/api/agents/sessions/{session_id}/state-diagnostics"),
    }


def _flush_receipt_complete(receipt: Mapping[str, Any]) -> bool:
    events_shipped = receipt.get("events_shipped")
    return (
        receipt.get("status") == "pass"
        and receipt.get("exit_code") == 0
        and receipt.get("daemon_paused") is True
        and receipt.get("daemon_restarted") is True
        and isinstance(events_shipped, int)
        and not isinstance(events_shipped, bool)
        and events_shipped >= 0
    )


def _events_page_metadata(payload: Mapping[str, Any], *, requested_limit: int = _RUNTIME_EVENTS_PAGE_LIMIT) -> dict[str, Any]:
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    has_more = payload.get("has_more")
    next_cursor = payload.get("next_cursor")
    total = payload.get("total")
    generation_id = payload.get("generation_id")
    pagination = payload.get("pagination") if isinstance(payload.get("pagination"), Mapping) else {}
    pages_read = pagination.get("pages_read", 1)
    exhausted = pagination.get("exhausted") is True or (has_more is False and next_cursor is None)
    total_valid = isinstance(total, int) and not isinstance(total, bool) and total >= len(events)
    return {
        "requested_anchor": "start",
        "requested_branch_mode": "head",
        "requested_limit": requested_limit,
        "requested_cursor": None,
        "returned_count": len(events),
        "total": total,
        "generation_id": generation_id,
        "branch_mode": payload.get("branch_mode"),
        "abandoned_events": payload.get("abandoned_events"),
        "next_cursor": next_cursor,
        "has_more": has_more,
        "pages_read": pages_read,
        "pagination_exhausted": exhausted,
        "complete": (
            payload.get("branch_mode") == "head" and total_valid and isinstance(generation_id, str) and bool(generation_id) and exhausted
        ),
    }


def _stable_event_id(value: object) -> bool:
    return isinstance(value, (int, str)) and not isinstance(value, bool) and bool(str(value))


def _served_projection_evidence(
    snapshot: Mapping[str, Any],
    *,
    session_id: str,
    native_session_id: str,
    marker: str,
) -> dict[str, Any]:
    """Keep only structural fields from the served Runtime Host responses."""

    detail = snapshot.get("detail") if isinstance(snapshot.get("detail"), Mapping) else {}
    thread = snapshot.get("thread") if isinstance(snapshot.get("thread"), Mapping) else {}
    events_payload = snapshot.get("events") if isinstance(snapshot.get("events"), Mapping) else {}
    events = events_payload.get("events") if isinstance(events_payload.get("events"), list) else []
    events_page = _events_page_metadata(events_payload)
    members = thread.get("sessions") if isinstance(thread.get("sessions"), list) else []
    marker_matches = assistant_marker_events(events, marker, default_origin="durable")
    marker_event_id = marker_matches[0].get("id") if len(marker_matches) == 1 else None
    diagnostic = snapshot.get("diagnostic") if isinstance(snapshot.get("diagnostic"), Mapping) else {}
    return {
        "requested_session_id": session_id,
        "detail": {
            "id": detail.get("id"),
            "provider": detail.get("provider"),
            "provider_session_id": detail.get("provider_session_id"),
        },
        "thread": {
            "root_session_id": thread.get("root_session_id"),
            "head_session_id": thread.get("head_session_id"),
            "session_ids": [member.get("id") for member in members if isinstance(member, Mapping) and member.get("id") is not None],
        },
        "events": [
            {
                "id": event.get("id"),
                "role": event.get("role"),
                "event_origin": event.get("event_origin", "durable"),
                "tool_name_present": bool(event.get("tool_name")),
                "marker_occurrences": event_text(event).count(marker),
            }
            for event in events
            if isinstance(event, Mapping)
        ],
        # Retain the marker-bearing served records themselves. Counts and
        # identities are useful indexes, not independent proof of content.
        "marker_events": [dict(event) for event in marker_matches],
        "marker_event_id": marker_event_id,
        "marker_event_durable": (len(marker_matches) == 1 and marker_matches[0].get("event_origin", "durable") == "durable"),
        "marker_event_identity_bound": len(marker_matches) == 1 and _stable_event_id(marker_event_id),
        "events_page": events_page,
        "diagnostic": {
            "session_id": diagnostic.get("session_id"),
            "served_path": diagnostic.get("served_path"),
        },
    }


def _runtime_convergence(
    api_url: str,
    token: str,
    *,
    session_id: str,
    native_session_id: str,
    marker: str,
    flush: Mapping[str, Any],
    native_source_path: str,
    timeout: float = 90,
) -> dict[str, Any]:
    def observe() -> dict[str, Any] | None:
        try:
            snapshot = _runtime_snapshot(api_url, token, session_id)
        except RuntimeError as exc:
            if str(exc).startswith("Runtime Host HTTP"):
                return None
            raise
        events_payload = snapshot.get("events") if isinstance(snapshot.get("events"), Mapping) else {}
        events = events_payload.get("events") if isinstance(events_payload.get("events"), list) else []
        matches = assistant_marker_events(events, marker, default_origin="durable")
        served_projection = _served_projection_evidence(
            snapshot,
            session_id=session_id,
            native_session_id=native_session_id,
            marker=marker,
        )
        events_page = served_projection["events_page"]
        if not isinstance(events_page, Mapping) or events_page.get("complete") is not True:
            return {
                "status": "unproven",
                "reason": "runtime_events_page_incomplete",
                "provider": "omp",
                "session_id": session_id,
                "native_session_id": native_session_id,
                "marker": marker,
                "flush": dict(flush),
                "native_source_path": native_source_path,
                "events_page": dict(events_page) if isinstance(events_page, Mapping) else {},
                "served_projection": served_projection,
            }
        diagnostic = snapshot.get("diagnostic") if isinstance(snapshot.get("diagnostic"), Mapping) else {}
        detail = served_projection.get("detail") if isinstance(served_projection.get("detail"), Mapping) else {}
        thread = served_projection.get("thread") if isinstance(served_projection.get("thread"), Mapping) else {}
        served_events = served_projection.get("events") if isinstance(served_projection.get("events"), list) else []
        served_event_ids = [event.get("id") for event in served_events if isinstance(event, Mapping)]
        exact_served_identity = (
            detail.get("id") == session_id
            and detail.get("provider") == "omp"
            and detail.get("provider_session_id") == native_session_id
            and thread.get("root_session_id") == session_id
            and thread.get("head_session_id") == session_id
            and session_id in (thread.get("session_ids") if isinstance(thread.get("session_ids"), list) else [])
        )
        marker_bound_durable_event = (
            len(matches) == 1
            and event_text(matches[0]).count(marker) == 1
            and matches[0].get("event_origin", "durable") == "durable"
            and _stable_event_id(served_projection.get("marker_event_id"))
            and served_projection.get("marker_event_id") in served_event_ids
            and served_projection.get("marker_event_durable") is True
            and served_projection.get("marker_event_identity_bound") is True
        )
        if not exact_served_identity or not marker_bound_durable_event or diagnostic.get("served_path") != "canonical_session_detail":
            return None
        return {
            "status": "pass",
            "provider": "omp",
            "session_id": session_id,
            "native_session_id": native_session_id,
            "marker": marker,
            "flush": dict(flush),
            "native_source_path": native_source_path,
            "served_projection": served_projection,
        }

    return _wait(observe, timeout=timeout, description=f"Runtime Host convergence for OMP marker {marker}")


def _served_control_identity(
    diagnostic: dict[str, Any],
    *,
    expected_subject_key: str,
) -> bool:
    """Require the exact managed channel before exercising control."""
    shadow = diagnostic.get("shadow") if isinstance(diagnostic.get("shadow"), dict) else {}
    fact_sources = shadow.get("fact_sources") if isinstance(shadow.get("fact_sources"), dict) else {}
    control_source = fact_sources.get("control") if isinstance(fact_sources.get("control"), dict) else {}
    control = shadow.get("control") if isinstance(shadow.get("control"), dict) else {}
    actions = control.get("actions") if isinstance(control.get("actions"), dict) else {}
    return (
        diagnostic.get("served_path") == "canonical_session_detail"
        and control_source.get("subject_key") == expected_subject_key
        and isinstance(actions.get("send_input"), dict)
        and actions["send_input"].get("state") == "available"
        and isinstance(actions.get("interrupt"), dict)
        and actions["interrupt"].get("state") == "available"
        and isinstance(actions.get("terminate"), dict)
        and actions["terminate"].get("state") == "available"
    )


def _control_identity_receipt(identity: Mapping[str, Any]) -> dict[str, Any]:
    diagnostic = identity.get("diagnostic") if isinstance(identity.get("diagnostic"), Mapping) else {}
    shadow = diagnostic.get("shadow") if isinstance(diagnostic.get("shadow"), Mapping) else {}
    fact_sources = shadow.get("fact_sources") if isinstance(shadow.get("fact_sources"), Mapping) else {}
    control_source = fact_sources.get("control") if isinstance(fact_sources.get("control"), Mapping) else {}
    control = shadow.get("control") if isinstance(shadow.get("control"), Mapping) else {}
    actions = control.get("actions") if isinstance(control.get("actions"), Mapping) else {}
    local_state = identity.get("local_state")
    owner_identity = list(_state_owner_identity(local_state)) if isinstance(local_state, Mapping) else None
    return {
        "session_id": identity.get("session_id"),
        "expected_subject_key": identity.get("expected_subject_key"),
        "served_path": diagnostic.get("served_path"),
        "shadow_mode": shadow.get("mode"),
        "control_subject_key": control_source.get("subject_key"),
        "owner_identity": owner_identity,
        "actions": {
            name: (value.get("state") if isinstance(value, Mapping) else None)
            for name, value in actions.items()
            if name in {"send_input", "interrupt", "terminate"}
        },
        "transient_errors": list(identity.get("transient_errors") or []),
    }


def _owner_identity_is_complete(owner_identity: Any) -> bool:
    if not isinstance(owner_identity, list) or len(owner_identity) != len(_OWNER_IDENTITY_FIELDS):
        return False
    session_id, native_session_id, session_file, launcher_pid, launcher_start, provider_pid, provider_start = owner_identity
    return (
        all(isinstance(value, str) and value.strip() for value in (session_id, native_session_id, session_file))
        and all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in (launcher_pid, provider_pid))
        and all(isinstance(value, str) and value.strip() for value in (launcher_start, provider_start))
    )


def _control_identity_receipt_is_bound(receipt: Any) -> bool:
    if not isinstance(receipt, Mapping):
        return False
    expected_subject_key = receipt.get("expected_subject_key")
    actions = receipt.get("actions")
    owner_identity = receipt.get("owner_identity")
    if not isinstance(expected_subject_key, str) or not expected_subject_key:
        return False
    if (
        receipt.get("session_id") is None
        or receipt.get("control_subject_key") != expected_subject_key
        or not _owner_identity_is_complete(owner_identity)
    ):
        return False
    if receipt.get("served_path") != "canonical_session_detail" or not isinstance(actions, Mapping):
        return False
    return all(isinstance(actions.get(name), str) and actions.get(name) == "available" for name in ("send_input", "interrupt", "terminate"))


def _runtime_control_identity_is_complete(identity: Any) -> bool:
    if not isinstance(identity, Mapping):
        return False
    labels = ("initial", "replacement", "cold_resume", "final")
    receipts = [identity.get(label) for label in labels]
    if not all(_control_identity_receipt_is_bound(receipt) for receipt in receipts):
        return False
    session_ids = {str(receipt["session_id"]) for receipt in receipts if isinstance(receipt, Mapping)}
    subject_keys = [str(receipt["expected_subject_key"]) for receipt in receipts if isinstance(receipt, Mapping)]
    owner_identities = [
        tuple(receipt["owner_identity"])
        for receipt in receipts
        if isinstance(receipt, Mapping) and isinstance(receipt.get("owner_identity"), list)
    ]
    return (
        len(session_ids) == 1
        and len(subject_keys) == len(labels)
        and len(set(subject_keys[:3])) == 3
        and len(owner_identities) == len(labels)
        and owner_identities[2] == owner_identities[3]
    )


_OWNER_IDENTITY_FIELDS = (
    "session_id",
    "native_session_id",
    "session_file",
    "launcher_pid",
    "launcher_process_start_time",
    "provider_pid",
    "provider_process_start_time",
)


def _state_owner_identity(state: Mapping[str, Any]) -> tuple[Any, ...]:
    """Return the execution-owner fields that must not change during a wait."""

    return tuple(state.get(field) for field in _OWNER_IDENTITY_FIELDS)


def _is_transient_runtime_read_error(error: BaseException) -> bool:
    message = str(error)
    return message.startswith("Runtime Host HTTP ") and any(
        message.startswith(f"Runtime Host HTTP {status}") for status in _RUNTIME_HOST_RETRY_STATUSES
    )


def _current_ready_state(home: Path, session_id: str) -> dict[str, Any] | None:
    state_dir = home / "managed-local" / "omp-helm"
    candidates: list[dict[str, Any]] = []
    for path in state_dir.glob("*.json"):
        value = _read_state(path)
        if (
            value
            and value.get("ready") is True
            and value.get("session_id") == session_id
            and value.get("connection_id")
            and value.get("lease_generation")
        ):
            candidates.append(value)
    if not candidates:
        return None
    return max(candidates, key=lambda value: str(value.get("updated_at") or ""))


def _control_observation_diagnostic(diagnostic: Mapping[str, Any]) -> dict[str, Any]:
    shadow = diagnostic.get("shadow") if isinstance(diagnostic.get("shadow"), Mapping) else {}
    sources = shadow.get("fact_sources") if isinstance(shadow.get("fact_sources"), Mapping) else {}
    control_source = sources.get("control") if isinstance(sources.get("control"), Mapping) else {}
    control = shadow.get("control") if isinstance(shadow.get("control"), Mapping) else {}
    actions = control.get("actions") if isinstance(control.get("actions"), Mapping) else {}
    return {
        "served_path": diagnostic.get("served_path"),
        "control_subject_key": control_source.get("subject_key"),
        "connection": control.get("connection"),
        "ownership": control.get("ownership"),
        "actions": {
            name: value.get("state") if isinstance(value, Mapping) else None
            for name, value in actions.items()
            if name in {"send_input", "interrupt", "terminate"}
        },
    }


def _wait_runtime_control_identity(
    url: str,
    token: str,
    *,
    home: Path | None = None,
    session_id: str,
    state: dict[str, Any],
    timeout: float = 90.0,
) -> dict[str, Any]:
    baseline_owner = _state_owner_identity(state)
    transient_errors: list[str] = []
    last_observation: dict[str, Any] = {}

    def observe() -> dict[str, Any] | None:
        local_before = _current_ready_state(home, session_id) if home is not None else state
        if local_before is None:
            last_observation.clear()
            last_observation.update({"reason": "local_state_unavailable"})
            return None
        if _state_owner_identity(local_before) != baseline_owner:
            last_observation.clear()
            last_observation.update(
                {
                    "reason": "execution_owner_changed",
                    "local_state": _redacted_state_snapshot(local_before),
                    "baseline_state": _redacted_state_snapshot(state),
                }
            )
            raise RuntimeError("OMP Helm execution owner changed while waiting for runtime control identity")
        connection_id = str(local_before.get("connection_id") or "").strip()
        lease_generation = str(local_before.get("lease_generation") or "").strip()
        expected_subject_key = f"connection:{connection_id}:{lease_generation}"
        try:
            diagnostic = _runtime_get(
                url,
                token,
                f"/api/agents/sessions/{session_id}/state-diagnostics",
            )
        except RuntimeError as exc:
            if not _is_transient_runtime_read_error(exc):
                raise
            transient_errors.append(str(exc))
            del transient_errors[:-8]
            last_observation.clear()
            last_observation.update(
                {
                    "reason": "transient_runtime_read",
                    "expected_subject_key": expected_subject_key,
                    "error": str(exc),
                }
            )
            return None
        local_after = _current_ready_state(home, session_id) if home is not None else local_before
        if local_after is None:
            last_observation.clear()
            last_observation.update(
                {
                    "reason": "local_state_unavailable_during_read",
                    "expected_subject_key": expected_subject_key,
                    "local_before": _redacted_state_snapshot(local_before),
                }
            )
            return None
        if _state_owner_identity(local_after) != baseline_owner:
            last_observation.clear()
            last_observation.update(
                {
                    "reason": "execution_owner_changed_during_read",
                    "expected_subject_key": expected_subject_key,
                    "local_before": _redacted_state_snapshot(local_before),
                    "local_after": _redacted_state_snapshot(local_after),
                }
            )
            raise RuntimeError("OMP Helm execution owner changed during runtime control identity read")
        after_connection_id = str(local_after.get("connection_id") or "").strip()
        after_lease_generation = str(local_after.get("lease_generation") or "").strip()
        after_subject_key = f"connection:{after_connection_id}:{after_lease_generation}"
        if after_subject_key != expected_subject_key:
            last_observation.clear()
            last_observation.update(
                {
                    "reason": "lease_rotated_during_read",
                    "expected_subject_key": expected_subject_key,
                    "current_subject_key": after_subject_key,
                }
            )
            return None
        last_observation.clear()
        last_observation.update(
            {
                "reason": "projection_not_bound",
                "expected_subject_key": expected_subject_key,
                "local_state": _redacted_state_snapshot(local_after),
                "runtime": _control_observation_diagnostic(diagnostic),
            }
        )
        if not _served_control_identity(diagnostic, expected_subject_key=expected_subject_key):
            return None
        return {
            "session_id": session_id,
            "expected_subject_key": expected_subject_key,
            "diagnostic": diagnostic,
            "local_state": _redacted_state_snapshot(local_after),
            "transient_errors": list(transient_errors),
        }

    try:
        return _wait(
            observe,
            timeout=timeout,
            description=f"Runtime Host managed control identity for {session_id}",
        )
    except RuntimeError as exc:
        evidence = json.dumps(last_observation, sort_keys=True, separators=(",", ":"))
        raise RuntimeError(f"{exc}; last_observation={evidence}") from exc


def _wait_state(
    home: Path,
    *,
    session_id: str | None = None,
    timeout: float = 30,
    predicate: Any = None,
) -> dict[str, Any]:
    state_dir = home / "managed-local" / "omp-helm"

    def observe() -> dict[str, Any] | None:
        for path in sorted(state_dir.glob("*.json")):
            state = _read_state(path)
            if not state or state.get("ready") is not True:
                continue
            if session_id is not None and state.get("session_id") != session_id:
                continue
            if predicate is not None and not predicate(state):
                continue
            return state
        return None

    return _wait(observe, timeout=timeout, description="OMP Helm state")


def _native_rows(session_file: Path, minimum_offset: int = 0) -> list[dict[str, Any]]:
    try:
        content = session_file.read_bytes()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    offset = 0
    for raw in content.splitlines(keepends=True):
        end = offset + len(raw)
        if end <= minimum_offset:
            offset = end
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            offset = end
            continue
        if isinstance(value, dict):
            value = dict(value)
            value["_source_offset"] = offset
            rows.append(value)
        offset = end
    return rows


def _wait_native_marker(
    session_file: Path,
    marker: str,
    *,
    minimum_offset: int = 0,
    role: str = "assistant",
    timeout: float = 90,
) -> dict[str, Any]:
    def observe() -> dict[str, Any] | None:
        for row in _native_rows(session_file, minimum_offset):
            if row.get("type") != "message":
                continue
            message = row.get("message")
            if not isinstance(message, Mapping) or message.get("role") != role:
                continue
            if marker in _native_message_text(message):
                return row
        return None

    return _wait(observe, timeout=timeout, description=f"OMP native marker {marker}")


def _native_message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text"))
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str)
        )
    return ""


def _is_terminal_agent_end(event: Mapping[str, Any]) -> bool:
    return omp_agent_end_is_terminal(event)


def _native_marker_evidence(
    row: Mapping[str, Any],
    session_file: Path,
    *,
    marker: str,
    minimum_offset: int,
    native_session_id: str,
    role: str = "assistant",
) -> dict[str, Any]:
    message = row.get("message")
    text = _native_message_text(message) if isinstance(message, Mapping) else ""
    offset = row.get("_source_offset")
    return {
        "native_source_bound": (
            session_file.is_file()
            and row.get("type") == "message"
            and isinstance(message, Mapping)
            and message.get("role") == role
            and isinstance(row.get("id"), str)
            and isinstance(offset, int)
            and offset >= minimum_offset
            and bool(native_session_id)
        ),
        "source_path": str(session_file),
        "source_offset": offset,
        "minimum_source_offset": minimum_offset,
        "native_session_id": native_session_id,
        "message_role": role,
        "event_id": row.get("id"),
        "marker": marker,
        "marker_count": text.count(marker),
    }


def _native_terminal_evidence(
    row: Mapping[str, Any],
    session_file: Path,
    *,
    minimum_offset: int,
    native_session_id: str,
) -> dict[str, Any]:
    offset = row.get("_source_offset")
    terminal = row.get("type") == "agent_end" and _is_terminal_agent_end(row)
    return {
        "native_source_bound": session_file.is_file() and terminal and isinstance(offset, int) and offset >= minimum_offset,
        "source_path": str(session_file),
        "source_offset": offset,
        "minimum_source_offset": minimum_offset,
        "native_session_id": native_session_id,
        "event_type": row.get("type"),
        "terminal": terminal,
    }


def _channel_terminal_evidence(
    event: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    native_session_id: str,
    session_file: Path,
) -> dict[str, Any]:
    terminal = event.get("type") == "agent_end" and _is_terminal_agent_end(event)
    evidence = {
        "channel_source_bound": (
            terminal
            and event.get("source") == "omp_helm_extension_channel"
            and state.get("status") == "ready"
            and state.get("native_session_id") == native_session_id
            and state.get("session_file") == str(session_file)
            and bool(state.get("connection_id"))
            and bool(state.get("lease_generation"))
        ),
        "source": "omp_helm_extension_channel",
        "event_type": event.get("type"),
        "native_session_id": native_session_id,
        "session_file": str(session_file),
        "connection_id": state.get("connection_id"),
        "lease_generation": state.get("lease_generation"),
        "terminal": terminal,
    }
    if "isTerminal" in event:
        evidence["is_terminal"] = event["isTerminal"]
    if "willContinue" in event:
        evidence["will_continue"] = event["willContinue"]
    return evidence


def _channel_binding_evidence(state: Mapping[str, Any]) -> dict[str, Any]:
    session_file = Path(str(state.get("session_file") or ""))
    return {
        "ready": state.get("ready") is True,
        "session_id_present": bool(state.get("session_id")),
        "native_session_id_present": bool(state.get("native_session_id")),
        "connection_id_present": bool(state.get("connection_id")),
        "lease_generation_present": bool(state.get("lease_generation")),
        "session_file_present": session_file.is_file(),
    }


def _redacted_state_snapshot(state: Mapping[str, Any]) -> dict[str, Any]:
    redacted = redact_state_for_evidence(dict(state))
    return redacted if isinstance(redacted, dict) else {}


def _channel_command_evidence(command: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    payload = command.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    request = command.get("request")
    request = request if isinstance(request, Mapping) else {}
    request_payload = request.get("payload")
    request_payload = request_payload if isinstance(request_payload, Mapping) else {}
    expected_path = f"/api/agents/sessions/{state.get('session_id')}/input"
    response_client_request_id = payload.get("client_request_id")
    request_client_request_id = request_payload.get("client_request_id")
    native_session_id = state.get("native_session_id")
    accepted = command.get("accepted") is True
    request_bound = (
        request.get("method") == "POST"
        and request.get("path") == expected_path
        and isinstance(request_payload.get("text"), str)
        and bool(request_payload.get("text"))
        and request_payload.get("intent") in {"auto", "steer"}
        and isinstance(request_client_request_id, str)
        and bool(request_client_request_id)
        and response_client_request_id == request_client_request_id
    )
    return {
        "accepted": accepted,
        "native_session_id": native_session_id,
        "status": state.get("phase"),
        "request_path": request.get("path"),
        "request_client_request_id": request_client_request_id,
        "response_client_request_id": response_client_request_id,
        "channel_ack_bound": (accepted and request_bound and state.get("ready") is True and bool(native_session_id)),
    }


def _exact_session_retirement(receipt: Mapping[str, Any] | None, session_id: str | None) -> bool:
    return (
        isinstance(receipt, Mapping)
        and bool(session_id)
        and receipt.get("status") == "pass"
        and receipt.get("session_id") == session_id
        and receipt.get("hidden") is True
        and receipt.get("archived") is True
        and receipt.get("present_in_served_inventory") is False
    )


def _wait_served_run_retirement(
    api_url: str,
    token: str,
    session_id: str,
    claims: list[dict[str, Any]],
    *,
    timeout: float = 30,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = dict(lifecycle._served_run_inventory_evidence(api_url, token, session_id, claims))
        if last.get("retired") is True:
            last["retirement_wait_status"] = "pass"
            return last
        time.sleep(0.2)
    last["retirement_wait_status"] = "timeout"
    return last


def _turn_sequence(state: Mapping[str, Any]) -> int:
    value = state.get("live_turn_seq")
    return value if type(value) is int and value >= 0 else 0


def _wait_channel_terminal(
    home: Path,
    *,
    session_id: str,
    native_session_id: str,
    session_file: Path,
    minimum_turn_seq: int = 0,
    timeout: float = 90,
) -> tuple[dict[str, Any], dict[str, Any]]:
    def observe() -> tuple[dict[str, Any], dict[str, Any]] | None:
        state = _read_state(home / "managed-local" / "omp-helm" / f"{session_id}.json")
        if not isinstance(state, dict):
            return None
        if (
            state.get("ready") is True
            and state.get("native_session_id") == native_session_id
            and state.get("session_file") == str(session_file)
            and _turn_sequence(state) >= minimum_turn_seq
            and state.get("agent_end_observed") is True
            and state.get("agent_end_is_terminal") is True
        ):
            return (
                {
                    "type": "agent_end",
                    "source": "omp_helm_extension_channel",
                    **({"isTerminal": state["agent_end_is_terminal"]} if state.get("agent_end_is_terminal_present") is True else {}),
                    **({"willContinue": state["agent_end_will_continue"]} if state.get("agent_end_will_continue_present") is True else {}),
                },
                state,
            )
        return None

    return _wait(observe, timeout=timeout, description="OMP Helm extension-channel agent_end")


def _native_settlement(
    session_file: Path,
    *,
    channel_state: Mapping[str, Any],
    native_session_id: str,
) -> dict[str, object]:
    try:
        payload = session_file.read_bytes()
    except OSError as exc:
        return {
            "status": "fail",
            "error": f"{type(exc).__name__}: {exc}",
            "agent_end_terminal": False,
            "agent_end_evidence_shape": False,
            "native_archive_bound": False,
            "native_terminal_after_assistant": False,
            "malformed_source": True,
            "session_file": str(session_file),
        }
    rows: list[dict[str, Any]] = []
    malformed_source = False
    offset = 0
    for raw in payload.splitlines(keepends=True):
        end = offset + len(raw)
        if not raw.strip():
            offset = end
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            malformed_source = True
            offset = end
            continue
        if isinstance(value, dict):
            row = dict(value)
            row["_source_offset"] = offset
            rows.append(row)
        else:
            malformed_source = True
        offset = end
    headers = [row for row in rows if row.get("type") == "session" and row.get("id") == native_session_id]
    assistant_rows = [
        row
        for row in rows
        if row.get("type") in {"message", "message_end"}
        and isinstance(row.get("message"), Mapping)
        and row["message"].get("role") == "assistant"
    ]
    native_archive_bound = len(headers) == 1
    native_terminal_after_assistant = False
    channel_bound = (
        channel_state.get("status") == "ready"
        and channel_state.get("phase") == "idle"
        and channel_state.get("native_session_id") == native_session_id
        and channel_state.get("session_file") == str(session_file)
        and isinstance(channel_state.get("updated_at"), str)
        and bool(channel_state.get("updated_at"))
    )
    channel_agent_end_observed = channel_state.get("agent_end_observed") is True and isinstance(
        channel_state.get("agent_end_is_terminal"), bool
    )
    agent_end_terminal = channel_agent_end_observed and channel_state.get("agent_end_is_terminal") is True
    return {
        "status": (
            "pass"
            if agent_end_terminal
            and native_archive_bound
            and bool(assistant_rows)
            and agent_end_terminal
            and channel_bound
            and not malformed_source
            else "fail"
        ),
        "agent_end_terminal": agent_end_terminal,
        "agent_end_evidence_shape": channel_bound and channel_agent_end_observed,
        "agent_end_evidence_source": "omp_helm_extension_channel" if channel_bound and channel_agent_end_observed else None,
        "native_archive_bound": native_archive_bound,
        "native_session_header_count": len(headers),
        "native_terminal_after_assistant": native_terminal_after_assistant,
        "malformed_source": malformed_source,
        "agent_settled_is_not_completion_contract": True,
        "channel_phase": channel_state.get("phase"),
        "channel_status": channel_state.get("status"),
        "channel_native_session_id": channel_state.get("native_session_id"),
        "channel_session_file": channel_state.get("session_file"),
        "channel_updated_at": channel_state.get("updated_at"),
        "session_file": str(session_file),
    }


def _stale_frame(old_state: Mapping[str, Any], *, text: str) -> dict[str, Any]:
    socket_path = str(old_state.get("socket_path") or "")
    if not socket_path:
        raise RuntimeError("old OMP Helm state has no socket path")
    request = {
        "kind": "send",
        "session_id": old_state.get("session_id"),
        "native_session_id": old_state.get("native_session_id"),
        "connection_id": old_state.get("connection_id"),
        "lease_generation": old_state.get("lease_generation"),
        "auth_token": old_state.get("channel_token"),
        "text": text,
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(socket_path)
        client.sendall((json.dumps(request) + "\n").encode())
        payload = b""
        while not payload.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            payload += chunk
    try:
        value = json.loads(payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        value = {}
    return value if isinstance(value, dict) else {}


def _runtime_post(api_url: str, token: str, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(dict(payload)).encode("utf-8")
    headers = {
        "X-Agents-Token": token,
        "Accept": "application/json",
        "User-Agent": "LonghouseProviderFactory/1.0",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(60):
        request = urllib.request.Request(
            f"{api_url.rstrip('/')}{path}",
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                value = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            # TranscriptShipper.flush restarts the real Machine Agent after
            # the storage proof. The control websocket can take a few seconds
            # to re-register; do not mistake that bounded reconnect window for
            # a failed provider control path.
            if exc.code == 409 and "live longhouse control channel" in detail.lower() and attempt < 59:
                time.sleep(0.5)
                continue
            raise RuntimeError(f"Runtime Host HTTP {exc.code}: {detail[:500]}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Runtime Host returned a non-object control response")
        return value
    raise RuntimeError("Runtime Host control request exhausted reconnect attempts")


def _run_engine(
    engine: Path,
    command: str,
    session_id: str,
    env: Mapping[str, str],
    *,
    text: str | None = None,
) -> dict[str, object]:
    """Dispatch OMP controls through the Runtime Host machine API.

    The ``engine`` argument remains part of the producer call contract so
    older invocation code cannot accidentally turn this proof back into a
    local-engine-only test.  It is intentionally not executed: the Runtime
    Host is the authority for remote control, and the disposable Machine Agent
    is the only process that talks to the provider.
    """
    del engine
    api_url = str(env.get("LONGHOUSE_OMP_HELM_URL") or "").strip()
    token = str(env.get("LONGHOUSE_OMP_HELM_TOKEN") or "").strip()
    if not api_url or not token:
        raise RuntimeError("OMP Helm control requires Runtime Host URL and token")

    if command in {"send", "steer"}:
        if not text:
            raise RuntimeError(f"OMP Helm {command} requires text")
        path = f"/api/agents/sessions/{session_id}/input"
        payload = {
            "text": text,
            "intent": "steer" if command == "steer" else "auto",
            "client_request_id": f"omp-helm-{command}-{os.urandom(8).hex()}",
        }
    elif command == "abort":
        path = f"/api/agents/sessions/{session_id}/interrupt-live"
        payload = None
    elif command == "terminate":
        path = f"/api/agents/sessions/{session_id}/terminate-live"
        payload = None
    else:
        raise RuntimeError(f"unsupported OMP Helm control command: {command}")

    response = _runtime_post(api_url, token, path, payload)
    if command in {"send", "steer"}:
        accepted = response.get("outcome") in {"sent", "queued"}
    elif command == "abort":
        accepted = response.get("interrupt_dispatched") is True
    else:
        accepted = response.get("terminate_dispatched") is True
    return {
        "returncode": 0,
        "accepted": accepted,
        "payload": response,
        "transport": "runtime_host_agents_api",
        "path": path,
        "request": {
            "method": "POST",
            "path": path,
            "payload": dict(payload) if isinstance(payload, Mapping) else None,
        },
    }


def _launch_argv(
    args: argparse.Namespace,
    *,
    workspace: Path,
    prompt: str,
    resume_session: str | None = None,
) -> list[str]:
    argv = [
        str(args.longhouse_cli),
        "omp",
        "--cwd",
        str(workspace),
        "--omp-bin",
        str(args.provider_bin),
        "--url",
        str(args.api_url),
        "--prompt",
        prompt,
    ]
    if getattr(args, "model", None):
        argv.extend(("--model", str(args.model)))
    if resume_session is not None:
        argv.extend(("--resume-session", resume_session))
    return argv


def _wait_stopped(
    longhouse_home: Path,
    session_id: str,
    *,
    timeout: float = 30,
) -> dict[str, Any]:
    state_path = longhouse_home / "managed-local" / "omp-helm" / f"{session_id}.json"

    def observe() -> dict[str, Any] | None:
        state = _read_state(state_path)
        if state and state.get("status") == "stopped":
            return state
        return None

    return _wait(observe, timeout=timeout, description="OMP Helm remote termination")


def _read_source_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _register_native_source(
    claims: list[dict[str, Any]],
    *,
    label: str,
    source_path: object,
    session_id: object,
    native_session_id: object,
) -> None:
    """Claim a native source before later lifecycle work can fail."""

    if not isinstance(source_path, str) or not source_path:
        return
    if any(item.get("source_path") == source_path for item in claims):
        return
    claims.append(
        {
            "label": label,
            "run_id": label,
            "source_path": source_path,
            "session_id": session_id,
            "native_session_id": native_session_id,
        }
    )


def _remove_isolation_after_source_retention(
    isolation: Path,
    *,
    source_retention_verified: bool,
    cleanup: dict[str, Any],
) -> bool:
    if not source_retention_verified:
        cleanup["isolation_retained"] = True
        cleanup["authoritative_source_evidence_retained"] = isolation.exists()
        return False
    try:
        shutil.rmtree(isolation)
    except FileNotFoundError:
        return True
    except OSError as exc:
        cleanup["isolation_remove_error"] = f"{type(exc).__name__}: {exc}"
        return False
    return not isolation.exists()


def _wait_native_agent_end(
    session_file: Path,
    *,
    minimum_offset: int,
    timeout: float = 90,
) -> dict[str, Any]:
    def observe() -> dict[str, Any] | None:
        for row in _native_rows(session_file, minimum_offset):
            if row.get("type") == "agent_end":
                return row
        return None

    return _wait(observe, timeout=timeout, description="OMP native agent_end")


def _process_record(pid: object, expected_birth: object, label: str, *, owner: str) -> dict[str, Any]:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return {
            "owner": owner,
            "label": label,
            "pid": pid,
            "process_group_id": None,
            "pgid": None,
            "birth_matches": False,
            "pid_positive": False,
            "process_group_positive": False,
            "pid_dead": False,
            "process_group_dead": False,
            "alive": False,
        }
    try:
        completed = subprocess.run(
            ["ps", "-o", "pid=", "-o", "pgid=", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        fields = completed.stdout.strip().split(None, 2)
        pgid = int(fields[1]) if len(fields) >= 2 else None
        birth = fields[2].strip() if len(fields) >= 3 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pgid = None
        birth = None
    return {
        "owner": owner,
        "label": label,
        "pid": pid,
        "process_group_id": pgid,
        "pgid": pgid,
        "birth": birth,
        "expected_birth": expected_birth,
        "birth_matches": bool(birth and expected_birth and birth == str(expected_birth)),
        "pid_positive": True,
        "process_group_positive": isinstance(pgid, int) and not isinstance(pgid, bool) and pgid > 0,
        "pid_dead": _pid_dead(pid),
        "process_group_dead": _pgid_dead(pgid),
        "alive": _pid_alive(pid),
    }


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_dead(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _pgid_dead(pgid: object) -> bool:
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _cleanup_receipt(records: list[dict[str, Any]]) -> dict[str, Any]:
    unique = {
        (
            item.get("owner"),
            item.get("label"),
            item.get("pid"),
            item.get("process_group_id", item.get("pgid")),
            item.get("expected_birth"),
        ): dict(item)
        for item in records
    }
    records = list(unique.values())
    for item in records:
        process_group_id = item.get("process_group_id", item.get("pgid"))
        item["process_group_id"] = process_group_id
        item["pgid"] = process_group_id
        item["pid_positive"] = isinstance(item.get("pid"), int) and not isinstance(item.get("pid"), bool) and item["pid"] > 0
        item["process_group_positive"] = (
            isinstance(process_group_id, int) and not isinstance(process_group_id, bool) and process_group_id > 0
        )
        item["pid_dead"] = _pid_dead(item.get("pid"))
        item["process_group_dead"] = _pgid_dead(process_group_id)
        item["alive"] = not item["pid_dead"]
    provider_records = [item for item in records if item.get("label") == "provider"]
    provider_dead = bool(provider_records) and all(item.get("pid_dead") is True for item in provider_records)
    groups_dead = bool(records) and all(item.get("process_group_dead") is True for item in records)
    birth_verified = bool(records) and all(item.get("birth_matches") is True for item in records)
    orphan_count = sum(not (item.get("pid_dead") is True and item.get("process_group_dead") is True) for item in records)
    return {
        "status": "pass" if provider_dead and groups_dead and birth_verified and orphan_count == 0 else "fail",
        "provider_process_dead": provider_dead,
        "process_group_dead": groups_dead,
        "no_orphan_provider_processes": orphan_count == 0 and birth_verified,
        "birth_identities_verified": birth_verified,
        "orphan_count": orphan_count,
        "owned_process_count": len(records),
        "owned_processes": records,
    }


def _wait_cleanup_receipt(records: list[dict[str, Any]], timeout: float = 10.0) -> dict[str, Any]:
    """Allow asynchronously exiting provider children to reach a settled receipt."""

    cleanup = _cleanup_receipt(records)
    deadline = time.monotonic() + timeout
    while cleanup.get("birth_identities_verified") is True and cleanup.get("status") != "pass" and time.monotonic() < deadline:
        time.sleep(0.1)
        cleanup = _cleanup_receipt(records)
    return cleanup


def _manifest_is_stable(root: Path, manifest: list[dict[str, Any]]) -> bool:
    """Require the final evidence tree to remain unchanged before publishing."""

    return bool(manifest) and manifest == artifact_manifest(root)


def _helm_cleanup_ready(cleanup: Mapping[str, Any]) -> bool:
    return (
        cleanup.get("status") == "pass"
        and cleanup.get("provider_process_dead") is True
        and cleanup.get("process_group_dead") is True
        and cleanup.get("orphan_count") == 0
        and cleanup.get("shipper_stop_verified") is True
        and cleanup.get("canary_session_hidden") is True
        and cleanup.get("served_run_retired") is True
        and cleanup.get("source_retention_verified") is True
        and cleanup.get("isolation_removed") is True
    )


def run_omp_helm(args: argparse.Namespace) -> dict[str, object]:
    root = args.evidence_root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    if os.environ.get("LONGHOUSE_OMP_LIVE") not in {"1", "true", "yes", "on"}:
        raise RuntimeError("OMP Helm qualification requires explicit LONGHOUSE_OMP_LIVE opt-in")
    if not args.api_url or not args.agents_token:
        raise RuntimeError("OMP Helm qualification requires Runtime Host URL and token")
    # Keep a failed isolation quarantine outside the sealed evidence tree. It
    # may contain the disposable machine token until owned processes are dead;
    # the evidence scanner must never have to choose between preserving it and
    # accepting a secret-bearing artifact.
    isolation = new_qualification_isolation_root("omp-helm")
    provider_home = isolation / "home"
    longhouse_home = provider_home / ".longhouse"
    workspace = isolation / "workspace"
    try:
        provider_home.mkdir(mode=0o700, parents=True)
        workspace.mkdir(mode=0o700, parents=True)
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(provider_home),
                "LONGHOUSE_HOME": str(longhouse_home),
                "LONGHOUSE_OMP_BIN": str(args.provider_bin),
                "LONGHOUSE_OMP_HELM_URL": str(args.api_url),
                "LONGHOUSE_OMP_HELM_TOKEN": str(args.agents_token),
                "LONGHOUSE_ORIGIN_KIND": "test_or_canary",
                "LONGHOUSE_LAUNCH_ACTOR": "automation",
                "LONGHOUSE_LAUNCH_SURFACE": "qa",
                "XDG_DATA_HOME": str(provider_home / ".local" / "share"),
                "LONGHOUSE_OMP_DATA_DIR": str(provider_home / ".local" / "share" / "omp"),
                "LONGHOUSE_OMP_SESSION_DIR": str(provider_home / ".local" / "share" / "omp" / "sessions"),
            }
        )
    except BaseException:
        shutil.rmtree(isolation)
        raise

    sessions: list[ProviderPtySession] = []
    shipper = None
    owner_records: list[dict[str, Any]] = []
    source_claims: list[dict[str, Any]] = []
    controls: dict[str, object] = {}
    observation: dict[str, object] = {
        "observation_scope": "scenario",
        "omp_native_extension_channel_bound": False,
        "omp_agent_end_settlement_observed": False,
        "omp_native_archive_bound": False,
        "omp_transcript_shipper_started": False,
        "omp_transcript_flush_completed": False,
        "omp_runtime_transcript_converged": False,
        "runtime_control_identity_complete": False,
        "send_idle": False,
        "follow_up_native": False,
        "steer_active": False,
        "abort_native": False,
        "runtime_agents_api_controls": False,
        "terminate_owned": False,
        "cold_resume_exact_file": False,
        "stale_owner_refused": False,
        "settlement": {},
        "cleanup": {},
    }
    initial_flush: dict[str, Any] = {}
    final_flush: dict[str, Any] = {}
    terminal_flush: dict[str, Any] = {}
    served_run_inventory: dict[str, Any] = {}
    runtime_convergence: dict[str, Any] = {}
    current_session_id: str | None = None
    current_session_file: Path | None = None
    current_state: dict[str, Any] = {}
    retirement_claims: list[dict[str, Any]] = []
    source_generations: list[dict[str, Any]] = []

    try:
        shipper = start_transcript_shipper(
            "omp",
            args,
            home=provider_home,
            environment=env,
            evidence_root=root / "shipper",
            longhouse_home=longhouse_home,
        )
        observation["omp_transcript_shipper_started"] = shipper.receipt.get("ready") is True
        lifecycle.write_json(root / "transcript-shipper-receipt.json", shipper.receipt)
        initial_marker = f"OMP_HELM_INITIAL_{os.urandom(8).hex()}"
        first = ProviderPtySession.start(
            argv=_launch_argv(
                args,
                workspace=workspace,
                prompt=f"Reply with exactly {initial_marker}.",
            ),
            cwd=workspace,
            env=env,
            terminal_path=root / "omp-helm-terminal.raw",
            thread_name="omp-helm-qualification-terminal-drain",
        )
        sessions.append(first)
        current_state = _wait_state(longhouse_home)
        current_session_id = str(current_state["session_id"])
        current_session_file = Path(str(current_state["session_file"]))
        owner_records.append(
            _process_record(
                current_state.get("launcher_pid"),
                current_state.get("launcher_process_start_time"),
                "launcher",
                owner="initial",
            )
        )
        owner_records.append(
            _process_record(
                current_state.get("provider_pid"),
                current_state.get("provider_process_start_time"),
                "provider",
                owner="initial",
            )
        )
        _register_native_source(
            source_claims,
            label="initial",
            source_path=str(current_session_file),
            session_id=current_session_id,
            native_session_id=current_state.get("native_session_id"),
        )
        initial_session_file = current_session_file
        initial_row = _wait_native_marker(current_session_file, initial_marker)
        initial_channel_state = _wait_state(
            longhouse_home,
            session_id=current_session_id,
            predicate=lambda value: (value.get("agent_end_observed") is True and _turn_sequence(value) >= _turn_sequence(current_state)),
            timeout=90,
        )
        _wait_channel_terminal(
            longhouse_home,
            session_id=current_session_id,
            native_session_id=str(current_state.get("native_session_id") or ""),
            session_file=current_session_file,
            minimum_turn_seq=_turn_sequence(initial_channel_state),
        )
        initial_flush = shipper.flush("omp-helm-initial")
        if not _flush_receipt_complete(initial_flush):
            raise RuntimeError("OMP Helm transcript flush did not complete a bounded ship")
        initial_convergence = _runtime_convergence(
            str(args.api_url),
            str(args.agents_token),
            session_id=current_session_id,
            native_session_id=str(current_state.get("native_session_id") or ""),
            marker=initial_marker,
            flush=initial_flush,
            native_source_path=str(current_session_file),
        )
        initial_control_identity = _wait_runtime_control_identity(
            str(args.api_url),
            str(args.agents_token),
            home=longhouse_home,
            session_id=current_session_id,
            state=current_state,
        )
        initial_control_receipt = _control_identity_receipt(initial_control_identity)
        initial_convergence["control_identity"] = initial_control_receipt
        runtime_convergence = {"initial": initial_convergence}
        observation["omp_transcript_flush_completed"] = initial_convergence.get("status") == "pass"
        observation["omp_runtime_transcript_converged"] = observation["omp_transcript_flush_completed"]
        observation["runtime_control_identity"] = {"initial": initial_control_receipt}
        observation["runtime_convergence"] = runtime_convergence
        old_state = dict(current_state)
        old_native_id = str(current_state.get("native_session_id") or "")
        if first.alive() is not True:
            raise RuntimeError("OMP original owner was not live for stale-launcher proof")
        stale_launcher = ProviderPtySession.start(
            argv=_launch_argv(
                args,
                workspace=workspace,
                prompt="This launcher must be refused while the original OMP owner remains live.",
                resume_session=current_session_id,
            ),
            cwd=workspace,
            env=env,
            terminal_path=root / "omp-helm-stale-launcher.raw",
            thread_name="omp-helm-stale-launcher-terminal-drain",
        )
        sessions.append(stale_launcher)
        try:
            stale_launcher_returncode = stale_launcher.process.wait(timeout=15)
        except subprocess.TimeoutExpired as exc:
            stale_launcher.close()
            raise RuntimeError("OMP stale launcher did not refuse a live owner") from exc
        stale_launcher.close()
        stale_launcher_output = stale_launcher.terminal_path.read_text(encoding="utf-8", errors="replace")
        stale_launcher_evidence = {
            "returncode": stale_launcher_returncode,
            "terminal_path": str(stale_launcher.terminal_path),
            "original_owner_live": True,
            "owner_refusal_observed": (
                stale_launcher_returncode != 0
                and ("execution owner" in stale_launcher_output or "already attached" in stale_launcher_output)
            ),
        }
        observation["omp_native_extension_channel_bound"] = bool(
            current_state.get("ready") is True
            and current_state.get("connection_id")
            and current_state.get("lease_generation")
            and current_state.get("native_session_id")
            and current_state.get("session_file")
        )
        observation["channel_binding"] = _channel_binding_evidence(current_state)
        source_generations.append(
            {
                "label": "initial",
                "native_session_id": current_state.get("native_session_id"),
                "source_path": str(current_session_file),
                "marker": initial_marker,
                "controls": ["send", "follow_up", "steer", "abort"],
            }
        )

        send_marker = f"OMP_HELM_SEND_{os.urandom(8).hex()}"
        send_offset = _read_source_size(current_session_file)
        send = _run_engine(args.engine, "send", current_session_id, env, text=f"Reply with exactly {send_marker}.")
        send_row = _wait_native_marker(current_session_file, send_marker, minimum_offset=send_offset)
        send_evidence = _native_marker_evidence(
            send_row,
            current_session_file,
            marker=send_marker,
            minimum_offset=send_offset,
            native_session_id=str(current_state.get("native_session_id") or ""),
        )
        send_evidence.update(_channel_command_evidence(send, current_state))
        send_evidence.update({"observation_scope": "initial", "source_generation": "initial"})
        controls["send"] = {
            "action_label": "send",
            "prompt": f"Reply with exactly {send_marker}.",
            "state": dict(current_state),
            "command": send,
            "marker_row": send_row,
            "evidence": send_evidence,
        }
        observation["send_idle"] = (
            send_evidence["channel_ack_bound"] and send_evidence["native_source_bound"] and send_evidence["marker_count"] == 1
        )
        observation["send_evidence"] = send_evidence

        active_marker = f"OMP_HELM_ACTIVE_{os.urandom(8).hex()}"
        follow_up_marker = f"OMP_HELM_FOLLOW_UP_{os.urandom(8).hex()}"
        steer_marker = f"OMP_HELM_STEER_{os.urandom(8).hex()}"
        active_offset = _read_source_size(current_session_file)
        follow_up_active = _run_engine(
            args.engine,
            "send",
            current_session_id,
            env,
            text=f"Run the shell command `sleep 8`, then reply with exactly {active_marker}.",
        )
        active_state = _wait_state(
            longhouse_home,
            session_id=current_session_id,
            predicate=lambda value: value.get("phase") in {"running", "thinking"},
            timeout=30,
        )
        follow_up = _run_engine(
            args.engine,
            "send",
            current_session_id,
            env,
            text=f"Reply with exactly {follow_up_marker}.",
        )
        follow_up_row = _wait_native_marker(current_session_file, follow_up_marker, minimum_offset=active_offset)
        follow_up_evidence = _native_marker_evidence(
            follow_up_row,
            current_session_file,
            marker=follow_up_marker,
            minimum_offset=active_offset,
            native_session_id=str(current_state.get("native_session_id") or ""),
        )
        active_command_evidence = _channel_command_evidence(follow_up_active, active_state)
        follow_up_command_evidence = _channel_command_evidence(follow_up, current_state)
        follow_up_evidence["active_state"] = _redacted_state_snapshot(active_state)
        follow_up_evidence["active_command_bound"] = (
            active_state.get("phase") in {"running", "thinking"} and active_command_evidence["channel_ack_bound"]
        )
        follow_up_evidence["follow_up_delivery"] = follow_up_command_evidence["channel_ack_bound"]
        follow_up_evidence.update(_channel_command_evidence(follow_up, current_state))
        follow_up_evidence.update({"observation_scope": "initial", "source_generation": "initial"})
        controls["follow_up"] = {
            "action_label": "follow_up",
            "prompt": f"Reply with exactly {follow_up_marker}.",
            "state": dict(current_state),
            "active_action_label": "active_turn_setup",
            "active_command": follow_up_active,
            "active_state": dict(active_state),
            "command": follow_up,
            "marker_row": follow_up_row,
            "evidence": follow_up_evidence,
        }
        observation["follow_up_native"] = (
            follow_up_evidence["active_command_bound"]
            and follow_up_evidence["follow_up_delivery"]
            and follow_up_evidence["channel_ack_bound"]
            and follow_up_evidence["native_source_bound"]
            and follow_up_evidence["marker_count"] == 1
        )
        observation["follow_up_evidence"] = follow_up_evidence

        _wait_state(
            longhouse_home,
            session_id=current_session_id,
            predicate=lambda value: value.get("phase") == "idle",
            timeout=30,
        )
        steer_active_offset = _read_source_size(current_session_file)
        steer_active_marker = f"OMP_HELM_STEER_ACTIVE_{os.urandom(8).hex()}"
        steer_active = _run_engine(
            args.engine,
            "send",
            current_session_id,
            env,
            text=f"Run the shell command `sleep 8`, then reply with exactly {steer_active_marker}.",
        )
        steer_active_state = _wait_state(
            longhouse_home,
            session_id=current_session_id,
            predicate=lambda value: value.get("phase") in {"running", "thinking"},
            timeout=30,
        )
        steer = _run_engine(args.engine, "steer", current_session_id, env, text=f"Reply with exactly {steer_marker}.")
        steer_row = _wait_native_marker(current_session_file, steer_marker, minimum_offset=steer_active_offset)
        steer_evidence = _native_marker_evidence(
            steer_row,
            current_session_file,
            marker=steer_marker,
            minimum_offset=steer_active_offset,
            native_session_id=str(steer_active_state.get("native_session_id") or ""),
        )
        steer_active_command_evidence = _channel_command_evidence(steer_active, steer_active_state)
        steer_command_evidence = _channel_command_evidence(steer, steer_active_state)
        steer_evidence["active_state"] = _redacted_state_snapshot(steer_active_state)
        steer_evidence["active_command_bound"] = steer_active_command_evidence["channel_ack_bound"]
        steer_evidence["steer_command_bound"] = steer_command_evidence["channel_ack_bound"]
        steer_evidence.update(steer_command_evidence)
        steer_evidence.update({"observation_scope": "initial", "source_generation": "initial"})
        controls["steer"] = {
            "action_label": "steer",
            "prompt": f"Reply with exactly {steer_marker}.",
            "state": dict(steer_active_state),
            "active_action_label": "active_turn_setup",
            "active_command": steer_active,
            "active_state": dict(steer_active_state),
            "command": steer,
            "marker_row": steer_row,
            "evidence": steer_evidence,
        }
        observation["steer_active"] = (
            steer_evidence["active_command_bound"]
            and steer_evidence["channel_ack_bound"]
            and steer_evidence["native_source_bound"]
            and steer_evidence["marker_count"] == 1
        )
        observation["steer_evidence"] = steer_evidence

        abort_idle_state = _wait_state(
            longhouse_home,
            session_id=current_session_id,
            predicate=lambda value: value.get("phase") == "idle",
            timeout=30,
        )
        abort_marker = f"OMP_HELM_ABORT_{os.urandom(8).hex()}"
        active_for_abort = _run_engine(
            args.engine,
            "send",
            current_session_id,
            env,
            text=f"Run the shell command `sleep 15`, then reply with exactly {abort_marker}.",
        )
        abort_active_state = _wait_state(
            longhouse_home,
            session_id=current_session_id,
            predicate=lambda value: (
                value.get("phase") in {"running", "thinking"} and _turn_sequence(value) > _turn_sequence(abort_idle_state)
            ),
            timeout=30,
        )
        abort_offset = _read_source_size(current_session_file)
        abort = _run_engine(args.engine, "abort", current_session_id, env)
        abort_end, abort_channel_state = _wait_channel_terminal(
            longhouse_home,
            session_id=current_session_id,
            native_session_id=str(current_state.get("native_session_id") or ""),
            session_file=current_session_file,
            minimum_turn_seq=_turn_sequence(abort_active_state),
        )
        abort_evidence = _channel_terminal_evidence(
            abort_end,
            abort_channel_state,
            native_session_id=str(current_state.get("native_session_id") or ""),
            session_file=current_session_file,
        )
        abort_evidence["channel_ack_bound"] = abort.get("accepted") is True and observation["channel_binding"]["ready"] is True
        abort_evidence.update({"observation_scope": "initial", "source_generation": "initial"})
        controls["abort"] = {
            "action_label": "abort",
            "state": dict(current_state),
            "active_action_label": "abort_turn_setup",
            "active_command": active_for_abort,
            "command": abort,
            "agent_end": abort_end,
            "evidence": abort_evidence,
        }
        observation["abort_native"] = abort_evidence["channel_ack_bound"] and abort_evidence["channel_source_bound"]
        observation["abort_evidence"] = abort_evidence

        first.submit_line("/new")
        replaced_state = _wait_state(
            longhouse_home,
            session_id=current_session_id,
            predicate=lambda value: bool(value.get("native_session_id"))
            and value.get("native_session_id") != old_native_id
            and value.get("ready") is True,
            timeout=60,
        )
        stale = _stale_frame(old_state, text="stale OMP owner must be refused")
        controls["stale_owner"] = {
            "second_launcher": stale_launcher_evidence,
            "response": stale,
            "old_state": old_state,
            "new_state": replaced_state,
        }
        observation["stale_owner_refused"] = (
            stale_launcher_evidence["owner_refusal_observed"]
            and stale.get("ok") is False
            and (stale.get("error") or {}).get("code") == "stale_channel"
        )
        observation["stale_owner_evidence"] = {
            "second_launcher": stale_launcher_evidence,
            "error_code": (stale.get("error") or {}).get("code"),
            "old_native_session_id": old_state.get("native_session_id"),
            "new_native_session_id": replaced_state.get("native_session_id"),
        }
        current_state = replaced_state
        current_session_file = Path(str(replaced_state["session_file"]))
        current_native_id = str(replaced_state["native_session_id"])
        context_phrase = f"OMP_HELM_CONTEXT_{os.urandom(8).hex()}"
        _register_native_source(
            source_claims,
            label="replacement",
            source_path=str(current_session_file),
            session_id=current_session_id,
            native_session_id=current_native_id,
        )
        replacement_control_identity = _wait_runtime_control_identity(
            str(args.api_url),
            str(args.agents_token),
            home=longhouse_home,
            session_id=current_session_id,
            state=replaced_state,
        )
        replacement_control_receipt = _control_identity_receipt(replacement_control_identity)
        runtime_control_identity = observation["runtime_control_identity"]
        if isinstance(runtime_control_identity, dict):
            runtime_control_identity["replacement"] = replacement_control_receipt
        replacement_marker = f"OMP_HELM_REPLACEMENT_{os.urandom(8).hex()}"
        source_generations.append(
            {
                "label": "replacement",
                "native_session_id": current_native_id,
                "source_path": str(current_session_file),
                "marker": replacement_marker,
                "controls": ["replacement", "cold_resume"],
            }
        )
        replacement_offset = _read_source_size(current_session_file)
        replacement = _run_engine(
            args.engine,
            "send",
            current_session_id,
            env,
            text=(f"Remember this context phrase: {context_phrase}. Then reply with exactly {replacement_marker}."),
        )
        replacement_row = _wait_native_marker(
            current_session_file,
            replacement_marker,
            minimum_offset=replacement_offset,
        )
        replacement_evidence = _native_marker_evidence(
            replacement_row,
            current_session_file,
            marker=replacement_marker,
            minimum_offset=replacement_offset,
            native_session_id=current_native_id,
        )
        context_seed_row = _wait_native_marker(
            current_session_file,
            context_phrase,
            minimum_offset=replacement_offset,
            role="user",
        )
        context_seed_evidence = _native_marker_evidence(
            context_seed_row,
            current_session_file,
            marker=context_phrase,
            minimum_offset=replacement_offset,
            native_session_id=current_native_id,
            role="user",
        )
        replacement_evidence.update(_channel_command_evidence(replacement, replaced_state))
        replacement_evidence.update({"observation_scope": "replacement", "source_generation": "replacement"})
        controls["replacement"] = {
            "action_label": "replacement_send",
            "prompt": f"Remember this context phrase: {context_phrase}. Then reply with exactly {replacement_marker}.",
            "state": dict(replaced_state),
            "command": replacement,
            "marker_row": replacement_row,
            "context_seed_row": context_seed_row,
            "evidence": replacement_evidence,
            "context_evidence": context_seed_evidence,
            "control_identity": replacement_control_receipt,
        }
        observation["native_replacement_bound"] = (
            replacement_evidence["channel_ack_bound"]
            and replaced_state.get("native_session_id") == current_native_id
            and replacement_evidence["native_source_bound"]
        )
        observation["replacement_evidence"] = replacement_evidence

        owner_records.append(
            _process_record(
                current_state.get("launcher_pid"),
                current_state.get("launcher_process_start_time"),
                "launcher",
                owner="replacement",
            )
        )
        owner_records.append(
            _process_record(
                current_state.get("provider_pid"),
                current_state.get("provider_process_start_time"),
                "provider",
                owner="replacement",
            )
        )
        terminate = _run_engine(args.engine, "terminate", current_session_id, env)
        stopped = _wait_stopped(longhouse_home, current_session_id)
        first.process.wait(timeout=15)
        controls["terminate"] = {
            "action_label": "terminate",
            "state": dict(replaced_state),
            "command": terminate,
            "stopped_state": stopped,
            "control_identity": replacement_control_receipt,
        }
        observation["terminate_owned"] = terminate.get("accepted") is True and stopped.get("terminal_reason") == "remote_terminate"

        resume_marker = f"OMP_HELM_RESUME_{os.urandom(8).hex()}"
        resume_offset = _read_source_size(current_session_file)
        pre_resume_source = root / "pre-resume-native-source.raw"
        pre_resume_source.write_bytes(current_session_file.read_bytes())
        resume_prompt = f"Without reading any files, reply with the context phrase you remember followed by exactly {resume_marker}."
        resumed = ProviderPtySession.start(
            argv=_launch_argv(
                args,
                workspace=workspace,
                prompt=resume_prompt,
                resume_session=current_session_id,
            ),
            cwd=workspace,
            env=env,
            terminal_path=root / "omp-helm-resume-terminal.raw",
            thread_name="omp-helm-resume-qualification-terminal-drain",
        )
        sessions.append(resumed)
        resume_state = _wait_state(longhouse_home, session_id=current_session_id)
        resume_control_identity = _wait_runtime_control_identity(
            str(args.api_url),
            str(args.agents_token),
            home=longhouse_home,
            session_id=current_session_id,
            state=resume_state,
        )
        resume_control_receipt = _control_identity_receipt(resume_control_identity)
        runtime_control_identity = observation["runtime_control_identity"]
        if isinstance(runtime_control_identity, dict):
            runtime_control_identity["cold_resume"] = resume_control_receipt
        resume_file = Path(str(resume_state["session_file"]))
        _register_native_source(
            source_claims,
            label="cold_resume",
            source_path=str(resume_file),
            session_id=current_session_id,
            native_session_id=resume_state.get("native_session_id"),
        )
        source_generations.append(
            {
                "label": "cold_resume",
                "native_session_id": resume_state.get("native_session_id"),
                "source_path": str(resume_file),
                "marker": resume_marker,
                "controls": ["cold_resume"],
            }
        )
        resume_row = _wait_native_marker(resume_file, resume_marker, minimum_offset=resume_offset)
        resume_input_row = _wait_native_marker(
            resume_file,
            resume_prompt,
            minimum_offset=resume_offset,
            role="user",
        )
        resume_context_row = _wait_native_marker(
            resume_file,
            context_phrase,
            minimum_offset=resume_offset,
            role="assistant",
        )
        resume_terminal, resume_channel_state = _wait_channel_terminal(
            longhouse_home,
            session_id=current_session_id,
            native_session_id=str(resume_state.get("native_session_id") or ""),
            session_file=resume_file,
            minimum_turn_seq=_turn_sequence(resume_state),
        )
        resume_marker_evidence = _native_marker_evidence(
            resume_row,
            resume_file,
            marker=resume_marker,
            minimum_offset=resume_offset,
            native_session_id=str(resume_state.get("native_session_id") or ""),
        )
        resume_input_evidence = _native_marker_evidence(
            resume_input_row,
            resume_file,
            marker=resume_prompt,
            minimum_offset=resume_offset,
            native_session_id=str(resume_state.get("native_session_id") or ""),
            role="user",
        )
        resume_context_evidence = _native_marker_evidence(
            resume_context_row,
            resume_file,
            marker=context_phrase,
            minimum_offset=resume_offset,
            native_session_id=str(resume_state.get("native_session_id") or ""),
        )
        context_recalled = (
            context_seed_evidence["native_source_bound"]
            and context_seed_evidence["marker_count"] == 1
            and resume_context_evidence["native_source_bound"]
            and resume_context_evidence["marker_count"] == 1
            and resume_input_evidence["native_source_bound"]
            and resume_input_evidence["marker_count"] == 1
            and context_phrase not in resume_prompt
        )
        resume_terminal_evidence = _channel_terminal_evidence(
            resume_terminal,
            resume_channel_state,
            native_session_id=str(resume_state.get("native_session_id") or ""),
            session_file=resume_file,
        )
        resume_marker_evidence.update({"observation_scope": "cold_resume", "source_generation": "cold_resume"})
        resume_terminal_evidence.update({"observation_scope": "cold_resume", "source_generation": "cold_resume"})
        resume_owner_records = [
            _process_record(
                resume_state.get("launcher_pid"),
                resume_state.get("launcher_process_start_time"),
                "launcher",
                owner="cold_resume",
            ),
            _process_record(
                resume_state.get("provider_pid"),
                resume_state.get("provider_process_start_time"),
                "provider",
                owner="cold_resume",
            ),
        ]
        owner_records.extend(resume_owner_records)
        observation["cold_resume_exact_file"] = (
            resume_state.get("native_session_id") == current_native_id
            and resume_state.get("session_file") == str(current_session_file)
            and resume_file == current_session_file
            and resume_row.get("_source_offset", -1) >= 0
            and resume_terminal.get("type") == "agent_end"
        )
        observation["cold_resume_evidence"] = {
            **resume_marker_evidence,
            "context_phrase": context_phrase,
            "resume_prompt": resume_prompt,
            "pre_resume_source_path": pre_resume_source.name,
            "context_recalled": context_recalled,
            "context_marker_count": resume_context_evidence["marker_count"],
            "context_marker_present": resume_context_evidence["marker_count"] >= 1,
            "context_marker_exactly_once": resume_context_evidence["marker_count"] == 1,
            "context_seed": context_seed_evidence,
            "context_resume": resume_context_evidence,
            "resume_input": resume_input_evidence,
            "native_source_bound": resume_marker_evidence["native_source_bound"],
            "channel_terminal_bound": resume_terminal_evidence["channel_source_bound"],
            "terminal": resume_terminal_evidence["terminal"],
            "exact_file": (
                resume_state.get("native_session_id") == current_native_id
                and resume_state.get("session_file") == str(current_session_file)
                and resume_file == current_session_file
            ),
        }
        controls["cold_resume"] = {
            "action_label": "cold_resume",
            "prompt": resume_prompt,
            "state": dict(resume_state),
            "marker_row": resume_row,
            "context_row": resume_context_row,
            "prompt_evidence": resume_input_evidence,
            "terminal": resume_terminal_evidence["terminal"],
            "marker_evidence": resume_marker_evidence,
            "context_evidence": resume_context_evidence,
            "evidence": resume_marker_evidence,
            "terminal_evidence": resume_terminal_evidence,
            "control_identity": resume_control_receipt,
        }
        settled_state = _wait_state(
            longhouse_home,
            session_id=current_session_id,
            predicate=lambda value: (
                value.get("phase") == "idle"
                and value.get("native_session_id") == str(resume_state.get("native_session_id") or "")
                and value.get("session_file") == str(resume_file)
                and value.get("updated_at") != resume_state.get("updated_at")
            ),
        )
        final_flush = shipper.flush("omp-helm-final")
        if not _flush_receipt_complete(final_flush):
            raise RuntimeError("OMP Helm cold-resume transcript flush did not complete a bounded ship")
        final_convergence = _runtime_convergence(
            str(args.api_url),
            str(args.agents_token),
            session_id=current_session_id,
            native_session_id=str(resume_state.get("native_session_id") or ""),
            marker=resume_marker,
            flush=final_flush,
            native_source_path=str(resume_file),
        )
        final_control_state = _wait_state(
            longhouse_home,
            session_id=current_session_id,
            predicate=lambda value: (
                value.get("phase") == "idle"
                and value.get("native_session_id") == str(resume_state.get("native_session_id") or "")
                and value.get("session_file") == str(resume_file)
            ),
        )
        final_control_identity = _wait_runtime_control_identity(
            str(args.api_url),
            str(args.agents_token),
            home=longhouse_home,
            session_id=current_session_id,
            state=final_control_state,
        )
        final_control_receipt = _control_identity_receipt(final_control_identity)
        final_convergence["control_identity"] = final_control_receipt
        runtime_control_identity = observation["runtime_control_identity"]
        if isinstance(runtime_control_identity, dict):
            runtime_control_identity["final"] = final_control_receipt
            observation["runtime_control_identity_complete"] = all(
                label in runtime_control_identity for label in ("initial", "replacement", "cold_resume", "final")
            )
        current_state = dict(final_control_state)
        runtime_convergence["final"] = final_convergence
        observation["omp_transcript_flush_completed"] = (
            observation["omp_transcript_flush_completed"] is True and final_convergence.get("status") == "pass"
        )
        observation["omp_runtime_transcript_converged"] = (
            observation["omp_runtime_transcript_converged"] is True and final_convergence.get("status") == "pass"
        )
        observation["runtime_convergence"] = runtime_convergence
        lifecycle.write_json(root / "runtime-convergence-receipt.json", runtime_convergence)
        observation["settlement"] = _native_settlement(
            resume_file,
            channel_state=settled_state,
            native_session_id=str(resume_state.get("native_session_id") or ""),
        )
        settlement = observation["settlement"]
        if isinstance(settlement, Mapping):
            observation["omp_agent_end_settlement_observed"] = settlement.get("agent_end_terminal") is True
            observation["omp_native_archive_bound"] = settlement.get("native_archive_bound") is True
            observation["omp_native_extension_channel_bound"] = settlement.get("agent_end_evidence_shape") is True
        current_state = dict(final_control_state)
        final_terminate = _run_engine(args.engine, "terminate", current_session_id, env)
        final_stopped = _wait_stopped(longhouse_home, current_session_id)
        resumed.process.wait(timeout=15)
        retirement_claims = [
            {
                "session_id": str(current_session_id or ""),
                "run_id": str(current_state.get("run_id") or ""),
                "state": "terminal",
            }
        ]
        if shipper is not None:
            try:
                terminal_flush = shipper.flush("omp-helm-terminal-retirement")
            except Exception as exc:  # noqa: BLE001 - preserve terminal-delivery evidence
                terminal_flush = {"status": "fail", "error": f"{type(exc).__name__}: {exc}"}
        served_run_inventory = _wait_served_run_retirement(
            str(args.api_url),
            str(args.agents_token),
            str(current_session_id or ""),
            retirement_claims,
        )
        controls["final_terminate"] = {
            "action_label": "final_terminate",
            "state": dict(final_control_state),
            "command": final_terminate,
            "stopped_state": final_stopped,
            "control_identity": final_control_receipt,
        }
    finally:
        for provider_session in sessions:
            if provider_session.alive():
                try:
                    os.killpg(provider_session.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    provider_session.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(provider_session.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            provider_session.close()
        cleanup_errors: list[str] = []
        cleanup_flush: dict[str, Any] = {}
        shipper_stop: dict[str, Any] = {}
        if shipper is not None:
            try:
                cleanup_flush = shipper.flush("omp-helm-cleanup")
            except Exception as exc:  # noqa: BLE001 - cleanup evidence must remain visible
                cleanup_errors.append(f"{type(exc).__name__}: {exc}")
            try:
                shipper_stop = shipper.stop()
            except Exception as exc:  # noqa: BLE001 - cleanup evidence must remain visible
                cleanup_errors.append(f"{type(exc).__name__}: {exc}")
                shipper_stop = {"status": "fail", "error": f"{type(exc).__name__}: {exc}"}
        lifecycle.write_json(root / "transcript-shipper-receipt.json", shipper_stop or {"status": "fail", "stopped": False})
        flush_receipt: dict[str, Any] = {
            "initial": dict(initial_flush),
            "final": dict(final_flush),
            "terminal": dict(terminal_flush),
            "cleanup": cleanup_flush,
        }
        lifecycle.write_json(root / "transcript-flush-receipt.json", flush_receipt)
        cleanup = (
            _wait_cleanup_receipt(owner_records)
            if owner_records
            else {
                "status": "fail",
                "provider_process_dead": False,
                "process_group_dead": False,
                "orphan_count": 1,
            }
        )
        dispatch_session_id = str(current_session_id or "")
        dispatch_run_id = str(current_state.get("run_id") or "")
        if not served_run_inventory:
            retirement_claims = retirement_claims or [
                {
                    "session_id": dispatch_session_id,
                    "run_id": dispatch_run_id,
                    "state": "terminal",
                }
            ]
            served_run_inventory = lifecycle._served_run_inventory_evidence(
                args.api_url,
                args.agents_token,
                dispatch_session_id,
                retirement_claims,
            )
        session_retirement = retire_qualification_session(
            args.api_url,
            args.agents_token,
            str(current_session_id or ""),
            provider="omp",
        )
        cleanup["session_retirement"] = session_retirement
        cleanup["served_run_inventory"] = served_run_inventory
        cleanup["served_run_retired"] = (
            served_run_inventory.get("retired") is True
            and served_run_inventory.get("session_id") == dispatch_session_id
            and served_run_inventory.get("active_run_count") == 0
        )
        cleanup["dispatch_session_id"] = dispatch_session_id
        cleanup["dispatch_run_id"] = dispatch_run_id
        cleanup["process_stop"] = {
            "verified": cleanup.get("provider_process_dead") is True
            and cleanup.get("process_group_dead") is True
            and cleanup.get("orphan_count") == 0,
        }
        cleanup["shipper_stop"] = shipper_stop
        cleanup["shipper_stop_verified"] = (
            shipper_stop.get("stopped") is True
            and shipper_stop.get("process_dead") is True
            and shipper_stop.get("process_group_dead") is True
        )
        cleanup["cleanup_flush"] = cleanup_flush
        cleanup["cleanup_errors"] = cleanup_errors
        cleanup["isolation_path"] = str(isolation)
        if not cleanup["shipper_stop_verified"] or cleanup_errors:
            cleanup["status"] = "fail"
        cleanup["canary_session_hidden"] = _exact_session_retirement(session_retirement, current_session_id)
        if not (
            cleanup["status"] == "pass"
            and cleanup["canary_session_hidden"] is True
            and cleanup["served_run_retired"] is True
            and cleanup["process_stop"]["verified"] is True
        ):
            cleanup["status"] = "fail"
        cleanup = redact_state_for_evidence(cleanup)
        observation["cleanup"] = cleanup
        owners_stopped = cleanup.get("provider_process_dead") is True and cleanup.get("process_group_dead") is True
        if owners_stopped:
            try:
                retained_sources = lifecycle._retain_claim_sources(root, source_claims, env, complete=True)
            except Exception as exc:  # noqa: BLE001 - preserve cleanup evidence on producer failure
                retained_sources = [
                    {
                        "source": source_claim.get("source_path"),
                        "kind": "source_path",
                        "retained": False,
                        "complete": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    for source_claim in source_claims
                ]
                lifecycle.write_json(root / "provider-source-retention.json", {"sources": retained_sources})
        else:
            retained_sources = [
                {
                    "source": source_claim.get("source_path"),
                    "kind": "source_path",
                    "retained": False,
                    "complete": False,
                    "error": "provider owners did not stop; authoritative source remains in isolation",
                }
                for source_claim in source_claims
            ]
            lifecycle.write_json(root / "provider-source-retention.json", {"sources": retained_sources})
        source_retention_verified = bool(retained_sources) and all(
            item.get("retained") is True and item.get("complete") is True and isinstance(item.get("path"), str) and bool(item.get("path"))
            for item in retained_sources
        )
        cleanup["source_retention_verified"] = source_retention_verified
        cleanup["source_retention"] = {
            "verified": source_retention_verified,
            "source_count": len(retained_sources),
            "retained_source_count": sum(item.get("retained") is True for item in retained_sources),
        }
        if not source_retention_verified:
            cleanup["status"] = "fail"
        isolation_removed = _remove_isolation_after_source_retention(
            isolation,
            source_retention_verified=source_retention_verified,
            cleanup=cleanup,
        )
        cleanup["isolation_removed"] = isolation_removed
        if not isolation_removed:
            cleanup["status"] = "fail"
        lifecycle.write_json(root / "cleanup-receipt.json", cleanup)
        retained_by_source = {
            str(item["source"]): str(item["path"])
            for item in retained_sources
            if item.get("retained") is True and isinstance(item.get("source"), str) and isinstance(item.get("path"), str)
        }
        for generation in source_generations:
            generation["retained_path"] = retained_by_source.get(str(generation.get("source_path") or ""))
        for record in controls.values():
            if not isinstance(record, dict):
                continue
            evidence = record.get("evidence")
            if isinstance(evidence, dict):
                evidence["retained_source_path"] = retained_by_source.get(str(evidence.get("source_path") or ""))
            for evidence_key in ("context_evidence", "marker_evidence", "prompt_evidence", "terminal_evidence"):
                nested = record.get(evidence_key)
                if isinstance(nested, dict):
                    nested["retained_source_path"] = retained_by_source.get(str(nested.get("source_path") or ""))
        if isinstance(observation.get("cold_resume_evidence"), dict):
            observation["cold_resume_evidence"]["retained_source_path"] = retained_by_source.get(
                str(observation["cold_resume_evidence"].get("source_path") or "")
            )
        settlement = observation.get("settlement")
        settlement = settlement if isinstance(settlement, dict) else {}
        native_source = settlement.get("session_file") or ""
        settlement["retained_source_path"] = retained_by_source.get(str(native_source))
        settlement["retained_source"] = native_source
        settlement["retained_sources"] = retained_by_source
        observation["native_source_generations"] = source_generations
        observation["provider_source_retention"] = retained_sources
        observation["settlement"] = settlement

    runtime_control_labels = ("send", "follow_up", "steer", "abort", "replacement", "terminate", "final_terminate")
    runtime_control_paths: dict[str, str | None] = {}
    for label in runtime_control_labels:
        record = controls.get(label)
        command = record.get("command") if isinstance(record, dict) else None
        runtime_control_paths[label] = command.get("transport") if isinstance(command, dict) else None
    observation["runtime_agents_api_controls"] = bool(runtime_control_paths) and all(
        path == "runtime_host_agents_api" for path in runtime_control_paths.values()
    )
    observation["runtime_agents_api_control_paths"] = runtime_control_paths

    observation["omp_owned_processes_dead"] = cleanup.get("provider_process_dead") is True and cleanup.get("process_group_dead") is True
    binary_receipt = {
        "provider": "omp",
        "path": str(args.provider_bin),
        "sha256": sha256_file(args.provider_bin),
        "version": args.provider_version,
    }
    cleanup_ready = _helm_cleanup_ready(cleanup)
    observation["final_evidence"] = {
        "cleanup_ready": cleanup_ready,
        "source_retention_verified": cleanup.get("source_retention_verified") is True,
        "isolation_removed": cleanup.get("isolation_removed") is True,
        "cleanup_receipt_written": True,
    }
    lifecycle.write_json(root / "provider-binary-receipt.json", binary_receipt)
    lifecycle.write_json(root / "omp-native-settlement-receipt.json", observation["settlement"])
    lifecycle.write_json(
        root / "omp-helm-receipt.json",
        {
            "managed_transport": "omp_helm_channel",
            "state": redact_state_for_evidence(current_state),
            "old_state": redact_state_for_evidence(old_state if "old_state" in locals() else {}),
            "controls": redact_state_for_evidence(controls),
            "observation": observation,
        },
    )
    lifecycle.write_json(root / "cleanup-receipt.json", cleanup)
    assertions = omp_helm_lifecycle_assertions(observation)
    final_manifest = artifact_manifest(root)
    manifest_stable = _manifest_is_stable(root, final_manifest)
    result_status = _helm_result_status(
        assertions,
        getattr(args, "variant", None),
        cleanup_ready=cleanup_ready,
        manifest_stable=manifest_stable,
    )
    result = {
        "schema_version": 1,
        "artifact_kind": "omp_helm_lifecycle_result",
        "producer": REGISTRATION.to_dict(),
        "provider": "omp",
        "variant": None,
        "observation_scope": "scenario",
        "scenario_id": SCENARIO_ID,
        "scenario_revision": REGISTRATION.scenario_revision,
        "evidence_class": "live_token",
        "generated_at": now(),
        "status": result_status,
        "assertions": assertions,
        "provider_binary": binary_receipt,
        "observation": observation,
        "final_evidence": {
            **dict(observation["final_evidence"]),
            "manifest_stable": manifest_stable,
            "manifest_entry_count": len(final_manifest),
        },
        "artifact_manifest": final_manifest,
    }
    lifecycle.write_json(root / "result.json", result)
    return result


def _request_args(
    request_path: Path,
    output_root: Path,
    *,
    request: Mapping[str, Any] | None = None,
    variant: str | None = None,
    agents_token: str | None = None,
) -> argparse.Namespace:
    request = dict(request) if request is not None else json.loads(request_path.read_text(encoding="utf-8"))
    runtime_token = agents_token if agents_token is not None else os.environ.get("LONGHOUSE_RUNTIME_AGENTS_TOKEN")
    return argparse.Namespace(
        evidence_root=output_root,
        repo_root=Path(__file__).resolve().parents[3],
        provider_bin=Path(str(request["provider_bin"])),
        provider_version=str(request["expected_provider_version"]),
        engine=Path(os.environ.get("LONGHOUSE_ENGINE_BIN") or ""),
        longhouse_cli=Path(os.environ.get("LONGHOUSE_CLI_BIN") or "longhouse"),
        api_url=os.environ.get("LONGHOUSE_RUNTIME_API_URL"),
        agents_token=runtime_token,
        model=os.environ.get("LONGHOUSE_OMP_QUALIFICATION_MODEL", ""),
        variant=variant,
    )


def run(request_path: Path, output_root: Path) -> dict[str, object]:
    request = identity.load_request(
        request_path,
        provider="omp",
        profile=PROFILE,
        version_grammar=_PROFILE.version_grammar,
    )
    runtime_token = str(os.environ.get("LONGHOUSE_RUNTIME_AGENTS_TOKEN") or "")

    def execute(binary: Path, evidence_root: Path):
        # The semantic entrypoint executes every registered cell; selection is
        # only meaningful for the direct CLI path. The validated request owns
        # the provider binary/version, while the runtime token remains secret.
        run_args = _request_args(request_path, evidence_root, request=request, agents_token=runtime_token)
        run_args.provider_bin = binary
        result = run_omp_helm(run_args)
        observation = dict(result.get("observation") or {})
        model_evidence = omp_native_model_evidence(
            evidence_root,
            source_canary=PROFILE,
            api_key_configured=bool(str(os.environ.get("OPENROUTER_API_KEY") or "").strip()),
            qualification_model=run_args.model,
        )
        if model_evidence is not None:
            observation["live_model_evidence"] = model_evidence
        assertions = omp_helm_lifecycle_assertions(observation)
        semantic_assertions = tuple(
            semantic.SemanticAssertion(
                assertion,
                AssertionOutcome.PASS if passed else AssertionOutcome.SEMANTIC_FAIL,
                EvidenceClass.LIVE_TOKEN,
            )
            for assertion, passed in assertions.items()
        )
        return (
            observation,
            semantic_assertions,
            tuple(
                dict.fromkeys(
                    value
                    for value in (
                        runtime_token,
                        str(os.environ.get("OPENROUTER_API_KEY") or "").strip(),
                    )
                    if value
                )
            ),
        )

    return semantic.run_semantic_profile(
        request_path,
        output_root,
        profile=_PROFILE,
        assertion_ids=ASSERTIONS,
        executor=execute,
        oracle_source=Path(__file__),
        scenario_revision=REGISTRATION.scenario_revision,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_factory_provider_arguments(parser, variants=_VARIANTS)
    parser.add_argument("--model", required=False)
    parser.add_argument("--api-url", default=os.environ.get("LONGHOUSE_RUNTIME_API_URL"))
    parser.add_argument("--agents-token", default=os.environ.get("LONGHOUSE_RUNTIME_AGENTS_TOKEN"))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    try:
        result = run_omp_helm(args)
    except Exception as exc:  # noqa: BLE001 - preserve a typed harness failure
        result = {
            "schema_version": 1,
            "artifact_kind": "omp_helm_lifecycle_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "omp",
            "variant": None,
            "observation_scope": "scenario",
            "scenario_id": SCENARIO_ID,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": now(),
            "status": "fail",
            "failure_code": "omp_helm_lifecycle_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        args.evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        result["artifact_manifest"] = artifact_manifest(args.evidence_root)
        lifecycle.write_json(args.evidence_root / "result.json", result)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
