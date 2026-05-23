"""Application lifespan (startup/shutdown) management.

Extracted from main.py — keeps the app factory lean.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI

from zerg.config import get_settings
from zerg.database import initialize_database
from zerg.observability import configure_observability
from zerg.observability import shutdown_observability
from zerg.services.ops_events import ops_events_bridge
from zerg.services.scheduler_service import scheduler_service

_settings = get_settings()
logger = logging.getLogger(__name__)


@contextmanager
def _timed_startup_step(name: str):
    started = time.monotonic()
    logger.info("Startup step starting: %s", name)
    try:
        yield
    finally:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info("Startup step complete: %s elapsed_ms=%.1f", name, elapsed_ms)


def _enforce_single_tenant_startup(app: FastAPI) -> None:
    """Validate and bootstrap the single-tenant owner or fail fast."""
    if not _settings.single_tenant or _settings.testing:
        return

    from zerg.database import db_session
    from zerg.services.single_tenant import SingleTenantViolation
    from zerg.services.single_tenant import bootstrap_owner_user
    from zerg.services.single_tenant import validate_single_tenant
    from zerg.services.single_tenant import validate_single_tenant_config

    config_error = validate_single_tenant_config()
    if config_error:
        app.state.single_tenant_violation = config_error
        logger.error("Single-tenant config error: %s", config_error)
        raise RuntimeError(config_error)

    try:
        with db_session() as db:
            validate_single_tenant(db)
            bootstrap_owner_user(db)
    except SingleTenantViolation as exc:
        app.state.single_tenant_violation = str(exc)
        logger.error(str(exc))
        raise
    except Exception as exc:
        message = f"Bootstrap failed: {exc}"
        app.state.single_tenant_violation = message
        logger.error("Single-tenant bootstrap failed: %s", exc)
        raise RuntimeError(message) from exc


def _validate_models_config_startup() -> None:
    """Fail fast when configured model providers are missing required keys."""
    if _settings.testing or _settings.llm_disabled or _settings.demo_mode:
        return

    from zerg.models_config import iter_required_provider_keys
    from zerg.models_config import validate_startup_config

    validate_startup_config()
    for env_var, scope, model_id, provider in iter_required_provider_keys():
        logger.info(
            "Models config OK: %s -> provider=%s model=%s key_env=%s",
            scope,
            provider,
            model_id,
            env_var,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle application startup and shutdown lifecycle."""
    startup_started = time.monotonic()
    try:
        with _timed_startup_step("configure_observability"):
            configure_observability()
        with _timed_startup_step("initialize_database"):
            initialize_database()

        from zerg.database import configure_write_serializer

        with _timed_startup_step("configure_write_serializer"):
            configure_write_serializer()

        try:
            from zerg.database import default_engine

            if not _settings.testing and default_engine is not None and default_engine.dialect.name == "sqlite":
                logger.info(
                    "SQLite mode: single-writer serializer active. "
                    "See VISION.md (Architecture Constraints / SQLite-only core) for details."
                )
        except Exception as _e:
            logger.error(str(_e))
            raise
        logger.info("Database tables initialized")

        # SessionInput reconciliation: any row stuck in `delivering` at boot
        # means a prior process died mid-dispatch. Rewind to queued, then
        # best-effort drain idle sessions so recovered queued rows don't sit
        # indefinitely waiting for the next terminal release.
        if not _settings.testing:
            try:
                with _timed_startup_step("session_input_reconciliation"):
                    from zerg.database import get_session_factory
                    from zerg.services.session_inputs import INPUT_STATUS_QUEUED
                    from zerg.services.session_inputs import requeue_stuck_delivering

                    session_factory = get_session_factory()
                    with session_factory() as db:
                        requeue_stuck_delivering(db)

                        from zerg.models.agents import SessionInput

                        queued_session_ids = [
                            row[0]
                            for row in (
                                db.query(SessionInput.session_id)
                                .filter(SessionInput.status == INPUT_STATUS_QUEUED)
                                .distinct()
                                .all()
                            )
                        ]

                if queued_session_ids:
                    from zerg.database import default_engine
                    from zerg.services.session_chat_impl import _drain_next_queued_input

                    async def _boot_drain_all() -> None:
                        for sid in queued_session_ids:
                            try:
                                await _drain_next_queued_input(
                                    db_bind=default_engine,
                                    session_id=sid,
                                    lock_scope_id=str(sid),
                                )
                            except Exception:
                                logger.exception(
                                    "Boot drain failed for session %s (non-fatal)",
                                    sid,
                                )

                    import asyncio as _asyncio

                    _asyncio.create_task(_boot_drain_all())
            except Exception as exc:
                logger.warning(f"SessionInput reconciliation failed (non-fatal): {exc}")
        try:
            url = default_engine.url
            masked = str(url).replace(url.password or "", "***") if url.password else str(url)
            logger.info("Database bound to: %s", masked)
        except Exception:
            pass

        with _timed_startup_step("fts5_readiness_check"):
            try:
                from sqlalchemy import text

                from zerg.database import default_engine

                if default_engine is not None and default_engine.dialect.name == "sqlite":
                    with default_engine.connect() as conn:
                        fts_row = conn.execute(
                            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events_fts' LIMIT 1")
                        ).fetchone()
                        if not fts_row:
                            raise RuntimeError("events_fts table is missing (FTS5 required).")
                        conn.execute(
                            text("SELECT rowid FROM events_fts WHERE events_fts MATCH 'fts5' LIMIT 1")
                        ).fetchone()
            except Exception as fts_error:
                app.state.fts_violation = str(fts_error)
                logger.error(f"FTS5 readiness check failed: {fts_error}")
                raise

        with _timed_startup_step("single_tenant_startup"):
            _enforce_single_tenant_startup(app)

        # Prefetch SSO signing keys
        if not _settings.testing:
            from zerg.services.sso_keys import prefetch_sso_keys

            with _timed_startup_step("prefetch_sso_keys"):
                prefetch_sso_keys()

        # Auto-seed
        if not _settings.testing:
            try:
                from zerg.services.auto_seed import run_auto_seed

                with _timed_startup_step("auto_seed"):
                    seed_results = run_auto_seed()
                logger.info(f"Auto-seed complete: {seed_results}")
            except Exception as e:
                logger.warning(f"Auto-seed failed (non-fatal): {e}")

        # Demo session seeding
        if _settings.demo_mode and not _settings.testing:
            try:
                from zerg.database import get_session_factory
                from zerg.services.demo_seed import seed_missing_demo_sessions

                with _timed_startup_step("demo_seed"):
                    session_factory = get_session_factory()
                    with session_factory() as db:
                        seeded_count, failed_count = seed_missing_demo_sessions(db)
                    if seeded_count > 0:
                        logger.info("Demo mode: seeded %d demo sessions", seeded_count)
                    elif failed_count > 0:
                        logger.warning(
                            "Demo mode: demo seed had %d failures (see per-session errors above)",
                            failed_count,
                        )
                    else:
                        logger.info("Demo mode: demo sessions already present, skipping seed")
            except Exception as e:
                logger.warning(f"Demo mode auto-seed failed (non-fatal): {e}")

        # External jobs repo
        if not _settings.testing:
            try:
                from zerg.jobs.registry import should_load_manifest_jobs

                if should_load_manifest_jobs():
                    from zerg.services.jobs_repo import bootstrap_jobs_repo
                    from zerg.services.jobs_repo import install_jobs_deps

                    with _timed_startup_step("jobs_repo_bootstrap"):
                        jobs_result = bootstrap_jobs_repo(_settings.data_dir)
                    if jobs_result["errors"]:
                        logger.warning(f"Jobs repo bootstrap had errors: {jobs_result['errors']}")
                    elif jobs_result["created"] or jobs_result["initialized_git"]:
                        logger.info(f"Jobs repo bootstrapped: {jobs_result['jobs_dir']}")

                    with _timed_startup_step("jobs_deps_install"):
                        deps_result = install_jobs_deps(_settings.data_dir)
                    if deps_result.get("error"):
                        logger.warning(f"Job deps install failed (non-fatal): {deps_result['error']}")
                    elif deps_result.get("installed"):
                        logger.info("Job dependencies installed from requirements.txt")
                else:
                    logger.info("Skipping jobs repo bootstrap — builtin-only mode")
            except Exception as e:
                logger.warning(f"Jobs repo bootstrap failed (non-fatal): {e}")

        # Fiche state recovery
        if not _settings.testing:
            from zerg.services.fiche_state_recovery import initialize_fiche_state_system

            with _timed_startup_step("fiche_state_recovery"):
                await initialize_fiche_state_system()

        with _timed_startup_step("models_config_validation"):
            _validate_models_config_startup()

        # Shared async runner
        from zerg.utils.async_runner import get_shared_runner

        get_shared_runner().start()

        # Core background services
        if not _settings.testing:
            started: list[str] = []
            failed: list[str] = []

            try:
                with _timed_startup_step("scheduler_service_start"):
                    await scheduler_service.start()
                started.append("scheduler")
            except Exception as e:  # noqa: BLE001
                failed.append(f"scheduler ({e})")
                logger.exception("Failed to start scheduler_service")

            try:
                with _timed_startup_step("ops_events_bridge_start"):
                    ops_events_bridge.start()
                started.append("ops_events_bridge")
            except Exception as e:  # noqa: BLE001
                failed.append(f"ops_events_bridge ({e})")
                logger.exception("Failed to start ops_events_bridge")

            try:
                from zerg.services.watch_renewal_service import watch_renewal_service

                with _timed_startup_step("watch_renewal_start"):
                    await watch_renewal_service.start()
                started.append("watch_renewal")
            except Exception as e:  # noqa: BLE001
                failed.append(f"watch_renewal ({e})")
                logger.exception("Failed to start watch_renewal_service")

            # Remote launch reaper: orphan expired launch rows.
            try:
                from zerg.database import get_session_factory as _get_sf
                from zerg.services.remote_session_launch import reap_orphaned_launches

                async def _launch_reaper_loop() -> None:
                    while True:
                        try:
                            await asyncio.sleep(60)
                            db = _get_sf()()
                            try:
                                reap_orphaned_launches(db)
                            finally:
                                db.close()
                        except asyncio.CancelledError:
                            raise
                        except Exception:  # noqa: BLE001
                            logger.exception("Remote launch reaper tick failed")

                asyncio.create_task(_launch_reaper_loop())
                started.append("remote_launch_reaper")
            except Exception as e:  # noqa: BLE001
                failed.append(f"remote_launch_reaper ({e})")
                logger.exception("Failed to start remote_launch_reaper")

            # Image attachment blob reaper: drops blobs whose parent session_input
            # is in a terminal state and older than the retention window.
            try:
                from zerg.database import get_session_factory as _get_sf_attach
                from zerg.services.session_input_attachments import cleanup_stale_blobs

                async def _attachment_cleanup_loop() -> None:
                    while True:
                        try:
                            await asyncio.sleep(3600)
                            db = _get_sf_attach()()
                            try:
                                cleanup_stale_blobs(db)
                            finally:
                                db.close()
                        except asyncio.CancelledError:
                            raise
                        except Exception:  # noqa: BLE001
                            logger.exception("attachment cleanup tick failed")

                asyncio.create_task(_attachment_cleanup_loop())
                started.append("attachment_cleanup")
            except Exception as e:  # noqa: BLE001
                failed.append(f"attachment_cleanup ({e})")
                logger.exception("Failed to start attachment_cleanup")

            # Live session summary/title enrichment. This scans session revision
            # lag directly; it is intentionally separate from the legacy ingest
            # task workers.
            try:
                from zerg.services.session_enrichment_reconciler import run_summary_reconciler

                asyncio.create_task(run_summary_reconciler())
                started.append("summary_reconciler")
            except Exception as e:  # noqa: BLE001
                failed.append(f"summary_reconciler ({e})")
                logger.exception("Failed to start summary_reconciler")

            # Job queue
            if _settings.job_queue_enabled and not _settings.testing:
                try:
                    from apscheduler.schedulers.asyncio import AsyncIOScheduler
                    from zerg.jobs.commis import enqueue_missed_runs
                    from zerg.jobs.commis import run_queue_commis
                    from zerg.jobs.git_sync import GitSyncService
                    from zerg.jobs.git_sync import run_git_sync_loop
                    from zerg.jobs.git_sync import set_git_sync_service
                    from zerg.jobs.git_sync import set_git_sync_task
                    from zerg.jobs.registry import register_all_jobs

                    def _resolve_repo_config() -> dict | None:
                        try:
                            from zerg.database import db_session
                            from zerg.models.models import JobRepoConfig
                            from zerg.utils.crypto import decrypt

                            with db_session() as db:
                                row = db.query(JobRepoConfig).first()
                                if row:
                                    token = decrypt(row.encrypted_token) if row.encrypted_token else None
                                    from urllib.parse import urlparse
                                    from urllib.parse import urlunparse

                                    _parsed = urlparse(row.repo_url)
                                    _safe_url = urlunparse(_parsed._replace(netloc=_parsed.netloc.split("@")[-1]))
                                    logger.info("Using DB repo config: %s (branch=%s)", _safe_url, row.branch)
                                    return {"repo_url": row.repo_url, "branch": row.branch, "token": token}
                        except Exception as e:
                            logger.warning("Failed to query DB repo config: %s", e)

                        if _settings.jobs_git_repo_url:
                            return {
                                "repo_url": _settings.jobs_git_repo_url,
                                "branch": _settings.jobs_git_branch,
                                "token": _settings.jobs_git_token,
                            }
                        return None

                    repo_config = _resolve_repo_config()

                    if not repo_config:
                        logger.info("No jobs repo configured — builtin jobs only")

                    if repo_config:
                        git_service = GitSyncService(
                            repo_url=repo_config["repo_url"],
                            local_path=Path(_settings.jobs_dir),
                            branch=repo_config["branch"],
                            token=repo_config.get("token"),
                        )

                        await git_service.ensure_cloned()
                        set_git_sync_service(git_service)

                        if _settings.jobs_refresh_interval_seconds > 0:
                            sync_task = asyncio.create_task(
                                run_git_sync_loop(
                                    git_service,
                                    _settings.jobs_refresh_interval_seconds,
                                )
                            )
                            set_git_sync_task(sync_task)

                        started.append("git_sync")
                        logger.info(
                            "Git sync service initialized: %s",
                            git_service.current_sha[:8] if git_service.current_sha else "unknown",
                        )

                    job_scheduler = AsyncIOScheduler()
                    scheduled_count = await register_all_jobs(scheduler=job_scheduler, use_queue=True)

                    from zerg.jobs.registry import job_registry as _jr

                    all_jobs = _jr.list_jobs()
                    builtin_count = sum(1 for j in all_jobs if "builtin" in (j.tags or []))
                    manifest_count = len(all_jobs) - builtin_count
                    logger.info(
                        "Scheduled %d jobs (%d builtin, %d from manifest)",
                        scheduled_count,
                        builtin_count,
                        manifest_count,
                    )

                    app.state.job_system_status = {
                        "started": True,
                        "git_sync": repo_config is not None,
                        "scheduled_count": scheduled_count,
                        "builtin_count": builtin_count,
                        "manifest_count": manifest_count,
                        "error": None,
                    }

                    await enqueue_missed_runs()
                    job_scheduler.start()
                    asyncio.create_task(run_queue_commis())
                    started.append("job_queue_commis")
                    logger.info("Job queue commis started (queue mode)")
                except Exception as e:  # noqa: BLE001
                    failed.append(f"job_queue_commis ({e})")
                    logger.exception("Failed to start job_queue_commis")
                    app.state.job_system_status = {
                        "started": False,
                        "git_sync": False,
                        "scheduled_count": 0,
                        "error": str(e),
                    }

            if failed:
                logger.warning(
                    "Background services partial startup: started=%s failed=%s",
                    started,
                    failed,
                )
            else:
                logger.info("Background services started: %s", started)

        # Email config status
        if not _settings.testing:
            try:
                from zerg.shared.email import resolve_email_config

                email_cfg = resolve_email_config()
                email_configured = all(
                    (
                        email_cfg.get("AWS_SES_ACCESS_KEY_ID"),
                        email_cfg.get("AWS_SES_SECRET_ACCESS_KEY"),
                        email_cfg.get("FROM_EMAIL"),
                        email_cfg.get("NOTIFY_EMAIL"),
                    )
                )
                if email_configured:
                    logger.info(
                        "Email configured (from=%s to=%s)",
                        email_cfg.get("FROM_EMAIL"),
                        email_cfg.get("NOTIFY_EMAIL"),
                    )
                else:
                    logger.warning("Email not configured — job notifications disabled")
            except Exception:
                logger.warning("Email not configured — job notifications disabled")

        # Telegram channel
        if not _settings.testing and _settings.telegram_bot_token:
            try:
                from zerg.channels.plugins.telegram import TelegramChannel
                from zerg.channels.registry import register_channel

                _tg_channel = TelegramChannel()
                await _tg_channel.configure(
                    {
                        "credentials": {"bot_token": _settings.telegram_bot_token},
                        "settings": {
                            "webhook_url": _settings.telegram_webhook_url,
                            "webhook_secret": _settings.telegram_webhook_secret,
                            "parse_mode": "html",
                        },
                    }
                )
                await _tg_channel.start()
                register_channel(_tg_channel, replace=True)
                app.state.telegram_channel = _tg_channel
                logger.info("Telegram channel started (@%s)", _tg_channel._bot_info.get("username", "unknown"))
            except Exception:
                logger.exception("Telegram startup failed (non-fatal) — bot will be unavailable")

        # Mark runners offline
        try:
            from sqlalchemy import update

            from zerg.database import db_session
            from zerg.models.models import Runner

            with db_session() as db:
                result = db.execute(update(Runner).where(Runner.status == "online").values(status="offline"))
                if result.rowcount:
                    logger.info("Startup: marked %d stale runner(s) offline", result.rowcount)
        except Exception as e:
            logger.warning("Startup: failed to reset runner statuses (non-fatal): %s", e)

        # WAL checkpoints
        if not _settings.testing:
            try:
                from zerg.database import start_wal_checkpoint_loop

                await start_wal_checkpoint_loop()
                logger.info("WAL checkpoint loop started")
            except Exception as e:
                logger.warning("Startup: WAL checkpoint loop failed (non-fatal): %s", e)

        elapsed_ms = (time.monotonic() - startup_started) * 1000
        logger.info("Application startup complete elapsed_ms=%.1f", elapsed_ms)
    except Exception as e:
        logger.error(f"Error during startup: {e}")
        raise

    yield  # Application is running

    # Shutdown
    try:
        if not _settings.testing:
            try:
                await scheduler_service.stop()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop scheduler_service")

            try:
                ops_events_bridge.stop()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop ops_events_bridge")

            try:
                if hasattr(app.state, "telegram_channel"):
                    await app.state.telegram_channel.stop()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop Telegram channel")

            try:
                from zerg.services.watch_renewal_service import watch_renewal_service

                await watch_renewal_service.stop()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop watch_renewal_service")

            try:
                from zerg.database import stop_wal_checkpoint_loop

                await stop_wal_checkpoint_loop()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop WAL checkpoint loop")

            if _settings.job_queue_enabled:
                try:
                    from zerg.jobs.ops_db import close_pool

                    await close_pool()
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to close DB pool")

            try:
                from zerg.tools.mcp_adapter import MCPManager

                await MCPManager().shutdown_stdio_processes()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to shutdown MCP stdio processes")

        from zerg.utils.async_runner import get_shared_runner

        get_shared_runner().stop()

        from zerg.websocket.manager import topic_manager

        await topic_manager.shutdown()

        try:
            from zerg.services.llm_audit import audit_logger

            await audit_logger.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to stop audit_logger")

        shutdown_observability()
        logger.info("Background services stopped")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")
