"""Serve command for starting the Zerg server.

Commands:
- serve: Start the Zerg server with uvicorn
- status: Show current server configuration and lite_mode status
"""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import typer

app = typer.Typer(help="Zerg server commands")


def _get_zerg_home() -> Path:
    """Return the Zerg home directory (~/.zerg), creating if needed."""
    zerg_home = Path.home() / ".zerg"
    zerg_home.mkdir(parents=True, exist_ok=True)
    return zerg_home


def _get_default_db_path() -> Path:
    """Return the default SQLite database path."""
    return _get_zerg_home() / "zerg.db"


def _get_or_create_fernet_secret() -> str:
    """Get or create a persistent FERNET_SECRET for lite mode.

    Stores the secret in ~/.zerg/fernet.key for persistence across restarts.
    """
    secret_file = _get_zerg_home() / "fernet.key"

    if secret_file.exists():
        return secret_file.read_text().strip()

    # Generate a new Fernet-compatible key (32 bytes, URL-safe base64)
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    secret_file.write_text(key)
    secret_file.chmod(0o600)  # Owner read/write only
    return key


def _mask_db_url(url: str) -> str:
    """Mask sensitive parts of a database URL for display."""
    if not url or url.startswith("sqlite"):
        return url

    try:
        parsed = urlparse(url)
        if parsed.password:
            # Mask password in URL
            masked = url.replace(f":{parsed.password}@", ":****@")
            return masked
        return url
    except Exception:
        # If parsing fails, just show the host part if @ is present
        if "@" in url:
            return f"...@{url.split('@')[-1]}"
        return url


def _apply_lite_mode_defaults() -> None:
    """Apply default environment variables for lite (SQLite) mode.

    Sets up sensible defaults for zero-config OSS startup:
    - SQLite database in ~/.zerg/zerg.db
    - Auth disabled (single-user local install)
    - Single-tenant mode
    - Auto-generated FERNET_SECRET (persisted)
    """
    # Database URL
    if not os.environ.get("DATABASE_URL"):
        default_db = _get_default_db_path()
        os.environ["DATABASE_URL"] = f"sqlite:///{default_db}"

    # Auth disabled for local use
    if "AUTH_DISABLED" not in os.environ:
        os.environ["AUTH_DISABLED"] = "1"

    # Single-tenant mode
    if "SINGLE_TENANT" not in os.environ:
        os.environ["SINGLE_TENANT"] = "1"

    # FERNET_SECRET (required by crypto module)
    if not os.environ.get("FERNET_SECRET"):
        os.environ["FERNET_SECRET"] = _get_or_create_fernet_secret()


@app.command()
def serve(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Host to bind to",
    ),
    port: int = typer.Option(
        8080,
        "--port",
        "-p",
        help="Port to bind to",
    ),
    reload: bool = typer.Option(
        False,
        "--reload",
        "-r",
        help="Enable auto-reload (dev mode)",
    ),
    db: Optional[str] = typer.Option(
        None,
        "--db",
        "-d",
        help="Database URL (default: sqlite:///~/.zerg/zerg.db)",
    ),
    workers: int = typer.Option(
        1,
        "--workers",
        "-w",
        help="Number of worker processes",
    ),
) -> None:
    """Start the Zerg server.

    By default, uses SQLite for zero-config startup. For production,
    configure a Postgres DATABASE_URL in your environment.

    Examples:
        zerg serve                           # SQLite on localhost:8080
        zerg serve --host 0.0.0.0 --port 80  # Bind to all interfaces
        zerg serve --db postgresql://...     # Use Postgres
        zerg serve --reload                  # Dev mode with auto-reload
    """
    import uvicorn

    # Set database URL if explicitly provided
    if db:
        os.environ["DATABASE_URL"] = db

    # Apply lite mode defaults (SQLite, auth disabled, etc.)
    _apply_lite_mode_defaults()

    db_url = os.environ["DATABASE_URL"]
    is_sqlite = db_url.startswith("sqlite")

    # Safety checks
    is_public_bind = host in ("0.0.0.0", "::", "")
    auth_disabled = os.environ.get("AUTH_DISABLED", "").lower() in ("1", "true", "yes")

    # Warn about public bind with auth disabled
    if is_public_bind and auth_disabled:
        typer.secho(
            "WARNING: Binding to public interface with auth disabled!",
            fg=typer.colors.YELLOW,
        )
        typer.secho(
            "  Set AUTH_DISABLED=0 and configure OAuth for production use.",
            fg=typer.colors.YELLOW,
        )
        typer.echo("")

    # Prevent SQLite with multiple workers
    if is_sqlite and workers > 1:
        typer.secho(
            "ERROR: SQLite does not support multiple workers.",
            fg=typer.colors.RED,
        )
        typer.echo("  Use --workers 1 with SQLite, or switch to Postgres.")
        raise typer.Exit(code=1)

    # Check for bundled frontend
    from zerg.main import FRONTEND_DIST_DIR
    from zerg.main import FRONTEND_SOURCE

    has_frontend = FRONTEND_DIST_DIR is not None
    frontend_source = FRONTEND_SOURCE

    typer.echo("Starting Zerg server...")
    typer.echo(f"  Host: {host}")
    typer.echo(f"  Port: {port}")
    typer.echo(f"  Database: {_mask_db_url(db_url)}")
    typer.echo(f"  Mode: {'lite (SQLite)' if is_sqlite else 'full (Postgres)'}")
    typer.echo(f"  Frontend: {frontend_source}")
    if reload:
        typer.echo("  Reload: enabled")
    typer.echo("")

    if has_frontend:
        typer.secho(f"  UI available at: http://{host}:{port}/", fg=typer.colors.GREEN)
        typer.echo("")

    uvicorn.run(
        "zerg.main:app",
        host=host,
        port=port,
        reload=reload,
        workers=workers if not reload else 1,
        log_level="info",
    )


@app.command()
def status(
    db: Optional[str] = typer.Option(
        None,
        "--db",
        "-d",
        help="Database URL to check (uses defaults if not set)",
    ),
) -> None:
    """Show Zerg configuration and status.

    Displays the current database configuration, lite_mode status,
    and other relevant settings without starting the server.

    Shows the *effective* configuration that `zerg serve` would use,
    including defaults applied for lite mode.
    """
    # Apply the same defaults as serve would
    if db:
        os.environ["DATABASE_URL"] = db
    _apply_lite_mode_defaults()

    # Now import settings with defaults applied
    # Use a minimal validation approach - catch errors but don't skip validation
    try:
        from zerg.config import get_settings

        settings = get_settings()
    except RuntimeError as e:
        # Show what's missing without crashing
        typer.secho("Configuration Issues:", fg=typer.colors.RED)
        typer.echo(f"  {e}")
        typer.echo("")
        typer.echo("Run 'zerg serve' to auto-configure defaults,")
        typer.echo("or set the missing environment variables.")
        raise typer.Exit(code=1)
    except Exception as e:
        typer.secho(f"Configuration error: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    # Database info
    db_url = settings.database_url or "(not set)"
    is_sqlite = settings.db_is_sqlite
    lite_mode = settings.lite_mode

    typer.echo("Zerg Configuration (effective)")
    typer.echo("=" * 40)
    typer.echo("")

    # Database
    typer.echo("Database:")
    typer.echo(f"  URL: {_mask_db_url(db_url)}")
    if is_sqlite:
        typer.secho("  Mode: lite (SQLite)", fg=typer.colors.CYAN)
    else:
        typer.secho("  Mode: full (Postgres)", fg=typer.colors.GREEN)

    # Check if DB file exists (SQLite only)
    if is_sqlite and db_url.startswith("sqlite:///"):
        db_path = Path(db_url.replace("sqlite:///", ""))
        if db_path.exists():
            size_mb = db_path.stat().st_size / (1024 * 1024)
            typer.echo(f"  File: {db_path} ({size_mb:.2f} MB)")
        else:
            typer.echo(f"  File: {db_path} (will be created)")

    typer.echo("")

    # Features
    typer.echo("Features:")
    typer.echo(f"  Auth: {'disabled' if settings.auth_disabled else 'enabled'}")
    typer.echo(f"  Single tenant: {'yes' if settings.single_tenant else 'no'}")
    typer.echo(f"  LLM available: {'yes' if settings.llm_available else 'no (set OPENAI_API_KEY)'}")
    typer.echo(f"  Job queue: {'enabled' if settings.job_queue_enabled else 'disabled'}")

    typer.echo("")

    # Secrets status (don't show values)
    typer.echo("Secrets:")
    fernet_set = bool(settings.fernet_secret)
    jwt_set = settings.jwt_secret not in ("", "dev-secret")
    typer.echo(f"  FERNET_SECRET: {'set' if fernet_set else 'not set'}")
    typer.echo(f"  JWT_SECRET: {'set' if jwt_set else 'using default (dev only)'}")

    typer.echo("")

    # Paths (only show if they exist to avoid side effects)
    typer.echo("Paths:")
    zerg_home = _get_zerg_home()
    typer.echo(f"  Config: {zerg_home}")
    typer.echo(f"  Workspace: {settings.oikos_workspace_path}")

    typer.echo("")

    # Frontend status
    typer.echo("Frontend:")
    try:
        from zerg.main import FRONTEND_DIST_DIR
        from zerg.main import FRONTEND_SOURCE

        if FRONTEND_DIST_DIR is not None:
            if FRONTEND_SOURCE == "bundled":
                typer.secho("  Source: bundled (pip install)", fg=typer.colors.GREEN)
            elif FRONTEND_SOURCE == "docker":
                typer.secho("  Source: docker", fg=typer.colors.GREEN)
            else:
                typer.secho("  Source: local (development)", fg=typer.colors.CYAN)
            typer.echo(f"  Path: {FRONTEND_DIST_DIR}")
        else:
            typer.secho("  Source: not found", fg=typer.colors.YELLOW)
            typer.echo("  (Build frontend or install from pip)")
    except ImportError:
        typer.secho("  Source: unknown (import error)", fg=typer.colors.YELLOW)

    typer.echo("")

    # SQLite limitations in lite mode
    if lite_mode:
        typer.echo("Lite Mode Limitations:")
        typer.echo("  - Single-process only (no FOR UPDATE SKIP LOCKED)")
        typer.echo("  - Memory checkpoints (state lost on restart)")
        typer.echo("  - No parallel job claiming")
        typer.secho("  Run with Postgres for full functionality", fg=typer.colors.YELLOW)


# Export for main.py
__all__ = ["app", "serve", "status"]
