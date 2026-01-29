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

# Also add ship and connect as top-level commands for convenience
app.command(name="ship")(connect_app.registered_commands[0].callback)
app.command(name="connect")(connect_app.registered_commands[1].callback)


def main():
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
