"""State helpers for the managed OpenCode bridge transport.

Longhouse owns the launch of `opencode serve`, captures the listening
URL + Basic-Auth password from stdout, and writes that into a per-session
state file so the bridge CLI (`longhouse opencode-bridge ...`) can drive
the upstream HTTP API later without re-discovery.

Mirrors the shape of ``zerg.services.claude_channel_bridge`` so both
provider transports follow the same on-disk contract.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from zerg.services.longhouse_paths import get_managed_local_dir

_OPENCODE_LISTEN_PREFIX = "opencode server listening on "


def generate_server_password() -> str:
    """Return a fresh URL-safe password for OPENCODE_SERVER_PASSWORD."""

    return secrets.token_urlsafe(24)


def parse_listen_line(line: str) -> str | None:
    """Extract the server URL from an `opencode serve` stdout line, or None."""

    text = (line or "").strip()
    if not text:
        return None
    idx = text.find(_OPENCODE_LISTEN_PREFIX)
    if idx < 0:
        return None
    candidate = text[idx + len(_OPENCODE_LISTEN_PREFIX) :].strip()
    if not candidate.startswith(("http://", "https://")):
        return None
    return candidate.rstrip("/")


def resolve_opencode_bridge_state_root(
    *,
    state_root: str | Path | None = None,
    config_dir: str | Path | None = None,
) -> Path:
    if state_root is not None:
        return Path(state_root).expanduser()
    base_dir = Path(config_dir).expanduser() if config_dir is not None else None
    return get_managed_local_dir("opencode", base_dir=base_dir) / "bridge"


def build_opencode_bridge_state_file(
    *,
    session_id: str,
    state_root: str | Path | None = None,
    config_dir: str | Path | None = None,
) -> Path:
    normalized = str(session_id or "").strip()
    if not normalized:
        raise ValueError("session_id must not be empty")
    return resolve_opencode_bridge_state_root(state_root=state_root, config_dir=config_dir) / "sessions" / f"{normalized}.json"


def write_opencode_bridge_state(
    *,
    session_id: str,
    server_url: str,
    server_password: str,
    server_username: str = "opencode",
    cwd: str,
    opencode_pid: int | None,
    opencode_session_id: str | None = None,
    state_root: str | Path | None = None,
    config_dir: str | Path | None = None,
    extra: dict[str, Any] | None = None,
    ready: bool = True,
) -> Path:
    """Write the bridge state file with 0600 permissions (contains a password)."""

    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id must not be empty")
    url = str(server_url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("server_url must be an http(s) URL")
    password = str(server_password or "").strip()
    if not password:
        raise ValueError("server_password must not be empty")

    payload: dict[str, Any] = {
        "session_id": sid,
        "server_url": url.rstrip("/"),
        "server_username": str(server_username or "opencode"),
        "server_password": password,
        "cwd": str(cwd or "").strip(),
        "opencode_pid": int(opencode_pid) if opencode_pid is not None else None,
        "opencode_session_id": str(opencode_session_id or "").strip() or None,
        "ready": bool(ready),
    }
    if extra:
        for key, value in extra.items():
            payload.setdefault(key, value)

    state_path = build_opencode_bridge_state_file(session_id=sid, state_root=state_root, config_dir=config_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2) + "\n"
    # Atomic publish: write to a per-pid temp file with 0600 perms, fsync,
    # then rename. Concurrent readers either see the previous file or the
    # complete new file — never a half-written truncation.
    tmp_path = state_path.with_name(f"{state_path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, state_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
    return state_path


def read_opencode_bridge_state(
    *,
    session_id: str,
    state_root: str | Path | None = None,
    config_dir: str | Path | None = None,
) -> dict[str, Any]:
    state_path = build_opencode_bridge_state_file(session_id=session_id, state_root=state_root, config_dir=config_dir)
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"OpenCode bridge state at {state_path} is not a JSON object")
    return raw


def wait_for_opencode_bridge_state(
    *,
    session_id: str,
    timeout_secs: float = 10.0,
    poll_interval_secs: float = 0.1,
    require_ready: bool = True,
    state_root: str | Path | None = None,
    config_dir: str | Path | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_secs
    state_path = build_opencode_bridge_state_file(session_id=session_id, state_root=state_root, config_dir=config_dir)
    last_state: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        if state_path.exists():
            try:
                last_state = read_opencode_bridge_state(
                    session_id=session_id,
                    state_root=state_root,
                    config_dir=config_dir,
                )
            except (json.JSONDecodeError, OSError, ValueError):
                time.sleep(poll_interval_secs)
                continue
            if not require_ready or bool(last_state.get("ready")):
                return last_state
        time.sleep(poll_interval_secs)
    if last_state is None:
        raise FileNotFoundError(f"OpenCode bridge state did not appear at {state_path} within {timeout_secs:.1f}s")
    raise TimeoutError(f"OpenCode bridge state at {state_path} did not become ready within {timeout_secs:.1f}s")


def remove_opencode_bridge_state(
    *,
    session_id: str,
    state_root: str | Path | None = None,
    config_dir: str | Path | None = None,
) -> None:
    state_path = build_opencode_bridge_state_file(session_id=session_id, state_root=state_root, config_dir=config_dir)
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass


__all__ = [
    "build_opencode_bridge_state_file",
    "generate_server_password",
    "parse_listen_line",
    "read_opencode_bridge_state",
    "remove_opencode_bridge_state",
    "resolve_opencode_bridge_state_root",
    "wait_for_opencode_bridge_state",
    "write_opencode_bridge_state",
]
