"""Serve command for starting the Longhouse server.

Commands:
- serve: Start the Longhouse server with uvicorn
- serve --daemon: Start as background daemon
- serve --stop: Stop running daemon
- status: Show local Longhouse health (one-line summary or --verbose detail)
"""

from __future__ import annotations

import base64
import ipaddress
import os
import secrets
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import typer

from zerg.services.longhouse_paths import resolve_longhouse_home

app = typer.Typer(help="Longhouse server commands")


def _get_lan_ip() -> str | None:
    """Return the LAN IP the OS would use for outbound traffic.

    Uses a UDP connect (no packets sent) to query the routing table.
    Returns None if the machine is offline or only has loopback.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        ip_obj = ipaddress.ip_address(ip)
        if ip_obj.is_loopback or ip_obj.is_link_local:
            return None
        return ip
    except OSError:
        return None


def _host_is_public(host: str) -> bool:
    """Return True when binding ``host`` exposes the server beyond this machine.

    Wildcard binds ("", 0.0.0.0, ::) are public. A concrete address is public
    when it is not a loopback address. ``localhost`` is the only hostname we
    treat as local; any other hostname is assumed routable (fail safe).
    """
    if host in ("", "0.0.0.0", "::"):
        return True
    if host == "localhost":
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Not an IP literal and not "localhost" → assume routable hostname.
        return True


def _effective_auth_disabled() -> bool:
    """Return True when runtime auth will be OFF, mirroring config resolution.

    Auth is disabled when AUTH_DISABLED, DEMO_MODE, or TESTING is truthy — the
    same inputs config.resolve_app_mode() uses. This is read after the repo
    .env has been loaded so file-based config cannot slip past the gate.
    """

    def _truthy(value: str | None) -> bool:
        return bool(value) and value.strip().lower() in {"1", "true", "yes", "on"}

    return _truthy(os.environ.get("AUTH_DISABLED")) or _truthy(os.environ.get("DEMO_MODE")) or _truthy(os.environ.get("TESTING"))


def _load_repo_env_for_gate() -> None:
    """Load the repo-root .env so the public-bind gate sees runtime auth config.

    Mirrors config._load_settings(): the real runtime loads .env with
    override=True when not testing. Without this, an AUTH_DISABLED=1 / DEMO_MODE=1
    in .env would bypass the gate (the gate runs before app config loads).
    Skipped under TESTING so unit tests are not polluted by a real .env.
    """
    if os.getenv("TESTING", "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    try:
        from dotenv import load_dotenv

        from zerg.config import _REPO_ROOT  # safe: config import does not load the DB

        env_path = _REPO_ROOT / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=True)
    except Exception:
        # Never let env discovery crash startup; the gate then sees process env only.
        pass


def _apply_runtime_public_url(public_url: str | None, *, force: bool = False) -> str | None:
    """Seed runtime env vars from the CLI/public config when needed."""
    configured = os.getenv("APP_PUBLIC_URL") or os.getenv("PUBLIC_SITE_URL")
    effective = public_url if force else (configured or public_url)
    if not effective:
        return None

    if force or not os.getenv("APP_PUBLIC_URL"):
        os.environ["APP_PUBLIC_URL"] = effective
    if force or not os.getenv("PUBLIC_SITE_URL"):
        os.environ["PUBLIC_SITE_URL"] = effective

    return effective


def _get_longhouse_home() -> Path:
    """Return the Longhouse home directory (~/.longhouse), creating if needed."""
    longhouse_home = resolve_longhouse_home()
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


def _get_or_create_trigger_secret() -> str:
    """Get or create a persistent TRIGGER_SIGNING_SECRET for lite mode.

    Stores the secret in ~/.longhouse/trigger.key for persistence across restarts.
    """
    secret_file = _get_longhouse_home() / "trigger.key"

    if secret_file.exists():
        return secret_file.read_text().strip()

    # Generate a 64-char hex string (32 bytes)
    key = secrets.token_hex(32)

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


def _apply_lite_mode_defaults(*, public_intent: bool = False) -> None:
    """Apply default environment variables for lite (SQLite) mode.

    Sets up sensible defaults for zero-config OSS startup:
    - SQLite database in ~/.longhouse/longhouse.db
    - Auth disabled (single-user local install) ONLY on a loopback bind
    - Single-tenant mode
    - Auto-generated secrets (FERNET_SECRET, TRIGGER_SIGNING_SECRET)

    ``public_intent`` is True when the operator asked to bind beyond loopback
    (``--host 0.0.0.0``/``::`` or ``--domain``). In that case we do NOT silently
    default ``AUTH_DISABLED=1`` — an unauthenticated server must never be the
    default on a public interface. The caller enforces the explicit gate.
    """
    # Database URL
    if not os.environ.get("DATABASE_URL"):
        default_db = _get_default_db_path()
        os.environ["DATABASE_URL"] = f"sqlite:///{default_db}"

    # Auth disabled for local use only. On a public bind we leave AUTH_DISABLED
    # unset so auth is enabled by default; the operator must opt in explicitly.
    if "AUTH_DISABLED" not in os.environ and not public_intent:
        os.environ["AUTH_DISABLED"] = "1"

    # Single-tenant mode
    if "SINGLE_TENANT" not in os.environ:
        os.environ["SINGLE_TENANT"] = "1"

    # FERNET_SECRET (required by crypto module)
    if not os.environ.get("FERNET_SECRET"):
        os.environ["FERNET_SECRET"] = _get_or_create_fernet_secret()

    # TRIGGER_SIGNING_SECRET (required for webhook triggers, persisted like FERNET)
    if not os.environ.get("TRIGGER_SIGNING_SECRET"):
        os.environ["TRIGGER_SIGNING_SECRET"] = _get_or_create_trigger_secret()


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
    from zerg.services.agents_store import AgentsStore
    from zerg.services.demo_sessions import build_demo_agent_sessions

    db_url = f"sqlite:///{db_path}"
    engine = make_engine(db_url).execution_options(schema_translate_map={"zerg": None, "agents": None})
    Base.metadata.create_all(bind=engine)

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
    domain: Optional[str] = typer.Option(
        None,
        "--domain",
        help="Public domain (e.g. longhouse.example.com). Stored in config, shown at startup.",
    ),
    allow_public_no_auth: bool = typer.Option(
        False,
        "--allow-public-no-auth",
        help="Allow binding to a public interface with auth disabled (e.g. behind a trusted reverse proxy that authenticates). Dangerous; off by default.",
    ),
) -> None:
    """Start the Longhouse server.

    Uses SQLite for zero-config startup. On a loopback bind auth is disabled
    for frictionless local use; on a public bind (``--host 0.0.0.0``/``::`` or
    ``--domain``) auth is REQUIRED unless you pass ``--allow-public-no-auth``.

    Examples:
        longhouse serve                                     # SQLite on localhost:8080
        longhouse serve --demo                              # Start with sample data
        longhouse serve --demo-fresh                        # Rebuild demo data on start
        longhouse serve --daemon                            # Run in background
        longhouse serve --stop                              # Stop background server
        longhouse serve --host 0.0.0.0 --port 80            # Public bind (auth required)
        longhouse serve --host 0.0.0.0 --domain my.host.com # LAN + public domain
        longhouse serve --reload                            # Dev mode with auto-reload
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
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                typer.secho(
                    f"Timed out waiting for server process {pid} to exit",
                    fg=typer.colors.RED,
                )
                raise typer.Exit(code=1)
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

    # Load the repo .env first so the gate evaluates the SAME auth inputs the
    # runtime will (file-based AUTH_DISABLED/DEMO_MODE must not slip past).
    _load_repo_env_for_gate()

    # Determine public intent before applying defaults: any non-loopback host or
    # a configured public domain means the server is reachable beyond this machine.
    public_intent = _host_is_public(host) or bool(domain)

    # Apply lite mode defaults early (before any imports that trigger config loading).
    # On a public bind we do NOT auto-disable auth.
    _apply_lite_mode_defaults(public_intent=public_intent)

    # Handle demo mode (may override DATABASE_URL set above)
    if demo or demo_fresh:
        demo_db_path = _get_longhouse_home() / "demo.db"
        if demo_fresh and demo_db_path.exists():
            demo_db_path.unlink()

        # Set DATABASE_URL before _build_demo_db so that any module-level
        # engine initialisation triggered by the import chain picks up the
        # demo database, not the default longhouse.db.
        os.environ["DATABASE_URL"] = f"sqlite:///{demo_db_path}"

        if not demo_db_path.exists():
            typer.echo("Building demo database with sample data...")
            _build_demo_db(demo_db_path)
        typer.secho("Demo mode: using sample data", fg=typer.colors.CYAN)
        typer.echo("")
    elif db:
        # Set database URL if explicitly provided
        os.environ["DATABASE_URL"] = db

    db_url = os.environ["DATABASE_URL"]
    is_sqlite = db_url.startswith("sqlite")

    # Safety gate: never expose an unauthenticated server on a public interface.
    # Evaluate the full set of auth-disabling inputs (AUTH_DISABLED/DEMO_MODE/
    # TESTING), matching how runtime config resolves auth_disabled.
    auth_disabled = _effective_auth_disabled()

    # Coerce to a real bool: direct (non-Typer) callers may pass the Typer
    # OptionInfo sentinel, which is truthy and would silently open the gate.
    allow_public_no_auth = allow_public_no_auth is True

    if public_intent and auth_disabled:
        if allow_public_no_auth:
            typer.secho(
                "WARNING: Public bind with auth disabled (--allow-public-no-auth).",
                fg=typer.colors.YELLOW,
            )
            typer.secho(
                "  Anyone who can reach this address has full, unauthenticated access.",
                fg=typer.colors.YELLOW,
            )
            typer.secho(
                "  Only safe behind a trusted reverse proxy that authenticates requests.",
                fg=typer.colors.YELLOW,
            )
            typer.echo("")
        else:
            typer.secho(
                "ERROR: Refusing to bind a public interface with authentication disabled.",
                fg=typer.colors.RED,
            )
            typer.echo("")
            typer.echo("  You bound a non-loopback host or set --domain, but AUTH_DISABLED=1.")
            typer.echo("  This would expose an unauthenticated server to the network.")
            typer.echo("")
            typer.echo("  Enable password auth (simplest):")
            typer.secho(
                '    export LONGHOUSE_PASSWORD_HASH="$(longhouse hash-password)"',
                fg=typer.colors.BRIGHT_BLACK,
            )
            typer.secho(
                "    export JWT_SECRET=$(openssl rand -hex 32)",
                fg=typer.colors.BRIGHT_BLACK,
            )
            typer.secho(
                "    export INTERNAL_API_SECRET=$(openssl rand -hex 32)",
                fg=typer.colors.BRIGHT_BLACK,
            )
            typer.echo("")
            typer.echo("  Or, if a trusted reverse proxy already authenticates requests,")
            typer.echo("  re-run with --allow-public-no-auth to accept the risk.")
            raise typer.Exit(code=1)

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

    # Check if port is available
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host if host != "0.0.0.0" else "127.0.0.1", port))
        sock.close()
    except OSError as e:
        typer.secho(f"ERROR: Port {port} is already in use.", fg=typer.colors.RED)
        typer.echo(f"  Try: longhouse serve --port {port + 1}")
        typer.echo(f"  Or find what's using it: lsof -i :{port}")
        raise typer.Exit(code=1) from e

    # Persist public domain to config if provided, then load from config as fallback.
    from zerg.cli.config_file import load_config
    from zerg.cli.config_file import save_loaded_config

    file_cfg = load_config()
    if domain:
        public_url = f"https://{domain}"
        file_cfg.server.host = host
        file_cfg.server.port = port
        file_cfg.server.public_url = public_url
        save_loaded_config(file_cfg)
    else:
        public_url = file_cfg.server.public_url

    public_url = _apply_runtime_public_url(public_url, force=bool(domain))

    is_public_interface = host in ("0.0.0.0", "::", "")
    lan_ip = _get_lan_ip() if is_public_interface else None

    # Check for bundled frontend
    from zerg.main import FRONTEND_DIST_DIR
    from zerg.main import FRONTEND_SOURCE

    has_frontend = FRONTEND_DIST_DIR is not None
    frontend_source = FRONTEND_SOURCE

    if not has_frontend:
        typer.secho(
            "WARNING: Frontend not found. API will work but no web UI.",
            fg=typer.colors.YELLOW,
        )
        typer.echo("  If you installed from PyPI, this may be a packaging issue.")
        typer.echo("  From source: cd web && bun run build")
        typer.echo("")

    typer.echo("Starting Longhouse server...")
    typer.echo(f"  Database: {_mask_db_url(db_url)}")
    typer.echo(f"  Mode: {'lite (SQLite)' if is_sqlite else 'full (Postgres)'}")
    typer.echo(f"  Frontend: {frontend_source}")
    if daemon:
        typer.echo("  Daemon: yes")
    if reload:
        typer.echo("  Reload: enabled")
    typer.echo("")

    typer.secho(f"  Local:    http://127.0.0.1:{port}/", fg=typer.colors.GREEN)
    if lan_ip:
        typer.secho(f"  LAN:      http://{lan_ip}:{port}/", fg=typer.colors.GREEN)
    if public_url:
        typer.secho(f"  Public:   {public_url}/", fg=typer.colors.CYAN)
    typer.echo("")

    if is_public_interface and lan_ip:
        typer.echo("  To connect from another machine:")
        typer.secho(f"    longhouse connect --url http://{lan_ip}:{port}", fg=typer.colors.BRIGHT_BLACK)
    if public_url:
        typer.echo("  To connect from any machine (via your domain):")
        typer.secho(f"    longhouse connect --url {public_url}", fg=typer.colors.BRIGHT_BLACK)
    if is_public_interface or public_url:
        typer.echo("")

    try:
        from zerg.cli.acquisition import emit_acquisition_event_once

        topology = "demo" if demo or demo_fresh else ("self_host_runtime" if public_url or is_public_interface else "local_runtime")
        emit_acquisition_event_once(
            "serve_started",
            "runtime_first_start",
            command="serve",
            topology=topology,
            props={"daemon": daemon, "public_bind": is_public_interface},
        )
    except Exception:
        pass

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
        # Machine Agent control uses an app-level heartbeat. Uvicorn protocol
        # pings have been flaky behind hosted proxies for non-browser clients,
        # and the control route has its own receive timeout for stale sockets.
        ws_ping_interval=None,
    )


_SEVERITY_COLOR = {
    "green": typer.colors.GREEN,
    "yellow": typer.colors.YELLOW,
    "red": typer.colors.RED,
    "gray": typer.colors.BRIGHT_BLACK,
}

_SEVERITY_SYMBOL = {
    "green": "●",
    "yellow": "▲",
    "red": "✖",
    "gray": "○",
}


@app.command()
def status(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show full detail"),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable JSON"),
) -> None:
    """Show local Longhouse health.

    One-line summary by default. Use --verbose for detail or --json for scripts.
    Exit code 0 = healthy or uninstalled, 1 = needs attention.
    """
    import json as json_mod

    from zerg.services.local_health import collect_local_health

    health = collect_local_health()

    if json_output:
        typer.echo(json_mod.dumps(health, indent=2))
        if health["health_state"] in ("broken", "degraded"):
            raise typer.Exit(code=1)
        return

    severity = health["severity"]
    state = health["health_state"]
    headline = health["headline"]
    color = _SEVERITY_COLOR.get(severity, typer.colors.WHITE)
    symbol = _SEVERITY_SYMBOL.get(severity, "?")

    # Target URL for context
    launch = health.get("launch_readiness") or {}
    target_url = launch.get("stored_url")
    machine_name = launch.get("machine_name")
    suffix_parts = []
    if machine_name:
        suffix_parts.append(machine_name)
    if target_url:
        suffix_parts.append(f"→ {target_url}")
    suffix = f"  ({', '.join(suffix_parts)})" if suffix_parts else ""

    typer.secho(f"{symbol} {headline}{suffix}", fg=color)

    if verbose:
        typer.echo("")

        # Service
        service = health.get("service") or {}
        service_status = service.get("status", "unknown")
        typer.echo(f"  Engine service: {service_status}")
        if service.get("service_file"):
            typer.echo(f"  Service file:   {service['service_file']}")

        # Engine status
        engine = health.get("engine_status") or {}
        if engine.get("exists"):
            age = engine.get("age_seconds")
            age_str = f"{age}s ago" if age is not None else "unknown"
            typer.echo(f"  Engine status:  {engine['path']} ({age_str})")
            payload = engine.get("payload") or {}
            if payload.get("spool_pending_count"):
                typer.echo(f"  Spool pending:  {payload['spool_pending_count']}")
            if payload.get("spool_dead_count"):
                typer.secho(f"  Spool dead:     {payload['spool_dead_count']}", fg=typer.colors.RED)
            if payload.get("consecutive_ship_failures"):
                typer.secho(f"  Ship failures:  {payload['consecutive_ship_failures']}", fg=typer.colors.YELLOW)
        else:
            typer.echo(f"  Engine status:  not found ({engine.get('path', '~/.longhouse/agent/engine-status.json')})")

        # Outbox
        outbox = health.get("outbox") or {}
        count = outbox.get("file_count", 0)
        if count > 0:
            oldest = outbox.get("oldest_age_seconds")
            oldest_str = f", oldest {oldest}s" if oldest is not None else ""
            typer.echo(f"  Outbox:         {count} files{oldest_str}")

        # Runner
        runner = launch.get("runner") or {}
        if runner.get("exists"):
            runner_name = runner.get("runner_name") or "(unnamed)"
            typer.echo(f"  Remote Runner:  {runner_name} ({runner.get('path')})")

        # Reasons and actions
        reasons = health.get("reasons") or []
        actions = health.get("suggested_actions") or []
        if reasons:
            typer.echo("")
            typer.echo("  Issues:")
            for reason in reasons:
                typer.echo(f"    - {reason}")
        if actions:
            typer.echo("")
            typer.echo("  Suggested:")
            for action in actions:
                typer.echo(f"    {action}")

    elif state in ("broken", "degraded"):
        actions = health.get("suggested_actions") or []
        if actions:
            typer.echo(f"  → {actions[0]}")

    if state in ("broken", "degraded"):
        raise typer.Exit(code=1)


@app.command(name="hash-password")
def hash_password(
    password: Optional[str] = typer.Option(
        None,
        "--password",
        help="Password to hash. Omit to be prompted (hidden input, recommended).",
    ),
) -> None:
    """Generate a LONGHOUSE_PASSWORD_HASH for password auth.

    Prints a pbkdf2_sha256 hash. Use it to enable auth on a public bind:

        export LONGHOUSE_PASSWORD_HASH="$(longhouse hash-password)"

    The plaintext password is never stored or logged.
    """
    if password is None:
        # Prompt on stderr so stdout stays clean for $(longhouse hash-password).
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True, err=True)

    if not password:
        typer.secho("ERROR: password must not be empty", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    import hashlib

    iterations = 600_000
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    encoded = "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(derived).decode("ascii"),
    )
    # Print only the hash on stdout so it can be captured by $(...).
    typer.echo(encoded)


# Export for main.py
__all__ = ["app", "serve", "status", "hash_password"]
