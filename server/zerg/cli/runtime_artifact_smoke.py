from __future__ import annotations

import json
from enum import Enum

import typer

from zerg.services.runtime_artifacts import RuntimeComponent
from zerg.services.runtime_artifacts import ensure_runtime_artifact


class RuntimeArtifactName(str, Enum):
    ENGINE = RuntimeComponent.ENGINE.value
    MANAGED_CODEX = RuntimeComponent.MANAGED_CODEX.value
    DESKTOP_APP = RuntimeComponent.DESKTOP_APP.value
    DESKTOP_WINDOW = RuntimeComponent.DESKTOP_WINDOW.value
    LOCAL_HEALTH_APP = "local-health-app"
    LOCAL_HEALTH_WINDOW = "local-health-window"


def _ensure_runtime_artifact_payload(component: RuntimeArtifactName, *, overwrite: bool) -> dict[str, object]:
    try:
        artifact = ensure_runtime_artifact(RuntimeComponent(component.value), overwrite=overwrite)
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    return {
        "component": artifact.component.value,
        "path": artifact.path,
        "launch_path": artifact.launch_path,
        "source": artifact.source,
        "installed_now": artifact.installed_now,
        "kind": artifact.kind.value,
    }


def _emit_runtime_artifact_payload(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return

    typer.echo(f"{payload['component']}: {payload['path']}")
    typer.echo(f"  launch: {payload['launch_path']}")
    typer.echo(f"  source: {payload['source']}")
    typer.echo(f"  installed_now: {'yes' if payload['installed_now'] else 'no'}")


def runtime_artifact_smoke_command(
    component: RuntimeArtifactName = typer.Argument(..., help="Runtime artifact to install or verify."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Force a reinstall even if the artifact already exists."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Ensure a local runtime artifact is installable.

    Hidden helper for CI and operator validation of released runtime artifacts.
    """

    payload = _ensure_runtime_artifact_payload(component, overwrite=overwrite)
    _emit_runtime_artifact_payload(payload, json_output=json_output)


def runtime_artifact_install_command(
    component: RuntimeArtifactName = typer.Argument(..., help="Runtime artifact to install or verify."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Force a reinstall even if the artifact already exists."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Install or verify a local runtime artifact.

    Hidden helper for installer/bootstrap flows that need a concrete local artifact path
    without wiring release-download logic into shell scripts.
    """

    payload = _ensure_runtime_artifact_payload(component, overwrite=overwrite)
    _emit_runtime_artifact_payload(payload, json_output=json_output)
