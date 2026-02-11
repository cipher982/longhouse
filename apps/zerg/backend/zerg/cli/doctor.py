"""Doctor command for Longhouse self-diagnosis.

Checks environment, server health, shipper status, and configuration.
Designed to run after install/upgrade to quickly identify issues.

Usage:
    longhouse doctor          # Run all checks
    longhouse doctor --json   # Machine-readable output
"""

from __future__ import annotations

import json as json_mod
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path

import typer

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

PASS = "pass"
WARN = "warn"
FAIL = "fail"

_SYMBOLS = {
    PASS: "\u2713",  # check mark
    WARN: "\u26a0",  # warning sign
    FAIL: "\u2717",  # cross mark
}

_COLORS = {
    PASS: typer.colors.GREEN,
    WARN: typer.colors.YELLOW,
    FAIL: typer.colors.RED,
}


class CheckResult:
    """A single check result."""

    __slots__ = ("status", "label", "detail")

    def __init__(self, status: str, label: str, detail: str | None = None) -> None:
        self.status = status
        self.label = label
        self.detail = detail

    def to_dict(self) -> dict:
        d: dict = {"status": self.status, "label": self.label}
        if self.detail:
            d["detail"] = self.detail
        return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_longhouse_home() -> Path:
    return Path.home() / ".longhouse"


def _get_claude_dir() -> Path:
    config_dir = os.getenv("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir).expanduser()
    return Path.home() / ".claude"


def _cmd_version(cmd: str) -> str | None:
    """Run `cmd --version` and return the first line, or None."""
    try:
        result = subprocess.run(
            [cmd, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip().splitlines()[0]
    except Exception:
        pass
    return None


def _port_available(host: str, port: int) -> bool:
    """Return True if the port is free."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Check groups
# ---------------------------------------------------------------------------


def _check_environment() -> list[CheckResult]:
    """Environment checks: Python, uv, bun, SQLite FTS5."""
    results: list[CheckResult] = []

    # Python version
    py_ver = sys.version.split()[0]
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 12):
        results.append(CheckResult(PASS, f"Python {py_ver}"))
    else:
        results.append(CheckResult(FAIL, f"Python {py_ver} (need >= 3.12)", "Upgrade: https://www.python.org/downloads/"))

    # uv
    if shutil.which("uv"):
        ver = _cmd_version("uv") or "uv"
        # `uv --version` prints "uv 0.x.y"
        results.append(CheckResult(PASS, ver))
    else:
        results.append(CheckResult(WARN, "uv not found", "Install: curl -LsSf https://astral.sh/uv/install.sh | sh"))

    # bun (dev only)
    if shutil.which("bun"):
        ver = _cmd_version("bun") or ""
        # bun --version returns just the number (e.g. "1.2.20")
        label = f"bun {ver}" if ver and not ver.lower().startswith("bun") else (ver or "bun")
        results.append(CheckResult(PASS, label))
    else:
        results.append(CheckResult(WARN, "bun not found (only needed for development)"))

    # SQLite FTS5
    sqlite_ver = sqlite3.sqlite_version
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE _fts5_test USING fts5(content)")
        conn.execute("DROP TABLE _fts5_test")
        conn.close()
        results.append(CheckResult(PASS, f"SQLite {sqlite_ver} (FTS5 supported)"))
    except Exception:
        results.append(CheckResult(WARN, f"SQLite {sqlite_ver} (FTS5 not available)", "Search will fall back to LIKE queries"))

    return results


def _check_server() -> list[CheckResult]:
    """Server checks: reachable, database, auth."""
    results: list[CheckResult] = []

    # Determine configured URL
    from zerg.cli.config_file import load_config

    config = load_config()
    host = config.server.host
    port = config.server.port
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::', '') else host}:{port}"

    # Server reachable?
    try:
        import httpx

        with httpx.Client(timeout=5) as client:
            resp = client.get(f"{url}/api/health")
            if resp.status_code == 200:
                results.append(CheckResult(PASS, f"Server reachable at {url}"))

                # Parse health response for database status
                try:
                    health = resp.json()
                    db_status = health.get("database", health.get("db"))
                    if db_status in ("ok", "healthy", True):
                        results.append(CheckResult(PASS, "Database healthy"))
                    elif db_status:
                        results.append(CheckResult(WARN, f"Database status: {db_status}"))
                except Exception:
                    pass
            else:
                results.append(CheckResult(WARN, f"Server at {url} returned HTTP {resp.status_code}"))
    except Exception:
        results.append(CheckResult(FAIL, f"Server not reachable at {url}", "Start with: longhouse serve"))

    # Database file (SQLite)
    longhouse_home = _get_longhouse_home()
    db_path = longhouse_home / "longhouse.db"
    if db_path.exists():
        size_mb = db_path.stat().st_size / (1024 * 1024)
        results.append(CheckResult(PASS, f"Database file exists ({size_mb:.1f} MB)"))
    else:
        # Check for DATABASE_URL env
        db_url = os.environ.get("DATABASE_URL", "")
        if db_url.startswith("sqlite:///"):
            alt_path = Path(db_url.replace("sqlite:///", ""))
            if alt_path.exists():
                size_mb = alt_path.stat().st_size / (1024 * 1024)
                results.append(CheckResult(PASS, f"Database file exists ({size_mb:.1f} MB)"))
            else:
                results.append(CheckResult(WARN, "Database file not found (will be created on first serve)"))
        elif db_url.startswith("postgresql"):
            results.append(CheckResult(PASS, "Using Postgres (external DB)"))
        else:
            results.append(CheckResult(WARN, "No database file yet", "Run: longhouse serve"))

    # Auth configuration
    auth_disabled = os.environ.get("AUTH_DISABLED", "").lower() in ("1", "true", "yes")
    password_set = bool(os.environ.get("LONGHOUSE_PASSWORD") or os.environ.get("LONGHOUSE_PASSWORD_HASH"))
    google_client = bool(os.environ.get("GOOGLE_CLIENT_ID"))

    if password_set:
        results.append(CheckResult(PASS, "Auth configured (password)"))
    elif google_client:
        results.append(CheckResult(PASS, "Auth configured (Google OAuth)"))
    elif auth_disabled:
        # Check if we're on localhost
        is_local = host in ("127.0.0.1", "localhost", "::1")
        if is_local:
            results.append(CheckResult(PASS, "Auth disabled (localhost OK)"))
        else:
            results.append(
                CheckResult(
                    WARN,
                    "Auth disabled on non-localhost",
                    "Set LONGHOUSE_PASSWORD for remote access",
                )
            )
    else:
        results.append(CheckResult(WARN, "No auth configured", "Set LONGHOUSE_PASSWORD or run: longhouse serve"))

    return results


def _check_shipper() -> list[CheckResult]:
    """Shipper checks: Claude sessions, device token, shipper log, service."""
    results: list[CheckResult] = []
    claude_dir = _get_claude_dir()

    # Claude Code sessions directory
    projects_dir = claude_dir / "projects"
    if projects_dir.is_dir():
        try:
            project_count = sum(1 for p in projects_dir.iterdir() if p.is_dir())
            results.append(CheckResult(PASS, f"Claude Code sessions found ({project_count} projects)"))
        except PermissionError:
            results.append(CheckResult(WARN, "Claude Code projects dir not readable"))
    else:
        results.append(CheckResult(WARN, "No Claude Code sessions directory", "Install Claude Code to start shipping sessions"))

    # Device token
    token_path = claude_dir / "longhouse-device-token"
    if token_path.exists():
        results.append(CheckResult(PASS, "Device token configured"))
    else:
        results.append(CheckResult(FAIL, "Device token not configured", "Run: longhouse auth"))

    # Shipper log
    log_path = claude_dir / "shipper.log"
    if log_path.exists():
        try:
            stat = log_path.stat()
            size_kb = stat.st_size / 1024
            mod_time = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            age = datetime.now(timezone.utc) - mod_time

            if age.total_seconds() < 3600:
                age_str = f"{int(age.total_seconds() / 60)}m ago"
            elif age.total_seconds() < 86400:
                age_str = f"{int(age.total_seconds() / 3600)}h ago"
            else:
                age_str = f"{int(age.total_seconds() / 86400)}d ago"

            results.append(CheckResult(PASS, f"Shipper log exists ({size_kb:.0f} KB, last modified {age_str})"))
        except Exception:
            results.append(CheckResult(PASS, "Shipper log exists"))
    else:
        results.append(CheckResult(WARN, "No shipper log found", "Run: longhouse connect"))

    # Shipper service status
    try:
        from zerg.services.shipper import get_service_status

        status = get_service_status()
        if status == "running":
            results.append(CheckResult(PASS, "Shipper service running"))
        elif status == "stopped":
            results.append(CheckResult(WARN, "Shipper service stopped", "Start with: longhouse connect --install"))
        else:
            results.append(CheckResult(WARN, "Shipper service not installed", "Install with: longhouse connect --install"))
    except Exception:
        # Service check not available on this platform
        pass

    return results


def _check_config() -> list[CheckResult]:
    """Config checks: data dir writable, config file, port availability."""
    results: list[CheckResult] = []

    # Data directory writable
    longhouse_home = _get_longhouse_home()
    if longhouse_home.exists():
        test_file = longhouse_home / ".doctor-write-test"
        try:
            test_file.write_text("test")
            test_file.unlink()
            results.append(CheckResult(PASS, f"Data directory writable ({longhouse_home})"))
        except (OSError, PermissionError):
            results.append(CheckResult(FAIL, f"Data directory not writable ({longhouse_home})"))
    else:
        try:
            longhouse_home.mkdir(parents=True, exist_ok=True)
            results.append(CheckResult(PASS, f"Data directory created ({longhouse_home})"))
        except (OSError, PermissionError):
            results.append(CheckResult(FAIL, f"Cannot create data directory ({longhouse_home})"))

    # Config file
    from zerg.cli.config_file import get_config_path

    config_path = get_config_path()
    if config_path.exists():
        results.append(CheckResult(PASS, f"Config file found ({config_path})"))
    else:
        results.append(CheckResult(WARN, "No config file", "Run: longhouse onboard"))

    # Port availability (only if server not running)
    from zerg.cli.config_file import load_config

    config = load_config()
    port = config.server.port
    host = config.server.host

    # Only check port if server is not already running
    try:
        import httpx

        with httpx.Client(timeout=2) as client:
            client.get(f"http://{'127.0.0.1' if host in ('0.0.0.0', '::', '') else host}:{port}/api/health")
            # Server is running, skip port check
    except Exception:
        # Server not responding -- check if port is free
        if _port_available("127.0.0.1", port):
            results.append(CheckResult(PASS, f"Port {port} available"))
        else:
            results.append(
                CheckResult(
                    WARN,
                    f"Port {port} in use (but server not responding)",
                    f"Check: lsof -i :{port}",
                )
            )

    return results


# ---------------------------------------------------------------------------
# Main command
# ---------------------------------------------------------------------------


def doctor(
    json: bool = typer.Option(
        False,
        "--json",
        help="Output results as JSON",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show details for all checks (not just failures)",
    ),
) -> None:
    """Run self-diagnosis checks on your Longhouse installation.

    Checks environment, server health, shipper status, and configuration.
    Run after install or upgrade to verify everything is working.

    Examples:
        longhouse doctor           # Quick health check
        longhouse doctor --json    # Machine-readable output
        longhouse doctor -v        # Show all details
    """
    sections: list[tuple[str, list[CheckResult]]] = [
        ("Environment", _check_environment()),
        ("Server", _check_server()),
        ("Shipper", _check_shipper()),
        ("Config", _check_config()),
    ]

    # Tally
    counts = {PASS: 0, WARN: 0, FAIL: 0}
    for _, checks in sections:
        for c in checks:
            counts[c.status] += 1

    # JSON output
    if json:
        output = {
            "sections": {name: [c.to_dict() for c in checks] for name, checks in sections},
            "summary": counts,
        }
        typer.echo(json_mod.dumps(output, indent=2))
        if counts[FAIL] > 0:
            raise typer.Exit(code=1)
        return

    # Pretty output
    typer.echo("")
    typer.secho("Longhouse Doctor", bold=True)
    typer.echo("=" * 40)

    for section_name, checks in sections:
        typer.echo("")
        typer.secho(f"  {section_name}", bold=True)
        for c in checks:
            symbol = _SYMBOLS[c.status]
            color = _COLORS[c.status]
            typer.secho(f"    {symbol} {c.label}", fg=color)
            if c.detail and (verbose or c.status != PASS):
                typer.echo(f"      -> {c.detail}")

    # Summary
    typer.echo("")
    parts = []
    if counts[PASS]:
        parts.append(typer.style(f"{counts[PASS]} passed", fg=typer.colors.GREEN))
    if counts[WARN]:
        parts.append(typer.style(f"{counts[WARN]} warning{'s' if counts[WARN] != 1 else ''}", fg=typer.colors.YELLOW))
    if counts[FAIL]:
        parts.append(typer.style(f"{counts[FAIL]} failed", fg=typer.colors.RED))
    typer.echo("  " + ", ".join(parts))
    typer.echo("")

    if counts[FAIL] > 0:
        raise typer.Exit(code=1)


# Export for main.py registration
__all__ = ["doctor"]
