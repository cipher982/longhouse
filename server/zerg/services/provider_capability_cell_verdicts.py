"""Latest failed-execution verdict per factory cell, for the public chip chart.

The factory's proof store holds passes only (a failed execution has no proof
record), so the chart cannot see a cell that started failing after its last
pass. The factory therefore mirrors one *verdict* per failed cell: the newest
non-pass outcome, how many executions in a row have failed, and when. This
store keeps the newest verdict per cell and nothing else. It is a fact about
"latest", not evidence: a verdict never certifies anything, it can only stop an
older pass from certifying. A pass needs no verdict; its proof record is newer
than the verdict and the fold ignores the verdict from then on.

One file per cell, replaced only by a strictly newer ``observed_at``. Reads are
strict: an unreadable or inconsistent file raises, so the public route fails
and the landing page renders "unavailable" rather than a chart missing a fact.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any

from zerg.services.provider_capability_proof import AssertionOutcome

VERDICT_BUNDLE_KIND = "provider_capability_cell_verdict_bundle"
VERDICT_SCHEMA_VERSION = 1
# Consecutive failed executions before a newer failure outranks an older pass.
# One flaky reconnect must not unlight a chip; two in a row is a real signal.
REVOKING_CONSECUTIVE_FAILURES = 2
# `observed_at` is factory-supplied; a far-future value would pin a cell's
# verdict (only strictly newer ones replace it), so refuse it.
_MAX_CLOCK_SKEW = timedelta(hours=1)
_MAX_TEXT = 256
_ACCEPTED_OUTCOMES = frozenset(outcome.value for outcome in AssertionOutcome if outcome is not AssertionOutcome.PASS)

CellKey = tuple[str, str, str, str | None]


class CellVerdictStoreError(RuntimeError):
    """The verdict store is unreadable or inconsistent. Never a claim."""


@dataclass(frozen=True)
class CellVerdict:
    provider: str
    assertion_id: str
    scenario_id: str
    variant: str | None
    outcome: str
    observed_at: datetime
    consecutive_failures: int

    @property
    def key(self) -> CellKey:
        return (self.provider, self.assertion_id, self.scenario_id, self.variant)

    def serialize(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "assertion_id": self.assertion_id,
            "scenario_id": self.scenario_id,
            "variant": self.variant,
            "outcome": self.outcome,
            "observed_at": self.observed_at.isoformat().replace("+00:00", "Z"),
            "consecutive_failures": self.consecutive_failures,
        }


def _text(value: object, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT:
        raise ValueError(f"verdict {field} must be a non-empty string of at most {_MAX_TEXT} characters")
    return value


def verdict_from_mapping(raw: object) -> CellVerdict:
    """Validate one published verdict. Raises ValueError with the reason."""

    if not isinstance(raw, Mapping):
        raise ValueError("verdict must be an object")
    expected = {"provider", "assertion_id", "scenario_id", "variant", "outcome", "observed_at", "consecutive_failures"}
    if set(raw) != expected:
        raise ValueError(f"verdict fields must be exactly {sorted(expected)}")
    outcome = raw["outcome"]
    if outcome not in _ACCEPTED_OUTCOMES:
        raise ValueError(f"verdict outcome must be one of {sorted(_ACCEPTED_OUTCOMES)}; a pass is a proof, not a verdict")
    failures = raw["consecutive_failures"]
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 1:
        raise ValueError("verdict consecutive_failures must be an integer >= 1")
    observed_raw = _text(raw["observed_at"], "observed_at")
    try:
        observed = datetime.fromisoformat(observed_raw.replace("Z", "+00:00"))  # type: ignore[union-attr]
    except ValueError as exc:
        raise ValueError("verdict observed_at must be an ISO-8601 timestamp") from exc
    if observed.tzinfo is None:
        raise ValueError("verdict observed_at must include a timezone")
    return CellVerdict(
        provider=_text(raw["provider"], "provider"),  # type: ignore[arg-type]
        assertion_id=_text(raw["assertion_id"], "assertion_id"),  # type: ignore[arg-type]
        scenario_id=_text(raw["scenario_id"], "scenario_id"),  # type: ignore[arg-type]
        variant=_text(raw["variant"], "variant", nullable=True),
        outcome=outcome,
        observed_at=observed.astimezone(UTC),
        consecutive_failures=failures,
    )


def _file_name(key: CellKey) -> str:
    encoded = json.dumps(list(key), ensure_ascii=False, separators=(",", ":")).encode()
    return f"{hashlib.sha256(encoded).hexdigest()}.json"


class CellVerdictStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def publish(self, verdicts: list[CellVerdict], *, now: datetime | None = None) -> int:
        """Keep each verdict only if strictly newer than the stored one.

        Returns how many replaced or created a cell's verdict. The stored file
        is the whole state; there is no history and nothing to rebuild. Raises
        ValueError, writing nothing, if any verdict is dated in the future.
        """

        horizon = (now or datetime.now(UTC)) + _MAX_CLOCK_SKEW
        for verdict in verdicts:
            if verdict.observed_at > horizon:
                raise ValueError("verdict observed_at is in the future")
        newest: dict[CellKey, CellVerdict] = {}
        for verdict in verdicts:
            held = newest.get(verdict.key)
            if held is None or verdict.observed_at > held.observed_at:
                newest[verdict.key] = verdict
        applied = 0
        for key, verdict in newest.items():
            path = self.root / _file_name(key)
            if path.exists() and self._read(path).observed_at >= verdict.observed_at:
                continue
            self._replace(path, json.dumps(verdict.serialize(), sort_keys=True, indent=2).encode() + b"\n")
            applied += 1
        return applied

    def verdicts(self) -> dict[CellKey, CellVerdict]:
        if not self.root.exists():
            return {}
        found: dict[CellKey, CellVerdict] = {}
        for path in sorted(self.root.glob("*.json")):
            verdict = self._read(path)
            found[verdict.key] = verdict
        return found

    def _read(self, path: Path) -> CellVerdict:
        try:
            verdict = verdict_from_mapping(json.loads(path.read_bytes()))
        except (OSError, ValueError) as exc:
            raise CellVerdictStoreError(f"unreadable cell verdict {path.name}: {exc}") from exc
        if _file_name(verdict.key) != path.name:
            raise CellVerdictStoreError(f"cell verdict {path.name} does not match its own key")
        return verdict

    @staticmethod
    def _replace(destination: Path, payload: bytes) -> None:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}-", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


__all__ = [
    "REVOKING_CONSECUTIVE_FAILURES",
    "VERDICT_BUNDLE_KIND",
    "VERDICT_SCHEMA_VERSION",
    "CellKey",
    "CellVerdict",
    "CellVerdictStore",
    "CellVerdictStoreError",
    "verdict_from_mapping",
]
