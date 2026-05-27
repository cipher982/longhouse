"""Longhouse Antigravity hook-inbox control commands."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import typer

app = typer.Typer(no_args_is_help=True, help="Antigravity hook-inbox control commands")
_MESSAGE_TTL = timedelta(minutes=5)
_MAX_MESSAGE_BYTES = 64 * 1024


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _antigravity_runtime_dir(config_dir: Path | None = None) -> Path:
    return (config_dir or (Path.home() / ".claude")) / "managed-local" / "antigravity"


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def antigravity_state_dir(config_dir: Path | None = None) -> Path:
    return _antigravity_runtime_dir(config_dir) / "sessions"


def antigravity_inbox_dir(session_id: str, config_dir: Path | None = None) -> Path:
    return _antigravity_runtime_dir(config_dir) / "inbox" / session_id


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    _ensure_private_dir(path.parent)
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
    finally:
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _claimed_message(session_id: str, message_id: str, *, config_dir: Path | None = None) -> dict[str, Any] | None:
    claimed_dir = antigravity_inbox_dir(session_id, config_dir) / "claimed"
    for path in claimed_dir.glob("*.json"):
        payload = _read_json(path)
        if payload and payload.get("id") == message_id:
            return payload
    return None


def enqueue_antigravity_message(
    *,
    session_id: str,
    text: str,
    intent: str = "send",
    config_dir: Path | None = None,
) -> dict[str, Any]:
    normalized_session = str(session_id or "").strip()
    normalized_text = str(text or "")
    normalized_intent = str(intent or "send").strip() or "send"
    if not normalized_session:
        raise ValueError("session_id is required")
    if not normalized_text.strip():
        raise ValueError("text is required")
    if normalized_intent != "send":
        raise ValueError("Antigravity hook inbox only supports intent=send")
    if len(normalized_text.encode("utf-8")) > _MAX_MESSAGE_BYTES:
        raise ValueError(f"text exceeds {_MAX_MESSAGE_BYTES} bytes")
    message_id = uuid4().hex
    created_at = _now_iso()
    payload = {
        "id": message_id,
        "session_id": normalized_session,
        "text": normalized_text,
        "intent": normalized_intent,
        "created_at": created_at,
        "expires_at": (datetime.now(UTC) + _MESSAGE_TTL).isoformat().replace("+00:00", "Z"),
    }
    runtime_dir = _antigravity_runtime_dir(config_dir)
    _ensure_private_dir(runtime_dir)
    _ensure_private_dir(runtime_dir / "inbox")
    path = antigravity_inbox_dir(normalized_session, config_dir) / f"msg-{message_id}.json"
    _write_private_json(path, payload)
    return {"message_id": message_id, "path": str(path), "payload": payload}


def wait_for_antigravity_message_claim(
    *,
    session_id: str,
    message_id: str,
    timeout_secs: float,
    config_dir: Path | None = None,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(0.0, float(timeout_secs))
    while time.monotonic() <= deadline:
        claimed = _claimed_message(session_id, message_id, config_dir=config_dir)
        if claimed is not None:
            return claimed
        time.sleep(0.05)
    return _claimed_message(session_id, message_id, config_dir=config_dir)


@app.command(name="send")
def send_command(
    session_id: str = typer.Option(..., "--session-id", help="Longhouse session id."),
    text: str = typer.Option(..., "--text", help="Text to inject through the Antigravity hook inbox."),
    wait_claimed_secs: float = typer.Option(
        15.0,
        "--wait-claimed-secs",
        min=0.0,
        help="Wait for a live Antigravity hook to claim the message.",
    ),
    config_dir: str | None = typer.Option(
        None,
        "--config-dir",
        "--claude-dir",
        help="Longhouse config directory (default: ~/.claude).",
    ),
) -> None:
    """Queue text for an active Antigravity hook loop and wait for claim."""

    resolved_config_dir = Path(config_dir) if config_dir else None
    try:
        queued = enqueue_antigravity_message(
            session_id=session_id,
            text=text,
            config_dir=resolved_config_dir,
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    claimed = wait_for_antigravity_message_claim(
        session_id=session_id,
        message_id=str(queued["message_id"]),
        timeout_secs=wait_claimed_secs,
        config_dir=resolved_config_dir,
    )
    if claimed is None:
        try:
            Path(str(queued["path"])).unlink(missing_ok=True)
        except OSError:
            pass
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "provider": "antigravity",
                    "transport": "antigravity_hook_inbox",
                    "message_id": queued["message_id"],
                    "error": "Antigravity hook did not claim queued input before timeout",
                },
                sort_keys=True,
            ),
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo(
        json.dumps(
            {
                "ok": True,
                "provider": "antigravity",
                "transport": "antigravity_hook_inbox",
                "message_id": queued["message_id"],
                "claimed_at": claimed.get("claimed_at"),
            },
            sort_keys=True,
        )
    )
