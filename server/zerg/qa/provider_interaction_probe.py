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
import shutil
import sqlite3
import subprocess
import tempfile
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
from zerg.qa.codex_auth import CodexAuthError
from zerg.qa.codex_auth import login_with_api_key
from zerg.qa.managed_claude_live import strip_terminal_controls
from zerg.qa.provider_interaction_semantics import MIN_NEGATIVE_PROOF_QUIESCENCE_SECONDS
from zerg.qa.provider_interaction_semantics import raw_event_digest
from zerg.qa.provider_interaction_semantics import semantic_boundary_fixture
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
        "LONGHOUSE_CURSOR_GATE0_ARTIFACT",
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


_TERMINAL_SECRET_PATTERNS = (
    (re.compile(rb"sk-ant-api\d{2}-[A-Za-z0-9_-]+"), b"sk-ant-<redacted>"),
    (re.compile(rb"sk-ant-[A-Za-z0-9_-]*\.\.\.[A-Za-z0-9_-]+"), b"sk-ant-<redacted>"),
    (re.compile(rb"sk-or-v1-[A-Za-z0-9_-]+"), b"sk-or-v1-<redacted>"),
    (re.compile(rb"sk-proj-[A-Za-z0-9_-]+"), b"sk-proj-<redacted>"),
    (re.compile(rb"crsr_[A-Za-z0-9]+"), b"crsr_<redacted>"),
    (re.compile(rb"sk-[A-Za-z0-9_-]{20,}"), b"sk-<redacted>"),
)


def _secret_values(environment: Mapping[str, str]) -> tuple[bytes, ...]:
    return tuple(
        sorted(
            {value.encode("utf-8") for name in _NO_TOKEN_AUTH_ENV_NAMES if (value := str(environment.get(name) or "").strip())},
            key=len,
            reverse=True,
        )
    )


def _redact_bytes(value: bytes, *, secrets: tuple[bytes, ...] = ()) -> bytes:
    redacted = value
    for secret in secrets:
        redacted = redacted.replace(secret, b"<provider-secret-redacted>")
    for pattern, replacement in _TERMINAL_SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _redact_terminal_file(path: Path, *, secrets: tuple[bytes, ...] = ()) -> None:
    """Remove provider key material that a TUI may echo during onboarding."""

    try:
        original = path.read_bytes()
    except OSError:
        return
    redacted = _redact_bytes(original, secrets=secrets)
    if redacted != original:
        try:
            path.write_bytes(redacted)
        except OSError:
            return


def _native_source_snapshot(root: Path, *, artifact_root: Path, limit: int = 128) -> list[dict[str, Any]]:
    """Return redacted, hash-addressed evidence for an isolated provider root."""

    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        paths = sorted(path for path in root.rglob("*") if path.is_file())
    except OSError:
        return []
    if len(paths) > limit:
        return []
    for path in paths:
        evidence = _file_evidence(path, artifact_root=artifact_root)
        if evidence is not None:
            rows.append(evidence)
    return rows


def _write_native_event_capture(
    events: list[dict[str, Any]],
    *,
    path: Path,
    artifact_root: Path,
    completion_signal: str,
    completion_status: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Materialize parsed provider events with byte-exact source receipts.

    ``native_source_rows`` are deliberately more precise than a list of files
    that happened to change. The semantic oracle must be able to reopen the
    artifact, locate every parsed event at a unique byte offset, and verify
    that the event was not assembled from terminal text or a self-reported
    boolean. A completed JSONL stream uses process EOF as its capture
    completion signal; transcript-backed probes use the longer stability
    receipt produced by ``_wait_for_transcript_quiescence``.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True) for event in events]
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    file_bytes = path.read_bytes()
    file_digest = hashlib.sha256(file_bytes).hexdigest()
    try:
        source_path = str(path.resolve().relative_to(artifact_root.resolve()))
    except ValueError as exc:
        raise RuntimeError("native event capture must be inside the qualification artifact root") from exc

    source_rows: list[dict[str, Any]] = []
    offset = 0
    for line, event in zip(lines, events, strict=True):
        line_bytes = line.encode("utf-8")
        source_rows.append(
            {
                "source_path": source_path,
                "source_offset": offset,
                "line": line,
                "line_sha256": hashlib.sha256(line_bytes).hexdigest(),
                "event_sha256": raw_event_digest(event),
                "source_binding": "file_bytes_at_offset",
                "source_file_bytes": len(file_bytes),
                "source_file_sha256": file_digest,
            }
        )
        offset += len(line_bytes) + 1

    receipt = {
        "source_kind": "provider_jsonl_stream",
        "completion_signal": completion_signal,
        "completion_status": completion_status,
        "stable_snapshots": 1,
        "stable_seconds": 0.0,
        "raw_event_count": len(source_rows),
        "window_sha256": _transcript_window_digest(source_rows),
    }
    return source_rows, receipt


def _terminal_evidence(
    path: Path,
    *,
    artifact_root: Path,
    secrets: tuple[bytes, ...] = (),
) -> dict[str, Any] | None:
    _redact_terminal_file(path, secrets=secrets)
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


def _codex_rollout_paths(codex_home: Path) -> list[Path]:
    """Return only rollout files owned by this isolated Codex home."""

    sessions_root = codex_home / "sessions"
    if not sessions_root.exists():
        return []
    return sorted(path for path in sessions_root.rglob("*.jsonl") if path.is_file())


def _materialize_codex_native_file(
    path: Path,
    *,
    codex_home: Path,
    evidence_root: Path,
    secrets: tuple[bytes, ...] = (),
) -> Path | None:
    try:
        relative_path = path.resolve().relative_to(codex_home.resolve())
        file_bytes = path.read_bytes()
    except (OSError, ValueError):
        return None
    if any(secret and secret in file_bytes for secret in secrets):
        return None
    destination = evidence_root / relative_path
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(file_bytes)
    except OSError:
        return None
    return destination


def _codex_native_store_snapshot(
    codex_home: Path,
    *,
    artifact_root: Path,
    evidence_root: Path,
    secrets: tuple[bytes, ...] = (),
) -> list[dict[str, Any]]:
    """Hash non-secret Codex state relevant to the native archive absence proof."""

    if not codex_home.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        paths = sorted(path for path in codex_home.rglob("*") if path.is_file())
    except OSError:
        return []
    if len(paths) > 128:
        return []
    for path in paths:
        if path.name == "auth.json" or ".tmp" in path.parts:
            continue
        if path.suffix not in {".jsonl", ".sqlite", ".sqlite-shm", ".sqlite-wal"}:
            continue
        materialized = _materialize_codex_native_file(
            path,
            codex_home=codex_home,
            evidence_root=evidence_root,
            secrets=secrets,
        )
        evidence = _file_evidence(materialized, artifact_root=artifact_root) if materialized is not None else None
        if evidence is not None:
            rows.append(evidence)
    return rows


def _codex_native_interaction_capture(
    codex_home: Path,
    *,
    artifact_root: Path,
    evidence_root: Path,
    secrets: tuple[bytes, ...] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Extract native records that characterize ``/model`` then cancel.

    Codex does not persist the slash command as a user message. Its rollout
    records persist the model context and the cancellation event. The absence
    of a user-message row is therefore part of the provider evidence, while
    the records below remain byte-addressed to the real rollout file.
    """

    events: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for path in _codex_rollout_paths(codex_home):
        try:
            file_bytes = path.read_bytes()
        except OSError:
            continue
        materialized = _materialize_codex_native_file(
            path,
            codex_home=codex_home,
            evidence_root=evidence_root,
            secrets=secrets,
        )
        if materialized is None:
            continue
        file_digest = hashlib.sha256(file_bytes).hexdigest()
        try:
            source_path = str(materialized.resolve().relative_to(artifact_root.resolve()))
        except ValueError:
            continue
        for offset, line_bytes in _jsonl_bytes(file_bytes, start_offset=0):
            try:
                row = json.loads(line_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(row, dict):
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            row_type = str(row.get("type") or "")
            payload_type = str(payload.get("type") or "")
            is_model_context = row_type == "turn_context" and bool(str(payload.get("model") or "").strip())
            message = str(payload.get("message") or "")
            is_cancel = row_type == "event_msg" and (payload_type in {"user_message", "agent_message"} and "cancel" in message.lower())
            if not (is_model_context or is_cancel):
                continue
            events.append(row)
            sources.append(
                {
                    "source_path": source_path,
                    "source_offset": offset,
                    "line": line_bytes.decode("utf-8"),
                    "line_sha256": hashlib.sha256(line_bytes).hexdigest(),
                    "event_sha256": raw_event_digest(row),
                    "source_binding": "file_bytes_at_offset",
                    "source_file_bytes": len(file_bytes),
                    "source_file_sha256": file_digest,
                }
            )
    if not events:
        current_sources = _codex_native_store_snapshot(
            codex_home,
            artifact_root=artifact_root,
            evidence_root=evidence_root,
            secrets=secrets,
        )
        stable_snapshots = 1
        stable_seconds = 0.0
        for _ in range(6):
            time.sleep(_NEGATIVE_PROOF_QUIESCENCE_SECONDS)
            next_sources = _codex_native_store_snapshot(
                codex_home,
                artifact_root=artifact_root,
                evidence_root=evidence_root,
                secrets=secrets,
            )
            if next_sources == current_sources:
                stable_snapshots += 1
                stable_seconds += _NEGATIVE_PROOF_QUIESCENCE_SECONDS
                if stable_snapshots >= 3:
                    break
            else:
                stable_snapshots = 1
                stable_seconds = 0.0
                current_sources = next_sources
        if stable_snapshots < 3 or not current_sources or _codex_rollout_paths(codex_home):
            return [], [], {}
        return (
            [],
            current_sources,
            {
                "source_kind": "codex_rollout_jsonl_negative",
                "negative_evidence": True,
                "completion_signal": "stable_native_store",
                "completion_status": 0,
                "stable_snapshots": stable_snapshots,
                "stable_seconds": stable_seconds,
                "raw_event_count": 0,
                "native_event_count": 0,
                "provider_store": {
                    "store_kind": "codex_rollout_jsonl",
                    "rollout_file_count": 0,
                    "file_count": len(current_sources),
                },
                "provider_store_root": str(evidence_root.resolve().relative_to(artifact_root.resolve())),
            },
        )
    return (
        events,
        sources,
        {
            "source_kind": "codex_rollout_jsonl",
            "completion_signal": "stable_native_store",
            "stable_snapshots": 3,
            "stable_seconds": _NEGATIVE_PROOF_QUIESCENCE_SECONDS,
            "raw_event_count": len(events),
            "window_sha256": _transcript_window_digest(sources),
        },
    )


def _opencode_database_path(data_root: Path) -> Path | None:
    candidates = [data_root / "opencode" / "opencode.db", data_root / "opencode.db"]
    return next((path for path in candidates if path.is_file()), None)


def _opencode_cli_model(model: str | None) -> str | None:
    """Translate the factory's OpenRouter API slug to OpenCode's model ID."""

    value = str(model or "").strip()
    if not value:
        return None
    return value if value.startswith("openrouter/") else f"openrouter/{value}"


def _opencode_native_interaction_capture(
    data_root: Path,
    *,
    output_root: Path,
    artifact_root: Path,
    probe_id: str,
    secrets: tuple[bytes, ...] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Capture OpenCode's native SQLite event for the reset probe.

    ``/new`` creates a second ``session.created.1`` event in the isolated
    database. ``/help`` creates no event or message row; that negative result
    is recorded as a stable native absence rather than fabricated as a
    transcript event.
    """

    database = _opencode_database_path(data_root)
    if database is None:
        return [], [], {}

    def native_snapshot() -> tuple[list[dict[str, Any]], tuple[tuple[str, bytes], ...]] | None:
        try:
            paths = sorted(path for path in data_root.rglob("*") if path.is_file())
        except OSError:
            return None
        if len(paths) > 128:
            return None
        rows: list[dict[str, Any]] = []
        payload: list[tuple[str, bytes]] = []
        for path in paths:
            try:
                relative_path = str(path.resolve().relative_to(data_root.resolve()))
                file_bytes = path.read_bytes()
            except (OSError, ValueError):
                return None
            if any(secret and secret in file_bytes for secret in secrets):
                return None
            rows.append(
                {
                    "source_path": relative_path,
                    "bytes": len(file_bytes),
                    "sha256": hashlib.sha256(file_bytes).hexdigest(),
                }
            )
            payload.append((relative_path, file_bytes))
        return rows, tuple(payload)

    def snapshot() -> tuple[list[tuple[Any, ...]], int, int, int, str, list[dict[str, Any]], tuple[tuple[str, bytes], ...]] | None:
        native_capture = native_snapshot()
        if native_capture is None:
            return None
        native_rows, native_payload = native_capture
        try:
            with sqlite3.connect(database) as connection:
                event_rows = connection.execute("SELECT id, type, data FROM event ORDER BY id").fetchall()
                message_count = int(connection.execute("SELECT count(*) FROM message").fetchone()[0])
                part_count = int(connection.execute("SELECT count(*) FROM part").fetchone()[0])
                session_count = int(connection.execute("SELECT count(*) FROM session").fetchone()[0])
            database_source_path = str(database.resolve().relative_to(data_root.resolve()))
            database_bytes = dict(native_payload).get(database_source_path)
            if database_bytes is None:
                return None
            return (
                event_rows,
                message_count,
                part_count,
                session_count,
                hashlib.sha256(database_bytes).hexdigest(),
                native_rows,
                native_payload,
            )
        except (OSError, sqlite3.Error, ValueError):
            return None

    current_snapshot = snapshot()
    if current_snapshot is None:
        return [], [], {}
    stable_snapshots = 1
    stable_seconds = 0.0
    for _ in range(6):
        time.sleep(_NEGATIVE_PROOF_QUIESCENCE_SECONDS)
        next_snapshot = snapshot()
        if next_snapshot is not None and next_snapshot == current_snapshot:
            stable_snapshots += 1
            stable_seconds += _NEGATIVE_PROOF_QUIESCENCE_SECONDS
            if stable_snapshots >= 3:
                break
        else:
            stable_snapshots = 1
            stable_seconds = 0.0
        if next_snapshot is not None:
            current_snapshot = next_snapshot
    if stable_snapshots < 3:
        return [], [], {}
    event_rows, message_count, part_count, session_count, database_digest, _native_rows, native_payload = current_snapshot
    frozen_root = output_root / "opencode-native-store"
    frozen_rows: list[dict[str, Any]] = []
    frozen_database_path: str | None = None
    try:
        database_source_path = str(database.resolve().relative_to(data_root.resolve()))
        sqlite_sidecar_paths = {
            database_source_path,
            f"{database_source_path}-wal",
            f"{database_source_path}-shm",
        }
        for index, (source_path, file_bytes) in enumerate(native_payload):
            destination = (
                frozen_root / "database" / Path(source_path).name
                if source_path in sqlite_sidecar_paths
                else frozen_root / f"{index:03d}-{Path(source_path).name}"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(file_bytes)
            frozen_path = str(destination.resolve().relative_to(artifact_root.resolve()))
            frozen_rows.append(
                {
                    "source_path": frozen_path,
                    "bytes": len(file_bytes),
                    "sha256": hashlib.sha256(file_bytes).hexdigest(),
                }
            )
            if source_path == database_source_path:
                frozen_database_path = frozen_path
    except (OSError, ValueError):
        return [], [], {}
    if frozen_database_path is None:
        return [], [], {}
    frozen_database_digest = next(row["sha256"] for row in frozen_rows if row["source_path"] == frozen_database_path)
    if probe_id == "opencode_help_command":
        if len(event_rows) != 1 or message_count != 0 or part_count != 0 or session_count != 1:
            return [], [], {}
        return (
            [],
            frozen_rows,
            {
                "source_kind": "opencode_sqlite_negative",
                "negative_evidence": True,
                "completion_signal": "stable_native_store",
                "completion_status": 0,
                "stable_snapshots": stable_snapshots,
                "stable_seconds": stable_seconds,
                "raw_event_count": 0,
                "native_event_count": 0,
                "provider_database": {
                    "store_kind": "opencode_sqlite",
                    "source_path": frozen_database_path,
                    "source_sha256": frozen_database_digest,
                    "event_count": len(event_rows),
                    "session_count": session_count,
                    "message_count": message_count,
                    "part_count": part_count,
                },
                "provider_store_root": str(frozen_root.resolve().relative_to(artifact_root.resolve())),
            },
        )
    if probe_id != "opencode_new_boundary" or len(event_rows) < 2:
        return [], [], {}
    selected = event_rows[-1:]
    events: list[dict[str, Any]] = []
    for event_id, event_type, raw_data in selected:
        try:
            data = json.loads(str(raw_data))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        events.append({"event_id": str(event_id), "type": str(event_type), "data": data})
    if not events:
        return [], [], {}
    capture_path = output_root / "opencode-native-events.jsonl"
    source_rows, receipt = _write_native_event_capture(
        events,
        path=capture_path,
        artifact_root=artifact_root,
        completion_signal="stable_native_store",
        completion_status=0,
    )
    receipt.update(
        {
            "stable_snapshots": stable_snapshots,
            "stable_seconds": stable_seconds,
            "provider_database": {
                "source_path": frozen_database_path,
                "source_sha256": frozen_database_digest,
                "event_count": len(event_rows),
            },
        }
    )
    return events, source_rows, receipt


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
    native_capture: Any | None = None,
) -> dict[str, Any]:
    """Run one isolated TUI interaction and retain honest evidence."""

    terminal_path = output_root / "terminal.raw"
    env = dict(environment)
    secrets = _secret_values(env)
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
            if command == "(escape)":
                session.write(b"\x1b")
                submitted.append(command)
                continue
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
        terminal_evidence = _terminal_evidence(terminal_path, artifact_root=artifact_root, secrets=secrets)
        if native_capture is not None:
            process_status = session.process.returncode
            session.close()
            session = None
            raw_events, native_source_rows, capture_receipt = native_capture(process_status)
            if raw_events and native_source_rows:
                row = _probe_status_row(
                    probe,
                    status="observed",
                    terminal_evidence=terminal_evidence,
                    native_source_rows=native_source_rows,
                    submitted_input_sequence=submitted,
                    raw_events=raw_events,
                    terminal_acknowledged=True,
                    capture_complete=True,
                    post_interaction_quiescent=True,
                    provider_state_after=False,
                )
                row["capture_receipt"] = capture_receipt
                row["native_source_root"] = str(artifact_root.resolve())
                return row
            if capture_receipt.get("negative_evidence") and native_source_rows:
                row = _probe_status_row(
                    probe,
                    status="observed_absence",
                    terminal_evidence=terminal_evidence,
                    native_source_rows=native_source_rows,
                    submitted_input_sequence=submitted,
                    terminal_acknowledged=True,
                    capture_complete=True,
                    post_interaction_quiescent=True,
                    provider_state_after=False,
                )
                row["capture_receipt"] = capture_receipt
                row["native_source_root"] = str(artifact_root.resolve())
                return row
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
        terminal_evidence = _terminal_evidence(terminal_path, artifact_root=artifact_root, secrets=secrets)
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
        _redact_terminal_file(terminal_path, secrets=secrets)


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
    evidence_root = output_root / "codex-native-store"
    env = dict(environment)
    runtime_home = Path(tempfile.mkdtemp(prefix="longhouse-codex-interaction-"))
    home = runtime_home / "home"
    home.mkdir(mode=0o700)
    codex_home = runtime_home / "codex-home"
    codex_home.mkdir(mode=0o700)
    env["CODEX_HOME"] = str(codex_home)
    secrets = _secret_values(env)
    auth_receipt: dict[str, str] | None = None
    api_key = str(env.get("CODEX_API_KEY") or "").strip()
    try:
        if api_key:
            auth_receipt = login_with_api_key(
                binary,
                api_key=api_key,
                environment=env,
                cwd=workspace,
                timeout=min(timeout, 30),
            )
            # The provider-native auth file is now the only credential source
            # for the TUI child. This proves the factory's internal env name is
            # not being mistaken for a stock Codex auth mechanism.
            env.pop("CODEX_API_KEY", None)
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
            native_capture=lambda _process_status: _codex_native_interaction_capture(
                codex_home,
                artifact_root=artifact_root,
                evidence_root=evidence_root,
                secrets=secrets,
            ),
        )
        if auth_receipt is not None:
            row["authentication"] = auth_receipt
        return [row], list(row.get("native_source_rows") or [])
    except CodexAuthError as exc:
        return [
            _probe_status_row(
                probe,
                status="blocked",
                failure_code="missing_isolated_auth",
                message=str(exc),
                submitted_input_sequence=[],
            )
        ], []
    finally:
        shutil.rmtree(runtime_home, ignore_errors=True)


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
        runtime_root = Path(tempfile.mkdtemp(prefix="longhouse-opencode-interaction-"))
        try:
            home = runtime_root / "home"
            home.mkdir(mode=0o700)
            data_root = runtime_root / "data"
            config_root = runtime_root / "config"
            cache_root = runtime_root / "cache"
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
            secrets = _secret_values(env)
            model = _opencode_cli_model(env.get("LONGHOUSE_OPENCODE_QUALIFICATION_MODEL"))
            argv = [str(binary)]
            if model is not None:
                argv.extend(("--model", model))
            argv.extend((str(workspace), "--pure", "--mini", "--no-replay"))
            row = _run_terminal_interaction_probe(
                provider="opencode",
                probe=probe,
                artifact_root=artifact_root,
                output_root=output_root,
                workspace=workspace,
                home=home,
                native_root=data_root,
                environment=env,
                argv=argv,
                ready_markers=("Ask anything",),
                acknowledgement_markers=("help",) if probe.probe_id == "opencode_help_command" else ("session",),
                timeout=timeout,
                native_capture=lambda _process_status,
                data_root=data_root,
                output_root=output_root,
                probe_id=probe.probe_id,
                secrets=secrets: _opencode_native_interaction_capture(
                    data_root,
                    output_root=output_root,
                    artifact_root=artifact_root,
                    probe_id=probe_id,
                    secrets=secrets,
                ),
            )
            rows.append(row)
            native_rows.extend(row.get("native_source_rows") or [])
        finally:
            shutil.rmtree(runtime_root, ignore_errors=True)
    return rows, native_rows


def _cursor_model_probe_with_runtime_home(
    *,
    binary: Path,
    artifact_root: Path,
    timeout: float,
    environment: Mapping[str, str],
    runtime_home: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    contract = contract_for_provider("cursor")
    assert contract is not None
    probe = next(row for row in contract.interaction_probes if row.probe_id == "cursor_model_launch_option")
    invocation = uuid4().hex
    output_root = artifact_root / "cursor-model" / invocation
    output_root.mkdir(parents=True, exist_ok=False)
    workspace = output_root / "workspace"
    workspace.mkdir()
    home = runtime_home / "home"
    home.mkdir(mode=0o700)
    events_path = output_root / "cursor-hooks.jsonl"
    env = dict(environment)
    secrets = _secret_values(env)
    env["LONGHOUSE_SESSION_ID"] = f"direct-{invocation}"
    env["LONGHOUSE_CURSOR_GATE0_EVENTS"] = str(events_path)
    env["HOME"] = str(home)
    # Cursor's default macOS credential store is the user's Keychain. The
    # factory runs on replaceable machines and must use the isolated profile's
    # file-backed store instead of waiting on or mutating desktop Keychain.
    env["AGENT_CLI_CREDENTIAL_STORE"] = "file"
    env["TERM"] = "xterm-256color"
    env["LINES"] = "40"
    env["COLUMNS"] = "132"
    from zerg.qa.cursor_helm_gate0 import write_project_hooks

    write_project_hooks(workspace, events_path)
    terminal_path = output_root / "terminal.raw"
    argv = [
        str(binary),
        "--print",
        "--output-format",
        "stream-json",
        "--trust",
        "--mode",
        "ask",
        "--workspace",
        str(workspace),
        "--model",
        str(environment.get("CURSOR_MODEL") or "auto"),
        "Longhouse provider qualification probe. Reply with one short word.",
    ]
    # Cursor's native profile is credential-bearing state. Keep it outside the
    # retained evidence tree; the stream-json result and hook receipt are the
    # provider-native evidence for this print probe.
    before_sources: list[dict[str, Any]] = []
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
    safe_stdout = _redact_bytes((result.stdout or "").encode("utf-8"), secrets=secrets).decode("utf-8", errors="replace")
    safe_stderr = _redact_bytes((result.stderr or "").encode("utf-8"), secrets=secrets).decode("utf-8", errors="replace")
    stdout_path.write_text(safe_stdout, encoding="utf-8")
    stderr_path.write_text(safe_stderr, encoding="utf-8")
    terminal_path.write_text(f"stdout\n{safe_stdout}\nstderr\n{safe_stderr}", encoding="utf-8")
    _redact_terminal_file(stdout_path, secrets=secrets)
    _redact_terminal_file(stderr_path, secrets=secrets)
    _redact_terminal_file(terminal_path, secrets=secrets)
    terminal_evidence = _terminal_evidence(terminal_path, artifact_root=artifact_root, secrets=secrets)
    native_rows: list[dict[str, Any]] = []
    file_evidence = _file_evidence(events_path, artifact_root=artifact_root)
    if file_evidence is not None:
        native_rows.append(file_evidence)
    combined = f"{safe_stdout}\n{safe_stderr}\n{events_path.read_text(encoding='utf-8') if events_path.exists() else ''}"
    stream_events = _cursor_stream_json_events(safe_stdout)
    init_event = next(
        (event for event in stream_events if event.get("type") == "system" and event.get("subtype") == "init"),
        None,
    )
    result_event = next(
        (
            event
            for event in stream_events
            if event.get("type") == "result" and event.get("subtype") == "success" and event.get("is_error") is not True
        ),
        None,
    )
    requested_model = str(environment.get("CURSOR_MODEL") or "auto").strip()
    observed_model = str((init_event or {}).get("model") or "").strip()
    api_key_source = str((init_event or {}).get("apiKeySource") or "").strip()
    model_selected = bool(observed_model) and (
        requested_model == "auto" or _cursor_model_identity(requested_model) == _cursor_model_identity(observed_model)
    )
    api_key_observed = api_key_source.lower() in {"apikey", "api_key", "env"}
    cursor_usage = _cursor_usage_evidence(result_event) if result_event is not None else None
    capture_path = output_root / "cursor-stream.jsonl"
    stream_source_rows, capture_receipt = _write_native_event_capture(
        stream_events,
        path=capture_path,
        artifact_root=artifact_root,
        completion_signal="process_exit",
        completion_status=result.returncode,
    )
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
    elif (
        result_event is not None
        and init_event is not None
        and model_selected
        and api_key_observed
        and bool(str(environment.get("CURSOR_API_KEY") or "").strip())
        and cursor_usage is not None
    ):
        cursor_result_event = {
            key: result_event[key]
            for key in ("type", "subtype", "session_id", "request_id", "duration_ms", "duration_api_ms")
            if key in result_event
        }
        cursor_result_event["usage"] = cursor_usage
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_write_input_tokens", "cacheRead", "cacheWrite"):
            if key in result_event:
                cursor_result_event[key] = result_event[key]
        cursor_result_event["accounting_status"] = "subscription_aggregate_unreported"
        cursor_result_event["accounting_status_source"] = "producer_observation_classification"
        cursor_result_event["model_source"] = "provider_event"
        cursor_result_event["model_source_event_sha256"] = raw_event_digest(init_event)
        cursor_result_event["native_event_sha256"] = raw_event_digest(result_event)
        row = _probe_status_row(
            probe,
            status="observed",
            terminal_evidence=terminal_evidence,
            native_source_rows=stream_source_rows,
            submitted_input_sequence=[f"cursor-agent --model {requested_model}"],
            raw_events=stream_events,
            terminal_acknowledged=True,
            capture_complete=True,
            post_interaction_quiescent=True,
            provider_state_after=True,
        )
        row["capture_receipt"] = capture_receipt
        row["native_source_root"] = str(artifact_root.resolve())
        row["provider_model"] = observed_model
        row["api_key_source"] = api_key_source
        source_artifacts = [
            {
                "path": str(capture_path.resolve()),
                "sha256": _sha256(capture_path),
                "kind": "provider_jsonl_stream",
                "event_type": result_event.get("type"),
                "event_sha256": raw_event_digest(result_event),
            }
        ]
        row["live_model_evidence"] = {
            "source_canary": "cursor_model_probe",
            "operation_evidence": {
                "live_token_behavior": {
                    "status": "pass",
                    "level": "live_token",
                }
            },
            "model": observed_model,
            "result_event": cursor_result_event,
            "source_artifacts": source_artifacts,
        }
    elif (
        stream_events
        and init_event is not None
        and result_event is not None
        and model_selected
        and api_key_observed
        and bool(str(environment.get("CURSOR_API_KEY") or "").strip())
        and cursor_usage is None
    ):
        row = _probe_status_row(
            probe,
            status="blocked",
            failure_code="cursor_usage_missing",
            message="Cursor completed a model request without exposing numeric token accounting in its native result event.",
            terminal_evidence=terminal_evidence,
            native_source_rows=stream_source_rows,
            submitted_input_sequence=[f"cursor-agent --model {requested_model}"],
            raw_events=stream_events,
            terminal_acknowledged=True,
        )
    elif stream_events and init_event is not None and result_event is not None:
        row = _probe_status_row(
            probe,
            status="blocked",
            failure_code="cursor_model_or_auth_binding_missing",
            message="Cursor emitted a completed stream, but did not bind the requested model to the configured API key.",
            terminal_evidence=terminal_evidence,
            native_source_rows=stream_source_rows,
            submitted_input_sequence=[f"cursor-agent --model {requested_model}"],
            raw_events=stream_events,
            terminal_acknowledged=True,
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
    return [row], stream_source_rows or native_rows or before_sources


def _cursor_model_probe(
    *,
    binary: Path,
    artifact_root: Path,
    timeout: float,
    environment: Mapping[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    runtime_home = Path(tempfile.mkdtemp(prefix="longhouse-cursor-interaction-"))
    try:
        return _cursor_model_probe_with_runtime_home(
            binary=binary,
            artifact_root=artifact_root,
            timeout=timeout,
            environment=environment,
            runtime_home=runtime_home,
        )
    finally:
        shutil.rmtree(runtime_home, ignore_errors=True)


def _cursor_stream_json_events(stdout: str) -> list[dict[str, Any]]:
    """Parse Cursor's documented one-JSON-object-per-line print stream."""

    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            event = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _cursor_model_identity(value: str) -> str:
    """Compare Cursor's CLI aliases with its human-readable init model name."""

    return re.sub(r"[^a-z0-9]", "", value.lower())


def _cursor_usage_evidence(result_event: Mapping[str, Any]) -> dict[str, Any] | None:
    """Keep provider-reported token accounting without inventing a price."""

    usage = result_event.get("usage")
    if isinstance(usage, Mapping):
        numeric_usage = {str(key): value for key, value in usage.items() if type(value) in {int, float}}
        if numeric_usage:
            return numeric_usage
    flat_keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_write_input_tokens",
        "cacheRead",
        "cacheWrite",
        "total_cost_usd",
        "cost",
    )
    flat_usage = {key: result_event[key] for key in flat_keys if type(result_event.get(key)) in {int, float}}
    return flat_usage or None


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


def _materialize_claude_source_rows(
    source_rows: list[dict[str, Any]],
    *,
    config_dir: Path,
    output_root: Path,
    artifact_root: Path,
    secrets: tuple[bytes, ...] = (),
) -> list[dict[str, Any]]:
    """Copy only the proven transcript files into retained evidence.

    Claude's profile can contain credentials, so the live profile stays in an
    external temporary directory. The semantic provenance gate still needs a
    byte-addressable source under the retained artifact root; copying the
    complete transcript file preserves every offset and digest while allowing
    the provider profile to be deleted after the probe.
    """

    materialized_root = output_root / "claude-native-store"
    copied: dict[Path, bytes] = {}
    remapped: list[dict[str, Any]] = []
    for source in source_rows:
        source_path = Path(str(source.get("source_path") or "")).expanduser()
        try:
            resolved_source = source_path.resolve(strict=True)
            relative = resolved_source.relative_to(config_dir.resolve())
            file_bytes = copied.get(resolved_source)
            if file_bytes is None:
                file_bytes = resolved_source.read_bytes()
                copied[resolved_source] = file_bytes
            if any(secret and secret in file_bytes for secret in secrets):
                raise RuntimeError("Claude native transcript contained configured credential material")
            destination = materialized_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and destination.read_bytes() != file_bytes:
                raise RuntimeError("Claude native transcript source changed during materialization")
            destination.write_bytes(file_bytes)
            remapped_source = dict(source)
            remapped_source["source_path"] = str(destination.resolve().relative_to(artifact_root.resolve()))
            remapped.append(remapped_source)
        except (OSError, ValueError) as exc:
            raise RuntimeError("Claude native transcript source could not be materialized") from exc
    return remapped


def _claude_command_window(
    rows: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    *,
    command: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drop Claude startup metadata and retain the native command window.

    A fresh Claude profile may create its JSONL transcript only after the
    first interaction, so an empty pre-submit file snapshot cannot always
    establish a byte offset. Startup rows are provider-native records, but
    they are outside this canary's semantic window. Once the exact command
    record is present, retain it, its local-command caveat, and every later
    native row so an unexpected assistant turn remains visible.
    """

    if len(rows) != len(sources):
        raise RuntimeError("Claude transcript rows and source receipts diverged")
    command_index = next(
        (index for index, row in enumerate(rows) if f"<command-name>{command}</command-name>" in json.dumps(row, ensure_ascii=False)),
        None,
    )
    if command_index is None:
        return [], []
    start = command_index
    if command_index > 0 and "<local-command-caveat>" in json.dumps(rows[command_index - 1], ensure_ascii=False):
        start -= 1
    return rows[start:], sources[start:]


def _claude_native_store_paths(config_dir: Path) -> list[Path]:
    candidates = [
        config_dir / ".claude.json",
        config_dir / "settings.json",
        config_dir / "history.jsonl",
        *_claude_transcript_paths(config_dir),
    ]
    return sorted({path for path in candidates if path.is_file()})


def _claude_native_store_capture(
    *,
    config_dir: Path,
    output_root: Path,
    artifact_root: Path,
    timeout: float,
    command: str,
    secrets: tuple[bytes, ...] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Freeze a stable Claude store snapshot for a bounded absence claim."""

    deadline = time.monotonic() + timeout
    previous_signature: tuple[tuple[str, int, str], ...] | None = None
    stable_snapshots = 0
    stable_since: float | None = None
    latest_paths: list[Path] = []
    while time.monotonic() < deadline:
        latest_paths = _claude_native_store_paths(config_dir)
        signature = tuple((str(path), path.stat().st_size, _sha256(path)) for path in latest_paths)
        if signature == previous_signature:
            stable_snapshots += 1
        else:
            previous_signature = signature
            stable_snapshots = 1
            stable_since = time.monotonic()
        if (
            stable_snapshots >= 3
            and stable_since is not None
            and time.monotonic() - stable_since >= _NEGATIVE_PROOF_QUIESCENCE_SECONDS
            and latest_paths
        ):
            store_root = output_root / "claude-native-store"
            for path in latest_paths:
                relative = path.relative_to(config_dir)
                target = store_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                file_bytes = path.read_bytes()
                if any(secret and secret in file_bytes for secret in secrets):
                    raise RuntimeError("Claude native store contained configured credential material")
                target.write_bytes(file_bytes)
            source_rows = _native_source_snapshot(store_root, artifact_root=artifact_root)
            return (
                source_rows,
                {
                    "negative_evidence": True,
                    "source_kind": "claude_jsonl_negative",
                    "completion_signal": "stable_native_store",
                    "completion_status": 0,
                    "stable_snapshots": stable_snapshots,
                    "stable_seconds": float(time.monotonic() - (stable_since or time.monotonic())),
                    "raw_event_count": 0,
                    "native_event_count": 0,
                    "provider_store": {
                        "store_kind": "claude_jsonl",
                        "target_command": command,
                        "matching_event_count": 0,
                        "rollout_file_count": 0,
                        "file_count": len(source_rows),
                    },
                    "provider_store_root": str(store_root.resolve().relative_to(artifact_root.resolve())),
                },
            )
        time.sleep(0.2)
    raise RuntimeError("Claude provider native store did not reach a stable absence window")


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


def _start_claude_probe_session(
    *,
    binary: Path,
    workspace: Path,
    home: Path,
    config_dir: Path,
    terminal_path: Path,
    environment: Mapping[str, str],
    timeout: float,
    session_name: str,
    secrets: tuple[bytes, ...] = (),
) -> ProviderPtySession:
    """Launch Claude in a fresh profile and prove provider onboarding ended."""

    env = dict(environment)
    env["HOME"] = str(home)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["LONGHOUSE_CLAUDE_BIN"] = str(binary)
    session = ProviderPtySession.start(
        argv=[str(binary), "--name", session_name, "--permission-mode", "dontAsk"],
        cwd=workspace,
        env=env,
        terminal_path=terminal_path,
        thread_name="claude-interaction-probe-terminal-drain",
    )
    try:
        confirmed_trust = False
        theme_attempts = 0
        api_key_attempts = 0
        security_notes_attempts = 0

        def onboarding_complete() -> bool:
            state_path = config_dir / ".claude.json"
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            if not isinstance(state, dict) or state.get("hasCompletedOnboarding") is not True:
                return False
            responses = state.get("customApiKeyResponses")
            return isinstance(responses, dict) and bool(responses.get("approved"))

        def launched_state():
            nonlocal api_key_attempts, confirmed_trust, security_notes_attempts, theme_attempts
            if not session.alive():
                raise RuntimeError(f"Claude interaction probe exited during launch ({session.process.returncode})")
            compact = re.sub(r"\s+", "", _terminal_text(terminal_path))
            if _looks_like_claude_auth_prompt(compact):
                raise RuntimeError("missing_isolated_auth: Claude opened its login flow in the isolated provider profile")
            # The terminal is append-only, so the original theme menu remains
            # visible forever. One Enter is enough; a second Enter can land on
            # the next API-key menu before this poll sees its new state.
            if theme_attempts == 0 and "Choosethetextstyle" in compact:
                session.write(b"\r")
                theme_attempts += 1
                return None
            if "DetectedacustomAPIkey" in compact and api_key_attempts == 0:
                session.write(b"\x1b[A")
                api_key_attempts = 1
                return None
            if "DetectedacustomAPIkey" in compact and api_key_attempts == 1:
                session.write(b"\r")
                api_key_attempts = 2
                return None
            if security_notes_attempts == 0 and ("Securitynotes" in compact or "PressEnte" in compact):
                session.write(b"\r")
                security_notes_attempts += 1
                return None
            if not confirmed_trust and "Yes,Itrustthisfolder" in compact:
                session.write(b"\r")
                confirmed_trust = True
                return None
            if api_key_attempts >= 2 and onboarding_complete():
                return True
            if "Yes,Itrustthisfolder" in compact or "DoyouwanttousethisAPIkey" in compact:
                return None
            # A fresh isolated profile is not ready merely because the first
            # menu disappeared. Wait for Claude's own profile state to prove
            # that the API-key and security onboarding completed.
            return None

        _wait(
            launched_state,
            timeout=timeout,
            message="Claude interaction probe did not become ready",
        )
        wait_for_terminal_quiescence(session, timeout=timeout)
        return session
    except BaseException:
        session.close()
        _redact_terminal_file(terminal_path, secrets=secrets)
        raise


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
    runtime_root = Path(tempfile.mkdtemp(prefix="longhouse-claude-effort-"))
    home = runtime_root / "home"
    home.mkdir(mode=0o700)
    config_dir = runtime_root / "claude-config"
    config_dir.mkdir(mode=0o700)
    terminal_path = output_root / "terminal.raw"
    secrets = _secret_values(environment)
    session: ProviderPtySession | None = None
    try:
        session = _start_claude_probe_session(
            binary=binary,
            workspace=workspace,
            home=home,
            config_dir=config_dir,
            terminal_path=terminal_path,
            environment=environment,
            timeout=timeout,
            session_name="Claude provider interaction probe",
            secrets=secrets,
        )
        transcript_before = _transcript_snapshot(config_dir)
        session.submit_line("/effort high")

        def command_rows():
            rows, sources = _new_transcript_rows(transcript_before, config_dir=config_dir)
            rows, sources = _claude_command_window(rows, sources, command="/effort")
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
        raw_rows, source_rows = _claude_command_window(raw_rows, source_rows, command="/effort")
        source_rows = _materialize_claude_source_rows(
            source_rows,
            config_dir=config_dir,
            output_root=output_root,
            artifact_root=artifact_root,
            secrets=secrets,
        )
        capture_receipt = {
            **capture_receipt,
            "raw_event_count": len(raw_rows),
            "window_sha256": _transcript_window_digest(source_rows),
        }
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
                "submitted_input_sequence": ["/effort high"],
                "acknowledgement": "local_command_stdout",
                "provider_session_id": provider_session_id,
                "longhouse_session_id": f"direct-{invocation}",
                "state_path": str((output_root / "claude-native-store").relative_to(artifact_root)),
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
        if session is not None:
            session.close()
        _redact_terminal_file(terminal_path, secrets=secrets)
        shutil.rmtree(runtime_root, ignore_errors=True)


def _claude_clear_probe(
    *,
    binary: Path,
    artifact_root: Path,
    timeout: float,
    environment: Mapping[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    probe_id = "claude_clear_boundary"
    invocation = uuid4().hex
    output_root = artifact_root / "claude-clear" / invocation
    output_root.mkdir(parents=True, exist_ok=False)
    workspace = output_root / "workspace"
    workspace.mkdir()
    runtime_root = Path(tempfile.mkdtemp(prefix="longhouse-claude-clear-"))
    home = runtime_root / "home"
    home.mkdir(mode=0o700)
    config_dir = runtime_root / "claude-config"
    config_dir.mkdir(mode=0o700)
    terminal_path = output_root / "terminal.raw"
    secrets = _secret_values(environment)
    session: ProviderPtySession | None = None
    try:
        session = _start_claude_probe_session(
            binary=binary,
            workspace=workspace,
            home=home,
            config_dir=config_dir,
            terminal_path=terminal_path,
            environment=environment,
            timeout=timeout,
            session_name="Claude clear interaction probe",
            secrets=secrets,
        )
        transcript_before = _transcript_snapshot(config_dir)
        session.submit_line("/clear")

        def command_rows():
            rows, sources = _new_transcript_rows(transcript_before, config_dir=config_dir)
            rows, sources = _claude_command_window(rows, sources, command="/clear")
            rendered = [json.dumps(row, ensure_ascii=False) for row in rows]
            if any("<command-name>/clear</command-name>" in value for value in rendered) and any(
                "<local-command-stdout>" in value for value in rendered
            ):
                return rows, sources
            return None

        _wait(
            command_rows,
            timeout=timeout,
            message="Claude did not persist the /clear local-command records",
        )
        wait_for_terminal_quiescence(session, timeout=timeout)
        raw_rows, source_rows, capture_receipt = _wait_for_transcript_quiescence(
            transcript_before,
            config_dir=config_dir,
            timeout=timeout,
            stable_seconds=_NEGATIVE_PROOF_QUIESCENCE_SECONDS,
        )
        raw_rows, source_rows = _claude_command_window(raw_rows, source_rows, command="/clear")
        source_rows = _materialize_claude_source_rows(
            source_rows,
            config_dir=config_dir,
            output_root=output_root,
            artifact_root=artifact_root,
            secrets=secrets,
        )
        capture_receipt = {
            **capture_receipt,
            "raw_event_count": len(raw_rows),
            "window_sha256": _transcript_window_digest(source_rows),
        }
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
                "input_sequence": ["/clear"],
                "submitted_input_sequence": ["/clear"],
                "acknowledgement": "local_command_stdout",
                "provider_session_id": provider_session_id,
                "longhouse_session_id": f"direct-{invocation}",
                "state_path": str((output_root / "claude-native-store").relative_to(artifact_root)),
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
        if session is not None:
            session.close()
        _redact_terminal_file(terminal_path, secrets=secrets)
        shutil.rmtree(runtime_root, ignore_errors=True)


def _claude_model_picker_probe(
    *,
    binary: Path,
    artifact_root: Path,
    timeout: float,
    environment: Mapping[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    probe_id = "claude_model_picker"
    invocation = uuid4().hex
    output_root = artifact_root / "claude-model" / invocation
    output_root.mkdir(parents=True, exist_ok=False)
    workspace = output_root / "workspace"
    workspace.mkdir()
    runtime_root = Path(tempfile.mkdtemp(prefix="longhouse-claude-model-"))
    home = runtime_root / "home"
    home.mkdir(mode=0o700)
    config_dir = runtime_root / "claude-config"
    config_dir.mkdir(mode=0o700)
    terminal_path = output_root / "terminal.raw"
    secrets = _secret_values(environment)
    session: ProviderPtySession | None = None
    try:
        session = _start_claude_probe_session(
            binary=binary,
            workspace=workspace,
            home=home,
            config_dir=config_dir,
            terminal_path=terminal_path,
            environment=environment,
            timeout=timeout,
            session_name="Claude model picker interaction probe",
            secrets=secrets,
        )
        before_bytes = terminal_path.stat().st_size
        session.submit_line("/model")
        _wait(
            lambda: terminal_path.stat().st_size > before_bytes,
            timeout=timeout,
            message="Claude model picker did not render a terminal acknowledgement",
        )
        wait_for_terminal_quiescence(
            session,
            timeout=timeout,
            minimum_bytes=max(1000, before_bytes),
            stable_seconds=0.5,
        )
        session.write(b"\x1b")
        wait_for_terminal_quiescence(
            session,
            timeout=timeout,
            minimum_bytes=max(1000, before_bytes),
            stable_seconds=0.5,
        )
        session.close()
        session = None
        source_rows, capture_receipt = _claude_native_store_capture(
            config_dir=config_dir,
            output_root=output_root,
            artifact_root=artifact_root,
            timeout=timeout,
            command="/model",
            secrets=secrets,
        )
        terminal_evidence = _terminal_evidence(terminal_path, artifact_root=artifact_root, secrets=secrets)
        return (
            {
                "probe_id": probe_id,
                "disposition": "implemented",
                "status": "observed_absence",
                "input_sequence": ["/model", "(escape)"],
                "submitted_input_sequence": ["/model", "(escape)"],
                "acknowledgement": "terminal_model_picker_then_escape",
                "native_source_rows": source_rows,
                "raw_events": [],
                "terminal_path": str(terminal_path),
                "terminal_evidence": terminal_evidence,
                "terminal_acknowledged": True,
                "capture_complete": True,
                "post_interaction_quiescent": True,
                "provider_state_after": False,
                "capture_receipt": capture_receipt,
            },
            source_rows,
        )
    finally:
        if session is not None:
            session.close()
        _redact_terminal_file(terminal_path, secrets=secrets)
        shutil.rmtree(runtime_root, ignore_errors=True)


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
    evidence_class: str = "live_no_token",
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run provider probes with the evidence class declared by the request."""

    if evidence_class not in {"live_no_token", "live_token"}:
        raise ValueError(f"unsupported live interaction evidence class: {evidence_class}")

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
            "semantic_boundary": semantic_boundary_fixture(provider),
            "native_source_rows": [],
            "reason": "Antigravity is Shadow-only; managed TUI control is policy-disabled.",
        }

    binary = _resolve_binary(provider, provider_bin)
    probe_environment = _no_token_environment() if evidence_class == "live_no_token" else os.environ.copy()
    observation: dict[str, Any] = {
        "schema_version": 1,
        "artifact_kind": "provider_interaction_semantics_observation",
        "provider": provider,
        "evidence_class": evidence_class,
        "synthetic": False,
        "provider_bin": str(binary),
        "provider_version": _provider_version(provider, binary, environment=probe_environment),
        "provider_executable_identity": f"sha256:{_sha256(binary)}",
        "started_at": datetime.now(UTC).isoformat(),
        "probes": _declared_probe_rows(provider),
        "raw_events": [],
        "native_source_rows": [],
        "native_source_root": str(artifact_root.resolve()),
        "semantic_boundary": semantic_boundary_fixture(provider),
    }
    model_name = next(
        (
            str(probe_environment.get(name)).strip()
            for name in (
                "ANTHROPIC_MODEL",
                "CODEX_MODEL",
                "LONGHOUSE_OPENCODE_QUALIFICATION_MODEL",
                "CURSOR_MODEL",
            )
            if str(probe_environment.get(name) or "").strip()
        ),
        None,
    )
    if model_name:
        observation["model"] = model_name
    if qualification_request_digest is not None:
        observation["qualification_request_digest"] = qualification_request_digest
    if provider == "claude":
        producers = {
            "claude_effort_command": _claude_effort_probe,
            "claude_model_picker": _claude_model_picker_probe,
            "claude_clear_boundary": _claude_clear_probe,
        }
        observed_rows: dict[str, dict[str, Any]] = {}
        all_source_rows: list[dict[str, Any]] = []
        all_raw_events: list[dict[str, Any]] = []
        for probe_id, producer in producers.items():
            try:
                probe, source_rows = producer(
                    binary=binary,
                    artifact_root=artifact_root,
                    timeout=timeout,
                    environment=probe_environment,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                declared_probe = next(row for row in observation["probes"] if row["probe_id"] == probe_id)
                failure_code = "interaction_probe_setup_failed"
                if str(exc).startswith("missing_isolated_auth"):
                    failure_code = "missing_isolated_auth"
                probe = {
                    **declared_probe,
                    "status": "blocked",
                    "failure_code": failure_code,
                    "message": str(exc),
                    "raw_events": [],
                }
                source_rows = []
                observation.setdefault("setup_failures", []).append(
                    {"probe_id": probe_id, "failure_code": failure_code, "message": str(exc)}
                )
            observed_rows[probe_id] = probe
            all_source_rows.extend(source_rows)
            all_raw_events.extend(probe.get("raw_events") or [])
        observation["probes"] = [observed_rows.get(row["probe_id"], row) for row in observation["probes"]]
        observation["raw_events"] = all_raw_events
        observation["native_source_rows"] = all_source_rows
    elif provider == "codex":
        rows, source_rows = _codex_model_probe(
            binary=binary,
            artifact_root=artifact_root,
            timeout=timeout,
            environment=probe_environment,
        )
        observation["probes"] = rows
        observation["raw_events"] = [event for row in rows for event in row.get("raw_events") or []]
        observation["native_source_rows"] = source_rows
    elif provider == "opencode":
        rows, source_rows = _opencode_interaction_probes(
            binary=binary,
            artifact_root=artifact_root,
            timeout=timeout,
            environment=probe_environment,
        )
        observation["probes"] = rows
        observation["raw_events"] = [event for row in rows for event in row.get("raw_events") or []]
        observation["native_source_rows"] = source_rows
    elif provider == "cursor":
        rows, source_rows = _cursor_model_probe(
            binary=binary,
            artifact_root=artifact_root,
            timeout=timeout,
            environment=probe_environment,
        )
        observation["probes"] = rows
        observation["raw_events"] = [event for row in rows for event in row.get("raw_events") or []]
        observation["native_source_rows"] = source_rows
        evidence = next(
            (row.get("live_model_evidence") for row in rows if isinstance(row.get("live_model_evidence"), dict)),
            None,
        )
        if evidence is not None:
            observation["live_model_evidence"] = evidence
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
    parser.add_argument("--evidence-class", choices=("live_no_token", "live_token"), default="live_no_token")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    observation = produce_live_observation(
        args.provider,
        provider_bin=Path(args.provider_bin) if args.provider_bin else None,
        artifact_root=args.artifact_root,
        timeout=args.timeout,
        evidence_class=args.evidence_class,
    )
    output = args.artifact_root / "provider-interaction-observation.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(observation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"artifact": str(output), "provider": args.provider}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
