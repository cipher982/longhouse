"""Longhouse Antigravity hook-inbox control commands."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from zerg.services.antigravity_hook_inbox import antigravity_inbox_dir
from zerg.services.antigravity_hook_inbox import antigravity_state_dir
from zerg.services.antigravity_hook_inbox import enqueue_antigravity_message
from zerg.services.antigravity_hook_inbox import wait_for_antigravity_message_claim

__all__ = [
    "antigravity_inbox_dir",
    "antigravity_state_dir",
    "enqueue_antigravity_message",
    "wait_for_antigravity_message_claim",
]

app = typer.Typer(no_args_is_help=True, help="Antigravity hook-inbox control commands")


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
