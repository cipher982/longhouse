"""Health, liveness, and readiness endpoints.

Extracted from main.py — these probe endpoints are logically separate
from the app factory and router registration.
"""

import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from zerg.config import get_settings

router = APIRouter(tags=["health"])

EVENTS_FTS_EXISTS_SQL = "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events_fts' LIMIT 1"
_ARCHIVE_DEGRADABLE_WRITER_LABELS = {
    "archive-primary-manifest",
    "heartbeat-bookkeeping",
    "ingest",
    "ingest-replay",
    "ingest-scan",
    "live-archive-drain",
}


def _write_serializer_stale_active_ms() -> float:
    return float(os.getenv("LONGHOUSE_WRITE_SERIALIZER_STALE_ACTIVE_MS", "300000"))


def _write_serializer_stale_queue_depth() -> int:
    return int(os.getenv("LONGHOUSE_WRITE_SERIALIZER_STALE_QUEUE_DEPTH", "5"))


def _serializer_metrics_check(serializer_getter_name: str) -> tuple[bool, dict]:
    try:
        import zerg.services.write_serializer as write_serializer

        ws = getattr(write_serializer, serializer_getter_name)()
        if not ws.is_configured:
            return False, {"status": "skip", "reason": "not configured"}
        metrics = ws.get_metrics()
        writer_stale = (
            bool(metrics.get("writer_active"))
            and int(metrics.get("queue_depth") or 0) >= _write_serializer_stale_queue_depth()
            and float(metrics.get("active_age_ms") or 0.0) >= _write_serializer_stale_active_ms()
        )
        return writer_stale, {"status": "fail" if writer_stale else "pass", **metrics}
    except Exception as e:
        return False, {"status": "warn", "error": str(e)}


def _archive_degraded_metrics(metrics: dict) -> dict:
    """Project cold archive writer pressure as degraded, not hot-path down."""

    return {
        **metrics,
        "status": "warn",
        "archive_degraded": True,
    }


def _writer_stall_is_archive_degraded(metrics: dict) -> bool:
    label = str(metrics.get("active_label") or "").strip()
    return label in _ARCHIVE_DEGRADABLE_WRITER_LABELS


def _write_serializer_stall_check() -> tuple[bool, dict]:
    return _serializer_metrics_check("get_write_serializer")


def _live_write_serializer_check() -> tuple[bool, dict]:
    return _serializer_metrics_check("get_live_write_serializer")


def _archive_wal_pressure_payload(wal_bytes: int | None) -> dict[str, object]:
    from zerg.services.archive_pressure import evaluate_archive_wal_pressure

    payload = evaluate_archive_wal_pressure(wal_bytes).as_health_payload()
    if payload.get("shed"):
        payload["archive_degraded"] = True
    return payload


def _session_projection_lag_check(session_factory=None) -> dict:
    """Return lag for sessions whose archive ingest skipped derived projections."""
    if session_factory is None:
        from zerg.database import get_session_factory

        session_factory = get_session_factory()

    db = session_factory()
    try:
        row = db.execute(
            text(
                """
                SELECT COUNT(*) AS pending_sessions,
                       MIN(last_activity_at) AS oldest_last_activity_at,
                       MAX(last_activity_at) AS newest_last_activity_at
                FROM sessions
                WHERE COALESCE(needs_projection, 0) = 1
                """
            )
        ).fetchone()
    finally:
        db.close()

    pending_sessions = int(row[0] or 0) if row is not None else 0
    return {
        "status": "pass" if pending_sessions == 0 else "warn",
        "pending_sessions": pending_sessions,
        "oldest_last_activity_at": row[1] if row is not None else None,
        "newest_last_activity_at": row[2] if row is not None else None,
    }


def _session_enrichment_lag_check(session_factory=None) -> dict:
    """Return lag for sessions ingested durably but still waiting on enrichment."""
    if session_factory is None:
        from zerg.database import get_session_factory

        session_factory = get_session_factory()

    db = session_factory()
    try:
        row = db.execute(
            text(
                """
                SELECT COUNT(*) AS pending_sessions,
                       MIN(last_activity_at) AS oldest_last_activity_at,
                       MAX(last_activity_at) AS newest_last_activity_at
                FROM sessions
                WHERE COALESCE(needs_embedding, 1) = 1
                """
            )
        ).fetchone()
    finally:
        db.close()

    pending_sessions = int(row[0] or 0) if row is not None else 0
    return {
        "status": "pass" if pending_sessions == 0 else "warn",
        "pending_sessions": pending_sessions,
        "oldest_last_activity_at": row[1] if row is not None else None,
        "newest_last_activity_at": row[2] if row is not None else None,
    }


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
            from zerg.dependencies.browser_auth import _get_browser_session_user

            user = _get_browser_session_user(request)
            if user is not None and getattr(user, "role", "USER") == "ADMIN":
                return True
        except Exception:
            pass

    return False


@router.get("/health/db", operation_id="health_db_check")
def health_db(request: Request):
    """Database readiness check - verifies critical tables are initialized.

    Returns ready/initializing/error. Schema detail (which table is missing,
    the verified table list) is operator-only; untrusted callers get a bare
    status so this isn't a public schema-disclosure surface.
    """
    from zerg.database import default_engine
    from zerg.database import live_catalog_enabled

    trusted = _request_is_trusted(request)
    catalog_mode = live_catalog_enabled()
    if catalog_mode:
        try:
            from zerg.catalogd.client import call_catalogd_sync
            from zerg.services.catalogd_supervisor import catalogd_paths

            _database_path, catalog_socket = catalogd_paths()
            ping = call_catalogd_sync(catalog_socket, "ping.v2", timeout_seconds=0.05)
            if ping.get("ready") is not True:
                raise RuntimeError("catalog not ready")
            return {"status": "ready", "catalog": ping} if trusted else {"status": "ready"}
        except Exception:
            return JSONResponse(status_code=503, content={"status": "error", "detail": "Catalog connection failed"})

    health_engine = default_engine
    required_tables = (
        ["users", "live_session_catalog", "live_timeline_cards", "live_runtime_state"]
        if catalog_mode
        else ["users", "sessions", "events", "events_fts"]
    )

    try:
        if health_engine is None:
            raise RuntimeError("database engine unavailable")
        with health_engine.connect() as conn:
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
def livez_check():
    """Liveness probe: process is up and serving requests."""
    return {"status": "ok"}


@router.get("/readyz", operation_id="readyz_check_get")
@router.head("/readyz", operation_id="readyz_check_head", include_in_schema=False)
def readyz_check():
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
    from zerg.database import get_live_engine
    from zerg.database import live_catalog_enabled
    from zerg.database import live_store_configured

    single_tenant_violation = getattr(_health_app_ref, "single_tenant_violation", None)
    if single_tenant_violation:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "reason": single_tenant_violation},
        )

    readiness_engine = (get_live_engine() if live_store_configured() else None) or default_engine
    if readiness_engine is None:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "reason": "database engine not initialized"},
        )

    catalogd_ready = False
    if live_catalog_enabled() and not _settings.testing:
        try:
            from zerg.catalogd.client import call_catalogd_sync
            from zerg.catalogd.schema import CATALOG_SCHEMA_GENERATION
            from zerg.catalogd.schema import CATALOG_SCHEMA_VERSION
            from zerg.services.catalogd_supervisor import catalogd_paths

            _database_path, catalog_socket = catalogd_paths()
            ping = call_catalogd_sync(catalog_socket, "ping.v2", timeout_seconds=0.025)
            catalogd_ready = (
                ping.get("ready") is True
                and ping.get("schema_version") == CATALOG_SCHEMA_VERSION
                and ping.get("schema_generation") == CATALOG_SCHEMA_GENERATION
            )
        except Exception:
            catalogd_ready = False
        if not catalogd_ready:
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "reason": "catalog_unavailable"},
            )

    db_url = str(readiness_engine.url)
    if not catalogd_ready and db_url.startswith("sqlite"):
        db_path = db_url.replace("sqlite:///", "").replace("sqlite://", "")
        if not db_path or db_path == ":memory:":
            return {"status": "ok"}
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            try:
                conn.execute("SELECT 1")
                if readiness_engine is default_engine:
                    row = conn.execute(EVENTS_FTS_EXISTS_SQL).fetchone()
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
    elif not catalogd_ready:
        try:
            with readiness_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "reason": "database unavailable"},
            )

    catalog_mode = live_catalog_enabled()
    writer_stale, writer_metrics = (False, {"status": "retired"}) if catalog_mode else _write_serializer_stall_check()
    archive_degraded = writer_stale and _writer_stall_is_archive_degraded(writer_metrics)
    live_writer_stale, live_writer_metrics = _live_write_serializer_check()
    if live_writer_stale:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "reason": "live_write_serializer_stalled",
                "live_write_serializer": live_writer_metrics,
            },
        )
    if archive_degraded:
        return {
            "status": "ready_with_archive_degraded",
            "reason": "archive_write_serializer_stalled",
            "write_serializer": _archive_degraded_metrics(writer_metrics),
        }
    if writer_stale:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "reason": "write_serializer_stalled",
                "write_serializer": writer_metrics,
            },
        )

    try:
        from zerg.database import get_wal_bytes

        archive_wal = _archive_wal_pressure_payload(None if catalog_mode else get_wal_bytes())
        if archive_wal.get("shed"):
            return {
                "status": "ready_with_archive_degraded",
                "reason": "archive_wal_pressure",
                "sqlite_wal": archive_wal,
            }
    except Exception:
        pass

    return {"status": "ok"}


@router.get("/health", operation_id="health_check_get")
@router.head("/health", operation_id="health_check_head", include_in_schema=False)
def health_check(request: Request):
    """Health probe: core dependencies are available.

    Returns HTTP 503 when any critical check fails so monitors and the README
    smoke test (`curl -sf`) correctly treat an unhealthy body as a failure.

    Verbose, infra-revealing detail (DB path, email addresses, migration log,
    env specifics) is included only for trusted callers (loopback, admin
    session, or internal token); public callers get a minimal status body.
    """
    from zerg.build_info import BuildIdentityMissing
    from zerg.build_info import load as load_build_identity
    from zerg.database import live_catalog_enabled

    _settings = get_settings()
    catalog_mode = live_catalog_enabled()
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
    if catalog_mode and not _settings.testing:
        try:
            from zerg.catalogd.client import call_catalogd_sync
            from zerg.services.catalogd_supervisor import catalogd_paths

            _database_path, catalog_socket = catalogd_paths()
            catalog_ping = call_catalogd_sync(catalog_socket, "ping.v2", timeout_seconds=0.05)
            checks["catalogd"] = {
                "status": "pass",
                "ready": catalog_ping.get("ready") is True,
                "schema_version": catalog_ping.get("schema_version"),
                "commit_seq": catalog_ping.get("commit_seq"),
            }
        except Exception as exc:
            checks["catalogd"] = {"status": "fail", "error": type(exc).__name__}
            health_status["status"] = "unhealthy"
            health_status["message"] = "Catalog service is unavailable"
            critical_failure = True

    try:
        from zerg.database import default_engine
        from zerg.database import get_live_engine
        from zerg.database import live_store_configured

        if catalog_mode:
            checks["database"] = {"status": "pass", "connection": "catalogd"}
        else:
            health_engine = (get_live_engine() if live_store_configured() else None) or default_engine
            with health_engine.connect() as conn:
                result = conn.execute(text("SELECT 1"))
                row = result.fetchone()
                db_check = {
                    "status": "pass" if row and row[0] == 1 else "fail",
                    "connection": "ok",
                }
                if trusted:
                    db_check["url"] = (
                        str(health_engine.url).replace(health_engine.url.password or "", "***")
                        if health_engine.url.password
                        else str(health_engine.url)
                    )
                checks["database"] = db_check
    except Exception as e:
        checks["database"] = {"status": "fail", "error": str(e)}
        health_status["status"] = "unhealthy"
        critical_failure = True

    # 2a. Canonical Live Store topology.
    try:
        from zerg.database import get_live_session_factory
        from zerg.database import live_store_configured
        from zerg.services.db_diagnostics import collect_sqlite_store_stats

        live_db = None
        if live_store_configured():
            live_session_factory = get_live_session_factory()
            if live_session_factory is not None:
                live_db = live_session_factory()
        try:
            live_store = collect_sqlite_store_stats(
                _settings.live_database_url,
                archive_database_url=_settings.database_url,
                db=live_db,
            )
        finally:
            if live_db is not None:
                live_db.close()

        live_status = live_store.get("status")
        live_warnings = live_store.get("warnings") or []
        outbox = live_store.get("live_archive_outbox") or {}
        outbox_warn = False
        outbox_reason = None
        if outbox.get("checked") and outbox.get("table_exists"):
            failed_count = outbox.get("failed_count") or 0
            oldest_pending = outbox.get("oldest_pending_created_at")
            if failed_count > 0:
                outbox_warn = True
                outbox_reason = "live_archive_outbox_failures"
            elif oldest_pending is not None:
                try:
                    from datetime import datetime as _dt
                    from datetime import timedelta as _td
                    from datetime import timezone as _tz

                    oldest = _dt.fromisoformat(oldest_pending)
                    if oldest.tzinfo is None:
                        oldest = oldest.replace(tzinfo=_tz.utc)
                    if (_dt.now(_tz.utc) - oldest) > _td(minutes=10):
                        outbox_warn = True
                        outbox_reason = "live_archive_outbox_lagging"
                except (ValueError, TypeError):
                    pass
        receipts = live_store.get("live_input_receipts") or {}
        receipts_warn = False
        receipts_reason = None
        if receipts.get("checked") and receipts.get("table_exists"):
            if int(receipts.get("failed_count") or 0) > 0:
                receipts_warn = True
                receipts_reason = "live_input_receipt_failures"
            elif int(receipts.get("delivering_old_count") or 0) > 0:
                receipts_warn = True
                receipts_reason = "live_input_receipts_stuck_delivering"
            elif int(receipts.get("missing_projection_old_count") or 0) > 0:
                receipts_warn = True
                receipts_reason = "live_input_receipts_projection_lag"
        live_is_warn = live_status == "unsupported" or live_warnings or outbox_warn or receipts_warn
        checks["live_store"] = {
            **live_store,
            "status": "warn" if live_is_warn else "pass",
            "store_status": live_status,
            "outbox_warn_reason": outbox_reason,
            "input_receipts_warn_reason": receipts_reason,
        }
        if outbox_warn and outbox_reason:
            checks["live_store"]["outbox_warn_reason"] = outbox_reason
    except Exception as e:
        checks["live_store"] = {"status": "warn", "error": str(e)}

    # 2b. Request DB pool pressure. This is intentionally passive telemetry:
    # it reads SQLAlchemy pool counters without checking out another connection.
    try:
        from zerg.database import get_live_engine
        from zerg.database import get_pool_status
        from zerg.database import live_catalog_enabled

        pool_status = get_pool_status(get_live_engine() if live_catalog_enabled() else None)
        if pool_status is None:
            checks["db_pool"] = {"status": "skip", "reason": "engine unavailable"}
        else:
            checks["db_pool"] = {
                "status": "warn" if pool_status.get("saturated") else "pass",
                **pool_status,
            }
    except Exception as e:
        checks["db_pool"] = {"status": "warn", "error": str(e)}

    # 3. SQLite FTS5 readiness
    try:
        from zerg.database import default_engine

        if catalog_mode:
            checks["fts5"] = {"status": "skip", "reason": "searchd_owned"}
        elif default_engine is not None and default_engine.dialect.name == "sqlite":
            with default_engine.connect() as conn:
                fts_row = conn.execute(text(EVENTS_FTS_EXISTS_SQL)).fetchone()
                if not fts_row:
                    raise RuntimeError("events_fts table is missing (FTS5 required).")
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
    writer_stale, writer_metrics = (False, {"status": "retired"}) if catalog_mode else _write_serializer_stall_check()
    archive_degraded = writer_stale and _writer_stall_is_archive_degraded(writer_metrics)
    checks["write_serializer"] = _archive_degraded_metrics(writer_metrics) if archive_degraded else writer_metrics
    if archive_degraded:
        if health_status.get("status") == "healthy":
            health_status["status"] = "degraded"
            health_status["message"] = "Archive write serializer is stalled; live lane may remain available"
    elif writer_stale:
        health_status["status"] = "unhealthy"
        health_status["message"] = "Write serializer is stalled"
        critical_failure = True
    _live_writer_stale, live_writer_metrics = _live_write_serializer_check()
    checks["live_write_serializer"] = live_writer_metrics
    if _live_writer_stale:
        health_status["status"] = "unhealthy"
        health_status["message"] = "Live write serializer is stalled"
        critical_failure = True

    # 8. Projection catch-up lag. Archive ingest may skip expensive derived
    # projections on the hot path; this should normally drain quickly in the
    # background and should be visible separately from raw ingest health.
    if catalog_mode:
        checks["session_projection_lag"] = {"status": "skip", "reason": "storage_v2_projectors"}
    else:
        try:
            checks["session_projection_lag"] = _session_projection_lag_check()
        except Exception as e:
            checks["session_projection_lag"] = {"status": "warn", "error": str(e)}

    # 9. Enrichment lag. Embeddings/search enrichment run after durable ingest;
    # they should be visible, but must not be mistaken for raw shipping health.
    if catalog_mode:
        checks["session_enrichment_lag"] = {"status": "skip", "reason": "storage_v2_projectors"}
    else:
        try:
            checks["session_enrichment_lag"] = _session_enrichment_lag_check()
        except Exception as e:
            checks["session_enrichment_lag"] = {"status": "warn", "error": str(e)}

    # 10. SQLite WAL pressure: phase 1 instrumentation. WAL bytes is the cheapest
    # leading indicator of write-side backpressure; the engine's adaptive
    # controller (phase 2) reads this to back off when pressure climbs.
    try:
        from zerg.database import get_live_wal_bytes
        from zerg.database import get_wal_bytes
        from zerg.database import get_wal_checkpoint_metrics

        wal_bytes = get_wal_bytes()
        live_wal_bytes = get_live_wal_bytes()
        checkpoint_metrics = get_wal_checkpoint_metrics()
        wal_check = _archive_wal_pressure_payload(wal_bytes)
        if live_wal_bytes is not None:
            wal_check["live_wal_bytes"] = live_wal_bytes
        if checkpoint_metrics:
            wal_check["checkpoints"] = checkpoint_metrics
        checks["sqlite_wal"] = wal_check
        if wal_check.get("shed") and health_status.get("status") == "healthy":
            health_status["status"] = "degraded"
            health_status["message"] = "Archive WAL pressure is shedding archive ingest; live lane may remain available"
    except Exception as e:
        checks["sqlite_wal"] = {"status": "warn", "error": str(e)}

    health_status["checks"] = checks

    # Untrusted callers get a minimal body: overall status, message, and the
    # build identity ONLY. The commit/version is already public (git history +
    # the image tag) and the deploy verifier reads it from here to confirm a
    # rollout, so it is safe to expose. Everything genuinely sensitive — DB
    # path, email addresses, migration log, env specifics, per-check internals —
    # stays behind the trust gate.
    if not trusted:
        minimal = {
            "status": health_status["status"],
            "message": "Longhouse API is running" if health_status["status"] == "healthy" else "degraded",
        }
        if "build" in health_status:
            minimal["build"] = health_status["build"]
        health_status = minimal

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
