"""Shared installer for the local Longhouse runtime.

This is the convergence seam for CLI onboarding and future app-first setup
flows. The install steps here should produce the same local runtime state
regardless of which entrypoint initiated the setup.
"""

from __future__ import annotations

from dataclasses import dataclass

from zerg.services.desktop_app import install_desktop_app_service
from zerg.services.longhouse_paths import resolve_longhouse_home_from_provider_home
from zerg.services.machine_state import MachineState
from zerg.services.machine_state import read_machine_state
from zerg.services.machine_state import write_machine_state
from zerg.services.runtime_artifacts import InstalledRuntimeBinary
from zerg.services.runtime_artifacts import RuntimeComponent
from zerg.services.runtime_artifacts import ensure_runtime_binary
from zerg.services.shipper import install_hooks
from zerg.services.shipper import install_service
from zerg.services.shipper import load_token
from zerg.services.shipper import sanitize_machine_name
from zerg.services.shipper import save_token


@dataclass(frozen=True)
class HookInstallResult:
    actions: list[str]
    warning: str | None = None


@dataclass(frozen=True)
class LocalRuntimeInstallResult:
    machine_name: str
    engine_runtime: InstalledRuntimeBinary
    service_result: dict[str, str]
    hooks: HookInstallResult
    desktop_app_result: dict[str, str] | None = None


@dataclass(frozen=True)
class LocalRuntimeReconcileResult:
    machine_state: MachineState
    install_result: LocalRuntimeInstallResult


def _install_local_runtime_artifacts(
    *,
    url: str,
    token: str | None,
    claude_dir: str | None,
    machine_name: str,
    menubar: bool,
) -> LocalRuntimeInstallResult:
    config_dir = resolve_longhouse_home_from_provider_home(claude_dir) if claude_dir else None
    resolved_name = sanitize_machine_name(machine_name)
    if resolved_name is None:
        raise ValueError(f"Invalid machine name: {machine_name!r}")

    if token:
        save_token(token, config_dir)

    engine_runtime = ensure_runtime_binary(RuntimeComponent.ENGINE)
    service_result = install_service(
        url=url,
        token=token,
        claude_dir=claude_dir,
        machine_name=resolved_name,
    )

    try:
        hook_actions = install_hooks(
            url=url,
            token=token,
            claude_dir=claude_dir,
            engine_path=engine_runtime.path,
        )
        hooks = HookInstallResult(actions=hook_actions)
    except Exception as exc:
        hooks = HookInstallResult(actions=[], warning=str(exc))

    desktop_app_result = None
    if menubar:
        desktop_app_result = install_desktop_app_service(
            ui_url=url,
            claude_dir=claude_dir,
        )

    return LocalRuntimeInstallResult(
        machine_name=resolved_name,
        engine_runtime=engine_runtime,
        service_result=service_result,
        hooks=hooks,
        desktop_app_result=desktop_app_result,
    )


def install_local_runtime(
    *,
    url: str,
    token: str | None,
    claude_dir: str | None,
    machine_name: str,
    menubar: bool,
    written_by: str = "connect-install",
    topology_intent: str | None = None,
) -> LocalRuntimeInstallResult:
    """Install the machine agent, CLI hooks, and optional desktop app."""

    config_dir = resolve_longhouse_home_from_provider_home(claude_dir) if claude_dir else None
    resolved_name = sanitize_machine_name(machine_name)
    if resolved_name is None:
        raise ValueError(f"Invalid machine name: {machine_name!r}")
    write_machine_state(
        base_dir=config_dir,
        written_by=written_by,
        runtime_url=url,
        machine_name=resolved_name,
        desktop_app_enabled=menubar,
        topology_intent=topology_intent,
    )
    return _install_local_runtime_artifacts(
        url=url,
        token=token,
        claude_dir=claude_dir,
        machine_name=resolved_name,
        menubar=menubar,
    )


def reconcile_local_runtime(
    *,
    claude_dir: str | None,
    token: str | None = None,
    written_by: str = "machine-reconcile",
    runtime_url: str | None = None,
    machine_name: str | None = None,
    menubar: bool | None = None,
    topology_intent: str | None = None,
) -> LocalRuntimeReconcileResult:
    """Regenerate runtime artifacts from canonical machine state.

    Explicit parameters override the current state before reconciliation.
    Missing durable facts are treated as configuration errors instead of
    inferring truth from runner.env, launchd plists, or other generated files.
    """

    config_dir = resolve_longhouse_home_from_provider_home(claude_dir) if claude_dir else None
    state_path, current_state, error = read_machine_state(config_dir)
    if error:
        raise RuntimeError(f"Failed to read existing machine state at {state_path}: {error}")

    resolved_url = runtime_url if runtime_url is not None else (current_state.runtime_url if current_state else None)
    resolved_name = machine_name if machine_name is not None else (current_state.machine_name if current_state else None)
    resolved_menubar = menubar if menubar is not None else (current_state.desktop_app_enabled if current_state else None)
    resolved_topology_intent = (
        topology_intent if topology_intent is not None else (current_state.topology_intent if current_state else None)
    )

    if not resolved_url:
        raise RuntimeError(
            f"Machine state missing runtime_url at {state_path}. " "Run `longhouse connect --install` once to configure this machine."
        )
    if not resolved_name:
        raise RuntimeError(
            f"Machine state missing machine_name at {state_path}. " "Run `longhouse connect --install` once to configure this machine."
        )

    write_kwargs: dict[str, object] = {
        "base_dir": config_dir,
        "written_by": written_by,
        "runtime_url": resolved_url,
        "machine_name": resolved_name,
    }
    if resolved_topology_intent is not None:
        write_kwargs["topology_intent"] = resolved_topology_intent
    if resolved_menubar is not None:
        write_kwargs["desktop_app_enabled"] = resolved_menubar

    machine_state = write_machine_state(**write_kwargs)
    effective_token = token if token is not None else load_token(config_dir)
    install_result = _install_local_runtime_artifacts(
        url=machine_state.runtime_url or resolved_url,
        token=effective_token,
        claude_dir=claude_dir,
        machine_name=machine_state.machine_name or resolved_name,
        menubar=bool(machine_state.desktop_app_enabled),
    )
    return LocalRuntimeReconcileResult(machine_state=machine_state, install_result=install_result)
