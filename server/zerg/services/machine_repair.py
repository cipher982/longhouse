"""Canonical local-machine repair flow for installed Longhouse runtimes."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from zerg.services.longhouse_paths import resolve_longhouse_home_from_provider_home
from zerg.services.machine_state import load_machine_state
from zerg.services.shipper import load_token
from zerg.services.shipper.service import get_engine_executable

if TYPE_CHECKING:
    from zerg.services.local_runtime_installer import LocalRuntimeReconcileResult


def recommended_machine_repair_command(*, can_reconcile_from_state: bool) -> str:
    if can_reconcile_from_state:
        return "Run: longhouse machine repair"
    return "Run: longhouse connect --install"


def can_repair_machine_from_state(
    *,
    claude_dir: str | None = None,
    state_root: Path | None = None,
) -> bool:
    if state_root is None:
        state_root = resolve_longhouse_home_from_provider_home(claude_dir)
    state = load_machine_state(state_root)
    if state is None:
        return False
    return bool(str(state.runtime_url or "").strip() and str(state.machine_name or "").strip())


@dataclass(frozen=True)
class SpoolReplayResult:
    attempted: bool
    success: bool
    summary: dict[str, Any] | None = None
    warning: str | None = None


@dataclass(frozen=True)
class MachineRepairResult:
    reconcile_result: "LocalRuntimeReconcileResult"
    spool_replay: SpoolReplayResult
    health_snapshot: dict[str, Any]


def _extract_ship_summary(stdout: str) -> dict[str, Any] | None:
    raw = str(stdout or "").strip()
    if not raw:
        return None
    start = raw.find("{")
    if start < 0:
        return None
    try:
        parsed, _end = json.JSONDecoder().raw_decode(raw[start:])
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def replay_machine_backlog(*, url: str, token: str, claude_dir: str | None) -> SpoolReplayResult:
    """Best-effort drain of queued local shipping backlog for the current machine."""
    try:
        engine = get_engine_executable()
    except RuntimeError as exc:
        logging.getLogger(__name__).debug("Skipping backlog replay: %s", exc)
        return SpoolReplayResult(
            attempted=False,
            success=False,
            warning=f"Queued shipping replay skipped: {exc}",
        )

    env = os.environ.copy()
    if claude_dir:
        env["CLAUDE_CONFIG_DIR"] = claude_dir

    try:
        completed = subprocess.run(
            [
                engine,
                "ship",
                "--url",
                url,
                "--token",
                token,
                "--json",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        logging.getLogger(__name__).warning("Queued shipping replay failed to start: %s", exc)
        return SpoolReplayResult(
            attempted=True,
            success=False,
            warning="Queued shipping could not be replayed immediately. Run `longhouse ship` if backlog stays stuck.",
        )

    summary = _extract_ship_summary(completed.stdout)
    if completed.returncode == 0:
        return SpoolReplayResult(
            attempted=True,
            success=True,
            summary=summary,
        )

    detail_lines = (completed.stderr or completed.stdout or "").strip().splitlines()
    logging.getLogger(__name__).warning(
        "Queued shipping replay exited %s%s",
        completed.returncode,
        f": {detail_lines[0]}" if detail_lines else "",
    )
    return SpoolReplayResult(
        attempted=True,
        success=False,
        summary=summary,
        warning="Queued shipping could not be replayed immediately. Run `longhouse ship` if backlog stays stuck.",
    )


def repair_machine_runtime(*, claude_dir: str | None) -> MachineRepairResult:
    """Repair an already-configured local machine and return post-repair health."""
    from zerg.services.local_runtime_installer import reconcile_local_runtime

    config_dir: Path = resolve_longhouse_home_from_provider_home(claude_dir)
    reconcile_result = reconcile_local_runtime(
        claude_dir=claude_dir,
        written_by="machine-repair",
    )

    runtime_url = str(reconcile_result.machine_state.runtime_url or "").strip()
    token = load_token(config_dir)
    if runtime_url and token:
        spool_replay = replay_machine_backlog(
            url=runtime_url,
            token=token,
            claude_dir=claude_dir,
        )
    elif not token:
        spool_replay = SpoolReplayResult(
            attempted=False,
            success=False,
            warning="No device token configured; skipped queued shipping replay.",
        )
    else:
        spool_replay = SpoolReplayResult(
            attempted=False,
            success=False,
            warning="Machine runtime URL is missing; skipped queued shipping replay.",
        )

    from zerg.services.local_health import collect_local_health

    health_snapshot = collect_local_health(config_dir)
    return MachineRepairResult(
        reconcile_result=reconcile_result,
        spool_replay=spool_replay,
        health_snapshot=health_snapshot,
    )
