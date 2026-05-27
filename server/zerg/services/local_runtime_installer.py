"""Shared installer for the local Longhouse runtime.

This is the convergence seam for CLI onboarding and future app-first setup
flows. The install steps here should produce the same local runtime state
regardless of which entrypoint initiated the setup.
"""

from __future__ import annotations

import plistlib
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from zerg.provider_cli_contract import LEGACY_MANAGED_CODEX_LAUNCHER_MARKER
from zerg.provider_release_status import persist_provider_release_status_config_from_env
from zerg.services.desktop_app import install_desktop_app_service
from zerg.services.local_health import collect_launch_readiness
from zerg.services.longhouse_paths import classify_longhouse_home
from zerg.services.longhouse_paths import is_stable_longhouse_home
from zerg.services.longhouse_paths import resolve_longhouse_home_from_provider_home
from zerg.services.machine_state import MachineState
from zerg.services.machine_state import machine_state_source_hash
from zerg.services.machine_state import read_machine_state
from zerg.services.machine_state import write_machine_state
from zerg.services.runtime_artifacts import InstalledRuntimeBinary
from zerg.services.runtime_artifacts import RuntimeComponent
from zerg.services.runtime_artifacts import ensure_runtime_binary
from zerg.services.shipper import get_service_info
from zerg.services.shipper import install_hooks
from zerg.services.shipper import install_service
from zerg.services.shipper import load_token
from zerg.services.shipper import sanitize_machine_name
from zerg.services.shipper import save_token

_OBSOLETE_CLAUDE_MANAGED_LOCAL_PROVIDERS = ("codex-bridge", "opencode", "antigravity")


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


@dataclass(frozen=True)
class MachineStateApplyResult:
    machine_state: MachineState
    reconciled: bool = False


def _is_stable_home(state_root: Path | None) -> bool:
    return is_stable_longhouse_home(state_root)


def _is_local_control_plane_url(url: str | None) -> bool:
    raw = str(url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    host = str(parsed.hostname or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _guard_stable_home_control_plane_target(
    *,
    state_root: Path | None,
    runtime_url: str | None,
    machine_name: str | None,
) -> None:
    if not _is_stable_home(state_root):
        return
    if not _is_local_control_plane_url(runtime_url):
        return

    readiness = collect_launch_readiness(
        state_root,
        runtime_url_override=runtime_url,
        machine_name_override=machine_name,
    )
    reasons = {str(item) for item in list(readiness.get("reasons") or [])}
    if not reasons.intersection({"config_url_runner_url_mismatch", "machine_name_runner_name_mismatch"}):
        return

    runner = dict(readiness.get("runner") or {})
    runner_name = str(runner.get("runner_name") or "").strip() or "unknown"
    runner_url_items = [str(item) for item in list(runner.get("runner_urls") or []) if str(item).strip()]
    runner_urls = ", ".join(runner_url_items) or "unknown"
    raise RuntimeError(
        "Refusing to point the stable Longhouse home at a local control plane while the machine runner is "
        f"enrolled as `{runner_name}` against `{runner_urls}`. "
        "Use LONGHOUSE_HOME=~/.longhouse-dev for scratch local work, or reconfigure the stable machine with "
        "`longhouse machine configure --url <control-plane-url> --machine-name <runner-name>`."
    )


def _extract_service_longhouse_home(service_file: str | None) -> Path | None:
    raw = str(service_file or "").strip()
    if not raw:
        return None

    path = Path(raw).expanduser()
    if not path.exists():
        return None

    try:
        if path.suffix == ".plist":
            payload = plistlib.loads(path.read_bytes())
            env = payload.get("EnvironmentVariables") if isinstance(payload, dict) else None
            if isinstance(env, dict):
                home = str(env.get("LONGHOUSE_HOME") or "").strip()
                return Path(home).expanduser() if home else None
            return None

        if path.suffix == ".service":
            for raw_line in path.read_text().splitlines():
                line = raw_line.strip()
                if not line.startswith("Environment="):
                    continue
                value = line.split("=", 1)[1].strip()
                for token in shlex.split(value):
                    if "=" not in token:
                        continue
                    key, env_value = token.split("=", 1)
                    if key == "LONGHOUSE_HOME":
                        return Path(env_value).expanduser()
    except Exception:
        return None

    return None


def _service_targets_state_root(service_info: dict[str, object], state_root: Path | None) -> bool:
    status = str(service_info.get("status") or "not-installed")
    if status == "not-installed":
        return False
    if state_root is None:
        return True

    service_home = _extract_service_longhouse_home(str(service_info.get("service_file") or ""))
    if service_home is None:
        return True

    return service_home.resolve(strict=False) == state_root.expanduser().resolve(strict=False)


def _is_obsolete_managed_codex_launcher(path: Path) -> bool:
    try:
        return path.is_file() and LEGACY_MANAGED_CODEX_LAUNCHER_MARKER in path.read_text(errors="ignore")
    except OSError:
        return False


def _remove_local_artifact(path: Path) -> None:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _cleanup_obsolete_managed_codex_runtime(config_dir: Path) -> None:
    launcher_path = Path.home() / ".local" / "bin" / "longhouse-codex"
    if _is_obsolete_managed_codex_launcher(launcher_path):
        _remove_local_artifact(launcher_path)

    _remove_local_artifact(config_dir / "runtimes" / "codex")


def _obsolete_claude_managed_local_root(
    *,
    config_dir: Path,
    claude_dir: str | None,
) -> Path | None:
    if claude_dir:
        return Path(claude_dir).expanduser() / "managed-local"
    if _is_stable_home(config_dir):
        return Path.home() / ".claude" / "managed-local"
    return None


def _cleanup_obsolete_claude_managed_local_state(
    *,
    config_dir: Path,
    claude_dir: str | None,
) -> None:
    root = _obsolete_claude_managed_local_root(config_dir=config_dir, claude_dir=claude_dir)
    if root is None:
        return
    for provider in _OBSOLETE_CLAUDE_MANAGED_LOCAL_PROVIDERS:
        _remove_local_artifact(root / provider)


def _reconcile_launch_artifacts(
    *,
    url: str,
    token: str | None,
    claude_dir: str | None,
    machine_name: str,
    menubar: bool,
    machine_config_generation: str | None,
    machine_state_hash: str | None,
    engine_path: str | None = None,
) -> tuple[dict[str, str], HookInstallResult, dict[str, str] | None]:
    persist_provider_release_status_config_from_env()
    service_result = install_service(
        url=url,
        token=token,
        claude_dir=claude_dir,
        machine_name=machine_name,
        machine_config_generation=machine_config_generation,
        machine_state_hash=machine_state_hash,
    )

    try:
        hook_kwargs: dict[str, str | None] = {
            "url": url,
            "token": token,
            "claude_dir": claude_dir,
        }
        if engine_path is not None:
            hook_kwargs["engine_path"] = engine_path
        hook_actions = install_hooks(**hook_kwargs)
        hooks = HookInstallResult(actions=hook_actions)
    except Exception as exc:
        hooks = HookInstallResult(actions=[], warning=str(exc))

    desktop_app_result = None
    if menubar:
        try:
            desktop_app_result = install_desktop_app_service(
                ui_url=url,
                claude_dir=claude_dir,
            )
        except Exception as exc:
            # Dev/unreleased builds may 404 on the release asset — degrade
            # gracefully rather than failing the entire install.
            desktop_app_result = {"warning": str(exc)}

    return service_result, hooks, desktop_app_result


def _install_local_runtime_artifacts(
    *,
    url: str,
    token: str | None,
    claude_dir: str | None,
    machine_name: str,
    menubar: bool,
    machine_config_generation: str | None = None,
    machine_state_hash: str | None = None,
) -> LocalRuntimeInstallResult:
    config_dir = resolve_longhouse_home_from_provider_home(claude_dir)
    resolved_name = sanitize_machine_name(machine_name)
    if resolved_name is None:
        raise ValueError(f"Invalid machine name: {machine_name!r}")

    if token:
        save_token(token, config_dir)

    engine_runtime = ensure_runtime_binary(RuntimeComponent.ENGINE)
    home_mode = classify_longhouse_home(config_dir)
    _cleanup_obsolete_claude_managed_local_state(config_dir=config_dir, claude_dir=claude_dir)
    if home_mode == "stable":
        _cleanup_obsolete_managed_codex_runtime(config_dir)
    if home_mode == "scratch":
        desktop_app_result = None
        if menubar:
            desktop_app_result = {
                "message": "Scratch Longhouse home active; skipped desktop app install.",
                "skipped": True,
            }
        return LocalRuntimeInstallResult(
            machine_name=resolved_name,
            engine_runtime=engine_runtime,
            service_result={
                "success": True,
                "mode": "scratch",
                "service": "skipped",
                "message": "Scratch Longhouse home active; skipped global service install.",
            },
            hooks=HookInstallResult(
                actions=["Scratch Longhouse home active; skipped Claude/Codex hook install."],
            ),
            desktop_app_result=desktop_app_result,
        )

    service_result, hooks, desktop_app_result = _reconcile_launch_artifacts(
        url=url,
        token=token,
        claude_dir=claude_dir,
        machine_name=resolved_name,
        menubar=menubar,
        machine_config_generation=machine_config_generation,
        machine_state_hash=machine_state_hash,
        engine_path=engine_runtime.path,
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

    config_dir = resolve_longhouse_home_from_provider_home(claude_dir)
    resolved_name = sanitize_machine_name(machine_name)
    if resolved_name is None:
        raise ValueError(f"Invalid machine name: {machine_name!r}")
    _guard_stable_home_control_plane_target(
        state_root=config_dir,
        runtime_url=url,
        machine_name=resolved_name,
    )
    machine_state = write_machine_state(
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
        machine_config_generation=machine_state.config_generation,
        machine_state_hash=machine_state_source_hash(machine_state),
    )


def apply_machine_state_update(
    *,
    claude_dir: str | None,
    base_dir: Path | None = None,
    written_by: str,
    runtime_url: str | None = None,
    machine_name: str | None = None,
    menubar: bool | None = None,
    topology_intent: str | None = None,
    token: str | None = None,
) -> MachineStateApplyResult:
    """Persist durable machine state and reconcile generated launch artifacts when installed.

    This is the safe seam for local config changes like switching runtime URL or
    machine label after a machine agent has already been installed. It does not
    install runtime binaries; use ``reconcile_local_runtime`` for full repair or
    first-install behavior.
    """

    config_dir = base_dir if base_dir is not None else resolve_longhouse_home_from_provider_home(claude_dir)
    service_info = get_service_info(claude_dir)

    write_kwargs: dict[str, object] = {
        "base_dir": config_dir,
        "written_by": written_by,
    }
    if runtime_url is not None:
        write_kwargs["runtime_url"] = runtime_url
    if machine_name is not None:
        write_kwargs["machine_name"] = machine_name
    if menubar is not None:
        write_kwargs["desktop_app_enabled"] = menubar
    if topology_intent is not None:
        write_kwargs["topology_intent"] = topology_intent

    _guard_stable_home_control_plane_target(
        state_root=config_dir,
        runtime_url=runtime_url,
        machine_name=machine_name,
    )

    machine_state = write_machine_state(**write_kwargs)
    if not _is_stable_home(config_dir):
        return MachineStateApplyResult(machine_state=machine_state)

    service_installed = _service_targets_state_root(service_info, config_dir)
    if not service_installed or not machine_state.runtime_url or not machine_state.machine_name:
        return MachineStateApplyResult(machine_state=machine_state)

    effective_token = token if token is not None else load_token(config_dir)
    if token:
        save_token(token, config_dir)

    _reconcile_launch_artifacts(
        url=machine_state.runtime_url,
        token=effective_token,
        claude_dir=claude_dir,
        machine_name=machine_state.machine_name,
        menubar=bool(machine_state.desktop_app_enabled),
        machine_config_generation=machine_state.config_generation,
        machine_state_hash=machine_state_source_hash(machine_state),
    )

    return MachineStateApplyResult(
        machine_state=machine_state,
        reconciled=True,
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

    config_dir = resolve_longhouse_home_from_provider_home(claude_dir)
    state_path, current_state, error = read_machine_state(config_dir)
    if error:
        raise RuntimeError(f"Failed to read existing machine state at {state_path}: {error}")

    resolved_url = runtime_url if runtime_url is not None else (current_state.runtime_url if current_state else None)
    if machine_name is not None:
        resolved_name = machine_name
    else:
        resolved_name = current_state.machine_name if current_state else None
    if menubar is not None:
        resolved_menubar = menubar
    else:
        resolved_menubar = current_state.desktop_app_enabled if current_state else None
    resolved_topology_intent = (
        topology_intent if topology_intent is not None else (current_state.topology_intent if current_state else None)
    )

    if not resolved_url:
        repair_hint = "Run `longhouse connect --install` once to configure this machine."
        message = f"Machine state missing runtime_url at {state_path}. {repair_hint}"
        raise RuntimeError(message)
    if not resolved_name:
        repair_hint = "Run `longhouse connect --install` once to configure this machine."
        message = f"Machine state missing machine_name at {state_path}. {repair_hint}"
        raise RuntimeError(message)

    _guard_stable_home_control_plane_target(
        state_root=config_dir,
        runtime_url=resolved_url,
        machine_name=resolved_name,
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
        machine_config_generation=machine_state.config_generation,
        machine_state_hash=machine_state_source_hash(machine_state),
    )
    return LocalRuntimeReconcileResult(machine_state=machine_state, install_result=install_result)
