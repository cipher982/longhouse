# E2E log suppression: only active when E2E_LOG_SUPPRESS=1 for test runs

from zerg.config import get_settings

_settings = get_settings()

if _settings.e2e_log_suppress:
    from zerg.e2e_logging_hacks import silence_info_logs

    silence_info_logs()

# --- TOP: Force silence for E2E or CLI if LOG_LEVEL=WARNING is set ---
import logging

# ---------------------------------------------------------------------
from dotenv import load_dotenv

# Load environment variables FIRST - before any other imports
load_dotenv()

# fmt: off
# ruff: noqa: E402
# Standard library
# fmt: on
# --------------------------------------------------------------------------
# LOGGING CONFIGURATION (dynamic, clean, less spammy):
# --------------------------------------------------------------------------
#
# - Default log level: INFO (dev-friendly)
# - Can be set at runtime with LOG_LEVEL env (e.g. LOG_LEVEL=WARNING for CI)
# - Explicitly suppresses spammy WebSocket modules to WARNING by default
#
# Third-party
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from zerg.constants import AGENTS_PREFIX
from zerg.constants import API_PREFIX
from zerg.constants import MODELS_PREFIX
from zerg.constants import THREADS_PREFIX
from zerg.database import initialize_database
from zerg.routers.account_connectors import router as account_connectors_router
from zerg.routers.admin import router as admin_router
from zerg.routers.agent_config import router as agent_config_router
from zerg.routers.agent_connectors import router as agent_connectors_router
from zerg.routers.agents import router as agents_router
from zerg.routers.auth import router as auth_router
from zerg.routers.connectors import router as connectors_router
from zerg.routers.email_webhooks import router as email_webhook_router
from zerg.routers.email_webhooks_pubsub import router as pubsub_webhook_router
from zerg.routers.funnel import router as funnel_router
from zerg.routers.graph_layout import router as graph_router
from zerg.routers.jarvis import router as jarvis_router
from zerg.routers.knowledge import router as knowledge_router
from zerg.routers.mcp_servers import router as mcp_servers_router
from zerg.routers.metrics import router as metrics_router
from zerg.routers.models import router as models_router
from zerg.routers.oauth import router as oauth_router
from zerg.routers.ops import beacon_router as ops_beacon_router
from zerg.routers.ops import router as ops_router
from zerg.routers.runners import router as runners_router
from zerg.routers.runs import router as runs_router
from zerg.routers.stream import router as stream_router
from zerg.routers.sync import router as sync_router
from zerg.routers.system import router as system_router
from zerg.routers.templates import router as templates_router
from zerg.routers.threads import router as threads_router
from zerg.routers.triggers import router as triggers_router
from zerg.routers.users import router as users_router
from zerg.routers.websocket import router as websocket_router
from zerg.routers.workflow_executions import router as workflow_executions_router
from zerg.routers.workflows import router as workflows_router

# Email trigger polling service (stub for now)
# Background services ---------------------------------------------------------
#
# Long-running polling loops like *SchedulerService* and *EmailTriggerService*
# keep the asyncio event-loop alive.  When the backend is imported by *pytest*
# those tasks cause the test runner to **hang** after the last test finishes
# unless they are stopped explicitly.  To make the entire test-suite
# friction-free we skip service start-up when the environment variable
# ``TESTING`` is truthy (set automatically by `backend/tests/conftest.py`).
from zerg.services.ops_events import ops_events_bridge  # noqa: E402
from zerg.services.scheduler_service import scheduler_service  # noqa: E402

# Import topic_manager at module level so event subscriptions register in worker process
from zerg.websocket.manager import topic_manager  # noqa: E402, F401

_log_level_name = _settings.log_level.upper()
try:
    _log_level = getattr(logging, _log_level_name)
except AttributeError:
    _log_level = logging.INFO
else:
    pass


# Custom formatter that displays structured fields from 'extra' dict
class StructuredFormatter(logging.Formatter):
    """Formatter that renders structured fields for grep-able telemetry logs.

    For logs with 'extra' dict, formats as:
        2025-12-15 03:19:33 INFO llm_call_complete worker_id=2025-12-15... phase=tool_decision duration_ms=19500
    """

    def format(self, record):
        # Start with timestamp and level
        parts = [
            self.formatTime(record, "%Y-%m-%d %H:%M:%S"),
            record.levelname,
            record.getMessage(),
        ]

        # Add structured fields if present (skip standard LogRecord attributes)
        BUILTIN_ATTRS = {
            "name",
            "msg",
            "args",
            "created",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "module",
            "msecs",
            "message",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "thread",
            "threadName",
            "exc_info",
            "exc_text",
            "stack_info",
            "event",  # 'event' is message content, not separate field
        }

        # Collect extra fields for structured output
        extra_fields = []
        for key, value in record.__dict__.items():
            if key not in BUILTIN_ATTRS and not key.startswith("_"):
                # Format value concisely
                if isinstance(value, str) and len(value) > 50:
                    value_str = value[:47] + "..."
                else:
                    value_str = str(value)
                extra_fields.append(f"{key}={value_str}")

        if extra_fields:
            parts.append(" ".join(extra_fields))

        return " ".join(parts)


# Configure logging with structured formatter
# Must configure before any loggers are created
_root_logger = logging.getLogger()
_root_logger.setLevel(_log_level)

# Remove any existing handlers to avoid duplicates
for handler in _root_logger.handlers[:]:
    _root_logger.removeHandler(handler)

# Add our structured formatter handler
_handler = logging.StreamHandler()
_handler.setFormatter(StructuredFormatter())
_root_logger.addHandler(_handler)

# Suppress verbose logs from known-noisy modules (even when LOG_LEVEL=DEBUG)
#
# Goal: keep dev logs high-signal. If you need full wire/debug output from these,
# temporarily set their log levels explicitly in your environment or in a local patch.
for _noisy_mod in (
    # Internal chatty modules
    "zerg.routers.websocket",
    "zerg.websocket.manager",
    # Third-party libraries that can dump huge payloads (e.g., OpenAI request options incl. prompts)
    "openai",
    "openai._base_client",
    "openai._utils",
    "stainless",
    "stainless._base_client",
    # HTTP client debug can be extremely verbose in dev
    "httpx",
    "httpcore",
):
    logging.getLogger(_noisy_mod).setLevel(logging.WARNING)

# Suppress SSE ping/chunk debug logs (sse-starlette healthchecks)
logging.getLogger("sse_starlette").setLevel(logging.WARNING)

# Suppress Uvicorn access logs (healthchecks and routine requests)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
# --------------------------------------------------------------------------

# Create the FastAPI app
# ---------------------------------------------------------------------------
# FastAPI application instance
# ---------------------------------------------------------------------------

# Ensure ./static directory exists before mounting.  `StaticFiles` raises at
# runtime if the path is missing, which would break unit-tests that import the
# app without running the server process.

# In Docker, we're at /app, so static should be /app/static
# In local dev, we're at repo/backend/zerg, so static should be repo/static
if Path("/app").exists() and Path(__file__).resolve().parent.parent == Path("/app"):
    # Docker environment: /app/zerg/main.py -> /app/static
    BASE_DIR = Path("/app")
else:
    # Local environment: repo/backend/zerg/main.py -> repo/static
    BASE_DIR = Path(__file__).resolve().parent.parent.parent  # repo root

STATIC_DIR = BASE_DIR / "static"
AVATARS_DIR = STATIC_DIR / "avatars"

# Create folders on import so they are there in tests and dev.
AVATARS_DIR.mkdir(parents=True, exist_ok=True)

# Set up logging early for lifespan handler
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle application startup and shutdown lifecycle."""
    # Startup phase
    try:
        # Create DB tables if they don't exist
        initialize_database()

        # Enforce PostgreSQL-only runtime for simplicity and correctness
        try:
            from zerg.database import default_engine

            if not _settings.testing and default_engine.dialect.name != "postgresql":  # pragma: no cover - caught in tests via conftest
                raise RuntimeError("PostgreSQL is required to run the backend (advisory locks, concurrency).")
        except Exception as _e:
            logger.error(str(_e))
            raise
        logger.info("Database tables initialized")

        # Auto-seed user context and credentials (idempotent)
        # Loads from scripts/*.local.json or ~/.config/zerg/*.json
        if not _settings.testing:
            try:
                from zerg.services.auto_seed import run_auto_seed

                seed_results = run_auto_seed()
                logger.info(f"Auto-seed complete: {seed_results}")
            except Exception as e:
                logger.warning(f"Auto-seed failed (non-fatal): {e}")

        # Initialize agent state recovery system
        if not _settings.testing:
            from zerg.services.agent_state_recovery import initialize_agent_state_system

            recovery_result = await initialize_agent_state_system()
            if recovery_result["recovered_agents"]:
                logger.info(f"Recovered {len(recovery_result['recovered_agents'])} stuck agents during startup")

        # Start shared async runner
        from zerg.utils.async_runner import get_shared_runner

        get_shared_runner().start()

        # Start core background services
        if not _settings.testing:
            started: list[str] = []
            failed: list[str] = []

            # Scheduler
            try:
                await scheduler_service.start()
                started.append("scheduler")
            except Exception as e:  # noqa: BLE001
                failed.append(f"scheduler ({e})")
                logger.exception("Failed to start scheduler_service")

            # Ops events bridge (SSE/WebSocket bridge)
            try:
                ops_events_bridge.start()
                started.append("ops_events_bridge")
            except Exception as e:  # noqa: BLE001
                failed.append(f"ops_events_bridge ({e})")
                logger.exception("Failed to start ops_events_bridge")

            # Watch renewal service for Gmail connectors
            try:
                from zerg.services.watch_renewal_service import watch_renewal_service

                await watch_renewal_service.start()
                started.append("watch_renewal")
            except Exception as e:  # noqa: BLE001
                failed.append(f"watch_renewal ({e})")
                logger.exception("Failed to start watch_renewal_service")

            # Worker job processor (critical for supervisor workers)
            try:
                from zerg.services.worker_job_processor import worker_job_processor

                await worker_job_processor.start()
                started.append("worker_job_processor")
            except Exception as e:  # noqa: BLE001
                failed.append(f"worker_job_processor ({e})")
                logger.exception("Failed to start worker_job_processor")

            if failed:
                logger.warning(
                    "Background services partial startup: started=%s failed=%s",
                    started,
                    failed,
                )
            else:
                logger.info("Background services started: %s", started)

        logger.info("Application startup complete")
    except Exception as e:
        logger.error(f"Error during startup: {e}")

    yield  # Application is running

    # Shutdown phase
    try:
        # Stop background services
        if not _settings.testing:
            # Stop each service independently so one failure doesn't block others.
            try:
                await scheduler_service.stop()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop scheduler_service")

            try:
                ops_events_bridge.stop()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop ops_events_bridge")

            try:
                from zerg.services.watch_renewal_service import watch_renewal_service

                await watch_renewal_service.stop()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop watch_renewal_service")

            try:
                from zerg.services.worker_job_processor import worker_job_processor

                await worker_job_processor.stop()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop worker_job_processor")

        # Stop shared async runner
        from zerg.utils.async_runner import get_shared_runner

        get_shared_runner().stop()

        # Shutdown websocket manager
        from zerg.websocket.manager import topic_manager

        await topic_manager.shutdown()

        logger.info("Background services stopped")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")


# Create FastAPI APP with lifespan handler
app = FastAPI(redirect_slashes=True, lifespan=lifespan)


# ========================================================================
# OPENAPI SCHEMA EXPORT - Phase 1 of Contract Enforcement
# ========================================================================
def custom_openapi():
    """Generate and export OpenAPI schema for contract enforcement."""
    if app.openapi_schema:
        return app.openapi_schema

    import json

    from fastapi.openapi.utils import get_openapi

    openapi_schema = get_openapi(
        title="Zerg Agent Platform API",
        version="1.0.0",
        description="Complete REST API specification for the Zerg Agent Platform. "
        "This schema is the single source of truth for frontend-backend contracts.",
        routes=app.routes,
    )

    # Add server information
    openapi_schema["servers"] = [
        {"url": "http://localhost:8001", "description": "Development server"},
        {"url": "https://api.zerg.ai", "description": "Production server"},
    ]

    # Export schema to file for CI consumption
    try:
        # Single source of truth: apps/zerg/openapi.json (used by frontend typegen + CI checks)
        schema_path = Path(__file__).parent.parent.parent / "openapi.json"

        with open(schema_path, "w") as f:
            json.dump(openapi_schema, f, indent=2)
            f.write("\n")

        print(f"✅ OpenAPI schema exported to {schema_path}")
    except Exception as e:
        print(f"⚠️  Could not export OpenAPI schema: {e}")

    app.openapi_schema = openapi_schema
    return app.openapi_schema


# Set the custom OpenAPI generator
app.openapi = custom_openapi


# Add CORS middleware with all necessary headers
# ------------------------------------------------------------------
# CORS – open wildcard in dev/tests, restricted in production unless env
# overrides it.  `ALLOWED_CORS_ORIGINS` can contain a comma-separated list.
# ------------------------------------------------------------------

if _settings.auth_disabled:
    # Dev mode: Allow localhost and 127.0.0.1 origins for development/e2e tests
    # Include both http and https variants for flexibility
    cors_origins = [
        # localhost variants
        "http://localhost:30080",
        "http://localhost:8080",
        "http://localhost:5173",
        "https://localhost:30080",
        # 127.0.0.1 variants (used by Playwright/e2e runners)
        "http://127.0.0.1:30080",
        "http://127.0.0.1:8080",
        "http://127.0.0.1:5173",
    ]
else:
    cors_origins_env = _settings.allowed_cors_origins
    if cors_origins_env.strip():
        cors_origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()]
    else:
        # Prod with no explicit ALLOWED_CORS_ORIGINS - warn and use restrictive default
        logger.warning(
            "ALLOWED_CORS_ORIGINS is not set with auth enabled. "
            "CORS will only allow http://localhost:30080. "
            "Set ALLOWED_CORS_ORIGINS=https://your-domain.com for production."
        )
        cors_origins = ["http://localhost:30080"]

# Custom exception handler to ensure CORS headers are included in error responses
from fastapi import Request
from fastapi.responses import JSONResponse


@app.exception_handler(Exception)
async def ensure_cors_on_errors(request: Request, exc: Exception):
    """Ensure CORS headers are included even in error responses."""
    # If the underlying HTTP protocol is already broken (e.g. h11 raised while
    # sending the response body), attempting to send a JSON error response can
    # trigger additional protocol errors. Let Uvicorn close the connection.
    try:  # pragma: no cover – depends on runtime transport
        from h11._util import LocalProtocolError  # type: ignore

        if isinstance(exc, LocalProtocolError):
            raise exc
    except Exception:
        # h11 may not be importable in some environments; ignore.
        pass

    # Log the actual error for debugging
    logger.error(f"Unhandled exception: {exc}", exc_info=True)

    # Determine allowed origin strictly (no wildcard fallback in prod).
    origin = request.headers.get("origin")
    headers = {"Vary": "Origin"}
    if origin and ("*" in cors_origins or origin in cors_origins):
        headers.update(
            {
                "Access-Control-Allow-Origin": origin if "*" not in cors_origins else "*",
                "Access-Control-Allow-Credentials": "true",  # Match middleware config for cookie auth
                "Access-Control-Allow-Methods": "*",
                "Access-Control-Allow-Headers": "*",
            }
        )

    return JSONResponse(status_code=500, content={"detail": "Internal server error"}, headers=headers)


app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,  # Required for cookie-based auth (dev login, session cookies)
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# Mount /static for avatars (and any future assets served by the backend)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ---------------------------------------------------------------------------
# Playwright worker database isolation – attach middleware early so every
# request, including those made during router setup, carries the correct
# context.
# ---------------------------------------------------------------------------

# We import lazily so local *unit-tests* that do not include the middleware
# file in their truncated import tree continue to work.
from importlib import import_module

try:
    WorkerDBMiddleware = getattr(import_module("zerg.middleware.worker_db"), "WorkerDBMiddleware")
    app.add_middleware(WorkerDBMiddleware)
except Exception:  # pragma: no cover – keep startup resilient
    # Defer logging until *logger* is available (defined right below).
    pass

# Include our API routers with centralized prefixes
app.include_router(agents_router, prefix=f"{API_PREFIX}{AGENTS_PREFIX}")
app.include_router(mcp_servers_router, prefix=f"{API_PREFIX}")  # MCP servers nested under agents
app.include_router(threads_router, prefix=f"{API_PREFIX}{THREADS_PREFIX}")
app.include_router(models_router, prefix=f"{API_PREFIX}{MODELS_PREFIX}")
app.include_router(websocket_router, prefix=API_PREFIX)
app.include_router(admin_router, prefix=API_PREFIX)
app.include_router(email_webhook_router, prefix=f"{API_PREFIX}")
app.include_router(pubsub_webhook_router, prefix=f"{API_PREFIX}")
app.include_router(connectors_router, prefix=f"{API_PREFIX}")
app.include_router(triggers_router, prefix=f"{API_PREFIX}")
app.include_router(knowledge_router, prefix=f"{API_PREFIX}")
app.include_router(runs_router, prefix=f"{API_PREFIX}")
app.include_router(runners_router, prefix=f"{API_PREFIX}")  # Runners execution infrastructure
app.include_router(workflows_router, prefix=f"{API_PREFIX}")
app.include_router(workflow_executions_router, prefix=f"{API_PREFIX}")
app.include_router(auth_router, prefix=f"{API_PREFIX}")
app.include_router(oauth_router, prefix=f"{API_PREFIX}")  # OAuth for third-party connectors
app.include_router(users_router, prefix=f"{API_PREFIX}")
app.include_router(templates_router, prefix=f"{API_PREFIX}")
app.include_router(graph_router, prefix=f"{API_PREFIX}")
app.include_router(jarvis_router)  # Jarvis integration - includes /api/jarvis prefix
app.include_router(sync_router)  # Conversation sync - includes /api/jarvis/sync prefix
app.include_router(stream_router)  # Resumable SSE v1 - includes /api/stream prefix
app.include_router(system_router, prefix=API_PREFIX)
app.include_router(metrics_router)  # no prefix – Prometheus expects /metrics
app.include_router(ops_router, prefix=f"{API_PREFIX}")
app.include_router(ops_beacon_router, prefix=f"{API_PREFIX}")  # Public beacon (no auth)
app.include_router(agent_config_router, prefix=f"{API_PREFIX}")
app.include_router(agent_connectors_router, prefix=f"{API_PREFIX}")  # Agent connector credentials
app.include_router(account_connectors_router, prefix=f"{API_PREFIX}")  # Account-level connector credentials
app.include_router(funnel_router, prefix=f"{API_PREFIX}")  # Funnel tracking

# ---------------------------------------------------------------------------
# Legacy admin routes without /api prefix – keep at very end so they override
# nothing and remain an optional convenience for old tests.
# ---------------------------------------------------------------------------

try:
    from zerg.routers.admin import _mount_legacy  # noqa: E402

    _mount_legacy(app)
except ImportError:  # pragma: no cover – should not happen
    pass

# Legacy logging setup (kept to avoid breaking existing comment reference)
# Set up logging
# Note: logger is now defined earlier for lifespan handler usage


# Root endpoint
@app.get("/")
async def read_root():
    """Return a simple message to indicate the API is working."""
    return {"message": "Agent Platform API is running"}


@app.get("/health", operation_id="health_check_get")
@app.head("/health", operation_id="health_check_head", include_in_schema=False)
async def health_check():
    """Health check endpoint with comprehensive system validation."""
    from pathlib import Path

    from sqlalchemy import text

    health_status = {"status": "healthy", "message": "Agent Platform API is running"}
    checks = {}

    # 1. Environment validation
    try:
        settings = get_settings()
        env_issues = []

        if not settings.openai_api_key:
            env_issues.append("OPENAI_API_KEY missing")
        if not settings.database_url:
            env_issues.append("DATABASE_URL missing")
        if not settings.auth_disabled and (not settings.jwt_secret or len(settings.jwt_secret) < 16):
            env_issues.append("JWT_SECRET invalid")

        checks["environment"] = {
            "status": "pass" if not env_issues else "fail",
            "issues": env_issues,
            "database_configured": bool(settings.database_url),
            "auth_enabled": not settings.auth_disabled,
        }
    except Exception as e:
        checks["environment"] = {"status": "fail", "error": str(e)}
        health_status["status"] = "unhealthy"

    # 2. Database connectivity
    try:
        from zerg.database import default_engine

        with default_engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            row = result.fetchone()
            checks["database"] = {
                "status": "pass" if row and row[0] == 1 else "fail",
                "connection": "ok",
                "url": str(default_engine.url).replace(default_engine.url.password or "", "***")
                if default_engine.url.password
                else str(default_engine.url),
            }
    except Exception as e:
        checks["database"] = {"status": "fail", "error": str(e)}
        health_status["status"] = "unhealthy"

    # 3. Migration status
    migration_log_file = Path("/app/static/migration.log")
    migration_status = {"log_exists": migration_log_file.exists(), "log_content": None}

    if migration_log_file.exists():
        try:
            with open(migration_log_file, "r") as f:
                migration_status["log_content"] = f.read()
        except Exception as e:
            migration_status["log_error"] = str(e)

    checks["migration"] = migration_status

    health_status["checks"] = checks
    return health_status


# Favicon endpoint is no longer needed since we use static file in the frontend
# Browsers will go directly to the frontend server for favicon.ico


# Redundant reset-database endpoint removed - use /admin/reset-database instead
