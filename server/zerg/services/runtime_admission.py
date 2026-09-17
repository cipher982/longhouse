"""Process-epoch admission fence for hosted runtime cutovers.

Catalogd owns the durable exact image/generation activation receipt. The Runtime
Host owns only this process-local fence: a restart creates a new epoch and all
old drain or reopen requests become unknown/conflicting instead of being
reported as a successful drain.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Awaitable
from typing import Callable
from uuid import uuid4


@dataclass(frozen=True)
class RuntimeFence:
    attempt_id: str
    request_id: str
    deployment_id: str
    target_id: str
    generation: str
    deadline_utc: str
    grace_seconds: float
    runtime_epoch: str
    fingerprint: str


CatalogAdmissionProbe = Callable[[str], Awaitable[dict[str, Any]]]
CatalogActivationProbe = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class RuntimeAdmission:
    """Admission and bounded drain state for one Runtime Host process."""

    def __init__(self) -> None:
        self.runtime_epoch = uuid4().hex
        self._startup_closed = os.getenv("LONGHOUSE_DEPLOYMENT_PENDING", "").strip() == "1"
        self._state = "closed" if self._startup_closed else "open"
        self._fence: RuntimeFence | None = None
        self._request_fingerprints: dict[str, str] = {}
        self._in_flight = 0
        self._request_results: dict[str, dict[str, Any]] = {}
        self._drained_at: str | None = None
        self._catalog_admission: dict[str, Any] = {
            "available": False,
            "state": "unknown",
            "depth": None,
            "accepting": None,
            "detail": "catalog writer admission has not been observed",
        }
        self._lock = asyncio.Lock()
        self._candidate_attempt: str | None = None
        self._candidate_generation: str | None = None
        self._candidate_ready_attempt: str | None = None
        self._candidate_consistent_attempt: str | None = None
        self._process_generation = os.getenv("LONGHOUSE_DEPLOYMENT_GENERATION", "").strip() or None
        self._process_image_digest = os.getenv("LONGHOUSE_IMAGE_DIGEST", "").strip() or None
        self.deployment_id = os.getenv("LONGHOUSE_DEPLOYMENT_ID") or None
        self.target_id = os.getenv("LONGHOUSE_TARGET_ID") or None

    @property
    def state(self) -> str:
        return self._state

    @property
    def fence(self) -> RuntimeFence | None:
        return self._fence

    def observe_candidate(self, *, attempt_id: str | None, generation: str | None) -> None:
        """Close a candidate when readiness is requested for a cutover attempt."""
        if not attempt_id:
            return
        candidate_generation = str(generation or "").strip() or None
        if candidate_generation is None:
            raise ValueError("candidate generation is required for cutover readiness")
        if self._process_generation is None:
            raise ValueError("candidate generation is unavailable from process startup metadata")
        if candidate_generation != self._process_generation:
            raise ValueError("candidate generation does not match this runtime")
        if self._candidate_attempt is None:
            self._candidate_attempt = attempt_id
            self._candidate_generation = candidate_generation
            self._startup_closed = True
            if self._state == "open":
                self._state = "closed"
            return
        if self._candidate_attempt != attempt_id or self._candidate_generation != candidate_generation:
            raise ValueError("candidate readiness fence conflicts with this runtime")

    def mark_candidate_ready(self, *, attempt_id: str) -> None:
        if self._candidate_attempt not in {None, attempt_id}:
            raise ValueError("candidate readiness fence conflicts with this runtime")
        self._candidate_ready_attempt = attempt_id

    def mark_candidate_consistent(self, *, attempt_id: str) -> None:
        if self._candidate_attempt not in {None, attempt_id}:
            raise ValueError("candidate consistency fence conflicts with this runtime")
        if self._candidate_ready_attempt != attempt_id:
            raise ValueError("candidate consistency requires successful readiness")
        self._candidate_consistent_attempt = attempt_id

    async def recover_startup(
        self,
        activation_probe: CatalogActivationProbe | None,
        catalog_probe: CatalogAdmissionProbe | None,
    ) -> dict[str, Any]:
        """Reopen a pending process only when its exact durable activation matches."""

        async with self._lock:
            if not self._startup_closed:
                return self._snapshot_unlocked()
            if activation_probe is None or catalog_probe is None:
                result = self._snapshot_unlocked()
                result.update(
                    {
                        "state": "unknown",
                        "code": "activation_unavailable",
                        "message": "durable activation authority is unavailable",
                    }
                )
                return result
            if self._process_generation is None or self._process_image_digest is None:
                result = self._snapshot_unlocked()
                result.update(
                    {
                        "state": "unknown",
                        "code": "activation_identity_unavailable",
                        "message": "runtime image or generation identity is unavailable",
                    }
                )
                return result
            try:
                evidence = await activation_probe("read", {})
            except Exception as exc:
                evidence = {"available": False, "activation": None, "detail": str(exc) or "activation read failed"}
            activation = evidence.get("activation") if isinstance(evidence, dict) else None
            if not isinstance(evidence, dict) or evidence.get("available") is not True:
                result = self._snapshot_unlocked()
                result.update(
                    {
                        "state": "unknown",
                        "code": "activation_unavailable",
                        "message": evidence.get("detail") if isinstance(evidence, dict) else "activation read failed",
                    }
                )
                return result
            if (
                not isinstance(activation, dict)
                or activation.get("image_digest") != self._process_image_digest
                or str(activation.get("generation") or "").strip() != self._process_generation
            ):
                result = self._snapshot_unlocked()
                result.update(
                    {
                        "state": "closed",
                        "code": "activation_mismatch",
                        "message": "durable activation evidence does not match this runtime",
                    }
                )
                return result
            opened = await self._catalog_operation(catalog_probe, "open")
            self._set_catalog_admission_unlocked(opened or {})
            if not self._catalog_open_ready(opened):
                await self._catalog_fail_closed(catalog_probe)
                result = self._snapshot_unlocked()
                result.update(
                    {
                        "state": "unknown",
                        "code": "catalog_unavailable",
                        "message": "catalog writer admission could not be reopened from durable activation",
                    }
                )
                return result
            self._state = "reopened"
            self._startup_closed = False
            result = self._snapshot_unlocked()
            result.update(
                {
                    "state": "reopened",
                    "startup_recovered": True,
                    "activation": dict(activation),
                }
            )
            return result

    @staticmethod
    def _catalog_open_ready(admission: dict[str, Any] | None) -> bool:
        return bool(
            isinstance(admission, dict)
            and admission.get("available") is True
            and admission.get("state") == "open"
            and admission.get("accepting") is True
            and admission.get("depth") == 0
            and admission.get("active_label") is None
        )

    def _set_catalog_admission_unlocked(self, admission: dict[str, Any]) -> None:
        self._catalog_admission = dict(admission)

    def _catalog_quiescent_unlocked(self) -> bool:
        catalog = self._catalog_admission
        return (
            catalog.get("available") is True
            and catalog.get("state") == "closed"
            and type(catalog.get("depth")) is int
            and catalog["depth"] == 0
            and catalog.get("active_label") is None
        )

    def _snapshot_unlocked(self) -> dict[str, Any]:
        catalog = self._catalog_admission
        catalog_depth = catalog.get("depth")
        catalog_known = (
            catalog.get("available") is True
            and catalog.get("state") in {"open", "closed"}
            and type(catalog_depth) is int
            and type(catalog.get("accepting")) is bool
        )
        active_writers = self._in_flight + catalog_depth if catalog_known else None
        queued_side_effects = 0 if catalog_known else None
        state = self._state
        if state == "drained" and not self._catalog_quiescent_unlocked():
            state = "draining"
        return {
            "runtime_epoch": self.runtime_epoch,
            "state": state,
            "active_writers": active_writers,
            "queued_side_effects": queued_side_effects,
            "runtime_in_flight": self._in_flight,
            "catalog_admission": dict(catalog),
            "startup_closed": self._startup_closed,
            "drained_at": None,
        }

    async def snapshot(self, *, catalog_admission: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._lock:
            if catalog_admission is not None:
                self._set_catalog_admission_unlocked(catalog_admission)
            if self._state == "draining" and self._is_drained_unlocked():
                self._state = "drained"
                self._drained_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            payload = self._snapshot_unlocked()
            if self._state == "drained" and not self._is_drained_unlocked():
                payload["state"] = "draining"
            if self._state == "drained" and self._is_drained_unlocked():
                payload["drained_at"] = getattr(self, "_drained_at", None)
            return payload

    async def update_catalog_admission(self, admission: dict[str, Any]) -> dict[str, Any]:
        return await self.snapshot(catalog_admission=admission)

    async def try_admit(self, *, path: str) -> tuple[bool, dict[str, Any]]:
        async with self._lock:
            if self._state in {"draining", "drained", "closed"} or self._startup_closed:
                payload = self._snapshot_unlocked()
                payload.update(
                    {
                        "retryable": True,
                        "code": "runtime_draining",
                        "message": "Runtime is restarting; retry after reopen with the same request identity.",
                        "path": path,
                    }
                )
                return False, payload
            self._in_flight += 1
            return True, self._snapshot_unlocked()

    async def release(self) -> None:
        async with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            if self._state == "draining" and self._is_drained_unlocked():
                self._state = "drained"
                self._drained_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _is_drained_unlocked(self) -> bool:
        return self._in_flight == 0 and self._catalog_quiescent_unlocked()

    def _fingerprint(self, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    async def drain(
        self,
        payload: dict[str, Any],
        *,
        attempt_id: str,
        catalog_probe: CatalogAdmissionProbe | None = None,
    ) -> dict[str, Any]:
        required = ("request_id", "deployment_id", "target_id", "generation", "deadline_utc", "grace_seconds")
        if any(not str(payload.get(key) or "").strip() for key in required[:-1]):
            return {
                "state": "conflict",
                "code": "invalid_fence",
                "message": "drain fence is incomplete",
                "runtime_epoch": self.runtime_epoch,
            }
        try:
            grace = float(payload.get("grace_seconds"))
        except (TypeError, ValueError):
            return {
                "state": "conflict",
                "code": "invalid_fence",
                "message": "grace_seconds is invalid",
                "runtime_epoch": self.runtime_epoch,
            }
        if grace < 0 or grace > 300:
            return {
                "state": "conflict",
                "code": "invalid_fence",
                "message": "grace_seconds must be between 0 and 300",
                "runtime_epoch": self.runtime_epoch,
            }
        try:
            deadline = datetime.fromisoformat(str(payload["deadline_utc"]).replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                raise ValueError
        except ValueError:
            return {
                "state": "conflict",
                "code": "invalid_fence",
                "message": "deadline_utc must be timezone-aware ISO-8601",
                "runtime_epoch": self.runtime_epoch,
            }
        runtime_epoch = str(payload.get("runtime_epoch") or "").strip()
        if runtime_epoch and runtime_epoch != self.runtime_epoch:
            return {
                "state": "unknown",
                "code": "runtime_epoch_mismatch",
                "message": "request belongs to a different runtime process epoch",
                "runtime_epoch": self.runtime_epoch,
            }
        canonical = {
            key: payload.get(key)
            for key in ("request_id", "deployment_id", "target_id", "generation", "deadline_utc", "grace_seconds", "runtime_epoch")
        }
        fingerprint = self._fingerprint(canonical)
        request_id = str(payload["request_id"])
        async with self._lock:
            existing_fingerprint = self._request_fingerprints.get(request_id)
            if existing_fingerprint is not None and existing_fingerprint != fingerprint:
                return {
                    "state": "conflict",
                    "code": "request_id_reused",
                    "message": "request_id was reused with different deployment fence",
                    "runtime_epoch": self.runtime_epoch,
                }
            if existing_fingerprint is not None:
                # Reopen completed this fence; replaying it must not close the newly opened catalog.
                if self._state != "reopened":
                    admission = await self._catalog_operation(catalog_probe, "close")
                    if admission is not None:
                        self._set_catalog_admission_unlocked(admission)
                existing = dict(self._request_results[request_id])
                if self._state == "draining" and self._is_drained_unlocked():
                    self._state = "drained"
                    self._drained_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    existing.update(self._snapshot_unlocked(), state="drained", drained_at=self._drained_at)
                    self._request_results[request_id] = dict(existing)
                else:
                    existing.update(self._snapshot_unlocked(), state=self._state)
                    self._request_results[request_id] = dict(existing)
                existing["replayed"] = True
                return existing
            # Keep the completed fence for identity checks, but let the next cutover claim a new one.
            if self._fence is not None and self._fence.fingerprint != fingerprint and self._state != "reopened":
                return {
                    "state": "conflict",
                    "code": "different_active_fence",
                    "message": "another deployment fence is active",
                    "runtime_epoch": self.runtime_epoch,
                }
            self._fence = RuntimeFence(
                attempt_id=attempt_id,
                request_id=request_id,
                deployment_id=str(payload["deployment_id"]),
                target_id=str(payload["target_id"]),
                generation=str(payload["generation"]),
                deadline_utc=deadline.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                grace_seconds=grace,
                runtime_epoch=self.runtime_epoch,
                fingerprint=fingerprint,
            )
            self._state = "draining"
            self._request_fingerprints[request_id] = fingerprint
            result = self._snapshot_unlocked()
            result.update(
                {
                    "state": "draining",
                    "attempt_id": attempt_id,
                    "request_id": request_id,
                    "deployment_id": str(payload["deployment_id"]),
                    "target_id": str(payload["target_id"]),
                    "generation": str(payload["generation"]),
                    "replayed": False,
                }
            )
            self._request_results[request_id] = dict(result)
        # The control-plane deadline is UTC; turn its remaining duration into a
        # single monotonic deadline before waiting. Mixing the UTC timestamp
        # directly with monotonic time collapses the bound to "now".
        now = time.monotonic()
        remaining_utc = max(0.0, deadline.astimezone(timezone.utc).timestamp() - time.time())
        end = now + min(grace, remaining_utc)
        while True:
            if catalog_probe is not None:
                try:
                    admission = await catalog_probe("close")
                except Exception as exc:
                    admission = {
                        "available": False,
                        "state": "unknown",
                        "depth": None,
                        "accepting": None,
                        "detail": str(exc) or "catalog writer admission unavailable",
                    }
                async with self._lock:
                    self._set_catalog_admission_unlocked(admission)
            async with self._lock:
                if self._is_drained_unlocked():
                    self._state = "drained"
                    self._drained_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    result = self._snapshot_unlocked()
                    result.update(
                        {
                            "state": "drained",
                            "attempt_id": attempt_id,
                            "request_id": request_id,
                            "deployment_id": str(payload["deployment_id"]),
                            "target_id": str(payload["target_id"]),
                            "generation": str(payload["generation"]),
                            "drained_at": self._drained_at,
                            "replayed": False,
                        }
                    )
                    self._request_results[request_id] = dict(result)
                    return result
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.01, remaining))
        async with self._lock:
            result = self._snapshot_unlocked()
            result.update(
                {
                    "state": "draining",
                    "attempt_id": attempt_id,
                    "request_id": request_id,
                    "deployment_id": str(payload["deployment_id"]),
                    "target_id": str(payload["target_id"]),
                    "generation": str(payload["generation"]),
                    "replayed": False,
                }
            )
            self._request_results[request_id] = dict(result)
            return result

    async def _catalog_operation(self, catalog_probe: CatalogAdmissionProbe | None, operation: str) -> dict[str, Any] | None:
        if catalog_probe is None:
            return None
        try:
            return await catalog_probe(operation)
        except Exception as exc:
            return {
                "available": False,
                "state": "unknown",
                "depth": None,
                "accepting": None,
                "detail": str(exc) or f"catalog writer admission {operation} unavailable",
            }

    async def _activation_operation(
        self,
        activation_probe: CatalogActivationProbe | None,
        operation: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if activation_probe is None:
            return {
                "available": False,
                "activation": None,
                "detail": f"catalog activation {operation} unavailable",
            }
        try:
            result = await activation_probe(operation, params)
        except Exception as exc:
            return {
                "available": False,
                "activation": None,
                "detail": str(exc) or f"catalog activation {operation} unavailable",
            }
        return (
            result
            if isinstance(result, dict)
            else {
                "available": False,
                "activation": None,
                "detail": "catalog activation response is malformed",
            }
        )

    async def reopen(
        self,
        payload: dict[str, Any],
        *,
        attempt_id: str,
        catalog_probe: CatalogAdmissionProbe | None = None,
        activation_probe: CatalogActivationProbe | None = None,
    ) -> dict[str, Any]:
        request_id = str(payload.get("request_id") or "").strip()
        expected_epoch = str(payload.get("runtime_epoch") or "").strip()
        async with self._lock:
            if expected_epoch != self.runtime_epoch:
                return {
                    "state": "unknown",
                    "code": "runtime_epoch_mismatch",
                    "message": "request belongs to a different runtime process epoch",
                    "runtime_epoch": self.runtime_epoch,
                }
            fence = self._fence
            if fence is None and self._startup_closed:
                if self._candidate_ready_attempt != attempt_id:
                    return {
                        "state": "conflict",
                        "code": "candidate_not_ready",
                        "message": "candidate must pass readiness before reopen",
                        "runtime_epoch": self.runtime_epoch,
                    }
                if self._candidate_consistent_attempt != attempt_id:
                    return {
                        "state": "conflict",
                        "code": "candidate_not_consistent",
                        "message": "candidate must pass read consistency before reopen",
                        "runtime_epoch": self.runtime_epoch,
                    }
                if self._candidate_attempt not in {None, attempt_id}:
                    return {
                        "state": "conflict",
                        "code": "candidate_attempt_mismatch",
                        "message": "reopen does not match the candidate attempt",
                        "runtime_epoch": self.runtime_epoch,
                    }
                generation = str(payload.get("generation") or "").strip()
                if self._candidate_generation not in {None, generation}:
                    return {
                        "state": "conflict",
                        "code": "candidate_generation_mismatch",
                        "message": "reopen does not match the candidate generation",
                        "runtime_epoch": self.runtime_epoch,
                    }
                if self._process_image_digest is None or self._process_generation != generation:
                    return {
                        "state": "unknown",
                        "code": "activation_identity_unavailable",
                        "message": "candidate image or generation identity is unavailable",
                        "runtime_epoch": self.runtime_epoch,
                    }
                canonical = {
                    key: payload.get(key)
                    for key in (
                        "request_id",
                        "deployment_id",
                        "target_id",
                        "generation",
                        "deadline_utc",
                        "grace_seconds",
                        "runtime_epoch",
                    )
                }
                fingerprint = self._fingerprint(canonical)
                deadline = datetime.fromisoformat(
                    str(payload["deadline_utc"]).replace("Z", "+00:00"),
                )
                fence = RuntimeFence(
                    attempt_id=attempt_id,
                    request_id=request_id,
                    deployment_id=str(payload["deployment_id"]),
                    target_id=str(payload["target_id"]),
                    generation=generation,
                    deadline_utc=deadline.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    grace_seconds=float(payload["grace_seconds"]),
                    runtime_epoch=self.runtime_epoch,
                    fingerprint=fingerprint,
                )
                activation = await self._activation_operation(
                    activation_probe,
                    "record",
                    {
                        "image_digest": self._process_image_digest,
                        "generation": generation,
                    },
                )
                recorded = activation.get("activation")
                if (
                    activation.get("available") is not True
                    or not isinstance(recorded, dict)
                    or recorded.get("image_digest") != self._process_image_digest
                    or str(recorded.get("generation") or "").strip() != generation
                    or not self._catalog_open_ready(activation)
                ):
                    await self._catalog_fail_closed(catalog_probe)
                    result = self._snapshot_unlocked()
                    result.update(
                        {
                            "state": "unknown",
                            "code": "activation_unavailable",
                            "message": activation.get("detail") or "durable activation could not be recorded before reopening",
                            "attempt_id": attempt_id,
                            "request_id": request_id,
                        }
                    )
                    return result
                self._set_catalog_admission_unlocked(activation)
                self._fence = fence
                self._request_fingerprints[request_id] = fingerprint
                self._state = "reopened"
                self._startup_closed = False
                result = self._snapshot_unlocked()
                result.update(
                    {
                        "state": "reopened",
                        "attempt_id": attempt_id,
                        "request_id": request_id,
                        "deployment_id": fence.deployment_id,
                        "target_id": fence.target_id,
                        "generation": fence.generation,
                        "activation": dict(recorded),
                        "replayed": False,
                    }
                )
                return result
            if fence is None or fence.attempt_id != attempt_id or fence.request_id != request_id:
                return {
                    "state": "conflict",
                    "code": "fence_mismatch",
                    "message": "reopen does not match the active drain fence",
                    "runtime_epoch": self.runtime_epoch,
                }
            if self._state not in {"drained", "draining"}:
                return {
                    "state": "conflict",
                    "code": "invalid_state",
                    "message": "runtime has no drained fence",
                    "runtime_epoch": self.runtime_epoch,
                }
            if self._catalog_admission.get("available") is not True:
                result = self._snapshot_unlocked()
                result.update(
                    {
                        "state": "unknown",
                        "code": "catalog_unavailable",
                        "message": "catalog writer admission is unavailable",
                        "attempt_id": attempt_id,
                        "request_id": request_id,
                    }
                )
                return result
            if not self._is_drained_unlocked():
                result = self._snapshot_unlocked()
                result.update(
                    {
                        "state": "draining",
                        "attempt_id": attempt_id,
                        "request_id": request_id,
                        "code": "writers_active",
                        "message": "runtime still has admitted writes",
                    }
                )
                return result
            opened = await self._catalog_operation(catalog_probe, "open")
            if opened is not None:
                self._set_catalog_admission_unlocked(opened)
                if not (
                    opened.get("available") is True
                    and opened.get("state") == "open"
                    and opened.get("accepting") is True
                    and opened.get("depth") == 0
                    and opened.get("active_label") is None
                ):
                    await self._catalog_fail_closed(catalog_probe)
                    result = self._snapshot_unlocked()
                    result.update(
                        {
                            "state": "unknown",
                            "code": "catalog_unavailable",
                            "message": "catalog writer admission could not be reopened",
                            "attempt_id": attempt_id,
                            "request_id": request_id,
                        }
                    )
                    return result
            self._state = "reopened"
            self._startup_closed = False
            result = self._snapshot_unlocked()
            result.update(
                {
                    "state": "reopened",
                    "attempt_id": attempt_id,
                    "request_id": request_id,
                    "deployment_id": fence.deployment_id,
                    "target_id": fence.target_id,
                    "generation": fence.generation,
                    "replayed": False,
                }
            )
            return result

    async def _catalog_fail_closed(self, catalog_probe: CatalogAdmissionProbe | None) -> None:
        closed = await self._catalog_operation(catalog_probe, "close")
        if closed is not None:
            self._set_catalog_admission_unlocked(closed)


_RUNTIME_ADMISSION = RuntimeAdmission()


def runtime_admission() -> RuntimeAdmission:
    return _RUNTIME_ADMISSION
