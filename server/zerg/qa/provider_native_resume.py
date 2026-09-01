#!/usr/bin/env python3
"""Direct stock-provider Helm Resume producer shared by non-Codex adapters.

The provider-specific modules only declare registration.  This module owns the
one black-box transaction: the shipped ``longhouse`` facade launches a real
provider TUI in a PTY, the Runtime Host transcript proves provider activity,
the old owner is stopped or lost, and the same facade performs native Resume.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from typing import Mapping

from zerg.qa import live_session_toolkit
from zerg.qa.provider_release_identity import artifact_manifest
from zerg.qa.provider_release_identity import now
from zerg.qa.provider_release_identity import sha256_file
from zerg.qa.provider_resume_oracles import native_resume_assertions
from zerg.qa.resume_assurance import ProducerRegistration

_DEFAULT_RESUME_INTENT_TIMEOUT_SECS = 45.0
_PROCESS_LOSS_RESUME_INTENT_TIMEOUT_SECS = 180.0
# A daemon shutdown can expose repair-lane backpressure immediately before a
# quarantined Cursor epoch becomes eligible for lineage reconciliation. Keep
# one bounded retry for each typed state, with no retry for arbitrary failures.
SPECS = {
    "claude": live_session_toolkit.ProviderSpec(
        provider="claude",
        producer_id="claude.native_resume.v1",
        executable_module="zerg.qa.claude_native_resume",
        binary_flag="--claude-bin",
        resume_flag="--resume",
        credential_binding_id="claude_provider_token",
        state_patterns=(
            ".claude/channels/longhouse/sessions/*.json",
            ".longhouse/managed-local/contracts/claude/*.json",
            "managed-local/contracts/claude/*.json",
        ),
    ),
    "cursor": live_session_toolkit.ProviderSpec(
        provider="cursor",
        producer_id="cursor.native_resume.v1",
        executable_module="zerg.qa.cursor_native_resume",
        binary_flag="--cursor-bin",
        resume_flag="--resume-session",
        credential_binding_id="cursor_provider_token",
        state_patterns=(
            ".longhouse/managed-local/cursor-helm/*.json",
            "managed-local/cursor-helm/*.json",
        ),
    ),
    "opencode": live_session_toolkit.ProviderSpec(
        provider="opencode",
        producer_id="opencode.native_resume.v1",
        executable_module="zerg.qa.opencode_native_resume",
        binary_flag="--opencode-bin",
        resume_flag="--resume-session",
        credential_binding_id="opencode_provider_token",
        state_patterns=(
            ".claude/managed-local/opencode-server/*.json",
            "managed-local/opencode-server/*.json",
            ".longhouse/managed-local/opencode/bridge/sessions/*.json",
            "managed-local/opencode/bridge/sessions/*.json",
        ),
    ),
}


def registration_for(provider: str) -> ProducerRegistration:
    spec = SPECS[provider]
    return ProducerRegistration(
        producer_id=spec.producer_id,
        producer_revision=6 if provider == "cursor" else 5 if provider == "opencode" else 4,
        scenario_id="helm_cold_resume",
        scenario_revision=5 if provider in {"cursor", "opencode"} else 4,
        assertion_cells=(
            ("native_provider_resume_proven", "clean_exit"),
            ("native_provider_resume_proven", "process_loss"),
        ),
        providers=(provider,),
        platforms=("linux",),
        architectures=("x86_64", "aarch64"),
        modes=("helm",),
        evidence_classes=("live_token",),
        observed_activity=(
            "provider_neutral_resume_intent",
            "native_resume_command",
            "post_resume_provider_activity",
            "stale_input_rejected",
            "concurrent_resume_refused",
            "artifact_secret_scan_passed",
        ),
        acquisition_methods=("staged_release", "observed_install"),
        credential_binding_ids=(spec.credential_binding_id, "runtime_host_control"),
        sandbox_policy="provider-qualification-bwrap-v3",
        network_policy="shared_provider_egress",
        required_artifacts=(
            "provider_binary_receipt",
            *(("opencode_model_profile_receipt",) if provider == "opencode" else ()),
            "transcript_shipper_receipt",
            "resume_intent_receipt",
            "initial_bridge_state",
            "initial_seed_send",
            "initial_transcript",
            "initial_transcript_ship_receipt",
            "native_resume_terminal_checkpoint",
            *(
                (
                    "initial_hook_correlation",
                    "resume_bootstrap_response_correlation",
                    "resume_bootstrap_transcript",
                    "resume_bootstrap_transcript_ship_receipt",
                )
                if provider == "cursor"
                else ()
            ),
            "resumed_bridge_state",
            "resumed_transcript",
            "post_resume_response_correlation",
            "post_resume_transcript_ship_receipt",
            "post_stop_transcript_ship_receipt",
            "process_transition_receipt",
            "stale_input_receipt",
            "concurrent_resume_receipt",
            "cleanup_receipt",
        ),
        required_cleanup=(
            "old_owner_dead",
            "final_bridge_stopped",
            "final_socket_absent",
            "no_orphan_provider_processes",
        ),
        implementation=f"server/{spec.executable_module.replace('.', '/')}.py".replace("server/zerg/", "server/zerg/"),
        oracle_source="server/zerg/qa/provider_resume_oracles.py",
        oracle_entrypoint="native_resume_assertions",
        executable_module=spec.executable_module,
    )


def _close_recordings(processes: tuple[live_session_toolkit.PtyProcess | None, ...]) -> None:
    """Close PTY recording streams before hashing the evidence manifest."""

    for process in processes:
        if process is not None:
            process.close()


def _refresh_result_manifest(root: Path) -> dict[str, Any] | None:
    """Hash result evidence after final cleanup has been written.

    The failure path writes ``result.json`` before the ``finally`` block tears
    down provider processes and records ``cleanup-receipt.json``.  Refreshing
    the manifest after that finalization keeps failure evidence exhaustive and
    prevents a legitimate cleanup receipt from looking like a TOCTOU mutation.
    ``result.json`` is intentionally excluded by ``artifact_manifest``.
    """

    result_path = root / "result.json"
    if not result_path.is_file() or result_path.is_symlink():
        return None
    try:
        result = live_session_toolkit._read_json(result_path)
    except (OSError, ValueError, TypeError):
        return None
    if result.get("status") not in {"fail", "pass"}:
        return None
    try:
        result["artifact_manifest"] = artifact_manifest(root)
        live_session_toolkit.write_json(result_path, result)
    except (OSError, TypeError, ValueError):
        return None
    return result


def _refresh_failure_result_manifest(root: Path) -> dict[str, Any] | None:
    """Backward-compatible name for callers that refresh failure evidence."""

    return _refresh_result_manifest(root)


def _finalize_result_payload(
    root: Path,
    payload: dict[str, Any] | None,
    *,
    redacted_files: list[str],
    finalization_errors: list[str],
) -> dict[str, Any] | None:
    """Persist a post-teardown result without certifying incomplete evidence."""

    if payload is None:
        return None

    def mark_finalization_failure(reason: str) -> None:
        if payload.get("status") != "pass":
            return
        payload["status"] = "fail"
        payload["failure_code"] = "finalization_failed"
        payload["error"] = reason
        observation = payload.get("observation")
        if isinstance(observation, dict):
            observation["artifact_secret_scan_passed"] = False
        assertions = payload.get("assertions")
        if isinstance(assertions, dict):
            assertions["native_provider_resume_proven"] = False

    if finalization_errors:
        mark_finalization_failure("; ".join(finalization_errors))
    if redacted_files:
        prior_redacted = payload.get("redacted_secret_files", [])
        payload["redacted_secret_files"] = sorted(set(prior_redacted) | set(redacted_files))
        if payload.get("status") == "pass":
            payload["status"] = "fail"
            payload["failure_code"] = "finalized_artifact_secret_scan_failed"
            payload["error"] = "finalized evidence contained a qualification secret"
            observation = payload.get("observation")
            if isinstance(observation, dict):
                observation["artifact_secret_scan_passed"] = False
            assertions = payload.get("assertions")
            if isinstance(assertions, dict):
                assertions["native_provider_resume_proven"] = False

    manifest_error: str | None = None
    if redacted_files or finalization_errors:
        try:
            live_session_toolkit.write_json(root / "result.json", payload)
        except Exception as exc:  # noqa: BLE001 - fail closed for a would-be pass
            manifest_error = f"result write: {type(exc).__name__}: {exc}"
    try:
        refreshed_result = _refresh_result_manifest(root)
        if refreshed_result is None:
            manifest_error = manifest_error or "result manifest refresh returned no result"
    except Exception as exc:  # noqa: BLE001 - never mask the causal result
        refreshed_result = None
        manifest_error = f"result manifest refresh: {type(exc).__name__}: {exc}"
    if manifest_error:
        mark_finalization_failure(manifest_error)
    if refreshed_result is not None:
        payload["artifact_manifest"] = refreshed_result["artifact_manifest"]
    if redacted_files:
        prior_redacted = payload.get("redacted_secret_files", [])
        payload["redacted_secret_files"] = sorted(set(prior_redacted) | set(redacted_files))
    try:
        live_session_toolkit.write_json(root / "result.json", payload)
    except OSError as exc:
        # Never leave any previously written result behind when the final
        # atomic write fails. Missing result evidence is rejected; stale
        # green or stale failure evidence must not be accepted either.
        try:
            (root / "result.json").unlink()
        except OSError:
            pass
        mark_finalization_failure(f"final result write: {type(exc).__name__}: {exc}")
    return payload


def _wait_claude_tui_ready(process: live_session_toolkit.PtyProcess, recording: Path, *, timeout: float = 30.0) -> None:
    """Wait for Claude's actual input prompt after its native state appears.

    Claude can publish the Longhouse channel state while its TUI is still
    finishing the bypass-permissions acknowledgement and drawing the first
    input prompt. Sending the seed as soon as the state file exists can then
    be consumed by that startup control surface instead of becoming a real
    provider turn. The visible ``❯ Try ...`` prompt is the provider-owned
    readiness boundary for terminal input.
    """

    del recording  # The process owns the append-only PTY recording.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        process.drain()
        if process.process.poll() is not None:
            raise RuntimeError("Claude Helm process exited before its TUI became ready")
        # The native development-channel selector can appear after the
        # managed contract has already been written. Keep handling that
        # provider-owned control record while waiting for the actual prompt;
        # otherwise the readiness wait observes the selector forever and the
        # channel server never initializes.
        live_session_toolkit._accept_claude_permission_prompt(process)
        live_session_toolkit._accept_claude_development_channel_prompt(process)
        terminal = live_session_toolkit._terminal_text(process.recording)
        if _claude_input_prompt_visible(terminal):
            process.settle()
            return
        time.sleep(0.1)
    raise RuntimeError("Claude TUI did not publish its input prompt")


def _claude_input_prompt_visible(terminal: str) -> bool:
    """Recognize Claude's provider-owned input line across TUI revisions.

    Claude used to render a hint such as ``❯ Try \"refactor ...\"``. Current
    builds render the same empty input surface as a bare ``❯``. The startup
    development-channel selector also begins with ``❯``, so accepting any
    arrow character would race that control record. Match a complete prompt
    line, while retaining the older inline redraw form used by some PTYs.
    """

    normalized = terminal.replace("\r", "\n")
    prompt_line = re.compile(r"(?:^|\n)[^\S\r\n]*[❯>][^\S\r\n]*(?:Try\b[^\r\n]*)?(?:\n|$)")
    return bool(prompt_line.search(normalized) or re.search(r"[❯>][^\S\r\n]*Try\b", normalized))


def _wait_resume_intent(
    spec: live_session_toolkit.ProviderSpec,
    args: argparse.Namespace,
    session_id: str,
    *,
    timeout: float = 45,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_reason = "resume intent was not projected"
    while time.monotonic() < deadline:
        try:
            intent = live_session_toolkit._api_json(
                args.api_url,
                args.agents_token,
                f"sessions/{session_id}/resume-intent",
                method="POST",
            )
        except live_session_toolkit._RuntimeHostHTTPError as exc:
            if exc.status in {401, 403}:
                raise
            last_reason = str(exc)
            time.sleep(0.5)
            continue
        except (OSError, urllib.error.URLError) as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
            time.sleep(0.5)
            continue
        if intent.get("available") is True:
            return intent
        last_reason = str(intent.get("reason") or "resume intent unavailable")
        time.sleep(0.5)
    raise RuntimeError(f"provider-neutral Resume intent remained unavailable: {last_reason}")


def _resume_intent_timeout(*, variant: str) -> float:
    """Allow the machine to publish a process-loss terminal fact.

    A clean provider exit publishes its terminal event synchronously.  A
    killed Helm owner has to be observed by the Machine Agent's complete
    managed-process reconciliation; startup deliberately defers that scan for
    two minutes so live transcript shipping can warm first.  The factory must
    wait for that real evidence instead of treating the intermediate
    ``run_active`` response as a failed Resume implementation.
    """

    if variant == "process_loss":
        return _PROCESS_LOSS_RESUME_INTENT_TIMEOUT_SECS
    return _DEFAULT_RESUME_INTENT_TIMEOUT_SECS


def _command_from_resume_intent(
    spec: live_session_toolkit.ProviderSpec,
    args: argparse.Namespace,
    session_id: str,
    intent: dict[str, Any],
    *,
    use_credential_files: bool = False,
    cwd: Path | None = None,
    prompt: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    working_directory = cwd or args.repo_root
    expected_argv = [
        "longhouse",
        spec.provider,
        "--cwd",
        str(working_directory),
        spec.resume_flag,
        session_id,
    ]
    received_argv = intent.get("argv")
    identity_valid = (
        intent.get("available") is True
        and intent.get("session_id") == session_id
        and intent.get("provider") == spec.provider
        and intent.get("cwd") == str(working_directory)
        and intent.get("handoff") == "terminal_command"
        and received_argv == expected_argv
    )
    if not identity_valid:
        raise RuntimeError("provider-neutral Resume intent did not match the exact session, provider, cwd, and native selector")
    selector_index = expected_argv.index(spec.resume_flag)
    overrides = ["--url", args.api_url]
    if not use_credential_files:
        overrides.extend(("--token", args.agents_token))
    overrides.extend((spec.binary_flag, str(args.provider_bin)))
    if spec.provider == "cursor":
        overrides.extend(("--permission-mode", "auto_approve"))
    command = [str(args.longhouse_cli), *expected_argv[1:selector_index], *overrides, *expected_argv[selector_index:]]
    if spec.provider == "cursor":
        cursor_model = os.environ.get("CURSOR_MODEL", "").strip()
        if cursor_model:
            command.extend(("--", "--model", cursor_model))
        if prompt:
            command.append(prompt)
    retained_command = ["<redacted>" if value == args.agents_token else value for value in command]
    receipt = {
        "requested_at": now(),
        "intent": intent,
        "identity_valid": identity_valid,
        "executed_argv": retained_command,
        "executed_argv_sha256": f"sha256:{hashlib.sha256(json.dumps(command, separators=(',', ':')).encode()).hexdigest()}",
        "factory_overrides": [
            "runtime_host",
            "provider_binary",
            *(("permission_mode",) if spec.provider == "cursor" else ()),
            *(("cursor_model",) if spec.provider == "cursor" and os.environ.get("CURSOR_MODEL", "").strip() else ()),
        ],
        "credential_source": "disposable_machine_file" if use_credential_files else "argv_token",
    }
    return command, receipt


def _wait_assistant_marker(api_url: str, token: str, session_id: str, marker: str, *, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last = live_session_toolkit._api_json(api_url, token, f"sessions/{session_id}/tail?limit=100&roles=user,assistant")
        except live_session_toolkit._RuntimeHostHTTPError as exc:
            if exc.status in {401, 403}:
                raise
            # Session projection can lag the local managed state by a short
            # interval immediately after launch or resume.  Keep polling for
            # transient application errors while preserving auth failures.
            time.sleep(0.5)
            continue
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
            continue
        if live_session_toolkit._assistant_contains(last, marker):
            return last
        time.sleep(0.5)
    raise RuntimeError(f"provider transcript did not retain assistant marker {marker}")


def _post_resume_response_correlated(provider: str, correlation: dict[str, Any]) -> bool:
    """Require the Runtime Host transcript to prove the resumed provider turn."""

    new_assistant_events = correlation.get("new_assistant_events")
    return bool(
        correlation.get("marker_observed_in_transcript")
        and isinstance(new_assistant_events, int)
        and not isinstance(new_assistant_events, bool)
        and new_assistant_events > 0
        and (provider == "claude" or correlation.get("marker_observed_in_assistant"))
    )


def _require_transcript_ship(receipt: dict[str, Any], *, label: str) -> None:
    """Reject infrastructure failure before judging transcript convergence."""

    if receipt.get("status") == "pass":
        return
    reason = str(receipt.get("failure_code") or receipt.get("retry_reason") or receipt.get("error") or "unknown")
    status = receipt.get("http_status")
    phrase = str(receipt.get("http_status_phrase") or "").strip()
    if isinstance(status, int) and not isinstance(status, bool):
        reason = f"{reason}: {status}{f' {phrase}' if phrase else ''}"
    if receipt.get("transport_error") == "operation_timed_out":
        reason = f"{reason}: storage-v2 capability request failed: operation timed out"
    raise RuntimeError(f"{label} transcript ship failed: {reason}")


def _cursor_idle_then_flush(
    state: dict[str, Any],
    environment: dict[str, str],
    shipper: live_session_toolkit.TranscriptShipper,
    *,
    label: str,
    minimum_hook_event_bytes: int | None = None,
    expected_generation_id: str | None = None,
    marker: str | None = None,
    diagnostic_path: Path | None = None,
) -> dict[str, Any]:
    """Keep provider completion ahead of the one-shot transcript flush."""

    live_session_toolkit._wait_cursor_idle(
        state,
        environment,
        minimum_hook_event_bytes=minimum_hook_event_bytes,
        expected_generation_id=expected_generation_id,
        diagnostic_path=diagnostic_path,
    )
    if marker:
        shipper.capture_cursor_projection_diagnostics(
            state,
            marker=marker,
            label=f"{label}-before-flush",
        )
    receipt = shipper.flush(label)
    if marker:
        shipper.capture_cursor_projection_diagnostics(
            state,
            marker=marker,
            label=f"{label}-after-flush",
        )
    return receipt


def _cursor_initial_send_then_flush(
    state: dict[str, Any],
    environment: dict[str, str],
    shipper: live_session_toolkit.TranscriptShipper,
    *,
    marker: str,
    expected_prompt: str,
    minimum_hook_event_bytes: int,
    timeout: float,
    diagnostic_path: Path | None = None,
    hook_correlation_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Settle Cursor's first managed turn before asking the engine to ship it.

    The initial Cursor seed is sent through the Helm socket after the native
    argv bootstrap.  A successful socket write only means the request was
    accepted; it does not mean Cursor completed the foreground turn.  Require
    the identity-matched native hook sequence and the subsequent idle phase
    before flushing the transcript.  This keeps the initial proof on the same
    causal boundary as Resume and prevents a one-shot flush from racing the
    provider's store projection.
    """

    hook_sequence = _wait_cursor_hook_sequence(
        state,
        environment,
        marker=marker,
        expected_prompt=expected_prompt,
        minimum_hook_event_bytes=minimum_hook_event_bytes,
        label="initial-seed",
        timeout=timeout,
    )
    # Persist the provider-owned causal observation before waiting on the
    # separate idle/projection boundary. If settlement fails, the valid hook
    # sequence must remain available in the failure bundle.
    if hook_correlation_path is not None:
        live_session_toolkit.write_json(hook_correlation_path, hook_sequence)
    ship_receipt = _cursor_idle_then_flush(
        state,
        environment,
        shipper,
        label="initial",
        minimum_hook_event_bytes=minimum_hook_event_bytes,
        expected_generation_id=hook_sequence.get("generation_id"),
        marker=marker,
        diagnostic_path=diagnostic_path,
    )
    return hook_sequence, ship_receipt


def _cursor_bootstrap_correlation(
    transcript_correlation: dict[str, Any],
    hook_sequence: dict[str, Any],
    ship_receipt: dict[str, Any],
) -> dict[str, Any]:
    """Classify Cursor's bootstrap response without weakening the final proof.

    Cursor's native hook is provider-owned evidence for the bootstrap turn,
    while the Runtime Host transcript remains the authority for the later
    managed Resume marker.  A hook-only bootstrap fallback is valid only when
    the exact hook sequence passed and the same transcript shipper delivered
    at least one event.
    """

    transcript_projection_correlated = bool(
        transcript_correlation.get("marker_observed_in_transcript")
        and transcript_correlation.get("marker_observed_in_assistant")
        and isinstance(transcript_correlation.get("new_assistant_events"), int)
        and not isinstance(transcript_correlation.get("new_assistant_events"), bool)
        and transcript_correlation["new_assistant_events"] > 0
    )
    hook_response_correlated = hook_sequence.get("hook_response_correlated") is True
    events_shipped = ship_receipt.get("events_shipped")
    bootstrap_ship_succeeded = bool(
        ship_receipt.get("status") == "pass"
        and isinstance(events_shipped, int)
        and not isinstance(events_shipped, bool)
        and events_shipped > 0
    )
    if transcript_projection_correlated:
        method = "transcript_projection"
    elif hook_response_correlated and bootstrap_ship_succeeded:
        method = "cursor_hook_with_transcript_ship"
    else:
        method = "unverified"
    return {
        "transcript_projection_correlated": transcript_projection_correlated,
        "hook_response_correlated": hook_response_correlated,
        "method": method,
        "bootstrap_correlated": transcript_projection_correlated or (hook_response_correlated and bootstrap_ship_succeeded),
    }


def _cursor_hook_event_bytes(state: dict[str, Any], environment: dict[str, str]) -> int:
    """Return the current lifecycle-hook log size for a managed Cursor run."""

    longhouse_home = str(environment.get("LONGHOUSE_HOME") or "").strip()
    if not longhouse_home:
        raise RuntimeError("Cursor Helm qualification has no explicit Longhouse home")
    path = Path(longhouse_home) / "managed-local" / "cursor-helm" / "hook-events" / f"{state['session_id']}.ndjson"
    try:
        return path.stat().st_size
    except OSError:
        return 0


_CURSOR_DIAGNOSTIC_JSON_BYTES = 65536


def _wait_cursor_initial_idle(
    state: dict[str, Any],
    environment: dict[str, str],
    args: argparse.Namespace,
    *,
    diagnostic_path: Path,
) -> dict[str, Any]:
    """Wait for the provider-argv bootstrap using the qualification budget.

    Cursor can legitimately remain active while its first model turn is
    producing tool or reasoning events. The default idle wait is intentionally
    short for ordinary control-path checks; the initial qualification turn
    must use the same bounded live-send budget as the rest of the assurance
    transaction so provider latency is not mistaken for a lifecycle failure.
    """

    return live_session_toolkit._wait_cursor_idle(
        state,
        environment,
        timeout=args.live_send_timeout_secs,
        diagnostic_path=diagnostic_path,
    )


def _wait_cursor_hook_sequence(
    state: dict[str, Any],
    environment: dict[str, str],
    *,
    marker: str,
    expected_prompt: str,
    minimum_hook_event_bytes: int,
    label: str,
    timeout: float = 45.0,
) -> dict[str, Any]:
    """Require one submitted Cursor turn to produce ordered native hook events.

    ``sessionStart`` is an idle phase, but it does not prove that the prompt
    written to the PTY was accepted.  The only provider-owned sequence that
    establishes a real foreground turn is ``beforeSubmitPrompt`` followed by
    ``afterAgentResponse`` for this session and retained conversation.  The
    prompt, response text, and generation identity are part of the predicate;
    an unrelated hook response must never authorize the transcript fallback.
    """

    longhouse_home = str(environment.get("LONGHOUSE_HOME") or "").strip()
    if not longhouse_home:
        raise RuntimeError("Cursor Helm qualification has no explicit Longhouse home")
    root = Path(longhouse_home) / "managed-local" / "cursor-helm"
    path = root / "hook-events" / f"{state['session_id']}.ndjson"
    claim_path = root / "binding-probes" / f"{state['session_id']}.json"
    expected_launch_id: str | None = None
    claim_status = ""
    deadline = time.monotonic() + timeout
    observed_events: list[str] = []
    matching_befores: list[tuple[dict[str, Any], int]] = []
    matching_afters: list[tuple[dict[str, Any], int]] = []
    seen_events: set[str] = set()
    event_position = 0
    while time.monotonic() < deadline:
        try:
            claim = live_session_toolkit._read_json_bounded(claim_path)
        except (OSError, ValueError, json.JSONDecodeError):
            claim = {}
        current_launch_id = str(claim.get("launch_id") or "").strip()
        claim_status = str(claim.get("status") or "").strip()
        if (
            claim.get("schema_version") != 2
            or claim.get("provider") != "cursor"
            or claim_status not in {"pending", "observed"}
            or claim.get("session_id") != state.get("session_id")
            or claim.get("conversation_uuid") != state.get("provider_session_id")
            or claim.get("run_id") != state.get("run_id")
            or not current_launch_id
            or (expected_launch_id is not None and current_launch_id != expected_launch_id)
        ):
            raise RuntimeError(
                "Cursor hook sequence lacks the enrolled launch binding "
                f"(claim_path={claim_path}, binding={json.dumps(live_session_toolkit._diagnostic_mapping(claim, ('schema_version', 'provider', 'status', 'session_id', 'conversation_uuid', 'launch_id', 'run_id')), sort_keys=True)})"
            )
        expected_launch_id = current_launch_id
        try:
            with path.open("rb") as stream:
                stream.seek(minimum_hook_event_bytes)
                raw = stream.read()
        except OSError:
            raw = b""
        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("session_id") != state.get("session_id"):
                continue
            if event.get("conversation_id") != state.get("provider_session_id"):
                continue
            if event.get("launch_id") != expected_launch_id:
                continue
            event_key = json.dumps(event, sort_keys=True, separators=(",", ":"))
            if event_key in seen_events:
                continue
            seen_events.add(event_key)
            name = str(event.get("event") or "")
            if name:
                observed_events.append(name)
                event_position += 1
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("session_id") != state.get("provider_session_id"):
                continue
            if payload.get("conversation_id") != state.get("provider_session_id"):
                continue
            generation_id = str(payload.get("generation_id") or "").strip()
            if not generation_id:
                continue
            # Cursor preserves the submitted content but its hook encoder can
            # retain terminal whitespace around a PTY-originated prompt.  The
            # prompt remains exact after boundary normalization; do not loosen
            # this to substring matching because it is the causal turn anchor.
            hook_prompt = str(payload.get("prompt") or "").strip()
            if name == "beforeSubmitPrompt" and hook_prompt == expected_prompt.strip():
                matching_befores.append((event, event_position))
            # The marker, generation id, launch binding and event ordering are
            # the proof.  Requiring the entire assistant payload to equal the
            # marker rejects truthful Cursor responses that wrap it in prose or
            # markdown even though the downstream transcript oracle likewise
            # correlates by marker containment.
            elif name == "afterAgentResponse" and marker in str(payload.get("text") or ""):
                matching_afters.append((event, event_position))
        before_generations = {str((event.get("payload") or {}).get("generation_id")) for event, _ in matching_befores}
        if len(before_generations) > 1:
            raise RuntimeError(
                f"Cursor bootstrap hook observed matching prompts from multiple generations ({sorted(before_generations)!r})"
            )
        generation_id = next(iter(before_generations), "")
        matching_before = next(
            (
                (event, position)
                for event, position in matching_befores
                if str((event.get("payload") or {}).get("generation_id")) == generation_id
            ),
            None,
        )
        matching_response = next(
            (
                (event, position)
                for event, position in matching_afters
                if str((event.get("payload") or {}).get("generation_id")) == generation_id
                and matching_before is not None
                and position > matching_before[1]
            ),
            None,
        )
        if matching_before is not None and matching_response is not None and claim_status == "observed":
            try:
                end_bytes = path.stat().st_size
            except OSError:
                end_bytes = minimum_hook_event_bytes
            return {
                "start_bytes": minimum_hook_event_bytes,
                "end_bytes": end_bytes,
                "events": observed_events,
                "before_submit_prompt": matching_before[0],
                "after_agent_response": matching_response[0],
                "before_submit_prompt_position": matching_before[1],
                "after_agent_response_position": matching_response[1],
                "launch_id": expected_launch_id,
                "generation_id": generation_id,
                "hook_response_correlated": True,
                "timed_out": False,
            }
        time.sleep(0.25)
    raise RuntimeError(
        f"Cursor {label} did not publish an ordered beforeSubmitPrompt/afterAgentResponse hook sequence "
        f"(start_bytes={minimum_hook_event_bytes}, events={observed_events[-20:]!r})"
    )


def _wait_cursor_bootstrap_hook_sequence(
    state: dict[str, Any],
    environment: dict[str, str],
    *,
    marker: str,
    minimum_hook_event_bytes: int,
    timeout: float = 45.0,
) -> dict[str, Any]:
    """Require the submitted bootstrap to produce ordered native hook events."""

    return _wait_cursor_hook_sequence(
        state,
        environment,
        marker=marker,
        expected_prompt=live_session_toolkit.cursor_bootstrap_prompt(marker),
        minimum_hook_event_bytes=minimum_hook_event_bytes,
        label="bootstrap",
        timeout=timeout,
    )


def _send_initial_seed(
    spec: live_session_toolkit.ProviderSpec,
    args: argparse.Namespace,
    state: dict[str, Any],
    process: live_session_toolkit.PtyProcess,
    text: str,
) -> dict[str, Any]:
    """Submit only the initial Cursor seed through its disposable PTY path."""

    return live_session_toolkit._control_send(spec, args, state, process, text, initial=True)


def _resume_marker(provider: str, phase: str) -> str:
    """Create a provider-safe live transcript marker.

    Cursor's interactive model reliably answers the short, human-readable
    bootstrap token but may silently complete a prompt containing a long
    UUID-bearing instruction. Keep Cursor's marker compact while retaining
    enough entropy to distinguish turns in the hosted transcript. Other
    providers keep the longer marker used by their existing canaries.
    """

    if provider == "cursor":
        return f"LH_CURSOR_{phase}_{uuid.uuid4().hex[:10]}"
    legacy_phase = {
        "SEED": "RESUME_SEED",
        "STALE": "STALE",
        "POST": "RESUME_POST",
    }[phase]
    return f"LONGHOUSE_{provider.upper()}_{legacy_phase}_{uuid.uuid4().hex}"


def _resume_marker_prompt(provider: str, marker: str) -> str:
    """Return wording that makes the marker response deterministic."""

    if provider == "cursor":
        # Cursor's native product canary uses this shorter provider-facing
        # instruction.  The extra ``and no other text`` clause can leave the
        # stock Cursor TUI in its Working state without committing an
        # afterAgentResponse hook, even though the managed send returned 0.
        # Keep the marker itself exact while matching the proven product
        # interaction wording.
        return f"Reply with exactly {marker}"
    return f"Reply exactly {marker} and nothing else."


def _reconcile_opencode_process_loss(
    args: argparse.Namespace,
    state: dict[str, Any],
    environment: dict[str, str],
) -> dict[str, Any]:
    """Publish the native OpenCode terminal event after an injected loss."""

    command = [str(args.engine), "opencode-bridge", "stop", "--session-id", state["session_id"]]
    claude_dir = str(environment.get("CLAUDE_CONFIG_DIR") or "").strip()
    if claude_dir:
        command.extend(("--claude-dir", claude_dir))
    completed = subprocess.run(
        command,
        cwd=args.repo_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    receipt = {
        "method": "opencode_bridge_stop_after_process_loss",
        "returncode": completed.returncode,
        "stdout": completed.stdout[-2000:],
        "stderr": completed.stderr[-2000:],
    }
    if completed.returncode != 0:
        raise RuntimeError(f"OpenCode process-loss terminal reconciliation failed: {completed.stderr[-1000:]}")
    return receipt


def _isolated_qualification_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if source is None else source)
    for key in tuple(environment):
        if key.startswith("LONGHOUSE_MANAGED_") or key in {
            "LONGHOUSE_SESSION_ID",
            "LONGHOUSE_ANTIGRAVITY_STATE_DIR",
            "LONGHOUSE_ANTIGRAVITY_INBOX_DIR",
        }:
            environment.pop(key, None)
    return environment


def run_native_resume(provider: str, args: argparse.Namespace) -> dict[str, Any]:
    spec = SPECS[provider]
    registration = registration_for(provider)
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    provider_receipt = {
        "path": str(args.provider_bin),
        "sha256": sha256_file(args.provider_bin),
        "version": subprocess.run(
            [str(args.provider_bin), "--version"], capture_output=True, text=True, timeout=30, check=False
        ).stdout.strip(),
    }
    live_session_toolkit.write_json(root / "provider-binary-receipt.json", provider_receipt)
    home: Path | None = None
    environment = _isolated_qualification_environment()
    # Qualification providers are independent sessions, even when the harness
    # itself is launched from a managed Helm session. Never let the child
    # inherit the parent's control identity or provider-specific hook paths.
    environment["LONGHOUSE_ENGINE_BIN"] = str(args.engine)
    environment["LONGHOUSE_ORIGIN_KIND"] = "test_or_canary"
    environment["LONGHOUSE_LAUNCH_ACTOR"] = "automation"
    environment["LONGHOUSE_LAUNCH_SURFACE"] = "test"
    if spec.provider == "opencode":
        configured_model = str(environment.get("LONGHOUSE_OPENCODE_QUALIFICATION_MODEL") or "").strip()
        if configured_model:
            environment["LONGHOUSE_OPENCODE_MODEL"] = (
                configured_model if configured_model.startswith("openrouter/") else f"openrouter/{configured_model}"
            )
    initial: live_session_toolkit.PtyProcess | None = None
    resumed: live_session_toolkit.PtyProcess | None = None
    concurrent: live_session_toolkit.PtyProcess | None = None
    shipper: live_session_toolkit.TranscriptShipper | None = None
    states: list[dict[str, Any]] = []
    final_cleanup: dict[str, Any] = {"verified": False}
    result_payload: dict[str, Any] | None = None
    provider_cwd = args.repo_root
    try:
        home = live_session_toolkit.isolated_provider_home()
        # Make every provider path explicit before any provider or engine
        # process starts. The factory must never inherit the operator's home.
        environment["HOME"] = str(home)
        environment["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
        environment["CURSOR_HOME"] = str(home / ".cursor")
        if spec.provider == "opencode":
            from zerg.qa.opencode_qualification_profile import prepare_opencode_qualification_profile

            model_profile = prepare_opencode_qualification_profile(home, environment)
            live_session_toolkit.write_json(root / "opencode-model-profile-receipt.json", model_profile)
        if spec.provider == "cursor":
            # Cursor CLI loads project hooks from <cwd>/.cursor/hooks.json.
            # Use a disposable project root so the factory never modifies the
            # checked-out source tree or relies on a global provider profile.
            provider_cwd = root / "cursor-workspace"
            provider_cwd.mkdir(mode=0o700, parents=True, exist_ok=True)
            live_session_toolkit.initialize_cursor_workspace(provider_cwd)
        if spec.provider == "claude":
            onboarding = live_session_toolkit.prepare_claude_profile(
                binary=args.provider_bin,
                home=home,
                workspace=args.repo_root,
                environment=environment,
                recording=root / "claude-onboarding.tty",
            )
            live_session_toolkit.write_json(root / "claude-onboarding-receipt.json", onboarding)
        shipper = live_session_toolkit.start_transcript_shipper(
            provider,
            args,
            home=home,
            environment=environment,
            evidence_root=root,
        )
        live_session_toolkit.write_json(root / "transcript-shipper-receipt.json", shipper.receipt)
        if spec.provider == "cursor":
            cursor_hooks = subprocess.run(
                [str(args.engine), "cursor-helm", "configure-hooks", "--cursor-dir", str(provider_cwd / ".cursor")],
                cwd=args.repo_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            live_session_toolkit.write_json(
                root / "cursor-hook-configure-receipt.json",
                {
                    "returncode": cursor_hooks.returncode,
                    "stdout": cursor_hooks.stdout[-2000:],
                    "stderr": cursor_hooks.stderr[-2000:],
                    "cursor_dir": str(provider_cwd / ".cursor"),
                    "workspace_is_git_project": (provider_cwd / ".git").is_dir(),
                },
            )
            if cursor_hooks.returncode != 0:
                raise RuntimeError(f"Cursor native hook configuration failed: {cursor_hooks.stderr[-1000:]}")
        initial = live_session_toolkit.PtyProcess(
            live_session_toolkit.launch_command(
                spec,
                args,
                None,
                use_credential_files=True,
                cwd=provider_cwd,
                prompt=live_session_toolkit.cursor_bootstrap_prompt() if spec.provider == "cursor" else None,
            ),
            cwd=provider_cwd,
            env=environment,
            recording=root / "initial.tty",
        )
        initial_state = live_session_toolkit.wait_state(spec, home, process=initial)
        if spec.provider == "cursor":
            live_session_toolkit.wait_cursor_tui_ready(initial, root / "initial.tty")
        elif spec.provider == "claude":
            _wait_claude_tui_ready(initial, root / "initial.tty")
        else:
            initial.settle()
        if spec.provider == "opencode":
            live_session_toolkit.wait_opencode_tui_ready(initial, root / "initial.tty")
        states.append(initial_state)
        live_session_toolkit.write_json(root / "initial-bridge-state.json", live_session_toolkit.redact_state_for_evidence(initial_state))
        initial_provider_pid = live_session_toolkit.provider_process_pid(spec, initial_state)
        seed_marker = _resume_marker(provider, "SEED")
        initial_prior_tail = live_session_toolkit.wait_session_tail(
            args.api_url,
            args.agents_token,
            initial_state["session_id"],
            timeout=5 if spec.provider == "claude" else 45,
            allow_unprojected=spec.provider in {"claude", "cursor"},
        )
        initial_prior_assistant_event_digests = live_session_toolkit.assistant_event_digests(initial_prior_tail)
        if spec.provider == "cursor":
            # The first Cursor turn was supplied through the provider's
            # native launch argv above. Wait for that turn's identity-matched
            # hook receipt before submitting the qualification seed. The
            # seed itself uses the disposable PTY bootstrap path: the Helm
            # socket can acknowledge a send in the narrow interval after the
            # idle hook and before Cursor has made its input surface writable,
            # leaving a real request stuck in Working with only a
            # beforeSubmitPrompt hook. Resume and post-resume sends still use
            # the authoritative Helm socket below.
            live_session_toolkit.write_json(
                root / "initial-bootstrap-send.json",
                {"method": "provider_argv_bootstrap", "returncode": 0},
            )
            _wait_cursor_initial_idle(
                initial_state,
                environment,
                args,
                diagnostic_path=root / "cursor-idle-timeout-initial.json",
            )
            # The bootstrap hook can publish idle while Cursor is still
            # redrawing the input surface. Revalidate the provider-owned PTY
            # prompt immediately before the initial seed injection.
            live_session_toolkit.wait_cursor_tui_ready(initial, root / "initial.tty")
            initial_bootstrap_ship_receipt = shipper.flush("initial-bootstrap")
            live_session_toolkit.write_json(root / "initial-bootstrap-transcript-ship-receipt.json", initial_bootstrap_ship_receipt)
            _require_transcript_ship(initial_bootstrap_ship_receipt, label="initial-bootstrap")
            bootstrap_tail = live_session_toolkit.wait_session_tail(args.api_url, args.agents_token, initial_state["session_id"])
            initial_prior_assistant_event_digests = live_session_toolkit.assistant_event_digests(bootstrap_tail)
            # Keep the provider-owned store and engine projection boundary
            # beside the strict seed correlation. This is diagnostic only; a
            # missing snapshot must never turn a failed provider turn into a
            # pass or mask its causal error.
            shipper.capture_cursor_projection_diagnostics(
                initial_state,
                marker=seed_marker,
                label="initial-seed-before-send",
            )
            initial_hook_event_bytes = _cursor_hook_event_bytes(initial_state, environment)
            initial_send = _send_initial_seed(
                spec,
                args,
                initial_state,
                initial,
                _resume_marker_prompt(provider, seed_marker),
            )
        else:
            initial_send = _send_initial_seed(
                spec,
                args,
                initial_state,
                initial,
                _resume_marker_prompt(provider, seed_marker),
            )
        live_session_toolkit.write_json(root / "initial-seed-send.json", initial_send)
        if spec.provider == "cursor":
            try:
                _, initial_ship_receipt = _cursor_initial_send_then_flush(
                    initial_state,
                    environment,
                    shipper,
                    marker=seed_marker,
                    expected_prompt=_resume_marker_prompt(provider, seed_marker),
                    minimum_hook_event_bytes=initial_hook_event_bytes,
                    timeout=args.live_send_timeout_secs,
                    diagnostic_path=root / "cursor-idle-timeout-initial-seed.json",
                    hook_correlation_path=root / "initial-hook-correlation.json",
                )
            except RuntimeError as exc:
                # Keep the lifecycle gate's failure evidence even when no
                # transcript flush was safe to attempt. The artifact is
                # mandatory on a pass and useful on a fail.
                correlation_path = root / "initial-hook-correlation.json"
                if not correlation_path.exists():
                    live_session_toolkit._write_best_effort_json(
                        correlation_path,
                        {"available": False, "error": f"{type(exc).__name__}: {exc}"},
                    )
                raise
        else:
            initial_ship_receipt = shipper.flush("initial")
        live_session_toolkit.write_json(root / "initial-transcript-ship-receipt.json", initial_ship_receipt)
        if spec.provider == "cursor":
            _require_transcript_ship(initial_ship_receipt, label="initial")
        initial_tail, initial_response_correlation = live_session_toolkit.wait_assistant_response_after_marker(
            args.api_url,
            args.agents_token,
            initial_state["session_id"],
            seed_marker,
            prior_assistant_event_digests=initial_prior_assistant_event_digests,
            require_assistant_marker=spec.provider != "claude",
            timeout=args.live_send_timeout_secs,
        )
        # Write this before applying the strict predicate so failed cells
        # retain the exact observation that explains the rejection.
        live_session_toolkit._write_best_effort_json(root / "initial-response-correlation.json", initial_response_correlation)
        if not (
            initial_response_correlation["marker_observed_in_transcript"]
            and initial_response_correlation["new_assistant_events"] > 0
            and (spec.provider == "claude" or initial_response_correlation["marker_observed_in_assistant"])
        ):
            raise RuntimeError(f"provider transcript did not correlate initial {provider} marker {seed_marker}")
        # The correlated assistant response is the completed-turn oracle for
        # the initial marker. Cursor's hook may publish its next idle phase
        # late (or omit that redundant transition while the TUI is redrawing),
        # so do not make teardown depend on a second idle receipt. The Resume
        # path still requires an idle boundary before sending its bootstrap and
        # a fresh hook event after it.
        live_session_toolkit.write_json(root / "initial-transcript.jsonl", initial_tail)

        transition = live_session_toolkit.stop_session(
            spec,
            args,
            initial_state,
            initial,
            force=args.variant == "process_loss",
            environment=environment,
            stop_phase="initial",
        )
        if args.variant == "process_loss" and spec.provider == "opencode":
            transition["terminal_reconciliation"] = _reconcile_opencode_process_loss(args, initial_state, environment)
        live_session_toolkit.write_json(root / "process-transition-receipt.json", transition)
        # A provider stop commits the terminal runtime event locally.  Flush
        # that exact enrolled shipper DB before asking the host for Resume;
        # otherwise the catalog can still report run_active/contract_missing
        # even though the provider owner is already dead.
        post_stop_ship_receipt = shipper.flush("post-stop")
        live_session_toolkit.write_json(root / "post-stop-transcript-ship-receipt.json", post_stop_ship_receipt)
        if spec.provider == "cursor":
            _require_transcript_ship(post_stop_ship_receipt, label="post-stop")
        stale_marker = _resume_marker(provider, "STALE")
        try:
            live_session_toolkit._control_send(
                spec,
                args,
                initial_state,
                initial,
                _resume_marker_prompt(provider, stale_marker),
                environment=environment,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            stale = {"marker": stale_marker, "rejected": True, "error": f"{type(exc).__name__}: {exc}"}
        else:
            stale = {"marker": stale_marker, "rejected": False}
        live_session_toolkit.write_json(root / "stale-input-receipt.json", stale)

        resume_intent = _wait_resume_intent(
            spec,
            args,
            initial_state["session_id"],
            timeout=_resume_intent_timeout(variant=args.variant),
        )
        resumed_command, resume_intent_receipt = _command_from_resume_intent(
            spec,
            args,
            initial_state["session_id"],
            resume_intent,
            use_credential_files=True,
            cwd=provider_cwd,
        )
        live_session_toolkit.write_json(root / "resume-intent-receipt.json", resume_intent_receipt)
        resumed = live_session_toolkit.PtyProcess(
            resumed_command,
            cwd=provider_cwd,
            env=environment,
            recording=root / "native-resume.tty",
        )
        resumed_state = live_session_toolkit.wait_state(
            spec,
            home,
            session_id=initial_state["session_id"],
            prior_run_id=str(initial_state["run_id"]),
            process=resumed,
        )
        if spec.provider == "cursor":
            live_session_toolkit.wait_cursor_tui_ready(resumed, root / "native-resume.tty")
        elif spec.provider == "claude":
            _wait_claude_tui_ready(resumed, root / "native-resume.tty")
        else:
            resumed.settle()
        if spec.provider == "opencode":
            live_session_toolkit.wait_opencode_tui_ready(resumed, root / "native-resume.tty")
        if spec.provider == "cursor":
            # Do not put the bootstrap prompt in Cursor's resume argv. Cursor
            # can render that prompt before its conversation restore is
            # complete, leaving it in Working forever. The resumed claim is
            # pending until Cursor handles its first foreground turn, so an
            # idle-hook wait here would deadlock. The TUI readiness wait gives
            # restoration a bounded settling window; submit exactly one
            # bootstrap through the provider PTY, then require the fresh hook
            # event before using the authoritative managed socket.
            bootstrap_marker = _resume_marker(provider, "BOOTSTRAP")
            bootstrap_prior_tail = live_session_toolkit.wait_session_tail(
                args.api_url,
                args.agents_token,
                resumed_state["session_id"],
            )
            bootstrap_prior_assistant_event_digests = live_session_toolkit.assistant_event_digests(bootstrap_prior_tail)
            bootstrap_hook_event_bytes = _cursor_hook_event_bytes(resumed_state, environment)
            bootstrap_send = live_session_toolkit._control_send(
                spec,
                args,
                resumed_state,
                resumed,
                live_session_toolkit.cursor_bootstrap_prompt(bootstrap_marker),
                initial=True,
            )
            live_session_toolkit.write_json(
                root / "resume-bootstrap-send.json",
                bootstrap_send,
            )
            bootstrap_hook_sequence = _wait_cursor_bootstrap_hook_sequence(
                resumed_state,
                environment,
                marker=bootstrap_marker,
                minimum_hook_event_bytes=bootstrap_hook_event_bytes,
            )
            bootstrap_ship_receipt = _cursor_idle_then_flush(
                resumed_state,
                environment,
                shipper,
                label="resume-bootstrap",
                minimum_hook_event_bytes=bootstrap_hook_event_bytes,
                marker=bootstrap_marker,
                diagnostic_path=root / "cursor-idle-timeout-resume-bootstrap.json",
            )
            live_session_toolkit.write_json(root / "resume-bootstrap-transcript-ship-receipt.json", bootstrap_ship_receipt)
            _require_transcript_ship(bootstrap_ship_receipt, label="resume-bootstrap")
            bootstrap_tail, bootstrap_response_correlation = live_session_toolkit.wait_assistant_response_after_marker(
                args.api_url,
                args.agents_token,
                resumed_state["session_id"],
                bootstrap_marker,
                prior_assistant_event_digests=bootstrap_prior_assistant_event_digests,
                require_assistant_marker=True,
                timeout=args.live_send_timeout_secs,
            )
            bootstrap_response_correlation["hook_sequence"] = bootstrap_hook_sequence
            bootstrap_response_correlation.update(
                _cursor_bootstrap_correlation(
                    bootstrap_response_correlation,
                    bootstrap_hook_sequence,
                    bootstrap_ship_receipt,
                )
            )
            live_session_toolkit.write_json(root / "resume-bootstrap-response-correlation.json", bootstrap_response_correlation)
            if not bootstrap_response_correlation["bootstrap_correlated"]:
                raise RuntimeError(f"provider transcript did not correlate resumed Cursor bootstrap marker {bootstrap_marker}")
            live_session_toolkit.write_json(root / "resume-bootstrap-transcript.jsonl", bootstrap_tail)
            # Cursor publishes its afterAgentResponse hook before the TUI has
            # necessarily finished redrawing the restored input surface. The
            # Helm socket can therefore accept the next send while Cursor is
            # still in its Working phase, leaving the provider with no
            # foreground turn. Re-check the provider-owned prompt after the
            # bootstrap response and before issuing the post-resume marker.
            live_session_toolkit.wait_cursor_tui_ready(resumed, root / "native-resume.tty")
            # The visual TUI readiness check is not the control authority. A
            # completed transcript can arrive while Cursor still owns a
            # foreground turn, and the Helm socket must reject sends during
            # that interval. Require the identity-matched provider hook to
            # publish idle before issuing the post-resume marker.
            live_session_toolkit._wait_cursor_idle(
                resumed_state,
                environment,
                timeout=args.live_send_timeout_secs,
                minimum_hook_event_bytes=bootstrap_hook_event_bytes,
                expected_generation_id=bootstrap_hook_sequence.get("generation_id"),
                diagnostic_path=root / "cursor-idle-timeout-resumed.json",
            )
        states.append(resumed_state)
        live_session_toolkit.write_json(root / "resumed-bridge-state.json", live_session_toolkit.redact_state_for_evidence(resumed_state))
        resumed_provider_pid = live_session_toolkit.provider_process_pid(spec, resumed_state)
        post_marker = _resume_marker(provider, "POST")
        prior_tail = live_session_toolkit.wait_session_tail(
            args.api_url,
            args.agents_token,
            resumed_state["session_id"],
        )
        prior_assistant_event_digests = live_session_toolkit.assistant_event_digests(prior_tail)
        post_resume_hook_event_bytes: int | None = None
        if spec.provider == "cursor":
            # The managed send returns once the Helm socket accepts the
            # request, not when Cursor has persisted the completed turn. Keep
            # the hook boundary separate from transcript projection so the
            # shipper cannot be flushed during that persistence window.
            post_resume_hook_event_bytes = _cursor_hook_event_bytes(resumed_state, environment)
        post_send = live_session_toolkit._control_send(
            spec,
            args,
            resumed_state,
            resumed,
            _resume_marker_prompt(provider, post_marker),
            environment=environment,
        )
        live_session_toolkit.write_json(root / "post-resume-send.json", post_send)
        if spec.provider == "cursor":
            try:
                post_resume_hook_sequence = _wait_cursor_hook_sequence(
                    resumed_state,
                    environment,
                    marker=post_marker,
                    expected_prompt=_resume_marker_prompt(provider, post_marker),
                    minimum_hook_event_bytes=post_resume_hook_event_bytes or 0,
                    label="post-resume",
                    timeout=args.live_send_timeout_secs,
                )
            except RuntimeError as exc:
                if "did not publish an ordered beforeSubmitPrompt/afterAgentResponse hook sequence" not in str(exc):
                    raise
                # Cursor's hook stream is provider-owned activity evidence, not
                # the durability authority.  A late or missing terminal hook
                # must not hide a Runtime Host projection that already proves
                # the exact new assistant response.  Keep the hook failure in
                # the evidence and let the strict transcript correlation below
                # decide whether this turn is admissible.
                post_resume_hook_sequence = {
                    "available": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "fallback": "runtime_host_transcript_projection",
                }
            live_session_toolkit.write_json(root / "post-resume-hook-correlation.json", post_resume_hook_sequence)
        hook_fallback = spec.provider == "cursor" and post_resume_hook_sequence.get("available") is False
        if hook_fallback:
            # The background shipper may already have projected the response.
            # Wait for that exact marker/new-assistant proof before forcing a
            # scan; flushing while Cursor is still working recreates the race
            # this canary is intended to catch.
            resumed_tail, response_correlation = live_session_toolkit.wait_assistant_response_after_marker(
                args.api_url,
                args.agents_token,
                resumed_state["session_id"],
                post_marker,
                prior_assistant_event_digests=prior_assistant_event_digests,
                require_assistant_marker=spec.provider != "claude",
                timeout=args.live_send_timeout_secs,
            )
            if not _post_resume_response_correlated(provider, response_correlation):
                live_session_toolkit.write_json(root / "post-resume-response-correlation.json", response_correlation)
                raise RuntimeError(f"provider transcript did not correlate post-resume {provider} marker {post_marker}")
            post_resume_ship_receipt = shipper.flush("post-resume")
        else:
            post_resume_ship_receipt = (
                _cursor_idle_then_flush(
                    resumed_state,
                    environment,
                    shipper,
                    label="post-resume",
                    minimum_hook_event_bytes=post_resume_hook_event_bytes,
                    expected_generation_id=(post_resume_hook_sequence.get("generation_id") if spec.provider == "cursor" else None),
                    marker=post_marker,
                    diagnostic_path=root / "cursor-idle-timeout-post-resume.json",
                )
                if spec.provider == "cursor"
                else shipper.flush("post-resume")
            )
        live_session_toolkit.write_json(root / "post-resume-transcript-ship-receipt.json", post_resume_ship_receipt)
        if spec.provider == "cursor":
            # A failed forced scan is harness/runtime evidence, not a provider
            # transcript finding. Preserve the receipt and stop here so a 5xx
            # capability outage cannot be rewritten as "marker absent".
            _require_transcript_ship(post_resume_ship_receipt, label="post-resume")
        # Re-read after the forced scan so the retained final correlation
        # remains the authority even if the first observation raced the
        # shipper restart.
        resumed_tail, response_correlation = live_session_toolkit.wait_assistant_response_after_marker(
            args.api_url,
            args.agents_token,
            resumed_state["session_id"],
            post_marker,
            prior_assistant_event_digests=prior_assistant_event_digests,
            require_assistant_marker=spec.provider != "claude",
            timeout=args.live_send_timeout_secs,
        )
        live_session_toolkit.write_json(root / "post-resume-response-correlation.json", response_correlation)
        post_resume_response_correlated = _post_resume_response_correlated(provider, response_correlation)
        post_resume_provider_activity = response_correlation["new_assistant_events"] > 0
        if not post_resume_response_correlated:
            raise RuntimeError(f"provider transcript did not correlate post-resume {provider} marker {post_marker}")
        live_session_toolkit.write_json(root / "resumed-transcript.jsonl", resumed_tail)
        post_resume_marker_observed = response_correlation["marker_observed_in_assistant"]
        stale_generation_dispatched = live_session_toolkit._assistant_contains(resumed_tail, stale_marker)

        concurrent = live_session_toolkit.PtyProcess(
            list(resumed_command),
            cwd=provider_cwd,
            env=environment,
            recording=root / "concurrent-resume-attempt.tty",
        )
        concurrent_exit = concurrent.wait(10)
        concurrent_refused = concurrent_exit not in {None, 0} and resumed.process.poll() is None
        if concurrent_exit is None:
            concurrent.kill_group(signal.SIGKILL)
            concurrent.wait(5)
        concurrent_receipt = {
            "rejected": concurrent_refused,
            "exit_code": concurrent_exit,
            "active_owner_preserved": resumed.process.poll() is None,
        }
        live_session_toolkit.write_json(root / "concurrent-resume-receipt.json", concurrent_receipt)

        final_transition = live_session_toolkit.stop_session(
            spec,
            args,
            resumed_state,
            resumed,
            force=False,
            environment=environment,
            stop_phase="final",
        )
        live_session_toolkit.write_json(root / "final-process-transition-receipt.json", final_transition)
        final_cleanup = live_session_toolkit.cleanup_processes(spec, (initial, resumed, concurrent), states)
        live_session_toolkit.write_json(root / "cleanup-receipt.json", final_cleanup)
        if shipper is not None:
            live_session_toolkit.write_json(root / "transcript-shipper-receipt.json", shipper.stop())
        _close_recordings((concurrent, resumed, initial))
        redacted = live_session_toolkit.secret_scan(root, list(live_session_toolkit.qualification_secrets(environment, args.agents_token)))
        observation = {
            "variant": args.variant,
            "same_longhouse_session": resumed_state["session_id"] == initial_state["session_id"],
            "same_provider_thread": resumed_state["provider_session_id"] == initial_state["provider_session_id"],
            "new_run": resumed_state["run_id"] != initial_state["run_id"],
            "new_connection": resumed_state["connection_id"] != initial_state["connection_id"],
            "new_app_server_process": resumed_provider_pid != initial_provider_pid,
            "initial_provider_pid": initial_provider_pid,
            "resumed_provider_pid": resumed_provider_pid,
            "provider_neutral_resume_intent": resume_intent_receipt["identity_valid"] is True,
            "native_resume_command": (
                spec.resume_flag in resumed_command
                and resumed_command[resumed_command.index(spec.resume_flag) + 1] == initial_state["session_id"]
            ),
            "bridge_subscribed": all(
                resumed_state.get(field) for field in ("session_id", "provider_session_id", "run_id", "connection_id")
            ),
            "post_resume_provider_activity": post_resume_provider_activity,
            "post_resume_response_correlated": post_resume_response_correlated,
            "post_resume_marker_in_assistant_transcript": spec.provider != "claude" and post_resume_marker_observed,
            "stale_input_rejected": stale["rejected"] is True,
            "stale_generation_dispatched": stale_generation_dispatched,
            "concurrent_resume_refused": concurrent_refused,
            "artifact_secret_scan_passed": not redacted,
            "clean_stop_verified": args.variant == "clean_exit" and transition["clean"] and final_transition["clean"],
            "old_bridge_process_dead": args.variant == "process_loss" and transition["dead"],
            "old_app_server_process_dead": args.variant == "process_loss" and transition["provider_process_dead"],
            "replacement_started_after_process_loss": args.variant == "process_loss" and resumed_state["run_id"] != initial_state["run_id"],
            "final_cleanup_verified": final_cleanup["verified"],
            "final_socket_absent": final_cleanup["final_socket_absent"],
            "orphan_count": final_cleanup["orphan_count"],
        }
        assertions = native_resume_assertions(args.variant, observation)
        result = {
            "schema_version": 1,
            "artifact_kind": "direct_native_resume_result",
            "producer": registration.to_dict(),
            "provider": provider,
            "variant": args.variant,
            "scenario_id": registration.scenario_id,
            "scenario_revision": registration.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": now(),
            "status": "pass" if assertions["native_provider_resume_proven"] else "fail",
            "observation": observation,
            "assertions": assertions,
            "session_id": initial_state["session_id"],
            "provider_thread_id": initial_state["provider_session_id"],
            "provider_binary": provider_receipt,
            "seed_marker": seed_marker,
            "post_resume_marker": post_marker,
            "artifact_manifest": artifact_manifest(root),
        }
        result_payload = result
        live_session_toolkit.write_json(root / "result.json", result)
        return result
    except Exception as exc:  # noqa: BLE001 - retain the exact causal failure
        if not (root / "post-resume-response-correlation.json").exists():
            live_session_toolkit.write_json(
                root / "post-resume-response-correlation.json",
                {
                    "available": False,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        try:
            if home is not None:
                live_session_toolkit.write_json(
                    root / "state-candidates.json", live_session_toolkit._state_candidate_diagnostics(spec, home)
                )
        except (OSError, TypeError):
            pass
        try:
            final_cleanup = live_session_toolkit.cleanup_processes(spec, (initial, resumed, concurrent), states)
        except Exception as cleanup_exc:  # noqa: BLE001 - preserve the provider failure
            final_cleanup = {
                "verified": False,
                "teardown_error": f"{type(cleanup_exc).__name__}: {cleanup_exc}",
            }
        try:
            live_session_toolkit.write_json(root / "cleanup-receipt.json", final_cleanup)
        except OSError:
            pass
        if shipper is not None:
            try:
                live_session_toolkit.write_json(root / "transcript-shipper-receipt.json", shipper.stop())
            except Exception:  # noqa: BLE001 - preserve the provider failure
                pass
        try:
            _close_recordings((concurrent, resumed, initial))
        except Exception:  # noqa: BLE001 - preserve the provider failure
            pass
        try:
            redacted = live_session_toolkit.secret_scan(
                root, list(live_session_toolkit.qualification_secrets(environment, args.agents_token))
            )
        except OSError:
            redacted = []
        failure_code = (
            "runtime_host_registration_temporarily_unavailable"
            if isinstance(exc, live_session_toolkit.RuntimeHostRegistrationTransient)
            else "direct_native_resume_failed"
        )
        failure = {
            "schema_version": 1,
            "artifact_kind": "direct_native_resume_result",
            "producer": registration.to_dict(),
            "provider": provider,
            "variant": args.variant,
            "scenario_id": registration.scenario_id,
            "scenario_revision": registration.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": now(),
            "status": "fail",
            "failure_code": failure_code,
            "error": f"{type(exc).__name__}: {exc}",
            "redacted_secret_files": redacted,
            "artifact_manifest": artifact_manifest(root),
        }
        result_payload = failure
        live_session_toolkit.write_json(root / "result.json", failure)
        return failure
    finally:
        final_redacted: list[str] = []
        finalization_errors: list[str] = []
        if shipper is not None:
            try:
                live_session_toolkit.write_json(root / "transcript-shipper-receipt.json", shipper.stop())
            except Exception as exc:  # noqa: BLE001 - preserve the causal result
                finalization_errors.append(f"transcript shipper stop: {type(exc).__name__}: {exc}")
        if not final_cleanup.get("verified"):
            try:
                final_cleanup = live_session_toolkit.cleanup_processes(spec, (initial, resumed, concurrent), states)
            except Exception as cleanup_exc:  # noqa: BLE001 - preserve the causal result
                final_cleanup = {
                    "verified": False,
                    "teardown_error": f"{type(cleanup_exc).__name__}: {cleanup_exc}",
                }
                finalization_errors.append(f"final cleanup: {type(cleanup_exc).__name__}: {cleanup_exc}")
            try:
                live_session_toolkit.write_json(root / "cleanup-receipt.json", final_cleanup)
            except OSError as exc:
                finalization_errors.append(f"cleanup receipt: {type(exc).__name__}: {exc}")
        try:
            _close_recordings((concurrent, resumed, initial))
        except Exception as exc:  # noqa: BLE001 - preserve the causal result
            finalization_errors.append(f"recording close: {type(exc).__name__}: {exc}")
        try:
            live_session_toolkit.bound_terminal_recordings(root, provider=provider, states=states)
        except Exception as exc:  # noqa: BLE001 - a would-be pass needs bounded, sealed diagnostics
            finalization_errors.append(f"terminal evidence bounding: {type(exc).__name__}: {exc}")
        try:
            # Teardown can write receipts and flush terminal recordings. Scan
            # those final bytes before pinning their manifest digests.
            final_redacted = live_session_toolkit.secret_scan(
                root, list(live_session_toolkit.qualification_secrets(environment, args.agents_token))
            )
        except OSError as exc:
            finalization_errors.append(f"final secret scan: {type(exc).__name__}: {exc}")
        finally:
            # The result is written before this final cleanup guard runs.
            # Refresh after the guard so teardown cannot invalidate its
            # manifest, while keeping the in-memory return value identical.
            _finalize_result_payload(
                root,
                result_payload,
                redacted_files=final_redacted,
                finalization_errors=finalization_errors,
            )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--variant", required=True, choices=("clean_exit", "process_loss"))
    value.add_argument("--evidence-root", required=True, type=Path)
    value.add_argument("--repo-root", required=True, type=Path)
    value.add_argument("--engine", required=True, type=Path)
    value.add_argument("--longhouse-cli", required=True, type=Path)
    value.add_argument("--provider-bin", required=True, type=Path)
    value.add_argument("--live-send-timeout-secs", type=int, default=180)
    return value


def main_for(provider: str, argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(registration_for(provider).to_dict(), indent=2, sort_keys=True))
        return 0
    args = parser().parse_args(arguments)
    args.api_url = os.environ.get(live_session_toolkit.RUNTIME_API_URL_ENV, "")
    args.agents_token = os.environ.get(live_session_toolkit.RUNTIME_AGENTS_TOKEN_ENV, "")
    if not args.api_url or not args.agents_token:
        print(json.dumps({"status": "fail", "failure_code": "runtime_host_control_credentials_missing"}))
        return 2
    for path, label in (
        (args.engine, "longhouse_engine"),
        (args.longhouse_cli, "longhouse_cli"),
        (args.provider_bin, f"{provider}_binary"),
    ):
        if not path.is_file() or not os.access(path, os.X_OK):
            print(json.dumps({"status": "fail", "failure_code": f"{label}_missing"}))
            return 2
    result = run_native_resume(provider, args)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


__all__ = ["SPECS", "main_for", "registration_for", "run_native_resume"]
