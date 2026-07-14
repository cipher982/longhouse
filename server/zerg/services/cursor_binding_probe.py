"""Durable, read-only evidence recorder for the Cursor Helm binding gate."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from zerg.services.cursor_transcript import _decode_meta_json
from zerg.services.cursor_transcript import _open_readonly
from zerg.services.longhouse_paths import get_managed_local_dir

_PHASES = ("before_launch", "after_prompt", "after_tool_turn", "at_exit")


def _artifact_path(session_id: str) -> Path:
    return get_managed_local_dir("cursor-helm") / "binding-probes" / f"{session_id}.json"


def _state_path(session_id: str) -> Path:
    return get_managed_local_dir("cursor-helm") / f"{session_id}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _agent_id(store_db: Path) -> str:
    con = _open_readonly(store_db)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta WHERE key = '0'"))
    except sqlite3.Error as exc:
        raise ValueError(f"cannot read Cursor meta['0']: {exc}") from exc
    finally:
        con.close()
    agent_id = str(_decode_meta_json(meta).get("agentId") or "").strip()
    if not agent_id:
        raise ValueError("Cursor store meta['0'].agentId is missing")
    return agent_id


def record_probe_observation(session_id: str, phase: str, store_db: Path | None = None) -> dict:
    """Record one operator-confirmed interactive phase without modifying Cursor.

    Operators run this before launch, after a normal prompt, after a tool turn,
    and after exit.  The only accepted link is exact equality of the launch
    token inherited by cursor-agent and Cursor's native ``agentId``.
    """
    if phase not in _PHASES:
        raise ValueError(f"phase must be one of {', '.join(_PHASES)}")
    if phase != "before_launch" and store_db is None:
        raise ValueError("--store-db is required after launch")
    path = _artifact_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        artifact = json.loads(path.read_text())
    except FileNotFoundError:
        artifact = {
            "schema_version": 1,
            "provider": "cursor",
            "session_id": session_id,
            "launch_token": session_id,
            "observations": [],
        }
    if artifact.get("session_id") != session_id or artifact.get("provider") != "cursor":
        raise ValueError("probe artifact does not match this Cursor Helm session")
    state: dict = {}
    try:
        state = json.loads(_state_path(session_id).read_text())
    except (OSError, ValueError):
        pass
    observation = {
        "phase": phase,
        "observed_at": _now(),
        "agent_id": _agent_id(store_db) if store_db else None,
        "launcher_pid": state.get("launcher_pid"),
        "cursor_pid": state.get("cursor_pid"),
        "cwd": state.get("cwd"),
    }
    artifact["observations"] = [item for item in artifact["observations"] if item.get("phase") != phase] + [observation]
    agent_ids = {item.get("agent_id") for item in artifact["observations"] if item.get("agent_id")}
    required = {"after_prompt", "after_tool_turn", "at_exit"}
    have_required = required <= {item.get("phase") for item in artifact["observations"]}
    has_process_metadata = any(
        isinstance(item.get("launcher_pid"), int)
        and item["launcher_pid"] > 0
        and isinstance(item.get("cursor_pid"), int)
        and item["cursor_pid"] > 0
        for item in artifact["observations"]
    )
    # Cursor exposes no documented launch-to-conversation handle.  Equality is
    # deliberately the only accepted proof; all normal generated IDs fail.
    passed = have_required and has_process_metadata and agent_ids == {session_id}
    artifact["status"] = "passed" if passed else ("failed" if phase == "at_exit" else "pending")
    artifact["failure_reason"] = (
        None if artifact["status"] != "failed" else "Cursor did not expose a deterministic launch token equal to meta['0'].agentId"
    )
    if passed:
        artifact["conversation_uuid"] = session_id
        artifact["agent_id"] = session_id
        artifact["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    artifact["artifact_path"] = str(path)
    return artifact
