"""Provider live-proof command surfaces."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Annotated

import typer

from zerg.qa.provider_live_canary import default_repo_root
from zerg.qa.provider_live_canary import run_provider_live_canary

app = typer.Typer(help="Run local managed-provider live proof canaries")


@app.command("canary")
def canary_command(
    provider: Annotated[
        str,
        typer.Option("--provider", help="Provider to prove: codex, claude, opencode, or antigravity."),
    ],
    provider_bin: Annotated[
        str | None,
        typer.Option("--provider-bin", help="Explicit provider binary path for debug/test runs."),
    ] = None,
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", help="Repo root override; source checkouts are auto-detected when omitted."),
    ] = None,
    evidence_root: Annotated[
        Path | None,
        typer.Option("--evidence-root", help="Directory for timestamped canary evidence."),
    ] = None,
    artifact: Annotated[
        Path | None,
        typer.Option("--artifact", help="Exact artifact JSON path to write."),
    ] = None,
    wait_ready_secs: Annotated[
        float,
        typer.Option("--wait-ready-secs", help="Seconds to wait for provider local servers to become ready."),
    ] = 15.0,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Run one local provider live canary and write its artifact."""

    args = argparse.Namespace(
        repo_root=repo_root or default_repo_root(),
        provider=provider,
        provider_bin=provider_bin,
        artifact=artifact,
        evidence_root=evidence_root,
        wait_ready_secs=wait_ready_secs,
        json=json_output,
    )
    result = run_provider_live_canary(args)
    if json_output:
        typer.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        typer.echo(f"{provider}: {result.get('verdict') or '-'}")
        if result.get("failure_code"):
            typer.echo(f"failure: {result.get('failure_code')}")
        typer.echo(f"artifact: {result.get('artifact_path')}")
        typer.echo(f"evidence: {result.get('evidence_root')}")
    if result.get("verdict") == "red":
        raise typer.Exit(code=1)
