"""Main CLI entry point for Longhouse."""

import json
import sys
import time
from pathlib import Path

import typer

import zerg.bootstrap_sqlite  # noqa: F401
from zerg.build_info import BuildIdentityMissing
from zerg.build_info import load as load_build_identity
from zerg.cli.antigravity import antigravity
from zerg.cli.antigravity_channel import app as antigravity_channel_app
from zerg.cli.apns_smoke import apns_smoke_command
from zerg.cli.archive import app as archive_app
from zerg.cli.claude import claude
from zerg.cli.claude_channel import app as claude_channel_app
from zerg.cli.codex import app as codex_app
from zerg.cli.connect import auth
from zerg.cli.connect import connect as connect_command
from zerg.cli.connect import recall
from zerg.cli.connect import ship
from zerg.cli.coordination import message
from zerg.cli.coordination import messages_app
from zerg.cli.coordination import peers
from zerg.cli.coordination import tail
from zerg.cli.coordination import wall
from zerg.cli.cursor import app as cursor_app
from zerg.cli.doctor import doctor
from zerg.cli.local_health import app as local_health_app
from zerg.cli.machine import app as machine_app
from zerg.cli.mcp_serve import mcp_server
from zerg.cli.onboard import onboard
from zerg.cli.opencode import opencode
from zerg.cli.opencode_bridge import app as opencode_bridge_app
from zerg.cli.opencode_channel import app as opencode_channel_app
from zerg.cli.provider_live import app as provider_live_app
from zerg.cli.runtime_artifact_smoke import runtime_artifact_install_command
from zerg.cli.runtime_artifact_smoke import runtime_artifact_smoke_command
from zerg.cli.serve import hash_password
from zerg.cli.serve import serve
from zerg.cli.serve import status
from zerg.cli.sessions import app as sessions_app
from zerg.cli.sessions import continue_session
from zerg.cli.update_manager import maybe_notify_update
from zerg.cli.update_manager import record_install_command
from zerg.cli.update_manager import upgrade_command
from zerg.cli.update_manager import version_command

app = typer.Typer(
    name="longhouse",
    help="Longhouse AI Agent Platform CLI",
    no_args_is_help=True,
)


def _emit_version(json_output: bool) -> None:
    try:
        identity = load_build_identity()
    except BuildIdentityMissing as exc:
        if json_output:
            typer.echo(json.dumps({"error": "build identity missing — rebuild", "detail": str(exc)}, indent=2))
        else:
            typer.echo(f"longhouse: build identity missing — rebuild. ({exc})", err=True)
        raise typer.Exit(code=2)
    if json_output:
        typer.echo(json.dumps({"installed_version": identity.qualified_version, "build": identity.as_dict()}, indent=2))
    else:
        typer.echo(f"longhouse {identity.qualified_version}")
    raise typer.Exit()


@app.callback(invoke_without_command=True)
def app_callback(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        help="Show Longhouse version and exit.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="When paired with --version, emit JSON instead of text.",
        hidden=True,
    ),
) -> None:
    """Longhouse AI Agent Platform CLI."""
    if version:
        _emit_version(json_output)
    maybe_notify_update(sys.argv[1:])
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


config_app = typer.Typer(help="Configuration management")
db_app = typer.Typer(help="SQLite database diagnostics and maintenance")


@config_app.command(name="show")
def config_show() -> None:
    """Show effective configuration with sources.

    Displays the merged configuration from file, environment variables,
    and defaults, indicating where each value comes from.
    """
    from zerg.cli.config_file import get_config_path
    from zerg.cli.config_file import get_effective_config_display
    from zerg.cli.config_file import load_config

    config = load_config()
    entries = get_effective_config_display(config)

    config_path = get_config_path()
    typer.echo(f"Config file: {config_path}")
    typer.echo(f"  {'exists' if config_path.exists() else 'not found'}")
    typer.echo("")
    typer.echo("Effective configuration:")
    typer.echo("-" * 50)

    for key, value, source in entries:
        source_indicator = {
            "file": typer.style("[file]", fg=typer.colors.CYAN),
            "env": typer.style("[env]", fg=typer.colors.YELLOW),
            "default": typer.style("[default]", fg=typer.colors.WHITE, dim=True),
        }.get(source, f"[{source}]")
        typer.echo(f"  {key}: {value} {source_indicator}")


def _resolve_db_engine(database_url: str | None):
    from zerg.database import default_engine
    from zerg.database import make_engine

    if database_url:
        engine = make_engine(database_url)
        return engine, database_url
    if default_engine is None:
        raise typer.Exit(code=2)
    return default_engine, str(default_engine.url)


def _resolve_db_url(database_url: str | None) -> str:
    from zerg.database import default_engine

    if database_url:
        return database_url
    if default_engine is None:
        raise typer.Exit(code=2)
    return str(default_engine.url)


@db_app.command(name="doctor")
def db_doctor(
    database_url: str | None = typer.Option(
        None,
        "--database-url",
        help="SQLite DATABASE_URL override (defaults to env).",
    ),
    live_database_url: str | None = typer.Option(
        None,
        "--live-database-url",
        help="Optional Live Store SQLite URL override (defaults to LONGHOUSE_LIVE_* env).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    deep: bool = typer.Option(
        False,
        "--deep",
        help="Run explicit indexed COUNT diagnostics for known maintenance backlogs.",
    ),
    identity_counts: bool = typer.Option(
        False,
        "--identity-counts",
        help="With --deep, include thread_id NULL counts that may scan large archive tables.",
    ),
    table_bytes: bool = typer.Option(
        False,
        "--table-bytes",
        help="Walk SQLite dbstat pages and include physical table/index byte usage.",
    ),
    table_bytes_cache: bool = typer.Option(
        False,
        "--table-bytes-cache",
        help="Include the full cached physical table/index byte map when available.",
    ),
    table_bytes_cache_max_age_seconds: int = typer.Option(
        86400,
        "--table-bytes-cache-max-age-seconds",
        min=1,
        help="Freshness threshold for cached table-byte samples.",
    ),
) -> None:
    """Inspect SQLite file, disk, planner, and optional backlog diagnostics."""
    from zerg.services.db_diagnostics import collect_sqlite_db_stats
    from zerg.services.db_diagnostics import collect_sqlite_deep_counts
    from zerg.services.db_diagnostics import collect_sqlite_schema_stats
    from zerg.services.db_diagnostics import collect_sqlite_store_stats
    from zerg.services.db_diagnostics import collect_sqlite_table_bytes
    from zerg.services.db_diagnostics import load_sqlite_table_bytes_cache
    from zerg.services.db_diagnostics import sqlite_db_paths

    engine, resolved_database_url = _resolve_db_engine(database_url)
    if live_database_url is None:
        from zerg.config import get_settings_unchecked

        live_database_url = get_settings_unchecked().live_database_url
    with engine.connect() as conn:
        payload = collect_sqlite_db_stats(resolved_database_url, db=conn)
        if payload is None:
            typer.echo("Database doctor only supports file-backed SQLite databases.", err=True)
            raise typer.Exit(code=2)
        payload["schema"] = collect_sqlite_schema_stats(conn)
        if deep:
            payload["deep_counts"] = collect_sqlite_deep_counts(conn, include_identity_counts=identity_counts)
            payload["deep_counts_skipped"] = False
        else:
            payload["deep_counts"] = None
            payload["deep_counts_skipped"] = True
        if table_bytes:
            payload["table_bytes"] = collect_sqlite_table_bytes(conn)
            payload["table_bytes_skipped"] = False
        else:
            payload["table_bytes"] = None
            payload["table_bytes_skipped"] = True
        payload["table_bytes_cache"] = load_sqlite_table_bytes_cache(
            resolved_database_url,
            max_age_seconds=table_bytes_cache_max_age_seconds,
            current_stats=payload,
            include_table_bytes=table_bytes_cache,
        )
        live_store_paths = sqlite_db_paths(live_database_url) if live_database_url else None
        live_store_db_path = live_store_paths[0].expanduser() if live_store_paths is not None else None
        if live_database_url and live_store_db_path is not None and live_store_db_path.exists():
            from zerg.database import make_live_engine

            live_engine = make_live_engine(live_database_url)
            try:
                with live_engine.connect() as live_conn:
                    payload["live_store"] = collect_sqlite_store_stats(
                        live_database_url,
                        archive_database_url=resolved_database_url,
                        db=live_conn,
                    )
            finally:
                live_engine.dispose()
        else:
            payload["live_store"] = collect_sqlite_store_stats(
                live_database_url,
                archive_database_url=resolved_database_url,
            )

    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    typer.echo(f"Database: {payload['db_path']}")
    typer.echo(f"  db_bytes: {payload['db_bytes']}")
    typer.echo(f"  wal_bytes: {payload['wal_bytes']}")
    typer.echo(f"  disk_free_bytes: {payload['disk_free_bytes']}")
    typer.echo(f"  backup_bytes: {payload['backup_bytes']}")
    typer.echo(f"  page_count: {payload.get('db_page_count')}")
    typer.echo(f"  freelist_count: {payload.get('db_freelist_count')}")
    if payload["deep_counts_skipped"]:
        typer.echo("  deep_counts: skipped (use --deep for explicit COUNT diagnostics)")
    if payload["table_bytes_skipped"]:
        typer.echo("  table_bytes: skipped (use --table-bytes to walk dbstat pages)")
    elif payload["table_bytes"]["available"]:
        typer.echo("  top_tables:")
        for table_name, table_payload in list(payload["table_bytes"]["tables"].items())[:8]:
            typer.echo(f"    {table_name}: {table_payload['bytes']}")
    else:
        typer.echo(f"  table_bytes: unavailable ({payload['table_bytes']['error']})")
    cache = payload["table_bytes_cache"]
    if cache["exists"]:
        age = cache["age_seconds"]
        age_text = f"{age}s ago" if age is not None else "unknown age"
        fresh_text = "fresh" if cache["fresh"] else "stale"
        typer.echo(f"  table_bytes_cache: {cache['status']} ({fresh_text}, sampled {age_text})")
        for row in cache["top_tables"][:3]:
            typer.echo(f"    cached {row['table']}: {row['bytes']}")
    else:
        suggestion = f", run {cache['suggested_command']}" if cache.get("suggested_command") else ""
        typer.echo(f"  table_bytes_cache: {cache['status']}{suggestion}")
    live_store = payload["live_store"]
    typer.echo(f"  live_store: {live_store['status']}")
    if live_store.get("db_path"):
        typer.echo(f"    path: {live_store['db_path']}")
        typer.echo(f"    db_bytes: {live_store.get('db_bytes')}")
    live_outbox = live_store.get("live_archive_outbox") or {}
    if live_outbox.get("checked") and live_outbox.get("table_exists"):
        typer.echo(
            "    outbox: "
            f"{live_outbox.get('pending_count')} pending, "
            f"{live_outbox.get('failed_count')} failed, "
            f"max_attempts={live_outbox.get('max_attempts')}"
        )
    if live_store.get("warnings"):
        typer.echo(f"    warnings: {', '.join(live_store['warnings'])}")


@db_app.command(name="sample-table-bytes")
def db_sample_table_bytes(
    database_url: str | None = typer.Option(
        None,
        "--database-url",
        help="SQLite DATABASE_URL override (defaults to env).",
    ),
    output_path: Path | None = typer.Option(
        None,
        "--output",
        help="Override table-byte cache artifact path.",
    ),
    timeout_seconds: int = typer.Option(
        300,
        "--timeout-seconds",
        min=1,
        help="Abort the dbstat page walk after this many seconds.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Sample SQLite physical table/index byte usage into a cache artifact."""
    from zerg.services.db_diagnostics import sample_sqlite_table_bytes_to_cache
    from zerg.services.db_diagnostics import sqlite_table_bytes_cache_path

    resolved_database_url = _resolve_db_url(database_url)
    try:
        payload = sample_sqlite_table_bytes_to_cache(
            resolved_database_url,
            output_path=output_path,
            timeout_seconds=timeout_seconds,
        )
    except ValueError as exc:
        if json_output:
            typer.echo(json.dumps({"status": "error", "error": str(exc)}, indent=2, sort_keys=True))
        else:
            typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    cache_path = sqlite_table_bytes_cache_path(resolved_database_url, output_path)
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        status = payload["status"]
        elapsed_ms = payload["elapsed_ms"]
        typer.echo(f"SQLite table-byte sample {status} elapsed_ms={elapsed_ms} output={cache_path}")
        if payload.get("error"):
            typer.echo(f"  error: {payload['error']}", err=True)
    if payload["status"] != "ok":
        raise typer.Exit(code=1)


@db_app.command(name="drain-live-archive-outbox")
def db_drain_live_archive_outbox(
    database_url: str | None = typer.Option(
        None,
        "--database-url",
        help="SQLite archive DATABASE_URL override (defaults to env).",
    ),
    live_database_url: str | None = typer.Option(
        None,
        "--live-database-url",
        help="Live Store SQLite URL override (defaults to LONGHOUSE_LIVE_* env).",
    ),
    limit: int = typer.Option(100, "--limit", min=1, help="Maximum undrained outbox rows to process."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Drain Live Store outbox rows into the archive SQLite database."""

    from zerg.database import make_live_engine
    from zerg.database import make_sessionmaker
    from zerg.services.archive_worker_status import archive_worker_enabled
    from zerg.services.live_archive_outbox import drain_live_archive_outbox

    archive_engine, resolved_database_url = _resolve_db_engine(database_url)
    if live_database_url is None:
        from zerg.config import get_settings_unchecked

        live_database_url = get_settings_unchecked().live_database_url
    if not live_database_url:
        payload = {
            "status": "disabled",
            "database_url": resolved_database_url,
            "live_database_url": None,
            "processed": 0,
            "drained": 0,
            "failed": 0,
        }
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
            return
        typer.echo("Live Store is not configured.", err=True)
        raise typer.Exit(code=2)

    live_engine = make_live_engine(live_database_url)
    ArchiveSession = make_sessionmaker(archive_engine)
    LiveSession = make_sessionmaker(live_engine)
    excluded_kinds = None
    if archive_worker_enabled():
        from zerg.services.archive_worker import worker_owned_outbox_kinds

        excluded_kinds = worker_owned_outbox_kinds()
    with LiveSession() as live_db, ArchiveSession() as archive_db:
        result = drain_live_archive_outbox(
            live_db,
            archive_db,
            limit=limit,
            exclude_kinds=excluded_kinds,
        )

    payload = {
        "status": "ok" if result.failed == 0 else "partial",
        "database_url": resolved_database_url,
        "live_database_url": live_database_url,
        **result.as_dict(),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(
        "Live archive outbox drain " f"{payload['status']} processed={result.processed} drained={result.drained} failed={result.failed}"
    )
    if result.failed:
        raise typer.Exit(code=1)


@db_app.command(name="optimize")
def db_optimize(
    database_url: str | None = typer.Option(
        None,
        "--database-url",
        help="SQLite DATABASE_URL override (defaults to env).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Run explicit SQLite PRAGMA optimize maintenance."""
    from zerg.services.db_diagnostics import collect_sqlite_schema_stats

    engine, resolved_database_url = _resolve_db_engine(database_url)
    started = time.monotonic()
    try:
        with engine.begin() as conn:
            before = collect_sqlite_schema_stats(conn)
            result = conn.exec_driver_sql("PRAGMA optimize")
            try:
                result_rows = [list(row) for row in result.fetchall()]
            except Exception:
                result_rows = []
            after = collect_sqlite_schema_stats(conn)
    except Exception as exc:
        payload = {
            "status": "error",
            "database_url": resolved_database_url,
            "pragma": "PRAGMA optimize",
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "error": str(exc),
        }
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(f"SQLite optimize failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    payload = {
        "status": "ok",
        "database_url": resolved_database_url,
        "pragma": "PRAGMA optimize",
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        "result_rows": result_rows,
        "before": before,
        "after": after,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        typer.echo(f"SQLite optimize complete elapsed_ms={payload['elapsed_ms']}")


@db_app.command(name="detect-provider-duplicates")
def db_detect_provider_duplicates(
    database_url: str | None = typer.Option(
        None,
        "--database-url",
        help="SQLite DATABASE_URL override (defaults to env).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Detect duplicate sessions sharing one provider-native id (read-only).

    Pure SELECT — no writes (read locks only). Safe to run against the live
    corpus. The destructive merge is intentionally not implemented; this only
    reports split rows backed by a recorded provider_binding_conflict
    observation (pre-instrumentation duplicates are not covered).
    """
    from zerg.database import make_sessionmaker
    from zerg.services.agents.provider_binding_cleanup import detect_duplicate_sessions_by_provider_binding

    engine, resolved_database_url = _resolve_db_engine(database_url)
    SessionLocal = make_sessionmaker(engine)
    db = SessionLocal()
    try:
        groups = detect_duplicate_sessions_by_provider_binding(db)
    finally:
        db.close()

    payload = {
        "status": "ok",
        "database_url": resolved_database_url,
        "discovery": "provider_binding_conflict observations only",
        "coverage_note": (
            "Detects duplicates backed by a recorded conflict observation. "
            "Duplicates created before conflict-recording shipped are NOT visible here."
        ),
        "duplicate_group_count": len(groups),
        "groups": [group.to_dict() for group in groups],
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    if not groups:
        typer.echo("No conflict-observation-backed duplicate provider-session bindings detected.")
        typer.echo("  (Pre-instrumentation duplicates without a conflict observation are not covered.)")
        return
    typer.echo(f"Found {len(groups)} provider-native id(s) mapping to multiple sessions:")
    for group in groups:
        typer.echo(f"  {group.provider} {group.provider_session_id}")
        typer.echo(f"    sessions: {', '.join(group.session_ids)}")
        typer.echo(f"    threads:  {', '.join(group.thread_ids)}")
        typer.echo(f"    evidence: {group.evidence}")


@db_app.command(name="classify-automation")
def db_classify_automation(
    database_url: str | None = typer.Option(
        None,
        "--database-url",
        help="SQLite DATABASE_URL override (defaults to env).",
    ),
    session_ids: list[str] | None = typer.Option(
        None,
        "--session-id",
        help="Reviewed session id to mark hidden by --origin-kind. Repeat for multiple ids.",
    ),
    origin_kind: str = typer.Option(
        "hatch_automation",
        "--origin-kind",
        help="Reviewed hidden origin kind to apply: hatch_automation or test_or_canary.",
    ),
    apply_changes: bool = typer.Option(
        False,
        "--apply",
        help="Apply reviewed --session-id classifications. Heuristic candidates remain report-only.",
    ),
    candidate_limit: int = typer.Option(
        100,
        "--candidate-limit",
        min=1,
        max=500,
        help="Maximum report-only heuristic candidates to include.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Report Hatch-shaped rows and optionally hide reviewed hidden ids."""
    from zerg.database import make_sessionmaker
    from zerg.services.agents.automation_backfill import REVIEWABLE_HIDDEN_ORIGIN_KINDS
    from zerg.services.agents.automation_backfill import classify_reviewed_hatch_automation_sessions

    if apply_changes and not session_ids:
        typer.echo("--apply requires at least one reviewed --session-id.", err=True)
        raise typer.Exit(code=2)
    normalized_origin_kind = str(origin_kind or "").strip().lower().replace("-", "_")
    if normalized_origin_kind not in REVIEWABLE_HIDDEN_ORIGIN_KINDS:
        typer.echo("--origin-kind must be hatch_automation or test_or_canary.", err=True)
        raise typer.Exit(code=2)

    engine, resolved_database_url = _resolve_db_engine(database_url)
    SessionLocal = make_sessionmaker(engine)
    db = SessionLocal()
    try:
        result = classify_reviewed_hatch_automation_sessions(
            db,
            session_ids=session_ids or [],
            apply=apply_changes,
            origin_kind=normalized_origin_kind,
            candidate_limit=candidate_limit,
        )
    finally:
        db.close()

    payload = {
        "status": "ok",
        "database_url": resolved_database_url,
        "mode": "apply_reviewed_ids" if apply_changes else "dry_run_report",
        "origin_kind": normalized_origin_kind,
        "note": "Heuristic Hatch candidates are report-only; only explicit reviewed --session-id rows are applied.",
        **result.to_dict(),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    if apply_changes:
        typer.echo(f"Applied {normalized_origin_kind} origin to {len(result.applied_session_ids)} reviewed session(s).")
    else:
        typer.echo("Dry run only. Pass --apply with reviewed --session-id values to hide rows.")
    if result.missing_session_ids:
        typer.echo(f"Missing session ids: {', '.join(result.missing_session_ids)}")
    if result.already_marked_session_ids:
        typer.echo(f"Already marked: {', '.join(result.already_marked_session_ids)}")
    typer.echo(f"Heuristic candidates (report-only): {len(result.heuristic_candidates)}")
    for candidate in result.heuristic_candidates[:10]:
        typer.echo(f"  {candidate['session_id']} {candidate['provider']} {candidate['prompt_preview'][:100]}")


app.add_typer(messages_app, name="messages", help="Durable session inbox commands")
app.add_typer(sessions_app, name="sessions", help="Session inspection commands")
app.add_typer(config_app, name="config", help="Configuration management")
app.add_typer(db_app, name="db", help="SQLite database diagnostics and maintenance")
app.add_typer(claude_channel_app, name="claude-channel", help="Claude channel bridge commands", hidden=True)
app.add_typer(opencode_bridge_app, name="opencode-bridge", help="OpenCode bridge commands", hidden=True)
app.add_typer(opencode_channel_app, name="opencode-channel", help="OpenCode server bridge commands", hidden=True)
app.add_typer(antigravity_channel_app, name="antigravity-channel", help="Antigravity hook-inbox commands", hidden=True)
app.add_typer(codex_app, name="codex")
app.add_typer(cursor_app, name="cursor")
app.add_typer(local_health_app, name="local-health")
app.add_typer(machine_app, name="machine", help="Machine runtime repair and reconciliation")
app.add_typer(archive_app, name="archive", help="Archive backlog inspection and control")
app.add_typer(provider_live_app, name="provider-live", help="Managed-provider live proof canaries")

for command in (serve, status, claude, wall, peers, message, tail, auth, ship, recall):
    app.command()(command)

app.command(name="hash-password")(hash_password)

app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})(opencode)
app.command(name="agy", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})(antigravity)
app.command(
    name="antigravity",
    hidden=True,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)(antigravity)
app.command(name="continue")(continue_session)
app.command(name="connect")(connect_command)
app.command(name="version")(version_command)
app.command(name="upgrade")(upgrade_command)
app.command(hidden=True, name="record-install")(record_install_command)
app.command(hidden=True, name="runtime-artifact-install")(runtime_artifact_install_command)
app.command(hidden=True, name="runtime-artifact-smoke")(runtime_artifact_smoke_command)
app.command(hidden=True, name="apns-smoke")(apns_smoke_command)


@app.command()
def migrate(
    database_url: str | None = typer.Option(
        None,
        "--database-url",
        help="SQLite DATABASE_URL override (defaults to env).",
    ),
    apply: bool = typer.Option(False, "--apply", help="Apply pending heavy migrations."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    schema_converge: bool = typer.Option(
        True,
        "--schema-converge/--no-schema-converge",
        help="Run lightweight startup schema convergence before planning heavy migrations.",
    ),
) -> None:
    """Plan or apply explicit heavy SQLite migrations."""
    from zerg.database import initialize_database
    from zerg.database import make_engine
    from zerg.db_migrations import apply_heavy_migrations
    from zerg.db_migrations import ensure_migration_ledger
    from zerg.db_migrations import plan_heavy_migrations

    engine = make_engine(database_url) if database_url else None
    if schema_converge:
        if not json_output:
            typer.echo("Running lightweight schema convergence before heavy migration plan...")
        initialize_database(engine)
    target_engine = engine

    if target_engine is None:
        from zerg.database import default_engine

        target_engine = default_engine
    if target_engine is None:
        raise typer.Exit(code=2)

    ensure_migration_ledger(target_engine)
    plan_before = plan_heavy_migrations(target_engine)
    pending_before = [item.name for item in plan_before if item.pending]

    run_items = []
    if apply:
        run_items = apply_heavy_migrations(target_engine)

    plan_after = plan_heavy_migrations(target_engine)
    pending_after = [item.name for item in plan_after if item.pending]

    payload = {
        "schema_converged": schema_converge,
        "pending_before": pending_before,
        "pending_after": pending_after,
        "plan": [
            {
                "name": item.name,
                "description": item.description,
                "pending": item.pending,
                "reason": item.reason,
                "last_status": item.last_status,
            }
            for item in plan_after
        ],
        "applied": [{"name": item.name, "status": item.status, "details": item.details} for item in run_items],
    }

    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        if pending_before:
            typer.echo(f"Pending heavy migrations: {', '.join(pending_before)}")
        else:
            typer.echo("No heavy migrations pending.")
        if run_items:
            for item in run_items:
                details = f" ({item.details})" if item.details else ""
                typer.echo(f"- {item.name}: {item.status}{details}")
        if apply and pending_after:
            typer.echo(f"Still pending: {', '.join(pending_after)}")

    if apply and pending_after:
        raise typer.Exit(code=1)


@app.command(hidden=True, name="rebuild-session")
def rebuild_session(
    session_id: str = typer.Argument(..., help="Session UUID to rebuild from SessionObservation."),
    runtime_key: str | None = typer.Option(
        None,
        "--runtime-key",
        help="Optional runtime key to include in the rebuild scope.",
    ),
    database_url: str | None = typer.Option(
        None,
        "--database-url",
        help="SQLite DATABASE_URL override (defaults to env).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Rebuild one session's projections; reducer errors are reported after committing partial output."""
    from uuid import UUID

    from zerg import database as database_module
    from zerg.database import initialize_database
    from zerg.database import make_engine
    from zerg.database import make_sessionmaker
    from zerg.services.session_observation_rebuild import SessionObservationRebuildCoverageError
    from zerg.services.session_observation_rebuild import rebuild_session_observation_projections

    try:
        parsed_session_id = UUID(str(session_id))
    except ValueError as exc:
        raise typer.BadParameter("session_id must be a UUID") from exc

    if database_url:
        engine = make_engine(database_url)
        initialize_database(engine)
        session_factory = make_sessionmaker(engine)
    else:
        initialize_database()
        session_factory = database_module.default_session_factory
    if session_factory is None:
        raise typer.Exit(code=2)

    with session_factory() as db:
        try:
            result = rebuild_session_observation_projections(db, session_id=parsed_session_id, runtime_key=runtime_key)
        except SessionObservationRebuildCoverageError as exc:
            db.rollback()
            if json_output:
                typer.echo(json.dumps({"error": "coverage_gap", "detail": str(exc)}, indent=2, sort_keys=True))
            else:
                typer.echo(f"Refusing rebuild: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        db.commit()

    payload = {
        "session_id": str(result.session_id) if result.session_id else None,
        "runtime_key": result.runtime_key,
        "observations_seen": result.observations_seen,
        "newest_observation_db_id": result.newest_observation_db_id,
        "provider_events_reduced": result.provider_events_reduced,
        "bridge_events_reduced": result.bridge_events_reduced,
        "source_lines_reduced": result.source_lines_reduced,
        "runtime_signals_reduced": result.runtime_signals_reduced,
        "skipped_observations": result.skipped_observations,
        "reducer_errors": [error.__dict__ for error in result.reducer_errors],
        "agent_events": result.agent_events,
        "source_lines": result.source_lines,
        "runtime_states": result.runtime_states,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        typer.echo(
            "Rebuilt session "
            f"{parsed_session_id}: observations={result.observations_seen}, "
            f"events={result.agent_events}, source_lines={result.source_lines}, "
            f"runtime_states={result.runtime_states}, errors={len(result.reducer_errors)}"
        )
    if result.reducer_errors:
        raise typer.Exit(code=1)


for command in (onboard, doctor):
    app.command()(command)

app.command(hidden=True)(mcp_server)


def main():
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
