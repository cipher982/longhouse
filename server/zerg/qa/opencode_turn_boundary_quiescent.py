#!/usr/bin/env python3
"""Direct managed-OpenCode turn-boundary activity producer.

Proves ``session.activity.turn_boundary`` for OpenCode
(``activity_returns_to_quiescent_at_turn_boundary``, scenario
``opencode_turn_boundary_quiescent``): launch a real Longhouse-managed
OpenCode Helm session through the shipped ``longhouse opencode`` facade, send
one real live-token prompt, and prove that observed activity leaves
quiescence while the turn is in flight and returns to (and remains at)
quiescence once the turn genuinely completes.

Launch/teardown machinery is deliberately reused from
``zerg.qa.provider_native_resume`` (``SPECS``, ``PtyProcess``,
``isolated_provider_home``, ``launch_command``, ``wait_state``,
``wait_opencode_tui_ready``, ``wait_assistant_response_after_marker``,
``stop_session``, ...) rather than re-implemented, because that module is the same,
already-tested code path backing OpenCode's registered
``opencode.native_resume.v1`` producer. Only the turn-boundary-specific
observation (quiescence of the owned terminal around one real turn) is new.

IMPORTANT — oracle_source mismatch (read before registering this producer):
schemas/managed_providers.yml declares
``oracle_source: server/zerg/qa/opencode_server_qualification.py`` for this
assertion. That file (99 lines, read in full while building this producer)
implements only the ``opencode_server_contract`` scenario (serve/reattach,
see ``opencode_server_contract_producer.py``) and contains no
turn-boundary/quiescence logic today — nor does ``codex_provider_release_
canary.py``, the analogous oracle_source declared for Codex's own
``session.activity.turn_boundary`` cell (grepped for "quiescent"/
"turn_boundary": zero matches in either file). ``session.activity.
turn_boundary`` has no existing implementation anywhere in this codebase for
any provider. Per this task's scope ("new files only, do not edit any
existing file"), this producer cannot add the missing entrypoint to
opencode_server_qualification.py, so the judgment
(``turn_boundary_quiescent_assertions`` below) is implemented locally in
this module instead. ``REGISTRATION.oracle_source`` is still set to the
exact schema-declared path (per the task's instruction not to invent
oracle_source values); ``oracle_entrypoint`` names the local function that
actually performs the judgment. Flag this mismatch for human review before
wiring this producer in.

IMPORTANT — "quiescent" here is OpenCode's own word for it, not the internal
``ActivityState`` literal: the served facts type
(``server/zerg/services/session_state_contract.py``:
``ActivityState = Literal["thinking", "executing", "quiescent", ...]``) is
the internal vocabulary this assertion's name borrows, but that field is not
exposed on the ``/api/agents/*`` machine surface this producer runs under
(``MachineSessionResponse`` — see ``session_views.py`` — deliberately narrows
out control/activity state; the browser-shaped ``SessionResponse`` that does
carry ``session_state.activity`` requires browser cookie auth, not the
``X-Agents-Token`` this sandbox provides). Quiescence is therefore proved from
the provider's authority on it: the session's own ``/session/status`` on the
``opencode serve`` it owns (the route the engine's control path and the Helm
lifecycle producer already use) reads busy while the turn runs and idle, and
stays idle, once it completes, with the turn's completion independently
correlated against the Runtime Host's served transcript. Owned-terminal byte
stability was the proxy before revision 4 and is not an oracle: OpenCode's
reasoning spinner can keep redrawing after the reply rendered and the server
already reads idle (upstream anomalyco/opencode 16646 and 17680), which failed
this cell once in 162 runs (2026-09-30 03:58Z, the first finding the factory
mailed). The terminal's behaviour is still recorded, as evidence beside the
verdict. A human should confirm whether provider status is what the capability
is meant to certify, or whether a new machine-surface field is the intended
(bigger) fix.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from zerg.qa.live_session_toolkit import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.live_session_toolkit import RUNTIME_API_URL_ENV
from zerg.qa.live_session_toolkit import PtyProcess
from zerg.qa.live_session_toolkit import TranscriptShipper
from zerg.qa.live_session_toolkit import assistant_event_digests
from zerg.qa.live_session_toolkit import isolated_provider_home
from zerg.qa.live_session_toolkit import launch_command
from zerg.qa.live_session_toolkit import opencode_session_busy
from zerg.qa.live_session_toolkit import provider_process_pid
from zerg.qa.live_session_toolkit import qualification_secrets
from zerg.qa.live_session_toolkit import redact_state_for_evidence
from zerg.qa.live_session_toolkit import require_disposable_runtime
from zerg.qa.live_session_toolkit import retain_opencode_serve_log
from zerg.qa.live_session_toolkit import start_transcript_shipper
from zerg.qa.live_session_toolkit import stop_session
from zerg.qa.live_session_toolkit import wait_assistant_response_after_marker
from zerg.qa.live_session_toolkit import wait_opencode_tui_ready
from zerg.qa.live_session_toolkit import wait_session_tail
from zerg.qa.live_session_toolkit import wait_state
from zerg.qa.live_session_toolkit import write_json
from zerg.qa.opencode_qualification_profile import prepare_opencode_qualification_profile
from zerg.qa.provider_native_resume import SPECS
from zerg.qa.provider_release_identity import artifact_manifest
from zerg.qa.provider_release_identity import now
from zerg.qa.provider_release_identity import sha256_file
from zerg.qa.resume_assurance import ProducerRegistration

_ASSERTION_ID = "activity_returns_to_quiescent_at_turn_boundary"
# The session must read idle, continuously, for this long to count as quiescent, and
# must read idle for this long again afterwards to count as having stayed so.
_QUIESCENCE_STABLE_SECONDS = 2.0

REGISTRATION = ProducerRegistration(
    producer_id="opencode.turn_boundary_quiescent.v1",
    producer_revision=4,
    scenario_id="opencode_turn_boundary_quiescent",
    scenario_revision=2,
    # The schema declares no "variant" key for this assertion cell, so the
    # authored variant is None (zerg.qa.resume_assurance.execution_variant_key
    # only treats a non-empty *string* as an authored variant; cell.get(
    # "variant") is None throughout provider_factory/cases.py for an
    # unvarianted cell). The dataclass field is typed tuple[str, str] but that
    # is documentation, not runtime-enforced -- match the real matching code
    # in _producer_supports_cell, which compares this tuple against
    # (assertion_id, cell.get("variant")) verbatim.
    assertion_cells=((_ASSERTION_ID, None),),  # type: ignore[arg-type]
    providers=("opencode",),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "managed_helm_launch",
        "turn_prompt_dispatched",
        "activity_left_quiescent_during_turn",
        "turn_completion_correlated_in_served_transcript",
        "activity_returned_to_quiescent_after_turn",
        "activity_remained_quiescent_post_turn",
    ),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=("opencode_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "opencode_model_profile_receipt",
        "transcript_shipper_receipt",
        "launch_state_receipt",
        "turn_activity_receipt",
        "turn_correlation_receipt",
        "cleanup_receipt",
    ),
    required_cleanup=(
        "managed_opencode_process_exited",
        "no_orphan_provider_processes",
    ),
    implementation="server/zerg/qa/opencode_turn_boundary_quiescent.py",
    # Schema-declared value; see the module docstring for the documented
    # mismatch between this path and where the judgment actually lives.
    oracle_source="server/zerg/qa/opencode_server_qualification.py",
    oracle_entrypoint="turn_boundary_quiescent_assertions",
    executable_module="zerg.qa.opencode_turn_boundary_quiescent",
    provider_artifact_required=True,
    executable=True,
)


def turn_boundary_quiescent_assertions(observation: dict[str, Any]) -> dict[str, bool]:
    """Pure judgment: observation -> {assertion_id: passed}.

    This is the local stand-in for the missing oracle_source entrypoint (see
    the module docstring). Every input is a plain boolean fact recorded by
    ``run_turn_boundary_quiescent`` below; this function adds no new I/O.
    """

    passed = bool(
        observation.get("activity_left_quiescent_during_turn") is True
        and observation.get("turn_completion_correlated_in_served_transcript") is True
        and observation.get("activity_returned_to_quiescent_after_turn") is True
        and observation.get("activity_remained_quiescent_post_turn") is True
    )
    return {_ASSERTION_ID: passed}


def _redact_retained_secrets(root: Path, secrets: list[str]) -> list[str]:
    encoded = [secret.encode() for secret in secrets if secret]
    redacted: list[str] = []
    if not encoded:
        return redacted
    for path in root.rglob("*"):
        if not path.is_file() or path.name == "result.json":
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        replaced = data
        for secret in encoded:
            replaced = replaced.replace(secret, b"<redacted>")
        if replaced != data:
            path.write_bytes(replaced)
            redacted.append(path.relative_to(root).as_posix())
    return redacted


def _terminal_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _pump_until(process: PtyProcess, recording: Path, *, timeout: float, predicate) -> float | None:
    """Drain the owned PTY until ``predicate(size)`` is true, or time out.

    New helper: ``zerg.qa.pty_session.wait_for_terminal_quiescence`` proves
    the same operational concept (bytes settle around a real terminal) but
    assumes a background auto-draining thread (``ProviderPtySession``).
    ``PtyProcess`` (the type every other real OpenCode producer in this
    codebase already uses) has no such thread -- its non-blocking ``drain()``
    only pulls bytes when called -- so this pumps it explicitly instead of
    duplicating a second background-thread PTY primitive.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        process.drain()
        if process.process.poll() is not None:
            raise RuntimeError("opencode Helm process exited during turn-boundary observation")
        if predicate(_terminal_size(recording)):
            return time.monotonic()
        time.sleep(0.1)
    return None


def _wait_terminal_growth(process: PtyProcess, recording: Path, *, baseline: int, timeout: float) -> float | None:
    return _pump_until(process, recording, timeout=timeout, predicate=lambda size: size > baseline)


def _wait_session_quiescence(
    process: PtyProcess,
    state: dict[str, Any],
    *,
    timeout: float,
    stable_seconds: float | None = None,
    poll_seconds: float = 0.2,
) -> tuple[float | None, list[list[Any]]]:
    """Wait until OpenCode's own ``/session/status`` reads idle and stays idle.

    Returns when the session first read idle in the window that lasted
    ``stable_seconds`` (None on timeout) and the busy/idle transitions seen, as
    ``[seconds since the first sample, "busy" | "idle"]``. The owned PTY is still
    drained: it is a pipe the provider's TUI writes to, and nothing may leave it full.
    """

    stable_seconds = _QUIESCENCE_STABLE_SECONDS if stable_seconds is None else stable_seconds
    started = time.monotonic()
    deadline = started + timeout
    idle_since: float | None = None
    last_busy: bool | None = None
    transitions: list[list[Any]] = []
    while time.monotonic() < deadline:
        process.drain()
        if process.process.poll() is not None:
            raise RuntimeError("opencode Helm process exited before turn-boundary quiescence")
        busy = opencode_session_busy(state)
        now = time.monotonic()
        if busy != last_busy:
            transitions.append([round(now - started, 2), "busy" if busy else "idle"])
            last_busy = busy
        if busy:
            idle_since = None
        elif idle_since is None:
            idle_since = now
        if idle_since is not None and now - idle_since >= stable_seconds:
            return idle_since, transitions
        time.sleep(poll_seconds)
    return None, transitions


def _session_stays_idle(process: PtyProcess, state: dict[str, Any], *, seconds: float, poll_seconds: float = 0.2) -> bool:
    """True when every status sample over ``seconds`` reads idle."""

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        process.drain()
        if opencode_session_busy(state):
            return False
        time.sleep(poll_seconds)
    return True


def _cleanup_receipt(stop_result: dict[str, Any]) -> dict[str, Any]:
    required_cleanup = {
        "managed_opencode_process_exited": stop_result.get("clean") is True,
        "no_orphan_provider_processes": stop_result.get("dead") is True and stop_result.get("provider_process_dead") is True,
    }
    receipt = dict(stop_result)
    receipt.update(
        {
            "schema_version": 1,
            "artifact_kind": "opencode_turn_boundary_cleanup_receipt",
            "status": "pass" if all(required_cleanup.values()) else "fail",
            "orphan_count": 0 if required_cleanup["no_orphan_provider_processes"] else 1,
            "required_cleanup": required_cleanup,
        }
    )
    return receipt


def run_turn_boundary_quiescent(args: argparse.Namespace) -> dict[str, Any]:
    spec = SPECS["opencode"]
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    provider_receipt = {
        "path": str(args.provider_bin),
        "sha256": sha256_file(args.provider_bin),
        "version": subprocess.run(
            [str(args.provider_bin), "--version"], capture_output=True, text=True, timeout=30, check=False
        ).stdout.strip(),
    }
    write_json(root / "provider-binary-receipt.json", provider_receipt)

    environment = os.environ.copy()
    environment["LONGHOUSE_ENGINE_BIN"] = str(args.engine)
    environment["LONGHOUSE_ORIGIN_KIND"] = "test_or_canary"
    environment["LONGHOUSE_LAUNCH_ACTOR"] = "automation"
    environment["LONGHOUSE_LAUNCH_SURFACE"] = "test"
    configured_model = str(environment.get("LONGHOUSE_OPENCODE_QUALIFICATION_MODEL") or "").strip()
    if configured_model:
        environment["LONGHOUSE_OPENCODE_MODEL"] = (
            configured_model if configured_model.startswith("openrouter/") else f"openrouter/{configured_model}"
        )

    initial: PtyProcess | None = None
    shipper: TranscriptShipper | None = None
    initial_state: dict[str, Any] | None = None
    stop_result: dict[str, Any] = {"dead": False, "clean": False}
    try:
        home = isolated_provider_home()
        environment["HOME"] = str(home)
        model_profile = prepare_opencode_qualification_profile(home, environment)
        write_json(root / "opencode-model-profile-receipt.json", model_profile)
        shipper = start_transcript_shipper("opencode", args, home=home, environment=environment, evidence_root=root)
        write_json(root / "transcript-shipper-receipt.json", shipper.receipt)

        provider_cwd = args.repo_root
        initial = PtyProcess(
            launch_command(spec, args, None, use_credential_files=True, cwd=provider_cwd),
            cwd=provider_cwd,
            env=environment,
            recording=root / "initial.tty",
        )
        initial_state = wait_state(spec, home, process=initial)
        wait_opencode_tui_ready(initial, root / "initial.tty")
        write_json(root / "launch-state-receipt.json", redact_state_for_evidence(initial_state))
        session_id = str(initial_state["session_id"])

        prior_tail = wait_session_tail(
            args.api_url,
            args.agents_token,
            session_id,
            timeout=45,
            allow_unprojected=True,
        )
        prior_assistant_event_digests = assistant_event_digests(prior_tail)

        marker = f"LONGHOUSE_OPENCODE_TURN_BOUNDARY_{uuid.uuid4().hex}"
        prompt = f"Reply exactly {marker} and nothing else."
        pre_send_size = _terminal_size(root / "initial.tty")
        submitted_at = time.monotonic()
        if initial.process.poll() is not None:
            raise RuntimeError("opencode Helm process is no longer live before the turn-boundary prompt")
        initial.send(prompt + "\r")

        left_quiescence_at = _wait_terminal_growth(
            initial,
            root / "initial.tty",
            baseline=pre_send_size,
            timeout=args.live_send_timeout_secs,
        )
        activity_left_quiescent_during_turn = left_quiescence_at is not None
        activity_receipt: dict[str, Any] = {
            "marker": marker,
            "pre_send_terminal_bytes": pre_send_size,
            "activity_left_quiescent_during_turn": activity_left_quiescent_during_turn,
            "left_quiescence_after_seconds": ((left_quiescence_at - submitted_at) if left_quiescence_at is not None else None),
        }
        write_json(root / "turn-activity-receipt.json", activity_receipt)

        _tail, correlation = wait_assistant_response_after_marker(
            args.api_url,
            args.agents_token,
            session_id,
            marker,
            prior_assistant_event_digests=prior_assistant_event_digests,
            require_assistant_marker=True,
            timeout=int(args.live_send_timeout_secs),
        )
        turn_completion_correlated_in_served_transcript = bool(
            correlation.get("timed_out") is False and correlation.get("marker_observed_in_assistant") is True
        )
        write_json(root / "turn-correlation-receipt.json", correlation)

        settled_at, status_transitions = _wait_session_quiescence(
            initial,
            initial_state,
            timeout=args.live_send_timeout_secs,
        )
        activity_returned_to_quiescent_after_turn = settled_at is not None
        settle_duration_seconds = (settled_at - submitted_at) if settled_at is not None else None

        # The terminal is evidence beside the verdict, not part of it: whether its bytes
        # kept changing after the provider reported idle is what a spinner flake looks like.
        terminal_size_at_idle = _terminal_size(root / "initial.tty")
        activity_remained_quiescent_post_turn = _session_stays_idle(initial, initial_state, seconds=_QUIESCENCE_STABLE_SECONDS)
        activity_receipt.update(
            {
                "quiescence_authority": "opencode_session_status",
                "session_status_transitions": status_transitions,
                "stable_window_seconds": _QUIESCENCE_STABLE_SECONDS,
                "terminal_bytes_after_idle": _terminal_size(root / "initial.tty") - terminal_size_at_idle,
            }
        )
        write_json(root / "turn-activity-receipt.json", activity_receipt)

        stop_result = stop_session(spec, args, initial_state, initial, force=False, environment=environment, stop_phase="initial")
        managed_opencode_process_exited = bool(stop_result.get("clean") is True)
        no_orphan_provider_processes = bool(stop_result.get("dead") is True and stop_result.get("provider_process_dead") is True)
        cleanup_receipt = _cleanup_receipt(stop_result)
        write_json(root / "cleanup-receipt.json", cleanup_receipt)

        if shipper is not None:
            write_json(root / "transcript-shipper-receipt.json", shipper.stop())

        # The server's log lives in the sandbox home and goes with it: keep its tail.
        serve_log = retain_opencode_serve_log(
            root / "opencode-serve.log", initial_state, qualification_secrets(os.environ, args.agents_token)
        )
        redacted_secret_files = _redact_retained_secrets(
            root,
            list(qualification_secrets(os.environ, args.agents_token)),
        )

        observation = {
            "provider": "opencode",
            "session_id": session_id,
            "provider_pid": provider_process_pid(spec, initial_state),
            "marker": marker,
            "activity_left_quiescent_during_turn": activity_left_quiescent_during_turn,
            "turn_completion_correlated_in_served_transcript": turn_completion_correlated_in_served_transcript,
            "activity_returned_to_quiescent_after_turn": activity_returned_to_quiescent_after_turn,
            "activity_remained_quiescent_post_turn": activity_remained_quiescent_post_turn,
            "settle_duration_seconds": settle_duration_seconds,
            "managed_opencode_process_exited": managed_opencode_process_exited,
            "no_orphan_provider_processes": no_orphan_provider_processes,
            "artifact_secret_scan_passed": not redacted_secret_files,
            "serve_log": serve_log,
        }
        assertions = turn_boundary_quiescent_assertions(observation)

        result: dict[str, Any] = {
            "schema_version": 1,
            "artifact_kind": "direct_turn_boundary_quiescent_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "opencode",
            # No authored variant exists for this cell (see REGISTRATION
            # comment); this must equal the compiled command's "variant"
            # field, not the synthetic --variant execution key this process
            # was actually invoked with (args.variant, recorded below for
            # traceability only).
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": now(),
            "status": "pass" if assertions[_ASSERTION_ID] else "fail",
            "observation": observation,
            "assertions": assertions,
            "session_id": session_id,
            "invoked_with_execution_variant": args.variant,
            "provider_binary": provider_receipt,
            "artifact_manifest": [],
        }
        result["artifact_manifest"] = artifact_manifest(root)
        write_json(root / "result.json", result)
        return result
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        if shipper is not None:
            write_json(root / "transcript-shipper-receipt.json", shipper.stop())
        if initial is not None and initial_state is not None and not stop_result.get("dead"):
            try:
                stop_result = stop_session(spec, args, initial_state, initial, force=True, environment=environment, stop_phase="initial")
                write_json(root / "cleanup-receipt.json", _cleanup_receipt(stop_result))
            except Exception:  # noqa: BLE001 - best-effort teardown during failure handling
                pass
        serve_log = (
            retain_opencode_serve_log(
                root / "opencode-serve.log", initial_state, qualification_secrets(os.environ, getattr(args, "agents_token", ""))
            )
            if initial_state is not None
            else None
        )
        redacted_secret_files = _redact_retained_secrets(
            root,
            list(qualification_secrets(os.environ, getattr(args, "agents_token", ""))),
        )
        failure = {
            "schema_version": 1,
            "artifact_kind": "direct_turn_boundary_quiescent_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "opencode",
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": now(),
            "status": "fail",
            "failure_code": "direct_turn_boundary_quiescent_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "serve_log": serve_log,
            "redacted_secret_files": redacted_secret_files,
            "artifact_manifest": artifact_manifest(root),
        }
        write_json(root / "result.json", failure)
        return failure
    finally:
        if initial is not None:
            initial.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path)
    # No authored variant exists for this assertion cell; the sandbox still
    # passes the synthetic execution-variant key
    # ("cell:opencode:activity_returns_to_quiescent_at_turn_boundary:
    # opencode_turn_boundary_quiescent") as --variant. Accept it as an
    # opaque string rather than a restricted choice.
    parser.add_argument("--variant", required=True)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--engine", required=True, type=Path)
    parser.add_argument("--longhouse-cli", required=True, type=Path)
    parser.add_argument("--provider-bin", required=True, type=Path)
    parser.add_argument("--live-send-timeout-secs", type=float, default=180.0)
    parser.add_argument("--registration", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    args.api_url = os.environ.get(RUNTIME_API_URL_ENV, "")
    require_disposable_runtime(args.api_url)
    args.agents_token = os.environ.get(RUNTIME_AGENTS_TOKEN_ENV, "")
    if not args.api_url or not args.agents_token:
        print(json.dumps({"status": "fail", "failure_code": "runtime_host_control_credentials_missing"}))
        return 2
    for label, path in (
        ("longhouse_engine", args.engine),
        ("longhouse_cli", args.longhouse_cli),
        ("opencode_binary", args.provider_bin),
    ):
        if not path.is_file() or not os.access(path, os.X_OK):
            print(json.dumps({"status": "fail", "failure_code": f"{label}_missing"}))
            return 2
    result = run_turn_boundary_quiescent(args)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
