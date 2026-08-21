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
from zerg.services.internal_sessions import classify_provider_proof_environment
from zerg.services.internal_sessions import is_hatch_execution_contract
from zerg.services.session_title import is_resume_seed_marker
from zerg.services.session_title import sanitize_timeline_title
from zerg.services.session_title import sanitize_title

logger = logging.getLogger(__name__)

_in_flight: set[str] = set()
_lock = asyncio.Lock()
STORAGE_TITLE_CATALOG_TIMEOUT_SECONDS = 10.0
STORAGE_TITLE_MODEL_TIMEOUT_SECONDS = 15.0
STORAGE_TITLE_DEPENDENCY_PROBE_LEASE_SECONDS = 60


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
    try:
        if get_settings().llm_disabled:
            return False
        first_user_message = str(candidate.get("first_user_message") or "")
        # A path can be poor display copy while still being meaningful model
        # input (the model may infer the file or feature being discussed).
        if sanitize_title(first_user_message) is None:
            raise ValueError("no_meaningful_user_text")
        if is_resume_seed_marker(first_user_message):
            # Automation seed marker (e.g. LONGHOUSE_OPENCODE_RESUME_SEED_<hex>)
            # from the provider-resume/factory assurance harness. It is a
            # deterministic synthetic token, not a user request, so it never
            # deserves an AI title; the fallback title is the marker itself.
            # Belt-and-suspenders with the candidate-query skip in catalogd.
            logger.info("Skipping storage-v2 AI title for seed-marker session=%s", session_id)
            return False
        if (
            classify_provider_proof_environment(
                machine_id=candidate.get("machine_id"),
                first_user_text=first_user_message,
            )
            == "test"
        ):
            logger.info("Skipping storage-v2 AI title for provider canary session=%s", session_id)
            return False
        if is_hatch_execution_contract(first_user_message):
            logger.info("Skipping storage-v2 AI title for Hatch automation session=%s", session_id)
            return False
        from zerg.models_config import get_llm_client_for_use_case
        from zerg.services.title_generator import generate_initial_session_title

        dependency = _dependency_identity()
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
        title = sanitize_timeline_title(raw_title, max_words=6)
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
            if dependency is not None and _is_dependency_auth_failure(exc):
                await _catalog_call(
                    "storage.session.title.dependency.fail.v2",
                    {
                        "session_id": session_id,
                        **dependency,
                        "probe_token": probe_token,
                        "reason": reason,
                        "failed_at": datetime.now(UTC).isoformat(),
                    },
                )
            else:
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
