"""Application lifespan (startup/shutdown) management.

Extracted from main.py — keeps the app factory lean.
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from contextlib import contextmanager

from fastapi import FastAPI

from zerg.config import get_settings
from zerg.database import initialize_database
from zerg.observability import configure_observability
from zerg.observability import shutdown_observability
from zerg.services.ops_events import ops_events_bridge
from zerg.services.scheduler_service import scheduler_service

_settings = get_settings()
logger = logging.getLogger(__name__)
_TRUTHY_ENV = {"1", "true", "yes", "on"}


def _live_preview_cleanup_enabled() -> bool:
    return os.getenv("LONGHOUSE_ENABLE_LIVE_PREVIEW_CLEANUP", "").strip().lower() in _TRUTHY_ENV


def _session_input_queue_recovery_enabled() -> bool:
    return os.getenv("LONGHOUSE_ENABLE_SESSION_INPUT_QUEUE_RECOVERY", "").strip().lower() in _TRUTHY_ENV


async def _reap_stale_machine_control_operations_once() -> int:
    from zerg.services.machine_control_operations import reap_stale_machine_control_operations
    from zerg.services.write_serializer import get_write_serializer

    return await get_write_serializer().execute(
        reap_stale_machine_control_operations,
        auto_commit=False,
        label="machine-control-reaper",
    )


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
    """Validate configured providers at boot.

    Three cases:
      - testing / llm_disabled / demo_mode: skip silently (operator opted out).
      - No LLM keys at all (`llm_available=False`): true first-run. Emit a
        prominent banner naming the degraded capabilities and how to enable
        them, then boot. This preserves the "UI boots without API keys"
        contract documented on Settings.llm_available.
      - Some keys configured (`llm_available=True`) but a specific declared
        provider is missing its key: hard-fail with the actionable error from
        models_config so operators catch misconfiguration immediately.
    """
    if _settings.testing or _settings.llm_disabled or _settings.demo_mode:
        return

    from zerg.models_config import iter_required_provider_keys
    from zerg.models_config import validate_startup_config

    if not _settings.llm_available:
        missing_scopes = sorted({scope for _env, scope, _model, _provider in iter_required_provider_keys()})
        scope_lines = "\n".join(f"      - {scope}" for scope in missing_scopes) or "      - (none declared)"
        banner = (
            "\n"
            "================================================================\n"
            "  Longhouse is running in LIMITED MODE (no LLM provider keys)\n"
            "\n"
            "  Disabled until you configure a provider key:\n"
            f"{scope_lines}\n"
            "\n"
            "  To enable: set one of OPENROUTER_API_KEY, OPENAI_API_KEY,\n"
            "    GROQ_API_KEY, XAI_API_KEY (matching config/models.json).\n"
            "  To suppress this banner: set LLM_DISABLED=1\n"
            "================================================================"
        )
        logger.warning(banner)
        return

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

        # SessionInput reconciliation: run one bounded queue recovery tick at
        # boot so startup, periodic recovery, and missed-signal recovery share
        # the same policy.
        if not _settings.testing and _session_input_queue_recovery_enabled():
            try:
                with _timed_startup_step("session_input_reconciliation"):
                    from zerg.database import default_engine
                    from zerg.services.session_input_queue import recover_session_input_queues

                    async def _boot_recover_session_inputs() -> None:
                        try:
                            result = await recover_session_input_queues(
                                db_bind=default_engine,
                                reason="startup_reconciliation",
                            )
                            if result.session_ids:
                                logger.info(
                                    "SessionInput startup recovery considered %d sessions",
                                    len(result.session_ids),
                                )
                        except Exception:
                            logger.exception(
                                "SessionInput startup recovery failed (non-fatal)",
                            )

                    asyncio.create_task(_boot_recover_session_inputs())
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
                        fts_probe_sql = "SELECT rowid FROM events_fts WHERE events_fts MATCH 'fts5' LIMIT 1"
                        conn.execute(text(fts_probe_sql)).fetchone()
            except Exception as fts_error:
                app.state.fts_violation = str(fts_error)
                logger.error(f"FTS5 readiness check failed: {fts_error}")
                raise

        with _timed_startup_step("single_tenant_startup"):
            _enforce_single_tenant_startup(app)

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

            # Managed session input queue recovery: safety net for process
            # restarts and missed local terminal/idle wakes. This is opt-in
            # until its DB writes use the hosted SQLite write serializer.
            if _session_input_queue_recovery_enabled():
                try:
                    from zerg.database import default_engine as _default_engine_input_queue
                    from zerg.services.session_input_queue import run_session_input_queue_recovery_loop

                    asyncio.create_task(run_session_input_queue_recovery_loop(db_bind=_default_engine_input_queue))
                    started.append("session_input_queue_recovery")
                except Exception as e:  # noqa: BLE001
                    failed.append(f"session_input_queue_recovery ({e})")
                    logger.exception("Failed to start session_input_queue_recovery")
            else:
                logger.info("Session input queue periodic recovery disabled; set LONGHOUSE_ENABLE_SESSION_INPUT_QUEUE_RECOVERY=1 to enable")

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

            # Machine-control operation reaper: expire commands whose result
            # did not return before their operation lease.
            try:

                async def _machine_control_operation_reaper_loop() -> None:
                    while True:
                        try:
                            await asyncio.sleep(60)
                            await _reap_stale_machine_control_operations_once()
                        except asyncio.CancelledError:
                            raise
                        except Exception:  # noqa: BLE001
                            logger.exception("Machine control operation reaper tick failed")

                asyncio.create_task(_machine_control_operation_reaper_loop())
                started.append("machine_control_operation_reaper")
            except Exception as e:  # noqa: BLE001
                failed.append(f"machine_control_operation_reaper ({e})")
                logger.exception("Failed to start machine_control_operation_reaper")

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

            # Live-preview observation reaper: keep disabled by default. Legacy
            # dogfood databases can contain millions of append-only preview rows,
            # and a global startup scan can starve the runtime. Session-scoped
            # cleanup still runs when durable transcript ingest completes.
            if _live_preview_cleanup_enabled():
                try:
                    from zerg.services.provisional_events import cleanup_bridge_transcript_preview_observations
                    from zerg.services.write_serializer import get_write_serializer

                    async def _live_preview_cleanup_loop() -> None:
                        ws = get_write_serializer()
                        skips_remaining = 0
                        while True:
                            try:
                                await asyncio.sleep(60)
                                # When the queue is under pressure, skip up to 3
                                # consecutive ticks but force one every 4th iteration
                                # so cleanup never stops permanently.
                                if skips_remaining > 0:
                                    skips_remaining -= 1
                                    continue
                                recent_wait = ws.stats.max_queue_wait_ms
                                if recent_wait > 500:
                                    skips_remaining = 3
                                    logger.info(
                                        "live-preview-cleanup skipping due to queue pressure (max_queue_wait=%.0fms)",
                                        recent_wait,
                                    )
                                    continue

                                # Pre-check: only queue a write if there are rows
                                # to delete. Avoids holding the serializer lock at
                                # all for no-op ticks on clean databases.
                                try:
                                    from sqlalchemy import text as _sa_text

                                    from zerg.database import default_engine as _def_eng

                                    if _def_eng is not None:
                                        with _def_eng.connect() as _conn:
                                            row = _conn.execute(
                                                _sa_text(
                                                    "SELECT 1 FROM session_observations "
                                                    "WHERE source = 'codex_bridge_live' "
                                                    "AND kind = 'bridge_transcript_delta' "
                                                    "LIMIT 1"
                                                )
                                            ).fetchone()
                                        if not row:
                                            continue
                                except Exception:
                                    pass  # fall through to full cleanup

                                await ws.execute(
                                    lambda db: cleanup_bridge_transcript_preview_observations(
                                        db,
                                        batch_size=100,
                                        max_sessions=2,
                                    ),
                                    label="live-preview-cleanup",
                                    timeout_seconds=5.0,
                                )
                            except asyncio.CancelledError:
                                raise
                            except asyncio.TimeoutError:
                                logger.warning("live preview cleanup timed out (will retry next tick)")
                            except Exception:  # noqa: BLE001
                                logger.exception("live preview cleanup tick failed")

                    asyncio.create_task(_live_preview_cleanup_loop())
                    started.append("live_preview_cleanup")
                except Exception as e:  # noqa: BLE001
                    failed.append(f"live_preview_cleanup ({e})")
                    logger.exception("Failed to start live_preview_cleanup")

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

            # Archive ingest can skip expensive derived projections on the hot
            # shipping path; this reconciler catches those sessions up later.
            try:
                from zerg.services.session_projection_reconciler import run_projection_reconciler

                asyncio.create_task(run_projection_reconciler())
                started.append("projection_reconciler")
            except Exception as e:  # noqa: BLE001
                failed.append(f"projection_reconciler ({e})")
                logger.exception("Failed to start projection_reconciler")

            # Periodic runtime maintenance (runner-health reconcile, etc.)
            if not _settings.testing:
                try:
                    from zerg.services.maintenance import start_maintenance_loop

                    start_maintenance_loop()
                    started.append("maintenance_loop")
                except Exception as e:  # noqa: BLE001
                    failed.append(f"maintenance_loop ({e})")
                    logger.exception("Failed to start maintenance loop")

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

            try:
                from zerg.services.maintenance import stop_maintenance_loop

                await stop_maintenance_loop()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop maintenance loop")

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
