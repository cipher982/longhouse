"""Health, liveness, and readiness endpoints.

Extracted from main.py — these probe endpoints are logically separate
from the app factory and router registration.
"""

import sqlite3
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from zerg.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health/db", operation_id="health_db_check")
async def health_db():
    """Database readiness check - verifies critical tables are initialized."""
    from zerg.database import default_engine

    required_tables = ["users", "fiches", "threads", "runs", "commis_tasks", "sessions", "events", "events_fts"]

    try:
        with default_engine.connect() as conn:
            for table in required_tables:
                result = conn.execute(text(f"SELECT 1 FROM sqlite_master WHERE type='table' AND name='{table}'"))
                if not result.fetchone():
                    return JSONResponse(
                        status_code=503,
                        content={"status": "initializing", "missing_table": table},
                    )
        return {"status": "ready", "tables_verified": required_tables}
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
        except Exception as exc:
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "reason": f"database: {exc}"},
            )
    else:
        try:
            with default_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "reason": f"database: {exc}"},
            )

    return {"status": "ok"}


@router.get("/health", operation_id="health_check_get")
@router.head("/health", operation_id="health_check_head", include_in_schema=False)
async def health_check():
    """Readiness probe: core dependencies are available."""
    from zerg.build_info import BuildIdentityMissing
    from zerg.build_info import load as load_build_identity

    _settings = get_settings()
    health_status = {"status": "healthy", "message": "Longhouse API is running"}

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
    except Exception as e:
        checks["environment"] = {"status": "fail", "error": str(e)}
        health_status["status"] = "unhealthy"

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

    # 5. Email config status
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
            "from_email": email_cfg.get("FROM_EMAIL") if email_configured else None,
            "notify_email": email_cfg.get("NOTIFY_EMAIL") if email_configured else None,
        }
    except Exception as e:
        checks["email"] = {"status": "warn", "error": str(e)}

    # 6. Migration status
    migration_log_file = Path("/app/static/migration.log")
    migration_status = {"log_exists": migration_log_file.exists(), "log_content": None}

    if migration_log_file.exists():
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
    return health_status


def set_health_app_ref(app):
    """Set the root app reference for health checks that need app.state."""
    global _health_app_ref
    _health_app_ref = app


_health_app_ref = None
