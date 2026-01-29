"""Main CLI entry point for Zerg.

Usage:
    zerg ship          # One-shot sync
    zerg connect       # Continuous polling
    zerg --help        # Show help
"""

import typer

from zerg.cli.connect import app as connect_app

# Create main app
app = typer.Typer(
    name="zerg",
    help="Zerg AI Agent Platform CLI",
    no_args_is_help=True,
)

# Add subcommands from connect module
app.add_typer(connect_app, name="session", help="Session shipping commands")

# Also add ship, connect, and auth as top-level commands for convenience
# Find commands by callback function name to avoid index-position bugs
_cmd_lookup = {cmd.callback.__name__: cmd.callback for cmd in connect_app.registered_commands}
app.command(name="auth")(_cmd_lookup["auth"])
app.command(name="ship")(_cmd_lookup["ship"])
app.command(name="connect")(_cmd_lookup["connect"])


def main():
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
