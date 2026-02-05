"""Main CLI entry point for Longhouse.

Usage:
    longhouse serve         # Start the server
    longhouse status        # Show configuration
    longhouse config show   # Show effective configuration
    longhouse ship          # One-shot sync
    longhouse connect       # Continuous polling
    longhouse onboard       # Run onboarding wizard
    longhouse doctor        # Self-diagnosis
    longhouse --help        # Show help
"""

import typer

from zerg.cli.connect import app as connect_app
from zerg.cli.doctor import doctor
from zerg.cli.onboard import onboard
from zerg.cli.serve import app as serve_app
from zerg.cli.serve import serve
from zerg.cli.serve import status

# Create main app
app = typer.Typer(
    name="longhouse",
    help="Longhouse AI Agent Platform CLI",
    no_args_is_help=True,
)

# Config subcommand group
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


# Add subcommands from connect module
app.add_typer(connect_app, name="session", help="Session shipping commands")
app.add_typer(serve_app, name="server", help="Server management commands")
app.add_typer(config_app, name="config", help="Configuration management")

# Top-level commands for quick access
# Server commands (primary use case)
app.command(name="serve")(serve)
app.command(name="status")(status)

# Session shipping commands (convenience aliases)
# Find commands by callback function name to avoid index-position bugs
_cmd_lookup = {cmd.callback.__name__: cmd.callback for cmd in connect_app.registered_commands}
app.command(name="auth")(_cmd_lookup["auth"])
app.command(name="ship")(_cmd_lookup["ship"])
app.command(name="connect")(_cmd_lookup["connect"])

# Onboarding wizard
app.command(name="onboard")(onboard)

# Self-diagnosis
app.command(name="doctor")(doctor)


def main():
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
