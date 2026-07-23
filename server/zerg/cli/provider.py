"""Provider automation-factory diagnostics."""

from __future__ import annotations

import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import typer

from zerg.services.longhouse_paths import resolve_longhouse_home

app = typer.Typer(help="Provider contracts and automation-factory diagnostics")
factory_app = typer.Typer(help="Read-only provider automation-factory health")
app.add_typer(factory_app, name="factory")


@factory_app.command(name="status")
def factory_status(
    evidence_root: Path | None = typer.Option(None, "--evidence-root"),
    provider: str | None = typer.Option(None, "--provider"),
    max_age_hours: int = typer.Option(48, "--max-age-hours", min=1),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Read the last completed transcript/control verdict package."""

    root = evidence_root or (resolve_longhouse_home() / "provider-factory")
    latest_path = root.expanduser().resolve() / "latest.json"
    if not latest_path.is_file():
        payload = {
            "factory_health": {"state": "unknown", "reason": "discovery_missing"},
            "provider": provider,
            "evidence_root": str(root),
        }
    else:
        payload = json.loads(latest_path.read_text())
        evaluated_at = payload.get("evaluated_at")
        try:
            evaluated = datetime.fromisoformat(str(evaluated_at).replace("Z", "+00:00"))
        except ValueError:
            evaluated = None
        stale = evaluated is None or datetime.now(timezone.utc) - evaluated > timedelta(hours=max_age_hours)
        provider_unobserved = bool(provider and provider not in payload.get("providers", []))
        recorded_health = payload.get("factory_health") if isinstance(payload.get("factory_health"), dict) else {}
        discovery_incomplete = recorded_health.get("state") != "current" or recorded_health.get("complete_window") is False
        reason = (
            "discovery_stale"
            if stale
            else (
                "provider_unobserved"
                if provider_unobserved
                else (str(recorded_health.get("reason") or "discovery_not_proven") if discovery_incomplete else None)
            )
        )
        payload["factory_health"] = {"state": "unknown" if reason else "current", "reason": reason}
        payload["provider"] = provider
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        health = payload["factory_health"]
        typer.echo(f"Factory: {health['state']}" + (f" ({health['reason']})" if health["reason"] else ""))
        if payload.get("verdicts"):
            typer.echo(
                "Control: "
                + str(payload["verdicts"]["control"]["verdict"])
                + " · Transcript: "
                + str(payload["verdicts"]["transcript"]["verdict"])
            )
