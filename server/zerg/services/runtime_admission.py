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
from datetime import timedelta
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
    claim_expected_back_by: str | None = None
    claim_deadline: str | None = None
    claim_cutoff: str | None = None


DEFAULT_DRAIN_HORIZONS_SECONDS = {"expected_back_by": 30, "deadline": 360, "cutoff": 960}
DEFAULT_PENDING_HORIZONS_SECONDS = {"expected_back_by": 15, "deadline": 300, "cutoff": 960}
PENDING_REOPEN_MAX_WAIT_SECONDS = 10.0
_DEPLOYER_PHASES = frozenset(
    {
        "prepare",
        "drain",
        "stop",
        "recovery_point",
        "migrate",
        "start",
        "readiness",
        "probe",
        "reopen",
        "rollback_stop",
        "rollback_start",
        "rollback_readiness",
        "rollback_probe",
        "rollback_reopen",
    }
)


def _parse_claim_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _format_claim_time(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value is not None else None


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
        self._reopen_event = asyncio.Event()
        self._initial_open_event = asyncio.Event()
        if not self._startup_closed:
            self._reopen_event.set()
            self._initial_open_event.set()
        self._candidate_attempt: str | None = None
        self._candidate_generation: str | None = None
        self._candidate_ready_attempt: str | None = None
        self._candidate_consistent_attempt: str | None = None
        self._process_generation = os.getenv("LONGHOUSE_DEPLOYMENT_GENERATION", "").strip() or None
        self._process_image_digest = os.getenv("LONGHOUSE_IMAGE_DIGEST", "").strip() or None
        self.deployment_id = os.getenv("LONGHOUSE_DEPLOYMENT_ID") or None
        self.target_id = os.getenv("LONGHOUSE_TARGET_ID") or None
        self._claim_attempt_id = os.getenv("LONGHOUSE_DEPLOYMENT_ATTEMPT_ID", "").strip() or None
        self._claim_phase: str | None = None
        self._claim_expected_back_by: datetime | None = None
        self._claim_deadline: datetime | None = None
        self._claim_cutoff: datetime | None = None
        if self._startup_closed:
            self._set_default_claim_horizons(DEFAULT_PENDING_HORIZONS_SECONDS)
        env_horizons = {
            "expected_back_by": _parse_claim_time(os.getenv("LONGHOUSE_CLAIM_EXPECTED_BACK_BY")),
            "deadline": _parse_claim_time(os.getenv("LONGHOUSE_CLAIM_DEADLINE")),
            "cutoff": _parse_claim_time(os.getenv("LONGHOUSE_CLAIM_CUTOFF")),
        }
        if any(value is not None for value in env_horizons.values()):
            self._set_claim_horizons(env_horizons, DEFAULT_PENDING_HORIZONS_SECONDS)

    def _set_default_claim_horizons(self, defaults: dict[str, int]) -> None:
        now = datetime.now(timezone.utc)
        self._claim_expected_back_by = now + timedelta(seconds=defaults["expected_back_by"])
        self._claim_deadline = now + timedelta(seconds=defaults["deadline"])
        self._claim_cutoff = now + timedelta(seconds=defaults["cutoff"])

    def _set_claim_horizons(self, values: dict[str, datetime | None], defaults: dict[str, int]) -> None:
        now = datetime.now(timezone.utc)
        self._claim_expected_back_by = values.get("expected_back_by") or now + timedelta(seconds=defaults["expected_back_by"])
        self._claim_deadline = values.get("deadline") or now + timedelta(seconds=defaults["deadline"])
        self._claim_cutoff = values.get("cutoff") or now + timedelta(seconds=defaults["cutoff"])
        if self._claim_deadline > self._claim_cutoff:
            self._claim_deadline = self._claim_cutoff

    def _admission_unlocked(self) -> str:
        if self._state in {"open", "reopened"} and not self._startup_closed:
            return "open"
        if self._startup_closed or self._state == "closed":
            return "pending"
        return "draining"

    def _host_lifecycle_unlocked(self) -> dict[str, Any]:
        serving = self._admission_unlocked() == "open"
        phase = self._claim_phase
        if serving and phase is None and self._state == "reopened":
            phase = "reopen"
        return {
            "type": "host.lifecycle",
            "state": "serving" if serving else "updating",
            "runtime_epoch": self.runtime_epoch,
            "attempt_id": self._claim_attempt_id or (self._fence.attempt_id if self._fence else None),
            "phase": phase,
            "expected_back_by": None if serving else _format_claim_time(self._claim_expected_back_by),
            "deadline": None if serving else _format_claim_time(self._claim_deadline),
            "cutoff": None if serving else _format_claim_time(self._claim_cutoff),
        }

    def host_lifecycle(self) -> dict[str, Any]:
        return self._host_lifecycle_unlocked()

    async def renew_claim(self, *, claim_deadline: str | None, attempt_id: str, phase: str) -> dict[str, Any]:
        async with self._lock:
            current_attempt = self._claim_attempt_id or (self._fence.attempt_id if self._fence else None)
            if current_attempt not in {None, attempt_id}:
                return self._snapshot_unlocked()
            self._claim_attempt_id = attempt_id
            self._claim_phase = phase if phase in _DEPLOYER_PHASES else self._claim_phase
            requested_deadline = _parse_claim_time(claim_deadline)
            if requested_deadline is not None:
                if self._claim_cutoff is None:
                    self._claim_cutoff = datetime.now(timezone.utc) + timedelta(seconds=DEFAULT_PENDING_HORIZONS_SECONDS["cutoff"])
                self._claim_deadline = min(requested_deadline, self._claim_cutoff)
            return self._snapshot_unlocked()

    async def wait_for_reopen(self, *, max_wait_seconds: float = PENDING_REOPEN_MAX_WAIT_SECONDS) -> bool:
        async with self._lock:
            admission = self._admission_unlocked()
            if admission == "open":
                return True
            if admission != "pending":
                return False
            now = datetime.now(timezone.utc)
            claim_remaining = (
                max(0.0, (self._claim_deadline - now).total_seconds()) if self._claim_deadline is not None else max_wait_seconds
            )
            timeout = min(max_wait_seconds, claim_remaining)
            event = self._reopen_event
        if timeout <= 0:
            return False
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        async with self._lock:
            return self._admission_unlocked() == "open"

    @property
    def initially_opened(self) -> bool:
        return self._initial_open_event.is_set()

    async def wait_until_initial_open(self) -> None:
        """Wait for the first successful reopen; used only by deferred startup work."""
        await self._initial_open_event.wait()

    @property
    def admission(self) -> str:
        return self._admission_unlocked()

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
        if self._claim_attempt_id not in {None, attempt_id}:
            raise ValueError("candidate readiness fence conflicts with the deployment attempt")
        if self._candidate_attempt is None:
            self._candidate_attempt = attempt_id
            self._candidate_generation = candidate_generation
            self._startup_closed = True
            self._claim_attempt_id = attempt_id
            self._claim_phase = "readiness"
            if self._claim_deadline is None:
                self._set_default_claim_horizons(DEFAULT_PENDING_HORIZONS_SECONDS)
            self._reopen_event.clear()
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
            self._claim_phase = "reopen"
            self._reopen_event.set()
            self._initial_open_event.set()
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
            "admission": self._admission_unlocked(),
            "claim": None if self._admission_unlocked() == "open" else self._host_lifecycle_unlocked(),
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
            admission = self._admission_unlocked()
            if admission == "open":
                self._in_flight += 1
                return True, self._snapshot_unlocked()
            should_wait = admission == "pending"

        if should_wait and await self.wait_for_reopen():
            async with self._lock:
                if self._admission_unlocked() == "open":
                    self._in_flight += 1
                    return True, self._snapshot_unlocked()

        async with self._lock:
            return False, self.restarting_payload(path=path)

    def restarting_payload(self, *, path: str) -> dict[str, Any]:
        """K1 body for a request this process cannot serve yet."""
        payload = self._snapshot_unlocked()
        payload.update(
            {
                "retryable": True,
                "code": "runtime_restarting",
                "message": "Runtime is restarting; retry after reopen with the same request identity.",
                "path": path,
            }
        )
        return payload

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
        on_drain_start: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
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
        defaults = DEFAULT_DRAIN_HORIZONS_SECONDS
        claim_values: dict[str, datetime | None] = {}
        for field, key in (
            ("expected_back_by", "expected_back_by"),
            ("claim_deadline", "deadline"),
            ("claim_cutoff", "cutoff"),
        ):
            raw_value = payload.get(field)
            parsed_value = _parse_claim_time(raw_value)
            if raw_value is not None and str(raw_value).strip() and parsed_value is None:
                return {
                    "state": "conflict",
                    "code": "invalid_fence",
                    "message": f"{field} must be a timezone-aware RFC 3339 timestamp",
                    "runtime_epoch": self.runtime_epoch,
                }
            claim_values[key] = parsed_value
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
            for key in (
                "request_id",
                "deployment_id",
                "target_id",
                "generation",
                "deadline_utc",
                "grace_seconds",
                "runtime_epoch",
                "expected_back_by",
                "claim_deadline",
                "claim_cutoff",
            )
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
            self._set_claim_horizons(claim_values, defaults)
            self._claim_attempt_id = attempt_id
            self._claim_phase = "drain"
            self._reopen_event.clear()
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
                claim_expected_back_by=_format_claim_time(self._claim_expected_back_by),
                claim_deadline=_format_claim_time(self._claim_deadline),
                claim_cutoff=_format_claim_time(self._claim_cutoff),
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
        if on_drain_start is not None:
            await on_drain_start(self.host_lifecycle())
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
                self._claim_attempt_id = attempt_id
                self._claim_phase = "reopen"
                self._reopen_event.set()
                self._initial_open_event.set()
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
            self._claim_attempt_id = attempt_id
            self._claim_phase = "reopen"
            self._reopen_event.set()
            self._initial_open_event.set()
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
