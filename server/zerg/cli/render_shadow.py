"""Read the host render shadow: how often the host rebuild equals the laptop render."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer


def render_shadow_summary(
    socket_path: Path | None = typer.Option(None, "--socket", help="Catalog socket; defaults to this Runtime Host's."),
) -> None:
    """Print per-provider outcome counts, rebuild latency and recent differences as JSON."""
    from zerg.catalogd.client import CatalogClient
    from zerg.services.catalogd_supervisor import catalogd_paths

    catalog = CatalogClient(socket_path or catalogd_paths()[1], default_timeout_seconds=30)
    summary = asyncio.run(catalog.call("render.shadow.summary.v2", {}))
    typer.echo(json.dumps(summary, indent=2, sort_keys=True))
