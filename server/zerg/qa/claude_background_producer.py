#!/usr/bin/env python3
"""Live Claude background-job fidelity through the stock Longhouse Helm path.

This producer deliberately has one launch path: the managed Claude Helm
launcher in :mod:`claude_helm_lifecycle`.  The optional scenario seam retains
the provider's exact lifecycle-hook stdin before the native engine reduces it
to an outbox record, then judges the canonical parent session reads.  A copied
registry, transcript prose, or a child archive cannot certify the scenario.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from zerg.qa import claude_helm_lifecycle as helm
from zerg.qa import claude_release_identity as release_identity
from zerg.qa import provider_release_identity as identity
from zerg.qa import provider_semantic_qualification as semantic
from zerg.qa.claude_live_session_support import ScenarioError
from zerg.qa.claude_live_session_support import artifact_manifest
from zerg.qa.claude_live_session_support import now_iso
from zerg.qa.live_session_toolkit import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.live_session_toolkit import RUNTIME_API_URL_ENV
from zerg.qa.live_session_toolkit import require_disposable_runtime
from zerg.qa.provider_factory_invocation import add_factory_provider_arguments
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key

SCENARIO_ID = "claude_background_jobs"
PROFILE = "claude_background_v1"
ARTIFACT_KIND = "claude_background_jobs_result"
ASSERTIONS = (
    "claude_background_native_source",
    "claude_background_parent_turn_boundary",
    "claude_background_registry_served",
    "claude_background_callbacks_scoped",
    "claude_background_explicit_empty",
)
FAULT = "claude_background_writer_disabled"
NEGATIVE_CONTROLS = {FAULT: ("claude_background_registry_served",)}
_VARIANTS = tuple(execution_variant_key(provider="claude", assertion_id=item, scenario_id=SCENARIO_ID, variant=None) for item in ASSERTIONS)

REGISTRATION = ProducerRegistration(
    producer_id="claude.background_jobs.v1",
    producer_revision=1,
    scenario_id=SCENARIO_ID,
    scenario_revision=1,
    assertion_cells=tuple((item, None) for item in ASSERTIONS),
    providers=("claude",),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "stock_longhouse_claude_helm_launch",
        "claude_native_hook_stdin_captured",
        "claude_background_subagent_and_shell_started",
        "claude_parent_turn_ended_with_background_work",
        "claude_background_registry_served",
        "claude_background_empty_registry_served",
        "claude_helm_process_exited",
    ),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=("claude_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "transcript_shipper_receipt",
        "claude_onboarding_receipt",
        "session_launch_receipt",
        "claude_hook_source_receipt",
        "claude_background_receipt",
        "session_close_receipt",
        "cleanup_receipt",
    ),
    required_cleanup=("claude_helm_process_exited",),
    implementation="server/zerg/qa/claude_background_producer.py",
    oracle_source="server/zerg/qa/claude_background_producer.py",
    oracle_entrypoint="claude_background_assertions",
    executable_module="zerg.qa.claude_background_producer",
    required_executables=("longhouse", "longhouse-engine"),
    provider_artifact_required=True,
    observation_scope="scenario",
)

_PROFILE = identity.IdentityProfile(
    provider="claude",
    profile=PROFILE,
    scenario_id=SCENARIO_ID,
    version_line=release_identity.VERSION_LINE,
    oracle_source=Path(__file__),
)


def _requested_assertion_id(variant: object) -> str | None:
    if variant is None:
        return None
    for assertion in ASSERTIONS:
        if execution_variant_key(provider="claude", assertion_id=assertion, scenario_id=SCENARIO_ID, variant=None) == variant:
            return assertion
    raise ValueError(f"unrecognized Claude background execution variant: {variant!r}")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse_timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _capture_records(capture_dir: Path | None) -> list[dict[str, Any]]:
    if capture_dir is None or not capture_dir.is_dir():
        return []
    records: list[dict[str, Any]] = []
    paths = sorted(capture_dir.glob("*.stdin"), key=lambda path: (path.stat().st_mtime_ns, path.name))
    for path in paths:
        try:
            raw = path.read_bytes()
            payload = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        metadata_path = path.with_name(path.name + ".meta.json")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            metadata = {}
        observed_digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        records.append(
            {
                "path": str(path),
                "metadata_path": str(metadata_path),
                "payload": payload,
                "event": payload.get("hook_event_name"),
                "native_session_id": payload.get("session_id"),
                "bytes": len(raw),
                "sha256": observed_digest,
                "metadata": metadata,
                "metadata_digest_matches": metadata.get("sha256") == observed_digest,
                "captured_at": metadata.get("captured_at"),
                "captured_mtime_ns": path.stat().st_mtime_ns,
            }
        )
    return records


def _registry(payload: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]] | None, bool]:
    if "background_tasks" not in payload:
        return None, False
    value = payload.get("background_tasks")
    if not isinstance(value, list):
        return None, True
    return [item for item in value if isinstance(item, Mapping)], True


def _active_registry(registry: list[Mapping[str, Any]] | None) -> bool:
    if not registry:
        return False
    kinds = {str(item.get("type") or "").strip().lower().replace("-", "").replace("_", "") for item in registry}
    running = {str(item.get("status") or "").strip().lower() for item in registry}
    return {"subagent", "shell"} <= kinds and bool(running & {"running", "pending", "in_progress"})


def _delegation(state: Mapping[str, Any]) -> Mapping[str, Any]:
    value = state.get("delegation")
    if isinstance(value, Mapping):
        return value
    nested = state.get("session_state")
    return _mapping(nested).get("delegation") if isinstance(_mapping(nested).get("delegation"), Mapping) else {}


def _served_active(state: Mapping[str, Any]) -> bool:
    delegation = _delegation(state)
    items = delegation.get("items")
    if isinstance(items, list) and items:
        return True
    count = delegation.get("count")
    return isinstance(count, (int, float)) and not isinstance(count, bool) and count > 0


def _served_empty(state: Mapping[str, Any]) -> bool:
    delegation = _delegation(state)
    state_name = str(delegation.get("state") or "").strip().lower()
    if state_name == "none":
        return True
    items = delegation.get("items")
    count = delegation.get("count")
    return state_name == "none" or (isinstance(items, list) and not items and count in (0, None))


def _nested_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _nested_strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _nested_strings(item)]
    return []


def _background_prompt(parent_marker: str, child_marker: str, shell_marker: str) -> str:
    return (
        "Start exactly two bounded background jobs in this disposable workspace, then end this parent turn without waiting. "
        "Use the Task/Agent subagent tool exactly once with run_in_background=true; have it run a read-only check, sleep 8 seconds, "
        f"and print {child_marker}. Use Bash exactly once with run_in_background=true for `sleep 8; printf {shell_marker}`. "
        f"Do not poll, wait, or start any other job. After both launches reply exactly {parent_marker}."
    )


def _fault_receipts(root: Path, fault: str, session_id: str) -> list[dict[str, Any]]:
    path = root / "qa-fault-receipt.jsonl"
    if not path.is_file() or not session_id:
        return []
    receipts: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(value, dict)
            and value.get("schema_version") == 1
            and value.get("fault") == fault
            and value.get("session_id") == session_id
            and isinstance(value.get("fired_at"), str)
            and isinstance(value.get("detail"), Mapping)
        ):
            receipts.append(value)
    return receipts


def _source_observation(records: list[dict[str, Any]], native_session_id: str | None) -> dict[str, Any]:
    relevant = [record for record in records if record.get("event") in {"SubagentStart", "SubagentStop", "Stop"}]
    identity_ok = (
        bool(native_session_id) and bool(relevant) and all(record.get("native_session_id") == native_session_id for record in relevant)
    )
    digests_ok = bool(relevant) and all(record.get("metadata_digest_matches") is True for record in relevant)
    parent_stops: list[dict[str, Any]] = []
    starts: dict[str, dict[str, Any]] = {}
    stops: dict[str, dict[str, Any]] = {}
    for record in relevant:
        payload = _mapping(record.get("payload"))
        event = record.get("event")
        registry, registry_present = _registry(payload)
        if event == "Stop" and registry_present and registry is not None:
            parent_stops.append({"record": record, "registry": registry})
        agent_id = payload.get("agent_id")
        if isinstance(agent_id, str) and agent_id:
            if event == "SubagentStart":
                starts[agent_id] = record
            elif event == "SubagentStop":
                stops[agent_id] = record
    active = [item for item in parent_stops if _active_registry(item["registry"])]
    empty = [item for item in parent_stops if item["registry"] == []]
    active_registry = active[-1]["registry"] if active else []
    active_ids = {str(item.get("id")) for item in active_registry if item.get("id")}
    active_kinds = sorted({str(item.get("type") or "").strip().lower().replace("-", "").replace("_", "") for item in active_registry})
    start_ids = set(starts)
    stop_ids = set(stops)
    callback_available = bool(start_ids or stop_ids)
    callbacks_matched = not callback_available or start_ids == stop_ids
    return {
        "records": [{key: value for key, value in record.items() if key not in {"payload", "metadata"}} for record in records],
        "native_session_id": native_session_id,
        "relevant_record_count": len(relevant),
        "identity_ok": identity_ok,
        "digests_ok": digests_ok,
        "parent_stop_count": len(parent_stops),
        "active_registry_observed": bool(active),
        "active_registry_ids": sorted(active_ids),
        "active_registry_kinds": active_kinds,
        "active_registry_record_index": records.index(active[-1]["record"]) if active else None,
        "explicit_empty_observed": bool(empty),
        "explicit_empty_record_index": records.index(empty[-1]["record"]) if empty else None,
        "subagent_start_ids": sorted(start_ids),
        "subagent_stop_ids": sorted(stop_ids),
        "native_callbacks_available": callback_available,
        "native_callbacks_matched": callbacks_matched,
    }


def claude_background_assertions(observation: Mapping[str, Any]) -> dict[str, bool]:
    assertions = observation.get("assertions")
    if not isinstance(assertions, Mapping):
        return {name: False for name in ASSERTIONS}
    return {name: assertions.get(name) is True for name in ASSERTIONS}


def _capture_background(
    *,
    args: argparse.Namespace,
    session: Any,
    session_id: str,
    provider_session_id: str | None = None,
    lookup_id: str,
    home: Path,
    root: Path,
    environment: Mapping[str, str],
    hook_capture_dir: Path | None,
    prompt: str,
) -> dict[str, Any]:
    del session, environment
    markers = {
        name.lower(): match.group(0)
        for name in ("parent", "child", "shell")
        if (match := re.search(rf"LONGHOUSE_CLAUDE_BG_{name.upper()}_[0-9a-f]+", prompt)) is not None
    }
    parent_marker = markers.get("parent")
    child_marker = markers.get("child")
    shell_marker = markers.get("shell")
    if not parent_marker or not child_marker or not shell_marker:
        raise ScenarioError("background prompt markers are incomplete")

    def rows() -> list[dict[str, Any]]:
        return helm._transcript_rows(lookup_id, home)

    native_session_id = str(provider_session_id or helm._channel_state(home, session_id).get("provider_session_id") or "")
    observation: dict[str, Any] = {
        "prompt": _background_prompt(parent_marker, child_marker, shell_marker),
        "markers": {"parent": parent_marker, "child": child_marker, "shell": shell_marker},
        "native_session_id": native_session_id,
        "source": {},
        "canonical_parent_reads": [],
        "assertions": {name: False for name in ASSERTIONS},
    }

    def read_state() -> dict[str, Any]:
        state = helm._served_state(args.api_url, args.agents_token, session_id)
        if isinstance(state, dict):
            observation["canonical_parent_reads"].append(
                {"captured_at": now_iso(), "served_path": "canonical_session_detail", "state": state}
            )
            observation["canonical_parent_reads"] = observation["canonical_parent_reads"][-20:]
            return state
        return {}

    def source_snapshot() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        records = _capture_records(hook_capture_dir)
        source = _source_observation(records, native_session_id)
        return records, source

    try:
        initial_state = read_state()
        helm.wait_until(
            lambda: (bounds := helm._turn_bounds(rows(), parent_marker)) is not None and bounds[1] is not None,
            timeout=args.response_timeout_secs,
            description="Claude background parent turn completing",
        )
        bounds = helm._turn_bounds(rows(), parent_marker)
        if bounds is None or bounds[1] is None:
            raise ScenarioError("Claude background parent turn boundary disappeared")
        end_row = rows()[bounds[1]]
        parent_end_at = helm._timestamp(end_row)
        parent_archived = any(
            helm.replied_with(text, parent_marker) for text in helm._hosted_assistant_texts(args.api_url, args.agents_token, session_id)
        )
        observation["parent_turn"] = {
            "bounds": bounds,
            "end_timestamp": parent_end_at,
            "assistant_marker_archived": parent_archived,
        }
        helm.wait_until(
            lambda: source_snapshot()[1].get("active_registry_observed") is True or None,
            timeout=args.response_timeout_secs,
            description="Claude native background registry capture",
        )
        source_records, source = source_snapshot()
        observation["source"] = source
        active_index = source.get("active_registry_record_index")
        empty_index = source.get("explicit_empty_record_index")
        source_after_parent = False
        if isinstance(active_index, int) and parent_end_at is not None and active_index < len(source_records):
            active_record = source_records[active_index]
            active_at = _parse_timestamp(active_record.get("captured_at"))
            if active_at is None:
                active_at = active_record.get("captured_mtime_ns", 0) / 1_000_000_000
            observation["source"]["active_registry_captured_at"] = active_record.get("captured_at") or active_at
            # The native Stop hook is Claude's parent-turn boundary.  Its
            # authoritative registry snapshot therefore proves work was
            # active at turn end even if transcript and hook clocks are
            # ordered a few milliseconds apart.
            source_after_parent = active_record.get("event") == "Stop"
        observation["source"]["active_registry_after_parent_turn"] = source_after_parent
        served_active = False
        deadline = time.monotonic() + args.response_timeout_secs
        while time.monotonic() < deadline:
            current = read_state()
            served_active = _served_active(current)
            if served_active or (getattr(args, "negative_control", None) and source_snapshot()[1].get("explicit_empty_observed") is True):
                break
            time.sleep(0.5)
        helm.wait_until(
            lambda: source_snapshot()[1].get("explicit_empty_observed") is True or None,
            timeout=args.response_timeout_secs,
            description="Claude native background empty registry capture",
        )
        source_records, source = source_snapshot()
        observation["source"] = {**observation["source"], **source}
        empty_index = source.get("explicit_empty_record_index")
        child_ids = set(source.get("subagent_start_ids") or []) | set(source.get("subagent_stop_ids") or [])
        callback_tool_replaced = False
        for sample in observation["canonical_parent_reads"]:
            state = _mapping(sample.get("state"))
            activity = _mapping(state.get("activity"))
            strings = _nested_strings(activity) + _nested_strings(state.get("active_tool"))
            callback_tool_replaced = callback_tool_replaced or any(child_id in text for child_id in child_ids for text in strings)
        observation["initial_served_active"] = _served_active(initial_state)
        observation["canonical_identity_ok"] = bool(
            native_session_id and any(_mapping(sample.get("state")) for sample in observation["canonical_parent_reads"])
        )
        if (
            empty_index is None
            or active_index is None
            or not isinstance(empty_index, int)
            or not isinstance(active_index, int)
            or empty_index <= active_index
        ):
            empty_served = False
        else:
            empty_served = False
            deadline = time.monotonic() + args.response_timeout_secs
            while time.monotonic() < deadline:
                current = read_state()
                empty_served = _served_empty(current)
                if empty_served:
                    break
                time.sleep(0.5)
        source_records, source = source_snapshot()
        observation["source"] = {**observation["source"], **source}
        callbacks_scoped = (
            observation.get("canonical_identity_ok") is True
            and source.get("native_callbacks_matched") is True
            and observation.get("initial_served_active") is not True
            and observation.get("child_callback_replaced_parent_tool") is not True
        )
        observation["served_empty"] = empty_served
        observation["assertions"] = {
            "claude_background_native_source": bool(
                source.get("identity_ok") is True
                and source.get("digests_ok") is True
                and source.get("active_registry_observed") is True
                and set(source.get("active_registry_kinds") or []) >= {"subagent", "shell"}
            ),
            "claude_background_parent_turn_boundary": bool(
                parent_archived and parent_end_at is not None and source.get("active_registry_observed") is True and source_after_parent
            ),
            "claude_background_registry_served": bool(served_active and empty_served),
            "claude_background_callbacks_scoped": callbacks_scoped,
            "claude_background_explicit_empty": source.get("explicit_empty_observed") is True,
        }
        observation["status"] = "pass" if all(observation["assertions"].values()) else "fail"
        if observation["status"] != "pass":
            observation["failure_code"] = "claude_background_assertion_failed"
    except Exception as exc:  # noqa: BLE001 - preserve source/canonical partial observations
        source_records, source = source_snapshot()
        observation["source"] = {**observation.get("source", {}), **source}
        observation["error"] = f"{type(exc).__name__}: {exc}"
        observation["status"] = "fail"
        observation["failure_code"] = "claude_background_scenario_incomplete"
    helm.write_json(root / "claude-background-receipt.json", observation)
    return observation


def _run_direct(args: argparse.Namespace) -> dict[str, Any]:
    requested_assertion = _requested_assertion_id(getattr(args, "variant", None))
    token = os.urandom(8).hex()
    prompt = _background_prompt(
        f"LONGHOUSE_CLAUDE_BG_PARENT_{token}",
        f"LONGHOUSE_CLAUDE_BG_CHILD_{token}",
        f"LONGHOUSE_CLAUDE_BG_SHELL_{token}",
    )
    result = helm.run_lifecycle(
        args,
        registration=REGISTRATION,
        artifact_kind=ARTIFACT_KIND,
        scenario_prompt=prompt,
        scenario_capture=_capture_background,
        capture_hook_stdin=True,
    )
    result = dict(result)
    result["producer"] = REGISTRATION.to_dict()
    result["profile"] = PROFILE
    result["scenario_id"] = SCENARIO_ID
    result["requested_assertion"] = requested_assertion
    root = args.evidence_root.resolve()
    observation = result.get("observation") if isinstance(result.get("observation"), dict) else {}
    lifecycle = observation.get("lifecycle") if isinstance(observation.get("lifecycle"), dict) else {}
    scenario = lifecycle.get("scenario") if isinstance(lifecycle.get("scenario"), dict) else {}
    assertions = claude_background_assertions(scenario)
    result["observation"]["background"] = scenario
    result["assertions"] = assertions
    fault = getattr(args, "negative_control", None)
    if fault:
        (target,) = NEGATIVE_CONTROLS[fault]
        receipts = _fault_receipts(root, fault, str(result.get("session_id") or ""))
        healthy = (
            result.get("claude_helm_process_exited", observation.get("claude_helm_process_exited")) is True
            and isinstance(lifecycle.get("launch_registration"), Mapping)
            and lifecycle["launch_registration"].get("passed") is True
            and assertions.get("claude_background_native_source") is True
            and assertions.get("claude_background_parent_turn_boundary") is True
            and assertions.get("claude_background_callbacks_scoped") is True
            and assertions.get("claude_background_explicit_empty") is True
            and scenario.get("source", {}).get("active_registry_observed") is True
            and scenario.get("source", {}).get("explicit_empty_observed") is True
            and scenario.get("parent_turn", {}).get("assistant_marker_archived") is True
        )
        target_rejected = assertions.get(target) is False and scenario.get("source", {}).get("active_registry_observed") is True
        observed_code = "background_registry_missing" if assertions.get(target) is False else "background_registry_present"
        expected_code = "background_registry_missing"
        preconditions_held = healthy
        negative = {
            "fault": fault,
            "fault_receipt_contract": {
                "path": str(root / "qa-fault-receipt.jsonl"),
                "required_fields": ["schema_version", "fault", "session_id", "fired_at", "detail"],
                "exact_fault": fault,
                "exact_session_id": result.get("session_id"),
            },
            "fault_fired": bool(receipts),
            "fault_receipts": receipts,
            "target_assertion": target,
            "target_rejected": target_rejected,
            "observed_code": observed_code,
            "expected_code": expected_code,
            "preconditions_held": preconditions_held,
            "healthy_preconditions": preconditions_held,
            "status": (
                "pass"
                if receipts and preconditions_held and target_rejected and observed_code == expected_code
                else "fail"
                if receipts and preconditions_held
                else "inconclusive"
            ),
        }
        result["negative_control"] = negative
        result["status"] = negative["status"]
        if result["status"] != "pass":
            result["failure_code"] = (
                "claude_background_negative_control_inconclusive"
                if result["status"] == "inconclusive"
                else "claude_background_negative_control_not_caught"
            )
    else:
        cleanup_ok = result.get("claude_helm_process_exited", observation.get("claude_helm_process_exited"))
        result["status"] = "pass" if result.get("status") == "pass" and cleanup_ok is True and all(assertions.values()) else "fail"
        if result["status"] != "pass":
            result["failure_code"] = str(scenario.get("failure_code") or result.get("failure_code") or "claude_background_scenario_failed")
    result["artifact_manifest"] = artifact_manifest(root)
    helm.write_json(root / "result.json", result)
    return result


def _request_args(request: Mapping[str, Any], output_root: Path) -> argparse.Namespace:
    return argparse.Namespace(
        evidence_root=output_root,
        repo_root=Path(__file__).resolve().parents[3],
        provider_bin=Path(str(request["provider_bin"])),
        provider_version=str(request["expected_provider_version"]),
        engine=Path(os.environ.get("LONGHOUSE_ENGINE_BIN") or ""),
        longhouse_cli=Path(os.environ.get("LONGHOUSE_CLI_BIN") or "longhouse"),
        api_url=os.environ.get(RUNTIME_API_URL_ENV),
        agents_token=os.environ.get(RUNTIME_AGENTS_TOKEN_ENV),
        model=os.environ.get("LONGHOUSE_CLAUDE_QUALIFICATION_MODEL") or os.environ.get("ANTHROPIC_MODEL"),
        variant=None,
        negative_control=None,
        project="zerg",
        launch_timeout_secs=90.0,
        response_timeout_secs=150.0,
    )


def run(request_path: Path, output_root: Path) -> dict[str, Any]:
    request = identity.load_request(request_path, provider="claude", profile=PROFILE, version_grammar=_PROFILE.version_grammar)

    def execute(binary: Path, evidence_root: Path):
        args = _request_args(request, evidence_root)
        args.provider_bin = binary
        result = _run_direct(args)
        assertions = result.get("assertions") if isinstance(result.get("assertions"), Mapping) else {}
        return (
            result,
            tuple(
                semantic.SemanticAssertion(
                    name,
                    semantic.AssertionOutcome.PASS if assertions.get(name) is True else semantic.AssertionOutcome.SEMANTIC_FAIL,
                    semantic.EvidenceClass.LIVE_TOKEN,
                )
                for name in ASSERTIONS
            ),
            (),
        )

    return semantic.run_semantic_profile(
        request_path,
        output_root,
        profile=_PROFILE,
        assertion_ids=ASSERTIONS,
        executor=execute,
        oracle_source=Path(__file__),
        scenario_revision=1,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_factory_provider_arguments(parser, variants=_VARIANTS, provider_bin_aliases=("--claude-bin",))
    parser.add_argument("--project", default="zerg")
    parser.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL"))
    parser.add_argument("--api-url", default=os.environ.get(RUNTIME_API_URL_ENV))
    parser.add_argument("--agents-token", default=os.environ.get(RUNTIME_AGENTS_TOKEN_ENV))
    parser.add_argument("--launch-timeout-secs", type=float, default=90.0)
    parser.add_argument("--response-timeout-secs", type=float, default=150.0)
    parser.add_argument("--negative-control", choices=(FAULT,), default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    for required in ("evidence_root", "repo_root", "engine", "provider_bin"):
        if getattr(args, required) is None:
            print(
                json.dumps(
                    {
                        "status": "fail",
                        "producer": REGISTRATION.to_dict(),
                        "failure_code": f"missing_required_argument:--{required.replace('_', '-')}",
                    }
                )
            )
            return 2
    args.api_url = args.api_url or os.environ.get(RUNTIME_API_URL_ENV, "")
    args.agents_token = args.agents_token or os.environ.get(RUNTIME_AGENTS_TOKEN_ENV, "")
    require_disposable_runtime(args.api_url)
    if not args.api_url or not args.agents_token:
        print(
            json.dumps({"status": "fail", "producer": REGISTRATION.to_dict(), "failure_code": "runtime_host_control_credentials_missing"})
        )
        return 2
    for path, code in ((args.engine, "longhouse_engine_missing"), (args.provider_bin, "claude_binary_missing")):
        if not path.is_file() or not os.access(path, os.X_OK):
            print(json.dumps({"status": "fail", "producer": REGISTRATION.to_dict(), "failure_code": code}))
            return 2
    try:
        result = _run_direct(args)
    except Exception as exc:  # noqa: BLE001 - preserve typed setup/cleanup failure
        root = args.evidence_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        result = {
            "schema_version": 1,
            "artifact_kind": ARTIFACT_KIND,
            "producer": REGISTRATION.to_dict(),
            "provider": "claude",
            "profile": PROFILE,
            "scenario_id": SCENARIO_ID,
            "variant": getattr(args, "variant", None),
            "status": "inconclusive" if args.negative_control else "fail",
            "failure_code": "claude_background_setup_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "assertions": {name: False for name in ASSERTIONS},
        }
        helm.write_json(root / "result.json", result)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
