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
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from zerg.qa.provider_native_resume import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.provider_native_resume import RUNTIME_API_URL_ENV
from zerg.qa.provider_native_resume import TranscriptShipper
from zerg.qa.provider_native_resume import _isolated_provider_home
from zerg.qa.provider_native_resume import _start_transcript_shipper
from zerg.qa.resume_assurance import ProducerRegistration

PROVIDERS = ("codex", "claude", "opencode", "cursor")
INTERRUPT_SUPPORTED = frozenset({"claude", "opencode", "cursor"})
INTERRUPT_UNSUPPORTED = frozenset({"codex"})
ASSERTION_ID = "console_adapter_release_contract_preserved"
SUPPORTED_VARIANT = "interrupt_supported"
UNSUPPORTED_VARIANT = "interrupt_unsupported"
SCENARIO_IDS = tuple(f"{provider}_console_adapter_lifecycle" for provider in PROVIDERS)
OBSERVED_ACTIVITY = (
    "adapter_dispatch_started",
    "stock_provider_response_bound",
    "exact_session_thread_run_binding",
    "transcript_converged_exactly_once",
    "interrupt_contract_preserved",
    "post_interrupt_sendable",
    "no_orphan_provider_processes",
)
RECEIPT_FILES = (
    "provider-binary-receipt.json",
    "adapter-dispatch-receipt.json",
    "provider-response-binding-receipt.json",
    "interrupt-contract-receipt.json",
    "cleanup-receipt.json",
)
PROVIDER_BIN_ENV = {
    "codex": "LONGHOUSE_CODEX_BIN",
    "claude": "LONGHOUSE_CLAUDE_BIN",
    "opencode": "LONGHOUSE_OPENCODE_BIN",
    "cursor": "LONGHOUSE_CURSOR_BIN",
}
ADAPTERS = {
    "codex": "codex_exec",
    "claude": "claude_print",
    "opencode": "opencode_run",
    "cursor": "cursor_print",
}
CAN_RESUME = frozenset({"codex", "claude", "opencode", "cursor"})
_VERSION_PATTERNS = {
    "codex": re.compile(r"^codex-cli (?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$"),
    "claude": re.compile(r"^(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?) \(Claude Code\)$"),
    "opencode": re.compile(r"^(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$"),
    "cursor": re.compile(r"^(?P<version>\d{4}\.\d{2}\.\d{2}(?:-[0-9A-Za-z.-]+)?)$"),
}

REGISTRATION = ProducerRegistration(
    producer_id="provider.console_lifecycle.v1",
    producer_revision=2,
    scenario_id=SCENARIO_IDS[0],
    scenario_ids=SCENARIO_IDS,
    scenario_revision=1,
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
        "provider_response_binding_receipt",
        "interrupt_contract_receipt",
        "cleanup_receipt",
    ),
    required_cleanup=(
        "provider_process_dead",
        "process_group_dead",
        "no_orphan_provider_processes",
    ),
    implementation="server/zerg/qa/provider_console_lifecycle.py",
    oracle_source="server/zerg/qa/provider_console_lifecycle.py",
    oracle_entrypoint="console_lifecycle_assertions",
    executable_module="zerg.qa.provider_console_lifecycle",
    provider_artifact_required=True,
    subject_kind="provider_release",
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _artifact_manifest(root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for name in RECEIPT_FILES:
        path = root / name
        if path.is_file():
            rows.append({"path": name, "size": path.stat().st_size, "sha256": _sha256_file(path)})
    return rows


def _expected_variant(provider: str) -> str:
    return SUPPORTED_VARIANT if provider in INTERRUPT_SUPPORTED else UNSUPPORTED_VARIANT


def _scenario_id(provider: str) -> str:
    return f"{provider}_console_adapter_lifecycle"


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


def _create_session(*, api_url: str, token: str, provider: str, device_id: str, cwd: Path) -> dict[str, Any]:
    payload = {
        "provider": provider,
        "device_id": device_id,
        "cwd": str(cwd),
        "project": f"provider-console-{provider}",
        "display_name": f"{provider} Console release qualification",
        "launch_surface": "test",
    }
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
    result = _request(
        api_url,
        token,
        "POST",
        f"/api/agents/sessions/{session_id}/turns",
        {"message": message, "client_request_id": request_id},
    )
    if result.get("state") not in {"queued", "starting", "active", "completed"}:
        raise RuntimeError(f"Console turn was not accepted: {result}")
    if not isinstance(result.get("run_id"), str) or not result["run_id"]:
        raise RuntimeError(f"Console turn returned no stable run_id: {result}")
    return result


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


def _event_text(event: Mapping[str, object]) -> str:
    return str(event.get("content_text") or event.get("content") or "")


def _assistant_marker_events(api_url: str, token: str, session_id: str, marker: str) -> list[dict[str, Any]]:
    result = _request(api_url, token, "GET", f"/api/agents/sessions/{session_id}/events?limit=200")
    events = result.get("events") if isinstance(result.get("events"), list) else []
    return [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("role") == "assistant"
        and event.get("event_origin", "durable") == "durable"
        and marker in _event_text(event)
    ]


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
        if last_count > 1:
            raise RuntimeError(f"assistant marker converged {last_count} times instead of exactly once")
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


def _claim_output_evidence(claim: Mapping[str, object], marker: str) -> dict[str, object] | None:
    for key in ("stdout_path", "source_path"):
        raw = claim.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            content = Path(raw).read_text(encoding="utf-8", errors="replace")
            marker_count = content.count(marker)
            if marker_count:
                return {
                    "provider_response_source_kind": key,
                    "provider_response_marker_count": marker_count,
                    "provider_response_excerpt": _bounded_marker_excerpt(content, marker),
                }
        except OSError:
            continue
    return None


def _pid_dead(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _process_group_dead(pgid: object) -> bool:
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        return True
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _wait_owned_processes_dead(claims: list[dict[str, Any]], timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(_pid_dead(claim.get("pid")) and _process_group_dead(claim.get("process_group_id")) for claim in claims):
            return True
        time.sleep(0.1)
    return all(_pid_dead(claim.get("pid")) and _process_group_dead(claim.get("process_group_id")) for claim in claims)


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


def _prepare_claude_hook(home: Path, environment: dict[str, str]) -> None:
    provider_home = Path(environment.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    hook_dir = provider_home / "hooks"
    hook_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    hook = hook_dir / "longhouse-hook.sh"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(0o700)
    settings_path = provider_home / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        settings["hooks"] = hooks
    entries = hooks.setdefault("SessionStart", [])
    if not isinstance(entries, list):
        entries = []
        hooks["SessionStart"] = entries
    entries.append({"hooks": [{"type": "command", "command": str(hook)}]})
    _write_json(settings_path, settings)
    environment["CLAUDE_CONFIG_DIR"] = str(provider_home)


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
        ),
    }


def _run_live(provider: str, variant: str, args: argparse.Namespace, root: Path) -> dict[str, Any]:
    if variant != _expected_variant(provider):
        raise RuntimeError(f"{provider} requires variant={_expected_variant(provider)}")
    home = _isolated_provider_home()
    environment = dict(os.environ)
    environment["HOME"] = str(home)
    environment["LONGHOUSE_ENGINE_BIN"] = str(args.engine)
    environment["LONGHOUSE_ORIGIN_KIND"] = "test_or_canary"
    environment["LONGHOUSE_LAUNCH_ACTOR"] = "automation"
    environment["LONGHOUSE_LAUNCH_SURFACE"] = "test"
    environment[PROVIDER_BIN_ENV[provider]] = str(args.provider_bin)
    environment.setdefault("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    environment.setdefault("CURSOR_HOME", str(home / ".cursor"))
    if provider == "claude":
        _prepare_claude_hook(home, environment)

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
    _write_json(root / "provider-binary-receipt.json", binary_receipt)

    # HOME is already unique to one mount-isolated qualification. Keep
    # ephemeral IPC paths compact: Linux Unix sockets cap the complete path
    # near 108 bytes, so semantic directory names plus another UUID make the
    # Console harness deployment-path dependent for no isolation benefit.
    runtime_root, engine_evidence, workspace, longhouse_home = _console_runtime_paths(home)
    engine_evidence.mkdir(mode=0o700, parents=True)
    workspace.mkdir(mode=0o700, parents=True)
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
    shipper: TranscriptShipper | None = None
    cleanup_written = False
    try:
        shipper = _start_transcript_shipper(
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
        )
        session_id = str(created["session_id"])
        thread_id = str(created["thread_id"])
        marker = f"LH_{provider.upper()}_CONSOLE_{uuid4().hex}"
        message = f"Reply with exactly {marker} and nothing else."
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
        first_events = _wait_exact_assistant_marker(api_url, token, session_id, marker)
        provider_response_evidence = _claim_output_evidence(first_claim, marker)
        if provider_response_evidence is None:
            raise RuntimeError("stock provider output did not contain the qualification marker")
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
            "argv": (first_claim.get("result") or {}).get("argv"),
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
            **provider_response_evidence,
            "bound_assistant_event_id": first_events[0].get("id"),
            "bound_assistant_event_origin": first_events[0].get("event_origin", "durable"),
            "bound_assistant_event_excerpt": _event_text(first_events[0])[:512],
            "bound_assistant_marker_count": _event_text(first_events[0]).count(marker),
            "marker_in_provider_response": True,
            "marker_in_bound_assistant_event": True,
            "assistant_event_count": len(first_events),
            "transcript_converged_exactly_once": len(first_events) == 1,
        }
        _write_json(root / "adapter-dispatch-receipt.json", dispatch)
        _write_json(root / "provider-response-binding-receipt.json", binding)

        if provider in CAN_RESUME:
            resume_marker = f"LH_{provider.upper()}_RESUME_{uuid4().hex}"
            resume = _start_turn(
                api_url=api_url,
                token=token,
                session_id=session_id,
                message=f"Reply with exactly {resume_marker} and nothing else.",
                request_id=f"console-resume-{uuid4()}",
            )
            resume_claim = _wait_claim(
                _claim_path(longhouse_home, str(resume["run_id"])),
                states=frozenset({"terminal", "failed"}),
            )
            claims.append(resume_claim)
            _wait_exact_assistant_marker(api_url, token, session_id, resume_marker)
            if first_claim.get("provider_thread_id") is None or resume_claim.get("provider_thread_id") != first_claim.get(
                "provider_thread_id"
            ):
                raise RuntimeError("second Console turn did not preserve the native provider thread")
            dispatch["resume_run_id"] = resume.get("run_id")
            dispatch["native_thread_resumed"] = True
            _write_json(root / "adapter-dispatch-receipt.json", dispatch)

        interrupt_marker = f"LH_{provider.upper()}_INTERRUPT_{uuid4().hex}"
        interrupt_turn = _start_turn(
            api_url=api_url,
            token=token,
            session_id=session_id,
            message=("Use the shell tool to run `sleep 8`, then reply with exactly " f"{interrupt_marker} and nothing else."),
            request_id=f"console-interrupt-{uuid4()}",
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
            cancelled = (terminal.get("result") or {}).get("terminal_state") == "run_cancelled"
            process_dead = _wait_owned_processes_dead([terminal])
            post_marker = f"LH_{provider.upper()}_POST_INTERRUPT_{uuid4().hex}"
            post = _start_turn(
                api_url=api_url,
                token=token,
                session_id=session_id,
                message=f"Reply with exactly {post_marker} and nothing else.",
                request_id=f"console-post-interrupt-{uuid4()}",
            )
            post_claim = _wait_claim(
                _claim_path(longhouse_home, str(post["run_id"])),
                states=frozenset({"terminal", "failed"}),
            )
            claims.append(post_claim)
            _wait_exact_assistant_marker(api_url, token, session_id, post_marker)
            interrupt_receipt = {
                "status": "pass" if interrupted.get("interrupt_dispatched") is True and cancelled and process_dead else "fail",
                "expectation": "supported",
                "interrupt_dispatched": interrupted.get("interrupt_dispatched") is True,
                "active_run_cancelled": cancelled,
                "provider_process_dead": process_dead,
                "post_interrupt_turn_completed": (post_claim.get("result") or {}).get("terminal_state") == "run_completed",
            }
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
            _wait_exact_assistant_marker(api_url, token, session_id, interrupt_marker, timeout=60)
            post_marker = f"LH_{provider.upper()}_AFTER_UNSUPPORTED_{uuid4().hex}"
            post = _start_turn(
                api_url=api_url,
                token=token,
                session_id=session_id,
                message=f"Reply with exactly {post_marker} and nothing else.",
                request_id=f"console-after-unsupported-{uuid4()}",
            )
            post_claim = _wait_claim(
                _claim_path(longhouse_home, str(post["run_id"])),
                states=frozenset({"terminal", "failed"}),
            )
            claims.append(post_claim)
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
        _write_json(root / "interrupt-contract-receipt.json", interrupt_receipt)

        naturally_dead = _wait_owned_processes_dead(claims)
        cleanup = {
            "status": "pass" if naturally_dead else "fail",
            "provider_process_dead": all(_pid_dead(claim.get("pid")) for claim in claims),
            "process_group_dead": all(_process_group_dead(claim.get("process_group_id")) for claim in claims),
            "orphan_count": sum(
                not (_pid_dead(claim.get("pid")) and _process_group_dead(claim.get("process_group_id"))) for claim in claims
            ),
        }
        _write_json(root / "cleanup-receipt.json", cleanup)
        cleanup_written = True
        observation = _observation_from_receipts(
            dispatch=dispatch,
            binding=binding,
            interrupt=interrupt_receipt,
            cleanup=cleanup,
        )
        assertion = console_lifecycle_assertions(observation)[ASSERTION_ID]
        return {
            "schema_version": 1,
            "artifact_kind": "provider_console_lifecycle_result",
            "producer": REGISTRATION.to_dict(),
            "provider": provider,
            "variant": variant,
            "scenario_id": _scenario_id(provider),
            "scenario_revision": 1,
            "evidence_class": "live_token",
            "generated_at": _now(),
            "status": "pass" if assertion else "fail",
            "assertions": {ASSERTION_ID: assertion},
            "provider_binary": binary_receipt,
            "observation": observation,
            "artifact_manifest": _artifact_manifest(root),
        }
    finally:
        _force_cleanup(claims)
        if shipper is not None:
            shipper.stop()
        if not cleanup_written:
            cleanup = {
                "status": "fail",
                "provider_process_dead": all(_pid_dead(claim.get("pid")) for claim in claims),
                "process_group_dead": all(_process_group_dead(claim.get("process_group_id")) for claim in claims),
                "orphan_count": sum(
                    not (_pid_dead(claim.get("pid")) and _process_group_dead(claim.get("process_group_id"))) for claim in claims
                ),
            }
            _write_json(root / "cleanup-receipt.json", cleanup)


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
            "scenario_revision": 1,
            "evidence_class": "live_token",
            "generated_at": _now(),
            "status": "fail",
            "failure_code": "provider_console_lifecycle_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "artifact_manifest": _artifact_manifest(root),
        }
    _write_json(root / "result.json", result)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
