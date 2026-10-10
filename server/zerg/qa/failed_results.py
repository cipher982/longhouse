"""How a failed producer result is shaped, with no Longhouse imports.

Producers import this, never ``factory_registration``: the provider factory pins
every module a producer imports (its verifier closure), and this file must stay a
leaf so pinning it does not pull the server in.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def settle_failed_result(failure: dict[str, Any], *, observation: Any, assertions: Mapping[str, Any]) -> dict[str, Any]:
    """Finish a failed producer result: the rule every producer follows.

    A failure keeps a verdict only when it reached one that fails: a non-empty
    observation and a boolean assertion map with at least one False. Anything else
    reached no failing verdict and is a typed harness failure
    (``factory_registration.typed_harness_failure``): a crash before any
    observation (no coordination authority, a launch or bridge that never came up),
    or a failure from outside the assertions (cleanup, a write) after every
    assertion held. The factory reports a typed harness failure by its cause; a
    synthesized False would be filed as a product finding, and an all-true map on a
    failed result contradicts itself. What the run did observe stays as
    ``partial_observation``, evidence rather than a verdict.
    """

    reached_failing_verdict = (
        isinstance(observation, Mapping)
        and bool(observation)
        and bool(assertions)
        and all(type(value) is bool for value in assertions.values())
        and not all(assertions.values())
    )
    if reached_failing_verdict:
        failure["observation"] = observation
        failure["assertions"] = dict(assertions)
    else:
        failure.pop("observation", None)
        failure.pop("assertions", None)
        if isinstance(observation, Mapping) and observation:
            failure["partial_observation"] = observation
    return failure
