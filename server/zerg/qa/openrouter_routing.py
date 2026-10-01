"""OpenRouter provider routing for the factory's OpenRouter-backed qualification lanes.

The OpenCode and Pi lanes call one model through OpenRouter, which by default
spreads every request over all of that model's hosts. A host that accepts a
stream and never finishes it fails the cell: on 2026-09-30 one request to
``OpenInference`` sat for 177.7 s with no finish reason and the five OpenCode
Helm cells went red (spec ``provider-factory-findings-loop.md``, "Advance #10
follow-up"). Pin the request to hosts that finish, and keep the rest as
fallbacks so one host's outage is not a red cell.

Measured from OpenRouter analytics for the three factory keys (OpenCode, Pi,
OMP), model ``deepseek/deepseek-v4.1-flash``, 2026-09-24 to 2026-10-01
(16,710 requests). Generation time is the request's whole stream, so a stall
shows as a tail, and a request the harness gave up on shows as no finish reason.

``order`` is the four highest-volume hosts whose p95 generation time stayed at
or under 3.6 s on every lane, fastest median first (p95 in seconds per lane,
OpenCode / Pi / OMP; p50 0.2-0.3 s for Together, 1.0-1.9 s for the others):
``together`` 2.1 / 0.8 / 1.0 (5,842 requests, 0.6% no finish reason), ``novita``
1.8 / 1.5 / 1.8 (1,318), ``atlas-cloud`` 2.3 / 1.6 / 1.9 (1,236), ``streamlake``
3.6 / 2.0 / 3.0 (1,333).

``ignore`` is every host whose p95 exceeded 10 s on some lane with at least 15
requests: ``open-inference`` 214.0 / 45.3 / 37.3 (122 requests, 8 without a
finish reason, 6.6%), ``wafer`` 12.8 (OpenCode, p99 101 s), ``morph`` 16.4 /
8.3 / 13.0, ``inference-net`` 12.9 / 7.0 / 7.8, ``sail-research`` 21.5 (OMP,
15 requests). Everything else stays reachable as a fallback.

Slugs are OpenRouter's (``GET /api/v1/models/<id>/endpoints``, ``tag``); a base
slug matches every variant of that host, and an unknown slug is ignored.
Confirmed against OpenRouter on 2026-10-01 with one-token requests: this routing
is served by Together, and with ``together`` ignored the next in ``order``
(Novita) serves it.

The routing is data, not policy for production: it applies only to the disposable
profiles these producers build. It was measured for this one model. Hosts that do not
serve another model are skipped by OpenRouter (``order`` falls through, an unknown
``ignore`` entry matches nothing), so a model change is harmless but unmeasured:
re-measure the same way when the factory's model changes.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

OPENROUTER_QUALIFICATION_ROUTING: dict[str, Any] = {
    "order": ["together", "novita", "atlas-cloud", "streamlake"],
    "allow_fallbacks": True,
    "ignore": ["open-inference", "wafer", "morph", "inference-net", "sail-research"],
}

_PI_THINKING_LEVELS = frozenset({"off", "minimal", "low", "medium", "high", "xhigh"})


def openrouter_qualification_routing() -> dict[str, Any]:
    """A fresh copy, so a caller can never edit the shared constant."""

    return copy.deepcopy(OPENROUTER_QUALIFICATION_ROUTING)


def pi_openrouter_model_id(model: str) -> str:
    """The OpenRouter model id inside Pi's ``--model`` value.

    Pi takes ``<id>`` or ``<id>:<thinking level>``. OpenRouter ids may carry their
    own ``:variant`` suffix, so only a known thinking level is stripped.
    """

    base, separator, suffix = str(model).strip().rpartition(":")
    if separator and suffix in _PI_THINKING_LEVELS:
        return base
    return str(model).strip()


def prepare_pi_openrouter_routing(agent_dir: Path, model: str) -> dict[str, Any]:
    """Route Pi's OpenRouter requests for ``model`` through the qualification routing.

    Pi reads ``<PI_CODING_AGENT_DIR>/models.json``; ``modelOverrides`` changes one
    built-in model's ``compat.openRouterRouting``, which Pi sends as-is as the
    request's ``provider`` object. Verified against Pi 0.99.1 with a local capture
    server: the request body carries exactly this object. The file holds no
    credential (Pi reads the key from the environment).
    """

    model_id = pi_openrouter_model_id(model)
    if not model_id:
        raise RuntimeError("Pi OpenRouter routing requires an explicit model")
    agent_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = agent_dir / "models.json"
    document: dict[str, Any] = {}
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise RuntimeError("Pi models.json must be a JSON object")
        document = loaded
    providers = document.setdefault("providers", {})
    openrouter = providers.setdefault("openrouter", {})
    overrides = openrouter.setdefault("modelOverrides", {})
    entry = overrides.setdefault(model_id, {})
    entry.setdefault("compat", {})["openRouterRouting"] = openrouter_qualification_routing()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    return {
        "provider": "openrouter",
        "model_id": model_id,
        "config_path": str(path),
        "routing": openrouter_qualification_routing(),
    }


__all__ = [
    "OPENROUTER_QUALIFICATION_ROUTING",
    "openrouter_qualification_routing",
    "pi_openrouter_model_id",
    "prepare_pi_openrouter_routing",
]
