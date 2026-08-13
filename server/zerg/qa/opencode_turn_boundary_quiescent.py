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
``_isolated_provider_home``, ``_launch_command``, ``_wait_state``,
``_wait_opencode_tui_ready``, ``_wait_assistant_response_after_marker``,
``_stop``, ...) rather than re-implemented, because that module is the same,
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

IMPORTANT — "quiescent" here means observed terminal/served-turn quiescence,
not the internal ``ActivityState`` literal: the served facts type
(``server/zerg/services/session_state_contract.py``:
``ActivityState = Literal["thinking", "executing", "quiescent", ...]``) is
the internal vocabulary this assertion's name borrows, but that field is not
exposed on the ``/api/agents/*`` machine surface this producer runs under
(``MachineSessionResponse`` — see ``session_views.py`` — deliberately narrows
out control/activity state; the browser-shaped ``SessionResponse`` that does
carry ``session_state.activity`` requires browser cookie auth, not the
``X-Agents-Token`` this sandbox provides). This producer instead proves
quiescence operationally: the owned PTY genuinely stops rendering new bytes
around a turn whose completion is independently correlated against the
Runtime Host's served transcript. A human should confirm whether that
operational proxy is what the capability is meant to certify, or whether a
new machine-surface field is the intended (bigger) fix.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from zerg.qa.provider_native_resume import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.provider_native_resume import RUNTIME_API_URL_ENV
from zerg.qa.provider_native_resume import SPECS
from zerg.qa.provider_native_resume import PtyProcess
from zerg.qa.provider_native_resume import TranscriptShipper
from zerg.qa.provider_native_resume import _assistant_event_digests
from zerg.qa.provider_native_resume import _isolated_provider_home
from zerg.qa.provider_native_resume import _launch_command
from zerg.qa.provider_native_resume import _provider_process_pid
from zerg.qa.provider_native_resume import _qualification_secrets
from zerg.qa.provider_native_resume import _redact_state_for_evidence
from zerg.qa.provider_native_resume import _start_transcript_shipper
from zerg.qa.provider_native_resume import _stop
from zerg.qa.provider_native_resume import _wait_assistant_response_after_marker
from zerg.qa.provider_native_resume import _wait_opencode_tui_ready
from zerg.qa.provider_native_resume import _wait_session_tail
from zerg.qa.provider_native_resume import _wait_state
from zerg.qa.resume_assurance import ProducerRegistration

_ASSERTION_ID = "activity_returns_to_quiescent_at_turn_boundary"

REGISTRATION = ProducerRegistration(
    producer_id="opencode.turn_boundary_quiescent.v1",
    producer_revision=1,
    scenario_id="opencode_turn_boundary_quiescent",
    scenario_revision=1,
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


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _artifact_manifest(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "result.json"
    ]


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


def _wait_terminal_quiescence(
    process: PtyProcess,
    recording: Path,
    *,
    timeout: float,
    stable_seconds: float = 2.0,
) -> float | None:
    deadline = time.monotonic() + timeout
    last_size = -1
    unchanged_since = time.monotonic()
    while time.monotonic() < deadline:
        process.drain()
        if process.process.poll() is not None:
            raise RuntimeError("opencode Helm process exited before turn-boundary quiescence")
        size = _terminal_size(recording)
        now = time.monotonic()
        if size != last_size:
            last_size = size
            unchanged_since = now
        elif now - unchanged_since >= stable_seconds:
            return now
        time.sleep(0.1)
    return None


def run_turn_boundary_quiescent(args: argparse.Namespace) -> dict[str, Any]:
    spec = SPECS["opencode"]
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    provider_receipt = {
        "path": str(args.provider_bin),
        "sha256": _sha256(args.provider_bin),
        "version": subprocess.run(
            [str(args.provider_bin), "--version"], capture_output=True, text=True, timeout=30, check=False
        ).stdout.strip(),
    }
    _write_json(root / "provider-binary-receipt.json", provider_receipt)

    environment = os.environ.copy()
    environment["LONGHOUSE_ENGINE_BIN"] = str(args.engine)
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
        home = _isolated_provider_home()
        environment["HOME"] = str(home)
        shipper = _start_transcript_shipper("opencode", args, home=home, environment=environment, evidence_root=root)
        _write_json(root / "transcript-shipper-receipt.json", shipper.receipt)

        provider_cwd = args.repo_root
        initial = PtyProcess(
            _launch_command(spec, args, None, use_credential_files=True, cwd=provider_cwd),
            cwd=provider_cwd,
            env=environment,
            recording=root / "initial.tty",
        )
        initial_state = _wait_state(spec, home, process=initial)
        _wait_opencode_tui_ready(initial, root / "initial.tty")
        _write_json(root / "launch-state-receipt.json", _redact_state_for_evidence(initial_state))
        session_id = str(initial_state["session_id"])

        prior_tail = _wait_session_tail(
            args.api_url,
            args.agents_token,
            session_id,
            timeout=45,
            allow_unprojected=True,
        )
        prior_assistant_event_digests = _assistant_event_digests(prior_tail)

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
        _write_json(
            root / "turn-activity-receipt.json",
            {
                "marker": marker,
                "pre_send_terminal_bytes": pre_send_size,
                "activity_left_quiescent_during_turn": activity_left_quiescent_during_turn,
                "left_quiescence_after_seconds": ((left_quiescence_at - submitted_at) if left_quiescence_at is not None else None),
            },
        )

        _tail, correlation = _wait_assistant_response_after_marker(
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
        _write_json(root / "turn-correlation-receipt.json", correlation)

        settled_at = _wait_terminal_quiescence(
            initial,
            root / "initial.tty",
            timeout=args.live_send_timeout_secs,
            stable_seconds=2.0,
        )
        activity_returned_to_quiescent_after_turn = settled_at is not None
        settle_duration_seconds = (settled_at - submitted_at) if settled_at is not None else None

        remained_quiescent_size = _terminal_size(root / "initial.tty")
        time.sleep(2.0)
        initial.drain()
        activity_remained_quiescent_post_turn = _terminal_size(root / "initial.tty") == remained_quiescent_size

        stop_result = _stop(spec, args, initial_state, initial, force=False, environment=environment, stop_phase="initial")
        managed_opencode_process_exited = bool(stop_result.get("clean") is True)
        no_orphan_provider_processes = bool(stop_result.get("dead") is True and stop_result.get("provider_process_dead") is True)
        _write_json(root / "cleanup-receipt.json", stop_result)

        if shipper is not None:
            _write_json(root / "transcript-shipper-receipt.json", shipper.stop())

        redacted_secret_files = _redact_retained_secrets(
            root,
            list(_qualification_secrets(os.environ, args.agents_token)),
        )

        observation = {
            "provider": "opencode",
            "session_id": session_id,
            "provider_pid": _provider_process_pid(spec, initial_state),
            "marker": marker,
            "activity_left_quiescent_during_turn": activity_left_quiescent_during_turn,
            "turn_completion_correlated_in_served_transcript": turn_completion_correlated_in_served_transcript,
            "activity_returned_to_quiescent_after_turn": activity_returned_to_quiescent_after_turn,
            "activity_remained_quiescent_post_turn": activity_remained_quiescent_post_turn,
            "settle_duration_seconds": settle_duration_seconds,
            "managed_opencode_process_exited": managed_opencode_process_exited,
            "no_orphan_provider_processes": no_orphan_provider_processes,
            "artifact_secret_scan_passed": not redacted_secret_files,
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
            "generated_at": _now(),
            "status": "pass" if assertions[_ASSERTION_ID] else "fail",
            "observation": observation,
            "assertions": assertions,
            "session_id": session_id,
            "invoked_with_execution_variant": args.variant,
            "provider_binary": provider_receipt,
            "artifact_manifest": [],
        }
        result["artifact_manifest"] = _artifact_manifest(root)
        _write_json(root / "result.json", result)
        return result
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        if shipper is not None:
            _write_json(root / "transcript-shipper-receipt.json", shipper.stop())
        if initial is not None and initial_state is not None and not stop_result.get("dead"):
            try:
                stop_result = _stop(spec, args, initial_state, initial, force=True, environment=environment, stop_phase="initial")
                _write_json(root / "cleanup-receipt.json", stop_result)
            except Exception:  # noqa: BLE001 - best-effort teardown during failure handling
                pass
        redacted_secret_files = _redact_retained_secrets(
            root,
            list(_qualification_secrets(os.environ, getattr(args, "agents_token", ""))),
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
            "generated_at": _now(),
            "status": "fail",
            "failure_code": "direct_turn_boundary_quiescent_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "redacted_secret_files": redacted_secret_files,
            "artifact_manifest": _artifact_manifest(root),
        }
        _write_json(root / "result.json", failure)
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
