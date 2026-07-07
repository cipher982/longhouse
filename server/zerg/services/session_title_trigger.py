"""Fast-path trigger for first-message session title generation."""

from __future__ import annotations

import asyncio
import logging
import threading

logger = logging.getLogger(__name__)

_in_flight_lock = threading.Lock()
_in_flight_session_ids: set[str] = set()


def reset_initial_title_trigger_for_test() -> None:
    with _in_flight_lock:
        _in_flight_session_ids.clear()


def maybe_start_initial_title_generation(
    session_id: str,
    *,
    reason: str,
) -> bool:
    """Start initial title generation without blocking the ingest hot path."""
    with _in_flight_lock:
        if session_id in _in_flight_session_ids:
            logger.debug("Initial title generation already in flight for session %s", session_id)
            return False
        _in_flight_session_ids.add(session_id)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            thread = threading.Thread(
                target=lambda: asyncio.run(_run_initial_title_generation(session_id=session_id, reason=reason)),
                name=f"initial-title-{session_id[:8]}",
                daemon=True,
            )
            thread.start()
            logger.info("Scheduled initial title generation for session %s reason=%s via thread", session_id, reason)
            return True
        except Exception:
            _discard_in_flight(session_id)
            logger.exception("Failed to schedule threaded initial title generation for session %s", session_id)
            return False

    try:
        loop.create_task(_run_initial_title_generation(session_id=session_id, reason=reason))
        logger.info("Scheduled initial title generation for session %s reason=%s", session_id, reason)
        return True
    except Exception:
        _discard_in_flight(session_id)
        logger.exception("Failed to schedule initial title generation for session %s", session_id)
        return False


async def _run_initial_title_generation(
    *,
    session_id: str,
    reason: str,
) -> None:
    try:
        from zerg.services.session_summaries import generate_initial_title_impl

        generated = await generate_initial_title_impl(session_id)
        logger.info("Initial title generation finished for session %s reason=%s generated=%s", session_id, reason, generated)
    except Exception:
        logger.exception("Initial title generation task failed for session %s reason=%s", session_id, reason)
    finally:
        _discard_in_flight(session_id)


def _discard_in_flight(session_id: str) -> None:
    with _in_flight_lock:
        _in_flight_session_ids.discard(session_id)
