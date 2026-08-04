from __future__ import annotations

import json
from pathlib import Path

from zerg.services.agent_heartbeat_health import _MACHINE_ACTION_IDS_BY_REASON
from zerg.services.local_health.classifier import _ACTION_IDS_BY_REASON


def _canonical_action_ids() -> dict[str, str]:
    path = Path(__file__).resolve().parents[2] / "schemas" / "health_action_ids.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["reason_to_action"]


def test_python_health_producers_use_the_canonical_action_contract() -> None:
    canonical = _canonical_action_ids()
    mappings = (_ACTION_IDS_BY_REASON, _MACHINE_ACTION_IDS_BY_REASON)
    for mapping in mappings:
        assert all(reason in canonical and canonical[reason] == action for reason, action in mapping.items())

    emitted_reasons = set().union(*(mapping.keys() for mapping in mappings))
    assert set(canonical) == emitted_reasons

    shared_reasons = {
        "connect_errors",
        "consecutive_failures",
        "parse_errors",
        "payload_rejected",
        "payload_too_large",
        "rate_limited",
        "reported_offline",
        "retryable_client_errors",
        "server_errors",
        "spool_dead",
        "spool_dead_letters",
    }
    for reason in shared_reasons:
        assert _ACTION_IDS_BY_REASON[reason] == canonical[reason]
        assert _MACHINE_ACTION_IDS_BY_REASON[reason] == canonical[reason]
