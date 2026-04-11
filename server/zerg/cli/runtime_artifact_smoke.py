from __future__ import annotations

import json
from enum import Enum

import typer

from zerg.services.runtime_artifacts import RuntimeComponent
from zerg.services.runtime_artifacts import ensure_runtime_artifact


class RuntimeArtifactName(str, Enum):
    ENGINE = RuntimeComponent.ENGINE.value
    LOCAL_HEALTH_APP = RuntimeComponent.LOCAL_HEALTH_APP.value
    LOCAL_HEALTH_WINDOW = RuntimeComponent.LOCAL_HEALTH_WINDOW.value


def runtime_artifact_smoke_command(
    component: RuntimeArtifactName = typer.Argument(..., help="Runtime artifact to install or verify."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Force a reinstall even if the artifact already exists."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Ensure a local runtime artifact is installable.

    Hidden helper for CI and operator validation of released runtime artifacts.
    """

    try:
        artifact = ensure_runtime_artifact(RuntimeComponent(component.value), overwrite=overwrite)
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    payload = {
        "component": artifact.component.value,
        "path": artifact.path,
        "launch_path": artifact.launch_path,
        "source": artifact.source,
        "installed_now": artifact.installed_now,
        "kind": artifact.kind.value,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return

    typer.echo(f"{artifact.component.value}: {artifact.path}")
    typer.echo(f"  launch: {artifact.launch_path}")
    typer.echo(f"  source: {artifact.source}")
    typer.echo(f"  installed_now: {'yes' if artifact.installed_now else 'no'}")
