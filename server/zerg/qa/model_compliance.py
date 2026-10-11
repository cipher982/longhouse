"""Producers report a model's deviation only when the run proves Longhouse did its part.

An observation may carry ``model_noncompliance``: for an assertion that is False,
an entry says the deviation is the model's choice, and names the Longhouse-side
facts the run already proved. It never changes an assertion's value or failure
code; it is only added evidence for the factory to read.

A producer that has to stop before its observation, because the reply it needs
to go on never came, says the same thing by raising `ModelDeviation`. Its
failure artifact then carries the one entry at its top level
(``failure_claim``): the scenario stopped, so the entry is about every
assertion the scenario would have judged.
"""

from __future__ import annotations

import re
from typing import Any

_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


def noncompliance_entry(reason: str, **contract_evidence: bool) -> dict[str, Any]:
    if not isinstance(reason, str) or not _SNAKE_CASE.fullmatch(reason):
        raise ValueError(f"model non-compliance reason must be snake_case, got {reason!r}")
    if not contract_evidence:
        raise ValueError("model non-compliance needs at least one contract_evidence fact")
    for fact, value in contract_evidence.items():
        if value is not True:
            raise ValueError(f"contract_evidence fact {fact!r} must be literal True, got {value!r}")
    return {"reason": reason, "contract_evidence": dict(contract_evidence)}


class ModelDeviation(RuntimeError):
    """A producer stopped on a reply the model chose, having proved Longhouse's part of the turn.

    The message opens with the reason and carries no model text, so whoever
    classifies it reads the producer's words and never the model's.
    ``entry`` is the claim (`noncompliance_entry`).
    """

    def __init__(self, reason: str, detail: str, **contract_evidence: bool) -> None:
        self.entry = noncompliance_entry(reason, **contract_evidence)
        self.reason = reason
        super().__init__(f"{reason}: {detail}")


def failure_claim(error: BaseException | None) -> dict[str, Any]:
    """What a failure artifact adds for ``error``: its model non-compliance entry, when it is one.

    Only the error itself and the errors it was explicitly raised from are
    read, as for a wait's kind (``zerg.qa.wait_kind.waited_on``).
    """

    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, ModelDeviation):
            return {"model_noncompliance": dict(error.entry)}
        seen.add(id(error))
        error = error.__cause__
    return {}


def attach(observation: dict[str, Any], assertion_id: str, entry: dict[str, Any]) -> None:
    if not isinstance(assertion_id, str) or not assertion_id:
        raise ValueError("model non-compliance needs an assertion id")
    # Re-validate here so a hand-built entry cannot bypass the contract.
    noncompliance_entry(entry.get("reason"), **(entry.get("contract_evidence") or {}))
    observation.setdefault("model_noncompliance", {})[assertion_id] = entry
