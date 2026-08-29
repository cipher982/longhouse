"""Application lifespan (startup/shutdown) management.

Extracted from main.py — keeps the app factory lean.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from contextlib import contextmanager

from fastapi import FastAPI

from zerg.config import get_settings
from zerg.database import initialize_live_database
from zerg.database import live_store_configured
from zerg.database import refresh_database_settings_from_env
from zerg.observability import configure_observability
from zerg.observability import shutdown_observability

_settings = get_settings()
logger = logging.getLogger(__name__)


async def _initialize_local_embedding_projector(app: FastAPI) -> None:
    """Provision dense recall without delaying the Runtime Host launch loop."""

    try:
        with _timed_startup_step("local_embedding_model"):
            from zerg.models_config import get_embedding_space_config
            from zerg.services.embedding_artifact import provision_embedding_artifact
            from zerg.services.local_embedder import initialize_local_embedder

            model_dir = await asyncio.to_thread(provision_embedding_artifact)
            await asyncio.to_thread(initialize_local_embedder, get_embedding_space_config(), model_dir)
            app.state.embedding_model_dir = str(model_dir)
        from zerg.services.embeddings_v2_projector import start_embeddings_v2_projector

        app.state.embeddings_v2_projector_started = start_embeddings_v2_projector()
        if not app.state.embeddings_v2_projector_started:
            logger.warning("Embeddings-v2 projector is degraded; hot Runtime Host readiness is unaffected")
    except asyncio.CancelledError:
        raise
    except Exception:
        app.state.embeddings_v2_projector_started = False
        logger.exception("Failed to initialize local embeddings (non-fatal)")


async def _stop_local_embedding_initializer(app: FastAPI) -> None:
    task = getattr(app.state, "embedding_initializer_task", None)
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    app.state.embedding_initializer_task = None


async def _stop_storage_title_services(app: FastAPI) -> None:
    task = getattr(app.state, "storage_title_reconciler_task", None)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        app.state.storage_title_reconciler_task = None
    from zerg.services.storage_session_titles import stop_storage_title_workers

    await stop_storage_title_workers()


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

    from zerg.services.single_tenant import OSS_DEFAULT_EMAIL
    from zerg.services.single_tenant import SingleTenantViolation
    from zerg.services.single_tenant import get_owner_email
    from zerg.services.single_tenant import validate_single_tenant_config

    config_error = validate_single_tenant_config()
    if config_error:
        app.state.single_tenant_violation = config_error
        logger.error("Single-tenant config error: %s", config_error)
        raise RuntimeError(config_error)

    try:
        from zerg.catalogd.client import CatalogRemoteError
        from zerg.catalogd.client import call_catalogd_sync
        from zerg.services.catalogd_supervisor import catalogd_paths

        owner_email = get_owner_email()
        provider = "local" if owner_email in {OSS_DEFAULT_EMAIL, "owner@longhouse.local"} else "google"
        provider_user_id = "local-user-1" if owner_email == OSS_DEFAULT_EMAIL else None
        _database_path, socket_path = catalogd_paths()
        try:
            call_catalogd_sync(
                socket_path,
                "auth.single_tenant.ensure.v2",
                params={
                    "email": owner_email,
                    "provider": provider,
                    "provider_user_id": provider_user_id,
                },
                timeout_seconds=1.0,
            )
        except CatalogRemoteError as exc:
            reason = (exc.details or {}).get("reason") if isinstance(exc.details, dict) else None
            raise SingleTenantViolation(f"Single-tenant violation: {reason or exc.code}") from exc
        return
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
    global _settings
    _settings = get_settings()
    refresh_database_settings_from_env()
    startup_started = time.monotonic()
    from zerg.services.factory_assurance_title_binding import factory_assurance_title_enabled

    factory_title_assurance = factory_assurance_title_enabled()
    e2e_catalog = _settings.testing and _settings.environment == "test:e2e"
    owns_test_catalog = factory_title_assurance or e2e_catalog
    try:
        with _timed_startup_step("configure_observability"):
            configure_observability()
        logger.info("Storage-v2 mode: retired cold database is not initialized or mounted")
        if not _settings.testing:
            with _timed_startup_step("catalogd_supervisor"):
                from zerg.services.catalogd_supervisor import start_catalogd_supervisor

                app.state.catalogd_ping = await start_catalogd_supervisor()
            logger.info("Live catalog schema is owned by catalogd")
            with _timed_startup_step("searchd_supervisor"):
                try:
                    from zerg.services.searchd_supervisor import start_searchd_supervisor

                    app.state.searchd_ping = await start_searchd_supervisor()
                    if app.state.searchd_ping is None:
                        logger.warning("searchd is degraded; hot Runtime Host readiness is unaffected")
                except Exception:  # search is derived and never gates the launch loop
                    app.state.searchd_ping = None
                    logger.exception("Failed to start searchd supervisor (non-fatal)")
            with _timed_startup_step("storage_v2_workers"):
                from zerg.services.raw_object_workers import get_raw_object_worker_pool
                from zerg.services.render_object_workers import get_render_object_worker_pool

                await asyncio.gather(
                    get_raw_object_worker_pool().start(),
                    get_render_object_worker_pool().start(),
                )
            logger.info("Storage-v2 live and repair worker lanes are ready")
            try:
                from zerg.services.semantic_v2_projector import start_semantic_v2_projector

                app.state.semantic_v2_projector_started = start_semantic_v2_projector()
                if not app.state.semantic_v2_projector_started:
                    logger.warning("Semantic-v2 projector is degraded; hot Runtime Host readiness is unaffected")
            except Exception:
                app.state.semantic_v2_projector_started = False
                logger.exception("Failed to start semantic-v2 projector (non-fatal)")
            try:
                from zerg.services.search_v2_projector import start_search_v2_projector

                app.state.search_v2_projector_started = start_search_v2_projector()
                if not app.state.search_v2_projector_started:
                    logger.warning("Search-v2 projector is degraded; hot Runtime Host readiness is unaffected")
            except Exception:
                app.state.search_v2_projector_started = False
                logger.exception("Failed to start search-v2 projector (non-fatal)")
            app.state.embedding_initializer_task = asyncio.create_task(
                _initialize_local_embedding_projector(app),
                name="local-embedding-initializer",
            )
            try:
                from zerg.services.storage_telemetry_snapshot import run_storage_telemetry_refresh_loop

                app.state.storage_telemetry_task = asyncio.create_task(run_storage_telemetry_refresh_loop())
                logger.info("Storage telemetry refresh loop started")
            except Exception:
                logger.exception("Failed to start storage telemetry refresh loop (non-fatal)")
        elif owns_test_catalog:
            # The hermetic title oracle and browser E2E runtime need the real
            # catalog owner and storage lanes, but none of the unrelated
            # production projectors.
            with _timed_startup_step("catalogd_supervisor"):
                from zerg.services.catalogd_supervisor import start_catalogd_supervisor

                app.state.catalogd_ping = await start_catalogd_supervisor()
            with _timed_startup_step("storage_v2_workers"):
                from zerg.services.raw_object_workers import get_raw_object_worker_pool
                from zerg.services.render_object_workers import get_render_object_worker_pool

                await asyncio.gather(
                    get_raw_object_worker_pool().start(),
                    get_render_object_worker_pool().start(),
                )
            if e2e_catalog:
                with _timed_startup_step("searchd_supervisor"):
                    from zerg.services.searchd_supervisor import start_searchd_supervisor

                    app.state.searchd_ping = await start_searchd_supervisor()
                from zerg.services.search_v2_projector import start_search_v2_projector
                from zerg.services.semantic_v2_projector import start_semantic_v2_projector

                app.state.semantic_v2_projector_started = start_semantic_v2_projector()
                app.state.search_v2_projector_started = start_search_v2_projector()
                logger.info("Browser E2E catalog, storage, and search lanes are ready")
            else:
                logger.info("Factory assurance catalog and storage-v2 lanes are ready")
        elif live_store_configured():
            with _timed_startup_step("initialize_live_database"):
                initialize_live_database()

        logger.info("Catalog services initialized")

        with _timed_startup_step("single_tenant_startup"):
            _enforce_single_tenant_startup(app)

        with _timed_startup_step("models_config_validation"):
            _validate_models_config_startup()

        # Shared async runner
        from zerg.utils.async_runner import get_shared_runner

        get_shared_runner().start()

        if not _settings.testing:
            try:
                from zerg.services.live_control_catalog import run_live_catalog_input_recovery_loop

                asyncio.create_task(run_live_catalog_input_recovery_loop())
                logger.info("Live catalog input recovery loop started")
            except Exception:
                logger.exception("Failed to start live catalog input recovery loop")
            # Machine-control lease expiry is catalogd's job (checkpoint loop).
            # The old API-side reaper called get_live_write_serializer(), which
            # is intentionally never configured on the catalog lane.

        # Factory title assurance is deliberately a test Runtime Host, but it
        # must exercise the real background worker. This is the only normal
        # production loop re-enabled by the startup-bound assurance gate.
        if not _settings.testing or owns_test_catalog:
            try:
                from zerg.services.storage_session_titles import run_storage_title_reconciler

                app.state.storage_title_reconciler_task = asyncio.create_task(run_storage_title_reconciler())
                logger.info("Storage-v2 AI title reconciler started")
            except Exception:
                logger.exception("Failed to start storage-v2 AI title reconciler")

        # Periodic runtime maintenance (runner-health reconcile, etc.).
        if not _settings.testing:
            try:
                from zerg.services.maintenance import start_maintenance_loop

                start_maintenance_loop()
                logger.info("Maintenance loop started")
            except Exception:
                logger.exception("Failed to start maintenance loop")

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
        if not _settings.testing or owns_test_catalog:
            await _stop_storage_title_services(app)
            await _stop_local_embedding_initializer(app)
            telemetry_task = getattr(app.state, "storage_telemetry_task", None)
            if telemetry_task is not None:
                telemetry_task.cancel()
                await asyncio.gather(telemetry_task, return_exceptions=True)
            try:
                from zerg.services.semantic_v2_projector import stop_semantic_v2_projector

                await stop_semantic_v2_projector()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop semantic-v2 projector")
            try:
                from zerg.services.search_v2_projector import stop_search_v2_projector

                await stop_search_v2_projector()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop search-v2 projector")
            try:
                from zerg.services.embeddings_v2_projector import stop_embeddings_v2_projector

                await stop_embeddings_v2_projector()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop embeddings-v2 projector")
            try:
                from zerg.services.raw_object_workers import close_raw_object_worker_pool
                from zerg.services.render_object_workers import close_render_object_worker_pool

                await asyncio.gather(
                    close_raw_object_worker_pool(),
                    close_render_object_worker_pool(),
                )
            except Exception:
                logger.exception("Failed to stop storage-v2 workers after startup failure")
            try:
                from zerg.services.searchd_supervisor import stop_searchd_supervisor

                await stop_searchd_supervisor()
            except Exception:
                logger.exception("Failed to stop searchd after startup failure")
            try:
                from zerg.services.catalogd_supervisor import stop_catalogd_supervisor

                await stop_catalogd_supervisor()
            except Exception:
                logger.exception("Failed to stop catalogd after startup failure")
        raise

    yield  # Application is running

    # Shutdown
    try:
        if not _settings.testing:
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

        from zerg.utils.async_runner import get_shared_runner

        get_shared_runner().stop()

        from zerg.websocket.manager import topic_manager

        await topic_manager.shutdown()

        if not _settings.testing or owns_test_catalog:
            await _stop_storage_title_services(app)
            await _stop_local_embedding_initializer(app)
            telemetry_task = getattr(app.state, "storage_telemetry_task", None)
            if telemetry_task is not None:
                telemetry_task.cancel()
                await asyncio.gather(telemetry_task, return_exceptions=True)
            try:
                from zerg.services.semantic_v2_projector import stop_semantic_v2_projector

                await stop_semantic_v2_projector()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop semantic-v2 projector")
            try:
                from zerg.services.search_v2_projector import stop_search_v2_projector

                await stop_search_v2_projector()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop search-v2 projector")
            try:
                from zerg.services.embeddings_v2_projector import stop_embeddings_v2_projector

                await stop_embeddings_v2_projector()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop embeddings-v2 projector")
            try:
                from zerg.services.raw_object_workers import close_raw_object_worker_pool
                from zerg.services.render_object_workers import close_render_object_worker_pool

                await asyncio.gather(
                    close_raw_object_worker_pool(),
                    close_render_object_worker_pool(),
                )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop storage-v2 workers")
            try:
                from zerg.services.searchd_supervisor import stop_searchd_supervisor

                await stop_searchd_supervisor()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop searchd supervisor")
            try:
                from zerg.services.catalogd_supervisor import stop_catalogd_supervisor

                await stop_catalogd_supervisor()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to stop catalogd supervisor")

        shutdown_observability()
        logger.info("Background services stopped")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")
