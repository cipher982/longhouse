"""Serve command for starting the Longhouse server.

Commands:
- serve: Start the Longhouse server with uvicorn
- serve --daemon: Start as background daemon
- serve --stop: Stop running daemon
- status: Show current server configuration and lite_mode status
"""

from __future__ import annotations

import base64
import os
import secrets
import signal
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import typer

app = typer.Typer(help="Longhouse server commands")


def _get_longhouse_home() -> Path:
    """Return the Longhouse home directory (~/.longhouse), creating if needed."""
    longhouse_home = Path.home() / ".longhouse"
    longhouse_home.mkdir(parents=True, exist_ok=True)
    return longhouse_home


def _get_default_db_path() -> Path:
    """Return the default SQLite database path."""
    return _get_longhouse_home() / "longhouse.db"


def _get_or_create_fernet_secret() -> str:
    """Get or create a persistent FERNET_SECRET for lite mode.

    Stores the secret in ~/.longhouse/fernet.key for persistence across restarts.
    Uses secure file creation to avoid permission race conditions.
    """
    secret_file = _get_longhouse_home() / "fernet.key"

    if secret_file.exists():
        return secret_file.read_text().strip()

    # Generate a new Fernet-compatible key (32 bytes, URL-safe base64)
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()

    # Secure file creation: open with O_CREAT|O_EXCL and 0600 permissions
    # This avoids the race condition of write-then-chmod
    fd = os.open(str(secret_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key.encode())
    finally:
        os.close(fd)

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
    - SQLite database in ~/.longhouse/longhouse.db
    - Auth disabled (single-user local install)
    - Single-tenant mode
    - Auto-generated secrets (FERNET_SECRET, TRIGGER_SIGNING_SECRET)
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

    # TRIGGER_SIGNING_SECRET (required for webhook triggers)
    if not os.environ.get("TRIGGER_SIGNING_SECRET"):
        os.environ["TRIGGER_SIGNING_SECRET"] = secrets.token_hex(32)


def _get_pid_file() -> Path:
    """Get the path to the server PID file."""
    return _get_longhouse_home() / "server.pid"


def _get_log_file() -> Path:
    """Get the path to the server log file."""
    return _get_longhouse_home() / "server.log"


def _is_server_running() -> tuple[bool, int | None]:
    """Check if server is already running.

    Returns:
        Tuple of (is_running, pid)
    """
    pid_file = _get_pid_file()
    if not pid_file.exists():
        return False, None

    try:
        pid = int(pid_file.read_text().strip())
        # Check if process exists
        os.kill(pid, 0)
        return True, pid
    except (ValueError, OSError, ProcessLookupError):
        # PID file exists but process is gone
        pid_file.unlink(missing_ok=True)
        return False, None


def _daemonize() -> None:
    """Fork into background daemon process (Unix only)."""
    # First fork
    try:
        pid = os.fork()
        if pid > 0:
            # Parent exits
            sys.exit(0)
    except OSError as e:
        raise RuntimeError(f"First fork failed: {e}")

    # Decouple from parent environment
    os.chdir("/")
    os.setsid()
    # Use restrictive umask for security (owner read/write only)
    os.umask(0o077)

    # Second fork
    try:
        pid = os.fork()
        if pid > 0:
            # Parent exits
            sys.exit(0)
    except OSError as e:
        raise RuntimeError(f"Second fork failed: {e}")

    # Redirect standard file descriptors
    sys.stdout.flush()
    sys.stderr.flush()

    log_file = _get_log_file()
    with open("/dev/null", "r") as devnull:
        os.dup2(devnull.fileno(), sys.stdin.fileno())

    # Open log file with secure permissions (0600)
    log_fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(log_fd, sys.stdout.fileno())
    os.dup2(log_fd, sys.stderr.fileno())
    os.close(log_fd)

    # Write PID file
    pid_file = _get_pid_file()
    pid_file.write_text(str(os.getpid()))
    pid_file.chmod(0o600)


def _build_demo_db(db_path: Path) -> None:
    """Build a demo database with sample data.

    Creates demo agent sessions for the timeline view.
    """
    from sqlalchemy.orm import sessionmaker

    from zerg.database import Base
    from zerg.database import make_engine
    from zerg.models.agents import AgentsBase
    from zerg.services.agents_store import AgentsStore
    from zerg.services.demo_sessions import build_demo_agent_sessions

    db_url = f"sqlite:///{db_path}"
    engine = make_engine(db_url).execution_options(schema_translate_map={"zerg": None, "agents": None})
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
    db = SessionLocal()
    try:
        store = AgentsStore(db)
        for session in build_demo_agent_sessions():
            store.ingest_session(session)
    finally:
        db.close()


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
        help="Database URL (default: sqlite:///~/.longhouse/longhouse.db)",
    ),
    workers: int = typer.Option(
        1,
        "--workers",
        "-w",
        help="Number of worker processes",
    ),
    daemon: bool = typer.Option(
        False,
        "--daemon",
        help="Run as background daemon",
    ),
    stop: bool = typer.Option(
        False,
        "--stop",
        help="Stop running daemon",
    ),
    demo: bool = typer.Option(
        False,
        "--demo",
        help="Use demo database with sample data",
    ),
    demo_fresh: bool = typer.Option(
        False,
        "--demo-fresh",
        help="Rebuild demo database with sample data (implies --demo)",
    ),
) -> None:
    """Start the Longhouse server.

    By default, uses SQLite for zero-config startup. For production,
    configure a Postgres DATABASE_URL in your environment.

    Examples:
        longhouse serve                           # SQLite on localhost:8080
        longhouse serve --demo                    # Start with sample data
        longhouse serve --demo-fresh              # Rebuild demo data on start
        longhouse serve --daemon                  # Run in background
        longhouse serve --stop                    # Stop background server
        longhouse serve --host 0.0.0.0 --port 80  # Bind to all interfaces
        longhouse serve --db postgresql://...     # Use Postgres
        longhouse serve --reload                  # Dev mode with auto-reload
    """
    import uvicorn

    # Handle --stop flag
    if stop:
        is_running, pid = _is_server_running()
        if not is_running:
            typer.echo("Server is not running")
            return

        try:
            os.kill(pid, signal.SIGTERM)
            typer.secho(f"Stopped server (PID {pid})", fg=typer.colors.GREEN)
            _get_pid_file().unlink(missing_ok=True)
        except OSError as e:
            typer.secho(f"Failed to stop server: {e}", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        return

    # Check if already running when starting daemon
    if daemon:
        is_running, existing_pid = _is_server_running()
        if is_running:
            typer.secho(
                f"Server already running (PID {existing_pid})",
                fg=typer.colors.YELLOW,
            )
            typer.echo("Use 'longhouse serve --stop' to stop it first")
            raise typer.Exit(code=1)

    # Apply lite mode defaults early (before any imports that trigger config loading)
    _apply_lite_mode_defaults()

    # Handle demo mode (may override DATABASE_URL set above)
    if demo or demo_fresh:
        demo_db_path = _get_longhouse_home() / "demo.db"
        if demo_fresh and demo_db_path.exists():
            demo_db_path.unlink()

        if not demo_db_path.exists():
            typer.echo("Building demo database with sample data...")
            _build_demo_db(demo_db_path)
        os.environ["DATABASE_URL"] = f"sqlite:///{demo_db_path}"
        typer.secho("Demo mode: using sample data", fg=typer.colors.CYAN)
        typer.echo("")
    elif db:
        # Set database URL if explicitly provided
        os.environ["DATABASE_URL"] = db

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

    # Daemon mode incompatible with reload
    if daemon and reload:
        typer.secho(
            "ERROR: --daemon and --reload cannot be used together",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    # Check for bundled frontend
    from zerg.main import FRONTEND_DIST_DIR
    from zerg.main import FRONTEND_SOURCE

    has_frontend = FRONTEND_DIST_DIR is not None
    frontend_source = FRONTEND_SOURCE

    typer.echo("Starting Longhouse server...")
    typer.echo(f"  Host: {host}")
    typer.echo(f"  Port: {port}")
    typer.echo(f"  Database: {_mask_db_url(db_url)}")
    typer.echo(f"  Mode: {'lite (SQLite)' if is_sqlite else 'full (Postgres)'}")
    typer.echo(f"  Frontend: {frontend_source}")
    if daemon:
        typer.echo("  Daemon: yes")
    if reload:
        typer.echo("  Reload: enabled")
    typer.echo("")

    if has_frontend:
        typer.secho(f"  UI available at: http://{host}:{port}/", fg=typer.colors.GREEN)
        typer.echo("")

    # Daemonize if requested (Unix only)
    if daemon:
        if sys.platform == "win32":
            typer.secho(
                "ERROR: Daemon mode not supported on Windows",
                fg=typer.colors.RED,
            )
            typer.echo("  Use a service manager like NSSM instead")
            raise typer.Exit(code=1)

        typer.echo(f"Starting daemon... (log: {_get_log_file()})")
        _daemonize()

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
    """Show Longhouse configuration and status.

    Displays the current database configuration, lite_mode status,
    and other relevant settings without starting the server.

    Shows the *effective* configuration that `longhouse serve` would use,
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
        typer.echo("Run 'longhouse serve' to auto-configure defaults,")
        typer.echo("or set the missing environment variables.")
        raise typer.Exit(code=1)
    except Exception as e:
        typer.secho(f"Configuration error: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    # Database info
    db_url = settings.database_url or "(not set)"
    is_sqlite = settings.db_is_sqlite
    lite_mode = settings.lite_mode

    typer.echo("Longhouse Configuration (effective)")
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
    longhouse_home = _get_longhouse_home()
    typer.echo(f"  Config: {longhouse_home}")
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
