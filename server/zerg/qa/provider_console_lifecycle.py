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
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from zerg.qa.answer_markers import marker_answer_count
from zerg.qa.answer_markers import marker_answered
from zerg.qa.codex_auth import login_with_api_key
from zerg.qa.console_served_state_core import MARKER_ANSWERED_MORE_THAN_ONCE
from zerg.qa.console_served_state_core import MARKER_REPEATED_IN_ONE_REPLY
from zerg.qa.console_served_state_core import NATIVE_TRANSCRIPT_EMPTY
from zerg.qa.console_served_state_core import NATIVE_TRANSCRIPT_MID_WRITE
from zerg.qa.console_served_state_core import NATIVE_TRANSCRIPT_READ
from zerg.qa.console_served_state_core import NATIVE_TRANSCRIPT_UNREADABLE
from zerg.qa.console_served_state_core import MarkerAnswerCountError
from zerg.qa.console_served_state_core import StandingServedAnswerFault
from zerg.qa.console_served_state_core import assistant_marker_events
from zerg.qa.console_served_state_core import assistant_marker_mentions
from zerg.qa.console_served_state_core import event_text
from zerg.qa.console_served_state_core import event_time
from zerg.qa.console_served_state_core import native_transcript_state
from zerg.qa.console_served_state_core import served_answer_correspondence
from zerg.qa.console_served_state_core import served_answer_fault
from zerg.qa.live_kit import group_dead as _process_group_dead
from zerg.qa.live_kit import pid_dead as _pid_dead
from zerg.qa.live_kit import poll
from zerg.qa.live_session_toolkit import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.live_session_toolkit import RUNTIME_API_URL_ENV
from zerg.qa.live_session_toolkit import TranscriptShipper
from zerg.qa.live_session_toolkit import isolated_provider_home
from zerg.qa.live_session_toolkit import require_disposable_runtime
from zerg.qa.live_session_toolkit import retire_qualification_session
from zerg.qa.live_session_toolkit import start_transcript_shipper
from zerg.qa.live_session_toolkit import write_json
from zerg.qa.model_compliance import ModelDeviation
from zerg.qa.model_compliance import attach
from zerg.qa.openrouter_routing import prepare_pi_openrouter_routing
from zerg.qa.pi_native import pi_transcript_rows
from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run
from zerg.qa.provider_release_identity import artifact_manifest
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key
from zerg.qa.wait_kind import LONGHOUSE_ENGINE
from zerg.qa.wait_kind import LONGHOUSE_INGEST
from zerg.qa.wait_kind import PROVIDER_TURN
from zerg.qa.wait_kind import RUNTIME_HOST
from zerg.qa.wait_kind import expired
from zerg.qa.wait_kind import waited_on
from zerg.services.provider_interaction_semantics import omp_agent_end_is_terminal

PROVIDERS = ("codex", "claude", "opencode", "cursor")
# Only OMP's retained JSONL stream carries the terminal agent_end contract used
# by the post-interrupt evidence check. The other supported adapters settle via
# their own Runtime Host/claim contracts.
INTERRUPT_OUTPUT_TERMINAL_PROVIDERS = frozenset({"omp"})
ASSERTION_ID = "console_adapter_release_contract_preserved"
# Mid-turn steer is its own cell: it never folds into ASSERTION_ID, which is
# the strict gate on session.turn.start, so a steer failure cannot switch off
# Console turns (docs/specs/console-full-control.md, A2).
STEER_ASSERTION_ID = "console_turn_steer_preserved"
STEER_PROVIDERS = frozenset({"codex", "claude", "pi", "omp", "opencode"})
SUPPORTED_VARIANT = "interrupt_supported"
# Every Console adapter interrupts, so no cell has this variant, the parser does not take it and nothing here runs
# it. The name stays while the factory's Console checker still has a branch for the string.
UNSUPPORTED_VARIANT = "interrupt_unsupported"
SCENARIO_IDS = tuple(f"{provider}_console_adapter_lifecycle" for provider in PROVIDERS)


def steer_execution_variant(provider: str, scenario_id: str) -> str:
    """The steer cell's own execution key: the factory runs one execution per
    cell, so the steer step runs in its own invocation, never in the one that
    proves ASSERTION_ID."""

    return execution_variant_key(provider=provider, assertion_id=STEER_ASSERTION_ID, scenario_id=scenario_id, variant=None)


# Negative control for every steer cell: a qa-fault-injection Machine Agent
# acknowledges the steer and drops it (engine/src/qa_fault.rs).
CONSOLE_STEER_FAULT = "console_steer_noop"

STEER_VARIANTS = {
    provider: steer_execution_variant(provider, f"{provider}_console_adapter_lifecycle")
    for provider in PROVIDERS
    if provider in STEER_PROVIDERS
}
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
    producer_revision=15,
    scenario_id=SCENARIO_IDS[0],
    scenario_ids=SCENARIO_IDS,
    # 5: Codex Console interrupt is supported (turn/interrupt).
    # 6: a mid-turn steer step, reported as STEER_ASSERTION_ID.
    # 22: the waits for a served reply read the newest events, as the steer receipt does.
    # 23: a process that has exited counts as stopped before its parent collects it (live_kit.pid_dead).
    scenario_revision=23,
    assertion_cells=(
        # No provider is interrupt_unsupported since Codex gained turn/interrupt.
        (ASSERTION_ID, SUPPORTED_VARIANT),
        (STEER_ASSERTION_ID, None),
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


def _interrupt_output_contract_applies(provider: str) -> bool:
    return provider in INTERRUPT_OUTPUT_TERMINAL_PROVIDERS


def _scenario_id(provider: str) -> str:
    return f"{provider}_console_adapter_lifecycle"


def _provider_environment(provider: str, args: argparse.Namespace, home: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["HOME"] = str(home)
    # A relocated HOME makes Cursor's default macOS Keychain store pop a
    # "Keychain Not Found ... Reset To Defaults" dialog on the desktop.
    environment["AGENT_CLI_CREDENTIAL_STORE"] = "file"
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
        if args.model:
            # One slow OpenRouter host must not stall a cell: pin the model's requests.
            prepare_pi_openrouter_routing(home / ".pi", args.model)
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
    raise expired(RuntimeError(f"Runtime Host never observed Console turn terminal: {last_result}"), RUNTIME_HOST)


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
    error = RuntimeError(f"Console claim did not reach {sorted(states)}: {last}")
    kind = _claim_wait_kind(last)
    raise expired(error, kind) if kind else error


def _claim_wait_kind(claim: Mapping[str, Any] | None) -> str | None:
    """What a wait on an adapter claim was waiting on, from the claim it last read.

    The claim is the engine's record of one Console turn
    (``engine/src/turn_claims.rs``): written ``claimed`` when the engine takes
    the run, ``spawned`` once the provider process is running, and
    ``terminal`` or ``failed`` when that process is done. A claim still at
    ``claimed`` is the engine not yet running the provider. A turn stuck at
    ``spawned`` with the provider still alive is the provider's own turn; with
    the provider gone, the engine never recorded the exit. Only those two
    states are read here. No claim at all says nothing (the Runtime Host may
    never have dispatched the run, or the engine never took it), and neither
    does any other state: one this producer has not been taught is not
    evidence about who was being waited on. Nor does a ``spawned`` claim that
    records no process: with no pid there is nothing to find alive or gone.
    """

    state = claim.get("state") if isinstance(claim, Mapping) else None
    if state == "claimed":
        return LONGHOUSE_ENGINE
    if state == "spawned":
        pid = claim.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        return LONGHOUSE_ENGINE if _pid_dead(pid) else PROVIDER_TURN
    return None


def _archive_events(api_url: str, token: str, session_id: str) -> list[object]:
    # This archive endpoint contains only durable records and omits origin.
    # Served projections use the same predicate without that archive default.
    # The newest events, for every reader: a long session's first 200 need not
    # include the reply a wait is for, nor the rows a receipt is judged from.
    result = _request(api_url, token, "GET", f"/api/agents/sessions/{session_id}/events?anchor=tail&limit=200")
    return result.get("events") if isinstance(result.get("events"), list) else []


def _assistant_marker_events(
    api_url: str, token: str, session_id: str, marker: str, *, alongside: tuple[str, ...] = ()
) -> list[dict[str, Any]]:
    return assistant_marker_events(_archive_events(api_url, token, session_id), marker, default_origin="durable", alongside=alongside)


# Console adapters whose served events carry the id of the provider's own row
# that wrote them, and the field of a turn's claim that names the record those
# rows are in. Pi and OMP write a session file whose assistant entries have an
# id; the Runtime Host serves an entry's first block under that id and its
# later text blocks under ``<id>-text-<n>``. Claude's print stream gives every
# assistant event a uuid and its text is served as ``<uuid>-text-<n>``. For
# these a served reply is held to the provider's own answering rows, by id.
_NATIVE_ROWS_IN_CLAIM = {"pi": "source_path", "omp": "source_path", "claude": "stdout_path"}

# Console adapters whose served events cannot be held to the provider's own
# rows by id, and why. Their served replies are counted instead: one finished
# turn that was asked for one reply serves exactly one answering event, and a
# second is ``marker_answered_more_than_once`` because this producer cannot
# tell a reply the provider wrote twice from one Longhouse served twice.
_SERVED_IDS_NOT_NATIVE = {
    "codex": (
        "a rollout line carries no id of its own, so the engine gives each one a fresh uuid as it parses it "
        "(engine/src/pipeline/parser.rs) and a served event names no row of the rollout"
    ),
    "cursor": (
        "Cursor writes no id per record: the engine names a transcript record by its byte offset, "
        "and the print stream this producer reads has no ids at all"
    ),
    "opencode": (
        "the print stream names messages and parts (msg_..., prt_...) and the served id is the engine's own "
        "derivation from them; reproducing that derivation here would compare the engine with itself"
    ),
}


# The two things `marker_answered_more_than_once` still says, in the words its
# message carries, so that the first occurrence can be read from the failure
# alone: which of the two it was, beside the ids it counted.
_COUNTED_ADAPTER = "exactly 1 (counted adapter: its served ids are not the provider's rows, so served replies are counted)"
_SINGLE_REPLY_TURN = "exactly 1 (single-reply turn: the provider itself answered in more than one row)"


def _native_assistant_rows(provider: str, path: Path) -> tuple[list[dict[str, object]] | None, str, list[str]]:
    """The assistant rows of the provider's own record at ``path``, what the read found, and the sessions it names.

    Each row has the ``id`` the provider gave it (None when it gave none), its
    ``source_offset`` and, where the reader knows it, ``source_end`` in the
    record, and a ``text`` that keeps one line group per text block.

    What the read found is one of the states of `native_transcript_state`. A
    record that could not be read, or is empty, has no rows to give (None). A
    record that ends inside a row gives the complete rows before that last
    line and says ``mid_write``: the row still being written must not hide the
    ones already there, and a reader that needs the whole record (how many
    rows answer, for a served page to be held to) can see that it has not got
    it.
    """

    if provider == "pi":
        parsed, provider_session_id, metadata = pi_transcript_rows(path)
        if metadata.get("error"):
            return None, NATIVE_TRANSCRIPT_UNREADABLE, []
        if not metadata.get("source_end_offset") or not metadata.get("lines"):
            return None, NATIVE_TRANSCRIPT_EMPTY, []
        state = NATIVE_TRANSCRIPT_READ if metadata.get("complete_last_line") is True else NATIVE_TRANSCRIPT_MID_WRITE
        complete_end = int(metadata.get("complete_end_offset") or 0)
        rows = [
            {
                "id": row.get("entry_id"),
                "source_offset": int(row.get("source_offset") or 0),
                "source_end": None,
                "text": str(row.get("text") or ""),
            }
            for row in parsed
            if row.get("type") == "assistant" and int(row.get("source_offset") or 0) < complete_end
        ]
        sessions = [provider_session_id] if isinstance(provider_session_id, str) and provider_session_id else []
    else:
        try:
            content: bytes | None = path.read_bytes()
        except OSError:
            content = None
        state = native_transcript_state(content)
        if content is None or state not in {NATIVE_TRANSCRIPT_READ, NATIVE_TRANSCRIPT_MID_WRITE}:
            return None, state, []
        rows = []
        sessions = []
        source_offset = 0
        # Up to the last newline: a line without one is a row still being written.
        for raw_line in content[: content.rfind(b"\n") + 1].splitlines(keepends=True):
            line_end = source_offset + len(raw_line)
            try:
                event = json.loads(raw_line) if raw_line.strip() else None
            except json.JSONDecodeError:
                event = None
            if isinstance(event, Mapping):
                message = event.get("message")
                assistant = isinstance(message, Mapping) and message.get("role") == "assistant"
                if provider == "omp":
                    if event.get("type") == "session" and isinstance(event.get("id"), str) and event["id"]:
                        sessions.append(event["id"])
                    if assistant and event.get("type") in {"message", "message_end"}:
                        rows.append(
                            {
                                "id": event.get("id"),
                                "source_offset": source_offset,
                                "source_end": line_end,
                                "text": _native_message_text(message),
                            }
                        )
                elif provider == "claude" and assistant and event.get("type") == "assistant":
                    rows.append(
                        {
                            "id": event.get("uuid"),
                            "source_offset": source_offset,
                            "source_end": line_end,
                            "text": _native_message_text(message),
                        }
                    )
            source_offset = line_end
    return rows, state, sessions


def _native_answer_rows(
    provider: str, path: Path, marker: str, *, alongside: tuple[str, ...] = (), recalled: bool = False
) -> tuple[dict[str, int] | None, str]:
    """The provider's own rows at ``path`` that answer ``marker``, by id, and what the read found.

    The one reader a served page is held to, for a Console turn and for a Helm
    session alike. Each answering row's id maps to how often it answers
    (``zerg.qa.answer_markers``; for ``recalled``, how often it holds the
    phrase). The rows are None unless the record was read to the end: one that
    could not be read, is empty or ends inside a row is not a record without
    answers, it is one whose answers are not known yet. They are None too when
    a row that answers has no id: a reply that cannot be named cannot be held
    to anything by id, and that is not the same as the provider not replying.
    """

    rows, state, _sessions = _native_assistant_rows(provider, path)
    if rows is None or state != NATIVE_TRANSCRIPT_READ:
        return None, state
    answers: dict[str, int] = {}
    for row in rows:
        text = str(row["text"])
        count = text.count(marker) if recalled else marker_answer_count(text, marker, alongside=alongside)
        if not count:
            continue
        if not isinstance(row["id"], str) or not row["id"]:
            return None, NATIVE_TRANSCRIPT_UNREADABLE
        # OMP can retain a message more than once while it reconciles its
        # stream; the record that answers most is the row as it stands.
        answers[row["id"]] = max(answers.get(row["id"], 0), count)
    return answers, state


def _claim_native_answers(
    provider: str, claim: Mapping[str, object], marker: str, *, alongside: tuple[str, ...] = ()
) -> Callable[[], tuple[dict[str, int] | None, str]] | None:
    """A read of the answering rows (`_native_answer_rows`) of the record one Console turn's claim names, or None.

    None is an adapter in `_SERVED_IDS_NOT_NATIVE`.
    """

    field = _NATIVE_ROWS_IN_CLAIM.get(provider)
    if field is None:
        return None
    raw = claim.get(field)
    if not isinstance(raw, str) or not raw:
        return lambda: (None, NATIVE_TRANSCRIPT_UNREADABLE)
    return lambda: _native_answer_rows(provider, Path(raw), marker, alongside=alongside)


def _wait_served_answers(
    api_url: str,
    token: str,
    session_id: str,
    marker: str,
    *,
    alongside: tuple[str, ...] = (),
    timeout: float = 180,
    native_answers: Callable[[], tuple[dict[str, int] | None, str]] | None = None,
    one_reply: bool = True,
    unanswered: Callable[[str], RuntimeError] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, object] | None]:
    """Wait until the archive serves what the provider answered ``marker`` with.

    An event answers under the rule in ``zerg.qa.answer_markers``; ``alongside``
    names the other markers the reply was asked to carry. An event that only
    mentions the marker (narration ahead of a tool call) is not a reply and is
    reported beside the count when the wait fails.

    ``native_answers`` reads the provider's own rows that answer
    (`_claim_native_answers`). With it the served events that answer must be
    those rows, by id (`served_answer_correspondence`): the wait returns them
    with that account once every answering row is served as often as it
    answers, and a served answer the provider's record does not hold, or one
    of its answers served more often than it was written, fails with the typed
    reason of `served_answer_fault`, which is a finding about Longhouse.
    Without it (an adapter in `_SERVED_IDS_NOT_NATIVE`) the served replies are
    counted, to one where the turn binds one reply, and the account is None.

    ``one_reply`` is a turn whose receipts bind one reply: the first turn, the
    continuation and the turns around an interrupt each ask for one reply of a
    finished print-mode run, and the receipts and the factory's validators of
    them name one native row and one served event. There a provider that
    answered in more than one row is ``marker_answered_more_than_once``, said
    of the provider's rows and not of what was served. The steer step binds no
    such receipt and passes False: OMP moves a running command to a background
    job when a steer arrives, answers as the steer lands and again when the
    job's notice arrives, and both replies are its own. False means the same
    for a counted adapter, whose repeated reply is accepted as it is for one
    held by id.

    The provider's turn is over before this is called, so what it waits on is
    Longhouse serving the reply. When the wait gives up, why is decided from
    the provider's own stream for the turn and not from the count that never
    came: ``unanswered`` takes the last served count and returns the failure
    (`_unanswered_turn_error`). A served-answer fault still standing at the
    deadline is reported as that fault. Only a caller with no turn to read,
    which no live path is, gets the bare count.
    """

    deadline = time.monotonic() + timeout
    stable_since: float | None = None
    last_count = 0
    last_mentions = 0
    correspondence: dict[str, object] | None = None
    refusal = StandingServedAnswerFault()
    while time.monotonic() < deadline:
        events = _archive_events(api_url, token, session_id)
        matches = assistant_marker_events(events, marker, default_origin="durable", alongside=alongside)
        mentions = assistant_marker_mentions(events, marker, default_origin="durable", alongside=alongside)
        last_count = len(matches)
        last_mentions = len(mentions)
        if native_answers is None:
            # Counted, for the reason `_SERVED_IDS_NOT_NATIVE` gives. In a turn
            # that binds one reply, more replies cannot bring the count back
            # down, so that fails now. A turn that binds none takes however
            # many the provider gave: they cannot be held to its rows by id,
            # and they are not refused for a count the scenario allows.
            if one_reply and last_count > 1:
                raise MarkerAnswerCountError(
                    MARKER_ANSWERED_MORE_THAN_ONCE, marker=marker, answers=matches, mentions=mentions, expected=_COUNTED_ADAPTER
                )
            converged = last_count >= 1
        else:
            # Read after the page, so nothing served is newer than what is read here.
            native, native_state = native_answers()
            native_readable = native is not None
            native = native or {}
            # Raised on the first read, with no settle window. The window the
            # served faults stand through exists because a record can change
            # under the read; this one cannot. Every caller reads it after
            # the turn's claim is terminal, so the process that wrote it has
            # exited: Pi's and OMP's session files, where an entry is
            # appended once (OMP may write one message's row again under the
            # same id while its stream reconciles, which is one row here),
            # and Claude's print stream, which is that process's captured
            # output. A record still ending mid-row is not read as answers
            # at all (`_claim_native_answers`), so a second row seen here is
            # a complete row of a finished record and will not go away.
            if one_reply and len(native) > 1:
                raise MarkerAnswerCountError(
                    MARKER_ANSWERED_MORE_THAN_ONCE,
                    marker=marker,
                    answers=[{"id": row_id} for row_id in native],
                    mentions=[],
                    expected=_SINGLE_REPLY_TURN,
                    surface="native assistant row",
                )
            correspondence = served_answer_correspondence(events, marker, native, default_origin="durable", alongside=alongside)
            correspondence["native_transcript_readable"] = native_readable
            correspondence["native_transcript_state"] = native_state
            found = served_answer_fault(events, marker, native, default_origin="durable", alongside=alongside) if native_readable else None
            if found is not None:
                found.observation = {"session_id": session_id, "answer_correspondence": correspondence}
            fault = refusal.standing(found)
            if fault is not None:
                raise fault
            converged = correspondence["converged"] is True
        # One reply says its marker once; a reply that repeats it is not one,
        # and nothing served later changes what this one says.
        repeated = [event for event in matches if event_text(event).count(marker) != 1] if one_reply else []
        if repeated:
            raise MarkerAnswerCountError(
                MARKER_REPEATED_IN_ONE_REPLY, marker=marker, answers=repeated, mentions=mentions, expected="each to say its marker once"
            )
        if converged:
            if stable_since is None:
                stable_since = time.monotonic()
            if time.monotonic() - stable_since >= 2:
                return matches, correspondence
        else:
            stable_since = None
        time.sleep(0.5)
    served = f"count={last_count}, events that only mention it={last_mentions}"
    if correspondence is not None:
        served += (
            f", native answering rows={correspondence['native_answer_rows']}, "
            f"not yet served={correspondence['unserved_native_row_ids']}, "
            f"native record {correspondence['native_transcript_state']}"
        )
        if correspondence["native_transcript_readable"] is not True:
            # The served page was held to nothing: that is not the provider's reply missing.
            raise ConsoleTurnFailure(
                PROVIDER_REPLY_NOT_JUDGED, f"the provider's own record of the turn could not be read as rows ({served})"
            )
    error = unanswered(served) if unanswered is not None else RuntimeError(f"assistant marker did not converge exactly once ({served})")
    # A fault still on the page is why this never converged: say that, and
    # that the wait was on ingest when the provider answered and nothing was served.
    fault = refusal.at_deadline()
    if fault is not None:
        raise expired(fault, LONGHOUSE_INGEST) if last_count == 0 and waited_on(error) == LONGHOUSE_INGEST else fault
    raise error


def _wait_turn_served(
    api_url: str,
    token: str,
    session_id: str,
    provider: str,
    claim: Mapping[str, object],
    marker: str,
    *,
    turn: str,
    alongside: tuple[str, ...] = (),
    context_marker: str | None = None,
    steered: bool = False,
    **wait: Any,
) -> tuple[list[dict[str, Any]], dict[str, object] | None]:
    """`_wait_served_answers` for one Console turn: held to the rows its claim names, decided from its stream if it gives up."""

    return _wait_served_answers(
        api_url,
        token,
        session_id,
        marker,
        alongside=alongside,
        native_answers=_claim_native_answers(provider, claim, marker, alongside=alongside),
        unanswered=lambda served: _unanswered_turn_error(
            provider, claim, marker, turn=turn, alongside=alongside, served=served, context_marker=context_marker, steered=steered
        ),
        **wait,
    )


def _bounded_marker_excerpt(value: str, marker: str, radius: int = 160) -> str:
    index = value.find(marker)
    if index < 0:
        return ""
    start = max(0, index - radius)
    end = min(len(value), index + len(marker) + radius)
    return value[start:end]


def _stream_event(provider: str, event: Mapping[str, object]) -> tuple[str, str] | None:
    """What one line of a Console adapter's stream is to its turn, or None.

    ``("assistant", text)`` and ``("user", text)`` are a message of the model
    and a prompt the stream echoes (OpenCode's echoes none). ``("end", "")`` is
    the provider's own end of the turn; ``("open", "")`` says the turn goes on:
    OpenCode's hand-off to a tool, an OMP ``agent_end`` that is not terminal,
    the start of a Codex task.
    """

    kind, message = event.get("type"), event.get("message")
    if provider in {"claude", "cursor"}:
        if kind in {"assistant", "user"} and isinstance(message, Mapping):
            return str(kind), _native_message_text(message)
        return ("end", "") if kind == "result" else None
    if provider == "opencode":
        part = event.get("part")
        part = part if isinstance(part, Mapping) else {}
        if kind == "text" and part.get("type") == "text" and isinstance(part.get("text"), str):
            return "assistant", str(part["text"])
        return ("open" if part.get("reason") == "tool-calls" else "end", "") if kind == "step_finish" else None
    if provider == "codex":
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        if kind == "response_item" and payload.get("role") in {"assistant", "user"}:
            text = payload.get("text") if not isinstance(payload.get("content"), list) else _native_message_text(payload, _CODEX_TEXT)
            return str(payload["role"]), text if isinstance(text, str) else ""
        if kind == "event_msg" and payload.get("type") in {"task_started", "task_complete"}:
            return ("open" if payload["type"] == "task_started" else "end"), ""
        return None
    if provider not in {"pi", "omp"}:
        return None
    if kind == "message_end" and isinstance(message, Mapping) and message.get("role") in {"assistant", "user"}:
        return str(message["role"]), _native_message_text(message)
    if kind == "agent_end":
        return ("end" if provider == "pi" or omp_agent_end_is_terminal(event) else "open"), ""
    return None


def _stream_events(provider: str, content: str) -> list[tuple[str, str]]:
    read: list[tuple[str, str]] = []
    for line in content.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        found = _stream_event(provider, event) if isinstance(event, Mapping) else None
        if found is not None:
            read.append(found)
    return read


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
        assistant_output = "\n".join(text for kind, text in _stream_events(provider, content) if kind == "assistant")
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


# The typed reasons a Console turn fails with before the producer can judge
# anything. Each opens the failure's message, as the reasons of
# `MarkerAnswerCountError` do, so the factory names the failure from the
# producer's own word and never from wording.
#
# The engine ended a run that had to complete as anything but ``run_completed``.
CONSOLE_RUN_NOT_COMPLETED = "console_run_not_completed"
# The provider's own stream holds the reply that answers, the turn's flush
# shipped what the engine had, and the archive never served the reply.
PROVIDER_REPLY_NOT_SERVED = "provider_reply_not_served"
# The engine completed the run and the provider's stream holds no end of turn.
PROVIDER_TURN_NOT_ENDED = "provider_turn_not_ended"
# The resumed provider's session record does not chain the continuation prompt
# back to the message that stored the context it asks for.
CONSOLE_RESUME_CONTEXT_MISSING = "console_resume_context_missing"
# The turn has no answering reply and what would say whose doing that is
# cannot be read: the stream, a prompt in it that names the marker, any reply
# at all, or what a resumed session loaded. Nothing is said about the model.
PROVIDER_REPLY_NOT_JUDGED = "provider_reply_not_judged"


class ConsoleTurnFailure(RuntimeError):
    """A Console turn failed for a reason this producer can name.

    The message opens with the reason and carries states, counts and marker
    names, never model text.
    """

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}")


# Where each Console adapter keeps one turn's stream, as a field of the turn's
# claim. Every adapter but Codex runs one provider process per turn and keeps
# its output; Codex's app-server writes the thread's rollout, which holds
# every turn of the session.
_TURN_STREAM_IN_CLAIM = {
    "claude": "stdout_path",
    "cursor": "stdout_path",
    "opencode": "stdout_path",
    "pi": "stdout_path",
    "omp": "stdout_path",
    "codex": "source_path",
}

# A qualification marker as a producer mints it: a name and a uuid's hex.
_MARKER_SHAPED = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)_[0-9a-f]{32}\b")


def _marker_name(marker: str) -> str:
    """A marker without its random part, which is all a failure message says of one."""

    found = _MARKER_SHAPED.fullmatch(marker)
    return found.group(1) if found else "the marker"


def _turn_account(provider: str, claim: Mapping[str, object], marker: str, *, alongside: tuple[str, ...] = ()) -> dict[str, object] | None:
    """What the provider's own stream says of one turn's reply to ``marker``, with none of the model's text.

    None when the stream cannot be read. ``answering_replies`` counts the
    assistant messages that answer under ``zerg.qa.answer_markers``. Of the
    final one it keeps whether it names the marker at all and the names of the
    other markers it carries: what the model sent in place of the reply it was
    asked for. ``prompt_named_marker`` is None where the stream echoes no
    prompt, which is nothing known either way.
    """

    raw = claim.get(_TURN_STREAM_IN_CLAIM.get(provider, ""))
    if not isinstance(raw, str) or not raw:
        return None
    try:
        content = Path(raw).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    replies: list[str] = []
    prompts: list[str] = []
    ended = False
    for kind, text in _stream_events(provider, content):
        if kind in {"end", "open"}:
            ended = kind == "end"
            if kind == "open" and provider == "codex":
                # The rollout holds the session: the task that starts last is the turn.
                replies, prompts = [], []
        elif not text.strip():
            continue
        elif kind == "user":
            prompts.append(text)
        elif provider == "opencode" and replies and text.startswith(replies[-1]):
            # OpenCode writes a text part again as it grows; the last write is the part.
            replies[-1] = text
        else:
            replies.append(text)
    final = replies[-1] if replies else ""
    return {
        "stream": _TURN_STREAM_IN_CLAIM[provider],
        "turn_ended": ended,
        "prompt_named_marker": any(marker in text for text in prompts) if prompts else None,
        "assistant_replies": len(replies),
        "answering_replies": sum(1 for text in replies if marker_answered(text, marker, alongside=alongside)),
        "final_reply_names_marker": marker in final,
        "final_reply_other_markers": sorted({found.group(1) for found in _MARKER_SHAPED.finditer(final) if found.group(0) != marker}),
    }


def _run_completed(claim: Mapping[str, object]) -> bool:
    result = claim.get("result")
    return claim.get("state") == "terminal" and isinstance(result, Mapping) and result.get("terminal_state") == "run_completed"


def _require_run_completed(provider: str, claim: Mapping[str, object], *, turn: str, marker: str, alongside: tuple[str, ...] = ()) -> None:
    """Fail a turn that had to complete, at once and in the engine's own words.

    The claim is the engine's statement of how the run ended. A run it ended
    as failed or cancelled will never be followed by a served reply worth
    waiting for, so this is raised as soon as the claim is read. The message
    carries the engine's reason code and error and, beside them, what the provider's own stream
    holds for the turn: whether the provider answered is a separate fact from
    how the engine ended the run.
    """

    if _run_completed(claim):
        return
    result = claim.get("result") if isinstance(claim.get("result"), Mapping) else {}
    # The engine's own code for why (a provider that moved the session to
    # another file, a refused credential), where the claim keeps its terminal event.
    event = claim.get("terminal_event")
    payload = event.get("payload") if isinstance(event, Mapping) else None
    code = payload.get("terminal_reason") if isinstance(payload, Mapping) else None
    reason = f" terminal_reason={code}" if isinstance(code, str) and code else ""
    account = _turn_account(provider, claim, marker, alongside=alongside)
    if account is None:
        answered = "could not be read"
    elif account["answering_replies"]:
        answered = f"holds the reply that answers {_marker_name(marker)}"
    else:
        answered = f"holds {account['assistant_replies']} assistant reply(ies) and none answers {_marker_name(marker)}"
    raise ConsoleTurnFailure(
        CONSOLE_RUN_NOT_COMPLETED,
        f"the engine ended the {turn} turn as state={claim.get('state')} terminal_state={result.get('terminal_state')}{reason} "
        f"with error {str(result.get('error') or claim.get('error'))[:500]!r}; the provider's own stream for the turn {answered}",
    )


def _resumed_session_holds_context(path: Path, *, prompt_marker: str, context_marker: str) -> bool | None:
    """Whether a resumed Pi or OMP session chains its continuation prompt back to the stored context.

    Both write each session entry with an ``id`` and the ``parentId`` it
    follows, and build a turn's context by walking that chain. True when the
    user entry that names ``prompt_marker`` has an ancestor that names
    ``context_marker``; False when its chain ends without one, which is a
    resumed process that did not load the earlier turn. None when the record
    cannot be read, does not hold the prompt, or links no entry to another
    (not the format this reads), so nothing is known.
    """

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    entries: dict[str, tuple[object, str]] = {}
    prompt: str | None = None
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str):
            continue
        message = entry.get("message")
        text = _native_message_text(message) if isinstance(message, Mapping) and message.get("role") == "user" else ""
        entries[entry["id"]] = (entry.get("parentId"), text)
        if prompt_marker in text:
            prompt = entry["id"]
    if prompt is None or not any(parent in entries for parent, _text in entries.values() if isinstance(parent, str)):
        return None
    parent = entries[prompt][0]
    while isinstance(parent, str) and parent in entries:
        parent, text = entries.pop(parent)
        if context_marker in text:
            return True
    return False


def _unanswered_turn_error(
    provider: str,
    claim: Mapping[str, object],
    marker: str,
    *,
    turn: str,
    alongside: tuple[str, ...] = (),
    served: str = "",
    context_marker: str | None = None,
    steered: bool = False,
) -> RuntimeError:
    """Why a turn the engine completed has no served answer to ``marker``, decided from the provider's own stream.

    The served side has given up (``served`` is its last count) or was never
    going to be asked. What the provider wrote for the turn says which of
    these happened, and each has a different owner:

    - The stream holds the reply that answers: Longhouse never served it
      (``provider_reply_not_served``, a wait on ingest).
    - The stream has no end of turn: the wait was on the provider's turn.
    - The turn ended and its final reply does not answer. That is the model's
      choice, a `ModelDeviation` whose reason says what the reply carried
      instead, only when Longhouse's part is shown: the prompt the stream
      echoes names the marker and, for a continuation that asks for stored
      context (``context_marker``), the resumed session chains back to the
      message that stored it. A resumed session without that chain is
      ``console_resume_context_missing``, a finding and never a deviation.
    - Anything that leaves it unknown is ``provider_reply_not_judged``.

    ``steered`` is a turn whose marker a mid-turn steer asked for and not its
    prompt. What the stream echoes of its prompts then says nothing about the
    marker (Claude takes a steer as hook context and OpenCode echoes nothing),
    so a stream without it is not read as a prompt that never named it, and
    the deviation claims only what the stream shows. That the steer reached
    the provider is not shown here: the caller holds it (`_steer_delivery`)
    before it calls the reply the model's.
    """

    account = _turn_account(provider, claim, marker, alongside=alongside)
    name, tail = _marker_name(marker), f" (served: {served})" if served else ""
    if account is None:
        return ConsoleTurnFailure(PROVIDER_REPLY_NOT_JUDGED, f"the provider's stream for the {turn} turn could not be read{tail}")
    where = f"the provider's {account['stream']} for the {turn} turn"
    if account["answering_replies"]:
        detail = (
            f"{where} holds the reply that answers {name}, the turn's flush shipped what the engine had, and the archive did not serve it"
        )
        return expired(ConsoleTurnFailure(PROVIDER_REPLY_NOT_SERVED, detail + tail), LONGHOUSE_INGEST)
    if not account["turn_ended"]:
        detail = f"{where} holds no end of turn and no reply that answers {name}"
        return expired(ConsoleTurnFailure(PROVIDER_TURN_NOT_ENDED, detail + tail), PROVIDER_TURN)
    held = {"run_completed": True, "provider_turn_ended": True, "provider_replied": True}
    unjudged: str | None = None
    if account["prompt_named_marker"] is False and not steered:
        unjudged = f"{where} echoes its prompts and none names {name}"
    elif not account["assistant_replies"]:
        unjudged = f"the provider completed the {turn} turn without producing an assistant reply"
    elif account["prompt_named_marker"]:
        held["prompt_named_marker"] = True
    if unjudged is None and context_marker is not None:
        record = Path(str(claim.get("source_path") or ""))
        context = _resumed_session_holds_context(record, prompt_marker=marker, context_marker=context_marker)
        if context is False:
            detail = f"the resumed provider's session record does not chain the {turn} prompt back to the message that stored its context"
            return ConsoleTurnFailure(CONSOLE_RESUME_CONTEXT_MISSING, detail + tail)
        if context is None:
            unjudged = f"the provider's session record does not show what the resumed {turn} turn loaded"
        held["resumed_session_holds_context"] = True
    if unjudged is not None:
        return ConsoleTurnFailure(PROVIDER_REPLY_NOT_JUDGED, unjudged + tail)
    others: list[str] = account["final_reply_other_markers"]  # type: ignore[assignment]
    if account["final_reply_names_marker"]:
        reason, carried = "reply_named_marker_with_other_text", f"names {name} with other text and does not answer with it"
    elif others:
        reason, carried = "reply_carried_another_marker", f"carries {', '.join(others)} and not the {name} it was asked for"
    else:
        reason, carried = "reply_carried_no_marker", f"carries no marker where {name} was asked for"
    detail = f"the {turn} turn completed and its final reply {carried} ({account['assistant_replies']} assistant reply(ies) in {where}, none answers)"
    return ModelDeviation(reason, detail + tail, **held)


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


def _one_marker_reply(
    rows: Iterable[Mapping[str, object]],
    marker: str,
    *,
    alongside: tuple[str, ...] = (),
    surface: str = "native assistant row",
) -> dict[str, object] | None:
    """The one assistant row, among ``rows``, that replies with ``marker``.

    ``rows`` are the assistant rows of a finished turn in the provider's own
    transcript, or the assistant messages of a finished run's output stream,
    each with an ``id`` and a ``text`` that keeps one line group per text
    block. A row replies when it answers under ``zerg.qa.answer_markers``
    (``alongside`` names the other markers the reply was asked to carry). A row
    that only names the marker is a mention: a turn that works through a tool
    says "I will read the file, then reply with <MARKER>" on the row that makes
    the call, and counting that row made a correct turn look like two replies.

    Returns the reply with ``marker_count`` (occurrences inside it) and
    ``mention_ids``; None when no row answers, because only the caller knows
    which boundary the reply was expected inside.

    This counts the provider's own replies, not what Longhouse served, and it
    counts them to one because every caller binds a receipt to one reply: the
    native row of a first turn or a continuation, the row a Pi tool pair
    answers into, the output message a terminal event must follow. The
    factory's validators of those receipts name one row against the retained
    bytes. A provider that answered such a turn in two rows therefore raises
    ``marker_answered_more_than_once`` here: said of the provider's rows, and
    never reported as a verdict about the subject. Whether what was served is
    what the provider wrote is a different question, settled by id in
    `_wait_served_answers`.
    """

    answers: list[Mapping[str, object]] = []
    mentions: list[Mapping[str, object]] = []
    for row in rows:
        text = str(row.get("text") or "")
        if marker and marker in text:
            (answers if marker_answered(text, marker, alongside=alongside) else mentions).append(row)
    if len(answers) > 1:
        raise MarkerAnswerCountError(
            MARKER_ANSWERED_MORE_THAN_ONCE,
            marker=marker,
            answers=[{"id": row.get("id")} for row in answers],
            mentions=[{"id": row.get("id")} for row in mentions],
            expected=_SINGLE_REPLY_TURN,
            surface=surface,
        )
    if not answers:
        return None
    return {
        **answers[0],
        "marker_count": str(answers[0].get("text") or "").count(marker),
        "mention_ids": [row.get("id") for row in mentions],
    }


# The block types that hold text in a Codex rollout message.
_CODEX_TEXT = frozenset({"output_text", "input_text", "text"})


def _native_message_text(message: Mapping[str, object], kinds: frozenset[str] = frozenset({"text"})) -> str:
    """Text of a native message, one line group per text block.

    Blocks are joined with a newline: the Runtime Host serves each text block
    as its own event, and a marker that ends one block must not run into the
    first word of the next.
    """

    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block["text"])
        for block in content
        if isinstance(block, Mapping) and block.get("type") in kinds and isinstance(block.get("text"), str)
    )


def _native_marker_evidence(
    provider: str,
    path: Path,
    marker: str,
    *,
    alongside: tuple[str, ...] = (),
    minimum_source_offset: int = 0,
    maximum_source_offset: int | None = None,
) -> dict[str, object] | None:
    """The native row of Pi's or OMP's session file, inside a turn's bounds, that replies with the marker.

    None when no row inside the bounds replies with the marker exactly once,
    when that row has no id, or when OMP's file does not name exactly one
    session. Two rows that reply raise (`_one_marker_reply`). A record whose
    last line is still being written is read up to that line: the turn's
    bounds are what say which rows count, and a complete row inside them is
    there whatever the provider is writing after it.
    """

    if provider not in {"pi", "omp"}:
        return None
    rows, _state, sessions = _native_assistant_rows(provider, path)
    if rows is None:
        return None
    reply = _one_marker_reply(
        [
            row
            for row in rows
            if int(row["source_offset"]) >= minimum_source_offset
            and (
                maximum_source_offset is None
                # A row is inside the bound when it ends there; a reader that
                # knows only where rows start asks that it starts before it.
                or (
                    int(row["source_end"]) <= maximum_source_offset
                    if row["source_end"] is not None
                    else int(row["source_offset"]) < maximum_source_offset
                )
            )
        ],
        marker,
        alongside=alongside,
    )
    if reply is None or reply["marker_count"] != 1 or not isinstance(reply.get("id"), str) or not reply["id"]:
        return None
    if provider == "omp" and len(sessions) != 1:
        return None
    return {
        "native_message_id": reply["id"],
        "provider_session_id": sessions[0] if sessions else None,
        "source_offset": reply["source_offset"],
        "marker_count": reply["marker_count"],
    }


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
    # The served reply is the native row: the Runtime Host serves a row's first
    # block under the row's id and its later text blocks under ``<id>-text-<n>``.
    native_and_projected_ids_equal = (
        isinstance(native_message_id, str)
        and bool(native_message_id)
        and projected_assistant_event_id is not None
        and str(projected_assistant_event_id) == native_message_id
    )
    native_and_projected_ids_bound = native_and_projected_ids_equal or (
        isinstance(native_message_id, str)
        and bool(native_message_id)
        and projected_assistant_event_id is not None
        and str(projected_assistant_event_id).startswith(f"{native_message_id}-")
    )
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


def _pi_native_tool_observation(path: Path, marker: str, *, alongside: tuple[str, ...] = ()) -> dict[str, object]:
    """Extract one native Pi tool pair and the marker response that follows it.

    ``marker_responses`` are the assistant rows that reply with the marker
    (``zerg.qa.answer_markers``); a row that only names it, such as the
    narration on the row that makes the tool call, is kept apart in
    ``marker_mentions`` and is never the response a pair is bound to.
    """

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
    marker_mentions: list[dict[str, object]] = []
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
                (marker_responses if marker_answered(text, marker, alongside=alongside) else marker_mentions).append(
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
        "marker_mentions": marker_mentions,
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
    reply_alongside: tuple[str, ...] = (),
    native_retained_path: str | None = None,
    provider_response_retained_path: str | None = None,
) -> dict[str, object]:
    """Bind retained native call/result evidence to the live Console turn.

    The receipt's failures are verdicts about the subject. A marker answered by
    more than one native row is not one: the count is this proof's contract
    with the model's reply, so it raises the typed reason instead.
    """
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
            native = _pi_native_tool_observation(native_source, marker, alongside=reply_alongside)
        except (OSError, ValueError, RuntimeError) as exc:
            native = {"error": f"{type(exc).__name__}: {exc}"}
            failures.append("retained_native_source_unreadable")
    marker_responses = native.get("marker_responses")
    marker_responses = marker_responses if isinstance(marker_responses, list) else []
    if len(marker_responses) > 1:
        marker_mentions = native.get("marker_mentions")
        raise MarkerAnswerCountError(
            MARKER_ANSWERED_MORE_THAN_ONCE,
            marker=marker,
            answers=[{"id": response.get("native_message_id")} for response in marker_responses],
            mentions=[{"id": mention.get("native_message_id")} for mention in marker_mentions] if isinstance(marker_mentions, list) else [],
            expected=_SINGLE_REPLY_TURN,
            surface="native assistant row",
        )

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
        and len(marker_responses) == 1
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


def _flush_shipped(receipt: Mapping[str, object]) -> bool:
    """A flush that stopped the Machine Agent, shipped with it stopped, and started it again."""

    shipped = receipt.get("events_shipped")
    return (
        receipt.get("status") == "pass"
        and receipt.get("exit_code") == 0
        and receipt.get("daemon_paused") is True
        and receipt.get("daemon_restarted") is True
        and isinstance(shipped, int)
        and not isinstance(shipped, bool)
        and shipped >= 0
    )


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
    return poll(
        lambda: all(_pid_dead(claim.get("pid")) and _process_group_dead(claim.get("process_group_id")) for claim in claims),
        timeout=timeout,
        interval=0.1,
    )


# How long the engine has to close the provider it ran a turn in, once it has
# reported the run terminal. An adapter that reports first closes its provider
# within about half a second; this is some thirty times that, and far short of
# any wait it stands in front of.
PROVIDER_CLOSE_SECONDS = 15.0
# The engine reported a run terminal, did not say it parked the invocation,
# and the provider it ran the turn in was still alive when that ran out.
PROVIDER_OUTLIVED_TERMINAL_RUN = "provider_outlived_terminal_run"


def _await_provider_closed(claim: Mapping[str, object], longhouse_home: Path, *, turn: str) -> dict[str, object]:
    """Wait until the provider a terminal run ran in is gone, before the Machine Agent is stopped.

    The per-turn flush stops the Machine Agent, and one stopped between a
    run's terminal report and the close of its provider leaves the provider
    running with nobody left to close it: the session's next turn then finds
    a second writer on the provider's session file. So every flush waits here
    first and keeps how long the close took.

    Nothing here depends on which comes first. OpenCode, Cursor and OMP stop
    the provider before they report terminal, and for them this is an
    assertion that returns at once; an adapter that reports first and closes
    after (Codex) is waited for.

    A provider still alive at `PROVIDER_CLOSE_SECONDS` is the engine not
    closing what it reported finished: typed, and declared a wait on the
    engine. Not waited for: a claim that names no process, and one whose
    invocation the engine says it parked (it keeps that provider on purpose,
    for work still pending). The claim is read again at the bound, since the
    engine records an invocation's state after the terminal one.
    """

    pid, group = claim.get("pid"), claim.get("process_group_id")
    if claim.get("invocation_state") == "parked" or not isinstance(pid, int) or not isinstance(group, int):
        return {"provider_closed": None}
    started = time.monotonic()
    if _wait_owned_processes_dead([dict(claim)], timeout=PROVIDER_CLOSE_SECONDS):
        return {"provider_closed": True, "close_wait_seconds": round(time.monotonic() - started, 3)}
    latest = _read_claim(_claim_path(longhouse_home, str(claim.get("run_id")))) or claim
    if latest.get("invocation_state") == "parked":
        return {"provider_closed": None}
    raise expired(
        ConsoleTurnFailure(
            PROVIDER_OUTLIVED_TERMINAL_RUN,
            f"the engine reported the {turn} turn's run terminal and did not close its provider: pid {pid} alive={not _pid_dead(pid)}, "
            f"process group {group} alive={not _process_group_dead(group)} after {PROVIDER_CLOSE_SECONDS:g} s "
            f"(invocation_state={latest.get('invocation_state')}, pending_count={latest.get('pending_count')})",
        ),
        LONGHOUSE_ENGINE,
    )


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
    """Bind the recovery marker and terminal event to retained provider output.

    The recovery reply is the one output message that answers with the marker
    (``zerg.qa.answer_markers``). ``assistant_marker_count`` stays every message
    that names the marker; ``assistant_reply_count`` is how many of them reply,
    and ``marker_event_index`` names the reply. A marker answered by more than
    one message raises the typed reason: that is a counting fault of this
    proof, not a verdict about the interrupt contract.
    """

    if not _interrupt_output_contract_applies(provider):
        return {
            "valid": None,
            "applicable": False,
            "provider": provider,
            "marker": marker,
            "assistant_marker_count": 0,
            "assistant_reply_count": 0,
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
        output_messages: list[dict[str, object]] = []
        terminal_indices: list[int] = []
        for index, event in enumerate(events):
            found = _stream_event(provider, event)
            if found is not None and found[0] == "assistant" and marker in found[1]:
                output_messages.append({"id": index, "text": found[1]})
            elif found == ("end", ""):
                terminal_indices.append(index)
        reply = _one_marker_reply(output_messages, marker, surface="provider output message")
        # The reply must say the marker once; a reply that repeats it is not one.
        marker_index = reply["id"] if reply is not None and reply["marker_count"] == 1 else None
        return {
            "valid": isinstance(marker_index, int) and any(index > marker_index for index in terminal_indices),
            "applicable": True,
            "provider": provider,
            "source_kind": field,
            "source_path": source_path,
            "retained_path": retained_path,
            "marker": marker,
            "assistant_marker_count": len(output_messages),
            "assistant_reply_count": 0 if reply is None else 1,
            "marker_event_index": marker_index,
            "marker_event_type": events[marker_index].get("type") if isinstance(marker_index, int) else None,
            "terminal_event_count": len(terminal_indices),
            "terminal_event_index": next(
                (index for index in terminal_indices if isinstance(marker_index, int) and index > marker_index),
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
        "assistant_reply_count": 0,
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


_TURN_IDENTITY_FIELDS = ("provider", "session_id", "thread_id", "run_id", "adapter", "provider_identity_confirmed")


def turn_identity_mismatches(
    claim: Mapping[str, object], *, provider: str, session_id: str, thread_id: str, run_id: str
) -> dict[str, dict[str, object]]:
    """Each identity field a Console turn's claim did not carry as expected, with both values.

    A lost identity is either a product regression (the turn ran on another
    session or provider thread) or a harness timing gap (identity not yet
    confirmed); which one is only readable from the fields that differed.
    They are ids, never secrets.
    """

    expected: dict[str, object] = {
        "provider": provider,
        "session_id": session_id,
        "thread_id": thread_id,
        "run_id": run_id,
        "adapter": ADAPTERS.get(provider),
        "provider_identity_confirmed": True,
    }
    mismatches = {
        field: {"expected": value, "observed": claim.get(field)}
        for field, value in expected.items()
        if (claim.get(field) is not True if field == "provider_identity_confirmed" else claim.get(field) != value)
    }
    if provider not in ADAPTERS:
        # No adapter can match a provider the Console does not know.
        mismatches["adapter"] = {"expected": f"an adapter for {provider!r}", "observed": claim.get("adapter")}
    return mismatches


def _turn_identity_ok(claim: Mapping[str, object], *, provider: str, session_id: str, thread_id: str, run_id: str) -> bool:
    return not turn_identity_mismatches(claim, provider=provider, session_id=session_id, thread_id=thread_id, run_id=run_id)


def claim_identity_summary(claim: Mapping[str, object]) -> dict[str, object]:
    """The identity a turn's claim carried, for a failure's partial evidence (no argv, no output)."""

    result = claim.get("result")
    return {
        **{field: claim.get(field) for field in _TURN_IDENTITY_FIELDS},
        "state": claim.get("state"),
        "terminal_state": result.get("terminal_state") if isinstance(result, Mapping) else None,
    }


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


def _codex_native_turn_model(source_path: Path) -> str | None:
    """Return the model Codex's app-server bound to its first turn.

    Codex's app-server protocol (engine/src/codex_exec.rs) sends the selected
    model over the ``turn/start`` JSON-RPC call, not the spawned process argv:
    a dedicated Rust test (``codex_app_server_args_omit_model_and_keep_console_
    defaults``) asserts the model must *not* reach argv. The rollout file Codex
    itself writes records that per-turn binding as a ``turn_context`` row whose
    payload carries ``model`` (schemas/transcript_shapes/codex.json).
    """
    try:
        lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, Mapping) or row.get("type") != "turn_context":
            continue
        payload = row.get("payload")
        model = payload.get("model") if isinstance(payload, Mapping) else None
        if isinstance(model, str) and model:
            return model
    return None


def _claim_uses_selected_model(claim: Mapping[str, object], *, provider: str, model: str) -> bool:
    expected = _native_model(provider, model)
    if provider == "codex":
        # Codex never puts the model in argv (see _codex_native_turn_model), so
        # checking argv here always fails closed against a successful run.
        source_path = claim.get("source_path")
        if not isinstance(source_path, str) or not source_path:
            return False
        return _codex_native_turn_model(Path(source_path)) == expected
    result = claim.get("result")
    if provider == "opencode":
        # The Console adapter sends the model in the prompt body, not on argv
        # (`opencode serve` takes no model); the claim records what it sent.
        return isinstance(result, Mapping) and result.get("model") == expected
    argv = result.get("argv") if isinstance(result, Mapping) else None
    if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
        return False
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


def _session_persistence_notices(content: bytes) -> list[dict[str, object]]:
    """OMP's own warnings, in one turn's output, about the session file it was started on.

    OMP raises ``{"type": "notice", "source": "session-persistence"}`` when
    another process still holds that file open, and names the file: a session
    about to continue somewhere else.
    """

    notices: list[dict[str, object]] = []
    for line in content.splitlines():
        if b"session-persistence" not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, Mapping) and event.get("type") == "notice" and event.get("source") == "session-persistence":
            message = str(event.get("message") or "")
            named = re.search(r"\S+\.jsonl", message)
            notices.append({"level": event.get("level"), "session_file": named.group(0) if named else None, "message": message[:500]})
    return notices


def _retain_claim_sources(
    root: Path,
    claims: list[dict[str, Any]],
    environment: Mapping[str, str],
    *,
    complete: bool = False,
) -> list[dict[str, object]]:
    """Verify retained provider bytes before the isolated HOME can disappear.

    The receipt also lists every session-persistence notice in the turns'
    output with the run that raised it, so a forked session is in the
    evidence of the first turn that saw it, whatever the run goes on to do.
    """

    secrets = [value for name, value in environment.items() if value and (name.endswith("_KEY") or name.endswith("_TOKEN"))]
    target_root = root / "provider-sources"
    retained: list[dict[str, object]] = []
    notices: list[dict[str, object]] = []
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
                if field == "stdout_path":
                    notices += [
                        {"turn_index": index, "run_id": claim.get("run_id"), **found} for found in _session_persistence_notices(content)
                    ]
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
    write_json(root / "provider-source-retention.json", {"sources": retained, "session_persistence_notices": notices})
    return retained


def console_lifecycle_assertions(observation: Mapping[str, object]) -> dict[str, bool]:
    return {ASSERTION_ID: all(observation.get(fact) is True for fact in OBSERVED_ACTIVITY)}


def console_steer_assertion(steer: Mapping[str, object] | None) -> bool:
    """A steer entered the running turn: the same run answered it, no new turn
    was created, and the tool it interrupted nothing (its output is present)."""
    return bool(
        steer
        and steer.get("steer_accepted") is True
        and steer.get("same_run_completed") is True
        and steer.get("steer_marker_answered") is True
        and steer.get("no_new_turn") is True
        and steer.get("tool_ran_to_completion") is True
    )


def run_with_console_steer_fault(execute: Callable[[], dict[str, Any]], root: Path) -> dict[str, Any]:
    """Run one steer invocation against a Machine Agent that acknowledges and
    drops the steer. It passes only when the fault fired and the steer
    assertion failed as Longhouse's; anything else is inconclusive or
    undetected. A drop the producer puts down to the model is undetected: the
    factory would retry it and keep it off the findings."""

    receipt_path = root / "qa-fault-receipt.jsonl"
    os.environ["LONGHOUSE_QA_FAULT"] = CONSOLE_STEER_FAULT
    os.environ["LONGHOUSE_QA_FAULT_RECEIPT"] = str(receipt_path)
    try:
        result = execute()
    finally:
        os.environ.pop("LONGHOUSE_QA_FAULT", None)
        os.environ.pop("LONGHOUSE_QA_FAULT_RECEIPT", None)
    fired = receipt_path.is_file() and any(
        json.loads(line).get("fault") == CONSOLE_STEER_FAULT
        for line in receipt_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    target = (result.get("assertions") or {}).get(STEER_ASSERTION_ID)
    # The drop must be why the assertion failed: everything else about the
    # steered turn held, and only the steer never reached it.
    steer = result.get("steer") if isinstance(result.get("steer"), Mapping) else {}
    preconditions_held = all(
        steer.get(fact) is True for fact in ("steer_accepted", "no_new_turn", "same_run_completed", "tool_ran_to_completion")
    )
    outcome = "pass" if target is not False else "model_deviation" if steer.get("model_noncompliance") else "semantic_fail"
    if result.get("failure_code") or not fired or not preconditions_held:
        verdict = "inconclusive"
    else:
        verdict = "rejected" if outcome == "semantic_fail" else "undetected"
    result["negative_control"] = {
        "fault": CONSOLE_STEER_FAULT,
        "target_assertion": STEER_ASSERTION_ID,
        "fault_fired": fired,
        "preconditions_held": preconditions_held,
        "target_outcome": outcome,
        "verdict": verdict,
    }
    result["status"] = "pass" if verdict == "rejected" else "fail"
    write_json(root / "result.json", result)
    return result


# Delivery unknown: the route says to re-send the same client_request_id.
_STEER_SAME_ID_RETRY = ("HTTP 502", "HTTP 503", "HTTP 429")


def _post_steer(
    api_url: str,
    token: str,
    session_id: str,
    *,
    text: str,
    turn_running: Callable[[], bool],
) -> tuple[dict[str, Any], int]:
    """Send one steer, retrying only what is transient.

    A 502/503/429 may still have delivered it, so it is re-sent under the same
    client_request_id. A 409 turn_ended while the turn's claim is still running
    is the start race (the adapter has no steerable turn yet); that receipt is
    already finished as failed and the same id would only replay it, so it is
    re-sent under a fresh id. Anything else is the answer."""

    request_id = f"console-steer-{uuid4()}"
    deadline = time.monotonic() + 30
    attempts = 0
    while True:
        attempts += 1
        try:
            return (
                _request(
                    api_url,
                    token,
                    "POST",
                    f"/api/agents/sessions/{session_id}/input",
                    {"text": text, "intent": "steer", "client_request_id": request_id},
                ),
                attempts,
            )
        except RuntimeError as exc:
            error = str(exc)
            if time.monotonic() >= deadline:
                return {"error": error[:500]}, attempts
            if "HTTP 409" in error and '"turn_ended"' in error and turn_running():
                request_id = f"console-steer-{uuid4()}"
            elif not any(marker in error for marker in _STEER_SAME_ID_RETRY):
                return {"error": error[:500]}, attempts
        time.sleep(1)


# OMP moves a running shell command to a background job when a steer arrives and
# reports the result in a later notice. That result reaches OMP as a `custom`
# message in its own stream, and the served transcript does not carry that
# system row. For these providers the stream the Machine Agent captured is the
# completion evidence.
_STEER_TOOL_RESULT_IN_PROVIDER_STREAM = frozenset({"omp"})


def _run_steer_step(
    *,
    api_url: str,
    token: str,
    session_id: str,
    provider: str,
    longhouse_home: Path,
    claims: list[dict[str, Any]],
    flush_for_projection: Any,
) -> dict[str, Any]:
    """Steer a running Console turn mid-tool and record what landed where."""

    tag = uuid4().hex
    # The shell computes the tool marker, so it appears only in the tool's
    # output and never in its command text: a running or cancelled tool's call
    # event cannot satisfy the completion check.
    tool_prefix = f"LH_{provider.upper()}_STEER_TOOL_"
    tool_marker = f"{tool_prefix}42_{tag}"
    first_marker = f"LH_{provider.upper()}_STEER_FIRST_{tag}"
    steer_marker = f"LH_{provider.upper()}_STEERED_{tag}"
    message = (
        f"Use the shell tool to run `sleep 15 && echo {tool_prefix}$((40+2))_{tag}`, "
        f"then reply with exactly {first_marker} and nothing else."
    )
    request_id = f"console-steer-turn-{uuid4()}"
    turn = _start_turn(api_url=api_url, token=token, session_id=session_id, message=message, request_id=request_id)
    claim_path = _claim_path(longhouse_home, str(turn["run_id"]))
    active = _wait_claim(claim_path, states=frozenset({"spawned", "terminal", "failed"}), timeout=60)
    claims.append(active)
    receipt: dict[str, Any] = {
        "run_id": turn.get("run_id"),
        "turn_id": turn.get("turn_id"),
        "tool_marker": tool_marker,
        "steer_marker": steer_marker,
    }
    if active.get("state") != "spawned":
        receipt.update(status="fail", reason="turn_finished_before_steer")
        return receipt
    # Give the model time to plan and start the tool; a steer that lands during
    # planning is still a valid steer (next boundary), and the tool check below
    # proves nothing in flight was cancelled either way.
    time.sleep(6)
    claims_before = {path.stem for path in claim_path.parent.glob("*.json")}
    steered, attempts = _post_steer(
        api_url,
        token,
        session_id,
        text=f"Change of plan: when the command finishes, reply with exactly {steer_marker} and nothing else.",
        turn_running=lambda: (_read_claim(claim_path) or {}).get("state") == "spawned",
    )
    receipt["steer_attempts"] = attempts
    receipt["steer_response"] = {key: steered.get(key) for key in ("outcome", "disposition", "turn", "error")}
    receipt["steer_accepted"] = steered.get("outcome") == "sent" and steered.get("disposition") == "accepted"
    terminal = _wait_claim(claim_path, states=frozenset({"terminal", "failed"}), timeout=180)
    claims[-1] = terminal
    # The steered run is a process of its own; the factory matches it against the
    # owned processes it retires by this identity.
    receipt.update({key: terminal.get(key) for key in ("pid", "process_group_id", "boot_id", "process_start_time")})
    _wait_turn_terminal(
        api_url=api_url,
        token=token,
        session_id=session_id,
        message=message,
        request_id=request_id,
        turn_id=str(turn["turn_id"]),
        run_id=str(turn["run_id"]),
        timeout=180,
    )
    receipt["same_run_completed"] = _run_completed(terminal) and str(terminal.get("run_id")) == str(turn["run_id"])
    flush_for_projection("console-steer-turn", terminal)
    deviation: ModelDeviation | None = None
    try:
        # A run the engine ended as failed or cancelled has no reply to wait
        # for: the assertion already fails on it, and why is kept as the error.
        _require_run_completed(provider, terminal, turn="steer", marker=steer_marker)
        # How often the model answers the steer is the provider's business:
        # told to reply "when the command finishes", OMP's model answers once as
        # the steer lands and again when the background job's notice arrives.
        # What is held is that the served replies are the provider's own rows.
        answers, answer_correspondence = _wait_turn_served(
            api_url, token, session_id, provider, terminal, steer_marker, turn="steer", steered=True, timeout=120, one_reply=False
        )
        receipt["steer_marker_answered"] = bool(answers)
        receipt["steer_answer_events"] = len(answers)
        receipt["steer_answer_event_ids"] = [event.get("id") for event in answers]
        receipt["steer_answer_correspondence"] = answer_correspondence
    except RuntimeError as exc:
        receipt["steer_marker_answered"] = False
        receipt["steer_marker_error"] = str(exc)[:300]
        deviation = exc if isinstance(exc, ModelDeviation) else None
    # A steer delivered as a follow-up starts its own Console run, and the
    # Machine Agent writes a claim for every run it starts.
    new_runs = sorted({path.stem for path in claim_path.parent.glob("*.json")} - claims_before)
    receipt["new_runs_after_steer"] = new_runs
    receipt["no_new_turn"] = not steered.get("turn") and not new_runs
    # The window the wait above read, so the receipt is judged from the events that ended it.
    events = _archive_events(api_url, token, session_id)
    receipt["tool_ran_to_completion"] = _tool_ran_to_completion(events, tool_marker=tool_marker, first_marker=first_marker)
    if receipt["tool_ran_to_completion"]:
        receipt["tool_result_source"] = "served_events"
    elif provider in _STEER_TOOL_RESULT_IN_PROVIDER_STREAM and _provider_stream_reports_tool_result(
        terminal, tool_marker=tool_marker, first_marker=first_marker
    ):
        receipt["tool_ran_to_completion"] = True
        receipt["tool_result_source"] = "provider_stream"
        receipt["tool_result_stream_path"] = str(terminal.get("stdout_path"))
    if not receipt["tool_ran_to_completion"]:
        # Say what was served, so a failure is explained by the receipt alone.
        receipt["tool_marker_events"] = _tool_marker_rows(events, tool_marker)
        receipt["served_event_roles"] = sorted({str(event.get("role")) for event in events if isinstance(event, dict)})
    receipt["steer_delivery"] = _steer_delivery(events, steer_marker)
    # A reply that carries something else is the model's only once the provider is
    # shown to have had the steer. An accepted steer that was dropped is answered with
    # the first marker too, and that is the fault this cell exists to find.
    if deviation is not None and receipt["steer_delivery"]["delivered_before_final_reply"]:
        held = {**deviation.entry["contract_evidence"], "steer_delivered_before_final_reply": True}
        receipt["model_noncompliance"] = {**deviation.entry, "contract_evidence": held}
    receipt["status"] = "pass" if console_steer_assertion(receipt) else "fail"
    return receipt


def _steer_delivery(events: Iterable[object], steer_marker: str) -> dict[str, object]:
    """Whether the provider's own record holds the steer ahead of its final reply.

    The Runtime Host's ``accepted`` says that Longhouse took the steer, not
    that the provider got it. That is read from the session's served events,
    which the Machine Agent ships from the provider's own record of the
    conversation: a user row that carries the steer's marker is the provider
    recording the steer as input. Every steer provider keeps one (Codex in
    its rollout, Claude in its session file as the hook context that carried
    it, Pi and OMP in their session files, OpenCode in its database), where
    the turn's stream echoes a steer for only some of them.

    ``delivered_before_final_reply`` is True only when that row is served
    ahead of the last assistant reply and is not timed after it. Otherwise
    ``missing`` says what was not found: a steer the provider never recorded,
    one it recorded only once it had finished replying, or two rows whose
    served times cannot be set against each other (`event_time`).
    """

    rows = [event for event in events if isinstance(event, dict)]
    steers = [index for index, row in enumerate(rows) if row.get("role") == "user" and steer_marker in event_text(row)]
    replies = [
        index for index, row in enumerate(rows) if row.get("role") == "assistant" and not row.get("tool_name") and event_text(row).strip()
    ]
    steer = rows[steers[0]] if steers else None
    reply = rows[replies[-1]] if replies else None
    if steer is None:
        missing: str | None = "no served user event carries the steer"
    elif reply is None or replies[-1] < steers[0]:
        missing = "no served assistant reply follows the steer"
    else:
        steer_at, reply_at = event_time(steer), event_time(reply)
        if steer_at is None or reply_at is None:
            missing = "the served times do not order the steer and the final reply"
        else:
            missing = None if steer_at <= reply_at else "the steer is timed after the final reply"
    return {
        "source": "served_events",
        "steer_event_id": steer.get("id") if steer else None,
        "steer_event_at": steer.get("timestamp") if steer else None,
        "final_reply_event_id": reply.get("id") if reply else None,
        "final_reply_at": reply.get("timestamp") if reply else None,
        "delivered_before_final_reply": missing is None,
        "missing": missing,
    }


def _tool_ran_to_completion(events: Iterable[object], *, tool_marker: str, first_marker: str) -> bool:
    """Did the steered turn's command finish and report its result?

    The command computes its marker (`$((40+2))`), so the literal can appear only
    in the command's output or in a reply that quotes it. Parsers keep tool output
    in `tool_output_text` and leave `content_text` empty, so reading `event_text`
    alone never saw it and a steer that worked failed this check.
    """

    for event in events:
        if not isinstance(event, dict) or event.get("role") == "user":
            continue
        text = f"{event_text(event)}\n{event.get('tool_output_text') or ''}"
        if tool_marker in text and first_marker not in text:
            return True
    return False


def _provider_stream_reports_tool_result(claim: Mapping[str, object], *, tool_marker: str, first_marker: str) -> bool:
    """Did the provider's own stream hand the steered command's result to the model?

    Reads the run's captured stdout for a message the provider itself injected
    (a background job's `custom` notice, or a `toolResult`) that carries the
    computed marker. The user's and the assistant's messages never count, so
    neither the prompt nor a reply that quotes the marker can satisfy it.
    """

    path = Path(str(claim.get("stdout_path") or ""))
    if not path.is_file():
        return False
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if tool_marker not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            message = record.get("message") if isinstance(record, dict) else None
            if not isinstance(message, dict) or message.get("role") not in {"custom", "toolResult"}:
                continue
            content = json.dumps(message.get("content"))
            if tool_marker in content and first_marker not in content:
                return True
    return False


def _tool_marker_rows(events: Iterable[object], tool_marker: str) -> list[dict[str, object]]:
    return [
        {
            "role": event.get("role"),
            "tool_name": event.get("tool_name"),
            "origin": event.get("event_origin"),
            "in_text": tool_marker in event_text(event),
            "in_tool_output": tool_marker in str(event.get("tool_output_text") or ""),
        }
        for event in events
        if isinstance(event, dict) and tool_marker in f"{event_text(event)}\n{event.get('tool_output_text') or ''}"
    ][:5]


def _observation_from_receipts(
    *,
    dispatch: Mapping[str, object],
    binding: Mapping[str, object],
    interrupt: Mapping[str, object],
    cleanup: Mapping[str, object],
    steer: Mapping[str, object] | None = None,
) -> dict[str, Any]:
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
    observation: dict[str, Any] = {
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
    if steer and steer.get("model_noncompliance"):
        attach(observation, STEER_ASSERTION_ID, dict(steer["model_noncompliance"]))
    return observation


def _run_live(provider: str, variant: str, args: argparse.Namespace, root: Path, *, steer: bool = False) -> dict[str, Any]:
    if variant != SUPPORTED_VARIANT:
        raise RuntimeError(f"{provider} requires variant={SUPPORTED_VARIANT}")
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
    require_disposable_runtime(api_url)
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
        # Pi and OMP are asked for the stored context value and then the marker,
        # so their replies carry both: on one line, on two, or run together.
        reply_alongside: tuple[str, ...] = (context_marker,) if provider in {"pi", "omp"} else ()
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
        _require_run_completed(provider, first_claim, turn="first", marker=marker, alongside=reply_alongside)
        mismatches = turn_identity_mismatches(
            first_claim,
            provider=provider,
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
        )
        if mismatches:
            raise RuntimeError(
                f"adapter claim did not preserve exact Console identity: {json.dumps(mismatches, sort_keys=True, default=str)}"
            )
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
            _native_marker_evidence(provider, first_native_source_path, marker, alongside=reply_alongside)
            if provider in {"pi", "omp"} and first_native_source_path is not None
            else None
        )
        if provider in {"pi", "omp"} and first_native_response is None:
            # A reply the model never gave is said as that, from the turn's stream. What is
            # left is a stream that answers beside a session record that does not.
            account = _turn_account(provider, first_claim, marker, alongside=reply_alongside)
            if account is not None and not account["answering_replies"]:
                raise _unanswered_turn_error(provider, first_claim, marker, turn="first", alongside=reply_alongside)
            raise RuntimeError(f"first Console {provider} turn has no native assistant row that answers with its marker once")
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
        first_provider_close = _await_provider_closed(first_claim, longhouse_home, turn="first")
        flush_receipt = {**_retain_flush_diagnostics(shipper.flush("console-first-turn")), "provider_close": first_provider_close}
        write_json(root / "transcript-flush-receipt.json", flush_receipt)

        def flush_for_projection(label: str, claim: Mapping[str, object]) -> dict[str, object]:
            if shipper is None:
                raise RuntimeError(f"Console transcript shipper unavailable for {label}")
            closed = _await_provider_closed(claim, longhouse_home, turn=label.removeprefix("console-").removesuffix("-turn"))
            receipt = {**_retain_flush_diagnostics(shipper.flush(label)), "provider_close": closed}
            turn_receipts = flush_receipt.setdefault("turns", {})
            if not isinstance(turn_receipts, dict):
                raise RuntimeError("Console transcript flush receipt has an invalid turns field")
            turn_receipts[label] = receipt
            write_json(root / "transcript-flush-receipt.json", flush_receipt)
            if not _flush_shipped(receipt):
                raise RuntimeError(f"Console transcript flush failed before {label} projection")
            return receipt

        def completed_turn(turn: str, message: str, turn_marker: str, **served: Any) -> tuple[dict[str, Any], list[dict[str, Any]], Any]:
            """Run one turn that has to complete; return its claim, the served events that answer and their account.

            The turn is started, its claim held to ``run_completed``, the Runtime
            Host seen to end it, the engine flushed once its provider is gone,
            and the served reply waited for.
            """

            request_id = f"console-{turn}-{uuid4()}"
            started = _start_turn(api_url=api_url, token=token, session_id=session_id, message=message, request_id=request_id)
            claim = _wait_claim(_claim_path(longhouse_home, str(started["run_id"])), states=frozenset({"terminal", "failed"}))
            claims.append(claim)
            _require_run_completed(provider, claim, turn=turn, marker=turn_marker, alongside=served.get("alongside", ()))
            _wait_turn_terminal(
                api_url=api_url,
                token=token,
                session_id=session_id,
                message=message,
                request_id=request_id,
                turn_id=str(started["turn_id"]),
                run_id=str(started["run_id"]),
            )
            flush_for_projection(f"console-{turn}-turn", claim)
            return (claim, *_wait_turn_served(api_url, token, session_id, provider, claim, turn_marker, turn=turn, **served))

        flush_receipt["turns"] = {"console-first-turn": flush_receipt.copy()}
        write_json(root / "transcript-flush-receipt.json", flush_receipt)
        marker_count = provider_response_evidence.get("provider_response_marker_count") if provider_response_evidence is not None else None
        flush_ok = _flush_shipped(flush_receipt)
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
            raise _unanswered_turn_error(provider, first_claim, marker, turn="first", alongside=reply_alongside)
        first_events, first_correspondence = _wait_turn_served(
            api_url, token, session_id, provider, first_claim, marker, turn="first", alongside=reply_alongside
        )
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
            # The provider's own rows that answered and the served events that
            # carry them, by id; None for an adapter whose served ids are not
            # the provider's (`_SERVED_IDS_NOT_NATIVE`), where the reply was counted.
            "answer_correspondence": first_correspondence,
        }
        if pi_tool_evidence is not None:
            binding["native_tool_evidence"] = pi_tool_evidence
        write_json(root / "provider-response-binding-receipt.json", binding)

        if provider in CAN_RESUME:
            resume_marker = f"LH_{provider.upper()}_RESUME_{uuid4().hex}"
            resume_message = f"Reply with exactly {resume_marker} and nothing else."
            if provider in {"pi", "omp"}:
                resume_message = _omp_continuation_prompt(context_marker, resume_marker)
            resume_claim, resume_events, resume_correspondence = completed_turn(
                "resume",
                resume_message,
                resume_marker,
                alongside=reply_alongside,
                # Pi's and OMP's continuation asks for a value an earlier turn stored, so a
                # reply without it is the model's only when the resumed session held that turn.
                context_marker=context_marker if provider in {"pi", "omp"} else None,
            )
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
                    alongside=reply_alongside,
                    minimum_source_offset=first_native_source_size,
                    maximum_source_offset=second_native_source_size,
                )
                if resume_native_response is None:
                    raise RuntimeError(
                        f"second Console {provider} turn has no native assistant row that answers with its marker once "
                        "inside its pre-interrupt boundary"
                    )
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
            dispatch["resume_run_id"] = resume_claim.get("run_id")
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
                    "answer_correspondence": resume_correspondence,
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

        # Before the interrupt step: OMP's interrupt evidence requires the
        # post-interrupt turn to be the last output in the retained source.
        steer_receipt: dict[str, Any] | None = None
        if steer:
            steer_receipt = _run_steer_step(
                api_url=api_url,
                token=token,
                session_id=session_id,
                provider=provider,
                longhouse_home=longhouse_home,
                claims=claims,
                flush_for_projection=flush_for_projection,
            )
            write_json(root / "steer-contract-receipt.json", steer_receipt)

        interrupt_marker = f"LH_{provider.upper()}_INTERRUPT_{uuid4().hex}"
        interrupt_message = (
            "Use the shell tool to run `sleep 8` in the foreground (not as a named or background job), "
            f"then reply with exactly {interrupt_marker} and nothing else."
        )
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
        # The interrupted request stays in the transcript; a small model can resume it
        # (OMP 2026-10-09 15:09Z re-ran `sleep 8` and answered the old marker), so the
        # follow-up cancels it explicitly. Interrupt delivery itself is asserted above.
        post_message = (
            f"The previous request was cancelled: do not run it or any other command. Reply with exactly {post_marker} and nothing else."
        )
        post_claim, post_events, _post_correspondence = completed_turn("post-interrupt", post_message, post_marker)
        interrupt_receipt = {
            "status": "pass" if interrupted.get("interrupt_dispatched") is True and cancelled and process_dead else "fail",
            "expectation": "supported",
            "interrupt_dispatched": interrupted.get("interrupt_dispatched") is True,
            "active_run_cancelled": cancelled,
            "provider_process_dead": process_dead,
            "post_interrupt_turn_completed": _run_completed(post_claim),
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
        write_json(root / "interrupt-contract-receipt.json", interrupt_receipt)

        naturally_dead = _wait_owned_processes_dead(claims)
        retained_sources = _retain_claim_sources(root, claims, environment, complete=True)
        if _interrupt_output_contract_applies(provider):
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
                    reply_alongside=reply_alongside,
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
            steer=steer_receipt,
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
        steer_passed = console_steer_assertion(steer_receipt)
        if provider == "pi":
            assertion = assertion and observation.get("pi_tool_enabled") is True
        return {
            "assertions": {STEER_ASSERTION_ID: steer_passed} if steer else {ASSERTION_ID: assertion},
            "steer": steer_receipt,
            "provider_binary": binary_receipt,
            "observation": observation,
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


def run_console_cell(producer: Producer, args: argparse.Namespace, body: Callable[[Run], object], **identity: Any) -> dict[str, Any]:
    """Run one Console cell through its producer; under ``--negative-control``, against the Machine Agent that drops the steer."""

    def execute() -> dict[str, Any]:
        return producer.execute(args.evidence_root, body, **identity)

    if getattr(args, "negative_control", None):
        return run_with_console_steer_fault(execute, args.evidence_root.resolve())
    return execute()


def negative_control_needs_steer(steer_variants: Iterable[str]) -> Callable[[argparse.Namespace], str | None]:
    """The refusal of a negative control asked of a cell that is not a steer cell."""

    variants = frozenset(steer_variants)
    return lambda args: "negative_control_requires_steer_variant" if args.negative_control and args.variant not in variants else None


def _refuse(args: argparse.Namespace) -> str | None:
    if args.variant in STEER_VARIANTS.values() and args.variant != STEER_VARIANTS.get(args.provider):
        return "steer_variant_provider_mismatch"
    return negative_control_needs_steer(STEER_VARIANTS.values())(args)


PRODUCER = Producer(
    REGISTRATION,
    artifact_kind="provider_console_lifecycle_result",
    failure_code="provider_console_lifecycle_failed",
    needs=("provider", "variant", "evidence_root", "repo_root", "engine", "longhouse_cli", "provider_bin", "provider_version", "model"),
    binaries=(("engine", "engine_missing"), ("longhouse_cli", "longhouse_cli_missing"), ("provider_bin", "provider_bin_missing")),
    variants=(SUPPORTED_VARIANT, *STEER_VARIANTS.values()),
    variant_required=False,
    negative_controls=(CONSOLE_STEER_FAULT,),
    refuse=_refuse,
)


def run_console_lifecycle(args: argparse.Namespace) -> dict[str, Any]:
    steer = args.variant in STEER_VARIANTS.values()

    def body(run: Run) -> None:
        reached = _run_live(args.provider, SUPPORTED_VARIANT if steer else args.variant, args, run.root, steer=steer)
        run.observation = reached.pop("observation")
        run.assertions = reached.pop("assertions")
        run.fields.update(reached)

    return run_console_cell(
        PRODUCER,
        args,
        body,
        provider=args.provider,
        # The steer cell's authored variant is null.
        variant=None if steer else args.variant,
        scenario_id=_scenario_id(args.provider),
    )


def main(argv: list[str] | None = None) -> int:
    return PRODUCER.main(run_console_lifecycle, argv)


if __name__ == "__main__":
    raise SystemExit(main())
