"""CLI helpers for managed provider session contract files."""

from __future__ import annotations

from pathlib import Path

from zerg.services.managed_session_contracts import build_managed_session_contract
from zerg.services.managed_session_contracts import capture_provider_version
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
    return write_managed_session_contract(contract, base_dir=config_dir)
