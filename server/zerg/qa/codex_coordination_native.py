#!/usr/bin/env python3
"""Direct stock-Codex coordination-awareness and directed-input producer.

Covers five assertion cells across three real scenarios, all against a live
managed Codex Helm bridge and the real Runtime Host coordination surface
(``/api/agents/directed-inputs``, ``/api/agents/sessions/{id}/coordination-token``):

* ``codex_coordination_awareness_create`` -- ``coordination_instructions_model_visible``
* ``codex_coordination_awareness_post_compaction`` -- ``coordination_instructions_model_visible_after_compaction``,
  ``no_duplicate_visible_bootstrap``
* ``codex_coordination_directed_input`` -- ``provider_input_receipt_linked`` (send),
  ``attributed_input_visible`` (receive)

Every managed Codex app-server launch (``codex_app_server_args`` in
``engine/src/codex_bridge.rs``) unconditionally wires the ``longhouse`` MCP
server. The create scenario requires a real ``peers`` MCP invocation in the
native rollout, not a model claim that it remembers particular tool names.

The post-compaction scenario uses Codex app-server's typed
``thread/compact/start`` request and requires the corresponding
``item/completed`` notification whose item type is ``contextCompaction``.
The visibility assertion then reads only a native assistant rollout event;
terminal rendering and echoed user prompts are never evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from websockets.sync.client import connect as websocket_connect

from zerg.mcp_server.server import COORDINATION_INSTRUCTIONS
from zerg.qa import codex_provider_release_canary as bridge_canary
from zerg.qa.provider_coordination_oracles import awareness_create_assertions
from zerg.qa.provider_coordination_oracles import awareness_post_compaction_assertions
from zerg.qa.provider_coordination_oracles import directed_input_assertions
from zerg.qa.provider_coordination_scenarios import observe_codex_post_compaction_bootstrap
from zerg.qa.provider_native_resume import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.provider_native_resume import RUNTIME_API_URL_ENV
from zerg.qa.provider_native_resume import _qualification_secrets
from zerg.qa.provider_native_resume import _redact_state_for_evidence
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key

# This producer's live-visibility probes ask the model to name the tools for
# durable recovery and for replying to attributed input, then scan its real
# reply for "inbox"/"reply" (see _answer_demonstrates_visibility). Ground that
# heuristic in the actual instructions text so a future rewording of
# COORDINATION_INSTRUCTIONS that drops those tool names fails loudly here
# instead of silently making every future run of this producer meaningless.
assert (
    "inbox" in COORDINATION_INSTRUCTIONS.lower() and "reply" in COORDINATION_INSTRUCTIONS.lower()
), "coordination visibility probe keywords are no longer grounded in the real MCP server instructions text"

_SCENARIO_CREATE = "codex_coordination_awareness_create"
_SCENARIO_POST_COMPACTION = "codex_coordination_awareness_post_compaction"
_SCENARIO_DIRECTED_INPUT = "codex_coordination_directed_input"

_ASSERTION_CREATE = "coordination_instructions_model_visible"
_ASSERTION_POST_COMPACTION_VISIBLE = "coordination_instructions_model_visible_after_compaction"
_ASSERTION_NO_DUP_BOOTSTRAP = "no_duplicate_visible_bootstrap"
_ASSERTION_SEND = "provider_input_receipt_linked"
_ASSERTION_RECEIVE = "attributed_input_visible"
_SESSION_HEADER = "X-Longhouse-Session-Id"

_CELLS: tuple[tuple[str, str], ...] = (
    (_ASSERTION_CREATE, _SCENARIO_CREATE),
    (_ASSERTION_POST_COMPACTION_VISIBLE, _SCENARIO_POST_COMPACTION),
    (_ASSERTION_NO_DUP_BOOTSTRAP, _SCENARIO_POST_COMPACTION),
    (_ASSERTION_SEND, _SCENARIO_DIRECTED_INPUT),
    (_ASSERTION_RECEIVE, _SCENARIO_DIRECTED_INPUT),
)
# Every one of the above assertions is authored in schemas/managed_providers.yml
# with no `variant:` key, so the compiler's execution_variant is always the
# generated `cell:<provider>:<assertion_id>:<scenario_id>` form (see
# resume_assurance.execution_variant_key). Compute the exact strings the real
# compiler will pass as --variant instead of guessing at a parallel format.
_CELL_BY_VARIANT: dict[str, tuple[str, str]] = {
    execution_variant_key(provider="codex", assertion_id=assertion_id, scenario_id=scenario_id, variant=None): (
        assertion_id,
        scenario_id,
    )
    for assertion_id, scenario_id in _CELLS
}

REGISTRATION = ProducerRegistration(
    producer_id="codex.coordination_awareness.v1",
    producer_revision=5,
    scenario_id=_SCENARIO_CREATE,
    scenario_revision=3,
    scenario_ids=(_SCENARIO_CREATE, _SCENARIO_POST_COMPACTION, _SCENARIO_DIRECTED_INPUT),
    assertion_cells=tuple((assertion_id, None) for assertion_id, _scenario_id in _CELLS),
    providers=("codex",),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "coordination_mcp_server_registered",
        "coordination_question_answered_live",
        "typed_compaction_requested",
        "context_compaction_item_completed",
        "structured_post_compaction_assistant_reply",
        "bootstrap_hook_probe_run",
        "directed_input_created",
        "directed_input_receipt_polled",
        "directed_input_delivery_visible",
    ),
    acquisition_methods=("staged_release",),
    credential_binding_ids=("codex_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "coordination_observation",
        "cleanup_receipt",
    ),
    required_artifacts_by_scenario={
        _SCENARIO_POST_COMPACTION: ("typed_compaction_receipt",),
        _SCENARIO_DIRECTED_INPUT: ("target_send_readiness",),
    },
    required_cleanup=(
        "final_bridge_stopped",
        "final_socket_absent",
        "no_orphan_provider_processes",
    ),
    required_executables=("jq",),
    implementation="server/zerg/qa/codex_coordination_native.py",
    oracle_source="server/zerg/qa/provider_coordination_oracles.py",
    # provider_coordination_oracles.py has three real pure judges
    # (awareness_create_assertions, awareness_post_compaction_assertions,
    # directed_input_assertions); this producer calls whichever one matches
    # the requested assertion's scenario (see module docstring / _run_*
    # dispatch below). oracle_entrypoint names the first alphabetically as a
    # representative single symbol, since the registration schema has no
    # field for "one of several" -- flagged in the accompanying report.
    oracle_entrypoint="awareness_create_assertions",
    executable_module="zerg.qa.codex_coordination_native",
    observation_scope="scenario",
)

_RUNTIME_HOST_USER_AGENT = "LonghouseProviderFactory/1.0"


class _RuntimeHostHTTPError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"Runtime Host HTTP {status}: {detail}")


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


def _api_call(
    api_url: str,
    token: str,
    path: str,
    *,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = {
        "X-Agents-Token": token,
        "Accept": "application/json",
        "User-Agent": _RUNTIME_HOST_USER_AGENT,
    }
    if extra_headers:
        headers.update(extra_headers)
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif method == "POST":
        data = b""
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/api/agents/{path.lstrip('/')}",
        headers=headers,
        data=data,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            raw_detail = exc.read(4096).decode("utf-8", "replace")
        except OSError:
            raw_detail = ""
        try:
            parsed_detail = json.loads(raw_detail)
        except json.JSONDecodeError:
            parsed_detail = raw_detail[:1000]
        if isinstance(parsed_detail, dict):
            detail = str(parsed_detail.get("detail") or parsed_detail)[:1000]
        else:
            detail = str(parsed_detail)[:1000]
        raise _RuntimeHostHTTPError(exc.code, detail) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Runtime Host returned a non-object")
    return payload


def _issue_coordination_token(args: argparse.Namespace, session_id: str) -> str:
    response = _api_call(args.api_url, args.agents_token, f"sessions/{session_id}/coordination-token", method="POST")
    token = str(response.get("coordination_token") or "").strip()
    if not token:
        raise RuntimeError("Runtime Host did not return a session coordination token")
    return token


def _last_assistant_message(thread_path: Path) -> str:
    try:
        lines = thread_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        text = bridge_canary._assistant_text_from_rollout_event(value)
        if text:
            return text
    return ""


def _coordination_awareness_question(marker: str) -> str:
    return (
        f"Without calling any tool, and using only what your MCP server instructions already told you, "
        f"answer in one short sentence starting with exactly {marker}: which exact tool name should be used "
        "for durable recovery when history needs to be found again, and which exact tool name should be used "
        "to reply to attributed input from a peer?"
    )


def _answer_demonstrates_visibility(text: str, marker: str) -> bool:
    lowered = text.lower()
    return marker in text and "inbox" in lowered and "reply" in lowered


def _recv_app_server_message(socket: Any, *, deadline: float) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Codex app-server response deadline expired")
    raw = socket.recv(timeout=remaining)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        raise RuntimeError("Codex app-server returned a non-text WebSocket message")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("Codex app-server returned a non-object JSON-RPC message")
    return value


def _typed_compact_thread(ws_url: str, thread_id: str, *, timeout: float) -> dict[str, Any]:
    """Compact one Codex thread and retain only typed, credential-free proof."""

    deadline = time.monotonic() + timeout
    with websocket_connect(ws_url, open_timeout=min(timeout, 10), close_timeout=5) as socket:
        socket.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "longhouse_coordination_assurance",
                            "title": "Longhouse Coordination Assurance",
                            "version": "1",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                }
            )
        )
        while True:
            message = _recv_app_server_message(socket, deadline=deadline)
            if message.get("id") == 1:
                if message.get("error") is not None:
                    raise RuntimeError(f"Codex app-server initialize failed: {message['error']}")
                break
            if message.get("id") is not None and message.get("method") is not None:
                raise RuntimeError("Codex app-server requested client action during initialize")

        socket.send(json.dumps({"method": "initialized", "params": {}}))
        socket.send(
            json.dumps(
                {
                    "id": 2,
                    "method": "thread/resume",
                    "params": {"threadId": thread_id, "excludeTurns": True},
                }
            )
        )
        while True:
            message = _recv_app_server_message(socket, deadline=deadline)
            if message.get("id") == 2:
                if message.get("error") is not None:
                    raise RuntimeError(f"Codex thread/resume failed: {message['error']}")
                break
            if message.get("id") is not None and message.get("method") is not None:
                raise RuntimeError("Codex app-server requested client action during thread subscription")

        socket.send(
            json.dumps(
                {
                    "id": 3,
                    "method": "thread/compact/start",
                    "params": {"threadId": thread_id},
                }
            )
        )
        response_observed = False
        completed_item: dict[str, Any] | None = None
        completed_turn_id: str | None = None
        while not response_observed or completed_item is None:
            message = _recv_app_server_message(socket, deadline=deadline)
            if message.get("id") == 3:
                if message.get("error") is not None:
                    raise RuntimeError(f"Codex thread/compact/start failed: {message['error']}")
                response_observed = True
                continue
            if message.get("id") is not None and message.get("method") is not None:
                raise RuntimeError("Codex app-server requested client action during compaction")
            if message.get("method") != "item/completed":
                continue
            params = message.get("params")
            if not isinstance(params, dict) or params.get("threadId") != thread_id:
                continue
            item = params.get("item")
            if not isinstance(item, dict) or item.get("type") != "contextCompaction":
                continue
            completed_item = item
            completed_turn_id = str(params.get("turnId") or "") or None

    return {
        "subscription_method": "thread/resume",
        "subscription_completed": True,
        "request_method": "thread/compact/start",
        "request_completed": response_observed,
        "completion_method": "item/completed",
        "context_compaction_completed": completed_item is not None,
        "thread_id": thread_id,
        "turn_id": completed_turn_id,
        "item_id": str((completed_item or {}).get("id") or "") or None,
        "item_type": (completed_item or {}).get("type"),
    }


def _aggregate_cleanup_receipt(receipts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    verifications = [receipt.get("verification") for receipt in receipts.values()]
    complete = bool(verifications) and all(isinstance(value, dict) for value in verifications)
    bridge_stopped = complete and all(value.get("verified") is True for value in verifications)
    sockets_absent = complete and all(value.get("socket_absent") is True for value in verifications)
    processes_dead = complete and all(value.get("owned_processes_dead") is True for value in verifications)
    required_cleanup = {
        "final_bridge_stopped": bridge_stopped,
        "final_socket_absent": sockets_absent,
        "no_orphan_provider_processes": processes_dead,
    }
    return {
        "schema_version": 1,
        "artifact_kind": "codex_coordination_cleanup_receipt",
        "status": "pass" if all(required_cleanup.values()) else "fail",
        "required_cleanup": required_cleanup,
        "sessions": receipts,
    }


def _codex_tool_call_evidence(thread_path: Path) -> list[dict[str, Any]]:
    """Return credential-free native rollout evidence for Codex tool calls."""

    def nested_string(node: object, keys: set[str]) -> str | None:
        if not isinstance(node, dict):
            return None
        for key, value in node.items():
            if key in keys and isinstance(value, str) and value.strip():
                return value
        for key, value in node.items():
            if key in {"arguments", "content", "message", "output", "result", "text"}:
                continue
            found = nested_string(value, keys)
            if found is not None:
                return found
        return None

    try:
        lines = thread_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    evidence: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = row.get("payload") if isinstance(row, dict) else None
        nodes = [payload]
        if isinstance(payload, dict):
            nodes.append(payload.get("item"))
        for node in nodes:
            if not isinstance(node, dict):
                continue
            kind = str(node.get("type") or "")
            if "call" not in kind.replace("_", "").lower():
                continue
            observed = nested_string(node, {"tool", "tool_name", "name"})
            evidence.append(
                {
                    "line": line_number,
                    "type": kind,
                    "server": nested_string(node, {"server", "server_name"}),
                    "tool": observed,
                    "status": node.get("status"),
                }
            )
    return evidence


def _live_send_and_wait(
    args: argparse.Namespace,
    isolation_root: Path,
    session_id: str,
    state_file: Path,
    prompt: str,
    *,
    timeout: int,
) -> dict[str, Any]:
    result = bridge_canary._run(
        [
            str(args.engine),
            "codex-bridge",
            "send",
            "--session-id",
            session_id,
            "--text",
            prompt,
            "--state-root",
            str(bridge_canary._bridge_state_root(isolation_root)),
            "--json",
        ],
        cwd=args.repo_root,
        timeout=timeout + 20,
    )
    if result.returncode:
        raise RuntimeError(f"codex-bridge send failed with status {result.returncode}: {result.stderr[-1000:]}")
    deadline = time.monotonic() + timeout
    state = bridge_canary._read_json(state_file)
    while not bridge_canary._terminal_turn_state(state) and time.monotonic() < deadline:
        time.sleep(0.25)
        state = bridge_canary._read_json(state_file)
    return state


def _run_awareness_create(args: argparse.Namespace, root: Path) -> tuple[dict[str, Any], dict[str, bool]]:
    isolation_root = Path(tempfile.mkdtemp(prefix="lcc-", dir="/tmp"))
    session_id = ""
    observation: dict[str, Any] | None = None
    try:
        summary, _start_result, isolation_root = bridge_canary._start_bridge(
            args,
            evidence_root=root,
            codex_bin=str(args.codex_bin),
            launch_mode="detached_ui",
            isolation_root=isolation_root,
            register_managed=True,
        )
        session_id = str(summary.get("session_id") or "")
        state_file = Path(str(summary.get("state_file") or ""))
        if not session_id or not state_file.is_file():
            raise RuntimeError("awareness-create bridge did not start a resumable provider thread")
        marker = f"LONGHOUSE_COORD_CREATE_{uuid.uuid4().hex}"
        probe_repo = f"longhouse-coordination-awareness-probe-{uuid.uuid4().hex[:12]}"
        prompt = (
            f'Call the Longhouse peers MCP tool with repo="{probe_repo}" and active_only=false. '
            f"After the tool returns, reply with exactly {marker} and nothing else."
        )
        _write_json(root / "awareness-probe.json", {"marker": marker, "probe_repo": probe_repo, "prompt": prompt})
        state = _live_send_and_wait(
            args,
            isolation_root,
            session_id,
            state_file,
            prompt,
            timeout=args.live_send_timeout_secs,
        )
        _write_json(root / "post-question-bridge-state.json", _redact_state_for_evidence(state))
        thread_path = Path(str(state.get("thread_path") or ""))
        assistant_text = _last_assistant_message(thread_path) if thread_path.is_file() else ""
        (root / "assistant-response.txt").write_text(assistant_text, encoding="utf-8")
        tool_calls = _codex_tool_call_evidence(thread_path) if thread_path.is_file() else []
        _write_json(root / "native-tool-call-evidence.json", tool_calls)
        peers_invoked = any(
            isinstance(item.get("tool"), str) and item["tool"].rsplit("__", 1)[-1].rsplit(".", 1)[-1] == "peers" for item in tool_calls
        )
        visible = peers_invoked and marker in assistant_text
        observation = {
            "coordination_instructions_model_visible": visible,
            "coordination_mcp_tool_invoked": peers_invoked,
            "turn_completed": state.get("last_turn_status") == "completed",
            "marker_present_in_reply": marker in assistant_text,
        }
        return observation, awareness_create_assertions(observation)
    finally:
        receipts: dict[str, dict[str, Any]] = {}
        if session_id:
            receipts["session"] = bridge_canary._stop_bridge(args, session_id, isolation_root)
        cleanup = _aggregate_cleanup_receipt(receipts)
        _write_json(root / "cleanup-receipt.json", cleanup)
        if observation is not None:
            observation.update(cleanup["required_cleanup"])


def _run_awareness_post_compaction(args: argparse.Namespace, root: Path) -> tuple[dict[str, Any], dict[str, bool]]:
    bootstrap_observation = observe_codex_post_compaction_bootstrap()
    _write_json(root / "bootstrap-noise-observation.json", bootstrap_observation)

    isolation_root = Path(tempfile.mkdtemp(prefix="lcp-", dir="/tmp"))
    session_id = ""
    cleanup: dict[str, Any] = _aggregate_cleanup_receipt({})
    compaction_signal_observed = False
    answered_after_compact_attempt = False
    try:
        summary, _start_result, isolation_root = bridge_canary._start_bridge(
            args,
            evidence_root=root,
            codex_bin=str(args.codex_bin),
            launch_mode="detached_ui",
            isolation_root=isolation_root,
            register_managed=True,
        )
        session_id = str(summary.get("session_id") or "")
        ws_url = str(summary.get("ws_url") or "")
        state_file = Path(str(summary.get("state_file") or ""))
        if not session_id or not ws_url or not state_file.is_file():
            raise RuntimeError("post-compaction bridge did not return its typed control state")
        seed_marker = f"LONGHOUSE_COORD_COMPACT_SEED_{uuid.uuid4().hex}"
        state = _live_send_and_wait(
            args,
            isolation_root,
            session_id,
            state_file,
            f"Reply with exactly {seed_marker} and nothing else.",
            timeout=args.live_send_timeout_secs,
        )
        thread_id = str(state.get("thread_id") or "").strip()
        seeded_thread_path = Path(str(state.get("thread_path") or ""))
        seed_reply = _last_assistant_message(seeded_thread_path) if seeded_thread_path.is_file() else ""
        if not thread_id or state.get("last_turn_status") != "completed" or seed_marker not in seed_reply:
            raise RuntimeError("Codex seed turn did not produce structured assistant history before compaction")

        compaction_receipt = _typed_compact_thread(
            ws_url,
            thread_id,
            timeout=float(args.live_send_timeout_secs),
        )
        _write_json(root / "typed-compaction-receipt.json", compaction_receipt)
        compaction_signal_observed = bool(
            compaction_receipt.get("request_completed")
            and compaction_receipt.get("context_compaction_completed")
            and compaction_receipt.get("item_type") == "contextCompaction"
        )

        marker = f"LONGHOUSE_COORD_COMPACT_{uuid.uuid4().hex}"
        state = _live_send_and_wait(
            args,
            isolation_root,
            session_id,
            state_file,
            _coordination_awareness_question(marker),
            timeout=args.live_send_timeout_secs,
        )
        final_thread_path = Path(str(state.get("thread_path") or ""))
        assistant_text = _last_assistant_message(final_thread_path) if final_thread_path.is_file() else ""
        (root / "post-compaction-assistant-response.txt").write_text(assistant_text, encoding="utf-8")
        answered_after_compact_attempt = state.get("last_turn_status") == "completed" and _answer_demonstrates_visibility(
            assistant_text,
            marker,
        )
    finally:
        receipts: dict[str, dict[str, Any]] = {}
        if session_id:
            receipts["session"] = bridge_canary._stop_bridge(args, session_id, isolation_root)
        cleanup = _aggregate_cleanup_receipt(receipts)
        _write_json(root / "cleanup-receipt.json", cleanup)

    observation = {
        "visible_bootstrap_count": bootstrap_observation.get("visible_bootstrap_count"),
        "compaction_signal_observed": compaction_signal_observed,
        "post_compact_question_answered": answered_after_compact_attempt,
        "assistant_evidence_source": "native_rollout_assistant_event",
        "coordination_instructions_model_visible_after_compaction": bool(compaction_signal_observed and answered_after_compact_attempt),
    }
    observation.update(cleanup["required_cleanup"])
    return observation, awareness_post_compaction_assertions(observation)


def _run_directed_input(args: argparse.Namespace, root: Path) -> tuple[dict[str, Any], dict[str, bool]]:
    source_root = root / "source"
    target_root = root / "target"
    source_root.mkdir()
    target_root.mkdir()
    source_isolation = Path(tempfile.mkdtemp(prefix="lcs-", dir="/tmp"))
    target_isolation = Path(tempfile.mkdtemp(prefix="lcd-", dir="/tmp"))
    source_session_id = ""
    target_session_id = ""
    minted_secrets: list[str] = []
    observation: dict[str, Any] | None = None
    try:
        source_summary, _sr, source_isolation = bridge_canary._start_bridge(
            args,
            evidence_root=source_root,
            codex_bin=str(args.codex_bin),
            launch_mode="detached_ui",
            isolation_root=source_isolation,
            register_managed=True,
        )
        target_summary, _tr, target_isolation = bridge_canary._start_bridge(
            args,
            evidence_root=target_root,
            codex_bin=str(args.codex_bin),
            launch_mode="detached_ui",
            isolation_root=target_isolation,
            register_managed=True,
        )
        source_session_id = str(source_summary.get("session_id") or "")
        target_session_id = str(target_summary.get("session_id") or "")
        if not source_session_id or not target_session_id:
            raise RuntimeError("directed-input peers did not both register a managed session")

        source_token = _issue_coordination_token(args, source_session_id)
        target_token = _issue_coordination_token(args, target_session_id)
        minted_secrets.extend((source_token, target_token))
        text = f"LONGHOUSE_DIRECTED_INPUT_{uuid.uuid4().hex}: please note you received this from a peer session."
        client_request_id = str(uuid.uuid4())
        created: dict[str, Any] = {}
        create_attempts = 0
        directed_input_id: object = None
        input_persisted = False
        input_receipt_linked = False
        input_visible = False
        observed_source_session_id = ""
        deadline = time.monotonic() + args.live_send_timeout_secs
        while time.monotonic() < deadline and (not input_receipt_linked or not input_visible):
            if not input_receipt_linked:
                create_attempts += 1
                created = _api_call(
                    args.api_url,
                    source_token,
                    "directed-inputs",
                    method="POST",
                    extra_headers={_SESSION_HEADER: source_session_id},
                    json_body={
                        "target_session_id": target_session_id,
                        "text": text,
                        "client_request_id": client_request_id,
                    },
                )
                directed_input_id = created.get("id")
                input_persisted = directed_input_id is not None
                input_receipt_linked = created.get("input_receipt") is not None
                outbound = _api_call(
                    args.api_url,
                    source_token,
                    "directed-inputs?direction=outbound",
                    method="GET",
                    extra_headers={_SESSION_HEADER: source_session_id},
                )
                for item in outbound.get("directed_inputs", []):
                    if isinstance(item, dict) and item.get("id") == directed_input_id and item.get("input_receipt") is not None:
                        input_receipt_linked = True
                        break
            inbound = _api_call(
                args.api_url,
                target_token,
                "directed-inputs?direction=inbound",
                method="GET",
                extra_headers={_SESSION_HEADER: target_session_id},
            )
            for item in inbound.get("directed_inputs", []):
                if isinstance(item, dict) and item.get("id") == directed_input_id:
                    input_visible = True
                    observed_source_session_id = str(item.get("source_session_id") or "")
                    break
            if not input_receipt_linked or not input_visible:
                time.sleep(0.5)
        _write_json(
            root / "target-send-readiness.json",
            {
                "session_id": target_session_id,
                "delivery_receipt_observed": input_receipt_linked,
                "create_attempts": create_attempts,
            },
        )
        _write_json(
            root / "directed-input-create-receipt.json",
            {"id": directed_input_id, "has_immediate_receipt": created.get("input_receipt") is not None},
        )
        _write_json(
            root / "directed-input-poll-result.json",
            {
                "input_receipt_linked": input_receipt_linked,
                "input_visible": input_visible,
                "observed_source_session_id": observed_source_session_id,
            },
        )
        observation = {
            "input_persisted": input_persisted,
            "input_receipt_linked": input_receipt_linked,
            "input_visible": input_visible,
            "source_session_id": observed_source_session_id,
        }
        return observation, directed_input_assertions(observation)
    finally:
        redacted = _redact_retained_secrets(root, minted_secrets)
        if redacted:
            _write_json(root / "redacted-secret-files.json", {"paths": redacted})
        cleanup_receipts: dict[str, Any] = {}
        if source_session_id:
            cleanup_receipts["source"] = bridge_canary._stop_bridge(args, source_session_id, source_isolation)
        if target_session_id:
            cleanup_receipts["target"] = bridge_canary._stop_bridge(args, target_session_id, target_isolation)
        cleanup = _aggregate_cleanup_receipt(cleanup_receipts)
        _write_json(root / "cleanup-receipt.json", cleanup)
        if observation is not None:
            observation.update(cleanup["required_cleanup"])


def run_coordination(args: argparse.Namespace) -> dict[str, Any]:
    assertion_id, scenario_id = _CELL_BY_VARIANT[args.variant]
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    provider_receipt = {
        "path": str(args.codex_bin),
        "sha256": _sha256(args.codex_bin),
        "version": bridge_canary._run([str(args.codex_bin), "--version"], timeout=30).stdout.strip(),
    }
    _write_json(root / "provider-binary-receipt.json", provider_receipt)
    try:
        if scenario_id == _SCENARIO_CREATE:
            observation, assertions = _run_awareness_create(args, root)
        elif scenario_id == _SCENARIO_POST_COMPACTION:
            observation, assertions = _run_awareness_post_compaction(args, root)
        else:
            observation, assertions = _run_directed_input(args, root)
        _write_json(root / "coordination-observation.json", observation)
        result: dict[str, Any] = {
            "schema_version": 1,
            "artifact_kind": "codex_coordination_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "codex",
            "variant": None,
            "scenario_id": scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": _now(),
            "status": "pass",
            "observation_scope": "scenario",
            "observation": observation,
            "assertions": assertions,
            "provider_binary": provider_receipt,
            "artifact_manifest": _artifact_manifest(root),
        }
        _write_json(root / "result.json", result)
        return result
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        failure = {
            "schema_version": 1,
            "artifact_kind": "codex_coordination_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "codex",
            "variant": None,
            "scenario_id": scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": _now(),
            "status": "fail",
            "observation_scope": "scenario",
            "failure_code": "codex_coordination_scenario_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "assertions": {assertion_id: False},
            "artifact_manifest": _artifact_manifest(root),
        }
        _write_json(root / "result.json", failure)
        return failure


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", required=True, choices=tuple(_CELL_BY_VARIANT))
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
    result = run_coordination(args)
    redacted = _redact_retained_secrets(
        args.evidence_root,
        list(_qualification_secrets(os.environ, args.agents_token)),
    )
    if redacted and result.get("status") == "pass":
        result["status"] = "fail"
        result["failure_code"] = "finalized_artifact_secret_scan_failed"
        assertions = result.get("assertions")
        if isinstance(assertions, dict):
            assertions[_CELL_BY_VARIANT[args.variant][0]] = False
        _write_json(args.evidence_root / "result.json", result)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
