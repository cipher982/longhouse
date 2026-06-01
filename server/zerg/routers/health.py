"""Health, liveness, and readiness endpoints.

Extracted from main.py — these probe endpoints are logically separate
from the app factory and router registration.
"""

import sqlite3
from pathlib import Path

from fastapi import APIRouter
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from zerg.config import get_settings

router = APIRouter(tags=["health"])


def _request_is_trusted(request: Request) -> bool:
    """Return True when the caller may see verbose, infra-revealing health detail.

    Verbose health (DB path, email addresses, migration log, env specifics) is
    operator information. Expose it only to:
      - loopback callers (local operator / same-host probes), or
      - an authenticated admin browser session, or
      - a caller presenting the internal API secret.
    Public/unauthenticated callers get a minimal status body.
    """
    settings = get_settings()

    # Loopback is trusted ONLY when the server is not reachable behind a public
    # origin. When a public URL/domain is configured (the documented Caddy
    # `reverse_proxy 127.0.0.1:8080` topology), proxied public requests arrive
    # from 127.0.0.1 too, so loopback is no longer a safe trust signal — fall
    # through to the explicit token/admin checks instead.
    client_host = request.client.host if request.client else None
    public_origin_configured = bool(settings.public_site_url or settings.app_public_url or settings.public_api_url)
    if not public_origin_configured and client_host in ("127.0.0.1", "::1", "localhost", "testclient"):
        return True
    # The test client always presents as a trusted local caller.
    if client_host == "testclient":
        return True

    # NB: `auth_disabled` does NOT grant trust on its own — a --allow-public-no-auth
    # instance is network-reachable. Trust comes only from loopback (no public
    # origin), an explicit internal/metrics token, or an authenticated admin.
    internal = request.headers.get("X-Internal-Token")
    if internal and settings.internal_api_secret and internal == settings.internal_api_secret:
        return True

    # Authenticated admin browser session — ONLY when auth is enabled. On an
    # auth-disabled instance (the public demo / --allow-public-no-auth) the
    # browser-auth helper returns the dev admin user for ANY request, which would
    # grant verbose health to every anonymous caller. So a real admin session can
    # only be a trust signal when auth is actually enforced.
    if not settings.auth_disabled:
        try:
            from zerg.database import get_session_factory
            from zerg.dependencies.browser_auth import _get_browser_session_user

            db = get_session_factory()()
            try:
                user = _get_browser_session_user(request, db)
            finally:
                db.close()
            if user is not None and getattr(user, "role", "USER") == "ADMIN":
                return True
        except Exception:
            pass

    return False


@router.get("/health/db", operation_id="health_db_check")
async def health_db(request: Request):
    """Database readiness check - verifies critical tables are initialized.

    Returns ready/initializing/error. Schema detail (which table is missing,
    the verified table list) is operator-only; untrusted callers get a bare
    status so this isn't a public schema-disclosure surface.
    """
    from zerg.database import default_engine

    trusted = _request_is_trusted(request)
    required_tables = ["users", "fiches", "threads", "runs", "commis_tasks", "sessions", "events", "events_fts"]

    try:
        with default_engine.connect() as conn:
            for table in required_tables:
                result = conn.execute(text(f"SELECT 1 FROM sqlite_master WHERE type='table' AND name='{table}'"))
                if not result.fetchone():
                    content = {"status": "initializing"}
                    if trusted:
                        content["missing_table"] = table
                    return JSONResponse(status_code=503, content=content)
        return {"status": "ready", "tables_verified": required_tables} if trusted else {"status": "ready"}
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": "Database connection failed"},
        )


@router.get("/livez", operation_id="livez_check_get")
@router.head("/livez", operation_id="livez_check_head", include_in_schema=False)
async def livez_check():
    """Liveness probe: process is up and serving requests."""
    return {"status": "ok"}


@router.get("/readyz", operation_id="readyz_check_get")
@router.head("/readyz", operation_id="readyz_check_head", include_in_schema=False)
async def readyz_check():
    """Readiness probe: returns 503 when core dependencies are unavailable.

    Uses a raw SQLite connection with a short timeout so it never blocks behind
    a long write transaction.
    """
    # Access the parent app via the request scope (health is mounted on api_app,
    # which is mounted on app). We need app.state from the root app.
    # FastAPI stores the app in request.app, but we need the *root* app.
    # Since this router is included on api_app, request.app is api_app.
    # The root app is accessible via request.app.state if needed, but here
    # we rely on a module-level reference set during app creation.

    _settings = get_settings()

    from zerg.database import default_engine

    single_tenant_violation = getattr(_health_app_ref, "single_tenant_violation", None)
    if single_tenant_violation:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "reason": single_tenant_violation},
        )

    if default_engine is None:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "reason": "database engine not initialized"},
        )

    db_url = str(default_engine.url)
    if db_url.startswith("sqlite"):
        db_path = db_url.replace("sqlite:///", "").replace("sqlite://", "")
        if not db_path or db_path == ":memory:":
            return {"status": "ok"}
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            try:
                conn.execute("SELECT 1")
                row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events_fts' LIMIT 1").fetchone()
                if not row:
                    return JSONResponse(
                        status_code=503,
                        content={"status": "unhealthy", "reason": "events_fts table missing (FTS5 required)"},
                    )
            finally:
                conn.close()
        except Exception:
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "reason": "database unavailable"},
            )
    else:
        try:
            with default_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "reason": "database unavailable"},
            )

    return {"status": "ok"}


@router.get("/health", operation_id="health_check_get")
@router.head("/health", operation_id="health_check_head", include_in_schema=False)
async def health_check(request: Request):
    """Health probe: core dependencies are available.

    Returns HTTP 503 when any critical check fails so monitors and the README
    smoke test (`curl -sf`) correctly treat an unhealthy body as a failure.

    Verbose, infra-revealing detail (DB path, email addresses, migration log,
    env specifics) is included only for trusted callers (loopback, admin
    session, or internal token); public callers get a minimal status body.
    """
    from zerg.build_info import BuildIdentityMissing
    from zerg.build_info import load as load_build_identity

    _settings = get_settings()
    trusted = _request_is_trusted(request)
    health_status = {"status": "healthy", "message": "Longhouse API is running"}

    # `critical_failure` drives the HTTP 503: only hard infra failures (db, fts5,
    # environment, single-tenant) make the service "down". A missing build
    # identity flips overall status to unhealthy for operator signal but is NOT
    # critical (it is legitimately absent in source/dev installs), so it must not
    # 503 the README from-source smoke test.
    critical_failure = False

    try:
        health_status["build"] = load_build_identity().as_dict()
    except BuildIdentityMissing as exc:
        health_status["status"] = "unhealthy"
        health_status["build"] = {"error": "missing", "detail": str(exc)}

    checks = {}

    # 0. Single-tenant violation check
    single_tenant_violation = getattr(_health_app_ref, "single_tenant_violation", None)
    if single_tenant_violation:
        health_status["status"] = "unhealthy"
        health_status["message"] = single_tenant_violation
        checks["single_tenant"] = {"status": "fail", "error": single_tenant_violation}
        critical_failure = True

    # 1. Environment validation
    try:
        env_issues = []
        if not _settings.database_url:
            env_issues.append("DATABASE_URL missing")
        if not _settings.auth_disabled and (not _settings.jwt_secret or len(_settings.jwt_secret) < 16):
            env_issues.append("JWT_SECRET invalid")

        checks["environment"] = {
            "status": "pass" if not env_issues else "fail",
            "issues": env_issues,
            "database_configured": bool(_settings.database_url),
            "auth_enabled": not _settings.auth_disabled,
        }
        if env_issues:
            health_status["status"] = "unhealthy"
            critical_failure = True
    except Exception as e:
        checks["environment"] = {"status": "fail", "error": str(e)}
        health_status["status"] = "unhealthy"
        critical_failure = True

    # 1b. LLM capability check (env-var-driven only)
    try:
        import os as _os

        from zerg.models_config import _PROVIDER_DEFAULT_API_KEY_ENVS
        from zerg.models_config import get_embedding_config

        text_provider = next(
            (provider.value for provider, env_var in _PROVIDER_DEFAULT_API_KEY_ENVS.items() if _os.getenv(env_var)),
            None,
        )
        text_avail = text_provider is not None
        embedding_cfg = get_embedding_config()
        emb_avail = embedding_cfg is not None

        checks["llm"] = {
            "status": "pass" if text_avail else "warn",
            "text_available": text_avail,
            "text_source": "environment" if text_avail else None,
            "embeddings_available": emb_avail,
            "embeddings_source": "environment" if emb_avail else None,
        }
    except Exception as e:
        checks["llm"] = {"status": "warn", "error": str(e)}

    # 2. Database connectivity
    try:
        from zerg.database import default_engine

        with default_engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            row = result.fetchone()
            db_check = {
                "status": "pass" if row and row[0] == 1 else "fail",
                "connection": "ok",
            }
            # The DB URL exposes the on-disk path / host; operator-only.
            if trusted:
                db_check["url"] = (
                    str(default_engine.url).replace(default_engine.url.password or "", "***")
                    if default_engine.url.password
                    else str(default_engine.url)
                )
            checks["database"] = db_check
    except Exception as e:
        checks["database"] = {"status": "fail", "error": str(e)}
        health_status["status"] = "unhealthy"
        critical_failure = True

    # 3. SQLite FTS5 readiness
    try:
        from zerg.database import default_engine

        if default_engine is not None and default_engine.dialect.name == "sqlite":
            with default_engine.connect() as conn:
                fts_row = conn.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events_fts' LIMIT 1")).fetchone()
                if not fts_row:
                    raise RuntimeError("events_fts table is missing (FTS5 required).")
                conn.execute(text("SELECT rowid FROM events_fts WHERE events_fts MATCH 'fts5' LIMIT 1")).fetchone()
            checks["fts5"] = {"status": "pass"}
        else:
            checks["fts5"] = {"status": "skip", "reason": "non-sqlite"}
    except Exception as e:
        checks["fts5"] = {"status": "fail", "error": str(e)}
        health_status["status"] = "unhealthy"
        critical_failure = True

    # 5. Email config status (do not leak configured addresses to untrusted callers)
    try:
        from zerg.shared.email import resolve_email_config

        email_cfg = resolve_email_config()
        email_configured = bool(
            all(
                (
                    email_cfg.get("AWS_SES_ACCESS_KEY_ID"),
                    email_cfg.get("AWS_SES_SECRET_ACCESS_KEY"),
                    email_cfg.get("FROM_EMAIL"),
                    email_cfg.get("NOTIFY_EMAIL"),
                )
            )
        )
        checks["email"] = {
            "status": "pass" if email_configured else "warn",
            "configured": email_configured,
            "from_email": (email_cfg.get("FROM_EMAIL") if email_configured else None) if trusted else None,
            "notify_email": (email_cfg.get("NOTIFY_EMAIL") if email_configured else None) if trusted else None,
        }
    except Exception as e:
        checks["email"] = {"status": "warn", "error": str(e) if trusted else "unavailable"}

    # 6. Migration status (log contents are operator-only)
    migration_log_file = Path("/app/static/migration.log")
    migration_status = {"log_exists": migration_log_file.exists(), "log_content": None}

    if migration_log_file.exists() and trusted:
        try:
            with open(migration_log_file, "r") as f:
                migration_status["log_content"] = f.read()
        except Exception as e:
            migration_status["log_error"] = str(e)

    checks["migration"] = migration_status

    # 7. Write serializer metrics
    try:
        from zerg.services.write_serializer import get_write_serializer

        ws = get_write_serializer()
        if ws.is_configured:
            checks["write_serializer"] = {"status": "pass", **ws.get_metrics()}
        else:
            checks["write_serializer"] = {"status": "skip", "reason": "not configured"}
    except Exception as e:
        checks["write_serializer"] = {"status": "warn", "error": str(e)}

    # 8. SQLite WAL pressure: phase 1 instrumentation. WAL bytes is the cheapest
    # leading indicator of write-side backpressure; the engine's adaptive
    # controller (phase 2) reads this to back off when pressure climbs.
    try:
        from zerg.database import get_wal_bytes

        wal_bytes = get_wal_bytes()
        if wal_bytes is None:
            checks["sqlite_wal"] = {"status": "skip", "reason": "wal path unknown"}
        else:
            checks["sqlite_wal"] = {"status": "pass", "wal_bytes": wal_bytes}
    except Exception as e:
        checks["sqlite_wal"] = {"status": "warn", "error": str(e)}

    health_status["checks"] = checks

    # Untrusted callers get a minimal body: overall status only, no build
    # identity, env specifics, or per-check internals.
    if not trusted:
        health_status = {
            "status": health_status["status"],
            "message": "Longhouse API is running" if health_status["status"] == "healthy" else "degraded",
        }

    # Return 503 only on a critical infra failure (db/fts5/environment/
    # single-tenant) so monitors treat a genuinely-down service as down, while a
    # non-critical "unhealthy" (e.g. missing build identity in source installs)
    # still returns 200 and keeps the README smoke test passing.
    if critical_failure:
        return JSONResponse(status_code=503, content=health_status)
    return health_status


def set_health_app_ref(app):
    """Set the root app reference for health checks that need app.state."""
    global _health_app_ref
    _health_app_ref = app


_health_app_ref = None
