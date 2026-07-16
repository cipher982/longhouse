"""Near-instant AI titles for storage-v2 sessions."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC
from datetime import datetime
from typing import Any

from zerg.config import get_settings
from zerg.services.session_title import sanitize_title

logger = logging.getLogger(__name__)

_in_flight: set[str] = set()
_lock = asyncio.Lock()


async def _catalog_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    from zerg.services.catalogd_supervisor import get_catalogd_client

    client = get_catalogd_client()
    if client is None:
        raise RuntimeError("catalogd is not supervised")
    return await client.call(method, params, timeout_seconds=2.0)


async def generate_storage_session_title(candidate: dict[str, Any]) -> bool:
    session_id = str(candidate["session_id"])
    async with _lock:
        if session_id in _in_flight:
            return False
        _in_flight.add(session_id)
    client = None
    try:
        if get_settings().llm_disabled:
            return False
        first_user_message = str(candidate.get("first_user_message") or "")
        if sanitize_title(first_user_message) is None:
            raise ValueError("no_meaningful_user_text")
        from zerg.models_config import get_llm_client_for_use_case
        from zerg.services.title_generator import generate_initial_session_title

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
            timeout_seconds=4.0,
        )
        title = sanitize_title(raw_title, max_words=6)
        if not title:
            raise ValueError("empty_model_response")
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
            await _catalog_call(
                "storage.session.title.fail.v2",
                {"session_id": session_id, "reason": reason, "failed_at": datetime.now(UTC).isoformat()},
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist storage-v2 title retry session=%s", session_id)
        return False
    finally:
        if client is not None:
            await client.close()
        async with _lock:
            _in_flight.discard(session_id)


def schedule_storage_session_title(candidate: dict[str, Any]) -> None:
    if not str(candidate.get("first_user_message") or "").strip():
        return
    asyncio.create_task(generate_storage_session_title(candidate))


async def run_storage_title_reconciler(*, interval_seconds: float = 0.5, batch_size: int = 16) -> None:
    if get_settings().llm_disabled:
        return
    while True:
        try:
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
