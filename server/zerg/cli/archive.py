"""Archive backlog inspection and control commands."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import typer

from zerg.config import get_settings
from zerg.services.archive_backlog import collect_archive_backlog
from zerg.services.archive_backlog import dead_letter_archive_path
from zerg.services.archive_backlog import inspect_archive_backlog
from zerg.services.archive_backlog import parse_byte_budget
from zerg.services.archive_backlog import ready_archive_backlog
from zerg.services.archive_backlog import retry_dead_archive_path
from zerg.services.archive_backlog import write_archive_control
from zerg.services.archive_store import FilesystemArchiveStore

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


def _format_duration(seconds: Any) -> str:
    try:
        total_seconds = int(float(seconds))
    except (TypeError, ValueError):
        return "-"
    if total_seconds < 0:
        return "-"
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, remaining_seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remaining_seconds}s"
    hours, remaining_minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {remaining_minutes}m"
    days, remaining_hours = divmod(hours, 24)
    return f"{days}d {remaining_hours}h"


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


def _eta_seconds(pending_bytes: Any, bytes_per_sec: Any) -> float | None:
    try:
        pending = float(pending_bytes or 0)
        rate = float(bytes_per_sec or 0)
    except (TypeError, ValueError):
        return None
    if pending <= 0 or rate < 1.0:
        return None
    return pending / rate


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
            "pending_bytes": summary.get("pending_bytes", 0),
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
    speed["archive"]["eta_seconds_ewma_10s"] = _eta_seconds(
        speed["archive"]["pending_bytes"],
        speed["archive"]["bytes_per_sec_ewma_10s"],
    )
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
    typer.echo(
        "  remaining: "
        f"{_format_bytes(speed['archive']['pending_bytes'])}, "
        f"eta {_format_duration(speed['archive']['eta_seconds_ewma_10s'])} at current EWMA"
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
    largest: bool = typer.Option(False, "--largest", help="Sort by pending bytes descending."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    state_root: Path | None = typer.Option(None, "--state-root", help="Longhouse home override for tests/debugging."),
) -> None:
    """List the largest pending archive paths."""

    _ = largest
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
    target: str | None = typer.Option(None, "--target", help="Drain target. Supported: max-safe."),
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
    normalized_target = str(target or "").strip().lower()
    if normalized_target and normalized_target != "max-safe":
        raise typer.BadParameter("--target currently supports only 'max-safe'")
    effective_include_huge = False if normalized_target == "max-safe" else include_huge
    result = write_archive_control(
        state_root,
        mode="drain",
        max_tick_bytes=parse_byte_budget(budget),
        include_huge=effective_include_huge,
    )
    if normalized_target == "max-safe":
        typer.echo(f"Archive repair max-safe drain enabled: {result['path']}")
    else:
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


@app.command("retry-dead")
def retry_dead_command(
    file_path: str = typer.Option(..., "--path", help="Exact source path to retry."),
    recoverable_only: bool = typer.Option(
        True,
        "--recoverable-only/--all",
        help="Only retry dead ranges with recoverable host/network errors.",
    ),
    state_root: Path | None = typer.Option(None, "--state-root", help="Longhouse home override for tests/debugging."),
) -> None:
    """Move dead-lettered archive ranges for one source path back to pending retry."""

    changed = retry_dead_archive_path(
        state_root,
        file_path=file_path,
        recoverable_only=recoverable_only,
    )
    typer.echo(f"Queued {changed} dead-lettered archive range(s) for retry.")


@app.command("export-legacy")
def export_legacy_command(
    session_id: str = typer.Option(..., "--session-id", help="Session UUID to export."),
    source_table: str = typer.Option(
        "source_lines",
        "--source-table",
        help="Legacy raw table: source_lines or events.",
    ),
    disk_floor: str = typer.Option(..., "--disk-floor", help="Required free-space floor, e.g. 30gb."),
    database_url: str | None = typer.Option(None, "--database-url", help="SQLite DATABASE_URL override."),
    archive_root: Path | None = typer.Option(None, "--archive-root", help="Archive root override."),
    tenant_id: str | None = typer.Option(None, "--tenant-id", help="Archive tenant id override."),
    batch_size: int = typer.Option(500, "--batch-size", min=1, help="Maximum legacy rows to read."),
    chunk_target: str = typer.Option("8mb", "--chunk-target", help="Target uncompressed archive chunk size."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Decode and count rows without archive/checkpoint writes."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Export one read-only legacy raw batch into the archive."""

    normalized_table = source_table.strip().lower()
    if normalized_table not in {"source_lines", "events"}:
        raise typer.BadParameter("--source-table must be source_lines or events")
    disk_floor_bytes = parse_byte_budget(disk_floor)
    if disk_floor_bytes is None or disk_floor_bytes <= 0:
        raise typer.BadParameter("--disk-floor must be greater than zero")
    chunk_target_bytes = parse_byte_budget(chunk_target)
    if chunk_target_bytes is None or chunk_target_bytes <= 0:
        raise typer.BadParameter("--chunk-target must be greater than zero")

    settings = get_settings()
    effective_database_url = database_url or settings.database_url
    effective_tenant_id = tenant_id or settings.archive_shadow_tenant_id
    effective_archive_root = archive_root or Path(settings.archive_root)

    from zerg.database import make_engine
    from zerg.database import make_sessionmaker
    from zerg.services.legacy_archive_exporter import export_legacy_raw_archive_batch

    engine = make_engine(effective_database_url)
    SessionLocal = make_sessionmaker(engine)
    archive_store = FilesystemArchiveStore(effective_archive_root)
    try:
        with SessionLocal() as db:
            result = export_legacy_raw_archive_batch(
                db,
                archive_store=archive_store,
                tenant_id=effective_tenant_id,
                source_table=normalized_table,  # type: ignore[arg-type]
                session_id=session_id,
                batch_size=batch_size,
                chunk_target_uncompressed_bytes=chunk_target_bytes,
                disk_floor_bytes=disk_floor_bytes,
                dry_run=dry_run,
            )
            if not dry_run:
                db.commit()
    finally:
        engine.dispose()

    payload = asdict(result)
    payload["session_id"] = str(result.session_id)
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    status = "paused" if result.paused else "ok"
    typer.echo(
        f"{status}: {result.source_table} session={result.session_id} "
        f"selected={result.selected_rows} exported={result.rows_exported} "
        f"skipped_no_raw={result.rows_skipped_no_raw} quarantined={result.rows_quarantined} "
        f"chunks={result.chunks_written} last_rowid={result.last_rowid}"
    )


@app.command("backfill-previews")
def backfill_previews_command(
    database_url: str | None = typer.Option(None, "--database-url", help="SQLite DATABASE_URL override."),
    limit: int = typer.Option(500, "--limit", min=1, help="Maximum sessions to backfill."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Rollback after computing the batch."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Backfill hot session preview columns from legacy events."""

    settings = get_settings()
    effective_database_url = database_url or settings.database_url

    from zerg.database import make_engine
    from zerg.database import make_sessionmaker
    from zerg.services.session_preview_backfill import backfill_missing_session_previews

    engine = make_engine(effective_database_url)
    SessionLocal = make_sessionmaker(engine)
    try:
        with SessionLocal() as db:
            result = backfill_missing_session_previews(db, limit=limit)
            if dry_run:
                db.rollback()
            else:
                db.commit()
    finally:
        engine.dispose()

    payload = asdict(result)
    payload["dry_run"] = dry_run
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    status = "dry-run" if dry_run else "ok"
    typer.echo(
        f"{status}: selected={result.selected_sessions} updated={result.updated_sessions} "
        f"cards={result.updated_timeline_cards} first_user={result.first_user_filled} "
        f"last_visible={result.last_visible_filled}"
    )


@app.command("backfill-compaction-kind")
def backfill_compaction_kind_command(
    database_url: str | None = typer.Option(None, "--database-url", help="SQLite DATABASE_URL override."),
    batch_size: int = typer.Option(1000, "--batch-size", min=1, help="Rows scanned per batch."),
    max_batches: int = typer.Option(0, "--max-batches", min=0, help="Stop after N batches (0 = until done)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Rollback after computing the batches."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Backfill events.compaction_kind from legacy raw payloads (resumable)."""

    settings = get_settings()
    effective_database_url = database_url or settings.database_url

    from zerg.database import make_engine
    from zerg.database import make_sessionmaker
    from zerg.services.compaction_kind_backfill import backfill_compaction_kind

    engine = make_engine(effective_database_url)
    SessionLocal = make_sessionmaker(engine)
    total_scanned = 0
    total_updated = 0
    last_id: int | None = 0
    batches = 0
    try:
        with SessionLocal() as db:
            cursor = 0
            while True:
                result = backfill_compaction_kind(db, after_id=cursor, batch_size=batch_size)
                if result.scanned == 0:
                    break
                total_scanned += result.scanned
                total_updated += result.updated
                last_id = result.last_id
                cursor = result.last_id or cursor
                batches += 1
                if max_batches and batches >= max_batches:
                    break
            if dry_run:
                db.rollback()
            else:
                db.commit()
    finally:
        engine.dispose()

    payload = {
        "scanned": total_scanned,
        "updated": total_updated,
        "last_id": last_id,
        "batches": batches,
        "dry_run": dry_run,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    status = "dry-run" if dry_run else "ok"
    typer.echo(f"{status}: scanned={total_scanned} updated={total_updated} batches={batches} last_id={last_id}")


@app.command("scan-orphan-tool-results")
def scan_orphan_tool_results_command(
    database_url: str | None = typer.Option(None, "--database-url", help="SQLite DATABASE_URL override."),
    session_id: str | None = typer.Option(None, "--session-id", help="Limit scan to one session UUID."),
    limit: int = typer.Option(500, "--limit", min=1, help="Maximum orphan calls to classify."),
    max_source_lines_per_call: int = typer.Option(
        500,
        "--max-source-lines-per-call",
        min=1,
        help="Maximum source_lines rows to inspect after each orphaned call.",
    ),
    archive_root: Path | None = typer.Option(None, "--archive-root", help="Archive root override for slim source_lines rows."),
    include_evidence: bool = typer.Option(False, "--include-evidence", help="Include source paths and recovered output previews in JSON."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Classify orphaned tool calls before any historical repair is attempted."""

    settings = get_settings()
    effective_database_url = database_url or settings.database_url
    effective_session_id = None
    if session_id:
        from uuid import UUID

        try:
            effective_session_id = str(UUID(session_id))
        except ValueError as exc:
            raise typer.BadParameter("--session-id must be a valid UUID") from exc

    from zerg.database import make_engine
    from zerg.database import make_sessionmaker
    from zerg.services.tool_result_repair import scan_orphan_tool_results

    engine = make_engine(effective_database_url)
    SessionLocal = make_sessionmaker(engine)
    archive_store = FilesystemArchiveStore(archive_root) if archive_root is not None else None
    try:
        with SessionLocal() as db:
            result = scan_orphan_tool_results(
                db,
                session_id=effective_session_id,
                limit=limit,
                max_source_lines_per_call=max_source_lines_per_call,
                archive_store=archive_store,
            )
    finally:
        engine.dispose()

    payload = _orphan_tool_result_scan_payload(result, include_evidence=include_evidence)
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    typer.echo(
        "orphan tool-result scan: "
        f"scanned={result.scanned_orphan_calls} recoverable={result.recoverable} "
        f"no_source={result.no_source_evidence} no_result={result.no_result_in_source} "
        f"unparseable={result.unparseable_result}"
    )


@app.command("repair-orphan-tool-results")
def repair_orphan_tool_results_command(
    database_url: str | None = typer.Option(None, "--database-url", help="SQLite DATABASE_URL override."),
    session_id: str | None = typer.Option(None, "--session-id", help="Limit repair to one session UUID."),
    limit: int = typer.Option(500, "--limit", min=1, help="Maximum orphan calls to classify."),
    max_source_lines_per_call: int = typer.Option(
        500,
        "--max-source-lines-per-call",
        min=1,
        help="Maximum source_lines rows to inspect after each orphaned call.",
    ),
    archive_root: Path | None = typer.Option(None, "--archive-root", help="Archive root override for slim source_lines rows."),
    apply: bool = typer.Option(False, "--apply", help="Insert recoverable missing tool-result events."),
    include_evidence: bool = typer.Option(False, "--include-evidence", help="Include source paths and recovered output previews in JSON."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Repair recoverable orphaned tool results. Defaults to dry-run."""

    settings = get_settings()
    effective_database_url = database_url or settings.database_url
    effective_session_id = None
    if session_id:
        from uuid import UUID

        try:
            effective_session_id = str(UUID(session_id))
        except ValueError as exc:
            raise typer.BadParameter("--session-id must be a valid UUID") from exc

    from zerg.database import make_engine
    from zerg.database import make_sessionmaker
    from zerg.services.tool_result_repair import repair_orphan_tool_results

    engine = make_engine(effective_database_url)
    SessionLocal = make_sessionmaker(engine)
    archive_store = FilesystemArchiveStore(archive_root) if archive_root is not None else None
    try:
        with SessionLocal() as db:
            try:
                result = repair_orphan_tool_results(
                    db,
                    session_id=effective_session_id,
                    limit=limit,
                    max_source_lines_per_call=max_source_lines_per_call,
                    archive_store=archive_store,
                    apply=apply,
                )
                if apply:
                    db.commit()
                else:
                    db.rollback()
            except Exception:
                db.rollback()
                raise
    finally:
        engine.dispose()

    payload = _orphan_tool_result_scan_payload(result, include_evidence=include_evidence)
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    mode = "apply" if apply else "dry-run"
    typer.echo(
        "orphan tool-result repair: "
        f"mode={mode} scanned={result.scanned_orphan_calls} recoverable={result.recoverable} "
        f"inserted={result.inserted} skipped_existing={result.skipped_existing} "
        f"no_source={result.no_source_evidence} no_result={result.no_result_in_source} "
        f"unparseable={result.unparseable_result}"
    )


def _orphan_tool_result_scan_payload(result, *, include_evidence: bool) -> dict[str, Any]:
    payload = asdict(result)
    if include_evidence:
        return payload
    payload["findings"] = [
        {
            "session_id": finding["session_id"],
            "event_id": finding["event_id"],
            "tool_call_id": finding["tool_call_id"],
            "branch_id": finding["branch_id"],
            "source_offset": finding["source_offset"],
            "status": finding["status"],
            "reason": finding["reason"],
            "recovered_event_uuid": finding["recovered_event_uuid"],
            "recovered_source_offset": finding["recovered_source_offset"],
        }
        for finding in payload["findings"]
    ]
    return payload
