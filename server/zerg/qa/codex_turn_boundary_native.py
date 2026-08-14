#!/usr/bin/env python3
"""Direct stock-Codex Helm turn-boundary quiescence producer.

Proves ``session.activity.turn_boundary`` for Codex: a managed Codex Helm
bridge's raw turn-lifecycle signal (``active_turn_id`` / ``last_turn_status``
on the bridge state file) starts quiescent, becomes active for a real live
turn, and returns to quiescent exactly at that turn's boundary.

This reuses the same real, already-tested bridge lifecycle helpers as
``codex_native_resume.py`` (``_start_bridge``, ``_stop_bridge``,
``_terminal_turn_state``) rather than re-deriving turn-boundary semantics from
scratch. ``_terminal_turn_state`` -- the exact existing predicate in the
declared ``oracle_source`` this producer is built on -- already backs the
identical "did this Codex turn return to a terminal/quiescent state" judgment
used by ``codex_helm_interrupt.py`` and ``run_managed_live_send`` for the
*same* raw fields (``active_turn_id``, ``last_turn_status``). Live catalog
projection maps this producer's raw ``idle``/``needs_user`` signal onto the
served ``activity.state == "quiescent"`` enum value (see
``schemas/session_state_contract.yml`` and
``session_state_facts_projector.py``'s ``_ACTIVITY_STATE`` table); this
producer proves the raw provider-adapter signal that projection consumes, not
the served HTTP surface itself, since ``/api/agents/sessions/{id}`` does not
expose ``activity`` (see ``services/session_views.py::MachineSessionResponse``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from zerg.qa import codex_provider_release_canary as bridge_canary
from zerg.qa.provider_native_resume import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.provider_native_resume import RUNTIME_API_URL_ENV
from zerg.qa.provider_native_resume import _qualification_secrets
from zerg.qa.provider_native_resume import _redact_state_for_evidence
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key

_SCENARIO_ID = "codex_turn_boundary_quiescent"
_ASSERTION_ID = "activity_returns_to_quiescent_at_turn_boundary"
# The compiler assigns this exact string as ``--variant`` for any assertion
# cell whose *authored* schema variant is null (see
# resume_assurance.execution_variant_key / _producer_supports_cell). None of
# schemas/managed_providers.yml's session.activity.turn_boundary entries carry
# a `variant:` key, so this is the only value execute_retained_plan will ever
# pass.
_EXECUTION_VARIANT = execution_variant_key(
    provider="codex",
    assertion_id=_ASSERTION_ID,
    scenario_id=_SCENARIO_ID,
    variant=None,
)
_ACTIVE_TURN_STATUSES = frozenset({"in_progress", "inprogress", "running"})

REGISTRATION = ProducerRegistration(
    producer_id="codex.turn_boundary_quiescent.v1",
    producer_revision=1,
    scenario_id=_SCENARIO_ID,
    scenario_revision=1,
    assertion_cells=((_ASSERTION_ID, None),),
    providers=("codex",),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "quiescent_before_turn",
        "activity_became_active_during_turn",
        "turn_completed",
        "quiescent_after_turn_boundary",
        "marker_in_assistant_transcript",
    ),
    acquisition_methods=("staged_release",),
    credential_binding_ids=("codex_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "pre_turn_bridge_state",
        "active_turn_bridge_state",
        "post_turn_bridge_state",
        "cleanup_receipt",
    ),
    required_cleanup=(
        "final_bridge_stopped",
        "final_socket_absent",
        "no_orphan_provider_processes",
    ),
    implementation="server/zerg/qa/codex_turn_boundary_native.py",
    oracle_source="server/zerg/qa/codex_provider_release_canary.py",
    # `_terminal_turn_state` is the real, existing predicate in oracle_source
    # this producer's verdict is built around; the surrounding before/during
    # boundary composition lives in this module (see module docstring).
    oracle_entrypoint="_terminal_turn_state",
    executable_module="zerg.qa.codex_turn_boundary_native",
)


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


def _is_active_turn(state: dict[str, Any]) -> bool:
    if state.get("active_turn_id"):
        return True
    return str(state.get("last_turn_status") or "").strip().lower() in _ACTIVE_TURN_STATUSES


def _is_quiescent(state: dict[str, Any]) -> bool:
    return not _is_active_turn(state)


def _send_marker_async(
    args: argparse.Namespace,
    isolation_root: Path,
    session_id: str,
    marker: str,
    result_box: dict[str, Any],
) -> threading.Thread:
    def _worker() -> None:
        try:
            completed = bridge_canary._run(
                [
                    str(args.engine),
                    "codex-bridge",
                    "send",
                    "--session-id",
                    session_id,
                    "--text",
                    f"Reply exactly {marker} and nothing else.",
                    "--state-root",
                    str(bridge_canary._bridge_state_root(isolation_root)),
                    "--json",
                ],
                cwd=args.repo_root,
                timeout=args.live_send_timeout_secs + 20,
            )
            result_box["completed"] = completed
        except Exception as exc:  # noqa: BLE001 - retained in result_box for the poller
            result_box["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=_worker, name="codex-turn-boundary-send", daemon=True)
    thread.start()
    return thread


def run_turn_boundary_quiescent(args: argparse.Namespace) -> dict[str, Any]:
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    provider_receipt = {
        "path": str(args.codex_bin),
        "sha256": _sha256(args.codex_bin),
        "version": bridge_canary._run([str(args.codex_bin), "--version"], timeout=30).stdout.strip(),
    }
    _write_json(root / "provider-binary-receipt.json", provider_receipt)

    isolation_root: Path | None = None
    session_id = ""
    final_cleanup: dict[str, Any] = {"verified": False}
    try:
        # codex-bridge places a UUID-named Unix socket below this root. Keep
        # the prefix short enough that the complete path stays below Linux's
        # SUN_LEN limit inside the qualification sandbox.
        isolation_root = Path(tempfile.mkdtemp(prefix="lct-", dir="/tmp"))
        summary, _start_result, isolation_root = bridge_canary._start_bridge(
            args,
            evidence_root=root,
            codex_bin=str(args.codex_bin),
            launch_mode="detached_ui",
            isolation_root=isolation_root,
        )
        session_id = str(summary.get("session_id") or "")
        state_file = Path(str(summary.get("state_file") or ""))
        thread_id = str(summary.get("thread_id") or "")
        if not session_id or not thread_id or not state_file.is_file():
            raise RuntimeError("turn-boundary bridge did not start a resumable provider thread")

        pre_turn_state = bridge_canary._read_json(state_file)
        _write_json(root / "pre-turn-bridge-state.json", _redact_state_for_evidence(pre_turn_state))
        quiescent_before_turn = _is_quiescent(pre_turn_state)

        marker = f"LONGHOUSE_CODEX_TURN_BOUNDARY_{uuid.uuid4().hex}"
        send_result_box: dict[str, Any] = {}
        send_thread = _send_marker_async(args, isolation_root, session_id, marker, send_result_box)

        activity_became_active_during_turn = False
        active_turn_state: dict[str, Any] = {}
        deadline = time.monotonic() + args.live_send_timeout_secs
        while time.monotonic() < deadline:
            try:
                candidate = bridge_canary._read_json(state_file)
            except (OSError, json.JSONDecodeError):
                candidate = {}
            if _is_active_turn(candidate):
                activity_became_active_during_turn = True
                active_turn_state = candidate
                break
            if not send_thread.is_alive() and "completed" in send_result_box:
                # The send command already returned; a fast turn may have
                # started and finished between polls. One more read below
                # (post_turn_state) still proves quiescence-after; treat this
                # as an inconclusive but non-fatal miss of the active window.
                break
            time.sleep(0.05)
        if not active_turn_state:
            try:
                active_turn_state = bridge_canary._read_json(state_file)
            except (OSError, json.JSONDecodeError):
                active_turn_state = {}
        _write_json(root / "active-turn-bridge-state.json", _redact_state_for_evidence(active_turn_state))

        send_thread.join(timeout=args.live_send_timeout_secs + 25)
        if "error" in send_result_box:
            raise RuntimeError(f"codex-bridge send failed: {send_result_box['error']}")
        completed = send_result_box.get("completed")
        if completed is None or completed.returncode:
            detail = completed.stderr[-1000:] if completed is not None else "send did not complete"
            raise RuntimeError(f"codex-bridge send failed: {detail}")

        post_turn_deadline = time.monotonic() + args.live_send_timeout_secs
        post_turn_state = bridge_canary._read_json(state_file)
        while not bridge_canary._terminal_turn_state(post_turn_state) and time.monotonic() < post_turn_deadline:
            time.sleep(0.1)
            post_turn_state = bridge_canary._read_json(state_file)
        _write_json(root / "post-turn-bridge-state.json", _redact_state_for_evidence(post_turn_state))

        turn_completed = post_turn_state.get("last_turn_status") == "completed"
        quiescent_after_turn_boundary = bridge_canary._terminal_turn_state(post_turn_state) and _is_quiescent(post_turn_state)
        thread_path = Path(str(post_turn_state.get("thread_path") or ""))
        marker_in_assistant_transcript = thread_path.is_file() and bridge_canary._assistant_transcript_contains(thread_path, marker)

        final_cleanup = bridge_canary._stop_bridge(args, session_id, isolation_root)
        verification = final_cleanup.get("verification") or {}
        _write_json(root / "cleanup-receipt.json", final_cleanup)

        observation = {
            "quiescent_before_turn": quiescent_before_turn,
            "activity_became_active_during_turn": activity_became_active_during_turn,
            "turn_completed": turn_completed,
            "quiescent_after_turn_boundary": quiescent_after_turn_boundary,
            "marker_in_assistant_transcript": marker_in_assistant_transcript,
            "final_bridge_stopped": bool(verification.get("verified")),
            "final_socket_absent": bool(verification.get("socket_absent")),
            "orphan_count": 0 if verification.get("verified") else 1,
        }
        proven = (
            observation["quiescent_before_turn"]
            and observation["activity_became_active_during_turn"]
            and observation["turn_completed"]
            and observation["quiescent_after_turn_boundary"]
            and observation["marker_in_assistant_transcript"]
        )
        observation["no_orphan_provider_processes"] = observation["orphan_count"] == 0
        assertions = {_ASSERTION_ID: bool(proven)}
        result: dict[str, Any] = {
            "schema_version": 1,
            "artifact_kind": "codex_turn_boundary_quiescent_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "codex",
            "variant": None,
            "scenario_id": _SCENARIO_ID,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": _now(),
            "status": "pass" if assertions[_ASSERTION_ID] else "fail",
            "observation": observation,
            "assertions": assertions,
            "session_id": session_id,
            "provider_thread_id": thread_id,
            "provider_binary": provider_receipt,
            "marker": marker,
            "artifact_manifest": _artifact_manifest(root),
        }
        _write_json(root / "result.json", result)
        return result
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        failure = {
            "schema_version": 1,
            "artifact_kind": "codex_turn_boundary_quiescent_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "codex",
            "variant": None,
            "scenario_id": _SCENARIO_ID,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": _now(),
            "status": "fail",
            "failure_code": "codex_turn_boundary_quiescent_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "assertions": {_ASSERTION_ID: False},
            "artifact_manifest": _artifact_manifest(root),
        }
        _write_json(root / "result.json", failure)
        return failure
    finally:
        if session_id and isolation_root and not (final_cleanup.get("verification") or {}).get("verified"):
            cleanup = bridge_canary._stop_bridge(args, session_id, isolation_root)
            if not (root / "cleanup-receipt.json").exists():
                _write_json(root / "cleanup-receipt.json", cleanup)
        _qualification_secrets(os.environ, getattr(args, "agents_token", "") or "")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", required=True, choices=(_EXECUTION_VARIANT,))
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--engine", required=True, type=Path)
    parser.add_argument("--codex-bin", required=True, type=Path)
    parser.add_argument("--model", default=os.environ.get("CODEX_MODEL"))
    parser.add_argument("--bridge-start-timeout-secs", type=int, default=30)
    parser.add_argument("--live-send-timeout-secs", type=int, default=120)
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
    args.script_bin = None
    args.timeout_bin = None
    if not args.api_url or not args.agents_token:
        print(json.dumps({"status": "fail", "failure_code": "runtime_host_control_credentials_missing"}))
        return 2
    if not args.engine.is_file() or not os.access(args.engine, os.X_OK):
        print(json.dumps({"status": "fail", "failure_code": "longhouse_engine_missing"}))
        return 2
    if not args.codex_bin.is_file() or not os.access(args.codex_bin, os.X_OK):
        print(json.dumps({"status": "fail", "failure_code": "codex_binary_missing"}))
        return 2
    result = run_turn_boundary_quiescent(args)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
