#!/usr/bin/env python3
"""Build synthetic, schema-shaped Longhouse engine soak state.

The fixtures contain invented identifiers and payloads only. State-file readers
ignore the explicit ``fixture_padding`` extension, which keeps Cinder's observed
median file sizes without copying any local contents.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_SHAPE = {
    "managed_local_omp_helm": {"count": 277, "median_bytes": 1600},
    "managed_local_codex_bridge": {"count": 299, "median_bytes": 2800},
    "managed_local_cursor_helm": {"count": 67, "median_bytes": 307},
    "managed_local_opencode": {"count": 77, "median_bytes": 307},
    "contracts_claude": {"count": 148, "median_bytes": 768},
    "contracts_codex": {"count": 115, "median_bytes": 819},
    "turn_claims": {"count": 789, "median_bytes": 1740},
    "source_epoch_registry": {"count": 51_000},
    "file_state": {"count": 2_300},
    "session_binding": {"count": 1_500},
    "session_phase_state": {"count": 600},
}


def _fixture_uuid(kind: str, index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"longhouse-perf-soak:{kind}:{index}"))


def _write_json(path: Path, value: dict[str, Any], target_bytes: int | None = None) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if target_bytes is not None:
        value["fixture_padding"] = ""
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if target_bytes is not None and len(data) < target_bytes:
        value["fixture_padding"] = "x" * (target_bytes - len(data))
        data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(data)
    return len(data)


def _write_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def _write_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")


def create_managed_active_fixture(
    home: Path,
    longhouse_home: Path,
    workspace: Path,
    *,
    state_target_bytes: int,
    provider_binary: Path,
    provider_binary_sha256: str,
    provider_pid: int,
    provider_process_start_time: str,
    launcher_pid: int,
    launcher_process_start_time: str,
    boot_id: str,
) -> dict[str, Any]:
    """Turn one representative OMP Helm fixture into a live session."""
    session_id = _fixture_uuid("omp-session", 0)
    run_id = _fixture_uuid("managed-active-run", 0)
    native_session_id = f"omp-{_fixture_uuid('omp-native', 0)}"
    thread_id = _fixture_uuid("managed-active-thread", 0)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    workspace_path = str(workspace.resolve())
    session_file = home / ".omp" / "agent" / "sessions" / f"{native_session_id}.jsonl"
    _write_jsonl(
        session_file,
        {"type": "session", "version": 3, "id": native_session_id, "cwd": workspace_path},
    )
    state_path = longhouse_home / "managed-local" / "omp-helm" / f"{session_id}.json"
    socket_path = longhouse_home / "agent" / "managed-active-omp.sock"
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.touch()
    source_claim_path = longhouse_home / "managed-local" / "claims" / f"{session_id}.json"
    source_claim = {
        "schema_version": 1,
        "session_id": session_id,
        "provider": "omp",
        "source_path": str(session_file),
        "native_session_id": native_session_id,
        "cwd": workspace_path,
        "provider_pid": provider_pid,
        "provider_start_time": provider_process_start_time,
        "state": "bound",
        "created_at": now,
        "updated_at": now,
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat().replace(
            "+00:00", "Z"
        ),
    }
    _write_json(source_claim_path, source_claim)
    state = {
        "schema_version": 1,
        "session_id": session_id,
        "run_id": run_id,
        "provider": "omp",
        "native_session_id": native_session_id,
        "session_file": str(session_file),
        "session_dir": str(session_file.parent),
        "cwd": workspace_path,
        "provider_binary": str(provider_binary),
        "provider_binary_sha256": provider_binary_sha256,
        "socket_path": str(socket_path),
        "channel_token": _fixture_uuid("managed-active-channel", 0),
        "connection_id": _fixture_uuid("managed-active-connection", 0),
        "lease_generation": _fixture_uuid("managed-active-generation", 0),
        "launcher_pid": launcher_pid,
        "launcher_process_start_time": launcher_process_start_time,
        "provider_pid": provider_pid,
        "provider_process_start_time": provider_process_start_time,
        "status": "ready",
        "phase": "thinking",
        "tool_name": None,
        "ready": True,
        "provider_session_started": True,
        "pending_transition": False,
        "initial_prompt_delivered": True,
        "agent_end_observed": False,
        "agent_end_is_terminal": None,
        "agent_end_will_continue": None,
        "agent_end_is_terminal_present": False,
        "agent_end_will_continue_present": False,
        "live_turn_seq": 1,
        "live_message_seq": 1,
        "terminal_state": None,
        "terminal_reason": None,
        "exit_code": None,
        "started_at": now,
        "updated_at": now,
    }
    _write_json(state_path, state, state_target_bytes)
    state_file_bytes = state_path.stat().st_size

    claim_path = longhouse_home / "agent" / "turn-claims" / f"{run_id}.json"
    claim = {
        "schema_version": 7,
        "run_id": run_id,
        "session_id": session_id,
        "thread_id": thread_id,
        "turn_id": _fixture_uuid("managed-active-turn", 0),
        "client_request_id": None,
        "provider_thread_id": native_session_id,
        "provider_identity_confirmed": True,
        "source_path": str(session_file),
        "provider": "omp",
        "state": "spawned",
        "claimed_at": now,
        "updated_at": now,
        "pid": provider_pid,
        "process_group_id": provider_pid,
        "boot_id": boot_id,
        "process_start_time": provider_process_start_time,
        "adapter": "omp_helm",
        "launch_id": _fixture_uuid("managed-active-launch", 0),
        "invocation_state": "running",
        "pending_count": 0,
        "pending_items": [],
        "origin": "synthetic_soak",
        "adopted_parked_invocation": False,
        "stdout_path": None,
        "stderr_path": None,
        "cancel_requested_at": None,
        "projected_stdout_offset": 0,
        "projected_seq": 0,
        "owned_processes": [
            {
                "pid": provider_pid,
                "process_group_id": provider_pid,
                "process_start_time": provider_process_start_time,
            },
            {
                "pid": launcher_pid,
                "process_group_id": launcher_pid,
                "process_start_time": launcher_process_start_time,
            },
        ],
        "result": None,
        "error": None,
        "terminal_event": None,
        "terminal_event_handed_off": False,
        "invocation_close_event": None,
        "invocation_close_event_handed_off": False,
    }
    _write_json(claim_path, claim)

    status_slot_path = longhouse_home / "agent" / "status" / f"{session_id}.json"
    status_slot = {
        "schema": 1,
        "session_id": session_id,
        "provider": "omp",
        "runtime_key": f"omp:{session_id}",
        "run_id": run_id,
        "source": "omp_helm_channel",
        "phase": "thinking",
        "tool_name": None,
        "observed_at": now,
        "payload": {"execution_lifetime": "interactive"},
        "preview": None,
        "producer_epoch": _fixture_uuid("managed-active-status-epoch", 0),
        "seq": 1,
    }
    _write_json(status_slot_path, status_slot)
    return {
        "session_id": session_id,
        "run_id": run_id,
        "native_session_id": native_session_id,
        "session_file": str(session_file),
        "state_file": str(state_path),
        "state_file_bytes": state_file_bytes,
        "turn_claim_file": str(claim_path),
        "status_slot_file": str(status_slot_path),
        "managed_source_claim_file": str(source_claim_path),
        "socket_path": str(socket_path),
        "provider_pid": provider_pid,
        "provider_process_start_time": provider_process_start_time,
        "launcher_pid": launcher_pid,
        "launcher_process_start_time": launcher_process_start_time,
        "workspace": workspace_path,
    }


def _database_rows(db_path: Path, home: Path, shape: dict[str, Any]) -> dict[str, int]:
    counts = {name: int(shape[name]["count"]) for name in (
        "source_epoch_registry", "file_state", "session_binding", "session_phase_state"
    )}
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    project = home / ".claude" / "projects" / "-synthetic-cinder-project"
    db = sqlite3.connect(db_path)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN")
        db.executemany(
            """INSERT INTO source_epoch_registry (
                source_epoch, provider, opaque_source_id, file_incarnation,
                start_reason, max_observed_len, source_revision,
                bound_session_id, provider_session_id, created_at, updated_at
            ) VALUES (?, 'claude', ?, ?, 'initial_discovery', ?, NULL, ?, ?, ?, ?)""",
            (
                (
                    _fixture_uuid("epoch", index),
                    "path-sha256:" + hashlib.sha256(f"{project}/transcript-{index}.jsonl".encode()).hexdigest(),
                    f"fixture-inode-{index}",
                    1024 + index % 4096,
                    _fixture_uuid("session", index % 1500),
                    f"synthetic-thread-{index}",
                    now,
                    now,
                )
                for index in range(counts["source_epoch_registry"])
            ),
        )
        db.executemany(
            """INSERT INTO file_state (
                path, provider, queued_offset, acked_offset, file_identity,
                acked_cursor_fingerprint, session_id, provider_session_id, last_updated
            ) VALUES (?, 'claude', ?, ?, NULL, NULL, ?, ?, ?)""",
            (
                (
                    str(project / f"legacy-{index}.jsonl"),
                    2048 + index,
                    2048 + index,
                    _fixture_uuid("session", index % 1500),
                    f"synthetic-thread-{index}",
                    now,
                )
                for index in range(counts["file_state"])
            ),
        )
        db.executemany(
            """INSERT INTO session_binding (
                path, session_id, provider, updated_at, provider_session_id,
                state, bound_at, last_seen_at
            ) VALUES (?, ?, 'claude', ?, ?, 'exited', ?, ?)""",
            (
                (
                    str(project / f"binding-{index}.jsonl"),
                    _fixture_uuid("session", index),
                    now,
                    f"synthetic-thread-{index}",
                    now,
                    now,
                )
                for index in range(counts["session_binding"])
            ),
        )
        db.executemany(
            """INSERT INTO session_phase_state (
                session_id, provider, phase, tool_name, source,
                observed_at, run_id, revision
            ) VALUES (?, 'claude', ?, ?, 'claude_hook', ?, ?, ?)""",
            (
                (
                    _fixture_uuid("phase-session", index),
                    "thinking" if index % 2 == 0 else "running",
                    "Bash" if index % 2 else None,
                    now,
                    _fixture_uuid("phase-run", index),
                    index + 1,
                )
                for index in range(counts["session_phase_state"])
            ),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
    return counts


def create_fixtures(
    home: Path,
    longhouse_home: Path,
    workspace: Path,
    db_path: Path,
    shape: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write synthetic engine-reader state and fill the initialized shipper DB."""
    shape = shape or DEFAULT_SHAPE
    home.mkdir(parents=True, exist_ok=True)
    longhouse_home.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    fake_bin = home / "fake-bin"
    fake_paths = {provider: fake_bin / provider for provider in ("claude", "codex", "omp", "opencode")}
    for path in fake_paths.values():
        _write_executable(path)
    omp_sha256 = hashlib.sha256(fake_paths["omp"].read_bytes()).hexdigest()

    file_families: dict[str, list[int]] = {}
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    workspace_path = str(workspace.resolve())

    omp_dir = longhouse_home / "managed-local" / "omp-helm"
    omp_transcripts = home / ".omp" / "agent" / "sessions"
    for index in range(int(shape["managed_local_omp_helm"]["count"])):
        session_id = _fixture_uuid("omp-session", index)
        native_id = f"omp-{_fixture_uuid('omp-native', index)}"
        transcript = omp_transcripts / f"{native_id}.jsonl"
        _write_jsonl(
            transcript,
            {"type": "session", "version": 3, "id": native_id, "cwd": workspace_path},
        )
        state = {
            "schema_version": 1,
            "provider": "omp",
            "session_id": session_id,
            "status": "stopped",
            "cwd": workspace_path,
            "provider_binary": str(fake_paths["omp"]),
            "provider_binary_sha256": omp_sha256,
            "native_session_id": native_id,
            "session_file": str(transcript),
            "phase": "completed",
            "started_at": now,
            "updated_at": now,
        }
        file_families.setdefault("managed_local_omp_helm", []).append(
            _write_json(omp_dir / f"{session_id}.json", state, int(shape["managed_local_omp_helm"]["median_bytes"]))
        )

    codex_dir = longhouse_home / "managed-local" / "codex-bridge"
    codex_sessions = codex_dir / "sessions"
    for index in range(int(shape["managed_local_codex_bridge"]["count"])):
        session_id = _fixture_uuid("codex-bridge", index)
        thread_path = home / ".codex" / "sessions" / "2026" / "10" / f"rollout-{index}.jsonl"
        _write_jsonl(thread_path, {"type": "session_meta", "payload": {"cwd": workspace_path}})
        state = {
            "schema_version": 2,
            "session_id": session_id,
            "run_id": _fixture_uuid("codex-run", index),
            "connection_id": _fixture_uuid("codex-connection", index),
            "lease_generation": _fixture_uuid("codex-lease", index),
            "cwd": workspace_path,
            "codex_bin": str(fake_paths["codex"]),
            "launch_mode": "detached_ui",
            "ws_url": "ws://127.0.0.1:9/synthetic",
            "thread_id": f"thread-{index}",
            "thread_path": str(thread_path),
            "pid": 0,
            "status": "stopped",
            "log_file": str(codex_dir / "logs" / f"{session_id}.log"),
            "updated_at": now,
            "terminal_state": "run_completed",
            "terminal_published": True,
        }
        file_families.setdefault("managed_local_codex_bridge", []).append(
            _write_json(codex_dir / f"{session_id}.json", state, int(shape["managed_local_codex_bridge"]["median_bytes"]))
        )
        if index < int(shape["contracts_codex"]["count"]):
            state_path = codex_sessions / f"{session_id}.json"
            _write_json(state_path, {"thread_id": f"thread-{index}", "thread_path": str(thread_path)})
            contract = {
                "schema_version": 2,
                "provider": "codex",
                "session_id": session_id,
                "workspace": {"canonical_cwd": workspace_path},
                "provider_binary": {"path": str(fake_paths["codex"])},
                "control": {"state_path": str(state_path)},
            }
            contracts = longhouse_home / "managed-local" / "contracts" / "codex"
            file_families.setdefault("contracts_codex", []).append(
                _write_json(contracts / f"{session_id}.json", contract, int(shape["contracts_codex"]["median_bytes"]))
            )

    cursor_dir = longhouse_home / "managed-local" / "cursor-helm"
    for index in range(int(shape["managed_local_cursor_helm"]["count"])):
        session_id = _fixture_uuid("cursor-helm", index)
        state = {
            "session_id": session_id,
            "provider_session_id": f"conversation-{index}",
            "cwd": workspace_path,
            "ready": False,
        }
        file_families.setdefault("managed_local_cursor_helm", []).append(
            _write_json(cursor_dir / f"{session_id}.json", state, int(shape["managed_local_cursor_helm"]["median_bytes"]))
        )

    opencode_dir = longhouse_home / "managed-local" / "opencode" / "bridge" / "sessions"
    for index in range(int(shape["managed_local_opencode"]["count"])):
        session_id = _fixture_uuid("opencode", index)
        contract = {
            "schema_version": 1,
            "provider": "opencode",
            "longhouse_session_id": session_id,
            "cwd": workspace_path,
            "provider_binary": str(fake_paths["opencode"]),
            "provider_session_id": "ses_synthetic",
        }
        file_families.setdefault("managed_local_opencode", []).append(
            _write_json(opencode_dir / f"{session_id}.json", contract, int(shape["managed_local_opencode"]["median_bytes"]))
        )

    claude_contracts = longhouse_home / "managed-local" / "contracts" / "claude"
    for index in range(int(shape["contracts_claude"]["count"])):
        session_id = _fixture_uuid("claude-contract", index)
        contract = {
            "schema_version": 1,
            "provider": "claude",
            "session_id": session_id,
            "provider_session_id": _fixture_uuid("claude-native", index),
            "permission_mode": "bypass",
            "workspace": {"canonical_cwd": workspace_path},
            "provider_binary": {"path": str(fake_paths["claude"])},
        }
        file_families.setdefault("contracts_claude", []).append(
            _write_json(claude_contracts / f"{session_id}.json", contract, int(shape["contracts_claude"]["median_bytes"]))
        )

    claim_dir = longhouse_home / "agent" / "turn-claims"
    for index in range(int(shape["turn_claims"]["count"])):
        run_id = _fixture_uuid("turn-claim", index)
        claim = {
            "schema_version": 7,
            "run_id": run_id,
            "session_id": _fixture_uuid("claim-session", index),
            "thread_id": _fixture_uuid("claim-thread", index),
            "turn_id": None,
            "client_request_id": None,
            "provider_thread_id": f"synthetic-thread-{index}",
            "provider_identity_confirmed": True,
            "source_path": None,
            "provider": "claude",
            "state": "terminal",
            "claimed_at": now,
            "updated_at": now,
            "pid": None,
            "process_group_id": None,
            "boot_id": None,
            "process_start_time": None,
            "adapter": "claude",
            "launch_id": None,
            "invocation_state": None,
            "pending_count": 0,
            "pending_items": [],
            "origin": "synthetic_soak",
            "adopted_parked_invocation": False,
            "stdout_path": None,
            "stderr_path": None,
            "cancel_requested_at": None,
            "projected_stdout_offset": 0,
            "projected_seq": 0,
            "owned_processes": [],
            "result": {"terminal_state": "run_completed", "summary": "x" * 520},
            "error": None,
            "terminal_event": None,
            "terminal_event_handed_off": True,
            "invocation_close_event": None,
            "invocation_close_event_handed_off": False,
        }
        file_families.setdefault("turn_claims", []).append(
            _write_json(claim_dir / f"{run_id}.json", claim, int(shape["turn_claims"]["median_bytes"]))
        )

    active_session_id = _fixture_uuid("active-shadow-session", 0)
    project_path = home / ".claude" / "projects" / (
        "-" + workspace_path.strip("/").replace("/", "-")
    )
    active_transcript = project_path / f"{active_session_id}.jsonl"
    _write_jsonl(
        active_transcript,
        {"cwd": workspace_path, "type": "user", "sessionId": active_session_id},
    )

    database_counts = _database_rows(db_path, home, shape)
    groups = {
        name: {
            "count": len(sizes),
            "median_bytes": sorted(sizes)[len(sizes) // 2] if sizes else None,
            "min_bytes": min(sizes) if sizes else None,
            "max_bytes": max(sizes) if sizes else None,
        }
        for name, sizes in file_families.items()
    }
    return {
        "shape": shape,
        "files": groups,
        "database_rows": database_counts,
        "active_shadow_transcript": str(active_transcript),
        "active_session_id": active_session_id,
        "workspace": workspace_path,
        "fake_provider_bin": {name: str(path) for name, path in fake_paths.items()},
        "omp_binary_sha256": omp_sha256,
        "database_path": str(db_path),
    }
