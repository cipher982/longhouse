#!/usr/bin/env python3
"""Direct stock-provider Helm Resume producer shared by non-Codex adapters.

The provider-specific modules only declare registration.  This module owns the
one black-box transaction: the shipped ``longhouse`` facade launches a real
provider TUI in a PTY, the Runtime Host transcript proves provider activity,
the old owner is stopped or lost, and the same facade performs native Resume.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import re
import signal
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from typing import Mapping

from zerg.qa import live_session_toolkit
from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run
from zerg.qa.provider_console_lifecycle import _served_run_terminal_evidence
from zerg.qa.provider_release_identity import now
from zerg.qa.provider_release_identity import sha256_file
from zerg.qa.provider_resume_oracles import native_resume_assertions
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.wait_kind import LONGHOUSE_ENGINE
from zerg.qa.wait_kind import LONGHOUSE_INGEST
from zerg.qa.wait_kind import PROVIDER_PROCESS
from zerg.qa.wait_kind import PROVIDER_TURN
from zerg.qa.wait_kind import RUNTIME_HOST
from zerg.qa.wait_kind import expired

_DEFAULT_RESUME_INTENT_TIMEOUT_SECS = 45.0
_PROCESS_LOSS_RESUME_INTENT_TIMEOUT_SECS = 180.0
_CURSOR_SHUTDOWN_BARRIER_TIMEOUT_SECS = 30.0
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
        # +1 for each: a failed result carries the binary receipt, and the result is made after teardown.
        # +1 for each: a process that has exited counts as stopped before its parent collects it (live_kit.pid_dead).
        scenario_revision={"cursor": 12, "opencode": 8}.get(provider, 7),
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


def _settle_finalization(run: Run, *, redacted_files: list[str], finalization_errors: list[str]) -> None:
    """Fold what teardown found into the run, without certifying incomplete evidence.

    The result is made after this, so its manifest binds what teardown wrote.
    A run that had held every assertion over evidence that could not be
    finalized, or that still contained a qualification secret, did not pass.
    """

    if redacted_files:
        run.fields["redacted_secret_files"] = sorted(set(run.fields.get("redacted_secret_files", [])) | set(redacted_files))
    if finalization_errors:
        failure_code, error = "finalization_failed", "; ".join(finalization_errors)
    elif redacted_files:
        failure_code, error = "finalized_artifact_secret_scan_failed", "finalized evidence contained a qualification secret"
    else:
        return
    if run.observation is not None and run.assertions and all(run.assertions.values()):
        run.fields.update(failure_code=failure_code, error=error)
        run.observation["artifact_secret_scan_passed"] = False
        run.assertions["native_provider_resume_proven"] = False


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
        live_session_toolkit._accept_latest_claude_runtime_prompt(process)
        terminal = live_session_toolkit._terminal_text(process.recording)
        if _claude_input_prompt_visible(terminal):
            process.settle()
            return
        time.sleep(0.1)
    raise expired(RuntimeError("Claude TUI did not publish its input prompt"), PROVIDER_PROCESS)


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
    raise expired(RuntimeError(f"provider-neutral Resume intent remained unavailable: {last_reason}"), RUNTIME_HOST)


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


def _hosted_correlation_waited_on(
    spec: live_session_toolkit.ProviderSpec,
    state: Mapping[str, Any],
    *,
    marker: str,
    home: Path | None,
    environment: Mapping[str, str],
) -> str | None:
    """Which side stopped when the hosted transcript never correlated ``marker``.

    That wait covers two things: the provider answers the prompt that carries
    the marker, then Longhouse ships and serves the answer. The provider's own
    record says which half is missing. For Cursor that is its hook stream, for
    Claude its transcript, for OpenCode its message store. A prompt the
    provider holds with no finished turn after it is the provider's turn; a
    finished turn is Longhouse's ingest. A prompt the provider never took, or
    a record that cannot be read, declares nothing: this is a best-effort look
    after a failure and must never replace it.
    """

    try:
        if spec.provider == "cursor":
            from zerg.qa import cursor_helm_product_e2e

            longhouse_home = str(environment.get("LONGHOUSE_HOME") or "").strip()
            if not longhouse_home:
                return None
            hook_root = Path(longhouse_home) / "managed-local" / "cursor-helm"
            return cursor_helm_product_e2e.hosted_reply_waited_on(
                cursor_helm_product_e2e._hook_rows(hook_root, str(state["session_id"])), marker
            )
        if spec.provider == "claude":
            from zerg.qa import claude_helm_lifecycle

            if home is None or not state.get("provider_session_id"):
                return None
            rows = claude_helm_lifecycle._transcript_rows(str(state["provider_session_id"]), home)
            bounds = claude_helm_lifecycle._turn_bounds(rows, marker)
            if bounds is None:
                return None
            return LONGHOUSE_INGEST if bounds[1] is not None else PROVIDER_TURN
        if spec.provider == "opencode":
            from zerg.qa import opencode_helm_lifecycle

            rows = opencode_helm_lifecycle._summarize(opencode_helm_lifecycle._messages(dict(state)))
            prompt_id = opencode_helm_lifecycle._user_message_id(rows, marker)
            if prompt_id is None:
                return None
            chain = opencode_helm_lifecycle._chain(rows, prompt_id)
            if any(row["finish"] == "stop" for row in chain):
                return LONGHOUSE_INGEST
            # A turn that ended in an error of the provider's own is a result, not a stall.
            return None if any(row["error"] for row in chain) else PROVIDER_TURN
    except Exception:  # noqa: BLE001 - evidence about a failure, never a second failure
        return None
    return None


# Cursor answered, the engine shipped the turn's records, and the hosted
# transcript did not show the reply inside the wait. A fault of Longhouse's
# hosted side, named so that it is its own result and never read as "the
# provider did not answer" or absorbed by another kind of evidence.
HOSTED_TRANSCRIPT_MISSING_SHIPPED_REPLY = "hosted_transcript_missing_shipped_reply"


def _uncorrelated(
    message: str,
    spec: live_session_toolkit.ProviderSpec,
    state: Mapping[str, Any],
    *,
    marker: str,
    home: Path | None,
    environment: Mapping[str, str],
    shipper: live_session_toolkit.TranscriptShipper | None = None,
    label: str | None = None,
    correlation: dict[str, Any] | None = None,
    receipt: Path | None = None,
) -> RuntimeError:
    """The "did not correlate" failure, marked with the side that stopped when that is known.

    For Cursor it also retains where the reply was when the wait gave up: a
    metadata-only snapshot of Cursor's own store and of the engine's capture
    and delivery state (``cursor-projection-diagnostics-<label>-at-timeout``).
    The snapshots taken around the flush predate the hosted wait, so until
    this one a failure could say the reply had been in Cursor's store minutes
    earlier, and nothing about the moment it was judged missing.

    That snapshot says whether the engine delivered the turn
    (`live_session_toolkit.cursor_turn_delivery`), and with Cursor's hooks it
    settles which fault this is. Cursor answered and the engine captured and
    shipped the turn: the hosted transcript is what failed, and the failure
    opens with ``HOSTED_TRANSCRIPT_MISSING_SHIPPED_REPLY``. Cursor's records
    hold the reply and the engine had not captured or shipped all of it: the
    engine is what was awaited. A snapshot that cannot say, or records of
    Cursor's that do not show the reply, change nothing. Either way the
    delivery, and the named observation when it holds, are written to the
    step's ``correlation`` receipt.
    """

    kind = _hosted_correlation_waited_on(spec, state, marker=marker, home=home, environment=environment)
    gap: dict[str, Any] | None = None
    if spec.provider == "cursor" and shipper is not None and label:
        delivery = shipper.cursor_turn_delivery(dict(state), marker=marker, label=f"{label}-at-timeout")
        if kind == LONGHOUSE_INGEST and delivery["known"] and delivery["source_holds_prompt_and_reply"]:
            if delivery["delivered"]:
                gap = {
                    "reason": HOSTED_TRANSCRIPT_MISSING_SHIPPED_REPLY,
                    "provider_answered": True,
                    "engine_delivery": delivery,
                }
            else:
                # Cursor's own records hold the reply and the engine has not
                # captured or shipped all of it.
                kind = LONGHOUSE_ENGINE
        if correlation is not None:
            correlation["engine_delivery"] = delivery
            correlation["hosted_transcript_gap"] = gap
            if receipt is not None:
                live_session_toolkit._write_best_effort_json(receipt, correlation)
    if gap is not None:
        delivery = gap["engine_delivery"]
        message = (
            f"{HOSTED_TRANSCRIPT_MISSING_SHIPPED_REPLY}: {message}: Cursor answered and the engine shipped the turn "
            f"(the store's latest root captured, {delivery['store_shipped_records']} of {delivery['store_captured_records']} "
            f"store records and {delivery['projection_shipped_bytes']} of {delivery['projection_bytes']} transcript bytes "
            "shipped, nothing pending), and the hosted transcript did not show the reply"
        )
    error = RuntimeError(message)
    return expired(error, kind) if kind else error


def _with_hook_sequence(correlation: dict[str, Any], hook_sequence: Mapping[str, Any] | None) -> dict[str, Any]:
    """A hosted-transcript correlation receipt that also says what the provider's own hooks showed.

    The hosted transcript is the authority for the verdict. The hook result is
    what tells a reply the provider never gave from one Longhouse never served,
    and a receipt without it leaves that to a second file that a failure may
    not have written.
    """

    return {**correlation, "hook_sequence": dict(hook_sequence)} if hook_sequence else correlation


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
        typed_into_provider=True,
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
) -> dict[str, Any]:
    """Whether the resumed Cursor's bootstrap reply reached the hosted transcript.

    The hosted transcript is the authority, here as for every other marker of
    this proof. Cursor's hook says whether Cursor answered, and is recorded
    beside it, but a reply the hosted transcript never showed is not proven
    by the hook. It used to be: a bootstrap whose hook passed was admitted
    when the one flush after it had shipped at least one event. Whether that
    flush or the engine's daemon shipped the turn's records is chance: in all
    40 retained runs where the hosted transcript lacked the reply the engine
    had shipped every record it had captured and the whole raw transcript,
    with nothing pending, and the flush had shipped events in 21 of them. So
    the same condition passed in one run and failed in the next. Those
    snapshots do not say whether the engine had captured the store's latest
    root, which is what tells a hosted fault from an engine one; the snapshot
    records it now, and `_uncorrelated` names the result from it.
    """

    transcript_projection_correlated = bool(
        transcript_correlation.get("marker_observed_in_transcript")
        and transcript_correlation.get("marker_observed_in_assistant")
        and isinstance(transcript_correlation.get("new_assistant_events"), int)
        and not isinstance(transcript_correlation.get("new_assistant_events"), bool)
        and transcript_correlation["new_assistant_events"] > 0
    )
    return {
        "transcript_projection_correlated": transcript_projection_correlated,
        "hook_response_correlated": hook_sequence.get("hook_response_correlated") is True,
        "method": "transcript_projection" if transcript_projection_correlated else "unverified",
        "bootstrap_correlated": transcript_projection_correlated,
        # Set by `_uncorrelated` when the reply was shipped and never served.
        "hosted_transcript_gap": None,
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
    typed_into_provider: bool = False,
) -> dict[str, Any]:
    """Require one submitted Cursor turn to produce ordered native hook events.

    ``sessionStart`` is an idle phase, but it does not prove that the prompt
    written to the PTY was accepted.  The only provider-owned sequence that
    establishes a real foreground turn is ``beforeSubmitPrompt`` followed by
    ``afterAgentResponse`` for this session and retained conversation.  The
    prompt, response text, and generation identity are part of the predicate;
    an unrelated hook response must never authorize the transcript fallback.

    ``typed_into_provider`` says the prompt was written to Cursor's own
    terminal rather than sent through Longhouse. It decides what an expired
    wait was waiting on when Cursor never took the prompt at all.
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
    matching_before: tuple[dict[str, Any], int] | None = None
    matching_response: tuple[dict[str, Any], int] | None = None
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
    error = RuntimeError(
        f"Cursor {label} did not publish an ordered beforeSubmitPrompt/afterAgentResponse hook sequence "
        f"(start_bytes={minimum_hook_event_bytes}, events={observed_events[-20:]!r})"
    )
    if matching_before is not None and matching_response is not None:
        # Cursor took the prompt and answered it, in the same generation and
        # in that order; the engine's binding claim for the launch never
        # reached `observed`.
        raise expired(error, LONGHOUSE_ENGINE)
    if matching_befores:
        # Cursor took the prompt and this turn has no answer. A response that
        # names the marker under another generation is another turn's, and is
        # not what this one was waiting for.
        raise expired(error, PROVIDER_TURN)
    if typed_into_provider:
        # Typed into Cursor's own terminal and never submitted by it.
        raise expired(error, PROVIDER_PROCESS)
    # Sent through Longhouse and never seen by Cursor: this record cannot say
    # which of the two dropped it.
    raise error


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
        typed_into_provider=True,
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


def _terminate_cursor_owner(
    args: argparse.Namespace,
    state: Mapping[str, Any],
    environment: Mapping[str, str],
) -> dict[str, Any]:
    """Use Cursor's supported terminate path before forced process cleanup.

    Killing the PTY group first leaves the launcher's Unix socket behind because
    the launcher's normal drop cleanup cannot run after SIGKILL.  Keep this
    operation best-effort so the original provider failure remains authoritative;
    ``cleanup_processes`` still proves the final process and endpoint state.
    """

    session_id = str(state.get("session_id") or "").strip()
    receipt: dict[str, Any] = {
        "method": "cursor_helm_supported_terminate",
        "session_id": session_id or None,
        "provider_session_id": state.get("provider_session_id"),
        "run_id": state.get("run_id"),
        "status": "fail",
    }
    if not session_id:
        receipt["failure_code"] = "cursor_session_identity_missing"
        return receipt

    command = [str(args.engine), "cursor-helm", "stop", "--session-id", session_id]
    try:
        completed = subprocess.run(
            command,
            cwd=args.repo_root,
            env=dict(environment),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - preserve the original provider failure
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        return receipt

    receipt.update(
        {
            "returncode": completed.returncode,
            "stdout": completed.stdout[-2000:],
            "stderr": completed.stderr[-2000:],
            "status": "pass" if completed.returncode == 0 else "fail",
        }
    )
    return receipt


def _cursor_shutdown_barrier(
    args: argparse.Namespace,
    state: Mapping[str, Any],
    process: live_session_toolkit.PtyProcess | None,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    """Separate Cursor's stop acknowledgement from completed teardown.

    ``cursor-helm stop`` acknowledges the control request before the launcher
    finishes its own socket/state cleanup and publishes the terminal event.
    Wait for those exact facts while the transcript producer is still alive;
    generic process cleanup remains a bounded fallback when the barrier fails.
    """

    receipt = _terminate_cursor_owner(args, state, environment)
    barrier: dict[str, Any] = {
        "status": "fail",
        "verified": False,
        "timeout_seconds": _CURSOR_SHUTDOWN_BARRIER_TIMEOUT_SECS,
    }
    receipt["shutdown_barrier"] = barrier
    if receipt.get("status") != "pass":
        barrier["failure_code"] = "cursor_stop_ack_failed"
        return receipt
    if process is None:
        barrier["failure_code"] = "cursor_launcher_process_missing"
        return receipt

    state_path_value = state.get("state_path")
    socket_path_value = state.get("socket_path")
    state_path = Path(state_path_value) if isinstance(state_path_value, str) and state_path_value else None
    socket_path = Path(socket_path_value) if isinstance(socket_path_value, str) and socket_path_value else None
    session_id = str(state.get("session_id") or "")
    run_id = str(state.get("run_id") or "")
    deadline = time.monotonic() + _CURSOR_SHUTDOWN_BARRIER_TIMEOUT_SECS
    terminal_evidence: dict[str, Any] = {
        "retired": False,
        "session_id": session_id,
        "expected_run_id": run_id,
        "error": "terminal evidence not observed",
    }
    launcher_exit_code: int | None = None
    launcher_wait_error: str | None = None
    process_group_dead = False
    while time.monotonic() < deadline:
        try:
            remaining = max(0.0, deadline - time.monotonic())
            if process.process.poll() is None and remaining > 0:
                launcher_exit_code = process.wait(min(0.25, remaining))
            else:
                launcher_exit_code = process.process.poll()
        except Exception as exc:  # noqa: BLE001 - retain an honest barrier failure
            launcher_wait_error = f"{type(exc).__name__}: {exc}"
        try:
            process_group_dead = live_session_toolkit.wait_process_group_dead(process.pid, timeout=0)
        except Exception as exc:  # noqa: BLE001 - retain an honest barrier failure
            process_group_dead = False
            launcher_wait_error = launcher_wait_error or f"{type(exc).__name__}: {exc}"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            terminal_evidence = _served_run_terminal_evidence(
                args.api_url,
                args.agents_token,
                session_id,
                run_id,
                timeout=remaining,
            )
        except Exception as exc:  # noqa: BLE001 - a missing served fact is not proof
            terminal_evidence = {
                "retired": False,
                "session_id": session_id,
                "expected_run_id": run_id,
                "error": f"{type(exc).__name__}: {exc}",
            }
        launcher_exited = process.process.poll() is not None
        state_absent = state_path is not None and not state_path.exists()
        socket_absent = socket_path is not None and not socket_path.exists()
        terminal_observed = terminal_evidence.get("retired") is True
        if launcher_exited and process_group_dead and state_absent and socket_absent and terminal_observed:
            barrier.update(
                {
                    "status": "pass",
                    "verified": True,
                    "launcher_exited": True,
                    "launcher_exit_code": launcher_exit_code,
                    "process_group_dead": True,
                    "state_absent": True,
                    "socket_absent": True,
                    "served_terminal": terminal_evidence,
                }
            )
            receipt["status"] = "pass"
            return receipt
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    barrier.update(
        {
            "failure_code": "cursor_shutdown_barrier_timeout",
            "launcher_exited": process.process.poll() is not None,
            "launcher_exit_code": launcher_exit_code,
            "process_group_dead": process_group_dead,
            "state_absent": state_path is not None and not state_path.exists(),
            "socket_absent": socket_path is not None and not socket_path.exists(),
            "served_terminal": terminal_evidence,
        }
    )
    if launcher_wait_error:
        barrier["launcher_wait_error"] = launcher_wait_error
    receipt["status"] = "fail"
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


def _reach(run: Run, provider: str, args: argparse.Namespace) -> None:
    spec = SPECS[provider]
    root = run.root
    provider_receipt = {
        "path": str(args.provider_bin),
        "sha256": sha256_file(args.provider_bin),
        "version": subprocess.run(
            [str(args.provider_bin), "--version"], capture_output=True, text=True, timeout=30, check=False
        ).stdout.strip(),
    }
    live_session_toolkit.write_json(root / "provider-binary-receipt.json", provider_receipt)
    run.fields["provider_binary"] = provider_receipt
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
    initial_state: dict[str, Any] = {}
    resumed_state: dict[str, Any] = {}
    failure_termination: dict[str, Any] | None = None
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
            raise _uncorrelated(
                f"provider transcript did not correlate initial {provider} marker {seed_marker}",
                spec,
                initial_state,
                marker=seed_marker,
                home=home,
                environment=environment,
                shipper=shipper,
                label="initial",
                correlation=initial_response_correlation,
                receipt=root / "initial-response-correlation.json",
            )
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
        # Enroll the new provider owner before any resumed bootstrap or
        # projection step can fail. Cleanup must use this run identity.
        states.append(resumed_state)
        live_session_toolkit.write_json(root / "resumed-bridge-state.json", live_session_toolkit.redact_state_for_evidence(resumed_state))
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
                )
            )
            live_session_toolkit.write_json(root / "resume-bootstrap-response-correlation.json", bootstrap_response_correlation)
            if not bootstrap_response_correlation["bootstrap_correlated"]:
                raise _uncorrelated(
                    f"provider transcript did not correlate resumed Cursor bootstrap marker {bootstrap_marker}",
                    spec,
                    resumed_state,
                    marker=bootstrap_marker,
                    home=home,
                    environment=environment,
                    shipper=shipper,
                    label="resume-bootstrap",
                    correlation=bootstrap_response_correlation,
                    receipt=root / "resume-bootstrap-response-correlation.json",
                )
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
        if spec.provider == "cursor":
            # Keep the daemon alive until the authoritative store has projected
            # the response. A response hook can precede Cursor committing its
            # reachable root; pausing for a one-shot flush at that point loses
            # the later wake. Require the same assistant proof with or without
            # hook evidence before forcing the final scan.
            resumed_tail, response_correlation = live_session_toolkit.wait_assistant_response_after_marker(
                args.api_url,
                args.agents_token,
                resumed_state["session_id"],
                post_marker,
                prior_assistant_event_digests=prior_assistant_event_digests,
                require_assistant_marker=True,
                timeout=args.live_send_timeout_secs,
            )
            response_correlation = _with_hook_sequence(response_correlation, post_resume_hook_sequence)
            if not _post_resume_response_correlated(provider, response_correlation):
                live_session_toolkit.write_json(root / "post-resume-response-correlation.json", response_correlation)
                raise _uncorrelated(
                    f"provider transcript did not correlate post-resume {provider} marker {post_marker}",
                    spec,
                    resumed_state,
                    marker=post_marker,
                    home=home,
                    environment=environment,
                    shipper=shipper,
                    label="post-resume",
                    correlation=response_correlation,
                    receipt=root / "post-resume-response-correlation.json",
                )
            if not hook_fallback:
                shipper.capture_cursor_projection_diagnostics(
                    resumed_state,
                    marker=post_marker,
                    label="post-resume-before-flush",
                )
        post_resume_ship_receipt = shipper.flush("post-resume")
        if spec.provider == "cursor" and not hook_fallback:
            shipper.capture_cursor_projection_diagnostics(
                resumed_state,
                marker=post_marker,
                label="post-resume-after-flush",
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
        if spec.provider == "cursor":
            response_correlation = _with_hook_sequence(response_correlation, post_resume_hook_sequence)
        live_session_toolkit.write_json(root / "post-resume-response-correlation.json", response_correlation)
        post_resume_response_correlated = _post_resume_response_correlated(provider, response_correlation)
        post_resume_provider_activity = response_correlation["new_assistant_events"] > 0
        if not post_resume_response_correlated:
            raise _uncorrelated(
                f"provider transcript did not correlate post-resume {provider} marker {post_marker}",
                spec,
                resumed_state,
                marker=post_marker,
                home=home,
                environment=environment,
                shipper=shipper,
                label="post-resume",
                correlation=response_correlation,
                receipt=root / "post-resume-response-correlation.json",
            )
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
        run.observation = observation
        run.assertions = native_resume_assertions(args.variant, observation)
        run.fields.update(
            session_id=initial_state["session_id"],
            provider_thread_id=initial_state["provider_session_id"],
            seed_marker=seed_marker,
            post_resume_marker=post_marker,
        )
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
        failure_identity = states[-1] if states else initial_state
        if spec.provider == "cursor" and failure_identity:
            failure_process = None
            if resumed is not None and resumed_state.get("run_id") == failure_identity.get("run_id"):
                failure_process = resumed
            elif initial is not None and initial_state.get("run_id") == failure_identity.get("run_id"):
                failure_process = initial
            # Stop acknowledgement is not completion. Wait for the exact
            # launcher, state/socket teardown, and served terminal fact before
            # allowing generic forced cleanup to run.
            failure_termination = _cursor_shutdown_barrier(
                args,
                failure_identity,
                failure_process,
                environment,
            )
            if shipper is not None:
                try:
                    terminal_ship_receipt = shipper.flush("cursor-failure-terminal")
                    failure_termination["terminal_transcript_ship"] = terminal_ship_receipt
                    if terminal_ship_receipt.get("status") != "pass":
                        failure_termination["status"] = "fail"
                except Exception as ship_exc:  # noqa: BLE001 - preserve the original provider failure
                    failure_termination["status"] = "fail"
                    failure_termination["terminal_transcript_ship"] = {
                        "status": "fail",
                        "error": f"{type(ship_exc).__name__}: {ship_exc}",
                    }
                try:
                    live_session_toolkit.write_json(
                        root / "cursor-failure-terminal-transcript-ship-receipt.json",
                        failure_termination["terminal_transcript_ship"],
                    )
                except OSError as ship_receipt_exc:
                    failure_termination["status"] = "fail"
                    failure_termination["terminal_transcript_ship_receipt_error"] = f"{type(ship_receipt_exc).__name__}: {ship_receipt_exc}"
        try:
            final_cleanup = live_session_toolkit.cleanup_processes(spec, (initial, resumed, concurrent), states)
        except Exception as cleanup_exc:  # noqa: BLE001 - preserve the provider failure
            final_cleanup = {
                "verified": False,
                "teardown_error": f"{type(cleanup_exc).__name__}: {cleanup_exc}",
            }
        if failure_termination is not None:
            final_cleanup["cursor_termination"] = failure_termination
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
        run.fields["redacted_secret_files"] = redacted
        if failure_identity:
            run.fields.update(
                session_id=failure_identity.get("session_id"),
                provider_thread_id=failure_identity.get("provider_session_id"),
                run_id=failure_identity.get("run_id"),
                initial_run_id=states[0].get("run_id") if states else failure_identity.get("run_id"),
            )
        raise
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
            if failure_termination is not None:
                final_cleanup["cursor_termination"] = failure_termination
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
            _settle_finalization(run, redacted_files=final_redacted, finalization_errors=finalization_errors)


@functools.cache
def producer_for(provider: str) -> Producer:
    return Producer(
        registration_for(provider),
        artifact_kind="direct_native_resume_result",
        failure_code=lambda exc: (
            "runtime_host_registration_temporarily_unavailable"
            if isinstance(exc, live_session_toolkit.RuntimeHostRegistrationTransient)
            else "direct_native_resume_failed"
        ),
        needs=("evidence_root", "repo_root", "engine", "longhouse_cli", "provider_bin"),
        binaries=(
            ("engine", "longhouse_engine_missing"),
            ("longhouse_cli", "longhouse_cli_missing"),
            ("provider_bin", f"{provider}_binary_missing"),
        ),
        runtime_host=True,
        variants=("clean_exit", "process_loss"),
        add_arguments=lambda parser: parser.add_argument("--live-send-timeout-secs", type=int, default=180),
    )


def run_native_resume(provider: str, args: argparse.Namespace) -> dict[str, Any]:
    return producer_for(provider).execute(args.evidence_root, lambda run: _reach(run, provider, args), variant=args.variant)


def main_for(provider: str, argv: list[str] | None = None) -> int:
    return producer_for(provider).main(lambda args: run_native_resume(provider, args), argv)


__all__ = ["SPECS", "main_for", "producer_for", "registration_for", "run_native_resume"]
