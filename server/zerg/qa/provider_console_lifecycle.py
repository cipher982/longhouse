#!/usr/bin/env python3
"""Live provider-release proof for the complete Console control path.

The producer deliberately enters through the Runtime Host HTTP API.  A
disposable real Machine Agent receives ``session.turn.start`` over its normal
control WebSocket and launches the exact staged stock provider binary.  The
proof then joins the local adapter claim to the Runtime Host's durable
assistant event, exercises the provider's typed interrupt contract, and
checks exact-process cleanup.
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
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from zerg.qa.codex_auth import login_with_api_key
from zerg.qa.console_served_state_core import assistant_marker_events
from zerg.qa.console_served_state_core import event_text
from zerg.qa.live_session_toolkit import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.live_session_toolkit import RUNTIME_API_URL_ENV
from zerg.qa.live_session_toolkit import TranscriptShipper
from zerg.qa.live_session_toolkit import isolated_provider_home
from zerg.qa.live_session_toolkit import retire_qualification_session
from zerg.qa.live_session_toolkit import start_transcript_shipper
from zerg.qa.live_session_toolkit import write_json
from zerg.qa.pi_native import pi_transcript_rows
from zerg.qa.provider_release_identity import artifact_manifest
from zerg.qa.provider_release_identity import now
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.services.provider_interaction_semantics import omp_agent_end_is_terminal

PROVIDERS = ("codex", "claude", "opencode", "cursor")
INTERRUPT_SUPPORTED = frozenset({"claude", "opencode", "cursor", "pi", "omp"})
INTERRUPT_UNSUPPORTED = frozenset({"codex"})
# Only OMP's retained JSONL stream carries the terminal agent_end contract used
# by the post-interrupt evidence check. The other supported adapters settle via
# their own Runtime Host/claim contracts.
INTERRUPT_OUTPUT_TERMINAL_PROVIDERS = frozenset({"omp"})
ASSERTION_ID = "console_adapter_release_contract_preserved"
SUPPORTED_VARIANT = "interrupt_supported"
UNSUPPORTED_VARIANT = "interrupt_unsupported"
SCENARIO_IDS = tuple(f"{provider}_console_adapter_lifecycle" for provider in PROVIDERS)
OBSERVED_ACTIVITY = (
    "adapter_dispatch_started",
    "qualification_model_bound",
    "stock_provider_response_bound",
    "exact_session_thread_run_binding",
    "transcript_converged_exactly_once",
    "interrupt_contract_preserved",
    "post_interrupt_sendable",
    "no_orphan_provider_processes",
)
PROVIDER_BIN_ENV = {
    "codex": "LONGHOUSE_CODEX_BIN",
    "claude": "LONGHOUSE_CLAUDE_BIN",
    "opencode": "LONGHOUSE_OPENCODE_BIN",
    "cursor": "LONGHOUSE_CURSOR_BIN",
    "pi": "LONGHOUSE_PI_BIN",
    "omp": "LONGHOUSE_OMP_BIN",
}
ADAPTERS = {
    "codex": "codex_exec",
    "claude": "claude_print",
    "opencode": "opencode_run",
    "cursor": "cursor_print",
    "pi": "pi_print",
    "omp": "omp_print",
}
CAN_RESUME = frozenset({"codex", "claude", "opencode", "cursor", "pi", "omp"})
OMP_CONTEXT_LABEL = "LONGHOUSE_CONTEXT_VALUE"
_VERSION_PATTERNS = {
    "codex": re.compile(r"^codex-cli (?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$"),
    "claude": re.compile(r"^(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?) \(Claude Code\)$"),
    "opencode": re.compile(r"^(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$"),
    "cursor": re.compile(r"^(?P<version>\d{4}\.\d{2}\.\d{2}(?:-[0-9A-Za-z.-]+)?)$"),
    "pi": re.compile(r"^(?P<version>\d+\.\d+\.\d+)$"),
    "omp": re.compile(r"^omp/(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$"),
}

REGISTRATION = ProducerRegistration(
    producer_id="provider.console_lifecycle.v1",
    producer_revision=13,
    scenario_id=SCENARIO_IDS[0],
    scenario_ids=SCENARIO_IDS,
    scenario_revision=4,
    assertion_cells=(
        (ASSERTION_ID, SUPPORTED_VARIANT),
        (ASSERTION_ID, UNSUPPORTED_VARIANT),
    ),
    providers=PROVIDERS,
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("console",),
    evidence_classes=("live_token",),
    observed_activity=OBSERVED_ACTIVITY,
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=(),
    credential_binding_ids_by_provider={provider: (f"{provider}_provider_token", "runtime_host_control") for provider in PROVIDERS},
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "adapter_dispatch_receipt",
        "transcript_flush_receipt",
        "console_boundary_receipt",
        "provider_response_binding_receipt",
        "interrupt_contract_receipt",
        "console_continuation_receipt",
        "cleanup_receipt",
    ),
    required_artifacts_by_scenario={
        "codex_console_adapter_lifecycle": ("provider_auth_receipt",),
    },
    required_cleanup=(
        "provider_process_dead",
        "process_group_dead",
        "no_orphan_provider_processes",
        "canary_session_hidden",
    ),
    implementation="server/zerg/qa/provider_console_lifecycle.py",
    oracle_source="server/zerg/qa/provider_console_lifecycle.py",
    oracle_entrypoint="console_lifecycle_assertions",
    executable_module="zerg.qa.provider_console_lifecycle",
    provider_artifact_required=True,
    subject_kind="provider_release",
)


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _artifact_manifest_after_shipper_stopped(
    root: Path,
    shipper: TranscriptShipper | None,
) -> list[dict[str, object]]:
    """Seal evidence only after its last background writer has exited."""

    if shipper is not None:
        shipper.stop()
    return artifact_manifest(root)


def _expected_variant(provider: str) -> str:
    return SUPPORTED_VARIANT if provider in INTERRUPT_SUPPORTED else UNSUPPORTED_VARIANT


def _interrupt_output_contract_applies(provider: str) -> bool:
    return provider in INTERRUPT_OUTPUT_TERMINAL_PROVIDERS


def _scenario_id(provider: str) -> str:
    return f"{provider}_console_adapter_lifecycle"


def _provider_environment(provider: str, args: argparse.Namespace, home: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["HOME"] = str(home)
    environment["LONGHOUSE_ENGINE_BIN"] = str(args.engine)
    environment["LONGHOUSE_ORIGIN_KIND"] = "test_or_canary"
    environment["LONGHOUSE_LAUNCH_ACTOR"] = "automation"
    environment["LONGHOUSE_LAUNCH_SURFACE"] = "test"
    environment[PROVIDER_BIN_ENV[provider]] = str(args.provider_bin)
    if provider == "codex" and args.model:
        environment["CODEX_MODEL"] = args.model
    if provider == "codex":
        environment["CODEX_HOME"] = str(home / ".codex")
        environment["XDG_CONFIG_HOME"] = str(home / ".config")
        environment["XDG_DATA_HOME"] = str(home / ".local" / "share")
        environment["XDG_CACHE_HOME"] = str(home / ".cache")
    if provider == "pi":
        environment["PI_CODING_AGENT_DIR"] = str(home / ".pi")
        environment["LONGHOUSE_PI_QUALIFICATION_MODEL"] = args.model
    if provider == "omp":
        environment["XDG_DATA_HOME"] = str(home / ".local" / "share")
        environment["LONGHOUSE_OMP_DATA_DIR"] = str(home / ".local" / "share" / "omp")
        environment["LONGHOUSE_OMP_SESSION_DIR"] = str(home / ".local" / "share" / "omp" / "sessions")
    environment.setdefault("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    environment.setdefault("CURSOR_HOME", str(home / ".cursor"))
    return environment


def _probe_version(provider: str, binary: Path) -> tuple[str, str]:
    completed = subprocess.run(
        [str(binary), "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{provider} --version failed with exit code {completed.returncode}")
    raw = completed.stdout.strip()
    match = _VERSION_PATTERNS[provider].fullmatch(raw)
    if match is None:
        raise RuntimeError(f"{provider} --version did not match its release grammar")
    return str(match.group("version")), raw


def _console_runtime_paths(home: Path) -> tuple[Path, Path, Path, Path]:
    """Return compact per-sandbox paths for Console runtime and IPC state."""

    runtime_root = home / "c"
    return runtime_root, runtime_root / "e", runtime_root / "w", runtime_root / "lh"


def _request(
    api_url: str,
    token: str,
    method: str,
    path: str,
    payload: Mapping[str, object] | None = None,
    *,
    timeout: float = 30,
) -> dict[str, Any]:
    body = json.dumps(dict(payload)).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}{path}",
        data=body,
        method=method,
        headers={
            "X-Agents-Token": token,
            "Content-Type": "application/json",
            "User-Agent": "LonghouseProviderConsoleLifecycle/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1000]
        raise RuntimeError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{method} {path} returned a non-object response")
    return value


def _create_session(
    *,
    api_url: str,
    token: str,
    provider: str,
    device_id: str,
    cwd: Path,
    model: str | None,
) -> dict[str, Any]:
    payload = {
        "provider": provider,
        "device_id": device_id,
        "cwd": str(cwd),
        "project": f"provider-console-{provider}",
        "display_name": f"{provider} Console release qualification",
        "launch_surface": "test",
    }
    if model:
        payload["model"] = _native_model(provider, model)
    deadline = time.monotonic() + 45
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return _request(api_url, token, "POST", "/api/agents/sessions", payload)
        except RuntimeError as exc:
            last_error = exc
            if "adapter_unavailable" not in str(exc):
                raise
            time.sleep(0.25)
    raise RuntimeError(f"Machine Agent never advertised {provider}.turn_start: {last_error}")


def _start_turn(*, api_url: str, token: str, session_id: str, message: str, request_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    stable_turn_id: str | None = None
    last_result: dict[str, Any] | None = None
    while True:
        try:
            result = _request(
                api_url,
                token,
                "POST",
                f"/api/agents/sessions/{session_id}/turns",
                {"message": message, "client_request_id": request_id},
            )
        except RuntimeError as exc:
            detail = str(exc)
            transient = (
                "adapter_unavailable" in detail
                or "returned HTTP 429" in detail
                or "returned HTTP 503" in detail
                or "Request timed out" in detail
            )
            if not transient or time.monotonic() >= deadline:
                raise
            time.sleep(0.25)
            continue
        last_result = result
        if result.get("state") not in {"queued", "starting", "active", "completed"}:
            raise RuntimeError(f"Console turn was not accepted: {result}")
        raw_turn_id = result.get("turn_id")
        if raw_turn_id is None or isinstance(raw_turn_id, bool) or not str(raw_turn_id):
            raise RuntimeError(f"Console turn returned no stable turn_id: {result}")
        turn_id = str(raw_turn_id)
        if stable_turn_id is None:
            stable_turn_id = turn_id
        elif turn_id != stable_turn_id:
            raise RuntimeError(f"Console request replay changed turn_id from {stable_turn_id} to {turn_id}")
        if isinstance(result.get("run_id"), str) and result["run_id"]:
            return result
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Console turn never acquired a stable run_id: {last_result}")
        # Queued FIFO turns intentionally have no run until the prior owner
        # releases. Replaying the same idempotency key observes that same turn
        # after ownership transfers; it must not manufacture another turn.
        time.sleep(0.25)


def _wait_turn_terminal(
    *,
    api_url: str,
    token: str,
    session_id: str,
    message: str,
    request_id: str,
    turn_id: str,
    run_id: str,
    timeout: float = 30,
) -> dict[str, Any]:
    """Wait until the Runtime Host, not only the local adapter, sees terminal."""

    deadline = time.monotonic() + timeout
    last_result: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        result = _request(
            api_url,
            token,
            "POST",
            f"/api/agents/sessions/{session_id}/turns",
            {"message": message, "client_request_id": request_id},
        )
        last_result = result
        if str(result.get("turn_id") or "") != turn_id or str(result.get("run_id") or "") != run_id:
            raise RuntimeError(f"Console terminal replay changed turn identity: {result}")
        state = str(result.get("state") or "")
        if state in {"completed", "failed", "cancelled"}:
            return result
        if state not in {"starting", "active"}:
            raise RuntimeError(f"Console turn lost its execution owner before terminal: {result}")
        time.sleep(0.25)
    raise RuntimeError(f"Runtime Host never observed Console turn terminal: {last_result}")


def _claim_path(longhouse_home: Path, run_id: str) -> Path:
    return longhouse_home / "agent" / "turn-claims" / f"{run_id}.json"


def _read_claim(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _wait_claim(
    path: Path,
    *,
    states: frozenset[str],
    timeout: float = 180,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = _read_claim(path)
        if last is not None and last.get("state") in states:
            return last
        time.sleep(0.1)
    raise RuntimeError(f"Console claim did not reach {sorted(states)}: {last}")


def _assistant_marker_events(api_url: str, token: str, session_id: str, marker: str) -> list[dict[str, Any]]:
    result = _request(api_url, token, "GET", f"/api/agents/sessions/{session_id}/events?limit=200")
    events = result.get("events") if isinstance(result.get("events"), list) else []
    # This archive endpoint contains only durable records and omits origin.
    # Served projections use the same predicate without that archive default.
    return assistant_marker_events(events, marker, default_origin="durable")


def _wait_exact_assistant_marker(
    api_url: str,
    token: str,
    session_id: str,
    marker: str,
    *,
    timeout: float = 180,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    stable_since: float | None = None
    last_count = 0
    while time.monotonic() < deadline:
        matches = _assistant_marker_events(api_url, token, session_id, marker)
        last_count = len(matches)
        if last_count > 1 or any(event_text(event).count(marker) != 1 for event in matches):
            raise RuntimeError("assistant marker did not converge to one event with exactly one occurrence")
        if last_count == 1:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= 2:
                return matches
        else:
            stable_since = None
        time.sleep(0.5)
    raise RuntimeError(f"assistant marker did not converge exactly once (count={last_count})")


def _bounded_marker_excerpt(value: str, marker: str, radius: int = 160) -> str:
    index = value.find(marker)
    if index < 0:
        return ""
    start = max(0, index - radius)
    end = min(len(value), index + len(marker) + radius)
    return value[start:end]


def _assistant_output_texts(provider: str, content: str) -> list[str]:
    texts: list[str] = []
    for line in content.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        if provider in {"claude", "cursor"} and event.get("type") == "assistant":
            message = event.get("message")
            blocks = message.get("content") if isinstance(message, Mapping) else None
            if isinstance(blocks, list):
                texts.extend(
                    str(block["text"])
                    for block in blocks
                    if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str)
                )
        elif provider == "opencode" and event.get("type") == "text":
            part = event.get("part")
            if isinstance(part, Mapping) and part.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(str(part["text"]))
        elif provider == "codex" and event.get("type") == "response_item":
            payload = event.get("payload")
            if not isinstance(payload, Mapping) or payload.get("role") != "assistant":
                continue
            blocks = payload.get("content")
            if isinstance(blocks, list):
                texts.extend(
                    str(block["text"])
                    for block in blocks
                    if isinstance(block, Mapping) and block.get("type") in {"output_text", "text"} and isinstance(block.get("text"), str)
                )
            elif isinstance(payload.get("text"), str):
                texts.append(str(payload["text"]))
        elif provider == "pi" and event.get("type") == "message_end":
            message = event.get("message")
            if not isinstance(message, Mapping) or message.get("role") != "assistant":
                continue
            blocks = message.get("content")
            if isinstance(blocks, list):
                texts.extend(
                    str(block["text"])
                    for block in blocks
                    if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str)
                )
            elif isinstance(blocks, str):
                texts.append(blocks)
        elif provider == "omp" and event.get("type") == "message_end":
            message = event.get("message")
            if isinstance(message, Mapping) and message.get("role") == "assistant":
                blocks = message.get("content")
                if isinstance(blocks, list):
                    texts.extend(
                        str(block["text"])
                        for block in blocks
                        if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str)
                    )
                elif isinstance(blocks, str):
                    texts.append(blocks)
    return texts


def _claim_output_evidence(provider: str, claim: Mapping[str, object], marker: str) -> dict[str, object] | None:
    candidates: list[dict[str, object]] = []
    for key in ("stdout_path", "source_path"):
        raw = claim.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            content = Path(raw).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        assistant_output = "\n".join(_assistant_output_texts(provider, content))
        candidates.append(
            {
                "provider_response_source_kind": key,
                "provider_response_source_path": raw,
                "provider_response_marker_count": assistant_output.count(marker),
                "provider_response_excerpt": _bounded_marker_excerpt(assistant_output, marker),
                "provider_response_source_bytes": len(content.encode("utf-8")),
                "provider_response_source_sha256": _sha256_bytes(content.encode("utf-8")),
            }
        )
    return max(candidates, key=lambda item: int(item["provider_response_marker_count"])) if candidates else None


def _pi_tool_evidence(claim: Mapping[str, object], marker: str) -> dict[str, object] | None:
    """Read Pi's native JSON-mode stream from the real Console child claim."""
    paths: list[Path] = []
    for key in ("stdout_path", "source_path"):
        value = claim.get(key)
        if isinstance(value, str) and value:
            paths.append(Path(value))
    candidates: list[dict[str, object]] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        events: list[Mapping[str, object]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, Mapping):
                events.append(value)
        tool_call_ids: set[str] = set()
        tool_result_ids: set[str] = set()
        output_marker_observed = False
        native_shapes: dict[str, int] = {}
        for event in events:
            event_type = str(event.get("type") or "")
            native_shapes[event_type] = native_shapes.get(event_type, 0) + 1
            if event_type == "tool_execution_start":
                call_id = str(event.get("toolCallId") or "")
                if call_id:
                    tool_call_ids.add(call_id)
            elif event_type == "tool_execution_end":
                call_id = str(event.get("toolCallId") or "")
                if call_id:
                    tool_result_ids.add(call_id)
                output_marker_observed = output_marker_observed or marker in json.dumps(event, sort_keys=True)
            elif event_type == "message_end":
                message = event.get("message")
                if not isinstance(message, Mapping):
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, Mapping):
                        continue
                    if block.get("type") == "toolCall" and block.get("id"):
                        tool_call_ids.add(str(block["id"]))
        candidates.append(
            {
                "source_path": str(path),
                "native_shapes": native_shapes,
                "tool_call_ids": sorted(tool_call_ids),
                "tool_result_ids": sorted(tool_result_ids),
                "linked_tool_call_ids": sorted(tool_call_ids & tool_result_ids),
                "output_marker_observed": output_marker_observed,
            }
        )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            len(item.get("linked_tool_call_ids") or []),
            bool(item.get("output_marker_observed")),
            len(item.get("native_shapes") or {}),
        ),
    )


def _pi_native_marker_evidence(
    path: Path,
    marker: str,
    *,
    minimum_source_offset: int = 0,
    maximum_source_offset: int | None = None,
) -> dict[str, object] | None:
    """Return the native JSONL message id for one assistant marker."""
    rows, provider_session_id, _metadata = pi_transcript_rows(path)
    matches = [
        row
        for row in rows
        if row.get("type") == "assistant"
        and int(row.get("source_offset") or 0) >= minimum_source_offset
        and (maximum_source_offset is None or int(row.get("source_offset") or 0) < maximum_source_offset)
        and str(row.get("text") or "").count(marker) == 1
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("entry_id"), str) or not matches[0]["entry_id"]:
        return None
    row = matches[0]
    return {
        "native_message_id": row["entry_id"],
        "provider_session_id": provider_session_id,
        "source_offset": row.get("source_offset"),
        "marker_count": str(row.get("text") or "").count(marker),
    }


def _omp_native_marker_evidence(
    path: Path,
    marker: str,
    *,
    minimum_source_offset: int = 0,
    maximum_source_offset: int | None = None,
) -> dict[str, object] | None:
    """Return OMP's native assistant message id for one bounded marker."""
    try:
        payload = path.read_bytes()
    except OSError:
        return None

    headers: list[str] = []
    matches: list[dict[str, object]] = []
    source_offset = 0

    def message_text(message: Mapping[str, object]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        return "".join(
            str(block["text"])
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str)
        )

    for raw_line in payload.splitlines(keepends=True):
        line_end = source_offset + len(raw_line)
        if raw_line.strip():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                event = None
            if isinstance(event, Mapping):
                if event.get("type") == "session" and isinstance(event.get("id"), str) and event["id"]:
                    headers.append(event["id"])
                if (
                    event.get("type") in {"message", "message_end"}
                    and isinstance(event.get("id"), str)
                    and event["id"]
                    and isinstance(event.get("message"), Mapping)
                    and event["message"].get("role") == "assistant"
                    and source_offset >= minimum_source_offset
                    and (maximum_source_offset is None or line_end <= maximum_source_offset)
                ):
                    text = message_text(event["message"])
                    if text.count(marker) == 1:
                        matches.append(
                            {
                                "native_message_id": event["id"],
                                "provider_session_id": headers[0] if len(headers) == 1 else None,
                                "source_offset": source_offset,
                                "marker_count": 1,
                            }
                        )
        source_offset = line_end

    if len(headers) != 1 or len(matches) != 1:
        return None
    return matches[0]


def _native_marker_evidence(
    provider: str,
    path: Path,
    marker: str,
    *,
    minimum_source_offset: int = 0,
    maximum_source_offset: int | None = None,
) -> dict[str, object] | None:
    if provider == "pi":
        return _pi_native_marker_evidence(
            path,
            marker,
            minimum_source_offset=minimum_source_offset,
            maximum_source_offset=maximum_source_offset,
        )
    if provider == "omp":
        return _omp_native_marker_evidence(
            path,
            marker,
            minimum_source_offset=minimum_source_offset,
            maximum_source_offset=maximum_source_offset,
        )
    return None


def _pi_continuation_linkage(
    native: Mapping[str, object] | None,
    projected_assistant_event_id: object,
    *,
    projected_marker_count: int,
    same_session: bool,
    same_thread: bool,
    native_provider_thread_id: object,
    projected_provider_thread_id: object,
) -> dict[str, object]:
    native_message_id = native.get("native_message_id") if isinstance(native, Mapping) else None
    native_marker_count = native.get("marker_count") if isinstance(native, Mapping) else None
    native_provider_session_id = native.get("provider_session_id") if isinstance(native, Mapping) else None
    native_session_matches_provider_thread = native_provider_session_id == native_provider_thread_id
    native_and_projected_ids_equal = (
        isinstance(native_message_id, str)
        and bool(native_message_id)
        and projected_assistant_event_id is not None
        and native_message_id == projected_assistant_event_id
    )
    native_and_projected_ids_distinct = (
        isinstance(native_message_id, str)
        and bool(native_message_id)
        and projected_assistant_event_id is not None
        and native_message_id != projected_assistant_event_id
    )
    native_and_projected_ids_bound = native_and_projected_ids_equal or native_and_projected_ids_distinct
    return {
        "native_message_id_present": isinstance(native_message_id, str) and bool(native_message_id),
        "projected_assistant_event_id_present": projected_assistant_event_id is not None,
        "native_marker_count": native_marker_count,
        "projected_marker_count": projected_marker_count,
        "same_session": same_session,
        "same_thread": same_thread,
        "same_provider_thread": native_provider_thread_id == projected_provider_thread_id,
        "native_session_matches_provider_thread": native_session_matches_provider_thread,
        "native_and_projected_ids_equal": native_and_projected_ids_equal,
        "native_and_projected_ids_distinct": native_and_projected_ids_distinct,
        "native_and_projected_ids_bound": native_and_projected_ids_bound,
        "proven": (
            isinstance(native_message_id, str)
            and bool(native_message_id)
            and projected_assistant_event_id is not None
            and native_marker_count == 1
            and projected_marker_count == 1
            and same_session
            and same_thread
            and native_provider_thread_id == projected_provider_thread_id
            and native_session_matches_provider_thread
            and native_and_projected_ids_bound
        ),
    }


def _pi_native_tool_observation(path: Path, marker: str) -> dict[str, object]:
    """Extract one native Pi tool pair and its following marker response."""

    rows, provider_session_id, metadata = pi_transcript_rows(path)
    raw_messages: dict[str, Mapping[str, object]] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, Mapping) or event.get("type") != "message":
                continue
            event_id = event.get("id")
            message = event.get("message")
            if isinstance(event_id, str) and isinstance(message, Mapping):
                raw_messages[event_id] = message
    except OSError as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "provider_session_id": provider_session_id}

    calls: dict[str, dict[str, object]] = {}
    results: dict[str, dict[str, object]] = {}
    marker_responses: list[dict[str, object]] = []
    native_models: list[str] = []
    native_providers: list[str] = []
    for row in rows:
        message = row.get("message") if isinstance(row.get("message"), Mapping) else {}
        model = message.get("model") if isinstance(message, Mapping) else None
        if isinstance(model, str) and model.strip():
            native_models.append(model.strip())
        provider_name = message.get("provider") if isinstance(message, Mapping) else None
        if isinstance(provider_name, str) and provider_name.strip():
            native_providers.append(provider_name.strip())
        if isinstance(row.get("model"), str) and row["model"].strip():
            native_models.append(str(row["model"]).strip())
        if row.get("type") == "assistant":
            text = str(row.get("text") or "")
            if marker and marker in text:
                marker_responses.append(
                    {
                        "native_message_id": row.get("entry_id"),
                        "source_offset": row.get("source_offset"),
                        "marker_count": text.count(marker),
                    }
                )
            tool_calls = row.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for tool in tool_calls:
                if not isinstance(tool, Mapping) or not tool.get("id"):
                    continue
                call_id = str(tool["id"])
                calls[call_id] = {
                    "id": call_id,
                    "name": tool.get("name"),
                    "arguments": tool.get("arguments"),
                    "native_message_id": row.get("entry_id"),
                    "source_offset": row.get("source_offset"),
                }
        elif row.get("type") == "tool_result" and row.get("tool_call_id"):
            call_id = str(row["tool_call_id"])
            native_message_id = row.get("entry_id")
            message = raw_messages.get(str(native_message_id))
            results[call_id] = {
                "id": native_message_id,
                "tool_call_id": call_id,
                "name": row.get("tool_name"),
                "result": message.get("content") if isinstance(message, Mapping) and "content" in message else row.get("text"),
                "is_error": row.get("is_error"),
                "native_message_id": native_message_id,
                "source_offset": row.get("source_offset"),
            }

    pairs: list[tuple[dict[str, object], dict[str, object], dict[str, object]]] = []
    for call_id, call in calls.items():
        result = results.get(call_id)
        if result is None:
            continue
        call_offset = call.get("source_offset")
        result_offset = result.get("source_offset")
        if not isinstance(call_offset, int) or not isinstance(result_offset, int) or result_offset <= call_offset:
            continue
        if (
            not isinstance(call.get("name"), str)
            or not call["name"]
            or "arguments" not in call
            or call.get("arguments") is None
            or not isinstance(result.get("id"), str)
            or not result["id"]
            or result.get("name") != call.get("name")
            or result.get("tool_call_id") != call_id
        ):
            continue
        for response in marker_responses:
            response_offset = response.get("source_offset")
            if isinstance(response_offset, int) and response_offset > result_offset:
                pairs.append((call, result, response))
                break

    return {
        "provider_session_id": provider_session_id,
        "native_model": native_models[-1] if native_models else metadata.get("model"),
        "native_provider": native_providers[-1] if native_providers else None,
        "header_present": metadata.get("has_header") is True,
        "calls": calls,
        "results": results,
        "marker_responses": marker_responses,
        "pair": pairs[0] if pairs else None,
    }


def _pi_native_tool_receipt(
    *,
    native_source: Path | None,
    provider_response_source: Path | None,
    inspection: Mapping[str, object] | None,
    dispatch: Mapping[str, object],
    binding: Mapping[str, object],
    binary_receipt: Mapping[str, object],
    cleanup: Mapping[str, object],
    marker: str,
    native_retained_path: str | None = None,
    provider_response_retained_path: str | None = None,
) -> dict[str, object]:
    """Bind retained native call/result evidence to the live Console turn."""
    receipt: dict[str, object] = {
        "schema_version": 1,
        "artifact_kind": "pi_native_tool_receipt",
        "provider": "pi",
        "status": "fail",
        "provider_binary": dict(binary_receipt),
        "session_id": dispatch.get("session_id"),
        "thread_id": dispatch.get("thread_id"),
        "run_id": dispatch.get("run_id"),
        "provider_thread_id": binding.get("provider_thread_id"),
        "provider_response": {
            "marker": marker,
            "source_kind": binding.get("provider_response_source_kind"),
            "source_sha256": binding.get("provider_response_source_sha256"),
            "retained_source_path": str(provider_response_source) if provider_response_source is not None else None,
            "retained_path": provider_response_retained_path,
            "bound_assistant_event_id": binding.get("bound_assistant_event_id"),
            "bound_assistant_event_origin": binding.get("bound_assistant_event_origin"),
        },
        "linkage": {},
        "failure_reasons": [],
    }
    failures: list[str] = []
    if native_source is None:
        failures.append("retained_native_source_missing")
        receipt["native_source"] = None
        native: dict[str, object] = {}
    else:
        try:
            receipt["native_source"] = {
                "path": str(native_source),
                "retained_path": native_retained_path,
                "sha256": _sha256_file(native_source),
                "kind": "provider_native_session_jsonl",
            }
            native = _pi_native_tool_observation(native_source, marker)
        except (OSError, ValueError, RuntimeError) as exc:
            native = {"error": f"{type(exc).__name__}: {exc}"}
            failures.append("retained_native_source_unreadable")

    pair = native.get("pair")
    if isinstance(pair, tuple) and len(pair) == 3:
        call, result, response = pair
        receipt["tool_call"] = call
        receipt["tool_result"] = result
        provider_response = receipt["provider_response"]
        if isinstance(provider_response, dict):
            provider_response["native_message_id"] = response.get("native_message_id")
            provider_response["projected_assistant_event_id"] = binding.get("bound_assistant_event_id")
            provider_response["native_marker_count"] = response.get("marker_count")
    else:
        receipt["tool_call"] = None
        receipt["tool_result"] = None
        failures.append("native_tool_call_result_pair_missing")

    provider_session_id = native.get("provider_session_id")
    provider_response = receipt["provider_response"]
    if isinstance(provider_response, dict):
        provider_response["native_source_path"] = str(native_source) if native_source is not None else None
        provider_response["native_retained_path"] = native_retained_path
        provider_response["native_provider_session_id"] = provider_session_id
    native_session_matches = (
        isinstance(provider_session_id, str)
        and bool(provider_session_id)
        and provider_session_id == dispatch.get("provider_thread_id")
        and provider_session_id == binding.get("provider_thread_id")
        and native.get("header_present") is True
    )
    if not native_session_matches:
        failures.append("native_session_provider_response_mismatch")

    inspection_ok = (
        inspection is not None
        and bool(inspection.get("linked_tool_call_ids"))
        and inspection.get("output_marker_observed") is True
        and bool(inspection.get("native_shapes"))
    )
    if not inspection_ok:
        failures.append("live_pi_tool_inspection_incomplete")
    provider_response_marker_count = binding.get("provider_response_marker_count")
    native_message_id = provider_response.get("native_message_id") if isinstance(provider_response, dict) else None
    projected_assistant_event_id = provider_response.get("projected_assistant_event_id") if isinstance(provider_response, dict) else None
    native_projected_linkage = (
        isinstance(provider_response, dict)
        and isinstance(native_message_id, str)
        and bool(native_message_id)
        and isinstance(projected_assistant_event_id, (str, int))
        and not isinstance(projected_assistant_event_id, bool)
        and bool(projected_assistant_event_id)
        and projected_assistant_event_id == binding.get("bound_assistant_event_id")
        and isinstance(pair, tuple)
        and len(pair) == 3
        and pair[2].get("marker_count") == 1
    )
    if not native_projected_linkage:
        failures.append("native_projected_assistant_linkage_missing")
    provider_response_ok = (
        binding.get("status") == "pass"
        and binding.get("marker") == marker
        and isinstance(provider_response_marker_count, int)
        and not isinstance(provider_response_marker_count, bool)
        and provider_response_marker_count >= 1
        and binding.get("bound_assistant_event_id") is not None
        and isinstance(pair, tuple)
        and len(pair) == 3
        and pair[2].get("marker_count") == 1
        and len(native.get("marker_responses") or []) == 1
    )
    if not provider_response_ok:
        failures.append("provider_response_not_bound_to_native_session")
    cleanup_ok = (
        cleanup.get("status") == "pass"
        and cleanup.get("provider_process_dead") is True
        and cleanup.get("process_group_dead") is True
        and cleanup.get("orphan_count") == 0
        and cleanup.get("process_stop_verified") is True
    )
    if not cleanup_ok:
        failures.append("cleanup_failed")
    runtime_identity_ok = all(
        dispatch.get(key) == binding.get(key) for key in ("provider", "session_id", "thread_id", "run_id", "prompt_digest")
    )
    if not runtime_identity_ok:
        failures.append("provider_response_runtime_identity_mismatch")

    receipt["provider_session_id"] = provider_session_id
    receipt["native_model"] = native.get("native_model")
    receipt["native_provider"] = native.get("native_provider")
    receipt["linkage"] = {
        "live_inspection_confirmed": inspection_ok,
        "native_session_matches_provider_thread": native_session_matches,
        "native_response_follows_tool_result": isinstance(pair, tuple) and len(pair) == 3,
        "provider_response_bound": provider_response_ok,
        "native_projected_assistant_linkage": native_projected_linkage,
        "native_message_id_preserved": bool(
            isinstance(provider_response.get("native_message_id"), str) if isinstance(provider_response, dict) else False
        ),
        "projected_assistant_event_id_preserved": (
            isinstance(provider_response.get("projected_assistant_event_id"), (str, int))
            and not isinstance(provider_response.get("projected_assistant_event_id"), bool)
            and provider_response.get("projected_assistant_event_id") == binding.get("bound_assistant_event_id")
        ),
        "runtime_identity_matches": runtime_identity_ok,
        "cleanup_pass": cleanup_ok,
    }
    receipt["failure_reasons"] = failures
    receipt["status"] = "pass" if not failures else "fail"
    return receipt


def _retain_flush_diagnostics(receipt: Mapping[str, object]) -> dict[str, object]:
    retained = dict(receipt)
    raw_path = retained.pop("log_path", None)
    if not isinstance(raw_path, str) or not raw_path:
        return retained
    try:
        content = Path(raw_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        retained["log_retained"] = False
        return retained
    retained.update(
        {
            "log_retained": True,
            "log_size_bytes": len(content.encode("utf-8")),
            "log_sha256": _sha256_bytes(content.encode("utf-8")),
            "log_tail": content[-2048:],
        }
    )
    return retained


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


def _process_group_dead(pgid: object) -> bool:
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _wait_owned_processes_dead(claims: list[dict[str, Any]], timeout: float = 15) -> bool:
    if not claims or not any(
        isinstance(claim.get("pid"), int)
        and not isinstance(claim.get("pid"), bool)
        and claim.get("pid", 0) > 0
        and isinstance(claim.get("process_group_id"), int)
        and not isinstance(claim.get("process_group_id"), bool)
        and claim.get("process_group_id", 0) > 0
        for claim in claims
    ):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(_pid_dead(claim.get("pid")) and _process_group_dead(claim.get("process_group_id")) for claim in claims):
            return True
        time.sleep(0.1)
    return all(_pid_dead(claim.get("pid")) and _process_group_dead(claim.get("process_group_id")) for claim in claims)


def _owned_process_evidence(claims: list[dict[str, Any]]) -> list[dict[str, object]]:
    """Retain exact provider owners and their independently observed cleanup state."""

    return [
        {
            "pid": claim.get("pid"),
            "process_group_id": claim.get("process_group_id"),
            "boot_id": claim.get("boot_id"),
            "process_start_time": claim.get("process_start_time"),
            "run_id": claim.get("run_id"),
            "turn_id": claim.get("turn_id"),
            "state": claim.get("state"),
            "pid_positive": isinstance(claim.get("pid"), int) and not isinstance(claim.get("pid"), bool) and claim["pid"] > 0,
            "process_group_positive": (
                isinstance(claim.get("process_group_id"), int)
                and not isinstance(claim.get("process_group_id"), bool)
                and claim["process_group_id"] > 0
            ),
            "birth_identity_present": (
                isinstance(claim.get("boot_id"), str)
                and bool(claim["boot_id"])
                and isinstance(claim.get("process_start_time"), str)
                and bool(claim["process_start_time"])
            ),
            "pid_dead": _pid_dead(claim.get("pid")),
            "process_group_dead": _process_group_dead(claim.get("process_group_id")),
        }
        for claim in claims
    ]


def _retained_post_interrupt_output_evidence(
    provider: str,
    claim: Mapping[str, Any],
    marker: str,
    retained_sources: list[dict[str, object]],
    root: Path,
) -> dict[str, object]:
    """Bind the recovery marker and terminal event to retained provider output."""

    if not _interrupt_output_contract_applies(provider):
        return {
            "valid": None,
            "applicable": False,
            "provider": provider,
            "marker": marker,
            "assistant_marker_count": 0,
            "terminal_event_count": 0,
        }

    for field in ("stdout_path", "source_path"):
        source_path = claim.get(field)
        if not isinstance(source_path, str) or not source_path:
            continue
        retained = next(
            (
                item
                for item in retained_sources
                if item.get("retained") is True and item.get("source") == source_path and item.get("kind") == field
            ),
            None,
        )
        retained_path = retained.get("path") if isinstance(retained, Mapping) else None
        if not isinstance(retained_path, str):
            continue
        try:
            lines = (root / retained_path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        events: list[Mapping[str, Any]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, Mapping):
                events.append(value)
        marker_indices: list[int] = []
        terminal_indices: list[int] = []
        for index, event in enumerate(events):
            output_text = "\n".join(_assistant_output_texts(provider, json.dumps(event, sort_keys=True)))
            if output_text.count(marker) == 1:
                marker_indices.append(index)
            if event.get("type") == "agent_end":
                is_terminal = (
                    omp_agent_end_is_terminal(event)
                    if provider == "omp"
                    else event.get("isTerminal")
                    if isinstance(event.get("isTerminal"), bool)
                    else event.get("willContinue") is False
                )
            else:
                is_terminal = False
            if is_terminal is True:
                terminal_indices.append(index)
        return {
            "valid": len(marker_indices) == 1 and bool(terminal_indices) and any(index > marker_indices[0] for index in terminal_indices),
            "applicable": True,
            "provider": provider,
            "source_kind": field,
            "source_path": source_path,
            "retained_path": retained_path,
            "marker": marker,
            "assistant_marker_count": len(marker_indices),
            "marker_event_index": marker_indices[0] if len(marker_indices) == 1 else None,
            "marker_event_type": events[marker_indices[0]].get("type") if len(marker_indices) == 1 else None,
            "terminal_event_count": len(terminal_indices),
            "terminal_event_index": next(
                (index for index in terminal_indices if marker_indices and index > marker_indices[0]),
                None,
            ),
            "terminal_event_type": "agent_end" if terminal_indices else None,
        }
    return {
        "valid": False,
        "applicable": True,
        "provider": provider,
        "marker": marker,
        "assistant_marker_count": 0,
        "terminal_event_count": 0,
    }


def _console_cleanup_receipt(
    claims: list[dict[str, Any]],
    retained_sources: list[dict[str, object]],
    *,
    process_stop_wait_completed: bool | None = None,
    shipper_stop: Mapping[str, object] | None = None,
    served_run_inventory: Mapping[str, object] | None = None,
    session_retirement: Mapping[str, object] | None = None,
    expected_session_id: str | None = None,
    run_failed: bool = False,
) -> dict[str, object]:
    provider_process_dead = bool(claims) and all(_pid_dead(claim.get("pid")) for claim in claims)
    process_group_dead = bool(claims) and all(_process_group_dead(claim.get("process_group_id")) for claim in claims)
    orphan_count = sum(not (_pid_dead(claim.get("pid")) and _process_group_dead(claim.get("process_group_id"))) for claim in claims)
    process_stop_verified = provider_process_dead and process_group_dead and orphan_count == 0 and process_stop_wait_completed is not False
    source_retention_verified = bool(retained_sources) and all(
        item.get("retained") is True
        and item.get("complete") is True
        and bool(item.get("original_sha256"))
        and bool(item.get("retained_sha256"))
        and isinstance(item.get("path"), str)
        and bool(item["path"])
        and bool(item.get("identities"))
        and all(
            all(identity.get(field) for field in ("provider", "session_id", "thread_id", "run_id"))
            and (expected_session_id is None or identity["session_id"] == expected_session_id)
            for identity in item["identities"]
        )
        for item in retained_sources
    )
    shipper_stop_verified = (
        isinstance(shipper_stop, Mapping)
        and shipper_stop.get("stopped") is True
        and shipper_stop.get("process_dead") is True
        and shipper_stop.get("process_group_dead") is True
    )
    served_run_retired = (
        isinstance(served_run_inventory, Mapping)
        and (expected_session_id is None or served_run_inventory.get("session_id") == expected_session_id)
        and served_run_inventory.get("retired") is True
        and served_run_inventory.get("active_run_count") == 0
    )
    canary_session_hidden = (
        isinstance(session_retirement, Mapping)
        and session_retirement.get("status") == "pass"
        and session_retirement.get("session_id") == (served_run_inventory or {}).get("session_id")
        and session_retirement.get("hidden") is True
        and session_retirement.get("archived") is True
        and session_retirement.get("present_in_served_inventory") is False
    )
    cleanup_pass = (
        not run_failed
        and process_stop_verified
        and source_retention_verified
        and shipper_stop_verified
        and served_run_retired
        and canary_session_hidden
    )
    return {
        "status": "pass" if cleanup_pass else "fail",
        "provider_process_dead": provider_process_dead,
        "process_group_dead": process_group_dead,
        "orphan_count": orphan_count,
        "process_stop_verified": process_stop_verified,
        "process_stop": {
            "verified": process_stop_verified,
            "provider_process_dead": provider_process_dead,
            "process_group_dead": process_group_dead,
            "orphan_count": orphan_count,
            "owned_process_count": len(claims),
            "wait_completed": process_stop_wait_completed,
        },
        "owned_processes": _owned_process_evidence(claims),
        "owned_process_count": len(claims),
        "source_retention_verified": source_retention_verified,
        "source_retention": {
            "verified": source_retention_verified,
            "source_count": len(retained_sources),
            "retained_source_count": sum(item.get("retained") is True for item in retained_sources),
            "sources": retained_sources,
        },
        "shipper_stop": dict(shipper_stop) if isinstance(shipper_stop, Mapping) else None,
        "shipper_stop_verified": shipper_stop_verified,
        "served_run_inventory": dict(served_run_inventory) if isinstance(served_run_inventory, Mapping) else None,
        "served_run_retired": served_run_retired,
        "session_retirement": dict(session_retirement) if isinstance(session_retirement, Mapping) else None,
        "canary_session_hidden": canary_session_hidden,
        "run_failed": run_failed,
    }


def _served_run_terminal_evidence(
    api_url: str,
    token: str,
    session_id: str,
    run_id: str,
    *,
    timeout: float = 30,
) -> dict[str, object]:
    """Read canonical terminal facts for one exact served run."""

    try:
        diagnostic = _request(api_url, token, "GET", f"/api/agents/sessions/{session_id}/state-diagnostics", timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - cleanup evidence must fail closed
        return {
            "retired": False,
            "session_id": session_id,
            "expected_run_id": run_id,
            "error": f"{type(exc).__name__}: {exc}",
        }
    shadow = diagnostic.get("shadow") if isinstance(diagnostic.get("shadow"), Mapping) else {}
    run = shadow.get("run") if isinstance(shadow, Mapping) and isinstance(shadow.get("run"), Mapping) else {}
    activity = shadow.get("activity") if isinstance(shadow, Mapping) and isinstance(shadow.get("activity"), Mapping) else {}
    terminal_state = str(run.get("lifecycle") or run.get("state") or "").lower()
    activity_state = str(activity.get("state") or "").lower()
    served_session_id = str(diagnostic.get("session_id") or "")
    served_run_id = str(run.get("id") or "")
    session_identity_match = served_session_id == session_id
    run_identity_match = bool(run_id) and served_run_id == run_id
    activity_compatible = activity_state in {"", "unknown", "quiescent", "idle", "finished"}
    retired = (
        diagnostic.get("served_path") == "canonical_session_detail"
        and session_identity_match
        and run_identity_match
        and terminal_state in {"completed", "ended", "failed", "cancelled", "terminal", "stopped"}
        and activity_compatible
    )
    return {
        "retired": retired,
        "session_id": session_id,
        "served_session_id": served_session_id,
        "expected_run_id": run_id or None,
        "served_run_id": served_run_id or None,
        "session_identity_match": session_identity_match,
        "run_identity_match": run_identity_match,
        "served_path": diagnostic.get("served_path"),
        "terminal_state": terminal_state or None,
        "activity_state": activity_state or None,
        "activity_state_authority": "diagnostic_head" if activity_state else "terminal_run_facts",
    }


def _served_run_inventory_evidence(
    api_url: str,
    token: str,
    session_id: str,
    claims: list[dict[str, Any]],
) -> dict[str, object]:
    """Prove the served run inventory retired the provider execution owner."""

    terminal_claims = [claim for claim in claims if claim.get("state") == "terminal"]
    claim_session_ids = [str(claim.get("session_id") or "").strip() for claim in claims]
    claim_run_ids = [str(claim.get("run_id") or "").strip() for claim in claims]
    expected_run_id = claim_run_ids[-1] if claim_run_ids and claim_run_ids[-1] else None
    terminal_evidence = _served_run_terminal_evidence(api_url, token, session_id, expected_run_id or "")
    terminal_state = str(terminal_evidence.get("terminal_state") or "")
    activity_state = str(terminal_evidence.get("activity_state") or "")
    served_session_id = str(terminal_evidence.get("served_session_id") or "")
    served_run_id = str(terminal_evidence.get("served_run_id") or "")
    session_identity_match = terminal_evidence.get("session_identity_match") is True
    run_identity_match = terminal_evidence.get("run_identity_match") is True
    terminal_run_proven = (
        len(terminal_claims) == len(claims)
        and bool(claims)
        and all(session_id == claim_session_id for claim_session_id in claim_session_ids)
        and len(claim_run_ids) == len(claims)
        and all(claim_run_ids)
        and terminal_evidence.get("served_path") == "canonical_session_detail"
        and session_identity_match
        and run_identity_match
        and terminal_state in {"completed", "ended", "failed", "cancelled", "terminal", "stopped"}
        and terminal_evidence.get("retired") is True
    )
    # Activity heads are expiring observations. A missing/unknown head must
    # not be converted into idle; terminal claim/run facts prove retirement
    # here, while an explicitly active head still disproves it.
    activity_compatible = activity_state in {"", "unknown", "quiescent", "idle", "finished"}
    retired = terminal_run_proven and activity_compatible
    return {
        "retired": retired,
        "active_run_count": 0 if retired else None,
        "session_id": session_id,
        "served_session_id": served_session_id,
        "expected_run_id": expected_run_id,
        "served_run_id": served_run_id or None,
        "session_identity_match": session_identity_match,
        "run_identity_match": run_identity_match,
        "served_path": terminal_evidence.get("served_path"),
        "terminal_state": terminal_state or None,
        "activity_state": activity_state or None,
        "activity_state_authority": "diagnostic_head" if activity_state else "terminal_run_facts",
        "run_ids": claim_run_ids,
        "terminal_evidence": terminal_evidence,
        **({"error": terminal_evidence["error"]} if terminal_evidence.get("error") else {}),
    }


def _terminate_live_qualification_session(
    api_url: str,
    token: str,
    session_id: str | None,
) -> dict[str, object]:
    """Dispatch terminal cleanup before killing a failed provider owner."""

    if not session_id:
        return {
            "status": "fail",
            "dispatched": False,
            "error": "session_id_unavailable",
        }
    try:
        response = _request(
            api_url,
            token,
            "POST",
            f"/api/agents/sessions/{session_id}/terminate-live",
        )
    except Exception as exc:  # noqa: BLE001 - cleanup must continue and report failure
        return {
            "status": "fail",
            "dispatched": False,
            "session_id": session_id,
            "error": f"{type(exc).__name__}: {exc}",
        }
    dispatched = response.get("terminate_dispatched") is True
    return {
        "status": "pass" if dispatched else "fail",
        "dispatched": dispatched,
        "session_id": session_id,
        "response": response,
    }


def _wait_served_run_retirement(
    api_url: str,
    token: str,
    session_id: str,
    claims: list[dict[str, Any]],
    *,
    timeout: float = 30,
) -> dict[str, object]:
    """Wait for the terminal run fact to reach the served projection."""

    deadline = time.monotonic() + timeout
    attempts = 0
    last = _served_run_inventory_evidence(api_url, token, session_id, claims)
    while last.get("retired") is not True and time.monotonic() < deadline:
        attempts += 1
        time.sleep(0.5)
        last = _served_run_inventory_evidence(api_url, token, session_id, claims)
    last["retirement_wait_attempts"] = attempts
    last["retirement_wait_status"] = "pass" if last.get("retired") is True else "fail"
    return last


def _force_cleanup(claims: list[dict[str, Any]]) -> None:
    groups = {
        int(claim["process_group_id"])
        for claim in claims
        if isinstance(claim.get("process_group_id"), int) and int(claim["process_group_id"]) > 0
    }
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pgid in groups:
            if _process_group_dead(pgid):
                continue
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                pass
        if all(_process_group_dead(pgid) for pgid in groups):
            return
        time.sleep(1)


def _configure_claude_hook(args: argparse.Namespace, environment: dict[str, str]) -> None:
    provider_home = Path(environment["CLAUDE_CONFIG_DIR"])
    completed = subprocess.run(
        [str(args.longhouse_cli), "claude", "configure", "--claude-dir", str(provider_home)],
        cwd=Path(environment["HOME"]),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"staged Longhouse could not configure the isolated Claude profile: {completed.stderr[-1000:]}")


def _turn_identity_ok(claim: Mapping[str, object], *, provider: str, session_id: str, thread_id: str, run_id: str) -> bool:
    return (
        claim.get("provider") == provider
        and claim.get("session_id") == session_id
        and claim.get("thread_id") == thread_id
        and claim.get("run_id") == run_id
        and claim.get("adapter") == ADAPTERS[provider]
        and claim.get("provider_identity_confirmed") is True
    )


def _claim_uses_provider_binary(claim: Mapping[str, object], provider_binary: Path) -> bool:
    result = claim.get("result")
    argv = result.get("argv") if isinstance(result, Mapping) else None
    if not isinstance(argv, list) or not argv or not isinstance(argv[0], str):
        return False
    try:
        return Path(argv[0]).resolve(strict=True) == provider_binary.resolve(strict=True)
    except OSError:
        return False


def _native_model(provider: str, model: str) -> str:
    # Pi receives the provider separately and its native model argument is the
    # OpenRouter model id without the provider prefix. OpenCode's CLI expects
    # the fully qualified provider/model token instead.
    return f"openrouter/{model}" if provider == "opencode" and not model.startswith("openrouter/") else model


def _omp_continuation_prompt(context_marker: str, resume_marker: str) -> str:
    return (
        "New-turn continuation check. Without reading files, recall the exact value "
        f"stored under the label {OMP_CONTEXT_LABEL!r} in the earlier user message. "
        f"Reply with that value followed by exactly {resume_marker} and no other text."
    )


def _claim_uses_selected_model(claim: Mapping[str, object], *, provider: str, model: str) -> bool:
    result = claim.get("result")
    argv = result.get("argv") if isinstance(result, Mapping) else None
    if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
        return False
    expected = _native_model(provider, model)
    if provider == "codex":
        encoded = expected.replace("\\", "\\\\").replace('"', '\\"')
        return f'model="{encoded}"' in argv
    return any(flag == "--model" and index + 1 < len(argv) and argv[index + 1] == expected for index, flag in enumerate(argv))


def _retain_failure_claim_diagnostics(
    root: Path,
    claims: list[dict[str, Any]],
    environment: Mapping[str, str],
) -> None:
    secrets = [value for name, value in environment.items() if value and (name.endswith("_KEY") or name.endswith("_TOKEN"))]

    def redact(value: object) -> str | None:
        if value is None:
            return None
        text = str(value)
        for secret in secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    retained: list[dict[str, object]] = []
    for claim in claims:
        result = claim.get("result") if isinstance(claim.get("result"), Mapping) else {}
        streams: dict[str, object] = {}
        for key in ("stdout_path", "stderr_path", "source_path"):
            raw_path = claim.get(key)
            if not isinstance(raw_path, str) or not raw_path:
                continue
            try:
                content = Path(raw_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            tail = content[-4096:]
            for secret in secrets:
                tail = tail.replace(secret, "[REDACTED]")
            streams[key] = {
                "size_bytes": len(content.encode("utf-8")),
                "sha256": _sha256_bytes(content.encode("utf-8")),
                "tail": tail,
            }
        retained.append(
            {
                "state": claim.get("state"),
                "run_id": claim.get("run_id"),
                "terminal_state": result.get("terminal_state"),
                "error": redact(result.get("error") or claim.get("error")),
                "streams": streams,
            }
        )
    write_json(root / "provider-failure-diagnostics.json", {"claims": retained})


def _retain_claim_sources(
    root: Path,
    claims: list[dict[str, Any]],
    environment: Mapping[str, str],
    *,
    complete: bool = False,
) -> list[dict[str, object]]:
    """Verify retained provider bytes before the isolated HOME can disappear."""

    secrets = [value for name, value in environment.items() if value and (name.endswith("_KEY") or name.endswith("_TOKEN"))]
    target_root = root / "provider-sources"
    retained: list[dict[str, object]] = []
    seen: dict[tuple[str, str], list[dict[str, object]]] = {}
    for index, claim in enumerate(claims):
        run_id = str(claim.get("run_id") or index)
        identity = {
            name: claim.get(name) for name in ("provider", "session_id", "thread_id", "run_id", "provider_thread_id", "native_session_id")
        }
        for field in ("source_path", "stdout_path"):
            raw_path = claim.get(field)
            if not isinstance(raw_path, str) or not raw_path:
                continue
            key = (raw_path, field)
            if key in seen:
                seen[key].append(identity)
                continue
            identities = [identity]
            seen[key] = identities
            entry: dict[str, object] = {"source": raw_path, "kind": field, "identities": identities, "retained": False}
            source = Path(raw_path)
            try:
                content = source.read_bytes()
                original_digest = _sha256_bytes(content)
                for secret in secrets:
                    content = content.replace(secret.encode(), b"[REDACTED]")
                max_bytes = 16 * 1024 * 1024
                truncated = not complete and len(content) > max_bytes
                if truncated:
                    content = content[:max_bytes] + b"\n[truncated by QA evidence bound]\n"
                expected_digest = _sha256_bytes(content)
                target = target_root / f"{index}-{run_id}-{field}.raw"
                target_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.write_bytes(content)
                retained_digest = _sha256_file(target)
                verified = retained_digest == expected_digest and _sha256_file(source) == original_digest
                entry.update(
                    {
                        "path": target.relative_to(root).as_posix(),
                        "retained": verified,
                        "complete": not truncated,
                        "truncated": truncated,
                        "bytes": len(content),
                        "original_sha256": original_digest,
                        "retained_sha256": retained_digest,
                    }
                )
                if not verified:
                    entry["error"] = "source changed during retention or retained bytes failed verification"
            except OSError as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"
            retained.append(entry)
    write_json(root / "provider-source-retention.json", {"sources": retained})
    return retained


def console_lifecycle_assertions(observation: Mapping[str, object]) -> dict[str, bool]:
    return {ASSERTION_ID: all(observation.get(fact) is True for fact in OBSERVED_ACTIVITY)}


def _observation_from_receipts(
    *,
    dispatch: Mapping[str, object],
    binding: Mapping[str, object],
    interrupt: Mapping[str, object],
    cleanup: Mapping[str, object],
) -> dict[str, bool]:
    marker = binding.get("marker")
    provider_excerpt = binding.get("provider_response_excerpt")
    assistant_excerpt = binding.get("bound_assistant_event_excerpt")
    raw_response_bound = (
        isinstance(marker, str)
        and bool(marker)
        and isinstance(provider_excerpt, str)
        and marker in provider_excerpt
        and isinstance(binding.get("provider_response_marker_count"), int)
        and not isinstance(binding.get("provider_response_marker_count"), bool)
        and int(binding["provider_response_marker_count"]) >= 1
        and isinstance(assistant_excerpt, str)
        and marker in assistant_excerpt
        and binding.get("bound_assistant_marker_count") == 1
        and binding.get("bound_assistant_event_id") is not None
        and binding.get("bound_assistant_event_origin") == "durable"
    )
    exact_identity = all(
        binding.get(key) == dispatch.get(key) for key in ("provider", "session_id", "thread_id", "run_id", "prompt_digest")
    )
    return {
        "adapter_dispatch_started": dispatch.get("status") == "pass",
        "qualification_model_bound": dispatch.get("qualification_model_bound") is True,
        "stock_provider_response_bound": (
            binding.get("status") == "pass"
            and raw_response_bound
            and binding.get("marker_in_provider_response") is True
            and binding.get("marker_in_bound_assistant_event") is True
        ),
        "exact_session_thread_run_binding": exact_identity,
        "transcript_converged_exactly_once": (
            binding.get("assistant_event_count") == 1 and binding.get("transcript_converged_exactly_once") is True
        ),
        "interrupt_contract_preserved": interrupt.get("status") == "pass",
        "post_interrupt_sendable": (
            interrupt.get("post_interrupt_turn_completed") is True or interrupt.get("normal_turn_completed") is True
        ),
        "no_orphan_provider_processes": (
            cleanup.get("status") == "pass"
            and cleanup.get("provider_process_dead") is True
            and cleanup.get("process_group_dead") is True
            and cleanup.get("orphan_count") == 0
            and cleanup.get("process_stop_verified") is True
        ),
    }


def _run_live(provider: str, variant: str, args: argparse.Namespace, root: Path) -> dict[str, Any]:
    if variant != _expected_variant(provider):
        raise RuntimeError(f"{provider} requires variant={_expected_variant(provider)}")
    home = isolated_provider_home()
    environment = _provider_environment(provider, args, home)
    if provider == "codex":
        auth_receipt = login_with_api_key(
            args.provider_bin,
            api_key=str(environment.get("CODEX_API_KEY") or ""),
            environment=environment,
            cwd=home,
        )
        environment.pop("CODEX_API_KEY", None)
        environment.pop("OPENAI_API_KEY", None)
        write_json(root / "provider-auth-receipt.json", auth_receipt)
    if provider == "claude":
        _configure_claude_hook(args, environment)

    api_url = str(os.environ.get(RUNTIME_API_URL_ENV) or "").strip().rstrip("/")
    token = str(os.environ.get(RUNTIME_AGENTS_TOKEN_ENV) or "").strip()
    if not api_url or not token:
        raise RuntimeError(f"{RUNTIME_API_URL_ENV} and {RUNTIME_AGENTS_TOKEN_ENV} are required")
    observed_version, raw_version_output = _probe_version(provider, args.provider_bin)
    if observed_version != args.provider_version:
        raise RuntimeError(f"{provider} staged release version mismatch: expected {args.provider_version}, observed {observed_version}")
    binary_receipt = {
        "provider": provider,
        "path": str(args.provider_bin),
        "sha256": _sha256_file(args.provider_bin),
        "version": args.provider_version,
        "raw_version_output": raw_version_output,
    }
    write_json(root / "provider-binary-receipt.json", binary_receipt)

    # HOME is already unique to one mount-isolated qualification. Keep
    # ephemeral IPC paths compact: Linux Unix sockets cap the complete path
    # near 108 bytes, so semantic directory names plus another UUID make the
    # Console harness deployment-path dependent for no isolation benefit.
    runtime_root, _ephemeral_engine_evidence, workspace, longhouse_home = _console_runtime_paths(home)
    # Retain the Machine Agent's own logs with a failed qualification. The
    # provider HOME is deliberately destroyed by the sandbox, so placing these
    # under it made the only process that owned a corrupt shipper DB disappear
    # before the failure could be diagnosed.
    engine_evidence = root / "shipper"
    engine_evidence.mkdir(mode=0o700, parents=True)
    workspace.mkdir(mode=0o700, parents=True)
    tool_marker = f"{provider.upper()}_CONSOLE_TOOL_{uuid4().hex}"
    proof_path: Path | None = None
    if provider in {"pi", "omp"}:
        proof_path = workspace / f"{provider}-console-proof.txt"
        proof_path.write_text(tool_marker + "\n", encoding="utf-8")
    if provider == "cursor":
        completed = subprocess.run(
            ["git", "init", "--quiet"],
            cwd=workspace,
            env={**environment, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("Cursor Console qualification workspace could not initialize")

    args.api_url = api_url
    args.agents_token = token
    claims: list[dict[str, Any]] = []
    retained_sources: list[dict[str, object]] = []
    shipper: TranscriptShipper | None = None
    session_id: str | None = None
    cleanup_written = False
    continuation_context_recalled = provider not in {"pi", "omp"}
    try:
        shipper = start_transcript_shipper(
            provider,
            args,
            home=home,
            environment=environment,
            evidence_root=engine_evidence,
            longhouse_home=longhouse_home,
        )
        longhouse_home = Path(environment["LONGHOUSE_HOME"])
        device_id = str(shipper.receipt["machine_name"])
        created = _create_session(
            api_url=api_url,
            token=token,
            provider=provider,
            device_id=device_id,
            cwd=workspace,
            model=args.model,
        )
        session_id = str(created["session_id"])
        thread_id = str(created["thread_id"])
        marker = f"LH_{provider.upper()}_CONSOLE_{uuid4().hex}"
        context_marker = f"LH_{provider.upper()}_CONTEXT_{uuid4().hex}"
        if proof_path is not None:
            # Let the native read result supply the exact response token. Long
            # random tokens are intentionally strict evidence, but asking a
            # model to transcribe one from the prompt makes the canary flaky.
            proof_path.write_text(f"{tool_marker}\n{marker}\n", encoding="utf-8")
        message = f"Reply with exactly {marker} and nothing else."
        if provider in {"pi", "omp"}:
            message = (
                f"New machine-check request. Store the exact value {context_marker!r} "
                f"under the label {OMP_CONTEXT_LABEL!r}. Use the read tool to read {proof_path}. "
                f"After the tool returns, reply with the stored context phrase followed by {marker} and no other text."
            )
        request_id = f"console-release-{uuid4()}"
        first = _start_turn(
            api_url=api_url,
            token=token,
            session_id=session_id,
            message=message,
            request_id=request_id,
        )
        replay = _start_turn(
            api_url=api_url,
            token=token,
            session_id=session_id,
            message=message,
            request_id=request_id,
        )
        if replay.get("run_id") != first.get("run_id"):
            raise RuntimeError("Console request replay changed the stable run_id")
        run_id = str(first["run_id"])
        first_claim = _wait_claim(
            _claim_path(longhouse_home, run_id),
            states=frozenset({"terminal", "failed"}),
        )
        claims.append(first_claim)
        if first_claim.get("state") != "terminal" or (first_claim.get("result") or {}).get("terminal_state") != "run_completed":
            raise RuntimeError(f"first Console turn did not complete: {first_claim}")
        if not _turn_identity_ok(
            first_claim,
            provider=provider,
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
        ):
            raise RuntimeError("adapter claim did not preserve exact Console identity")
        if not _claim_uses_provider_binary(first_claim, args.provider_bin):
            raise RuntimeError("Console adapter did not launch the exact staged provider binary")
        if not _claim_uses_selected_model(first_claim, provider=provider, model=args.model):
            raise RuntimeError("Console adapter did not bind the selected qualification model")
        _wait_turn_terminal(
            api_url=api_url,
            token=token,
            session_id=session_id,
            message=message,
            request_id=request_id,
            turn_id=str(first["turn_id"]),
            run_id=run_id,
        )
        provider_response_evidence = _claim_output_evidence(provider, first_claim, marker)
        pi_tool_evidence = _pi_tool_evidence(first_claim, tool_marker) if provider == "pi" else None
        first_native_source_path: Path | None = None
        first_native_source_size: int | None = None
        second_native_source_size: int | None = None
        if provider in {"pi", "omp"}:
            raw_first_native_source = first_claim.get("source_path")
            if not isinstance(raw_first_native_source, str) or not raw_first_native_source:
                raise RuntimeError(f"first Console {provider} turn has no retained native source boundary")
            first_native_source_path = Path(raw_first_native_source)
            first_native_source_size = len(first_native_source_path.read_bytes())
        first_native_response = (
            _native_marker_evidence(provider, first_native_source_path, marker)
            if provider in {"pi", "omp"} and first_native_source_path is not None
            else None
        )
        if provider in {"pi", "omp"} and first_native_response is None:
            raise RuntimeError(f"first Console {provider} turn has no unique native assistant marker message")
        dispatch = {
            "status": "pass",
            "provider": provider,
            "adapter": first_claim.get("adapter"),
            "provider_executable_sha256": binary_receipt["sha256"],
            "session_id": session_id,
            "thread_id": thread_id,
            "run_id": run_id,
            "turn_id": first_claim.get("turn_id"),
            "client_request_id": request_id,
            "prompt_digest": _sha256_bytes(message.encode()),
            "stable_run_id_on_retry": True,
            "provider_thread_id": first_claim.get("provider_thread_id"),
            "qualification_model": args.model,
            "native_model": _native_model(provider, args.model),
            "native_provider": "openrouter" if provider == "pi" else None,
            "qualification_model_bound": True,
            "argv": (first_claim.get("result") or {}).get("argv"),
        }
        write_json(root / "adapter-dispatch-receipt.json", dispatch)
        flush_receipt = _retain_flush_diagnostics(shipper.flush("console-first-turn"))
        write_json(root / "transcript-flush-receipt.json", flush_receipt)

        def flush_for_projection(label: str) -> dict[str, object]:
            if shipper is None:
                raise RuntimeError(f"Console transcript shipper unavailable for {label}")
            receipt = _retain_flush_diagnostics(shipper.flush(label))
            turn_receipts = flush_receipt.setdefault("turns", {})
            if not isinstance(turn_receipts, dict):
                raise RuntimeError("Console transcript flush receipt has an invalid turns field")
            turn_receipts[label] = receipt
            write_json(root / "transcript-flush-receipt.json", flush_receipt)
            flush_ok = (
                receipt.get("status") == "pass"
                and receipt.get("exit_code") == 0
                and receipt.get("daemon_paused") is True
                and receipt.get("daemon_restarted") is True
                and isinstance(receipt.get("events_shipped"), int)
                and not isinstance(receipt.get("events_shipped"), bool)
                and receipt.get("events_shipped") >= 0
            )
            if not flush_ok:
                raise RuntimeError(f"Console transcript flush failed before {label} projection")
            return receipt

        flush_receipt["turns"] = {"console-first-turn": flush_receipt.copy()}
        write_json(root / "transcript-flush-receipt.json", flush_receipt)
        marker_count = provider_response_evidence.get("provider_response_marker_count") if provider_response_evidence is not None else None
        flush_ok = (
            flush_receipt.get("status") == "pass"
            and flush_receipt.get("exit_code") == 0
            and flush_receipt.get("daemon_paused") is True
            and flush_receipt.get("daemon_restarted") is True
            and isinstance(flush_receipt.get("events_shipped"), int)
            and not isinstance(flush_receipt.get("events_shipped"), bool)
            and flush_receipt.get("events_shipped") >= 0
        )
        boundary_receipt = {
            "status": "pass" if flush_ok and isinstance(marker_count, int) and marker_count >= 1 else "fail",
            "provider": provider,
            "session_id": session_id,
            "thread_id": thread_id,
            "run_id": run_id,
            "claim_state": first_claim.get("state"),
            "claim_terminal_state": (first_claim.get("result") or {}).get("terminal_state"),
            "assistant_output_source_kind": (
                provider_response_evidence.get("provider_response_source_kind") if provider_response_evidence is not None else None
            ),
            "assistant_output_marker_count": marker_count,
            "transcript_flush_status": flush_receipt.get("status"),
            "transcript_flush_events_shipped": flush_receipt.get("events_shipped"),
        }
        write_json(root / "console-boundary-receipt.json", boundary_receipt)
        if not flush_ok:
            raise RuntimeError("Console transcript flush failed before durable convergence")
        if provider_response_evidence is None:
            raise RuntimeError("stock provider output source was unavailable")
        if not isinstance(marker_count, int) or isinstance(marker_count, bool) or marker_count < 1:
            raise RuntimeError("stock provider assistant output did not contain the qualification marker")
        first_events = _wait_exact_assistant_marker(api_url, token, session_id, marker)
        bound_assistant_event = {
            **first_events[0],
            "tool_name_present": bool(first_events[0].get("tool_name")),
        }
        binding = {
            "status": "pass",
            "provider": provider,
            "session_id": session_id,
            "thread_id": thread_id,
            "run_id": run_id,
            "prompt_digest": dispatch["prompt_digest"],
            "provider_thread_id": first_claim.get("provider_thread_id"),
            "marker": marker,
            "tool_marker": tool_marker,
            **provider_response_evidence,
            "bound_assistant_event_id": bound_assistant_event.get("id"),
            "bound_assistant_event_origin": bound_assistant_event.get("event_origin", "durable"),
            "bound_assistant_event_excerpt": event_text(bound_assistant_event)[:512],
            "bound_assistant_event": bound_assistant_event,
            "bound_assistant_event_count": len(first_events),
            "bound_assistant_marker_count": event_text(bound_assistant_event).count(marker),
            "marker_in_provider_response": True,
            "marker_in_bound_assistant_event": True,
            "assistant_event_count": len(first_events),
            "transcript_converged_exactly_once": len(first_events) == 1,
        }
        if pi_tool_evidence is not None:
            binding["native_tool_evidence"] = pi_tool_evidence
        write_json(root / "provider-response-binding-receipt.json", binding)

        if provider in CAN_RESUME:
            resume_marker = f"LH_{provider.upper()}_RESUME_{uuid4().hex}"
            resume_message = f"Reply with exactly {resume_marker} and nothing else."
            if provider in {"pi", "omp"}:
                resume_message = _omp_continuation_prompt(context_marker, resume_marker)
            resume_request_id = f"console-resume-{uuid4()}"
            resume = _start_turn(
                api_url=api_url,
                token=token,
                session_id=session_id,
                message=resume_message,
                request_id=resume_request_id,
            )
            resume_claim = _wait_claim(
                _claim_path(longhouse_home, str(resume["run_id"])),
                states=frozenset({"terminal", "failed"}),
            )
            claims.append(resume_claim)
            _wait_turn_terminal(
                api_url=api_url,
                token=token,
                session_id=session_id,
                message=resume_message,
                request_id=resume_request_id,
                turn_id=str(resume["turn_id"]),
                run_id=str(resume["run_id"]),
            )
            flush_for_projection("console-resume-turn")
            resume_events = _wait_exact_assistant_marker(api_url, token, session_id, resume_marker)
            resume_native_response: dict[str, object] | None = None
            if provider in {"pi", "omp"}:
                raw_resume_source = resume_claim.get("source_path") or resume_claim.get("stdout_path")
                if not isinstance(raw_resume_source, str) or not first_native_source_size:
                    raise RuntimeError(f"second Console {provider} turn has no native source boundary")
                second_native_source_size = len(Path(raw_resume_source).read_bytes())
                if second_native_source_size <= first_native_source_size:
                    raise RuntimeError(f"second Console {provider} turn has no complete native source boundary")
                resume_native_response = _native_marker_evidence(
                    provider,
                    Path(raw_resume_source),
                    resume_marker,
                    minimum_source_offset=first_native_source_size,
                    maximum_source_offset=second_native_source_size,
                )
                if resume_native_response is None:
                    raise RuntimeError(f"second Console {provider} marker escaped its pre-interrupt native boundary")
            resume_context_marker_count = (
                event_text(resume_events[0]).count(context_marker) if provider in {"pi", "omp"} and resume_events else None
            )
            continuation_context_recalled = provider not in {"pi", "omp"} or resume_context_marker_count == 1
            if first_claim.get("provider_thread_id") is None or resume_claim.get("provider_thread_id") != first_claim.get(
                "provider_thread_id"
            ):
                raise RuntimeError("second Console turn did not preserve the native provider thread")
            continuation_linkage = (
                _pi_continuation_linkage(
                    resume_native_response,
                    resume_events[0].get("id") if resume_events else None,
                    projected_marker_count=event_text(resume_events[0]).count(resume_marker) if resume_events else 0,
                    same_session=resume_claim.get("session_id") == session_id,
                    same_thread=resume_claim.get("thread_id") == thread_id,
                    native_provider_thread_id=resume_claim.get("provider_thread_id"),
                    projected_provider_thread_id=first_claim.get("provider_thread_id"),
                )
                if provider in {"pi", "omp"}
                else None
            )
            if provider in {"pi", "omp"} and continuation_linkage is not None and continuation_linkage.get("proven") is not True:
                raise RuntimeError(f"Console {provider} continuation did not prove native/projected assistant linkage")
            dispatch["resume_run_id"] = resume.get("run_id")
            dispatch["native_thread_resumed"] = True
            write_json(root / "adapter-dispatch-receipt.json", dispatch)
            continuation_receipt = {
                "status": "pass" if continuation_context_recalled else "fail",
                "failure_code": None if continuation_context_recalled else "context_not_recalled",
                "context_recalled": continuation_context_recalled,
                "provider": provider,
                "session_id": session_id,
                "thread_id": thread_id,
                "first_process": {
                    "pid": first_claim.get("pid"),
                    "process_group_id": first_claim.get("process_group_id"),
                    "run_id": first_claim.get("run_id"),
                    "turn_id": first_claim.get("turn_id"),
                    "provider_thread_id": first_claim.get("provider_thread_id"),
                    "boot_id": first_claim.get("boot_id"),
                    "process_start_time": first_claim.get("process_start_time"),
                },
                "second_process": {
                    "pid": resume_claim.get("pid"),
                    "process_group_id": resume_claim.get("process_group_id"),
                    "run_id": resume_claim.get("run_id"),
                    "turn_id": resume_claim.get("turn_id"),
                    "provider_thread_id": resume_claim.get("provider_thread_id"),
                    "boot_id": resume_claim.get("boot_id"),
                    "process_start_time": resume_claim.get("process_start_time"),
                },
                "context": {
                    "seed_marker": context_marker,
                    "first_prompt": message,
                    "first_prompt_digest": _sha256_bytes(message.encode()),
                    "prompt": resume_message,
                    "prompt_digest": _sha256_bytes(resume_message.encode()),
                    "marker": resume_marker,
                },
                "response": {
                    "projected_assistant_event_id": resume_events[0].get("id") if resume_events else None,
                    "native_message_id": resume_native_response.get("native_message_id") if resume_native_response else None,
                    "marker_count": event_text(resume_events[0]).count(resume_marker) if resume_events else 0,
                    "context_marker_count": resume_context_marker_count,
                    "context_marker_exactly_once": resume_context_marker_count == 1,
                    "event_count": len(resume_events),
                    "linkage": continuation_linkage,
                    "native_source_end_offset_before_interrupt": second_native_source_size,
                    "native_source_start_offset_before_resume": first_native_source_size,
                },
                "same_session": resume_claim.get("session_id") == session_id,
                "same_thread": resume_claim.get("thread_id") == thread_id,
                "new_run": resume_claim.get("run_id") != first_claim.get("run_id"),
                "native_thread_resumed": resume_claim.get("provider_thread_id") == first_claim.get("provider_thread_id"),
            }
            if not continuation_receipt["new_run"]:
                raise RuntimeError("Console Pi continuation reused the first run identity")
            write_json(root / "console-continuation-receipt.json", continuation_receipt)

        interrupt_marker = f"LH_{provider.upper()}_INTERRUPT_{uuid4().hex}"
        interrupt_message = f"Use the shell tool to run `sleep 8`, then reply with exactly {interrupt_marker} and nothing else."
        interrupt_request_id = f"console-interrupt-{uuid4()}"
        interrupt_turn = _start_turn(
            api_url=api_url,
            token=token,
            session_id=session_id,
            message=interrupt_message,
            request_id=interrupt_request_id,
        )
        interrupt_claim_path = _claim_path(longhouse_home, str(interrupt_turn["run_id"]))
        active_claim = _wait_claim(
            interrupt_claim_path,
            states=frozenset({"spawned", "terminal", "failed"}),
            timeout=60,
        )
        claims.append(active_claim)
        if active_claim.get("state") != "spawned":
            raise RuntimeError("interrupt canary turn completed before its active contract could be tested")

        if variant == SUPPORTED_VARIANT:
            interrupted = _request(
                api_url,
                token,
                "POST",
                f"/api/agents/sessions/{session_id}/turns/current/interrupt",
            )
            terminal = _wait_claim(interrupt_claim_path, states=frozenset({"terminal", "failed"}), timeout=30)
            claims[-1] = terminal
            _wait_turn_terminal(
                api_url=api_url,
                token=token,
                session_id=session_id,
                message=interrupt_message,
                request_id=interrupt_request_id,
                turn_id=str(interrupt_turn["turn_id"]),
                run_id=str(interrupt_turn["run_id"]),
            )
            cancelled = (terminal.get("result") or {}).get("terminal_state") == "run_cancelled"
            process_dead = _wait_owned_processes_dead([terminal])
            post_marker = f"LH_{provider.upper()}_POST_INTERRUPT_{uuid4().hex}"
            post_message = f"Reply with exactly {post_marker} and nothing else."
            post_request_id = f"console-post-interrupt-{uuid4()}"
            post = _start_turn(
                api_url=api_url,
                token=token,
                session_id=session_id,
                message=post_message,
                request_id=post_request_id,
            )
            post_claim = _wait_claim(
                _claim_path(longhouse_home, str(post["run_id"])),
                states=frozenset({"terminal", "failed"}),
            )
            claims.append(post_claim)
            _wait_turn_terminal(
                api_url=api_url,
                token=token,
                session_id=session_id,
                message=post_message,
                request_id=post_request_id,
                turn_id=str(post["turn_id"]),
                run_id=str(post["run_id"]),
            )
            flush_for_projection("console-post-interrupt-turn")
            post_events = _wait_exact_assistant_marker(api_url, token, session_id, post_marker)
            post_completed = (post_claim.get("result") or {}).get("terminal_state") == "run_completed"
            interrupt_receipt = {
                "status": "pass"
                if interrupted.get("interrupt_dispatched") is True and cancelled and process_dead and post_completed
                else "fail",
                "expectation": "supported",
                "interrupt_dispatched": interrupted.get("interrupt_dispatched") is True,
                "active_run_cancelled": cancelled,
                "provider_process_dead": process_dead,
                "post_interrupt_turn_completed": post_completed,
                "interrupt_request_id": interrupt_request_id,
                "active_run": {
                    "provider": provider,
                    "session_id": active_claim.get("session_id"),
                    "thread_id": active_claim.get("thread_id"),
                    "run_id": active_claim.get("run_id"),
                    "turn_id": active_claim.get("turn_id"),
                    "provider_thread_id": active_claim.get("provider_thread_id"),
                    "boot_id": active_claim.get("boot_id"),
                    "process_start_time": active_claim.get("process_start_time"),
                },
                "active_terminal": {
                    "claim_state": terminal.get("state"),
                    "terminal_state": (terminal.get("result") or {}).get("terminal_state"),
                    "pid": active_claim.get("pid"),
                    "process_group_id": active_claim.get("process_group_id"),
                    "pid_positive": (
                        isinstance(active_claim.get("pid"), int)
                        and not isinstance(active_claim.get("pid"), bool)
                        and active_claim.get("pid", 0) > 0
                    ),
                    "process_group_positive": (
                        isinstance(active_claim.get("process_group_id"), int)
                        and not isinstance(active_claim.get("process_group_id"), bool)
                        and active_claim.get("process_group_id", 0) > 0
                    ),
                    "boot_id": active_claim.get("boot_id"),
                    "process_start_time": active_claim.get("process_start_time"),
                },
                "cancellation_claim": {
                    "claim_state": terminal.get("state"),
                    "run_id": terminal.get("run_id"),
                    "turn_id": terminal.get("turn_id"),
                    "terminal_state": (terminal.get("result") or {}).get("terminal_state"),
                },
                "post_interrupt_run": {
                    "provider": provider,
                    "session_id": post_claim.get("session_id"),
                    "thread_id": post_claim.get("thread_id"),
                    "pid": post_claim.get("pid"),
                    "process_group_id": post_claim.get("process_group_id"),
                    "boot_id": post_claim.get("boot_id"),
                    "process_start_time": post_claim.get("process_start_time"),
                    "run_id": post_claim.get("run_id"),
                    "turn_id": post_claim.get("turn_id"),
                    "provider_thread_id": post_claim.get("provider_thread_id"),
                    "terminal_state": (post_claim.get("result") or {}).get("terminal_state"),
                    "marker": post_marker,
                    "marker_observed": len(post_events) == 1 and event_text(post_events[0]).count(post_marker) == 1,
                },
            }
            interrupt_run_id = active_claim.get("run_id")
            post_interrupt_run_id = post_claim.get("run_id")
            if not (
                isinstance(interrupt_run_id, str)
                and interrupt_run_id
                and isinstance(post_interrupt_run_id, str)
                and post_interrupt_run_id
                and len(
                    {
                        str(first_claim.get("run_id")),
                        str(resume_claim.get("run_id")),
                        interrupt_run_id,
                        post_interrupt_run_id,
                    }
                )
                == 4
            ):
                raise RuntimeError("Console Pi lifecycle reused a first, resume, interrupt, or post-interrupt run identity")
        else:
            refused = False
            try:
                _request(
                    api_url,
                    token,
                    "POST",
                    f"/api/agents/sessions/{session_id}/turns/current/interrupt",
                )
            except RuntimeError as exc:
                refused = "adapter_unavailable" in str(exc) or "not supported" in str(exc) or "unsupported" in str(exc)
            terminal = _wait_claim(interrupt_claim_path, states=frozenset({"terminal", "failed"}), timeout=60)
            claims[-1] = terminal
            _wait_turn_terminal(
                api_url=api_url,
                token=token,
                session_id=session_id,
                message=interrupt_message,
                request_id=interrupt_request_id,
                turn_id=str(interrupt_turn["turn_id"]),
                run_id=str(interrupt_turn["run_id"]),
            )
            flush_for_projection("console-unsupported-interrupt-turn")
            _wait_exact_assistant_marker(api_url, token, session_id, interrupt_marker, timeout=60)
            post_marker = f"LH_{provider.upper()}_AFTER_UNSUPPORTED_{uuid4().hex}"
            post_message = f"Reply with exactly {post_marker} and nothing else."
            post_request_id = f"console-after-unsupported-{uuid4()}"
            post = _start_turn(
                api_url=api_url,
                token=token,
                session_id=session_id,
                message=post_message,
                request_id=post_request_id,
            )
            post_claim = _wait_claim(
                _claim_path(longhouse_home, str(post["run_id"])),
                states=frozenset({"terminal", "failed"}),
            )
            claims.append(post_claim)
            _wait_turn_terminal(
                api_url=api_url,
                token=token,
                session_id=session_id,
                message=post_message,
                request_id=post_request_id,
                turn_id=str(post["turn_id"]),
                run_id=str(post["run_id"]),
            )
            flush_for_projection("console-after-unsupported-post-turn")
            _wait_exact_assistant_marker(api_url, token, session_id, post_marker)
            normal_completed = (terminal.get("result") or {}).get("terminal_state") == "run_completed" and (
                post_claim.get("result") or {}
            ).get("terminal_state") == "run_completed"
            interrupt_receipt = {
                "status": "pass" if refused and normal_completed else "fail",
                "expectation": "unsupported",
                "interrupt_dispatched": False,
                "reason": "unsupported" if refused else "unexpected_dispatch",
                "normal_turn_completed": normal_completed,
            }
        write_json(root / "interrupt-contract-receipt.json", interrupt_receipt)

        naturally_dead = _wait_owned_processes_dead(claims)
        retained_sources = _retain_claim_sources(root, claims, environment, complete=True)
        if variant == SUPPORTED_VARIANT and _interrupt_output_contract_applies(provider):
            post_interrupt_output = _retained_post_interrupt_output_evidence(
                provider,
                post_claim,
                post_marker,
                retained_sources,
                root,
            )
            interrupt_receipt["post_interrupt_output"] = post_interrupt_output
            interrupt_receipt["status"] = (
                "pass" if interrupt_receipt.get("status") == "pass" and post_interrupt_output.get("valid") is True else "fail"
            )
            write_json(root / "interrupt-contract-receipt.json", interrupt_receipt)
        shipper_stop = shipper.stop() if shipper is not None else {}
        served_run_inventory = _wait_served_run_retirement(api_url, token, session_id, claims)
        session_retirement = retire_qualification_session(
            api_url,
            token,
            session_id,
            provider=provider,
            project=f"provider-console-{provider}",
        )
        cleanup = _console_cleanup_receipt(
            claims,
            retained_sources,
            process_stop_wait_completed=naturally_dead,
            shipper_stop=shipper_stop,
            served_run_inventory=served_run_inventory,
            session_retirement=session_retirement,
            expected_session_id=session_id,
        )
        write_json(root / "cleanup-receipt.json", cleanup)
        native_tool_receipt: dict[str, object] | None = None
        if provider in {"pi", "omp"}:
            retained_by_source = {
                str(item["source"]): root / Path(str(item["path"]))
                for item in retained_sources
                if item.get("retained") is True and isinstance(item.get("source"), str) and isinstance(item.get("path"), str)
            }
            retained_path_by_source = {
                str(item["source"]): str(item["path"])
                for item in retained_sources
                if item.get("retained") is True and isinstance(item.get("source"), str) and isinstance(item.get("path"), str)
            }
            if provider == "pi":
                raw_native_source = first_claim.get("source_path")
                native_source = retained_by_source.get(raw_native_source) if isinstance(raw_native_source, str) else None
                raw_response_source = first_claim.get(str(binding.get("provider_response_source_kind") or ""))
                provider_response_source = retained_by_source.get(raw_response_source) if isinstance(raw_response_source, str) else None
                native_tool_receipt = _pi_native_tool_receipt(
                    native_source=native_source,
                    provider_response_source=provider_response_source,
                    inspection=pi_tool_evidence,
                    dispatch=dispatch,
                    binding=binding,
                    binary_receipt=binary_receipt,
                    cleanup=cleanup,
                    marker=marker,
                    native_retained_path=retained_path_by_source.get(raw_native_source) if isinstance(raw_native_source, str) else None,
                    provider_response_retained_path=retained_path_by_source.get(raw_response_source)
                    if isinstance(raw_response_source, str)
                    else None,
                )
                write_json(root / "native-tool-receipt.json", native_tool_receipt)
            if continuation_receipt is not None:
                raw_first_source = str(first_native_source_path) if first_native_source_path is not None else None
                raw_second_source = resume_claim.get("source_path") or resume_claim.get("stdout_path")
                second_retained = retained_path_by_source.get(str(raw_second_source)) if isinstance(raw_second_source, str) else None
                if (
                    second_retained is None
                    or not isinstance(raw_second_source, str)
                    or raw_first_source != raw_second_source
                    or first_native_source_size is None
                ):
                    raise RuntimeError(f"Console {provider} continuation does not share one bounded native source")
                second_payload = Path(str(raw_second_source)).read_bytes()
                second_end_offset = second_native_source_size or len(second_payload)
                if first_native_source_size >= second_end_offset or second_end_offset > len(second_payload):
                    raise RuntimeError(f"Console {provider} continuation did not append to the retained native source")
                first_retained = retained_path_by_source.get(raw_first_source)
                if first_retained is None:
                    raise RuntimeError("first Console turn has no retained provider source")
                continuation_receipt["first_turn_evidence"] = {
                    "source_kind": "source_path",
                    "retained_path": first_retained,
                    "retained_source_path": raw_first_source,
                    # Both turn windows address one retained native history;
                    # the digest identifies the retained bytes, while offsets
                    # identify each turn within that immutable source.
                    "source_sha256": _sha256_bytes(second_payload),
                    "source_start_offset": 0,
                    "source_end_offset": first_native_source_size,
                    "session_id": session_id,
                    "thread_id": thread_id,
                    "run_id": first_claim.get("run_id"),
                    "provider_thread_id": first_claim.get("provider_thread_id"),
                    "projected_assistant_event_id": first_events[0].get("id"),
                    "native_message_id": first_native_response.get("native_message_id") if first_native_response else None,
                }
                continuation_receipt["second_turn_evidence"] = {
                    "source_kind": "source_path" if resume_claim.get("source_path") else "stdout_path",
                    "retained_path": second_retained,
                    "retained_source_path": raw_second_source,
                    "source_sha256": _sha256_bytes(second_payload),
                    "source_start_offset": first_native_source_size,
                    "source_start_offset_before_resume": first_native_source_size,
                    "source_end_offset": second_end_offset,
                    "source_end_offset_before_interrupt": second_end_offset,
                    "session_id": session_id,
                    "thread_id": thread_id,
                    "run_id": resume_claim.get("run_id"),
                    "provider_thread_id": resume_claim.get("provider_thread_id"),
                    "projected_assistant_event_id": resume_events[0].get("id"),
                    "native_message_id": resume_native_response.get("native_message_id") if resume_native_response else None,
                    "assistant_excerpt": event_text(resume_events[0])[:1024],
                    "marker_count": event_text(resume_events[0]).count(resume_marker),
                    "context_marker_count": resume_context_marker_count,
                    "linkage": continuation_receipt["response"]["linkage"],
                }
                continuation_receipt["native_source_end_offset_before_interrupt"] = second_end_offset
                continuation_receipt["interrupt_boundary"] = {
                    "source_path": str(raw_second_source),
                    "source_end_offset": second_end_offset,
                    "captured_before_interrupt": True,
                }
                write_json(root / "console-continuation-receipt.json", continuation_receipt)
        cleanup_written = True
        observation = _observation_from_receipts(
            dispatch=dispatch,
            binding=binding,
            interrupt=interrupt_receipt,
            cleanup=cleanup,
        )
        observation["continuation_context_recalled"] = continuation_context_recalled
        if provider == "pi":
            observation.update(
                {
                    "native_tool_call_observed": bool(pi_tool_evidence and pi_tool_evidence.get("tool_call_ids")),
                    "native_tool_result_observed": bool(pi_tool_evidence and pi_tool_evidence.get("tool_result_ids")),
                    "native_tool_result_linked": bool(pi_tool_evidence and pi_tool_evidence.get("linked_tool_call_ids")),
                    "native_tool_output_exact": bool(pi_tool_evidence and pi_tool_evidence.get("output_marker_observed")),
                    "native_shadow_taxonomy_observed": bool(pi_tool_evidence and pi_tool_evidence.get("native_shapes")),
                    "native_tool_receipt_valid": native_tool_receipt is not None and native_tool_receipt.get("status") == "pass",
                }
            )
            observation["pi_tool_enabled"] = all(
                observation[key]
                for key in (
                    "native_tool_call_observed",
                    "native_tool_result_observed",
                    "native_tool_result_linked",
                    "native_tool_output_exact",
                    "native_shadow_taxonomy_observed",
                    "native_tool_receipt_valid",
                )
            )
        observation["provider_source_artifacts"] = retained_sources
        assertion = console_lifecycle_assertions(observation)[ASSERTION_ID]
        if provider == "pi":
            assertion = assertion and observation.get("pi_tool_enabled") is True
        return {
            "schema_version": 1,
            "artifact_kind": "provider_console_lifecycle_result",
            "producer": REGISTRATION.to_dict(),
            "provider": provider,
            "variant": variant,
            "scenario_id": _scenario_id(provider),
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": now(),
            "status": "pass" if assertion else "fail",
            "assertions": {ASSERTION_ID: assertion},
            "provider_binary": binary_receipt,
            "observation": observation,
            "artifact_manifest": artifact_manifest(root),
        }
    finally:
        if not cleanup_written:
            _retain_failure_claim_diagnostics(root, claims, environment)
        termination_dispatch = _terminate_live_qualification_session(api_url, token, session_id) if not cleanup_written else None
        _force_cleanup(claims)
        shipper_stop: Mapping[str, object] | None = None
        if shipper is not None:
            try:
                shipper_stop = shipper.stop()
            except Exception as exc:  # noqa: BLE001 - cleanup must continue and report failure
                shipper_stop = {"status": "fail", "error": f"{type(exc).__name__}: {exc}"}
        if not cleanup_written:
            retained_sources = _retain_claim_sources(root, claims, environment, complete=True)
        if not cleanup_written:
            served_run_inventory = (
                _wait_served_run_retirement(api_url, token, str(session_id), claims)
                if session_id is not None
                else {"retired": False, "active_run_count": None, "error": "session_id_unavailable"}
            )
            session_retirement = retire_qualification_session(
                api_url,
                token,
                str(session_id or ""),
                provider=provider,
                project=f"provider-console-{provider}",
            )
            cleanup = _console_cleanup_receipt(
                claims,
                retained_sources,
                process_stop_wait_completed=_wait_owned_processes_dead(claims),
                shipper_stop=shipper_stop,
                served_run_inventory=served_run_inventory,
                session_retirement=session_retirement,
                expected_session_id=session_id,
                run_failed=True,
            )
            cleanup["termination_dispatch"] = termination_dispatch
            write_json(root / "cleanup-receipt.json", cleanup)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=PROVIDERS)
    parser.add_argument("--variant", choices=(SUPPORTED_VARIANT, UNSUPPORTED_VARIANT))
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--longhouse-cli", type=Path)
    parser.add_argument("--provider-bin", type=Path)
    parser.add_argument("--provider-version")
    parser.add_argument("--model", required=True)
    parser.add_argument("--registration", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    for name in (
        "provider",
        "variant",
        "evidence_root",
        "repo_root",
        "engine",
        "longhouse_cli",
        "provider_bin",
        "provider_version",
    ):
        if getattr(args, name) is None:
            print(json.dumps({"status": "fail", "failure_code": f"missing_required_argument:--{name.replace('_', '-')}"}))
            return 2
    for name in ("engine", "longhouse_cli", "provider_bin"):
        path = getattr(args, name)
        if not path.is_file() or not os.access(path, os.X_OK):
            print(json.dumps({"status": "fail", "failure_code": f"{name}_missing"}))
            return 2
    root = args.evidence_root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        result = _run_live(args.provider, args.variant, args, root)
    except Exception as exc:  # noqa: BLE001 - producer must retain one typed failure artifact
        result = {
            "schema_version": 1,
            "artifact_kind": "provider_console_lifecycle_result",
            "producer": REGISTRATION.to_dict(),
            "provider": args.provider,
            "variant": args.variant,
            "scenario_id": _scenario_id(args.provider),
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": now(),
            "status": "fail",
            "failure_code": "provider_console_lifecycle_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "artifact_manifest": artifact_manifest(root),
        }
    write_json(root / "result.json", result)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
