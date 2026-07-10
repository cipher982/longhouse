# E2E log suppression: only active when E2E_LOG_SUPPRESS=1 for test runs

# CRITICAL: Load environment variables FIRST - before ANY other imports that might use os.getenv()
# Use override=False in test/e2e contexts so Node-spawned overrides (ENVIRONMENT, TESTING, etc.)
# are preserved; override=True for normal dev/prod to keep .env authoritative and strip quotes.
import json
import os

from dotenv import load_dotenv

_env = os.getenv("ENVIRONMENT", "").lower()
_testing = os.getenv("TESTING", "").strip().lower() in {"1", "true", "yes", "on"}
_is_test_env = _testing or ("test" in _env) or ("e2e" in _env)

load_dotenv(override=not _is_test_env)

# fmt: off
# ruff: noqa: E402
from zerg.config import get_settings
from zerg.config import resolve_cors_origins
from zerg.config import validate_public_origin_config

_settings = get_settings()

if _settings.e2e_log_suppress:
    from zerg.e2e_logging_hacks import silence_info_logs

    silence_info_logs()

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Logging configuration
from zerg.logging_config import configure_logging

configure_logging(_settings.log_level)

logger = logging.getLogger(__name__)

# Static/frontend paths
if Path("/app").exists() and Path(__file__).resolve().parent.parent == Path("/app"):
    BASE_DIR = Path("/app")
else:
    BASE_DIR = Path(__file__).resolve().parent.parent.parent

STATIC_DIR = BASE_DIR / "static"
AVATARS_DIR = STATIC_DIR / "avatars"
AVATARS_DIR.mkdir(parents=True, exist_ok=True)


def _get_frontend_dist_path() -> tuple[Path | None, str]:
    """Locate the frontend dist directory."""
    try:
        import importlib.resources

        pkg_dist = importlib.resources.files("zerg").joinpath("_frontend_dist")
        dist_path = Path(str(pkg_dist))
        if dist_path.is_dir() and (dist_path / "index.html").exists():
            return dist_path, "bundled"
    except (ImportError, TypeError, AttributeError, FileNotFoundError, OSError):
        pass

    dev_dist = Path(__file__).resolve().parent.parent.parent / "web" / "dist"
    if dev_dist.is_dir() and (dev_dist / "index.html").exists():
        return dev_dist, "local"

    docker_dist = Path("/app/web/dist")
    if docker_dist.is_dir() and (docker_dist / "index.html").exists():
        return docker_dist, "docker"

    return None, "none"


FRONTEND_DIST_DIR, FRONTEND_SOURCE = _get_frontend_dist_path()


def _frontend_static_cache_control(static_file: Path, frontend_dist_dir: Path) -> str:
    """Return cache headers for a concrete frontend static file."""
    try:
        rel = static_file.relative_to(frontend_dist_dir).as_posix()
    except ValueError:
        rel = static_file.name

    if rel.startswith("assets/"):
        return "public, max-age=31536000, immutable"

    return "public, max-age=86400, stale-while-revalidate=604800"

# --- Router imports ---
from zerg.constants import MODELS_PREFIX

# Lifespan
from zerg.lifespan import _enforce_single_tenant_startup  # noqa: F401 — re-exported for tests
from zerg.lifespan import lifespan

# OpenAPI
from zerg.openapi_schema import build_api_openapi_schema
from zerg.openapi_schema import export_openapi_schema
from zerg.routers.admin import router as admin_router
from zerg.routers.admin_bootstrap import router as admin_bootstrap_router
from zerg.routers.agents_backfill import router as agents_backfill_router
from zerg.routers.agents_control import router as agents_control_router
from zerg.routers.agents_demo import router as agents_demo_router
from zerg.routers.agents_ingest import router as agents_ingest_router
from zerg.routers.agents_machine_presence import router as agents_machine_presence_router
from zerg.routers.agents_machines import router as agents_machines_router
from zerg.routers.agents_media import browser_router as media_router
from zerg.routers.agents_media import router as agents_media_router
from zerg.routers.agents_providers import router as agents_providers_router
from zerg.routers.agents_search import router as agents_search_router
from zerg.routers.agents_sessions import router as agents_sessions_router
from zerg.routers.agents_source_lines import router as agents_source_lines_router
from zerg.routers.agents_turns import router as agents_turns_router
from zerg.routers.auth import router as auth_router
from zerg.routers.device_tokens import router as device_tokens_router
from zerg.routers.health import router as health_router
from zerg.routers.health import set_health_app_ref
from zerg.routers.heartbeat import router as heartbeat_router
from zerg.routers.metrics import router as metrics_router
from zerg.routers.models import router as models_router
from zerg.routers.observability import router as observability_router
from zerg.routers.ops import beacon_router as ops_beacon_router
from zerg.routers.ops import router as ops_router
from zerg.routers.permission_gate import router as permission_gate_router
from zerg.routers.presence import router as presence_router
from zerg.routers.runners import router as runners_router
from zerg.routers.runtime import router as runtime_router
from zerg.routers.session_chat import agents_router as agents_session_chat_router
from zerg.routers.session_chat import router as session_chat_router
from zerg.routers.session_inputs_attachments import agents_router as agents_session_inputs_attachments_router
from zerg.routers.session_inputs_attachments import router as session_inputs_attachments_router
from zerg.routers.session_shares import public_router as session_shares_public_router
from zerg.routers.session_shares import router as session_shares_router
from zerg.routers.skills import router as skills_router
from zerg.routers.system import router as system_router
from zerg.routers.telemetry import admin_router as telemetry_admin_router
from zerg.routers.telemetry import beacon_router as telemetry_beacon_router
from zerg.routers.telemetry import canary_router as telemetry_canary_router
from zerg.routers.timeline import canary_stream_router as timeline_canary_stream_router
from zerg.routers.timeline import router as timeline_router
from zerg.routers.timeline import timeline_stream_router
from zerg.routers.users import router as users_router
from zerg.routers.websocket import router as websocket_router
from zerg.services.public_downloads import PublicDownloadUnavailable
from zerg.services.public_downloads import download_macos_desktop_app_response

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(redirect_slashes=True, lifespan=lifespan)
api_app = FastAPI(redirect_slashes=True)


@api_app.middleware("http")
async def isolate_archive_reads(request, call_next):
    """Keep cold SQLite reads outside the hot Runtime Host process."""

    from fastapi.responses import JSONResponse

    from zerg.database import catalog_db_session
    from zerg.database import live_catalog_enabled
    from zerg.dependencies.agents_auth import require_single_tenant
    from zerg.dependencies.agents_auth import verify_agents_token
    from zerg.dependencies.browser_auth import _get_browser_session_user
    from zerg.services.archive_read_proxy import normalized_api_path
    from zerg.services.archive_read_proxy import proxy_archive_read
    from zerg.services.archive_read_proxy import should_proxy_archive_read

    if live_catalog_enabled() and should_proxy_archive_read(request):
        try:
            path = normalized_api_path(request)
            if path.startswith("/agents/"):
                verify_agents_token(request)
                require_single_tenant()
            else:
                with catalog_db_session() as db:
                    if _get_browser_session_user(request, db) is None:
                        raise HTTPException(status_code=401, detail="Not authenticated")
            return await proxy_archive_read(request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
    return await call_next(request)

# Set health app reference for readyz/health endpoints that need app.state
set_health_app_ref(app)


# OpenAPI schema export
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = build_api_openapi_schema(api_app)

    try:
        schema_path = export_openapi_schema(openapi_schema)
        print(f"✅ OpenAPI schema exported to {schema_path}")
    except Exception as e:
        print(f"⚠️  Could not export OpenAPI schema: {e}")

    app.openapi_schema = openapi_schema
    return openapi_schema


app.openapi = custom_openapi

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
cors_origins = resolve_cors_origins(_settings)
if _settings.allowed_cors_origins.strip():
    logger.info(f"CORS configured with explicit origins: {cors_origins}")
elif _settings.auth_disabled:
    logger.info(f"CORS configured for dev defaults: {cors_origins}")
else:
    logger.warning(
        "ALLOWED_CORS_ORIGINS is not set with auth enabled. "
        "CORS is derived from PUBLIC_SITE_URL/APP_PUBLIC_URL if present, "
        "otherwise defaults to localhost."
    )

for warning in validate_public_origin_config(_settings, cors_origins):
    logger.warning(warning)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

if _settings.demo_mode:
    from zerg.middleware.demo_guard import DemoGuardMiddleware

    app.add_middleware(DemoGuardMiddleware)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

from zerg.middleware.no_cache_static import NoCacheStaticMiddleware

app.add_middleware(NoCacheStaticMiddleware)

from zerg.middleware.request_timeout import RequestTimeoutMiddleware

app.add_middleware(RequestTimeoutMiddleware)

from zerg.middleware.test_worker_routing import E2EWorkerRoutingMiddleware

app.add_middleware(E2EWorkerRoutingMiddleware, enabled=_settings.testing)

from zerg.middleware.safe_error_response import SafeErrorResponseMiddleware

app.add_middleware(SafeErrorResponseMiddleware, cors_origins=cors_origins)

# ---------------------------------------------------------------------------
# API routers
# ---------------------------------------------------------------------------
api_app.include_router(models_router, prefix=MODELS_PREFIX)
api_app.include_router(websocket_router)
api_app.include_router(admin_router)
api_app.include_router(admin_bootstrap_router)
api_app.include_router(runners_router)
api_app.include_router(auth_router)
api_app.include_router(users_router)
api_app.include_router(system_router)
api_app.include_router(ops_router)
api_app.include_router(ops_beacon_router)
api_app.include_router(telemetry_beacon_router)
api_app.include_router(telemetry_admin_router)
api_app.include_router(telemetry_canary_router)
api_app.include_router(observability_router)
api_app.include_router(skills_router)
api_app.include_router(session_chat_router)
api_app.include_router(agents_session_chat_router)
api_app.include_router(session_shares_router)
api_app.include_router(session_shares_public_router)
api_app.include_router(session_inputs_attachments_router)
api_app.include_router(agents_session_inputs_attachments_router)
api_app.include_router(timeline_stream_router)
api_app.include_router(timeline_router)
api_app.include_router(timeline_canary_stream_router)
api_app.include_router(agents_control_router)
api_app.include_router(agents_ingest_router)
api_app.include_router(agents_machine_presence_router)
api_app.include_router(agents_machines_router)
api_app.include_router(agents_media_router)
api_app.include_router(media_router)
api_app.include_router(agents_providers_router)
api_app.include_router(agents_search_router)
api_app.include_router(agents_sessions_router)
api_app.include_router(agents_source_lines_router)
api_app.include_router(agents_turns_router)
api_app.include_router(agents_backfill_router)
api_app.include_router(agents_demo_router)
api_app.include_router(heartbeat_router)
api_app.include_router(presence_router)
api_app.include_router(permission_gate_router)
api_app.include_router(runtime_router)
api_app.include_router(device_tokens_router)
api_app.include_router(health_router)

# metrics on parent app (Prometheus expects /metrics at root)
app.include_router(metrics_router)

app.mount("/api", api_app)

# ---------------------------------------------------------------------------
# Dynamic config.js
# ---------------------------------------------------------------------------


@app.get("/config.js", include_in_schema=False)
async def serve_config_js():
    from fastapi.responses import Response

    base_url = _settings.app_public_url or _settings.public_site_url or ""
    ws_scheme = "wss" if base_url.startswith("https") else "ws"
    ws_host = ""
    if base_url:
        from urllib.parse import urlparse as _urlparse

        parsed = _urlparse(base_url)
        ws_host = f"{ws_scheme}://{parsed.netloc}"

    from zerg.models_config import is_capability_available

    _llm_avail_bool = is_capability_available("text")
    _emb_avail_bool = is_capability_available("embedding")
    google_client_id = "" if _settings.control_plane_url else (_settings.google_client_id or "")
    runtime_config = {
        "API_BASE_URL": "/api",
        "WS_BASE_URL": ws_host or "",
        "__APP_MODE__": _settings.app_mode.value,
        "__GOOGLE_CLIENT_ID__": google_client_id,
        # In dev mode (auth disabled), expose landing page by reporting single_tenant=false
        "__SINGLE_TENANT__": False if _settings.auth_disabled else _settings.single_tenant,
        "__LLM_AVAILABLE__": _llm_avail_bool,
        "__EMBEDDINGS_AVAILABLE__": _emb_avail_bool,
        "__UMAMI_WEBSITE_ID__": _settings.umami_website_id or "",
        "__UMAMI_SCRIPT_SRC__": _settings.umami_script_src or "",
        "__UMAMI_DOMAINS__": _settings.umami_domains or "",
        "__UMAMI_TAG__": _settings.umami_tag or "prod",
    }
    js = "".join(f"window.{key}={json.dumps(value)};\n" for key, value in runtime_config.items())
    return Response(
        content=js,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/", include_in_schema=False)
async def read_root():
    if FRONTEND_DIST_DIR is not None:
        from fastapi.responses import FileResponse

        index_path = FRONTEND_DIST_DIR / "index.html"
        if index_path.is_file():
            return FileResponse(
                index_path,
                media_type="text/html",
                headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
            )

    return {"message": "Longhouse API is running"}


@app.get("/download/macos", include_in_schema=False)
async def download_macos_desktop_app():
    try:
        return await download_macos_desktop_app_response()
    except PublicDownloadUnavailable as exc:
        logger.warning("Public macOS download unavailable: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/s/{prefix}", include_in_schema=False)
async def short_session_link(prefix: str):
    """Resolve a short session link (/s/<id-prefix>) to the full timeline URL.

    Lets the CLI print a clean `https://host/s/111a5a5d` instead of the full
    UUID. Session ids are hyphenated CHAR(36) strings, so a left-anchored prefix
    match is exact for the launch case (first 8 hex chars). On an ambiguous or
    missing prefix we fall back to the timeline home rather than guessing.
    """
    from fastapi.responses import RedirectResponse

    from zerg.database import catalog_db_session
    from zerg.database import live_catalog_enabled
    from zerg.models.agents import AgentSession
    from zerg.models.live_store import LiveSessionCatalog

    cleaned = (prefix or "").strip().lower()
    if not cleaned or any(ch not in "0123456789abcdef-" for ch in cleaned):
        return RedirectResponse(url="/timeline", status_code=302)

    session_model = LiveSessionCatalog if live_catalog_enabled() else AgentSession
    session_id_column = session_model.session_id if live_catalog_enabled() else session_model.id
    with catalog_db_session() as db:
        matches = (
            db.query(session_id_column)
            .filter(session_id_column.like(f"{cleaned}%"))
            .limit(2)
            .all()
        )

    if len(matches) == 1:
        return RedirectResponse(url=f"/timeline/{matches[0][0]}", status_code=302)
    # zero matches or an ambiguous prefix -> timeline home, no guessing
    return RedirectResponse(url="/timeline", status_code=302)


@app.get("/s/{prefix}/preview", include_in_schema=False)
async def short_session_link_preview(prefix: str):
    """Public-safe metadata for a short-link session preview.

    Lets the login page tell a logged-out visitor whose session they were
    trying to reach before they sign in. Returns only the provider, device
    label, timing, and owner display info — never transcript, project, cwd,
    summary, or any content-derived field.

    Same prefix resolution rules as /s/{prefix}: zero or ambiguous matches
    return 404 (don't guess, don't leak existence).
    """
    from fastapi.responses import JSONResponse

    from zerg.database import catalog_db_session
    from zerg.database import live_catalog_enabled
    from zerg.models.agents import AgentSession
    from zerg.models.live_store import LiveSessionCatalog
    from zerg.models.user import User

    cleaned = (prefix or "").strip().lower()
    if not cleaned or any(ch not in "0123456789abcdef-" for ch in cleaned):
        raise HTTPException(status_code=404, detail="Session not found")

    session_model = LiveSessionCatalog if live_catalog_enabled() else AgentSession
    session_id_column = session_model.session_id if live_catalog_enabled() else session_model.id
    with catalog_db_session() as db:
        row = (
            db.query(
                session_id_column,
                session_model.provider,
                session_model.device_name,
                session_model.started_at,
                session_model.ended_at,
            )
            .filter(session_id_column.like(f"{cleaned}%"))
            .limit(2)
            .all()
        )
        if len(row) != 1:
            raise HTTPException(status_code=404, detail="Session not found")
        session_row = row[0]
        # Single-tenant model: the one configured user is the owner of every
        # session. Multi-tenant ownership lives in the control plane and is
        # out of scope for the public preview surface.
        owner = (
            db.query(User.display_name, User.email)
            .order_by(User.id.asc())
            .first()
        )

    owner_display_name: str | None = None
    owner_email_local: str | None = None
    if owner is not None:
        owner_display_name = (owner[0] or "").strip() or None
        email = (owner[1] or "").strip()
        if email and "@" in email:
            owner_email_local = email.split("@", 1)[0] or None

    return JSONResponse(
        content={
            "session_id": str(session_row[0]),
            "provider": session_row.provider,
            "device_name": session_row.device_name,
            "started_at": session_row.started_at.isoformat() if session_row.started_at else None,
            "ended_at": session_row.ended_at.isoformat() if session_row.ended_at else None,
            "owner_display_name": owner_display_name,
            "owner_email_local": owner_email_local,
        },
        headers={"Cache-Control": "public, max-age=60"},
    )


# ---------------------------------------------------------------------------
# Frontend static serving (MUST be last - catch-all route)
# ---------------------------------------------------------------------------
if FRONTEND_DIST_DIR is not None:
    _assets_dir = FRONTEND_DIST_DIR / "assets"
    if _assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="frontend_assets")

    app.mount("/frontend-static", StaticFiles(directory=str(FRONTEND_DIST_DIR)), name="frontend_root")

    _frontend_dist_resolved = FRONTEND_DIST_DIR.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def serve_spa(path: str):
        from fastapi.responses import FileResponse
        from fastapi.responses import RedirectResponse

        def _serve_index() -> FileResponse | RedirectResponse:
            index_path = _frontend_dist_resolved / "index.html"
            if index_path.is_file():
                return FileResponse(
                    index_path,
                    media_type="text/html",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
                )
            return RedirectResponse(url="/")

        if ".." in path or path.startswith("/"):
            return _serve_index()

        try:
            static_file = (_frontend_dist_resolved / path).resolve()
            if static_file.is_relative_to(_frontend_dist_resolved) and static_file.is_file():
                return FileResponse(
                    static_file,
                    headers={"Cache-Control": _frontend_static_cache_control(static_file, _frontend_dist_resolved)},
                )
        except (ValueError, OSError):
            pass

        return _serve_index()

    logger.info(f"Frontend catch-all route registered (FRONTEND_DIST_DIR={FRONTEND_DIST_DIR})")
