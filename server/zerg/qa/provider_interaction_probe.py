"""Provider-native interaction probes used by the universal harness.

The producer owns live PTY/provider-store mechanics. The shared interaction
oracle remains in ``provider_interaction_semantics`` so a raw artifact can be
replayed without starting a provider or spending a model turn.

Only probes with an exact provider-native evidence implementation can be
observed here. A terminal acknowledgement without a persisted provider record
is emitted as blocked evidence; an incomplete live adapter can therefore never
look like a passing provider qualification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Mapping
from uuid import uuid4

from zerg.provider_cli_contract import PROVIDER_CLI_BINARY_BY_PROVIDER
from zerg.provider_cli_contract import PROVIDER_CLI_ENV_BY_PROVIDER
from zerg.qa import antigravity_release_identity
from zerg.qa import claude_release_identity
from zerg.qa import codex_release_identity
from zerg.qa import cursor_release_identity
from zerg.qa import opencode_release_identity
from zerg.qa.claude_conversation_reset import _terminal_text
from zerg.qa.claude_conversation_reset import _wait
from zerg.qa.managed_claude_live import strip_terminal_controls
from zerg.qa.provider_interaction_semantics import MIN_NEGATIVE_PROOF_QUIESCENCE_SECONDS
from zerg.qa.provider_interaction_semantics import raw_event_digest
from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.pty_session import wait_for_terminal_quiescence
from zerg.services.managed_provider_contracts import contract_for_provider

_DEFAULT_TIMEOUT_SECONDS = 60.0
_NEGATIVE_PROOF_QUIESCENCE_SECONDS = MIN_NEGATIVE_PROOF_QUIESCENCE_SECONDS
_PROVIDER_VERSION_PATTERNS = {
    "antigravity": antigravity_release_identity.VERSION_LINE,
    "claude": claude_release_identity.VERSION_LINE,
    "codex": codex_release_identity._VERSION_LINE,
    "cursor": cursor_release_identity.VERSION_LINE,
    "opencode": opencode_release_identity.VERSION_LINE,
}
_NO_TOKEN_AUTH_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_VERTEX",
        "CODEX_AGENTS_TOKEN",
        "CODEX_API_KEY",
        "CURSOR_ACCESS_TOKEN",
        "CURSOR_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_API_KEY",
        "GROQ_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_CONFIG_FILE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_PROFILE",
        "AWS_ROLE_ARN",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "TAVILY_API_KEY",
        "XAI_API_KEY",
        "ZAI_API_KEY",
    }
)
_NO_TOKEN_AUTH_FLAG_ENV_NAMES = frozenset({"CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"})
_NO_TOKEN_FALSE_FLAG_VALUES = frozenset({"0", "false", "no", "off"})
_NO_TOKEN_SCRUB_ENV_NAMES = _NO_TOKEN_AUTH_ENV_NAMES | frozenset(
    {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_MODEL",
        "AWS_DEFAULT_REGION",
        "AWS_EC2_METADATA_DISABLED",
        "AWS_REGION",
        "CODEX_API_URL",
        "CODEX_HOME",
        "CODEX_MANAGED_PACKAGE_ROOT",
        "CLAUDE_CONFIG_DIR",
        "CURSOR_MODEL",
        "CURSOR_CONFIG_DIR",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_REGION",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_DIR",
        "VERTEXAI_LOCATION",
        "VERTEXAI_PROJECT",
        "LONGHOUSE_ANTIGRAVITY_QUALIFICATION_HOME",
        "LONGHOUSE_ANTIGRAVITY_QUALIFICATION_LIVE",
        "ANTIGRAVITY_QUALIFICATION_HOME",
        "LONGHOUSE_CLAUDE_QUALIFICATION_LIVE",
        "LONGHOUSE_CLAUDE_INTERACTION_ARTIFACT",
        "LONGHOUSE_CLAUDE_QUALIFICATION_USE_DEFAULT_HOME",
        "LONGHOUSE_CODEX_BIN",
        "LONGHOUSE_ENGINE_BIN",
        "LONGHOUSE_OPENCODE_BIN",
        "LONGHOUSE_OPENCODE_QUALIFICATION_MODEL",
        "LONGHOUSE_PROVIDER_INTERACTION_ARTIFACT",
        "LONGHOUSE_PROVIDER_INTERACTION_LIVE",
    }
)


def _no_token_environment() -> dict[str, str]:
    credentials = []
    for name in sorted(_NO_TOKEN_AUTH_ENV_NAMES):
        value = str(os.environ.get(name) or "").strip()
        if value and (name not in _NO_TOKEN_AUTH_FLAG_ENV_NAMES or value.casefold() not in _NO_TOKEN_FALSE_FLAG_VALUES):
            credentials.append(name)
    if credentials:
        raise RuntimeError("live_no_token requires a credential-free environment; found: " + ", ".join(credentials))
    environment = os.environ.copy()
    for name in _NO_TOKEN_SCRUB_ENV_NAMES:
        environment.pop(name, None)
    # Prevent a provider SDK from discovering an instance/container role after
    # the explicit environment has been scrubbed.
    environment["AWS_EC2_METADATA_DISABLED"] = "true"
    return environment


def _looks_like_claude_auth_prompt(compact_terminal: str) -> bool:
    return any(
        marker in compact_terminal.lower()
        for marker in (
            "selectloginmethod",
            "logintoclaudecode",
            "claudecodecanbeused",
            "please run/login",
            "401invalidauthenticationcredentials",
        )
    )


def _looks_like_provider_auth_prompt(terminal: str) -> bool:
    """Recognize setup/auth failure without treating it as command evidence."""

    normalized = " ".join(terminal.lower().split())
    return any(
        marker in normalized
        for marker in (
            "login required",
            "please log in",
            "sign in",
            "authentication required",
            "authentication failed",
            "unauthorized",
            "401 unauthorized",
            "api key required",
            "not logged in",
        )
    )


def _normalized_terminal(path: Path) -> str:
    try:
        text = _terminal_text(path)
    except OSError:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _normalized_terminal_delta(path: Path, offset: int) -> str:
    """Normalize only PTY bytes rendered after an interaction was submitted."""

    try:
        with path.open("rb") as handle:
            handle.seek(max(0, offset))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return re.sub(r"\s+", " ", strip_terminal_controls(text)).strip()


def _file_evidence(path: Path, *, artifact_root: Path) -> dict[str, Any] | None:
    try:
        relative_path = path.resolve().relative_to(artifact_root.resolve())
        size = path.stat().st_size
        return {
            "source_path": str(relative_path),
            "bytes": size,
            "sha256": _sha256(path),
        }
    except (OSError, ValueError):
        return None


def _native_source_snapshot(root: Path, *, artifact_root: Path, limit: int = 128) -> list[dict[str, Any]]:
    """Return redacted, hash-addressed evidence for an isolated provider root."""

    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        paths = sorted(path for path in root.rglob("*") if path.is_file())
    except OSError:
        return []
    for path in paths[:limit]:
        evidence = _file_evidence(path, artifact_root=artifact_root)
        if evidence is not None:
            rows.append(evidence)
    return rows


def _terminal_evidence(path: Path, *, artifact_root: Path) -> dict[str, Any] | None:
    return _file_evidence(path, artifact_root=artifact_root)


def _probe_status_row(
    probe: Any,
    *,
    status: str,
    failure_code: str | None = None,
    message: str | None = None,
    terminal_evidence: dict[str, Any] | None = None,
    native_source_rows: list[dict[str, Any]] | None = None,
    submitted_input_sequence: list[str] | None = None,
    raw_events: list[dict[str, Any]] | None = None,
    terminal_acknowledged: bool | None = None,
    capture_complete: bool | None = None,
    post_interaction_quiescent: bool | None = None,
    provider_state_after: bool | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "probe_id": probe.probe_id,
        "disposition": probe.disposition,
        "status": status,
        "raw_events": raw_events or [],
        "submitted_input_sequence": submitted_input_sequence or [],
        "native_source_rows": native_source_rows or [],
    }
    if failure_code:
        row["failure_code"] = failure_code
    if message:
        row["message"] = message
    if terminal_evidence is not None:
        row["terminal_evidence"] = terminal_evidence
    if terminal_acknowledged is not None:
        row["terminal_acknowledged"] = terminal_acknowledged
    if capture_complete is not None:
        row["capture_complete"] = capture_complete
    if post_interaction_quiescent is not None:
        row["post_interaction_quiescent"] = post_interaction_quiescent
    if provider_state_after is not None:
        row["provider_state_after"] = provider_state_after
    return row


def _run_terminal_interaction_probe(
    *,
    provider: str,
    probe: Any,
    artifact_root: Path,
    output_root: Path,
    workspace: Path,
    home: Path,
    native_root: Path,
    environment: Mapping[str, str],
    argv: list[str],
    ready_markers: tuple[str, ...],
    acknowledgement_markers: tuple[str, ...],
    timeout: float,
    minimum_terminal_bytes: int = 128,
) -> dict[str, Any]:
    """Run one isolated TUI interaction and retain honest evidence."""

    terminal_path = output_root / "terminal.raw"
    env = dict(environment)
    env["HOME"] = str(home)
    env["TERM"] = "xterm-256color"
    env["LINES"] = "40"
    env["COLUMNS"] = "132"
    before_sources = _native_source_snapshot(native_root, artifact_root=artifact_root)
    submitted: list[str] = []
    session: ProviderPtySession | None = None
    try:
        session = ProviderPtySession.start(
            argv=argv,
            cwd=workspace,
            env=env,
            terminal_path=terminal_path,
            thread_name=f"{provider}-interaction-probe-terminal-drain",
        )

        def ready() -> bool | None:
            if not session or not session.alive():
                return False
            text = _normalized_terminal(terminal_path)
            if _looks_like_provider_auth_prompt(text):
                raise RuntimeError("missing_isolated_auth: provider opened an authentication flow")
            if ready_markers and all(marker.lower() in text.lower() for marker in ready_markers):
                return True
            try:
                if terminal_path.stat().st_size >= minimum_terminal_bytes:
                    return True
            except OSError:
                pass
            return None

        _wait(ready, timeout=timeout, message=f"{provider} interaction probe did not become ready")
        wait_for_terminal_quiescence(
            session,
            timeout=timeout,
            minimum_bytes=minimum_terminal_bytes,
            stable_seconds=0.3,
        )
        try:
            interaction_offset = terminal_path.stat().st_size
        except OSError:
            interaction_offset = 0
        for command in probe.input_sequence:
            if command.startswith("("):
                continue
            session.submit_line(command)
            submitted.append(command)

        def acknowledged() -> bool | None:
            if not session or not session.alive():
                return False
            text = _normalized_terminal_delta(terminal_path, interaction_offset)
            if _looks_like_provider_auth_prompt(text):
                raise RuntimeError("missing_isolated_auth: provider requested authentication after launch")
            if acknowledgement_markers and all(marker.lower() in text.lower() for marker in acknowledgement_markers):
                return True
            return None

        _wait(
            acknowledged,
            timeout=timeout,
            message=f"{provider} did not acknowledge {probe.probe_id}",
        )
        terminal_evidence = _terminal_evidence(terminal_path, artifact_root=artifact_root)
        after_sources = _native_source_snapshot(native_root, artifact_root=artifact_root)
        # A terminal acknowledgement proves only that the TUI rendered a
        # response. It is not a provider transcript record. Until an adapter
        # parses and hash-addresses a changed native source, keep this probe
        # blocked instead of manufacturing raw events from terminal text.
        return _probe_status_row(
            probe,
            status="blocked",
            failure_code="interaction_native_raw_evidence_missing",
            message="Provider acknowledged the input, but no parsed provider-native raw event was captured.",
            terminal_evidence=terminal_evidence,
            native_source_rows=after_sources or before_sources,
            submitted_input_sequence=submitted,
            terminal_acknowledged=True,
        )
    except (OSError, RuntimeError, TimeoutError) as exc:
        terminal_evidence = _terminal_evidence(terminal_path, artifact_root=artifact_root)
        after_sources = _native_source_snapshot(native_root, artifact_root=artifact_root)
        message = str(exc)
        failure_code = "interaction_probe_setup_failed"
        if message.startswith("missing_isolated_auth") or _looks_like_provider_auth_prompt(_normalized_terminal(terminal_path)):
            failure_code = "missing_isolated_auth"
        elif "did not acknowledge" in message:
            failure_code = "interaction_acknowledgement_missing"
        return _probe_status_row(
            probe,
            status="blocked",
            failure_code=failure_code,
            message=message,
            terminal_evidence=terminal_evidence,
            native_source_rows=after_sources or before_sources,
            submitted_input_sequence=submitted,
        )
    finally:
        if session is not None:
            session.close()


def _codex_model_probe(
    *,
    binary: Path,
    artifact_root: Path,
    timeout: float,
    environment: Mapping[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    probe = next(row for row in contract_for_provider("codex").interaction_probes if row.probe_id == "codex_model_picker")
    invocation = uuid4().hex
    output_root = artifact_root / "codex-model" / invocation
    output_root.mkdir(parents=True, exist_ok=False)
    workspace = output_root / "workspace"
    workspace.mkdir()
    home = output_root / "home"
    home.mkdir(mode=0o700)
    codex_home = output_root / "codex"
    codex_home.mkdir(mode=0o700)
    env = dict(environment)
    env["CODEX_HOME"] = str(codex_home)
    row = _run_terminal_interaction_probe(
        provider="codex",
        probe=probe,
        artifact_root=artifact_root,
        output_root=output_root,
        workspace=workspace,
        home=home,
        native_root=codex_home,
        environment=env,
        argv=[
            str(binary),
            "--no-alt-screen",
            "--ask-for-approval",
            "never",
            "--sandbox",
            "read-only",
            "-C",
            str(workspace),
        ],
        ready_markers=(),
        acknowledgement_markers=("model",),
        timeout=timeout,
    )
    return [row], list(row.get("native_source_rows") or [])


def _opencode_interaction_probes(
    *,
    binary: Path,
    artifact_root: Path,
    timeout: float,
    environment: Mapping[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    contract = contract_for_provider("opencode")
    assert contract is not None
    rows: list[dict[str, Any]] = []
    native_rows: list[dict[str, Any]] = []
    for probe in contract.interaction_probes:
        invocation = uuid4().hex
        output_root = artifact_root / "opencode" / probe.probe_id / invocation
        output_root.mkdir(parents=True, exist_ok=False)
        workspace = output_root / "workspace"
        workspace.mkdir()
        home = output_root / "home"
        home.mkdir(mode=0o700)
        data_root = output_root / "data"
        config_root = output_root / "config"
        cache_root = output_root / "cache"
        for path in (data_root, config_root, cache_root):
            path.mkdir(mode=0o700)
        env = dict(environment)
        env.update(
            {
                "XDG_CONFIG_HOME": str(config_root),
                "XDG_DATA_HOME": str(data_root),
                "XDG_CACHE_HOME": str(cache_root),
            }
        )
        row = _run_terminal_interaction_probe(
            provider="opencode",
            probe=probe,
            artifact_root=artifact_root,
            output_root=output_root,
            workspace=workspace,
            home=home,
            native_root=data_root,
            environment=env,
            argv=[str(binary), str(workspace), "--pure", "--mini", "--no-replay"],
            ready_markers=("Ask anything",),
            acknowledgement_markers=("help",) if probe.probe_id == "opencode_help_command" else ("session",),
            timeout=timeout,
        )
        rows.append(row)
        native_rows.extend(row.get("native_source_rows") or [])
    return rows, native_rows


def _cursor_model_probe(
    *,
    binary: Path,
    artifact_root: Path,
    timeout: float,
    environment: Mapping[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    contract = contract_for_provider("cursor")
    assert contract is not None
    probe = next(row for row in contract.interaction_probes if row.probe_id == "cursor_model_launch_option")
    invocation = uuid4().hex
    output_root = artifact_root / "cursor-model" / invocation
    output_root.mkdir(parents=True, exist_ok=False)
    workspace = output_root / "workspace"
    workspace.mkdir()
    home = output_root / "home"
    home.mkdir(mode=0o700)
    events_path = output_root / "cursor-hooks.jsonl"
    env = dict(environment)
    env["LONGHOUSE_SESSION_ID"] = f"direct-{invocation}"
    env["LONGHOUSE_CURSOR_GATE0_EVENTS"] = str(events_path)
    env["HOME"] = str(home)
    env["TERM"] = "xterm-256color"
    env["LINES"] = "40"
    env["COLUMNS"] = "132"
    from zerg.qa.cursor_helm_gate0 import write_project_hooks

    write_project_hooks(workspace, events_path)
    terminal_path = output_root / "terminal.raw"
    argv = [
        str(binary),
        "--print",
        "--trust",
        "--mode",
        "ask",
        "--workspace",
        str(workspace),
        "--model",
        "auto",
    ]
    before_sources = _native_source_snapshot(home / ".cursor", artifact_root=artifact_root)
    result = subprocess.run(
        argv,
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    stdout_path = output_root / "stdout.log"
    stderr_path = output_root / "stderr.log"
    stdout_path.write_text(result.stdout or "", encoding="utf-8")
    stderr_path.write_text(result.stderr or "", encoding="utf-8")
    terminal_path.write_text(f"stdout\n{result.stdout or ''}\nstderr\n{result.stderr or ''}", encoding="utf-8")
    terminal_evidence = _terminal_evidence(terminal_path, artifact_root=artifact_root)
    native_rows = _native_source_snapshot(home / ".cursor", artifact_root=artifact_root)
    file_evidence = _file_evidence(events_path, artifact_root=artifact_root)
    if file_evidence is not None:
        native_rows.append(file_evidence)
    combined = f"{result.stdout}\n{result.stderr}\n{events_path.read_text(encoding='utf-8') if events_path.exists() else ''}"
    if _looks_like_provider_auth_prompt(combined):
        row = _probe_status_row(
            probe,
            status="blocked",
            failure_code="missing_isolated_auth",
            message="Cursor could not establish an authenticated isolated console session.",
            terminal_evidence=terminal_evidence,
            native_source_rows=native_rows or before_sources,
            submitted_input_sequence=[],
        )
    elif result.returncode != 0:
        row = _probe_status_row(
            probe,
            status="blocked",
            failure_code="interaction_probe_setup_failed",
            message=f"cursor-agent exited with {result.returncode}",
            terminal_evidence=terminal_evidence,
            native_source_rows=native_rows or before_sources,
            submitted_input_sequence=[],
        )
    elif all(marker.lower() in combined.lower() for marker in (*probe.raw_markers, *probe.raw_output_markers)):
        row = _probe_status_row(
            probe,
            status="blocked",
            failure_code="interaction_native_raw_evidence_missing",
            message="Cursor acknowledged the launch option, but no parsed provider-native raw event was captured.",
            terminal_evidence=terminal_evidence,
            native_source_rows=native_rows or before_sources,
            submitted_input_sequence=[],
            terminal_acknowledged=True,
        )
    else:
        row = _probe_status_row(
            probe,
            status="blocked",
            failure_code="interaction_acknowledgement_missing",
            message="Cursor completed without emitting the model-selection acknowledgement markers.",
            terminal_evidence=terminal_evidence,
            native_source_rows=native_rows or before_sources,
            submitted_input_sequence=[],
        )
    return [row], native_rows or before_sources


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_binary(provider: str, configured: str | Path | None) -> Path:
    value = str(configured or os.environ.get(PROVIDER_CLI_ENV_BY_PROVIDER.get(provider) or "") or "").strip()
    if not value:
        value = PROVIDER_CLI_BINARY_BY_PROVIDER.get(provider, provider)
    path = Path(value).expanduser()
    if not path.is_absolute():
        resolved = subprocess.run(
            ["/usr/bin/which", str(path)],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        ).stdout.strip()
        if not resolved:
            raise RuntimeError(f"{provider} binary not found: {value}")
        path = Path(resolved)
    return path.resolve(strict=True)


def _provider_version(
    provider: str,
    binary: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    result = subprocess.run(
        [str(binary), "--version"],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
        env=dict(environment) if environment is not None else _no_token_environment(),
    )
    if result.returncode != 0:
        raise RuntimeError(f"provider version probe failed ({result.returncode}): {(result.stderr or result.stdout).strip()}")
    try:
        pattern = _PROVIDER_VERSION_PATTERNS[provider]
    except KeyError as exc:
        raise RuntimeError(f"provider version grammar is not registered: {provider}") from exc
    versions = {
        match.group("version")
        for stream in (result.stdout, result.stderr)
        for line in stream.splitlines()
        if (match := pattern.fullmatch(line.strip())) is not None
    }
    if len(versions) != 1:
        raise RuntimeError(f"provider version output was not unambiguous for {provider}: {(result.stdout + result.stderr).strip()}")
    return versions.pop()


def _claude_transcript_paths(config_dir: Path) -> list[Path]:
    return list((config_dir / "projects").glob("**/*.jsonl"))


def _transcript_snapshot(config_dir: Path) -> dict[Path, int]:
    snapshot: dict[Path, int] = {}
    for path in _claude_transcript_paths(config_dir):
        try:
            snapshot[path] = path.stat().st_size
        except OSError:
            continue
    return snapshot


def _new_transcript_rows(
    snapshot: dict[Path, int],
    *,
    config_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for path in _claude_transcript_paths(config_dir):
        offset = snapshot.get(path, 0)
        try:
            with path.open("rb") as handle:
                file_bytes = handle.read()
        except OSError:
            continue
        raw = file_bytes[offset:]
        file_digest = hashlib.sha256(file_bytes).hexdigest()
        for relative_offset, line in _jsonl_bytes(raw, start_offset=offset):
            try:
                row = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(row, dict):
                continue
            rows.append(row)
            sources.append(
                {
                    "source_path": str(path),
                    "source_offset": relative_offset,
                    "line": line.decode("utf-8"),
                    "line_sha256": hashlib.sha256(line).hexdigest(),
                    "event_sha256": raw_event_digest(row),
                    "source_binding": "file_bytes_at_offset",
                    "source_file_bytes": len(file_bytes),
                    "source_file_sha256": file_digest,
                }
            )
    return rows, sources


def _jsonl_bytes(raw: bytes, *, start_offset: int) -> list[tuple[int, bytes]]:
    result: list[tuple[int, bytes]] = []
    cursor = 0
    for line in raw.splitlines(keepends=True):
        line_without_newline = line.rstrip(b"\r\n")
        if line_without_newline:
            result.append((start_offset + cursor, line_without_newline))
        cursor += len(line)
    return result


def _transcript_window_digest(source_rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256("".join(str(row["event_sha256"]) for row in source_rows).encode("ascii")).hexdigest()


def _transcript_file_signature(
    *,
    config_dir: Path,
) -> tuple[tuple[str, int, str], ...]:
    """Capture every current transcript file, including files with no valid rows yet."""

    signature: list[tuple[str, int, str]] = []
    for path in _claude_transcript_paths(config_dir):
        try:
            stat = path.stat()
        except OSError:
            continue
        signature.append((str(path), int(stat.st_size), str(stat.st_mtime_ns)))
    return tuple(sorted(signature))


def _transcript_has_unparsed_new_bytes(
    snapshot: dict[Path, int],
    *,
    config_dir: Path,
) -> bool:
    """Return true when a new transcript line is present but not valid JSON yet."""

    for path in _claude_transcript_paths(config_dir):
        offset = snapshot.get(path, 0)
        try:
            raw = path.read_bytes()[offset:]
        except OSError:
            continue
        for line in raw.splitlines(keepends=True):
            line_without_newline = line.rstrip(b"\r\n")
            if not line_without_newline:
                continue
            try:
                json.loads(line_without_newline.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return True
    return False


def _wait_for_transcript_quiescence(
    snapshot: dict[Path, int],
    *,
    config_dir: Path,
    timeout: float,
    stable_seconds: float = _NEGATIVE_PROOF_QUIESCENCE_SECONDS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Require a bounded stable window before making a negative claim.

    Provider-local commands can flush their archive asynchronously. The
    longer default is intentional: a short terminal pause is not evidence
    that a delayed assistant turn will never be persisted.
    """

    deadline = time.monotonic() + timeout
    previous_signature: tuple[tuple[str, int, str], ...] | None = None
    stable_snapshots = 0
    stable_since: float | None = None
    latest_rows: list[dict[str, Any]] = []
    latest_sources: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        rows, sources = _new_transcript_rows(snapshot, config_dir=config_dir)
        row_signature = tuple(
            (
                str(source.get("source_path") or ""),
                int(source.get("source_offset") or 0),
                str(source.get("line_sha256") or ""),
            )
            for source in sources
        )
        file_signature = _transcript_file_signature(config_dir=config_dir)
        pending_new_bytes = _transcript_has_unparsed_new_bytes(snapshot, config_dir=config_dir)
        signature = (row_signature, file_signature)
        latest_rows, latest_sources = rows, sources
        if signature == previous_signature:
            stable_snapshots += 1
        else:
            previous_signature = signature
            stable_snapshots = 1 if row_signature else 0
            stable_since = time.monotonic() if row_signature else None
        if (
            stable_snapshots >= 3
            and stable_since is not None
            and time.monotonic() - stable_since >= max(0.4, float(stable_seconds))
            and latest_rows
            and not pending_new_bytes
        ):
            return (
                latest_rows,
                latest_sources,
                {
                    "source_kind": "claude_jsonl",
                    "stable_snapshots": stable_snapshots,
                    "stable_seconds": float(stable_seconds),
                    "raw_event_count": len(latest_rows),
                    "window_sha256": _transcript_window_digest(latest_sources),
                },
            )
        time.sleep(0.2)
    raise RuntimeError("Claude provider transcript did not reach three stable source snapshots")


def _claude_effort_probe(
    *,
    binary: Path,
    artifact_root: Path,
    timeout: float,
    environment: Mapping[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    probe_id = "claude_effort_command"
    invocation = uuid4().hex
    output_root = artifact_root / "claude-effort" / invocation
    output_root.mkdir(parents=True, exist_ok=False)
    workspace = output_root / "workspace"
    workspace.mkdir()
    home = output_root / "home"
    home.mkdir(mode=0o700)
    config_dir = output_root / "claude-config"
    config_dir.mkdir(mode=0o700)
    terminal_path = output_root / "terminal.raw"
    env = dict(environment)
    env["HOME"] = str(home)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["LONGHOUSE_CLAUDE_BIN"] = str(binary)
    session = ProviderPtySession.start(
        argv=[str(binary), "--name", "Claude provider interaction probe", "--permission-mode", "dontAsk"],
        cwd=workspace,
        env=env,
        terminal_path=terminal_path,
        thread_name="claude-interaction-probe-terminal-drain",
    )
    try:
        confirmed_trust = False

        def launched_state():
            nonlocal confirmed_trust
            if not session.alive():
                raise RuntimeError(f"Claude interaction probe exited during launch ({session.process.returncode})")
            text = _terminal_text(terminal_path)
            compact = re.sub(r"\s+", "", text)
            if _looks_like_claude_auth_prompt(compact):
                raise RuntimeError("missing_isolated_auth: Claude opened its login flow in the isolated provider profile")
            if not confirmed_trust and "Yes,Itrustthisfolder" in compact:
                session.write(b"\r")
                confirmed_trust = True
                return None
            if "Yes,Itrustthisfolder" in compact:
                return None
            return True

        _wait(
            launched_state,
            timeout=timeout,
            message="Claude interaction probe did not become ready",
        )
        wait_for_terminal_quiescence(session, timeout=timeout)
        transcript_before = _transcript_snapshot(config_dir)
        session.submit_line("/effort high")

        def command_rows():
            rows, sources = _new_transcript_rows(transcript_before, config_dir=config_dir)
            rendered = [json.dumps(row, ensure_ascii=False) for row in rows]
            if any("<local-command-stdout>" in value for value in rendered) and any(
                "<command-name>/effort</command-name>" in value for value in rendered
            ):
                return rows, sources
            return None

        raw_rows, source_rows = _wait(
            command_rows,
            timeout=timeout,
            message="Claude did not persist the /effort local-command records",
        )
        # The command rows are the positive control evidence, but the full
        # post-submit transcript window is also part of the observation. Do
        # not discard an assistant row: its presence would disprove the
        # contract's no-model-turn assertion.
        wait_for_terminal_quiescence(session, timeout=timeout)
        raw_rows, source_rows, capture_receipt = _wait_for_transcript_quiescence(
            transcript_before,
            config_dir=config_dir,
            timeout=timeout,
            stable_seconds=_NEGATIVE_PROOF_QUIESCENCE_SECONDS,
        )
        provider_session_id = next(
            (
                str(row.get("sessionId") or row.get("session_id") or row.get("uuid") or "")
                for row in raw_rows
                if row.get("sessionId") or row.get("session_id") or row.get("uuid")
            ),
            "",
        )
        return (
            {
                "probe_id": probe_id,
                "disposition": "implemented",
                "status": "observed",
                "input_sequence": ["/effort high"],
                "acknowledgement": "local_command_stdout",
                "provider_session_id": provider_session_id,
                "longhouse_session_id": f"direct-{invocation}",
                "state_path": str(config_dir),
                "native_source_rows": source_rows,
                "raw_events": raw_rows,
                "terminal_path": str(terminal_path),
                "capture_complete": True,
                "post_interaction_quiescent": True,
                "capture_receipt": capture_receipt,
            },
            source_rows,
        )
    finally:
        session.close()


def _declared_probe_rows(provider: str) -> list[dict[str, Any]]:
    contract = contract_for_provider(provider)
    if contract is None:
        raise ValueError(f"managed provider contract missing for {provider!r}")
    rows: list[dict[str, Any]] = []
    for probe in contract.interaction_probes:
        if probe.disposition in {"policy_disabled", "upstream_absent"}:
            rows.append(
                {
                    "probe_id": probe.probe_id,
                    "disposition": probe.disposition,
                    "status": "not_applicable",
                    "raw_events": [],
                }
            )
        else:
            rows.append(
                {
                    "probe_id": probe.probe_id,
                    "disposition": probe.disposition,
                    "status": "blocked",
                    "failure_code": "interaction_probe_canary_not_implemented",
                    "raw_events": [],
                }
            )
    return rows


def produce_live_observation(
    provider: str,
    *,
    provider_bin: Path | None,
    artifact_root: Path,
    qualification_request_digest: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run the implemented no-token probes and return a replayable artifact."""

    contract = contract_for_provider(provider)
    if contract is None:
        raise ValueError(f"managed provider contract missing for {provider!r}")
    if provider == "antigravity":
        return {
            "schema_version": 1,
            "artifact_kind": "provider_interaction_semantics_observation",
            "provider": provider,
            "evidence_class": "hermetic",
            "synthetic": True,
            "probes": _declared_probe_rows(provider),
            "raw_events": [],
            "native_source_rows": [],
            "reason": "Antigravity is Shadow-only; managed TUI control is policy-disabled.",
        }

    binary = _resolve_binary(provider, provider_bin)
    no_token_environment = _no_token_environment()
    observation: dict[str, Any] = {
        "schema_version": 1,
        "artifact_kind": "provider_interaction_semantics_observation",
        "provider": provider,
        "evidence_class": "live_no_token",
        "synthetic": False,
        "provider_bin": str(binary),
        "provider_version": _provider_version(provider, binary, environment=no_token_environment),
        "provider_executable_identity": f"sha256:{_sha256(binary)}",
        "started_at": datetime.now(UTC).isoformat(),
        "probes": _declared_probe_rows(provider),
        "raw_events": [],
        "native_source_rows": [],
        "native_source_root": str(artifact_root.resolve()),
    }
    if qualification_request_digest is not None:
        observation["qualification_request_digest"] = qualification_request_digest
    if provider == "claude":
        try:
            probe, source_rows = _claude_effort_probe(
                binary=binary,
                artifact_root=artifact_root,
                timeout=timeout,
                environment=no_token_environment,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            probe = next(row for row in observation["probes"] if row["probe_id"] == "claude_effort_command")
            terminal_path = artifact_root / "claude-effort"
            failure_code = "interaction_probe_setup_failed"
            terminal_files = sorted(terminal_path.glob("**/terminal.raw"))
            if terminal_files:
                terminal_text = re.sub(r"\s+", "", _terminal_text(terminal_files[-1]))
                if str(exc).startswith("missing_isolated_auth") or _looks_like_claude_auth_prompt(terminal_text):
                    failure_code = "missing_isolated_auth"
            probe = {
                **probe,
                "status": "blocked",
                "failure_code": failure_code,
                "message": str(exc),
                "raw_events": [],
            }
            source_rows = []
            observation["setup_failure"] = {"failure_code": failure_code, "message": str(exc)}
        probe_rows = []
        for row in observation["probes"]:
            probe_rows.append(probe if row["probe_id"] == probe["probe_id"] else row)
        observation["probes"] = probe_rows
        observation["raw_events"] = list(probe.get("raw_events") or [])
        observation["native_source_rows"] = source_rows
    elif provider == "codex":
        rows, source_rows = _codex_model_probe(
            binary=binary,
            artifact_root=artifact_root,
            timeout=timeout,
            environment=no_token_environment,
        )
        observation["probes"] = rows
        observation["raw_events"] = [event for row in rows for event in row.get("raw_events") or []]
        observation["native_source_rows"] = source_rows
    elif provider == "opencode":
        rows, source_rows = _opencode_interaction_probes(
            binary=binary,
            artifact_root=artifact_root,
            timeout=timeout,
            environment=no_token_environment,
        )
        observation["probes"] = rows
        observation["raw_events"] = [event for row in rows for event in row.get("raw_events") or []]
        observation["native_source_rows"] = source_rows
    elif provider == "cursor":
        rows, source_rows = _cursor_model_probe(
            binary=binary,
            artifact_root=artifact_root,
            timeout=timeout,
            environment=no_token_environment,
        )
        observation["probes"] = rows
        observation["raw_events"] = [event for row in rows for event in row.get("raw_events") or []]
        observation["native_source_rows"] = source_rows
    for row in observation.get("probes") or []:
        if isinstance(row, dict) and row.get("native_source_rows"):
            row["native_source_root"] = str(artifact_root.resolve())
    return observation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--provider-bin")
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT_SECONDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    observation = produce_live_observation(
        args.provider,
        provider_bin=Path(args.provider_bin) if args.provider_bin else None,
        artifact_root=args.artifact_root,
        timeout=args.timeout,
    )
    output = args.artifact_root / "provider-interaction-observation.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(observation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"artifact": str(output), "provider": args.provider}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
