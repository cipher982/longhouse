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
from typing import Callable

from zerg.services.longhouse_paths import resolve_longhouse_home_from_provider_home
from zerg.services.machine_state import load_machine_state
from zerg.services.shipper import load_token
from zerg.services.shipper.service import get_engine_executable

if TYPE_CHECKING:
    from zerg.services.local_runtime_installer import LocalRuntimeReconcileResult

ProgressReporter = Callable[[str], None]


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


def _report_progress(progress: ProgressReporter | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _format_replay_summary(summary: dict[str, Any] | None) -> str:
    if not isinstance(summary, dict):
        return "Queued shipping replay finished."
    parts: list[str] = []
    labels = (
        ("spool_replayed", "replayed"),
        ("spool_pending", "pending"),
        ("spool_dead", "dead"),
        ("outbox_files", "outbox"),
    )
    for key, label in labels:
        value = summary.get(key)
        if value is not None:
            parts.append(f"{label}={value}")
    if not parts:
        return "Queued shipping replay finished."
    return "Queued shipping replay finished: " + ", ".join(parts) + "."


def replay_machine_backlog(
    *,
    url: str,
    token: str,
    claude_dir: str | None,
    progress: ProgressReporter | None = None,
) -> SpoolReplayResult:
    """Best-effort drain of queued local shipping backlog for the current machine."""
    try:
        engine = get_engine_executable()
    except RuntimeError as exc:
        logging.getLogger(__name__).debug("Skipping backlog replay: %s", exc)
        _report_progress(progress, f"Queued shipping replay skipped: {exc}")
        return SpoolReplayResult(
            attempted=False,
            success=False,
            warning=f"Queued shipping replay skipped: {exc}",
        )

    env = os.environ.copy()
    if claude_dir:
        env["CLAUDE_CONFIG_DIR"] = claude_dir

    _report_progress(progress, "Starting queued shipping replay with longhouse-engine ship --json.")
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
    except subprocess.TimeoutExpired:
        warning = "Queued shipping replay timed out after 30 seconds; the Machine Agent will keep retrying in the background."
        logging.getLogger(__name__).warning(warning)
        _report_progress(progress, warning)
        return SpoolReplayResult(
            attempted=True,
            success=False,
            warning=warning,
        )
    except Exception as exc:
        logging.getLogger(__name__).warning("Queued shipping replay failed to start: %s", exc)
        _report_progress(progress, f"Queued shipping replay could not start: {exc}")
        return SpoolReplayResult(
            attempted=True,
            success=False,
            warning="Queued shipping could not be replayed immediately. Run `longhouse ship` if backlog stays stuck.",
        )

    summary = _extract_ship_summary(completed.stdout)
    if completed.returncode == 0:
        _report_progress(progress, _format_replay_summary(summary))
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
    if detail_lines:
        _report_progress(progress, f"Queued shipping replay exited {completed.returncode}: {detail_lines[0]}")
    else:
        _report_progress(progress, f"Queued shipping replay exited {completed.returncode}.")
    return SpoolReplayResult(
        attempted=True,
        success=False,
        summary=summary,
        warning="Queued shipping could not be replayed immediately. Run `longhouse ship` if backlog stays stuck.",
    )


def repair_machine_runtime(*, claude_dir: str | None, progress: ProgressReporter | None = None) -> MachineRepairResult:
    """Repair an already-configured local machine and return post-repair health."""
    from zerg.services.local_runtime_installer import reconcile_local_runtime

    config_dir: Path = resolve_longhouse_home_from_provider_home(claude_dir)
    _report_progress(progress, "Step 1/4: reconciling local runtime from canonical machine state.")
    reconcile_result = reconcile_local_runtime(
        claude_dir=claude_dir,
        written_by="machine-repair",
    )

    runtime_url = str(reconcile_result.machine_state.runtime_url or "").strip()
    token = load_token(config_dir)
    if runtime_url and token:
        _report_progress(progress, "Step 2/4: replaying queued shipping backlog.")
        spool_replay = replay_machine_backlog(
            url=runtime_url,
            token=token,
            claude_dir=claude_dir,
            progress=progress,
        )
    elif not token:
        _report_progress(progress, "Step 2/4: skipping queued shipping replay because no device token is configured.")
        spool_replay = SpoolReplayResult(
            attempted=False,
            success=False,
            warning="No device token configured; skipped queued shipping replay.",
        )
    else:
        _report_progress(progress, "Step 2/4: skipping queued shipping replay because the runtime URL is missing.")
        spool_replay = SpoolReplayResult(
            attempted=False,
            success=False,
            warning="Machine runtime URL is missing; skipped queued shipping replay.",
        )

    from zerg.services.local_health import collect_local_health

    _report_progress(progress, "Step 3/4: collecting post-repair local health snapshot.")
    health_snapshot = collect_local_health(config_dir)
    _report_progress(progress, f"Step 4/4: repair complete; local health is {health_snapshot.get('health_state')}.")
    return MachineRepairResult(
        reconcile_result=reconcile_result,
        spool_replay=spool_replay,
        health_snapshot=health_snapshot,
    )
