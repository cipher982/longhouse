from __future__ import annotations

import asyncio
import json
from datetime import datetime
from datetime import timezone
from uuid import uuid4

import typer

from zerg.config import get_settings


def _emit(payload: dict, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        for key, value in payload.items():
            typer.echo(f"{key}: {value}")


def apns_smoke_command(
    owner_id: int = typer.Option(1, "--owner-id", help="Owner whose latest APNs token should receive the smoke."),
    device_token: str | None = typer.Option(None, "--device-token", help="Explicit APNs device token override."),
    push_environment: str | None = typer.Option(
        None,
        "--push-environment",
        help="APNs environment for --device-token: sandbox or production.",
    ),
    title: str = typer.Option("Longhouse APNs smoke", "--title", help="Alert title."),
    body: str = typer.Option("Production push smoke from Longhouse.", "--body", help="Alert body."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Send a real APNs alert to verify hosted iOS push configuration."""

    settings = get_settings()
    config = {
        "testing": bool(settings.testing),
        "apns_enabled": bool(settings.apns_enabled),
        "apns_team_id_present": bool(settings.apns_team_id),
        "apns_key_id_present": bool(settings.apns_key_id),
        "apns_private_key_present": bool(settings.apns_private_key_p8),
        "apns_topic": settings.apns_topic,
    }
    if settings.testing or not settings.apns_enabled:
        _emit({"ok": False, "reason": "apns_not_configured", "config": config}, json_output=json_output)
        raise typer.Exit(code=2)

    token = str(device_token or "").strip().lower()
    environment = str(push_environment or "").strip().lower()
    token_source = "explicit" if token else "latest_registration"

    if token:
        if environment not in {"sandbox", "production"}:
            _emit(
                {"ok": False, "reason": "invalid_push_environment", "config": config},
                json_output=json_output,
            )
            raise typer.Exit(code=2)
    else:
        from zerg.database import configure_database
        from zerg.database import get_session_factory
        from zerg.models.apns_device_registration import APNSDeviceRegistration

        configure_database(settings)
        try:
            SessionLocal = get_session_factory()
        except RuntimeError:
            _emit({"ok": False, "reason": "database_unavailable", "config": config}, json_output=json_output)
            raise typer.Exit(code=2)
        with SessionLocal() as db:
            registration = (
                db.query(APNSDeviceRegistration)
                .filter(
                    APNSDeviceRegistration.owner_id == owner_id,
                    APNSDeviceRegistration.platform == "ios",
                    APNSDeviceRegistration.revoked_at.is_(None),
                )
                .order_by(APNSDeviceRegistration.last_seen_at.desc(), APNSDeviceRegistration.created_at.desc())
                .first()
            )
            if registration is None:
                _emit({"ok": False, "reason": "no_active_apns_registration", "config": config}, json_output=json_output)
                raise typer.Exit(code=2)
            token = str(registration.device_token or "").strip().lower()
            environment = "production" if registration.push_environment == "production" else "sandbox"

    from zerg.services.apns_sender import APNSDeviceTarget
    from zerg.services.apns_sender import SessionAttentionPush
    from zerg.services.apns_sender import send_session_attention_push

    now = datetime.now(timezone.utc)
    notification = SessionAttentionPush(
        session_id=str(uuid4()),
        state="blocked",
        occurred_at=now,
        title=title,
        summary=body,
        project="ops",
        provider="Longhouse",
        tool_name=None,
        alert_title=title,
        alert_body=body,
        collapse_id=f"lh-smoke-{int(now.timestamp())}",
        targets=(APNSDeviceTarget(device_token=token, push_environment=environment),),
        event_type="apns_smoke",
    )
    accepted = asyncio.run(send_session_attention_push(notification))
    payload = {
        "ok": bool(accepted),
        "accepted": bool(accepted),
        "token_source": token_source,
        "token_suffix": token[-12:],
        "push_environment": environment,
        "config": config,
    }
    _emit(payload, json_output=json_output)
    if not accepted:
        raise typer.Exit(code=1)
