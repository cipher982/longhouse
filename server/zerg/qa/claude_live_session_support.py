"""Shared live managed-Claude session primitives for continuous QA producers.

Every producer in this file's family (``claude_turn_boundary_quiescent``,
``claude_coordination_awareness_create``,
``claude_coordination_awareness_post_compaction``,
``claude_coordination_directed_input``) launches a real ``longhouse claude``
Helm session inside the provider-qualification sandbox and drives it against
a real Runtime Host. Factoring the launch/send/observe/API primitives here
keeps the subtle parts -- Claude's first-run prompt handling, channel
readiness, served-activity polling -- defined exactly once instead of
diverging across four otherwise-identical producer files.

The sandbox's isolated ``$HOME`` has no ambient Machine Agent. ``longhouse
claude`` (like every other user-facing entrypoint) discovers the Runtime Host
purely from local machine config -- ``~/.longhouse/machine/state.json`` plus a
device-token file, read by ``zerg.services.shipper.token`` -- never from the
factory's ``LONGHOUSE_RUNTIME_API_URL`` / ``LONGHOUSE_RUNTIME_AGENTS_TOKEN``
environment. Every scenario here must therefore provision that local machine
identity and run the same disposable Machine Agent (``longhouse-engine
connect``) a real installed machine runs -- see
``zerg.qa.provider_native_resume._start_transcript_shipper`` -- before a
managed session can be created, issue a coordination token, or report served
activity facts at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Callable

from zerg.qa.managed_claude_live import assistant_transcript_contains
from zerg.qa.managed_claude_live import channel_send
from zerg.qa.managed_claude_live import find_channel_session_id
from zerg.qa.managed_claude_live import read_provider_session_id
from zerg.qa.managed_claude_live import strip_terminal_controls
from zerg.qa.managed_claude_live import transcript_lookup_id
from zerg.qa.managed_claude_live import transcript_paths
from zerg.qa.managed_claude_live import wait_for_channel_ready
from zerg.qa.provider_native_resume import TranscriptShipper
from zerg.qa.provider_native_resume import _start_transcript_shipper
from zerg.qa.pty_session import ProviderPtySession

_RUNTIME_HOST_USER_AGENT = "LonghouseProviderFactory/1.0"


class ScenarioError(RuntimeError):
    """A scenario postcondition was not observed in time (not a bare crash)."""


class RuntimeHostHTTPError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"Runtime Host HTTP {status}: {detail}")


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def artifact_manifest(root: Path) -> list[dict[str, Any]]:
    return [
        {"path": path.relative_to(root).as_posix(), "size": path.stat().st_size, "sha256": sha256_file(path)}
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "result.json"
    ]


def wait_until(predicate: Callable[[], Any], *, timeout: float, description: str) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    raise ScenarioError(f"timed out waiting for {description}")


def terminal_text(path: Path) -> str:
    try:
        return strip_terminal_controls(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def api_json(
    api_url: str,
    token: str,
    path: str,
    *,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Call ``/api/agents/<path>`` the same way the other native producers do.

    Mirrors ``codex_native_resume._api_json`` / ``provider_native_resume._api_json``
    (urllib, not httpx) so this family of producers has one consistent Runtime
    Host client convention. ``extra_headers`` exists for callers authenticating
    with a session-scoped coordination token that also needs the current
    session identity header (``X-Longhouse-Session-Id``) -- see
    ``zerg.services.managed_session_env.CURRENT_SESSION_HEADER``.
    """

    data = json.dumps(json_body).encode("utf-8") if json_body is not None else (b"" if method == "POST" else None)
    headers = {
        "X-Agents-Token": token,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": _RUNTIME_HOST_USER_AGENT,
    }
    headers.update(extra_headers or {})
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
        raise RuntimeHostHTTPError(exc.code, detail) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Runtime Host returned a non-object")
    return payload


def api_json_tolerant(api_url: str, token: str, path: str, *, method: str = "GET") -> dict[str, Any] | None:
    """Like ``api_json``, but transient/absence responses become ``None`` instead of raising.

    Used by polling loops (served-activity, directed-input visibility) where a
    404/503 mid-poll is an ordinary "not yet" rather than a scenario failure.
    """

    try:
        return api_json(api_url, token, path, method=method)
    except RuntimeHostHTTPError as exc:
        if exc.status in {404, 429, 503}:
            return None
        raise
    except (OSError, urllib.error.URLError):
        return None


_SAFE_LOCAL_CONTROL_FIELDS = (
    "authority_class",
    "provider",
    "session_id",
    "provider_session_id",
    "run_id",
    "connection_id",
    "lease_generation",
    "state",
    "terminal_attached",
    "bridge_status",
    "granted_operations",
    "source",
    "observed_at",
)


def local_managed_control_snapshot(longhouse_home: Path, session_id: str) -> dict[str, Any] | None:
    """Return a credential-free local control snapshot for one managed session."""

    status_path = longhouse_home / "agent" / "engine-status.json"
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    machine_evidence = payload.get("machine_evidence")
    control = machine_evidence.get("control") if isinstance(machine_evidence, dict) else None
    controls = [
        {field: item.get(field) for field in _SAFE_LOCAL_CONTROL_FIELDS if field in item}
        for item in (control if isinstance(control, list) else [])
        if isinstance(item, dict) and item.get("session_id") == session_id
    ]
    leases = [
        {field: item.get(field) for field in ("session_id", "provider", "state", "bridge_status", "observed_at") if field in item}
        for item in (payload.get("managed_sessions") if isinstance(payload.get("managed_sessions"), list) else [])
        if isinstance(item, dict) and item.get("session_id") == session_id
    ]
    sessions = [
        {
            "session_id": item.get("session_id"),
            "provider": item.get("provider"),
            "state": item.get("state"),
            "control_path": item.get("control_path"),
            "reason_codes": item.get("reason_codes"),
        }
        for item in (payload.get("sessions") if isinstance(payload.get("sessions"), list) else [])
        if isinstance(item, dict) and item.get("session_id") == session_id
    ]
    return {
        "status_path": str(status_path),
        "controls": controls,
        "managed_sessions": leases,
        "sessions": sessions,
    }


def local_managed_control_fact(longhouse_home: Path, session_id: str) -> dict[str, Any] | None:
    """Return the safe, attached control fact emitted by the disposable Machine Agent.

    ``engine-status.json`` is the local projection immediately upstream of the
    Runtime Host heartbeat.  Polling it avoids using the timeline wall as a
    control-readiness API: the wall is a bounded, eventually-consistent list
    and a newly registered qualification session may legitimately not be in
    its first 200 cards yet.  Select fields instead of copying the raw status
    document because adjacent provider state may contain credentials.
    """

    snapshot = local_managed_control_snapshot(longhouse_home, session_id)
    if snapshot is None:
        return None
    for item in snapshot["controls"]:
        operations = item.get("granted_operations")
        if item.get("state") != "attached" or not isinstance(operations, list) or "send_input" not in operations:
            continue
        return item
    return None


def activity_state(session_payload: dict[str, Any] | None) -> str | None:
    session_state = (session_payload or {}).get("session_state")
    activity = session_state.get("activity") if isinstance(session_state, dict) else None
    return activity.get("state") if isinstance(activity, dict) else None


def isolation_paths(isolation_root: Path) -> tuple[Path, Path]:
    """Return the (home, longhouse_home) pair every helper in this module agrees on."""

    return isolation_root / "home", isolation_root / "longhouse"


def start_machine_and_shipper(args: Any, *, isolation_root: Path, evidence_root: Path) -> tuple[TranscriptShipper, dict[str, str]]:
    """Register the sandbox's disposable machine identity and run its Machine Agent.

    Returns the shipper plus the environment dict (HOME/LONGHOUSE_HOME set) that
    the managed Claude session itself must reuse so both processes agree on one
    disposable Longhouse home.
    """

    home, longhouse_home = isolation_paths(isolation_root)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment["LONGHOUSE_HOME"] = str(longhouse_home)
    shipper = _start_transcript_shipper(
        "claude",
        args,
        home=home,
        environment=environment,
        evidence_root=evidence_root,
        longhouse_home=longhouse_home,
    )
    return shipper, environment


def scanner_visible_claude_binary(claude_bin: Path, *, longhouse_home: Path) -> Path:
    """Expose a staged Claude executable under the production ``claude`` basename.

    Factory release artifacts are intentionally normalized to a generic file
    named ``provider``.  The Machine Agent's process-identity scanner is
    intentionally stricter: a PID is Claude authority only when its command
    contains a ``claude`` basename.  Launch through an isolation-local symlink
    so qualification preserves that invariant instead of weakening it.
    """

    if claude_bin.name == "claude":
        return claude_bin
    alias = longhouse_home / "qa-provider-bin" / "claude"
    alias.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        alias.symlink_to(claude_bin)
    except FileExistsError:
        if alias.resolve(strict=False) != claude_bin.resolve(strict=False):
            raise
    return alias


def claude_launch_environment(
    base_environment: dict[str, str],
    *,
    claude_bin: Path,
    engine: Path,
    model: str | None,
    longhouse_home: Path,
) -> dict[str, str]:
    environment = dict(base_environment)
    environment["LONGHOUSE_CLAUDE_BIN"] = str(scanner_visible_claude_binary(claude_bin, longhouse_home=longhouse_home))
    environment["LONGHOUSE_ENGINE_BIN"] = str(engine)
    environment["LONGHOUSE_ORIGIN_KIND"] = "test_or_canary"
    environment["LONGHOUSE_LAUNCH_ACTOR"] = "automation"
    environment["LONGHOUSE_LAUNCH_SURFACE"] = "test"
    if model:
        environment["ANTHROPIC_MODEL"] = model
    return environment


def launch_claude_session(
    *,
    workspace: Path,
    project: str,
    name: str,
    env: dict[str, str],
    terminal_path: Path,
    launch_timeout_secs: float,
) -> tuple[ProviderPtySession, str, str | None]:
    """Launch ``longhouse claude``, clear its first-run prompts, return (session, session_id, provider_session_id).

    Prompt text/handling mirrors the already-live ``claude_conversation_reset``
    and ``managed_claude_live`` canaries: a fresh disposable profile always
    renders the workspace-trust and local-development-channel selectors before
    it registers its channel state. ``provider_session_id`` is Claude's own
    native session id (see ``read_provider_session_id``) -- callers that will
    later look up transcript content for this session (``send_and_await_marker``,
    ``find_tool_invocation``) must pass it through; the Longhouse ``session_id``
    alone does not name a real transcript file.
    """

    session = ProviderPtySession.start(
        argv=["longhouse", "claude", "--cwd", str(workspace), "--project", project, "--name", name],
        cwd=workspace,
        env=env,
        terminal_path=terminal_path,
    )
    # managed_claude_live.wait_for_channel_ready/find_channel_session_id
    # default to Path.home(), which is only correct when the spawned Claude
    # process shares the caller's ambient HOME. start_machine_and_shipper
    # gives this session its own disposable, isolated HOME (recorded in
    # `env`), so the readiness poll below must look there too -- not at
    # whatever HOME this producer's own top-level sandbox process happens to
    # have (QualificationSandbox's own fixed sandbox-home, a different path
    # entirely). Without this, polling silently checks the wrong directory
    # forever and every launch times out regardless of real readiness.
    session_home = Path(env["HOME"]) if env.get("HOME") else None
    confirmed_trust = False
    confirmed_channel = False
    confirmed_permission_bypass = False
    resolved_provider_session_id: str | None = None

    def channel_ready() -> str | None:
        nonlocal confirmed_trust, confirmed_channel, confirmed_permission_bypass, resolved_provider_session_id
        if not session.alive():
            raise ScenarioError(f"longhouse claude exited before channel readiness (exit={session.process.returncode})")
        compact = re.sub(r"\s+", "", terminal_text(terminal_path))
        # Recent Claude builds render a native bypass-permissions
        # acknowledgement ("1. No, exit" / "2. Yes, I accept") before
        # publishing channel state, on every fresh launch in this sandboxed
        # mode -- not just first-run onboarding. provider_native_resume.py's
        # _accept_claude_permission_prompt already handles this for Resume;
        # this helper never learned to, so every producer built on top of it
        # hung at this exact prompt indefinitely (found registering the 12
        # non-Resume producers' real end-to-end verification).
        if not confirmed_permission_bypass and "1.No,exit" in compact and "2.Yes,Iaccept" in compact:
            session.write(b"2\r")
            confirmed_permission_bypass = True
        if not confirmed_trust and "Yes,Itrustthisfolder" in compact:
            session.write(b"\r")
            confirmed_trust = True
        if not confirmed_channel and "Iamusingthisforlocaldevelopment" in compact:
            session.write(b"\r")
            confirmed_channel = True
        candidate = find_channel_session_id(workspace, home=session_home)
        if candidate and wait_for_channel_ready(candidate, timeout_secs=0.2, home=session_home):
            # The lifecycle hook that attaches Claude's own native session id
            # to this same state file can lag slightly behind `ready` --
            # require it too before declaring the session usable, or a later
            # transcript lookup has no real id to search for.
            resolved_provider_session_id = read_provider_session_id(candidate, home=session_home)
            if resolved_provider_session_id:
                return candidate
        return None

    try:
        session_id = wait_until(channel_ready, timeout=launch_timeout_secs, description="Claude managed channel readiness")
    except Exception:
        session.close()
        raise
    return session, session_id, resolved_provider_session_id


def send_and_await_marker(
    *,
    session_id: str,
    prompt: str,
    marker: str,
    repo_root: Path,
    timeout: float,
    env: dict[str, str] | None = None,
    provider_session_id: str | None = None,
) -> tuple[str, int, str | None]:
    """Send ``prompt`` over the Claude channel and wait for ``marker`` in the assistant transcript.

    ``provider_session_id`` is Claude's own native session id (from
    ``launch_claude_session``'s return value) -- the transcript file is named
    by that id, not the Longhouse ``session_id``, which is unrelated.
    """

    send = channel_send(session_id, prompt, repo_root=repo_root, env=env)
    if send.returncode != 0:
        raise ScenarioError(f"claude-channel send failed with status {send.returncode}: {send.stderr[-1000:]}")

    home = Path(env["HOME"]) if env and env.get("HOME") else None
    return await_assistant_marker(
        session_id=session_id,
        marker=marker,
        timeout=timeout,
        home=home,
        provider_session_id=provider_session_id,
    )


def await_assistant_marker(
    *,
    session_id: str,
    marker: str,
    timeout: float,
    home: Path | None = None,
    provider_session_id: str | None = None,
) -> tuple[str, int, str | None]:
    """Wait for one assistant marker after the caller has submitted a real user turn.

    Coordination-channel input is attributed, untrusted peer input. Producers
    that need the model to follow an instruction must submit that instruction
    through their owned Helm PTY, then use this observer instead of disguising
    the channel message as a trusted user command.
    """

    lookup_id = transcript_lookup_id(session_id, provider_session_id)

    def observed() -> tuple[bool, str | None, int | None, str | None] | None:
        result = assistant_transcript_contains(lookup_id, marker, home=home)
        return result if result[0] else None

    _observed, transcript_path, transcript_line, transcript_timestamp = wait_until(
        observed, timeout=timeout, description=f"Claude assistant marker {marker!r}"
    )
    return str(transcript_path), int(transcript_line or 0), transcript_timestamp


def wait_for_served_quiescent(
    *,
    api_url: str,
    token: str,
    session_id: str,
    timeout: float,
) -> tuple[bool, float, list[str]]:
    """Poll the canonical session reducer until this session is quiescent.

    ``GET /api/agents/sessions/{id}`` intentionally returns the narrow archive
    shape and does not expose ``session_state``. The QA diagnostics surface
    exposes the exact ``canonical_session_detail`` reducer under ``shadow``;
    use that per-session authority rather than a bounded wall listing.
    """

    samples: list[str] = []
    started = time.monotonic()

    def settled() -> dict[str, Any] | None:
        payload = api_json_tolerant(api_url, token, f"sessions/{session_id}/state-diagnostics")
        shadow = payload.get("shadow") if isinstance(payload, dict) else None
        activity = shadow.get("activity") if isinstance(shadow, dict) else None
        state = activity.get("state") if isinstance(activity, dict) else None
        if isinstance(state, str):
            samples.append(state)
        if payload and payload.get("served_path") == "canonical_session_detail" and state == "quiescent":
            return payload
        return None

    try:
        wait_until(settled, timeout=timeout, description="served Claude activity settling to quiescent at the turn boundary")
    except ScenarioError:
        return False, time.monotonic() - started, samples
    return True, time.monotonic() - started, samples


def find_tool_invocation(
    session_id: str,
    tool_name_contains: str,
    *,
    after_line_counts: dict[str, int] | None = None,
    home: Path | None = None,
) -> dict[str, Any] | None:
    """Find a real ``tool_use``/``tool_result`` pair in Claude's own transcript.

    Claude Code's local ``.claude/projects/**/<id>.jsonl`` transcript uses the
    Anthropic Messages API content-block shape: an assistant row's
    ``message.content`` carries ``{"type": "tool_use", "id", "name", "input"}``
    blocks, and the matching result is a later ``user`` row whose
    ``message.content`` carries ``{"type": "tool_result", "tool_use_id", ...}``.
    This is the same row shape ``managed_claude_live.text_fragments`` already
    assumes when it walks ``message.content`` for text. Returns ``None`` if no
    matching call is found (nothing invoked the tool -- not proof of failure,
    just absence).
    """

    for path in transcript_paths(session_id, home=home):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        first_candidate_line = int((after_line_counts or {}).get(str(path), 0) or 0) + 1
        tool_use: dict[str, Any] | None = None
        tool_use_line: int | None = None
        for index, line in enumerate(lines, start=1):
            if index < first_candidate_line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = row.get("message") if isinstance(row, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            if tool_use is None and row.get("type") == "assistant":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use" and tool_name_contains in str(block.get("name") or ""):
                        tool_use = block
                        tool_use_line = index
                        break
            if tool_use is not None and index > (tool_use_line or 0):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") == tool_use.get("id"):
                        return {
                            "transcript_path": str(path),
                            "tool_use_line": tool_use_line,
                            "tool_result_line": index,
                            "tool_name": tool_use.get("name"),
                            "tool_use_id": tool_use.get("id"),
                            "input": tool_use.get("input"),
                            "is_error": block.get("is_error") is True,
                            "result": block.get("content"),
                        }
        if tool_use is not None:
            # The call was made but no result row has landed yet under this
            # path -- report the call so a caller can decide whether to keep
            # waiting rather than treating it as "never called".
            return {
                "transcript_path": str(path),
                "tool_use_line": tool_use_line,
                "tool_result_line": None,
                "tool_name": tool_use.get("name"),
                "tool_use_id": tool_use.get("id"),
                "input": tool_use.get("input"),
                "is_error": None,
                "result": None,
            }
    return None


def find_compaction_boundary(
    session_id: str,
    *,
    after_line_counts: dict[str, int],
    home: Path | None = None,
) -> dict[str, Any] | None:
    """Find a real Claude compaction row written after the supplied transcript cursor."""

    for path in transcript_paths(session_id, home=home):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        first_candidate_line = int(after_line_counts.get(str(path), 0) or 0) + 1
        for index, line in enumerate(lines, start=1):
            if index < first_candidate_line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            row_type = row.get("type")
            subtype = row.get("subtype")
            if row_type == "summary" or (row_type == "system" and subtype in {"compact_boundary", "microcompact_boundary"}):
                return {
                    "transcript_path": str(path),
                    "line": index,
                    "type": row_type,
                    "subtype": subtype,
                }
    return None


def mcp_bootstrap_config_paths(longhouse_home: Path, session_id: str) -> list[Path]:
    """List the coordination MCP bootstrap config(s) Longhouse wrote for one session.

    ``engine/src/longhouse.rs::write_claude_mcp_config`` writes exactly one
    file per launch at ``$LONGHOUSE_HOME/run/claude-mcp/<session_id>-<uuid>.json``,
    called once before the provider binary is even spawned -- there is no
    compaction-triggered call path back into it.
    """

    directory = longhouse_home / "run" / "claude-mcp"
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"{session_id}-*.json"))


def read_coordination_token(longhouse_home: Path, session_id: str) -> str | None:
    """Read the real per-session coordination token Longhouse issued at launch.

    This is the exact token value the model's own MCP ``send``/``inbox``/``reply``
    tool calls use (``mcpServers.longhouse-coordination.env.LONGHOUSE_COORDINATION_TOKEN``
    in the bootstrap config written by ``write_claude_mcp_config``), so a
    producer authenticating with it exercises the real per-session coordination
    authority path rather than a synthetic substitute.
    """

    for path in mcp_bootstrap_config_paths(longhouse_home, session_id):
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        token = config.get("mcpServers", {}).get("longhouse-coordination", {}).get("env", {}).get("LONGHOUSE_COORDINATION_TOKEN")
        if isinstance(token, str) and token:
            return token
    return None


def close_session(session: ProviderPtySession, *, exit_command: str | None = "/exit") -> dict[str, Any]:
    try:
        if exit_command and session.alive():
            session.submit_line(exit_command)
            wait_until(lambda: not session.alive() or None, timeout=10, description="Claude Helm process exit")
    except ScenarioError:
        pass
    finally:
        session.close()
    return {"exit_code": session.process.returncode, "alive_after_close": session.alive()}
