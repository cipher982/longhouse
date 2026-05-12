"""Main CLI entry point for Longhouse."""

import json
import sys

import typer

import zerg.bootstrap_sqlite  # noqa: F401
from zerg.build_info import BuildIdentityMissing
from zerg.build_info import load as load_build_identity
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
from zerg.cli.doctor import doctor
from zerg.cli.local_health import app as local_health_app
from zerg.cli.machine import app as machine_app
from zerg.cli.mcp_serve import mcp_server
from zerg.cli.onboard import onboard
from zerg.cli.opencode import opencode
from zerg.cli.runtime_artifact_smoke import runtime_artifact_install_command
from zerg.cli.runtime_artifact_smoke import runtime_artifact_smoke_command
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


app.add_typer(messages_app, name="messages", help="Durable session inbox commands")
app.add_typer(sessions_app, name="sessions", help="Session inspection commands")
app.add_typer(config_app, name="config", help="Configuration management")
app.add_typer(claude_channel_app, name="claude-channel", help="Claude channel bridge commands", hidden=True)
app.add_typer(codex_app, name="codex")
app.add_typer(local_health_app, name="local-health")
app.add_typer(machine_app, name="machine", help="Machine runtime repair and reconciliation")

for command in (serve, status, claude, wall, peers, message, tail, auth, ship, recall):
    app.command()(command)

app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})(opencode)
app.command(name="continue")(continue_session)
app.command(name="connect")(connect_command)
app.command(name="version")(version_command)
app.command(name="upgrade")(upgrade_command)
app.command(hidden=True, name="record-install")(record_install_command)
app.command(hidden=True, name="runtime-artifact-install")(runtime_artifact_install_command)
app.command(hidden=True, name="runtime-artifact-smoke")(runtime_artifact_smoke_command)


@app.command()
def migrate(
    database_url: str | None = typer.Option(
        None,
        "--database-url",
        help="SQLite DATABASE_URL override (defaults to env).",
    ),
    apply: bool = typer.Option(False, "--apply", help="Apply pending heavy migrations."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Plan or apply explicit heavy SQLite migrations."""
    from zerg.database import initialize_database
    from zerg.database import make_engine
    from zerg.db_migrations import apply_heavy_migrations
    from zerg.db_migrations import plan_heavy_migrations

    engine = make_engine(database_url) if database_url else None
    initialize_database(engine)
    target_engine = engine

    if target_engine is None:
        from zerg.database import default_engine

        target_engine = default_engine
    if target_engine is None:
        raise typer.Exit(code=2)

    plan_before = plan_heavy_migrations(target_engine)
    pending_before = [item.name for item in plan_before if item.pending]

    run_items = []
    if apply:
        run_items = apply_heavy_migrations(target_engine)

    plan_after = plan_heavy_migrations(target_engine)
    pending_after = [item.name for item in plan_after if item.pending]

    payload = {
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
    runtime_key: str | None = typer.Option(None, "--runtime-key", help="Optional runtime key to include in the rebuild scope."),
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
        result = rebuild_session_observation_projections(db, session_id=parsed_session_id, runtime_key=runtime_key)
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
