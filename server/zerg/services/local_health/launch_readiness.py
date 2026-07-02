from __future__ import annotations

import os
import plistlib
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zerg.services.longhouse_paths import get_agent_db_path
from zerg.services.machine_repair import recommended_machine_repair_command
from zerg.services.machine_state import machine_state_source_hash
from zerg.services.machine_state import read_machine_state

from ._shared import _canonical_stable_home
from ._shared import _coerce_path
from ._shared import _with_action


def _state_root_tracks_machine_runner(base_dir: Path) -> bool:
    return base_dir.expanduser().resolve(strict=False) == _canonical_stable_home()


def _collect_local_config(base_dir: Path) -> dict[str, Any]:
    state_path, machine_state, state_error = read_machine_state(base_dir)
    return {
        "state_path": str(state_path),
        "state_exists": state_path.exists(),
        "state_error": state_error,
        "config_generation": machine_state.config_generation if machine_state else None,
        "stored_url": machine_state.runtime_url if machine_state else None,
        "machine_name": machine_state.machine_name if machine_state else None,
        "state_hash": machine_state_source_hash(machine_state),
    }


def _candidate_runner_env_paths() -> list[Path]:
    paths = [Path.home() / ".config" / "longhouse" / "runner.env"]
    if os.name != "nt":
        paths.append(Path("/etc/longhouse/runner.env"))
    return paths


def _parse_env_file(path: Path) -> dict[str, str]:
    payload: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        normalized_value = value.strip().strip("\"'")
        if normalized_key:
            payload[normalized_key] = normalized_value
    return payload


def _runner_config_payload(
    path: Path,
    *,
    exists: bool,
    error: str | None = None,
    runner_name: str | None = None,
    runner_id: str | None = None,
    runner_urls: list[str] | None = None,
    install_mode: str | None = None,
) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": exists,
        "error": error,
        "runner_name": runner_name,
        "runner_id": runner_id,
        "runner_urls": runner_urls or [],
        "install_mode": install_mode,
    }


def _missing_runner_config() -> dict[str, Any]:
    from zerg.services import local_health as _local_health_pkg

    return _runner_config_payload(_local_health_pkg._candidate_runner_env_paths()[0], exists=False)


def _runner_urls_from_env(env: dict[str, str]) -> list[str]:
    raw_urls = str(env.get("LONGHOUSE_URLS") or "").strip()
    if raw_urls:
        return [item.strip() for item in raw_urls.split(",") if item.strip()]

    raw_url = str(env.get("LONGHOUSE_URL") or "").strip()
    return [raw_url] if raw_url else []


def _runner_config_from_env(path: Path, env: dict[str, str]) -> dict[str, Any]:
    return _runner_config_payload(
        path,
        exists=True,
        runner_name=str(env.get("RUNNER_NAME") or "").strip() or None,
        runner_id=str(env.get("RUNNER_ID") or "").strip() or None,
        runner_urls=_runner_urls_from_env(env),
        install_mode=str(env.get("RUNNER_INSTALL_MODE") or "").strip() or None,
    )


def _collect_runner_config(*, include_global_runner: bool = True) -> dict[str, Any]:
    if not include_global_runner:
        return _missing_runner_config()

    from zerg.services import local_health as _local_health_pkg

    for path in _local_health_pkg._candidate_runner_env_paths():
        if not path.exists():
            continue
        try:
            env = _parse_env_file(path)
        except OSError as exc:
            return _runner_config_payload(path, exists=True, error=str(exc))

        return _runner_config_from_env(path, env)

    return _missing_runner_config()


def _extract_machine_name_from_args(arguments: list[str]) -> str | None:
    for index, arg in enumerate(arguments[:-1]):
        if arg == "--machine-name":
            candidate = str(arguments[index + 1] or "").strip()
            return candidate or None
    return None


def _service_file_path(service_file: str | None) -> Path | None:
    raw = str(service_file or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.exists():
        return None
    return path


def _read_service_plist(path: Path) -> dict[str, Any]:
    payload = plistlib.loads(path.read_bytes())
    return payload if isinstance(payload, dict) else {}


def _systemd_exec_start_arguments(path: Path) -> list[str] | None:
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line.startswith("ExecStart="):
            continue
        return shlex.split(line.split("=", 1)[1].strip())
    return None


def _systemd_environment(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line.startswith("Environment="):
            continue
        value = line.split("=", 1)[1].strip()
        for token in shlex.split(value):
            if "=" not in token:
                continue
            key, env_value = token.split("=", 1)
            env[key] = env_value
    return env


def _service_metadata_from_env(env: dict[str, Any] | None) -> dict[str, str | None]:
    env = env or {}
    return {
        "config_generation": str(env.get("LONGHOUSE_MACHINE_GENERATION") or "").strip() or None,
        "state_hash": str(env.get("LONGHOUSE_MACHINE_STATE_HASH") or "").strip() or None,
    }


def _empty_service_metadata() -> dict[str, str | None]:
    return {
        "config_generation": None,
        "state_hash": None,
    }


def _extract_service_machine_name(service_file: str | None) -> str | None:
    path = _service_file_path(service_file)
    if path is None:
        return None

    try:
        if path.suffix == ".plist":
            payload = _read_service_plist(path)
            arguments = [str(item) for item in payload.get("ProgramArguments") or []]
            return _extract_machine_name_from_args(arguments)

        if path.suffix == ".service":
            arguments = _systemd_exec_start_arguments(path)
            if arguments is not None:
                return _extract_machine_name_from_args(arguments)
    except Exception:
        return None

    return None


def _extract_service_metadata(service_file: str | None) -> dict[str, str | None]:
    metadata = _empty_service_metadata()
    path = _service_file_path(service_file)
    if path is None:
        return metadata

    try:
        if path.suffix == ".plist":
            payload = _read_service_plist(path)
            env = payload.get("EnvironmentVariables") if isinstance(payload, dict) else None
            if isinstance(env, dict):
                metadata = _service_metadata_from_env(env)
            return metadata

        if path.suffix == ".service":
            metadata = _service_metadata_from_env(_systemd_environment(path))
    except Exception:
        return metadata

    return metadata


def _can_reconcile_launch_from_state(
    *,
    state_exists: bool,
    state_error: str | None,
    stored_url: str | None,
    machine_name: str | None,
) -> bool:
    return state_exists and not state_error and bool(stored_url) and bool(machine_name)


def _repair_command(*, can_reconcile_from_state: bool) -> str:
    return recommended_machine_repair_command(can_reconcile_from_state=can_reconcile_from_state)


@dataclass
class _LaunchReadinessContext:
    runner: dict[str, Any]
    shipper_db_path: Path
    stored_url: str | None
    machine_name: str | None
    config_generation: str | None
    state_hash: str | None
    state_exists: bool
    state_error: str | None
    runner_expected: bool
    runner_name: str | None
    runner_urls: list[str]
    service_machine_name: str | None
    service_config_generation: str | None
    service_state_hash: str | None
    service_status: str
    service_file_exists: bool
    shipper_state_exists: bool
    can_reconcile_from_state: bool


@dataclass
class _LaunchOverrideContext:
    effective_url: str | None
    effective_machine_name: str | None
    runner_expected: bool
    runner_name: str | None
    runner_urls: list[str]
    reasons: list[str]
    actions: list[str]
    warnings: list[str]
    had_override: bool


def _collect_launch_readiness_context(base_dir: Path, *, service: dict[str, Any]) -> _LaunchReadinessContext:
    config = _collect_local_config(base_dir)
    runner = _collect_runner_config(include_global_runner=_state_root_tracks_machine_runner(base_dir))
    shipper_db_path = get_agent_db_path(base_dir)
    service_file_raw = str(service.get("service_file") or "").strip()
    service_file = Path(service_file_raw) if service_file_raw else None
    service_machine_name = _extract_service_machine_name(service.get("service_file"))
    service_metadata = _extract_service_metadata(service.get("service_file"))

    stored_url = str(config.get("stored_url") or "").strip() or None
    machine_name = str(config.get("machine_name") or "").strip() or None
    config_generation = str(config.get("config_generation") or "").strip() or None
    state_hash = str(config.get("state_hash") or "").strip() or None
    state_exists = bool(config.get("state_exists"))
    state_error = str(config.get("state_error") or "").strip() or None
    runner_expected = bool(runner.get("exists"))
    runner_name = str(runner.get("runner_name") or "").strip() or None
    runner_urls = [str(item).strip() for item in list(runner.get("runner_urls") or []) if str(item).strip()]
    service_config_generation = str(service_metadata.get("config_generation") or "").strip() or None
    service_state_hash = str(service_metadata.get("state_hash") or "").strip() or None
    service_status = str(service.get("status") or "not-installed")
    service_file_exists = bool(service_file and service_file.exists())
    shipper_state_exists = shipper_db_path.exists()
    can_reconcile_from_state = _can_reconcile_launch_from_state(
        state_exists=state_exists,
        state_error=state_error,
        stored_url=stored_url,
        machine_name=machine_name,
    )

    return _LaunchReadinessContext(
        runner=runner,
        shipper_db_path=shipper_db_path,
        stored_url=stored_url,
        machine_name=machine_name,
        config_generation=config_generation,
        state_hash=state_hash,
        state_exists=state_exists,
        state_error=state_error,
        runner_expected=runner_expected,
        runner_name=runner_name,
        runner_urls=runner_urls,
        service_machine_name=service_machine_name,
        service_config_generation=service_config_generation,
        service_state_hash=service_state_hash,
        service_status=service_status,
        service_file_exists=service_file_exists,
        shipper_state_exists=shipper_state_exists,
        can_reconcile_from_state=can_reconcile_from_state,
    )


def _add_launch_machine_state_reasons(ctx: _LaunchReadinessContext, reasons: list[str], actions: list[str]) -> None:
    if ctx.state_error:
        reasons.append("machine_state_invalid")
        _with_action(actions, _repair_command(can_reconcile_from_state=False))
    elif not ctx.state_exists and (ctx.service_machine_name or ctx.runner.get("exists")):
        reasons.append("machine_state_missing")
        _with_action(actions, _repair_command(can_reconcile_from_state=False))

    if ctx.state_exists and not ctx.stored_url:
        reasons.append("machine_state_missing_runtime_url")
        _with_action(actions, _repair_command(can_reconcile_from_state=False))

    if ctx.state_exists and not ctx.machine_name:
        reasons.append("machine_state_missing_machine_name")
        _with_action(actions, _repair_command(can_reconcile_from_state=False))


def _add_launch_runner_config_reasons(ctx: _LaunchReadinessContext, reasons: list[str], actions: list[str]) -> None:
    if (
        ctx.runner_expected
        and ctx.can_reconcile_from_state
        and ctx.stored_url
        and ctx.runner_urls
        and ctx.stored_url not in ctx.runner_urls
    ):
        reasons.append("config_url_runner_url_mismatch")
        _with_action(actions, _repair_command(can_reconcile_from_state=True))

    if (
        ctx.runner_expected
        and ctx.can_reconcile_from_state
        and ctx.machine_name
        and ctx.runner_name
        and ctx.machine_name != ctx.runner_name
    ):
        reasons.append("machine_name_runner_name_mismatch")
        _with_action(actions, _repair_command(can_reconcile_from_state=True))


def _add_launch_service_config_reasons(
    ctx: _LaunchReadinessContext,
    reasons: list[str],
    warnings: list[str],
    actions: list[str],
) -> None:
    machine_name = ctx.machine_name
    service_machine_name = ctx.service_machine_name
    service_machine_name_mismatch = machine_name and service_machine_name and machine_name != service_machine_name
    if ctx.can_reconcile_from_state and service_machine_name_mismatch:
        reasons.append("service_machine_name_mismatch")
        _with_action(actions, _repair_command(can_reconcile_from_state=True))

    service_state_hash_mismatch = ctx.state_hash and ctx.service_state_hash and ctx.state_hash != ctx.service_state_hash
    if ctx.can_reconcile_from_state and service_state_hash_mismatch:
        reasons.append("service_state_hash_mismatch")
        _with_action(actions, _repair_command(can_reconcile_from_state=True))

    if (
        ctx.can_reconcile_from_state
        and ctx.config_generation
        and ctx.service_config_generation
        and ctx.config_generation != ctx.service_config_generation
    ):
        if ctx.state_hash and ctx.service_state_hash and ctx.state_hash == ctx.service_state_hash:
            warnings.append("service_generation_mismatch")
        else:
            reasons.append("service_generation_mismatch")
        _with_action(actions, _repair_command(can_reconcile_from_state=True))


def _add_launch_shipper_state_reason(ctx: _LaunchReadinessContext, reasons: list[str], actions: list[str]) -> None:
    if ctx.service_status != "not-installed" and ctx.service_file_exists and not ctx.shipper_state_exists:
        reasons.append("shipper_state_missing")
        _with_action(actions, f"Inspect or restore shipper state: {ctx.shipper_db_path}")


def _add_launch_service_runner_reason(ctx: _LaunchReadinessContext, reasons: list[str], actions: list[str]) -> None:
    if (
        ctx.runner_expected
        and ctx.can_reconcile_from_state
        and ctx.runner_name
        and ctx.service_machine_name
        and ctx.runner_name != ctx.service_machine_name
    ):
        reasons.append("service_runner_name_mismatch")
        _with_action(actions, _repair_command(can_reconcile_from_state=True))


def _launch_readiness_configured(ctx: _LaunchReadinessContext) -> bool:
    return any(
        (
            ctx.state_exists,
            ctx.stored_url,
            ctx.machine_name,
            ctx.service_machine_name,
            ctx.runner.get("exists"),
        )
    )


def _launch_readiness_state(*, reasons: list[str], configured: bool) -> tuple[str, str]:
    if reasons:
        return "broken", "Managed launch config is inconsistent"
    if configured:
        return "ready", "Managed launch configuration looks coherent"
    return "unconfigured", "Managed launch has not been configured on this machine"


def _launch_readiness_payload(
    ctx: _LaunchReadinessContext,
    *,
    state: str,
    headline: str,
    reasons: list[str],
    warnings: list[str],
    actions: list[str],
) -> dict[str, Any]:
    return {
        "state": state,
        "headline": headline,
        "reasons": reasons,
        "warnings": warnings,
        "suggested_actions": actions,
        "control_plane_url": ctx.stored_url,
        "stored_url": ctx.stored_url,
        "machine_name": ctx.machine_name,
        "state_exists": ctx.state_exists,
        "state_error": ctx.state_error,
        "config_generation": ctx.config_generation,
        "state_hash": ctx.state_hash,
        "runner_expected": ctx.runner_expected,
        "service_machine_name": ctx.service_machine_name,
        "service_config_generation": ctx.service_config_generation,
        "service_state_hash": ctx.service_state_hash,
        "service_file_exists": ctx.service_file_exists,
        "shipper_db_path": str(ctx.shipper_db_path),
        "shipper_state_exists": ctx.shipper_state_exists,
        "runner": ctx.runner,
    }


def _collect_launch_readiness(base_dir: Path, *, service: dict[str, Any]) -> dict[str, Any]:
    ctx = _collect_launch_readiness_context(base_dir, service=service)
    reasons: list[str] = []
    warnings: list[str] = []
    actions: list[str] = []

    # Keep this ordering stable; the top-level health classifier preserves it.
    _add_launch_machine_state_reasons(ctx, reasons, actions)
    _add_launch_runner_config_reasons(ctx, reasons, actions)
    _add_launch_service_config_reasons(ctx, reasons, warnings, actions)
    _add_launch_shipper_state_reason(ctx, reasons, actions)
    _add_launch_service_runner_reason(ctx, reasons, actions)

    state, headline = _launch_readiness_state(
        reasons=reasons,
        configured=_launch_readiness_configured(ctx),
    )
    return _launch_readiness_payload(
        ctx,
        state=state,
        headline=headline,
        reasons=reasons,
        warnings=warnings,
        actions=actions,
    )


def _drop_launch_reason(reasons: list[str], reason_code: str) -> None:
    while reason_code in reasons:
        reasons.remove(reason_code)


def _launch_override_repair_command(
    readiness: dict[str, Any],
    *,
    stored_url: str | None,
    machine_name: str | None,
) -> str:
    return _repair_command(
        can_reconcile_from_state=_can_reconcile_launch_from_state(
            state_exists=bool(readiness.get("state_exists")),
            state_error=str(readiness.get("state_error") or "").strip() or None,
            stored_url=stored_url,
            machine_name=machine_name,
        )
    )


def _launch_override_context(
    readiness: dict[str, Any],
    *,
    runtime_url_override: str | None,
    machine_name_override: str | None,
) -> _LaunchOverrideContext:
    runner = dict(readiness.get("runner") or {})
    override_machine_name = str(machine_name_override or "").strip()
    stored_machine_name = str(readiness.get("machine_name") or "").strip()
    effective_machine_name = override_machine_name or stored_machine_name or None
    return _LaunchOverrideContext(
        effective_url=str(runtime_url_override or "").strip() or str(readiness.get("stored_url") or "").strip() or None,
        effective_machine_name=effective_machine_name,
        runner_expected=bool(readiness.get("runner_expected")),
        runner_name=str(runner.get("runner_name") or "").strip() or None,
        runner_urls=[str(item).strip() for item in list(runner.get("runner_urls") or []) if str(item).strip()],
        reasons=[str(item) for item in list(readiness.get("reasons") or [])],
        actions=[str(item) for item in list(readiness.get("suggested_actions") or [])],
        warnings=[str(item) for item in list(readiness.get("warnings") or [])],
        had_override=runtime_url_override is not None or machine_name_override is not None,
    )


def _apply_runner_url_override_reason(readiness: dict[str, Any], ctx: _LaunchOverrideContext) -> None:
    _drop_launch_reason(ctx.reasons, "config_url_runner_url_mismatch")
    runner_url_mismatch = ctx.effective_url and ctx.runner_urls and ctx.effective_url not in ctx.runner_urls
    if ctx.runner_expected and runner_url_mismatch:
        ctx.reasons.append("config_url_runner_url_mismatch")
        _with_action(
            ctx.actions,
            _launch_override_repair_command(
                readiness,
                stored_url=ctx.effective_url,
                machine_name=ctx.effective_machine_name,
            ),
        )


def _apply_runner_name_override_reason(readiness: dict[str, Any], ctx: _LaunchOverrideContext) -> None:
    _drop_launch_reason(ctx.reasons, "machine_name_runner_name_mismatch")
    effective_machine_name = ctx.effective_machine_name
    runner_name_mismatch = effective_machine_name and ctx.runner_name and effective_machine_name != ctx.runner_name
    if ctx.runner_expected and runner_name_mismatch:
        ctx.reasons.append("machine_name_runner_name_mismatch")
        _with_action(
            ctx.actions,
            _launch_override_repair_command(
                readiness,
                stored_url=ctx.effective_url,
                machine_name=ctx.effective_machine_name,
            ),
        )


def _launch_override_state(readiness: dict[str, Any], ctx: _LaunchOverrideContext) -> tuple[str, str]:
    state = str(readiness.get("state") or "unconfigured")
    headline = str(readiness.get("headline") or "Managed launch configuration looks coherent")
    if ctx.reasons:
        state = "broken"
        headline = "Managed launch config is inconsistent"
    elif ctx.had_override:
        state = "ready"
        headline = "Managed launch configuration looks coherent"
    return state, headline


def _apply_launch_readiness_overrides(
    readiness: dict[str, Any],
    *,
    runtime_url_override: str | None,
    machine_name_override: str | None,
) -> dict[str, Any]:
    ctx = _launch_override_context(
        readiness,
        runtime_url_override=runtime_url_override,
        machine_name_override=machine_name_override,
    )
    _apply_runner_url_override_reason(readiness, ctx)
    _apply_runner_name_override_reason(readiness, ctx)
    state, headline = _launch_override_state(readiness, ctx)

    readiness.update(
        {
            "state": state,
            "headline": headline,
            "reasons": ctx.reasons,
            "warnings": ctx.warnings,
            "suggested_actions": ctx.actions,
            "control_plane_url": ctx.effective_url,
            "machine_name": ctx.effective_machine_name,
        }
    )
    return readiness


def collect_launch_readiness(
    base_dir: str | Path | None = None,
    *,
    runtime_url_override: str | None = None,
    machine_name_override: str | None = None,
) -> dict[str, Any]:
    """Collect the local managed-launch readiness contract.

    `runtime_url_override` / `machine_name_override` let callers validate a
    concrete launch target without first mutating canonical machine state.
    """

    resolved_base_dir = _coerce_path(base_dir)
    service = _collect_service(resolved_base_dir)
    readiness = _collect_launch_readiness(resolved_base_dir, service=service)
    return _apply_launch_readiness_overrides(
        readiness,
        runtime_url_override=runtime_url_override,
        machine_name_override=machine_name_override,
    )


def _collect_service(base_dir: Path) -> dict[str, Any]:
    from zerg.services import local_health as _local_health_pkg

    return _local_health_pkg.get_service_info(str(base_dir))


__all__ = [
    "_state_root_tracks_machine_runner",
    "_collect_local_config",
    "_candidate_runner_env_paths",
    "_parse_env_file",
    "_runner_config_payload",
    "_missing_runner_config",
    "_runner_urls_from_env",
    "_runner_config_from_env",
    "_collect_runner_config",
    "_extract_machine_name_from_args",
    "_service_file_path",
    "_read_service_plist",
    "_systemd_exec_start_arguments",
    "_systemd_environment",
    "_service_metadata_from_env",
    "_empty_service_metadata",
    "_extract_service_machine_name",
    "_extract_service_metadata",
    "_can_reconcile_launch_from_state",
    "_repair_command",
    "_LaunchReadinessContext",
    "_LaunchOverrideContext",
    "_collect_launch_readiness_context",
    "_add_launch_machine_state_reasons",
    "_add_launch_runner_config_reasons",
    "_add_launch_service_config_reasons",
    "_add_launch_shipper_state_reason",
    "_add_launch_service_runner_reason",
    "_launch_readiness_configured",
    "_launch_readiness_state",
    "_launch_readiness_payload",
    "_collect_launch_readiness",
    "_drop_launch_reason",
    "_launch_override_repair_command",
    "_launch_override_context",
    "_apply_runner_url_override_reason",
    "_apply_runner_name_override_reason",
    "_launch_override_state",
    "_apply_launch_readiness_overrides",
    "collect_launch_readiness",
    "_collect_service",
]
