"""Process-epoch admission fence for hosted runtime cutovers.

The control plane owns durable deployment receipts.  The Runtime Host owns only
this process-local fence: a restart creates a new epoch and all old drain or
reopen requests become unknown/conflicting instead of being reported as a
successful drain.
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


class RuntimeAdmission:
    """Admission and bounded drain state for one Runtime Host process."""

    def __init__(self) -> None:
        self.runtime_epoch = uuid4().hex
        self._startup_closed = os.getenv("LONGHOUSE_DEPLOYMENT_PENDING", "").strip() == "1"
        self._state = "closed" if self._startup_closed else "open"
        self._fence: RuntimeFence | None = None
        self._request_fingerprints: dict[str, str] = {}
        self._request_results: dict[str, dict[str, Any]] = {}
        self._in_flight = 0
        self._lock = asyncio.Lock()
        self._candidate_attempt: str | None = None
        self._candidate_generation: str | None = None
        self._candidate_ready_attempt: str | None = None
        self._candidate_consistent_attempt: str | None = None
        self._process_generation = os.getenv("LONGHOUSE_DEPLOYMENT_GENERATION", "").strip() or None
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

    def _snapshot_unlocked(self) -> dict[str, Any]:
        # The Runtime Host no longer owns the catalog writer. All mutating
        # ingress that this process can admit is counted here; catalogd's
        # authoritative writer queue is reported by ping.v2/readiness.
        active_writers = self._in_flight
        queued_side_effects = 0
        return {
            "runtime_epoch": self.runtime_epoch,
            "state": self._state,
            "active_writers": active_writers,
            "queued_side_effects": queued_side_effects,
            "startup_closed": self._startup_closed,
            "drained_at": None,
        }

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            payload = self._snapshot_unlocked()
            if self._state == "drained" and payload["active_writers"] == 0 and payload["queued_side_effects"] == 0:
                payload["drained_at"] = getattr(self, "_drained_at", None)
            return payload

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
        snapshot = self._snapshot_unlocked()
        return self._in_flight == 0 and snapshot["active_writers"] == 0 and snapshot["queued_side_effects"] == 0

    def _fingerprint(self, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    async def drain(self, payload: dict[str, Any], *, attempt_id: str) -> dict[str, Any]:
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
            if self._fence is not None and self._fence.fingerprint != fingerprint:
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
        # Wait outside lock so admitted handlers can release. A request which
        # exceeds its grace remains explicitly draining, never falsely drained.
        end = min(time.monotonic() + grace, max(time.monotonic(), deadline.timestamp() - time.time()))
        while time.monotonic() < end:
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
            await asyncio.sleep(0.01)
        async with self._lock:
            result = self._snapshot_unlocked()
            result.update(
                {
                    "state": "drained" if self._is_drained_unlocked() else "draining",
                    "attempt_id": attempt_id,
                    "request_id": request_id,
                    "deployment_id": str(payload["deployment_id"]),
                    "target_id": str(payload["target_id"]),
                    "generation": str(payload["generation"]),
                    "replayed": False,
                }
            )
            if result["state"] == "drained":
                self._drained_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                result["drained_at"] = self._drained_at
            self._request_results[request_id] = dict(result)
            return result

    async def reopen(self, payload: dict[str, Any], *, attempt_id: str) -> dict[str, Any]:
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


_RUNTIME_ADMISSION = RuntimeAdmission()


def runtime_admission() -> RuntimeAdmission:
    return _RUNTIME_ADMISSION
