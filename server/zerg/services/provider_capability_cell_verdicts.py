"""Latest failed-execution verdict per class-qualified factory cell, for the public chip chart.

The factory's proof store holds passes only (a failed execution has no proof
record), so the chart cannot see a cell that started failing after its last
pass. The factory therefore mirrors one *verdict* per failed evidence class:
the newest non-pass outcome, how many executions in a row have failed, and
when. Each evidence class keeps its own latest verdict. A verdict is a fact
about "latest", not evidence: it never certifies anything, it can only stop a
pass in that same class from certifying. A pass needs no verdict; its proof
record is newer than that class's verdict and the fold ignores it from then on.

One file per class-qualified cell, replaced only by a strictly newer
``observed_at``. Reads are strict: an unreadable or inconsistent file raises,
so every projection route (public chart, machine surface, admin page share one
path) fails and the landing page renders "unavailable" rather than a chart
missing a fact.

An older classless failure is attributed to ``live_token`` only so it can
revoke a chip; it never qualifies a scripted lane or supplies proof.
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
from zerg.services.provider_capability_proof import EvidenceClass

VERDICT_BUNDLE_KIND = "provider_capability_cell_verdict_bundle"
VERDICT_SCHEMA_VERSION = 2
# Consecutive failed executions before a newer failure outranks an older pass.
# One flaky reconnect must not unlight a chip; two in a row is a real signal.
REVOKING_CONSECUTIVE_FAILURES = 2
# `observed_at` is factory-supplied; a far-future value would pin a cell's
# verdict (only strictly newer ones replace it), so refuse it.
_MAX_CLOCK_SKEW = timedelta(hours=1)
_MAX_TEXT = 256
_ACCEPTED_OUTCOMES = frozenset(outcome.value for outcome in AssertionOutcome if outcome is not AssertionOutcome.PASS)
_LEGACY_VERDICT_FIELDS = frozenset({"provider", "assertion_id", "scenario_id", "variant", "outcome", "observed_at", "consecutive_failures"})

CellKey = tuple[str, str, str, str | None, str]


class CellVerdictStoreError(RuntimeError):
    """The verdict store is unreadable or inconsistent. Never a claim."""


@dataclass(frozen=True)
class CellVerdict:
    provider: str
    assertion_id: str
    scenario_id: str
    variant: str | None
    evidence_class: EvidenceClass
    outcome: str
    observed_at: datetime
    consecutive_failures: int

    @property
    def key(self) -> CellKey:
        return (self.provider, self.assertion_id, self.scenario_id, self.variant, self.evidence_class.value)

    def serialize(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "assertion_id": self.assertion_id,
            "scenario_id": self.scenario_id,
            "variant": self.variant,
            "evidence_class": self.evidence_class.value,
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
    expected = {"provider", "assertion_id", "scenario_id", "variant", "evidence_class", "outcome", "observed_at", "consecutive_failures"}
    if set(raw) != expected:
        raise ValueError(f"verdict fields must be exactly {sorted(expected)}")
    outcome = raw["outcome"]
    if outcome not in _ACCEPTED_OUTCOMES:
        raise ValueError(f"verdict outcome must be one of {sorted(_ACCEPTED_OUTCOMES)}; a pass is a proof, not a verdict")
    try:
        evidence_class = EvidenceClass(_text(raw["evidence_class"], "evidence_class"))
    except ValueError as exc:
        raise ValueError(f"verdict evidence_class must be one of {sorted(item.value for item in EvidenceClass)}") from exc
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
        evidence_class=evidence_class,
        outcome=outcome,
        observed_at=observed.astimezone(UTC),
        consecutive_failures=failures,
    )


def _file_name(key: CellKey) -> str:
    encoded = json.dumps(list(key), ensure_ascii=False, separators=(",", ":")).encode()
    return f"{hashlib.sha256(encoded).hexdigest()}.json"


def _legacy_file_name(verdict: CellVerdict) -> str:
    """Name a pre-evidence-class verdict file using its original four-part key."""

    key = (verdict.provider, verdict.assertion_id, verdict.scenario_id, verdict.variant)
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
        legacy_keys: set[CellKey] = set()
        for path in sorted(self.root.glob("*.json")):
            is_legacy = False
            try:
                raw = json.loads(path.read_bytes())
                if isinstance(raw, Mapping) and set(raw) == _LEGACY_VERDICT_FIELDS:
                    legacy_row = dict(raw)
                    legacy_row["evidence_class"] = EvidenceClass.LIVE_TOKEN.value
                    verdict = verdict_from_mapping(legacy_row)
                    if _legacy_file_name(verdict) != path.name:
                        raise ValueError("legacy verdict file does not match its cell key")
                    is_legacy = True
                else:
                    verdict = verdict_from_mapping(raw)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise CellVerdictStoreError(f"unreadable cell verdict {path.name}: {exc}") from exc
            if not is_legacy and _file_name(verdict.key) != path.name:
                raise CellVerdictStoreError(f"cell verdict {path.name} does not match its own key")
            held = found.get(verdict.key)
            if (
                held is None
                or verdict.observed_at > held.observed_at
                or (verdict.observed_at == held.observed_at and not is_legacy and verdict.key in legacy_keys)
            ):
                found[verdict.key] = verdict
                if is_legacy:
                    legacy_keys.add(verdict.key)
                else:
                    legacy_keys.discard(verdict.key)
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
