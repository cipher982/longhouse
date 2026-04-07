"""Main CLI entry point for Longhouse."""

import json
from importlib import metadata

import typer

import zerg.bootstrap_sqlite  # noqa: F401
from zerg.cli.claude import claude
from zerg.cli.claude_channel import app as claude_channel_app
from zerg.cli.codex import codex
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
from zerg.cli.mcp_serve import mcp_server
from zerg.cli.onboard import onboard
from zerg.cli.serve import serve
from zerg.cli.serve import status
from zerg.cli.sessions import app as sessions_app
from zerg.cli.sessions import continue_session
from zerg.cli.update_manager import record_install_command
from zerg.cli.update_manager import upgrade_command
from zerg.cli.update_manager import version_command

app = typer.Typer(
    name="longhouse",
    help="Longhouse AI Agent Platform CLI",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if not value:
        return
    typer.echo(f"longhouse {metadata.version('longhouse')}")
    raise typer.Exit()


@app.callback()
def app_callback(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show Longhouse version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Longhouse AI Agent Platform CLI."""


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

for command in (serve, status, claude, codex, wall, peers, message, tail, auth, ship, recall):
    app.command()(command)

app.command(name="continue")(continue_session)
app.command(name="connect")(connect_command)
app.command(name="version")(version_command)
app.command(name="upgrade")(upgrade_command)
app.command(hidden=True, name="record-install")(record_install_command)


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


for command in (onboard, doctor):
    app.command()(command)

app.command(hidden=True)(mcp_server)


def main():
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
