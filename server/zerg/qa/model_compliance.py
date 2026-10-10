"""Producers report a model's deviation only when the run proves Longhouse did its part.

An observation may carry ``model_noncompliance``: for an assertion that is False,
an entry says the deviation is the model's choice, and names the Longhouse-side
facts the run already proved. It never changes an assertion's value or failure
code; it is only added evidence for the factory to read.
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


def attach(observation: dict[str, Any], assertion_id: str, entry: dict[str, Any]) -> None:
    if not isinstance(assertion_id, str) or not assertion_id:
        raise ValueError("model non-compliance needs an assertion id")
    # Re-validate here so a hand-built entry cannot bypass the contract.
    noncompliance_entry(entry.get("reason"), **(entry.get("contract_evidence") or {}))
    observation.setdefault("model_noncompliance", {})[assertion_id] = entry
