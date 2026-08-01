"""Provider-native interaction semantics used by ingest and qualification.

Providers are allowed to persist local controls as ``role=user`` records. The
raw record remains durable evidence, but Longhouse must not treat every such
record as a conversational prompt. This module is deliberately mechanical:
provider-specific markers establish facts, while an unknown slash command stays
an ordinary user message until a provider contract proves otherwise.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

INTERACTION_DURABLE_USER_MESSAGE = "durable_user_message"
INTERACTION_LOCAL_CONTROL = "local_control"
INTERACTION_LOCAL_CONTROL_OUTPUT = "local_control_output"
INTERACTION_PROVIDER_SYSTEM = "provider_system"
INTERACTION_CONVERSATION_BOUNDARY = "conversation_boundary"
INTERACTION_UNKNOWN_USER_INPUT = "unknown_user_input"

_TITLE_ELIGIBLE_KINDS = frozenset({INTERACTION_DURABLE_USER_MESSAGE, INTERACTION_UNKNOWN_USER_INPUT})


def _raw_mapping(raw_json: Any) -> Mapping[str, Any] | None:
    if isinstance(raw_json, Mapping):
        return raw_json
    if not isinstance(raw_json, str) or not raw_json.strip():
        return None
    try:
        value = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _raw_content(raw: Mapping[str, Any] | None) -> str:
    if raw is None:
        return ""
    message = raw.get("message")
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [str(item.get("text") or "") for item in content if isinstance(item, Mapping)]
            return "\n".join(part for part in parts if part)
    for key in ("content", "text", "prompt"):
        value = raw.get(key)
        if isinstance(value, str):
            return value
    return ""


def classify_provider_interaction(
    provider: str | None,
    *,
    role: str | None,
    content_text: str | None,
    raw_json: Any = None,
    source_surface: str | None = None,
) -> dict[str, Any]:
    """Return mechanical semantic facts for one normalized provider event.

    The classifier only makes a positive control classification from exact
    provider evidence. In particular, a text value beginning with ``/`` is not
    enough: custom slash commands may be sent to the model as ordinary prompts.
    """

    normalized_provider = str(provider or "").strip().lower()
    normalized_role = str(role or "").strip().lower()
    raw = _raw_mapping(raw_json)
    raw_text = _raw_content(raw)
    text = str(content_text or "")
    combined = "\n".join(value for value in (text, raw_text) if value)

    kind = INTERACTION_DURABLE_USER_MESSAGE if normalized_role == "user" else INTERACTION_PROVIDER_SYSTEM
    changes_provider_state: bool | None = None
    starts_model_turn: bool | None = None

    if normalized_provider == "claude" and normalized_role == "user":
        if "<local-command-stdout>" in combined:
            kind = INTERACTION_LOCAL_CONTROL_OUTPUT
            changes_provider_state = False
            starts_model_turn = False
        elif "<command-name>/clear</command-name>" in combined:
            kind = INTERACTION_CONVERSATION_BOUNDARY
            changes_provider_state = True
            starts_model_turn = False
        elif any(marker in combined for marker in ("<local-command-caveat>", "<command-name>", "<command-message>", "<command-args>")):
            kind = INTERACTION_LOCAL_CONTROL
            changes_provider_state = True
            starts_model_turn = False

    # A provider parser may attach an exact semantic marker to its normalized
    # event. Accept only the small vocabulary owned by this module; arbitrary
    # raw provider fields must not silently become Longhouse semantics.
    explicit_kind = raw.get("longhouse_interaction_kind") if raw is not None else None
    if explicit_kind in {
        INTERACTION_DURABLE_USER_MESSAGE,
        INTERACTION_LOCAL_CONTROL,
        INTERACTION_LOCAL_CONTROL_OUTPUT,
        INTERACTION_PROVIDER_SYSTEM,
        INTERACTION_CONVERSATION_BOUNDARY,
        INTERACTION_UNKNOWN_USER_INPUT,
    }:
        kind = str(explicit_kind)
        if kind in {INTERACTION_LOCAL_CONTROL, INTERACTION_CONVERSATION_BOUNDARY}:
            changes_provider_state = True
            starts_model_turn = False
        elif kind == INTERACTION_LOCAL_CONTROL_OUTPUT:
            changes_provider_state = False
            starts_model_turn = False

    explicit_state_change = raw.get("longhouse_changes_provider_state") if raw is not None else None
    if isinstance(explicit_state_change, bool):
        changes_provider_state = explicit_state_change

    title_eligible = normalized_role == "user" and kind in _TITLE_ELIGIBLE_KINDS
    counts_as_user_message = title_eligible
    return {
        "interaction_kind": kind,
        "counts_as_user_message": counts_as_user_message,
        "title_eligible": title_eligible,
        "starts_model_turn": starts_model_turn,
        "changes_provider_state": changes_provider_state,
        "source_surface": source_surface or "unknown",
    }


def title_eligible_provider_event(
    provider: str | None,
    *,
    role: str | None,
    content_text: str | None,
    raw_json: Any = None,
) -> bool:
    """Whether an event can be the first conversational title candidate."""

    return bool(
        classify_provider_interaction(
            provider,
            role=role,
            content_text=content_text,
            raw_json=raw_json,
        )["title_eligible"]
    )


def interaction_contract_snapshot(provider: str) -> tuple[dict[str, Any], ...]:
    """Expose the provider's declared probe rows in JSON-safe form."""

    from zerg.services.managed_provider_contracts import contract_for_provider

    contract = contract_for_provider(provider)
    if contract is None:
        return ()
    return tuple(
        {
            "probe_id": probe.probe_id,
            "surface": probe.surface,
            "input_sequence": list(probe.input_sequence),
            "acknowledgement_oracle": probe.acknowledgement_oracle,
            "native_sources": list(probe.native_sources),
            "raw_markers": list(probe.raw_markers),
            "raw_output_markers": list(probe.raw_output_markers),
            "expected_interaction_kind": probe.expected_interaction_kind,
            "expected_title_eligibility": probe.expected_title_eligibility,
            "expected_model_turn": probe.expected_model_turn,
            "changes_provider_state": probe.changes_provider_state,
            "source_surface": probe.source_surface,
            "state_mutation_scope": probe.state_mutation_scope,
            "evidence_class": probe.evidence_class,
            "disposition": probe.disposition,
            "canary": probe.canary,
        }
        for probe in contract.interaction_probes
    )


__all__ = [
    "INTERACTION_CONVERSATION_BOUNDARY",
    "INTERACTION_DURABLE_USER_MESSAGE",
    "INTERACTION_LOCAL_CONTROL",
    "INTERACTION_LOCAL_CONTROL_OUTPUT",
    "INTERACTION_PROVIDER_SYSTEM",
    "INTERACTION_UNKNOWN_USER_INPUT",
    "classify_provider_interaction",
    "interaction_contract_snapshot",
    "title_eligible_provider_event",
]
