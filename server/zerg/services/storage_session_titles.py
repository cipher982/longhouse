"""Near-instant AI titles for storage-v2 sessions."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import uuid4

from zerg.config import get_settings
from zerg.services.session_processing.summarize import ai_titles_and_summaries_enabled
from zerg.services.session_title import sanitize_timeline_title
from zerg.services.session_title import sanitize_title

logger = logging.getLogger(__name__)

STORAGE_TITLE_MAX_CONCURRENCY = 4
STORAGE_TITLE_CANDIDATE_LOOKAHEAD = STORAGE_TITLE_MAX_CONCURRENCY * 2
STORAGE_TITLE_CATALOG_TIMEOUT_SECONDS = 10.0
# Hosted title responses normally complete in 5-12 seconds. A 15-second edge
# converted ordinary tail latency into a provider-wide outage during backlog
# recovery; background titles can safely allow the full 30-second budget.
STORAGE_TITLE_MODEL_TIMEOUT_SECONDS = 30.0
STORAGE_TITLE_DEPENDENCY_PROBE_LEASE_SECONDS = 60
STORAGE_TITLE_CLIENT_CLOSE_TIMEOUT_SECONDS = 2.0

_in_flight: set[str] = set()
_lock = asyncio.Lock()
_model_slots = asyncio.Semaphore(STORAGE_TITLE_MAX_CONCURRENCY)
_scheduled_tasks: dict[str, asyncio.Task[bool]] = {}
_client_close_tasks: set[asyncio.Task[None]] = set()
_scheduled_workers_peak = 0


def _dependency_identity() -> dict[str, str]:
    from zerg.models_config import MODELS_BY_ID
    from zerg.models_config import resolve_use_case_runtime_identity

    binding = resolve_use_case_runtime_identity("session_title")
    model = binding.model_id
    config = MODELS_BY_ID[model]
    credential_binding = binding.credential_binding
    credential_state = binding.credential.encode() if binding.credential is not None else b"<missing>"
    generation = hashlib.sha256(b"longhouse-title-credential-v1\0" + credential_state).hexdigest()
    return {
        "provider": config.provider.value,
        "model": model,
        "credential_binding": credential_binding,
        "credential_generation": generation,
    }


def _is_dependency_auth_failure(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code in {401, 403}:
        return True
    class_name = type(exc).__name__.lower()
    message = str(exc).lower()
    return class_name in {"authenticationerror", "permissiondeniederror"} or any(
        marker in message
        for marker in (
            "401",
            "403",
            "unauthorized",
            "forbidden",
            "authentication",
            "api key",
            "required for use case",
            "user not found",
        )
    )


def _dependency_failure_class(exc: Exception) -> str | None:
    """Classify failures shared by every title obligation using this binding."""

    if _is_dependency_auth_failure(exc):
        return "authentication"
    status_code = getattr(exc, "status_code", None)
    if status_code == 429 or (isinstance(status_code, int) and status_code >= 500):
        return "availability"
    class_name = type(exc).__name__.lower()
    message = str(exc).strip().lower()
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)) or class_name in {
        "apiconnectionerror",
        "apitimeouterror",
        "internalservererror",
        "ratelimiterror",
        "serviceunavailableerror",
    }:
        return "availability"
    if any(marker in message for marker in ("rate limit", "timed out", "timeout", "temporarily unavailable")):
        return "availability"
    return None


async def _reconcile_dependency(identity: dict[str, str]) -> dict[str, Any]:
    return await _catalog_call(
        "storage.session.title.dependency.reconcile.v2",
        {**identity, "observed_at": datetime.now(UTC).isoformat()},
    )


async def _catalog_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    from zerg.services.catalogd_supervisor import get_catalogd_client

    client = get_catalogd_client()
    if client is None:
        raise RuntimeError("catalogd is not supervised")
    # The title worker is background work, and candidate selection can wait
    # behind catalogd's single writer on large self-hosted corpora. A two
    # second deadline caused the worker to loop forever without reaching the
    # model, making title generation look intermittently broken.
    return await client.call(method, params, timeout_seconds=STORAGE_TITLE_CATALOG_TIMEOUT_SECONDS)


async def generate_storage_session_title(candidate: dict[str, Any]) -> bool:
    session_id = str(candidate["session_id"])
    async with _lock:
        if session_id in _in_flight:
            return False
        _in_flight.add(session_id)
    client = None
    dependency: dict[str, str] | None = None
    dependency_claim: dict[str, Any] | None = None
    probe_token = str(uuid4())
    slot_acquired = False
    failure_scope = "row"
    dependency_failure_class: str | None = None
    try:
        await _model_slots.acquire()
        slot_acquired = True
        if get_settings().llm_disabled or not ai_titles_and_summaries_enabled():
            # Off is a capability state, not a failed attempt. Without this the
            # chokepoint gate in generate_initial_session_title returns None,
            # sanitize_timeline_title returns None, and the row below is charged
            # an "empty_model_response" failure -- so every ingested session
            # would burn three catalogd RPCs and record durable retry state for
            # a call that can never happen.
            return False
        if candidate.get("canonical_title_eligible") is not True:
            logger.warning("Skipping storage-v2 title without canonical catalog eligibility session=%s", session_id)
            return False
        first_user_message = str(candidate.get("first_user_message") or "")
        # A path can be poor display copy while still being meaningful model
        # input (the model may infer the file or feature being discussed).
        if sanitize_title(first_user_message) is None:
            raise ValueError("no_meaningful_user_text")
        from zerg.models_config import get_llm_client_for_use_case
        from zerg.services.title_generator import generate_initial_session_title

        dependency = _dependency_identity()
        failure_scope = "catalog"
        await _reconcile_dependency(dependency)
        dependency_claim = await _catalog_call(
            "storage.session.title.dependency.acquire.v2",
            {
                "session_id": session_id,
                **dependency,
                "probe_token": probe_token,
                "observed_at": datetime.now(UTC).isoformat(),
                "lease_seconds": STORAGE_TITLE_DEPENDENCY_PROBE_LEASE_SECONDS,
            },
        )
        if dependency_claim.get("allowed") is not True:
            return False
        failure_scope = "provider"
        try:
            client, model, _provider = get_llm_client_for_use_case("session_title")
            started = datetime.now(UTC)
            raw_title = await generate_initial_session_title(
                first_user_message=first_user_message,
                client=client,
                model=model,
                metadata={
                    "project": candidate.get("project"),
                    "provider": candidate.get("provider"),
                    "git_branch": candidate.get("git_branch"),
                },
                # Match the title generator's default. Four seconds caused large
                # but valid first prompts to fail just before the provider replied.
                timeout_seconds=STORAGE_TITLE_MODEL_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - preserve provider failure ownership
            dependency_failure_class = _dependency_failure_class(exc)
            raise
        failure_scope = "catalog"
        incident_id = dependency_claim.get("incident_id")
        if incident_id:
            await _catalog_call(
                "storage.session.title.dependency.recover.v2",
                {
                    **dependency,
                    "incident_id": incident_id,
                    "probe_token": probe_token,
                    "recovered_at": datetime.now(UTC).isoformat(),
                },
            )
        failure_scope = "row"
        title = sanitize_timeline_title(raw_title, max_words=6)
        if not title:
            raise ValueError("empty_model_response")
        failure_scope = "catalog"
        result = await _catalog_call(
            "storage.session.title.complete.v2",
            {"session_id": session_id, "title": title, "completed_at": datetime.now(UTC).isoformat()},
        )
        if result.get("changed"):
            from zerg.services.session_pubsub import publish_session_title_update

            publish_session_title_update(
                session_id=session_id,
                provider=candidate.get("provider"),
                source="storage_ai_title",
            )
            elapsed_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
            logger.info(
                "Generated storage-v2 AI title session=%s elapsed_ms=%d title=%s",
                session_id,
                elapsed_ms,
                title,
            )
        return bool(result.get("changed"))
    except Exception as exc:  # noqa: BLE001 - failure becomes durable retry state
        reason = type(exc).__name__ if str(exc) == "" else str(exc)[:128]
        logger.warning("Storage-v2 AI title failed session=%s reason=%s", session_id, reason)
        try:
            if failure_scope == "provider" and dependency is not None and dependency_failure_class is not None:
                await _catalog_call(
                    "storage.session.title.dependency.fail.v2",
                    {
                        "session_id": session_id,
                        **dependency,
                        "probe_token": probe_token,
                        "failure_class": dependency_failure_class,
                        "reason": reason,
                        "failed_at": datetime.now(UTC).isoformat(),
                    },
                )
            elif failure_scope in {"provider", "row"}:
                await _catalog_call(
                    "storage.session.title.fail.v2",
                    {"session_id": session_id, "reason": reason, "failed_at": datetime.now(UTC).isoformat()},
                )
            else:
                logger.warning("Leaving durable title obligation uncharged after catalog failure session=%s", session_id)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist storage-v2 title retry session=%s", session_id)
        return False
    finally:
        try:
            if client is not None:
                await _close_client_bounded(client, session_id=session_id)
        finally:
            if slot_acquired:
                _model_slots.release()
            async with _lock:
                _in_flight.discard(session_id)


async def _run_scheduled_title(candidate: dict[str, Any]) -> bool:
    session_id = str(candidate["session_id"])
    try:
        return await generate_storage_session_title(candidate)
    finally:
        current = asyncio.current_task()
        if _scheduled_tasks.get(session_id) is current:
            _scheduled_tasks.pop(session_id, None)


def schedule_storage_session_title(candidate: dict[str, Any]) -> bool:
    global _scheduled_workers_peak

    if not str(candidate.get("first_user_message") or "").strip():
        return False
    session_id = str(candidate.get("session_id") or "").strip()
    if not session_id or session_id in _scheduled_tasks or len(_scheduled_tasks) >= STORAGE_TITLE_MAX_CONCURRENCY:
        return False
    task = asyncio.create_task(_run_scheduled_title(candidate))
    _scheduled_tasks[session_id] = task
    _scheduled_workers_peak = max(_scheduled_workers_peak, len(_scheduled_tasks))
    return True


def storage_title_scheduler_snapshot() -> dict[str, int]:
    return {
        "scheduled_workers": len(_scheduled_tasks),
        "scheduled_workers_peak": _scheduled_workers_peak,
        "closing_clients": len(_client_close_tasks),
        "worker_limit": STORAGE_TITLE_MAX_CONCURRENCY,
    }


def _consume_close_task(task: asyncio.Task[None]) -> None:
    _client_close_tasks.discard(task)
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        return


async def _close_client_bounded(client: Any, *, session_id: str) -> None:
    close_task = asyncio.create_task(client.close())
    _client_close_tasks.add(close_task)
    close_task.add_done_callback(_consume_close_task)
    done, _pending = await asyncio.wait({close_task}, timeout=STORAGE_TITLE_CLIENT_CLOSE_TIMEOUT_SECONDS)
    if close_task not in done:
        logger.error("Timed out closing storage-v2 title client session=%s", session_id)
        close_task.cancel()
        return
    try:
        close_task.result()
    except Exception:  # noqa: BLE001 - cleanup must never leak capacity
        logger.exception("Failed to close storage-v2 title client session=%s", session_id)


async def stop_storage_title_workers() -> None:
    tasks = list(_scheduled_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _scheduled_tasks.clear()
    close_tasks = list(_client_close_tasks)
    for task in close_tasks:
        task.cancel()
    if close_tasks:
        await asyncio.wait(close_tasks, timeout=STORAGE_TITLE_CLIENT_CLOSE_TIMEOUT_SECONDS)


async def run_storage_title_reconciler(
    *,
    interval_seconds: float = 0.5,
    batch_size: int = STORAGE_TITLE_CANDIDATE_LOOKAHEAD,
) -> None:
    if get_settings().llm_disabled or not ai_titles_and_summaries_enabled():
        # Nothing to reconcile: no candidate can produce a title while
        # transcript egress is off, so do not run the 0.5s loop at all.
        return
    while True:
        try:
            await _reconcile_dependency(_dependency_identity())
            result = await _catalog_call("storage.session.title.candidates.v2", {"limit": batch_size})
            candidates = result.get("sessions") if isinstance(result, dict) else None
            for candidate in candidates or []:
                if isinstance(candidate, dict):
                    schedule_storage_session_title(candidate)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Storage-v2 title reconciler tick failed")
        await asyncio.sleep(interval_seconds)
