#!/usr/bin/env python3
"""Read-only Phase 7 deep-health probe for canonical session state."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

TIMEOUT_SECONDS = 20


def _get_json(api_url: str, token: str | None, path: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}{path}",
        headers={
            "Accept": "application/json",
            "User-Agent": "longhouse-session-state-deep-health/1.0",
            **({"X-Agents-Token": token} if token else {}),
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} returned a non-object JSON payload")
    return payload


def _get_first_machine_delta(api_url: str, token: str | None) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/api/agents/sessions/stream?limit=1&skip_initial_replay=false",
        headers={
            "Accept": "text/event-stream",
            "User-Agent": "longhouse-session-state-deep-health/1.0",
            **({"X-Agents-Token": token} if token else {}),
        },
    )
    event = ""
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").rstrip("\r\n")
            if line.startswith("event:"):
                event = line.partition(":")[2].strip()
            elif line.startswith("data:") and event == "session_delta":
                payload = json.loads(line.partition(":")[2].strip())
                if not isinstance(payload, dict):
                    raise ValueError("machine session stream returned a non-object delta")
                return payload
    raise ValueError("machine session stream returned no initial session delta")


def _session_ids(listing: dict[str, Any], limit: int) -> list[str]:
    rows = listing.get("sessions") or listing.get("items") or []
    if not isinstance(rows, list):
        return []
    values: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = str(row.get("id") or row.get("session_id") or "").strip()
        if value:
            values.append(value)
        if len(values) >= limit:
            break
    return values


def _common_signature(payload: dict[str, Any], *, compact: bool) -> dict[str, Any]:
    state = payload if compact else payload.get("session_state")
    if not isinstance(state, dict):
        raise ValueError("session surface is missing canonical state")
    presentation = state.get("presentation") if isinstance(state.get("presentation"), dict) else {}
    control = state.get("control") if isinstance(state.get("control"), dict) else {}
    actions = control.get("actions") if isinstance(control.get("actions"), dict) else {}
    run = state.get("run") if isinstance(state.get("run"), dict) else None
    interaction = state.get("pending_interaction") if isinstance(state.get("pending_interaction"), dict) else None
    return {
        "commit_seq": str(state.get("commit_seq") if not compact else payload.get("commit_seq")),
        "state_contract_version": state.get("state_contract_version"),
        "presentation_policy_version": state.get("presentation_policy_version"),
        "mode": state.get("mode"),
        "primary_key": (presentation.get("primary") or {}).get("key") if isinstance(presentation.get("primary"), dict) else None,
        "access_key": (presentation.get("access") or {}).get("key") if isinstance(presentation.get("access"), dict) else None,
        "activity": state.get("activity"),
        "control": {
            "ownership": control.get("ownership"),
            "connection": control.get("connection"),
            "terminate": actions.get("terminate"),
            "reattach": actions.get("reattach"),
        },
        "run": ({"id": run.get("id"), "lifecycle": run.get("lifecycle")} if run is not None else None),
        "pending_interaction": (
            {
                "id": interaction.get("id"),
                "kind": interaction.get("kind"),
                "opened_at": interaction.get("opened_at"),
                "can_respond": interaction.get("can_respond"),
            }
            if interaction is not None
            else None
        ),
    }


def compare_live_surfaces(*, detail: dict[str, Any], machine_delta: dict[str, Any]) -> dict[str, Any]:
    api = _common_signature(detail, compact=False)
    machine = _common_signature(machine_delta, compact=True)
    mismatched = sorted(key for key in api if key != "commit_seq" and api[key] != machine[key])
    if mismatched:
        status = "diverged"
    elif api["commit_seq"] == machine["commit_seq"]:
        status = "matched_same_commit"
    else:
        status = "matched_equivalent_different_commit"
    return {
        "status": status,
        "api_detail": api,
        "machine_stream": machine,
        "mismatched_fields": mismatched,
    }


def assess(
    *,
    reducer_health: dict[str, Any],
    diagnostics: list[dict[str, Any]],
    build: dict[str, Any] | None,
    required_providers: set[str],
    require_canonical: bool,
    live_surface_parity: dict[str, Any] | None,
    allow_cross_commit_equivalence: bool,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if require_canonical:
        if reducer_health.get("served_path") != "canonical_session_detail":
            errors.append("canonical detail serving is not active")
        if reducer_health.get("authorization_path") != "provider_scoped_canonical_control":
            errors.append("canonical command authorization is not active")
    contract = reducer_health.get("contract")
    if not isinstance(contract, dict) or not contract.get("fingerprint"):
        errors.append("session-state contract fingerprint is missing")
        contract = {}

    provider_counts: dict[str, int] = {}
    sessions: list[dict[str, Any]] = []
    if not diagnostics:
        errors.append("no sessions were sampled; deep health cannot pass vacuously")
    for diagnostic in diagnostics:
        session_id = str(diagnostic.get("session_id") or "")
        provider = str(diagnostic.get("provider") or "unknown")
        provider_counts[provider] = provider_counts.get(provider, 0) + 1
        explain = diagnostic.get("explain")
        comparison = diagnostic.get("comparison")
        if not isinstance(explain, dict):
            errors.append(f"{session_id}: explain payload missing")
            continue
        projection = explain.get("projection_parity")
        if require_canonical and (not isinstance(projection, dict) or projection.get("status") != "matched"):
            errors.append(f"{session_id}: canonical compact projection is not matched")
        if isinstance(comparison, dict) and comparison.get("status") == "different":
            errors.append(f"{session_id}: reducer comparison has unexplained deltas")
        if explain.get("state_contract_version") != contract.get("state_contract_version"):
            errors.append(f"{session_id}: state contract version diverged")
        if explain.get("presentation_policy_version") != contract.get("presentation_policy_version"):
            errors.append(f"{session_id}: presentation policy version diverged")
        sessions.append(
            {
                "session_id": session_id,
                "provider": provider,
                "commit_seq": explain.get("commit_seq"),
                "presentation_keys": explain.get("presentation_keys"),
                "fact_sources": explain.get("fact_sources"),
                "actions": explain.get("actions"),
                "projection_parity": projection,
                "reducer_comparison": comparison,
            }
        )

    missing_providers = sorted(required_providers - set(provider_counts))
    if missing_providers:
        errors.append(f"provider coverage missing: {', '.join(missing_providers)}")
    live_status = live_surface_parity.get("status") if isinstance(live_surface_parity, dict) else None
    allowed_live_statuses = {"matched_same_commit"}
    if allow_cross_commit_equivalence:
        allowed_live_statuses.add("matched_equivalent_different_commit")
    if require_canonical and live_status not in allowed_live_statuses:
        errors.append(f"live API/machine-stream parity is not acceptable: {live_status or 'missing'}")
    artifact = {
        "schema_version": 1,
        "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "pass" if not errors else "fail",
        "build": build,
        "contract": contract,
        "served_path": reducer_health.get("served_path"),
        "authorization_path": reducer_health.get("authorization_path"),
        "canonical_authorization_providers": reducer_health.get("canonical_authorization_providers"),
        "provider_counts": provider_counts,
        "missing_providers": missing_providers,
        "sessions": sessions,
        "live_surface_parity": live_surface_parity,
        "errors": errors,
    }
    return artifact, errors


def _write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=os.environ.get("LONGHOUSE_QA_API_URL") or os.environ.get("LONGHOUSE_API_URL"))
    parser.add_argument("--token", default=os.environ.get("LONGHOUSE_MACHINE_TOKEN") or os.environ.get("LONGHOUSE_DEVICE_TOKEN"))
    parser.add_argument("--session-id", action="append", default=[])
    parser.add_argument("--max-sessions", type=int, default=12)
    parser.add_argument("--require-provider", action="append", default=[])
    parser.add_argument("--allow-legacy", action="store_true")
    parser.add_argument("--allow-cross-commit-equivalence", action="store_true")
    parser.add_argument("--artifact", type=Path)
    args = parser.parse_args()
    if not args.api_url:
        parser.error("--api-url or LONGHOUSE_QA_API_URL is required")
    try:
        reducer_health = _get_json(args.api_url, args.token, "/api/agents/session-state/health")
        build_health = _get_json(args.api_url, args.token, "/api/health")
        session_ids = list(dict.fromkeys(args.session_id))
        if not session_ids:
            listing = _get_json(
                args.api_url,
                args.token,
                f"/api/agents/sessions?limit={max(1, min(args.max_sessions, 100))}&include_test=false",
            )
            session_ids = _session_ids(listing, args.max_sessions)
        if not session_ids:
            raise ValueError("session listing returned no sessions")
        machine_delta = _get_first_machine_delta(args.api_url, args.token)
        machine_session_id = str(machine_delta.get("session_id") or "").strip()
        if not machine_session_id:
            raise ValueError("machine session delta is missing session_id")
        machine_detail = _get_json(
            args.api_url,
            args.token,
            f"/api/agents/sessions/{urllib.parse.quote(machine_session_id)}",
        )
        live_surface_parity = compare_live_surfaces(detail=machine_detail, machine_delta=machine_delta)
        diagnostics = [
            _get_json(
                args.api_url,
                args.token,
                f"/api/agents/sessions/{urllib.parse.quote(session_id)}/state-diagnostics",
            )
            for session_id in session_ids
        ]
        artifact, errors = assess(
            reducer_health=reducer_health,
            diagnostics=diagnostics,
            build=build_health.get("build") if isinstance(build_health.get("build"), dict) else None,
            required_providers=set(args.require_provider),
            require_canonical=not args.allow_legacy,
            live_surface_parity=live_surface_parity,
            allow_cross_commit_equivalence=args.allow_cross_commit_equivalence,
        )
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        artifact = {"schema_version": 1, "status": "fail", "errors": [str(exc)]}
        errors = artifact["errors"]
    if args.artifact:
        _write_artifact(args.artifact, artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
