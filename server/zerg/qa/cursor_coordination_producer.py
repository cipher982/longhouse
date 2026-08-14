#!/usr/bin/env python3
"""Direct stock-Cursor coordination producer.

Continuously re-verifies three of Cursor's four coordination capabilities
(``coordination.awareness.create``, ``coordination.directed_input.send``,
``coordination.directed_input.receive``) declared in
``schemas/managed_providers.yml``. Cursor has no
``coordination.awareness.post_compaction`` entry there -- confirmed directly
against the schema, not assumed -- so this producer registers exactly three
assertion cells across two scenario ids, using the ``scenario_ids`` field
``resume_assurance.ProducerRegistration`` documents for exactly this case:
"a producer [that] can advertise a bounded set of scenario ids while
retaining one immutable registration and one executable entrypoint" (see
``zerg.qa.provider_generic_resume`` for the only other user of that field).

No producer for ANY provider's coordination capabilities existed before this
file -- there is no prior art to pattern-match beyond the pure postcondition
functions in ``provider_coordination_oracles.py`` and the coordination
product surface itself (the same MCP tools exposed to this session via
``longhouse-coordination`` -- see ``engine/src/claude_channel_server.rs``'s
``coordination_tools()``/``instructions`` and
``engine/src/cursor_helm_launcher.rs``'s ``write_cursor_mcp_config``, and the
HTTP surface in ``zerg.routers.agents_sessions``:
``POST /sessions/{id}/coordination-token``, ``POST /directed-inputs``,
``GET /directed-inputs``).

Design notes / open uncertainty (flagged for human review before this is
registered and run for real):

* ``coordination.awareness.create`` -> ``coordination_instructions_model_visible``
  has no established live proof anywhere in this codebase. The coordination
  MCP server's ``instructions`` field (delivered at MCP `initialize`, not a
  per-tool description) is what "model visible" means here. There is no way
  to intercept that handshake without changing cursor-agent, so this producer
  probes behaviorally whether the live model classifies cross-session input
  as attributed, untrusted peer input. This is a recitation probe, not a
  protocol-level capture, and is the single highest-uncertainty design choice
  in this module.
* ``coordination.directed_input.send``/``.receive`` accept ``hermetic``
  evidence per the schema, but this producer always proves them with
  ``live_token`` (two real, simultaneously-live Cursor Helm sessions) rather
  than inventing a hermetic shortcut -- live_token is unconditionally
  admissible for both, and a real end-to-end proof is the safer default given
  zero prior art for what a correct hermetic path would look like here.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from zerg.qa.cursor_helm_product_e2e import _activity_state
from zerg.qa.cursor_helm_product_e2e import _assistant_texts
from zerg.qa.cursor_helm_product_e2e import _canonical_state_from_diagnostics
from zerg.qa.cursor_helm_product_e2e import _wait_until
from zerg.qa.provider_coordination_oracles import awareness_create_assertions
from zerg.qa.provider_coordination_oracles import directed_input_assertions
from zerg.qa.provider_native_resume import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.provider_native_resume import RUNTIME_API_URL_ENV
from zerg.qa.provider_native_resume import SPECS
from zerg.qa.provider_native_resume import PtyProcess
from zerg.qa.provider_native_resume import TranscriptShipper
from zerg.qa.provider_native_resume import _artifact_manifest
from zerg.qa.provider_native_resume import _cleanup_processes
from zerg.qa.provider_native_resume import _initialize_cursor_workspace
from zerg.qa.provider_native_resume import _launch_command
from zerg.qa.provider_native_resume import _qualification_secrets
from zerg.qa.provider_native_resume import _secret_scan
from zerg.qa.provider_native_resume import _sha256
from zerg.qa.provider_native_resume import _start_transcript_shipper
from zerg.qa.provider_native_resume import _wait_cursor_tui_ready
from zerg.qa.provider_native_resume import _wait_state
from zerg.qa.provider_native_resume import _write_json
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key

_SPEC = SPECS["cursor"]

_AWARENESS_CREATE_SCENARIO = "cursor_coordination_awareness_create"
_DIRECTED_INPUT_SCENARIO = "cursor_coordination_directed_input"
_AWARENESS_CREATE_ASSERTION = "coordination_instructions_model_visible"
_SEND_ASSERTION = "provider_input_receipt_linked"
_RECEIVE_ASSERTION = "attributed_input_visible"

REGISTRATION = ProducerRegistration(
    producer_id="cursor.coordination.v1",
    producer_revision=1,
    scenario_id=_AWARENESS_CREATE_SCENARIO,
    scenario_revision=1,
    scenario_ids=(_AWARENESS_CREATE_SCENARIO, _DIRECTED_INPUT_SCENARIO),
    # No `variant:` is authored for any of these three assertions in
    # schemas/managed_providers.yml, so every cell's variant is None -- not
    # an empty string -- matching provider_capability_schema.py's parsing of
    # an absent `variant:` field and provider_generic_resume.py's own
    # variant-less cells.
    assertion_cells=(
        (_AWARENESS_CREATE_ASSERTION, None),
        (_SEND_ASSERTION, None),
        (_RECEIVE_ASSERTION, None),
    ),
    providers=("cursor",),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "coordination_mcp_server_registered",
        "model_answered_coordination_probe",
        "directed_input_created",
        "directed_input_delivered_to_live_target",
        "directed_input_attribution_present",
    ),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=("cursor_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "session_launch_receipts",
        "coordination_observation",
        "cleanup_receipt",
    ),
    required_cleanup=("no_orphan_provider_processes",),
    implementation="server/zerg/qa/cursor_coordination_producer.py",
    oracle_source="server/zerg/qa/provider_coordination_oracles.py",
    # provider_coordination_oracles.py has no single dispatcher (unlike
    # provider_resume_oracles.py's assertions_for). Two of this producer's
    # three assertion cells come from directed_input_assertions; the third
    # (coordination_instructions_model_visible) comes from
    # awareness_create_assertions -- both are actually called, see
    # run_coordination below.
    oracle_entrypoint="directed_input_assertions",
    executable_module="zerg.qa.cursor_coordination_producer",
)

_AWARENESS_CREATE_VARIANT_KEY = execution_variant_key(
    provider="cursor", assertion_id=_AWARENESS_CREATE_ASSERTION, scenario_id=_AWARENESS_CREATE_SCENARIO, variant=None
)
_SEND_VARIANT_KEY = execution_variant_key(
    provider="cursor", assertion_id=_SEND_ASSERTION, scenario_id=_DIRECTED_INPUT_SCENARIO, variant=None
)
_RECEIVE_VARIANT_KEY = execution_variant_key(
    provider="cursor", assertion_id=_RECEIVE_ASSERTION, scenario_id=_DIRECTED_INPUT_SCENARIO, variant=None
)
# execute_retained_plan never passes an explicit --scenario-id / --assertion-id
# to a non-generic producer (only "is_generic" producers get that). The
# opaque execution_variant key it passes via --variant is the only signal
# available to tell these three cells apart, so precompute the exact keys
# the real compiler would derive (via the same public execution_variant_key
# helper it uses) and dispatch on those rather than parsing the "cell:"
# string format by hand.
_DISPATCH: dict[str, tuple[str, str, str]] = {
    _AWARENESS_CREATE_VARIANT_KEY: ("awareness_create", _AWARENESS_CREATE_SCENARIO, _AWARENESS_CREATE_ASSERTION),
    _SEND_VARIANT_KEY: ("directed_input", _DIRECTED_INPUT_SCENARIO, _SEND_ASSERTION),
    _RECEIVE_VARIANT_KEY: ("directed_input", _DIRECTED_INPUT_SCENARIO, _RECEIVE_ASSERTION),
}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class _CursorSession:
    session_id: str
    provider_thread_id: str
    home: Path
    environment: dict[str, str]
    process: PtyProcess
    shipper: TranscriptShipper
    provider_cwd: Path
    state: dict[str, Any]


def _launch_cursor_session(args: argparse.Namespace, root: Path, isolation_root: Path, *, label: str, prompt: str) -> _CursorSession:
    home = isolation_root / f"home-{label}"
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
    environment["CURSOR_HOME"] = str(home / ".cursor")
    environment["LONGHOUSE_ENGINE_BIN"] = str(args.engine)
    # The Machine Agent snapshots advertised control capabilities when its
    # WebSocket starts. Bind the exact staged Cursor binary before starting
    # the shipper so cursor.send is present for directed-input delivery.
    environment["LONGHOUSE_CURSOR_BIN"] = str(args.provider_bin)

    evidence_root = root / f"shipper-{label}"
    evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    shipper = _start_transcript_shipper("cursor", args, home=home, environment=environment, evidence_root=evidence_root)
    _write_json(root / f"transcript-shipper-receipt-{label}.json", shipper.receipt)

    provider_cwd = root / f"cursor-workspace-{label}"
    provider_cwd.mkdir(mode=0o700, parents=True, exist_ok=True)
    _initialize_cursor_workspace(provider_cwd)

    hooks = subprocess.run(
        [str(args.engine), "cursor-helm", "configure-hooks", "--cursor-dir", str(provider_cwd / ".cursor")],
        cwd=args.repo_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if hooks.returncode != 0:
        raise RuntimeError(f"Cursor native hook configuration failed for {label}: {hooks.stderr[-1000:]}")

    process = PtyProcess(
        _launch_command(_SPEC, args, None, use_credential_files=True, cwd=provider_cwd, prompt=prompt),
        cwd=provider_cwd,
        env=environment,
        recording=root / f"{label}.tty",
    )
    state = _wait_state(_SPEC, home, process=process)
    _wait_cursor_tui_ready(process, root / f"{label}.tty")
    return _CursorSession(
        session_id=str(state["session_id"]),
        provider_thread_id=str(state["provider_session_id"]),
        home=home,
        environment=environment,
        process=process,
        shipper=shipper,
        provider_cwd=provider_cwd,
        state=state,
    )


def _teardown_cursor_session(session: _CursorSession | None) -> dict[str, Any]:
    if session is None:
        return {"skipped": True}
    cleanup = _cleanup_processes(_SPEC, (session.process,), [session.state])
    try:
        shipper_receipt = session.shipper.stop()
    except Exception as exc:  # noqa: BLE001 - best-effort teardown
        shipper_receipt = {"status": "stop_failed", "error": f"{type(exc).__name__}: {exc}"}
    cleanup["shipper_stop"] = shipper_receipt
    return cleanup


def _api_get(api_url: str, token: str, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    try:
        response = httpx.get(f"{api_url.rstrip('/')}{path}", headers={"X-Agents-Token": token}, params=params, timeout=10)
    except httpx.TransportError:
        return None
    if response.status_code in {404, 429, 503}:
        return None
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else None


def _hosted_events(api_url: str, token: str, session_id: str) -> dict[str, Any] | None:
    return _api_get(
        api_url,
        token,
        f"/api/agents/sessions/{session_id}/events",
        params={"context_mode": "forensic", "branch_mode": "head", "limit": 100},
    )


def _session_activity_state(api_url: str, token: str, session_id: str) -> str | None:
    payload = _api_get(api_url, token, f"/api/agents/sessions/{session_id}/state-diagnostics")
    return _activity_state(_canonical_state_from_diagnostics(payload))


def _wait_first_turn_settled(api_url: str, token: str, session_id: str, *, timeout: float) -> None:
    """Wait for the session's first turn to finish and served activity to settle.

    A directed input can only be live-delivered to a target whose control
    path reports the "send" capability, and a coordination token can only be
    minted for a session the catalog already reports as managed/connected.
    Both are true once the session's own bootstrap turn has completed and
    activity has returned to quiescent -- see cursor_helm_product_e2e.py's
    settled() for the same postcondition on the turn_boundary side.
    """

    _wait_until(
        lambda: True if _session_activity_state(api_url, token, session_id) == "quiescent" else None,
        timeout=timeout,
        description=f"Cursor session {session_id} settling to quiescent after its first turn",
    )


def _wait_marker_reply(api_url: str, token: str, session_id: str, marker: str, *, timeout: float) -> str:
    def probe() -> str | None:
        payload = _hosted_events(api_url, token, session_id)
        if payload is None:
            return None
        for text in _assistant_texts(payload):
            if marker in text:
                return text
        return None

    return _wait_until(probe, timeout=timeout, description=f"Cursor session {session_id} reply containing marker {marker}")


def _mint_coordination_token(api_url: str, device_token: str, session_id: str, *, timeout: float) -> str:
    def attempt() -> str | None:
        response = httpx.post(
            f"{api_url.rstrip('/')}/api/agents/sessions/{session_id}/coordination-token",
            headers={"X-Agents-Token": device_token},
            timeout=15,
        )
        if response.status_code == 409:
            # Not yet managed/connected -- the control-path connection can
            # lag a few seconds behind the session becoming visible at all.
            return None
        response.raise_for_status()
        payload = response.json()
        token = str(payload.get("coordination_token") or "").strip()
        return token or None

    return _wait_until(attempt, timeout=timeout, description=f"session-scoped coordination authority for {session_id}")


def _create_directed_input(
    api_url: str,
    coordination_token: str,
    source_session_id: str,
    target_session_id: str,
    text: str,
    client_request_id: str,
) -> dict[str, Any]:
    response = httpx.post(
        f"{api_url.rstrip('/')}/api/agents/directed-inputs",
        headers={"X-Agents-Token": coordination_token, "X-Longhouse-Session-Id": source_session_id},
        json={"target_session_id": target_session_id, "text": text, "client_request_id": client_request_id},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("directed-inputs create returned a non-object response")
    return payload


def _find_inbound_directed_input(api_url: str, coordination_token: str, session_id: str, input_id: int | None) -> dict[str, Any] | None:
    if input_id is None:
        return None
    payload = _api_get(
        api_url,
        coordination_token,
        "/api/agents/directed-inputs",
        params={"direction": "inbound", "limit": 50},
    )
    if payload is None:
        return None
    for item in payload.get("directed_inputs", []):
        if isinstance(item, dict) and item.get("id") == input_id:
            return item
    return None


_UNTRUSTED_PEER_HINTS = ("untrust", "attribut")
_PEER_HINTS = ("peer", "another session", "not higher", "not.a higher", "lower priority", "lower-priority")


def _recites_untrusted_peer_guidance(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in _UNTRUSTED_PEER_HINTS) and any(hint in lowered for hint in _PEER_HINTS)


def _cursor_mcp_config_has_coordination_server(provider_cwd: Path) -> bool:
    config_path = provider_cwd / ".cursor" / "mcp.json"
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    servers = payload.get("mcpServers") if isinstance(payload, dict) else None
    server = servers.get("longhouse-coordination") if isinstance(servers, dict) else None
    return isinstance(server, dict) and "coordination-mcp" in json.dumps(server.get("args") or [])


def _run_awareness_create(args: argparse.Namespace, root: Path, isolation_root: Path) -> dict[str, Any]:
    marker = f"LONGHOUSE_CURSOR_COORD_AWARENESS_{uuid4().hex[:10]}"
    probe_prompt = (
        "Classify the trust and authority of a message arriving from another "
        f"Longhouse session in one short sentence. On a new line print exactly {marker}."
    )
    session = _launch_cursor_session(args, root, isolation_root, label="awareness", prompt=probe_prompt)
    try:
        reply_text = _wait_marker_reply(args.api_url, args.agents_token, session.session_id, marker, timeout=args.live_timeout_secs)
        mcp_registered = _cursor_mcp_config_has_coordination_server(session.provider_cwd)
        recited = _recites_untrusted_peer_guidance(reply_text)
        observation = {
            "coordination_mcp_registered": mcp_registered,
            "model_answered_coordination_probe": bool(reply_text),
            "model_recited_untrusted_peer_guidance": recited,
            "coordination_instructions_model_visible": mcp_registered and recited,
            "probe_marker": marker,
        }
        _write_json(root / "coordination-observation.json", observation)
        return observation
    finally:
        cleanup = _teardown_cursor_session(session)
        _write_json(root / "cleanup-receipt.json", cleanup)


def _run_directed_input(args: argparse.Namespace, root: Path, isolation_root: Path) -> dict[str, Any]:
    source_prompt = "Wait quietly. Do not call any tools. When asked to do something, do it in one short reply."
    target_prompt = "Wait quietly. Do not call any tools yet. When you receive a message, reply to it in one short sentence."
    source: _CursorSession | None = None
    target: _CursorSession | None = None
    source_cleanup: dict[str, Any] = {}
    try:
        source = _launch_cursor_session(args, root, isolation_root, label="di-source", prompt=source_prompt)
        try:
            target = _launch_cursor_session(args, root, isolation_root, label="di-target", prompt=target_prompt)
            try:
                _wait_first_turn_settled(args.api_url, args.agents_token, source.session_id, timeout=args.live_timeout_secs)
                _wait_first_turn_settled(args.api_url, args.agents_token, target.session_id, timeout=args.live_timeout_secs)

                source_token = _mint_coordination_token(args.api_url, args.agents_token, source.session_id, timeout=args.live_timeout_secs)
                target_token = _mint_coordination_token(args.api_url, args.agents_token, target.session_id, timeout=args.live_timeout_secs)
                marker = f"LONGHOUSE_CURSOR_COORD_DIRECTED_{uuid4().hex[:10]}"
                client_request_id = uuid4().hex
                create_attempts: list[dict[str, Any]] = []

                def create_with_receipt() -> dict[str, Any] | None:
                    response = _create_directed_input(
                        args.api_url,
                        source_token,
                        source.session_id,
                        target.session_id,
                        marker,
                        client_request_id,
                    )
                    create_attempts.append(response)
                    return response if response.get("input_receipt") is not None else None

                try:
                    created = _wait_until(
                        create_with_receipt,
                        timeout=args.live_timeout_secs,
                        description="Cursor directed input receipt link",
                    )
                except RuntimeError:
                    if not create_attempts:
                        create_with_receipt()
                    created = create_attempts[-1]
                input_id = created.get("id")
                input_receipt = created.get("input_receipt")

                def _marker_delivered() -> bool | None:
                    payload = _hosted_events(args.api_url, args.agents_token, target.session_id)
                    return True if _marker_in_any_event(payload, marker) else None

                delivered = False
                try:
                    _wait_until(
                        _marker_delivered,
                        timeout=args.live_timeout_secs,
                        description=f"directed input marker {marker} visible in target session {target.session_id}",
                    )
                    delivered = True
                except RuntimeError:
                    delivered = False

                inbox_item = _find_inbound_directed_input(args.api_url, target_token, target.session_id, input_id)

                observation = {
                    "input_id": input_id,
                    "input_persisted": input_id is not None,
                    "input_receipt_linked": isinstance(input_receipt, dict) and bool(input_receipt),
                    "input_visible": delivered,
                    "source_session_id": created.get("source_session_id") or (inbox_item or {}).get("source_session_id"),
                    "inbox_item_present": inbox_item is not None,
                    "inbox_item_source_session_id": (inbox_item or {}).get("source_session_id"),
                }
                _write_json(root / "coordination-observation.json", observation)
                return observation
            finally:
                cleanup = _teardown_cursor_session(target)
                _write_json(root / "cleanup-receipt-target.json", cleanup)
        finally:
            source_cleanup = _teardown_cursor_session(source)
    finally:
        if source_cleanup:
            _write_json(root / "cleanup-receipt-source.json", source_cleanup)


def _marker_in_any_event(payload: dict[str, Any] | None, marker: str) -> bool:
    if not payload:
        return False
    return marker in json.dumps(payload.get("events", []), ensure_ascii=False)


def run_coordination(args: argparse.Namespace) -> dict[str, Any]:
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    provider_receipt = {
        "path": str(args.provider_bin),
        "sha256": _sha256(args.provider_bin),
    }
    _write_json(root / "provider-binary-receipt.json", provider_receipt)

    dispatch = _DISPATCH.get(args.variant)
    if dispatch is None:
        result = {
            "schema_version": 1,
            "artifact_kind": "direct_coordination_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "cursor",
            "variant": None,
            "scenario_id": None,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": _now(),
            "status": "fail",
            "failure_code": "unrecognized_execution_variant",
            "error": f"--variant {args.variant!r} did not match any known coordination assertion cell",
            "artifact_manifest": _artifact_manifest(root),
        }
        _write_json(root / "result.json", result)
        return result

    kind, scenario_id, assertion_id = dispatch
    isolation_root = Path(tempfile.mkdtemp(prefix="lhx-cursor-coord-", dir="/tmp"))
    try:
        if kind == "awareness_create":
            observation = _run_awareness_create(args, root, isolation_root)
            assertions = awareness_create_assertions(observation)
        else:
            observation = _run_directed_input(args, root, isolation_root)
            assertions = directed_input_assertions(observation)

        redacted_secret_files = _secret_scan(root, list(_qualification_secrets(dict(os.environ), args.agents_token)))
        status = "pass" if assertions.get(assertion_id) is True else "fail"
        result: dict[str, Any] = {
            "schema_version": 1,
            "artifact_kind": "direct_coordination_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "cursor",
            # Must equal the compiled command's raw authored `variant`, which
            # is None for every cell this producer owns (no `variant:` field
            # is authored for any of them) -- NOT the opaque execution_variant
            # scheduling key received via --variant. Echoing args.variant
            # here would fail _validate_execution_result's
            # `result.variant == command.variant` check.
            "variant": None,
            "scenario_id": scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": _now(),
            "status": status,
            "requested_assertion_id": assertion_id,
            "observation": observation,
            "assertions": assertions,
            "provider_binary": provider_receipt,
            "redacted_secret_files": redacted_secret_files,
            "artifact_manifest": _artifact_manifest(root),
        }
        _write_json(root / "result.json", result)
        return result
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        redacted_secret_files = _secret_scan(root, list(_qualification_secrets(dict(os.environ), args.agents_token)))
        failure = {
            "schema_version": 1,
            "artifact_kind": "direct_coordination_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "cursor",
            "variant": None,
            "scenario_id": scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": _now(),
            "status": "fail",
            "failure_code": "direct_coordination_failed",
            "requested_assertion_id": assertion_id,
            "error": f"{type(exc).__name__}: {exc}",
            "redacted_secret_files": redacted_secret_files,
            "artifact_manifest": _artifact_manifest(root),
        }
        _write_json(root / "result.json", failure)
        return failure
    finally:
        shutil.rmtree(isolation_root, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    # Opaque execution-variant scheduling key from
    # resume_assurance.execution_variant_key(); none of this producer's
    # cells have an authored variant, so the value is always a synthetic
    # "cell:<provider>:<assertion_id>:<scenario_id>" string. It is the only
    # signal that tells this invocation's three possible assertion cells
    # apart -- see _DISPATCH above -- and is never echoed into result.json.
    parser.add_argument("--variant", required=True)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--engine", required=True, type=Path)
    # NOTE: the real (unmodified) provider_factory/assurance.py
    # execute_retained_plan only passes --codex-bin for provider == "codex".
    # Every other provider, cursor included, gets --longhouse-cli and
    # --provider-bin. Do not rename these to --cursor-bin.
    parser.add_argument("--longhouse-cli", required=True, type=Path)
    parser.add_argument("--provider-bin", required=True, type=Path)
    parser.add_argument("--live-timeout-secs", dest="live_timeout_secs", type=float, default=90.0)
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
    if not args.engine.is_file() or not os.access(args.engine, os.X_OK):
        print(json.dumps({"status": "fail", "failure_code": "longhouse_engine_missing"}))
        return 2
    if not args.longhouse_cli.is_file() or not os.access(args.longhouse_cli, os.X_OK):
        print(json.dumps({"status": "fail", "failure_code": "longhouse_cli_missing"}))
        return 2
    if not args.provider_bin.is_file() or not os.access(args.provider_bin, os.X_OK):
        print(json.dumps({"status": "fail", "failure_code": "cursor_binary_missing"}))
        return 2
    result = run_coordination(args)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
