"""How a failed producer result is shaped, with no Longhouse imports.

Producers import this, never ``factory_registration``: the provider factory pins
every module a producer imports (its verifier closure), and this file must stay a
leaf so pinning it does not pull the server in.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def settle_failed_result(failure: dict[str, Any], *, observation: Any, assertions: Mapping[str, Any]) -> dict[str, Any]:
    """Finish a failed producer result: the rule the coordination producers follow, and every producer should.

    A failure with evidence carries its observation and the verdict it reached. A
    failure before any observation existed (no coordination authority, a launch or
    bridge that never came up, a precondition crash) reached no verdict, so it
    carries neither and is a typed harness failure (``typed_harness_failure``):
    the factory reports its cause as infrastructure or harness, where a false
    assertion would be filed as a product finding.
    """

    if isinstance(observation, Mapping):
        failure["observation"] = observation
        failure["assertions"] = dict(assertions)
    else:
        failure.pop("observation", None)
        failure.pop("assertions", None)
    return failure


def typed_harness_failure(result: Mapping[str, Any]) -> bool:
    """A producer that could not reach its observation boundary and says so, which the factory records as its cause.

    Mirrors the branch at the top of control-plane ``_validate_execution_outcome``: a failing result with a
    ``failure_code`` and an ``error`` and neither an ``observation`` nor an ``assertions`` object.
    """

    return (
        result.get("status") == "fail"
        and isinstance(result.get("failure_code"), str)
        and isinstance(result.get("error"), str)
        and not isinstance(result.get("observation"), Mapping)
        and not isinstance(result.get("assertions"), Mapping)
    )
