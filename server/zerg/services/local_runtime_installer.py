"""Shared installer for the local Longhouse runtime.

This is the convergence seam for CLI onboarding and future app-first setup
flows. The install steps here should produce the same local runtime state
regardless of which entrypoint initiated the setup.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from zerg.services.desktop_app import install_desktop_app_service
from zerg.services.runtime_artifacts import InstalledRuntimeBinary
from zerg.services.runtime_artifacts import RuntimeComponent
from zerg.services.runtime_artifacts import ensure_runtime_binary
from zerg.services.shipper import install_hooks
from zerg.services.shipper import install_service
from zerg.services.shipper import sanitize_machine_name
from zerg.services.shipper import save_machine_name
from zerg.services.shipper import save_token
from zerg.services.shipper import save_zerg_url


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


def install_local_runtime(
    *,
    url: str,
    token: str | None,
    claude_dir: str | None,
    machine_name: str,
    menubar: bool,
) -> LocalRuntimeInstallResult:
    """Install the machine agent, CLI hooks, and optional desktop app."""

    config_dir = Path(claude_dir) if claude_dir else None
    save_zerg_url(url, config_dir)
    if token:
        save_token(token, config_dir)

    resolved_name = sanitize_machine_name(machine_name)
    save_machine_name(resolved_name, config_dir)

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
