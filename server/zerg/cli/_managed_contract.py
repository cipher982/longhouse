"""CLI helpers for managed provider session contract files."""

from __future__ import annotations

from pathlib import Path

from zerg.services.longhouse_paths import resolve_longhouse_home_from_provider_home
from zerg.services.managed_session_contracts import build_managed_session_contract
from zerg.services.managed_session_contracts import capture_provider_version
from zerg.services.managed_session_contracts import remove_managed_session_contract
from zerg.services.managed_session_contracts import write_managed_session_contract


def record_managed_provider_contract(
    *,
    provider: str,
    session_id: str,
    cwd: Path,
    config_dir: Path | None,
    launch_mode: str | None,
    provider_binary_path: str | None,
    provider_binary_source: str | None,
    control_kind: str | None,
    control_state_path: str | Path | None = None,
    config_dir_is_provider_home: bool = False,
) -> Path:
    contract = build_managed_session_contract(
        session_id=session_id,
        provider=provider,
        cwd=cwd,
        launch_mode=launch_mode,
        provider_binary_path=provider_binary_path,
        provider_binary_source=provider_binary_source,
        provider_version=capture_provider_version(provider_binary_path),
        control_kind=control_kind,
        control_state_path=control_state_path,
    )
    base_dir = resolve_longhouse_home_from_provider_home(config_dir) if config_dir_is_provider_home else config_dir
    return write_managed_session_contract(contract, base_dir=base_dir)


def remove_managed_provider_contract(
    *,
    provider: str,
    session_id: str,
    config_dir: Path | None,
    config_dir_is_provider_home: bool = False,
) -> None:
    base_dir = resolve_longhouse_home_from_provider_home(config_dir) if config_dir_is_provider_home else config_dir
    remove_managed_session_contract(provider=provider, session_id=session_id, base_dir=base_dir)
