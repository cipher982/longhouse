"""Archive backlog inspection and control commands."""

from __future__ import annotations

import json
import time
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import typer

from zerg.services.archive_backlog import collect_archive_backlog
from zerg.services.archive_backlog import dead_letter_archive_path
from zerg.services.archive_backlog import inspect_archive_backlog
from zerg.services.archive_backlog import parse_byte_budget
from zerg.services.archive_backlog import ready_archive_backlog
from zerg.services.archive_backlog import write_archive_control

app = typer.Typer(help="Inspect and control local archive backlog repair")


def _format_bytes(value: Any) -> str:
    size = int(value or 0)
    units = ("B", "KB", "MB", "GB", "TB")
    scaled = float(size)
    for unit in units:
        if scaled < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{size} B"
            return f"{scaled:.1f} {unit}"
        scaled /= 1024
    return f"{size} B"


def _format_rate(value: Any, suffix: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if abs(number) < 0.0001:
        number = 0.0
    return f"{number:.1f}{suffix}"


def _format_ms(value: Any) -> str:
    try:
        return f"{int(value)}ms"
    except (TypeError, ValueError):
        return "-"


def _format_epoch_ms(value: Any) -> str:
    try:
        timestamp_ms = int(value)
    except (TypeError, ValueError):
        return "-"
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def _shipper_diagnostics(
    summary: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    shipper = dict(summary.get("shipper") or {})
    scheduler = dict(shipper.get("ship_scheduler") or {})
    limiter = dict(shipper.get("adaptive_backlog_limiter") or {})
    lanes = dict(shipper.get("ship_lanes") or {})
    live_lane = dict(lanes.get("live") or {})
    archive_lane = dict(lanes.get("archive") or {})
    return shipper, scheduler, limiter, live_lane, archive_lane


@app.command("status")
def status_command(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    watch: bool = typer.Option(False, "--watch", help="Refresh until interrupted."),
    interval: float = typer.Option(2.0, "--interval", min=0.5, help="Watch refresh interval in seconds."),
    state_root: Path | None = typer.Option(None, "--state-root", help="Longhouse home override for tests/debugging."),
) -> None:
    """Show local archive backlog repair state."""

    if watch and json_output:
        raise typer.BadParameter("--watch cannot be combined with --json")

    if watch:
        try:
            while True:
                typer.echo("\033[2J\033[H", nl=False)
                _render_status_summary(collect_archive_backlog(state_root))
                time.sleep(interval)
        except KeyboardInterrupt:
            return
        return

    summary = collect_archive_backlog(state_root)
    if json_output:
        typer.echo(json.dumps(summary, indent=2))
        return

    _render_status_summary(summary)


def _render_status_summary(summary: dict[str, Any]) -> None:
    typer.echo(f"Archive repair: {summary['state']} ({summary['mode']})")
    typer.echo(f"  pending ranges:   {summary['pending_ranges']}")
    typer.echo(f"  pending paths:    {summary['pending_paths']}")
    typer.echo(f"  pending sessions: {summary['pending_sessions']}")
    typer.echo(f"  pending bytes:    {_format_bytes(summary['pending_bytes'])}")
    typer.echo(f"  huge ranges:      {summary['huge_pending_ranges']} ({_format_bytes(summary['huge_pending_bytes'])})")
    typer.echo(f"  dead letters:     {summary['dead_ranges']} ({_format_bytes(summary['dead_bytes'])})")
    if summary.get("oldest_pending_at"):
        typer.echo(f"  oldest pending:   {summary['oldest_pending_at']}")
    if summary.get("next_retry_at_min"):
        typer.echo(f"  next retry:       {summary['next_retry_at_min']}")

    _, scheduler, limiter, live_lane, archive_lane = _shipper_diagnostics(summary)

    if scheduler or limiter or live_lane or archive_lane:
        typer.echo("")
        typer.echo("Shipper controller:")
    if scheduler:
        ready_archive = int(scheduler.get("ready_retry") or 0) + int(scheduler.get("ready_scan") or 0)
        active_archive = int(scheduler.get("in_flight_retry") or 0) + int(scheduler.get("in_flight_scan") or 0)
        ready_archive_bytes = _format_bytes(scheduler.get("ready_backlog_bytes"))
        active_archive_bytes = _format_bytes(scheduler.get("in_flight_backlog_bytes"))
        typer.echo(
            "  scheduler: "
            f"ready live {scheduler.get('ready_live', 0)}, "
            f"ready archive {ready_archive} ({ready_archive_bytes}), "
            f"active archive {active_archive} ({active_archive_bytes}), "
            f"archive cap {scheduler.get('backlog_cap', '-')}"
        )
    if limiter:
        typer.echo(
            "  limiter: "
            f"cap {limiter.get('current_cap', '-')}/{limiter.get('ceiling', '-')}, "
            f"pressure {limiter.get('pressure_state', '-')}, "
            f"live guard {limiter.get('live_latency_guard_state', '-')}, "
            f"batch {_format_bytes(limiter.get('archive_target_batch_bytes'))}"
        )
        typer.echo(
            "  host: "
            f"queue ewma {_format_rate(limiter.get('ewma_queue_wait_ms'), 'ms')}, "
            f"exec ewma {_format_rate(limiter.get('ewma_exec_ms'), 'ms')}, "
            f"backpressure {limiter.get('total_backpressure', 0)}"
        )
    if live_lane:
        typer.echo(
            "  live 1h: "
            f"{live_lane.get('successes_1h', 0)}/{live_lane.get('attempts_1h', 0)} ok, "
            f"{live_lane.get('connect_errors_1h', 0)} connect errors, "
            f"latency p50/p95 "
            f"{_format_ms(live_lane.get('latency_p50_ms_1h'))}/"
            f"{_format_ms(live_lane.get('latency_p95_ms_1h'))}"
        )
        stage_p95 = dict(live_lane.get("stage_latency_p95_ms_1h") or {})
        if stage_p95:
            typer.echo(
                "  live stages p95: "
                f"observed->send {_format_ms(stage_p95.get('observed_to_http_send_ms'))}, "
                f"observed->ack {_format_ms(stage_p95.get('observed_to_ack_ms'))}, "
                f"enqueue->job {_format_ms(stage_p95.get('enqueue_to_job_ms'))}, "
                f"http {_format_ms(stage_p95.get('http_latency_ms'))}"
            )
        if (
            live_lane.get("last_observed_at_ms") is not None
            or live_lane.get("last_http_send_started_at_ms") is not None
            or live_lane.get("last_http_finished_at_ms") is not None
        ):
            typer.echo(
                "  last live: "
                f"observed {_format_epoch_ms(live_lane.get('last_observed_at_ms'))}, "
                f"send {_format_epoch_ms(live_lane.get('last_http_send_started_at_ms'))}, "
                f"ack {_format_epoch_ms(live_lane.get('last_http_finished_at_ms'))}"
            )
    if archive_lane:
        typer.echo(
            "  archive 1h: "
            f"{archive_lane.get('successes_1h', 0)}/{archive_lane.get('attempts_1h', 0)} ok, "
            f"{archive_lane.get('backpressure_1h', 0)} backpressure, "
            f"{_format_bytes(archive_lane.get('bytes_1h'))}, "
            f"{archive_lane.get('events_1h', 0)} events"
        )


@app.command("speed")
def speed_command(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    state_root: Path | None = typer.Option(None, "--state-root", help="Longhouse home override for tests/debugging."),
) -> None:
    """Show archive drain speed and live-lane guardrail signals."""

    summary = collect_archive_backlog(state_root)
    _, scheduler, limiter, live_lane, archive_lane = _shipper_diagnostics(summary)
    ready_backlog = int(
        scheduler.get("ready_backlog")
        if scheduler.get("ready_backlog") is not None
        else int(scheduler.get("ready_retry") or 0) + int(scheduler.get("ready_scan") or 0)
    )
    in_flight_backlog = int(
        scheduler.get("in_flight_backlog")
        if scheduler.get("in_flight_backlog") is not None
        else int(scheduler.get("in_flight_retry") or 0) + int(scheduler.get("in_flight_scan") or 0)
    )
    speed = {
        "archive": {
            "bytes_per_sec_ewma_10s": archive_lane.get("bytes_per_sec_ewma_10s"),
            "events_per_sec_ewma_10s": archive_lane.get("events_per_sec_ewma_10s"),
            "attempts_1h": archive_lane.get("attempts_1h", 0),
            "successes_1h": archive_lane.get("successes_1h", 0),
            "backpressure_1h": archive_lane.get("backpressure_1h", 0),
            "bytes_1h": archive_lane.get("bytes_1h", 0),
            "events_1h": archive_lane.get("events_1h", 0),
        },
        "live": {
            "latency_p95_ms_1h": live_lane.get("latency_p95_ms_1h"),
            "observed_to_ack_p95_ms_1h": dict(live_lane.get("stage_latency_p95_ms_1h") or {}).get("observed_to_ack_ms"),
            "limiter_state": limiter.get("live_latency_guard_state"),
            "limiter_latency_p95_ms": limiter.get("last_live_latency_p95_ms"),
            "limiter_enqueue_to_job_p95_ms": limiter.get("last_live_enqueue_to_job_p95_ms"),
            "limiter_cooldown_remaining_ms": limiter.get("live_pressure_cooldown_remaining_ms"),
        },
        "scheduler": {
            "ready_backlog": ready_backlog,
            "ready_backlog_bytes": scheduler.get("ready_backlog_bytes", 0),
            "in_flight_backlog": in_flight_backlog,
            "in_flight_backlog_bytes": scheduler.get("in_flight_backlog_bytes", 0),
            "backlog_cap": scheduler.get("backlog_cap"),
        },
        "host": {
            "pressure_state": limiter.get("pressure_state"),
            "queue_wait_ewma_ms": limiter.get("ewma_queue_wait_ms"),
            "exec_ewma_ms": limiter.get("ewma_exec_ms"),
            "archive_target_batch_bytes": limiter.get("archive_target_batch_bytes"),
            "total_backpressure": limiter.get("total_backpressure", 0),
        },
    }
    if json_output:
        typer.echo(json.dumps(speed, indent=2))
        return

    typer.echo("Archive speed")
    typer.echo(
        "  archive: "
        f"{_format_rate(speed['archive']['events_per_sec_ewma_10s'], ' events/s')}, "
        f"{_format_bytes(speed['archive']['bytes_per_sec_ewma_10s'])}/s, "
        f"{speed['archive']['successes_1h']}/{speed['archive']['attempts_1h']} ok, "
        f"{speed['archive']['backpressure_1h']} backpressure"
    )
    typer.echo(f"  totals 1h: {_format_bytes(speed['archive']['bytes_1h'])}, {speed['archive']['events_1h']} events")
    typer.echo(
        "  live guardrail: "
        f"p95 {_format_ms(speed['live']['latency_p95_ms_1h'])}, "
        f"observed->ack p95 {_format_ms(speed['live']['observed_to_ack_p95_ms_1h'])}, "
        f"state {speed['live']['limiter_state'] or '-'}, "
        f"limiter p95 {_format_ms(speed['live']['limiter_latency_p95_ms'])}, "
        f"enqueue->job {_format_ms(speed['live']['limiter_enqueue_to_job_p95_ms'])}"
    )
    if speed["live"]["limiter_cooldown_remaining_ms"] is not None:
        typer.echo(f"  live cooldown: {_format_ms(speed['live']['limiter_cooldown_remaining_ms'])}")
    typer.echo(
        "  scheduler: "
        f"ready {speed['scheduler']['ready_backlog']} ({_format_bytes(speed['scheduler']['ready_backlog_bytes'])}), "
        f"active {speed['scheduler']['in_flight_backlog']} "
        f"({_format_bytes(speed['scheduler']['in_flight_backlog_bytes'])}), "
        f"cap {speed['scheduler']['backlog_cap']}"
    )
    typer.echo(
        "  host: "
        f"{speed['host']['pressure_state'] or '-'}, "
        f"queue {_format_rate(speed['host']['queue_wait_ewma_ms'], 'ms')}, "
        f"exec {_format_rate(speed['host']['exec_ewma_ms'], 'ms')}, "
        f"batch {_format_bytes(speed['host']['archive_target_batch_bytes'])}, "
        f"backpressure {speed['host']['total_backpressure']}"
    )


@app.command("inspect")
def inspect_command(
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="Maximum pending paths to show."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    state_root: Path | None = typer.Option(None, "--state-root", help="Longhouse home override for tests/debugging."),
) -> None:
    """List the largest pending archive paths."""

    rows = inspect_archive_backlog(state_root, limit=limit)
    if json_output:
        typer.echo(json.dumps(rows, indent=2))
        return
    for row in rows:
        provider = row["provider"]
        pending_bytes = _format_bytes(row["pending_bytes"])
        pending_ranges = row["pending_ranges"]
        file_path = row["file_path"]
        typer.echo(f"{provider} {pending_bytes} {pending_ranges} range(s) {file_path}")


@app.command("pause")
def pause_command(
    archive_class: str | None = typer.Option(None, "--class", help="Archive class to pause. Supported: huge."),
    state_root: Path | None = typer.Option(None, "--state-root", help="Longhouse home override for tests/debugging."),
) -> None:
    """Pause local archive repair replay."""

    if archive_class:
        normalized_class = archive_class.strip().lower()
        if normalized_class != "huge":
            raise typer.BadParameter("--class currently supports only 'huge'")
        result = write_archive_control(state_root, mode="drain", include_huge=False)
        typer.echo(f"Archive repair huge-range replay paused; non-huge drain remains enabled: {result['path']}")
        return

    result = write_archive_control(state_root, mode="paused")
    typer.echo(f"Archive repair paused: {result['path']}")


@app.command("resume")
def resume_command(
    mode: str = typer.Option("drain", "--mode", help="Resume mode: trickle or drain."),
    budget: str | None = typer.Option(None, "--budget", help="Per-tick byte budget, e.g. 512MB or 4GB."),
    include_huge: bool = typer.Option(True, "--include-huge/--exclude-huge", help="Allow ranges >=100MB."),
    retry_now: bool = typer.Option(False, "--retry-now", help="Make pending archive ranges eligible immediately."),
    state_root: Path | None = typer.Option(None, "--state-root", help="Longhouse home override for tests/debugging."),
) -> None:
    """Resume local archive repair replay."""

    result = write_archive_control(
        state_root,
        mode=mode,
        max_tick_bytes=parse_byte_budget(budget),
        include_huge=include_huge,
    )
    typer.echo(f"Archive repair resumed in {result['mode']} mode: {result['path']}")
    if retry_now:
        changed = ready_archive_backlog(state_root)
        typer.echo(f"Archive retry clocks reset for {changed} pending range(s).")


@app.command("drain")
def drain_command(
    budget: str = typer.Option("4GB", "--budget", help="Per-tick byte budget, e.g. 4GB."),
    include_huge: bool = typer.Option(True, "--include-huge/--exclude-huge", help="Allow ranges >=100MB."),
    retry_now: bool = typer.Option(False, "--retry-now", help="Make pending archive ranges eligible immediately."),
    max_minutes: int | None = typer.Option(
        None,
        "--max-minutes",
        help="Accepted for operator intent; engine drains by tick budget.",
    ),
    state_root: Path | None = typer.Option(None, "--state-root", help="Longhouse home override for tests/debugging."),
) -> None:
    """Switch archive repair to explicit drain mode."""

    _ = max_minutes
    result = write_archive_control(
        state_root,
        mode="drain",
        max_tick_bytes=parse_byte_budget(budget),
        include_huge=include_huge,
    )
    typer.echo(f"Archive repair drain enabled: {result['path']}")
    if retry_now:
        changed = ready_archive_backlog(state_root)
        typer.echo(f"Archive retry clocks reset for {changed} pending range(s).")


@app.command("dead-letter")
def dead_letter_command(
    file_path: str = typer.Option(..., "--path", help="Exact source path to dead-letter."),
    reason: str = typer.Option(..., "--reason", help="Operator reason recorded on pending ranges."),
    state_root: Path | None = typer.Option(None, "--state-root", help="Longhouse home override for tests/debugging."),
) -> None:
    """Move pending ranges for one source path to dead-letter state."""

    changed = dead_letter_archive_path(state_root, file_path=file_path, reason=reason)
    typer.echo(f"Dead-lettered {changed} pending archive range(s).")
