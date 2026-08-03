"""Provider-native interaction semantics used by ingest and qualification.

Providers are allowed to persist local controls as ``role=user`` records. The
raw record remains durable evidence, but Longhouse must not treat every such
record as a conversational prompt. This module is deliberately mechanical:
provider-specific markers establish facts, while an unknown slash command stays
an ordinary user message until a provider contract proves otherwise.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from collections.abc import MutableMapping
from typing import Any

from zerg.services.raw_json_compression import decode_raw_json

INTERACTION_DURABLE_USER_MESSAGE = "durable_user_message"
INTERACTION_LOCAL_CONTROL = "local_control"
INTERACTION_LOCAL_CONTROL_OUTPUT = "local_control_output"
INTERACTION_PROVIDER_SYSTEM = "provider_system"
INTERACTION_CONVERSATION_BOUNDARY = "conversation_boundary"
INTERACTION_UNKNOWN_USER_INPUT = "unknown_user_input"

_TITLE_ELIGIBLE_KINDS = frozenset({INTERACTION_DURABLE_USER_MESSAGE, INTERACTION_UNKNOWN_USER_INPUT})
_INTERACTION_CONTEXT_KEY_MAX_BYTES = 255
_INTERACTION_CONTEXT_KEYS_PREFIX = "keys:"
VALID_INTERACTION_KINDS = frozenset(
    {
        INTERACTION_DURABLE_USER_MESSAGE,
        INTERACTION_LOCAL_CONTROL,
        INTERACTION_LOCAL_CONTROL_OUTPUT,
        INTERACTION_PROVIDER_SYSTEM,
        INTERACTION_CONVERSATION_BOUNDARY,
        INTERACTION_UNKNOWN_USER_INPUT,
    }
)


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


def _raw_identifier(raw: Mapping[str, Any] | None, field: str) -> str | None:
    if raw is None:
        return None
    value = raw.get(field)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _bounded_native_context_key(
    value: str,
    *,
    prefix: str = "",
    max_bytes: int = _INTERACTION_CONTEXT_KEY_MAX_BYTES,
) -> str:
    candidate = f"{prefix}{value}"
    if len(candidate.encode("utf-8")) <= max_bytes:
        return candidate
    return "sha256:" + hashlib.sha256(candidate.encode("utf-8")).hexdigest()


def _claude_prompt_context_candidates(prompt_id: str) -> tuple[str, ...]:
    """Return canonical and legacy bounded forms for a Claude prompt id.

    Composite prompt/UUID keys reserve space for the UUID component and
    therefore hash long prompt ids at 96 bytes. Older rows persisted the
    prompt-only form with the full 255-byte bound. Matching both forms keeps
    already-persisted evidence usable while making split-batch classification
    use the same representation as the newly persisted key.
    """

    return tuple(
        dict.fromkeys(
            (
                _bounded_native_context_key(prompt_id, max_bytes=96),
                _bounded_native_context_key(prompt_id, max_bytes=_INTERACTION_CONTEXT_KEY_MAX_BYTES),
            )
        )
    )


def _interaction_context_keys(provider: str, raw: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Return the provider-native sequence key when one is present.

    Claude's local-command caveat and its sibling command/output rows normally
    share a ``promptId``. Older and some current Claude transcripts omit that
    field, but retain the native ``uuid``/``parentUuid`` chain. Persisting the
    event UUID as a fallback lets a later ingest batch continue the same
    classification even when the raw bytes are already archive-primary.
    """

    if str(provider or "").strip().lower() != "claude" or raw is None:
        return ()
    keys: list[str] = []
    event_uuid = _raw_identifier(raw, "uuid")
    parent_uuid = _raw_identifier(raw, "parentUuid")
    prompt_id = _raw_identifier(raw, "promptId")
    if prompt_id is not None:
        # Preserve the readable legacy key for ordinary-sized prompt IDs.
        # Composite keys reserve room for the UUID fallback as well.
        keys.append(
            _bounded_native_context_key(
                prompt_id,
                max_bytes=96 if event_uuid is not None or parent_uuid is not None else _INTERACTION_CONTEXT_KEY_MAX_BYTES,
            )
        )
    if event_uuid is not None:
        keys.append(_bounded_native_context_key(event_uuid, prefix="uuid:"))
    elif parent_uuid is not None:
        keys.append(_bounded_native_context_key(parent_uuid, prefix="uuid:"))
    return tuple(dict.fromkeys(keys))


def _interaction_context_key(provider: str, raw: Mapping[str, Any] | None) -> str | None:
    keys = _interaction_context_keys(provider, raw)
    if not keys:
        return None
    if len(keys) == 1:
        return keys[0]
    encoded = json.dumps(keys, ensure_ascii=False, separators=(",", ":"))
    composite = f"{_INTERACTION_CONTEXT_KEYS_PREFIX}{encoded}"
    if len(composite.encode("utf-8")) > _INTERACTION_CONTEXT_KEY_MAX_BYTES:
        # Each component is bounded before it reaches this branch. Keep the
        # database contract explicit so a future component cannot silently
        # create an overlong key or truncate native evidence.
        raise ValueError("provider interaction context composite exceeds 255 UTF-8 bytes")
    return composite


def interaction_context_key_parts(value: str | None) -> tuple[str, ...]:
    """Decode one persisted sequence key into all native keys it carries."""

    cleaned = str(value or "").strip()
    if not cleaned:
        return ()
    if not cleaned.startswith(_INTERACTION_CONTEXT_KEYS_PREFIX):
        return (cleaned,)
    try:
        decoded = json.loads(cleaned.removeprefix(_INTERACTION_CONTEXT_KEYS_PREFIX))
    except (TypeError, json.JSONDecodeError):
        return (cleaned,)
    if not isinstance(decoded, list):
        return (cleaned,)
    parts = tuple(dict.fromkeys(part for part in decoded if isinstance(part, str) and part))
    return parts or (cleaned,)


_INTERACTION_KINDS = VALID_INTERACTION_KINDS


def _claude_native_meta_record(raw: Mapping[str, Any] | None) -> bool:
    """Whether a Claude row has the provider's local-command record shape.

    The markup is user-visible text and can be quoted in a real prompt. Claude
    marks its own local-command records with ``isMeta``; that structural fact,
    or a parser-owned semantic field, is the boundary evidence. Text alone is
    intentionally insufficient.
    """

    if raw is None or raw.get("isMeta") is not True:
        return False
    if raw.get("type") not in {None, "user"}:
        return False
    message = raw.get("message")
    return not isinstance(message, Mapping) or message.get("role") in {None, "user"}


_CLAUDE_COMMAND_RECORD_RE = re.compile(
    r"^\s*<command-name>.*?</command-name>"
    r"(?:\s*<command-message>.*?</command-message>)?"
    r"(?:\s*<command-args>.*?</command-args>)?\s*$",
    re.DOTALL,
)
_CLAUDE_COMMAND_OUTPUT_RE = re.compile(r"^\s*<local-command-stdout>.*?</local-command-stdout>\s*$", re.DOTALL)


def _claude_native_command_record(
    raw: Mapping[str, Any] | None,
    combined: str,
    sequence_context: MutableMapping[str, Any] | None,
) -> bool:
    """Recognize Claude's exact command envelope across its JSONL rows.

    Claude 2.1.219 marks the caveat row with ``isMeta`` but leaves the sibling
    command and stdout rows as ordinary-looking ``type=user`` records. The
    exact tag envelope is the remaining provider-native evidence. A sentence
    that merely quotes one of the tags does not match these full-record shapes.
    """

    if _claude_native_meta_record(raw):
        return True
    if raw is None or raw.get("type") not in {None, "user"}:
        return False
    message = raw.get("message")
    if isinstance(message, Mapping) and message.get("role") not in {None, "user"}:
        return False
    prompt_id = _raw_identifier(raw, "promptId")
    prompt_ids = sequence_context.get("claude_local_command_prompt_ids", set()) if sequence_context is not None else set()
    prompt_match = prompt_id is not None and any(key in prompt_ids for key in _claude_prompt_context_candidates(prompt_id))
    parent_uuid = _raw_identifier(raw, "parentUuid")
    local_uuids = sequence_context.get("claude_local_command_uuids", set()) if sequence_context is not None else set()
    uuid_match = parent_uuid is not None and parent_uuid in local_uuids
    sequence_match = prompt_match or uuid_match
    if not sequence_match:
        return False
    return bool(_CLAUDE_COMMAND_RECORD_RE.fullmatch(combined) or _CLAUDE_COMMAND_OUTPUT_RE.fullmatch(combined))


def claude_sequence_dependent_control_candidate(
    *,
    content_text: str | None,
    raw_json: Any = None,
) -> bool:
    """Whether a Claude row needs adjacent sequence evidence to classify.

    Claude 2.1.219 emits command and stdout rows with exact local-command
    markup and a ``promptId``, but without the ``isMeta`` marker carried by the
    preceding caveat.  The row is safe to defer until the preceding raw
    sequence is available; without the caveat it remains an ordinary prompt.
    """

    raw = _raw_mapping(raw_json)
    if raw is None or _claude_native_meta_record(raw):
        return False
    if raw.get("type") not in {None, "user"}:
        return False
    message = raw.get("message")
    if isinstance(message, Mapping) and message.get("role") not in {None, "user"}:
        return False
    prompt_id = _raw_identifier(raw, "promptId")
    if prompt_id is None and _raw_identifier(raw, "uuid") is None and _raw_identifier(raw, "parentUuid") is None:
        return False
    raw_text = _raw_content(raw)
    values = [value for value in (str(content_text or ""), raw_text) if value]
    return any(_CLAUDE_COMMAND_RECORD_RE.fullmatch(value) or _CLAUDE_COMMAND_OUTPUT_RE.fullmatch(value) for value in values)


def _advance_claude_sequence_context(
    raw: Mapping[str, Any] | None,
    raw_text: str,
    sequence_context: MutableMapping[str, Any] | None,
) -> None:
    if sequence_context is None or not _claude_native_meta_record(raw) or "<local-command-caveat>" not in raw_text:
        return
    prompt_id = _raw_identifier(raw, "promptId")
    if prompt_id is not None:
        prompt_ids = sequence_context.setdefault("claude_local_command_prompt_ids", set())
        if isinstance(prompt_ids, set):
            prompt_ids.update(_claude_prompt_context_candidates(prompt_id))
    event_uuid = _raw_identifier(raw, "uuid")
    if event_uuid is not None:
        local_uuids = sequence_context.setdefault("claude_local_command_uuids", set())
        if isinstance(local_uuids, set):
            local_uuids.add(event_uuid)


def _remember_claude_local_context(
    raw: Mapping[str, Any] | None,
    interaction_kind: str,
    sequence_context: MutableMapping[str, Any] | None,
) -> None:
    if (
        sequence_context is None
        or raw is None
        or interaction_kind
        not in {
            INTERACTION_LOCAL_CONTROL,
            INTERACTION_LOCAL_CONTROL_OUTPUT,
            INTERACTION_CONVERSATION_BOUNDARY,
        }
    ):
        return
    event_uuid = _raw_identifier(raw, "uuid")
    if event_uuid is None:
        return
    local_uuids = sequence_context.setdefault("claude_local_command_uuids", set())
    if isinstance(local_uuids, set):
        local_uuids.add(event_uuid)


def seed_provider_interaction_sequence_context(
    provider: str | None,
    raw_events: list[Any] | tuple[Any, ...],
    sequence_context: MutableMapping[str, Any],
) -> None:
    """Seed complete-window native caveat evidence before replay.

    Normal ingest observes rows in provider order and must not use future
    evidence. Storage-v2 receives an immutable raw window, so it can safely
    discover a caveat anywhere in that complete window before recovering a
    parser-owned render projection. This also handles a split envelope whose
    command row sorts before its caveat row.
    """

    if str(provider or "").strip().lower() != "claude":
        return
    for raw_event in raw_events:
        raw = _raw_mapping(raw_event)
        if raw is None or not _claude_native_meta_record(raw):
            continue
        content = _raw_content(raw)
        if "<local-command-caveat>" not in content:
            continue
        message = raw.get("message")
        role = message.get("role") if isinstance(message, Mapping) else raw.get("type")
        classify_provider_interaction(
            provider,
            role=str(role or ""),
            content_text=content,
            raw_json=raw,
            sequence_context=sequence_context,
        )

    # A complete immutable raw window may be physically out of order. Close
    # the UUID lineage before the normal ordered replay so stdout can still be
    # connected when it precedes its command, and the command precedes its
    # caveat. Normal ingest does not call this helper and therefore remains
    # causal rather than using future evidence.
    classified_indexes: set[int] = set()
    changed = True
    while changed:
        changed = False
        for index, raw_event in enumerate(raw_events):
            if index in classified_indexes:
                continue
            raw = _raw_mapping(raw_event)
            if raw is None:
                continue
            content = _raw_content(raw)
            if not (_CLAUDE_COMMAND_RECORD_RE.fullmatch(content) or _CLAUDE_COMMAND_OUTPUT_RE.fullmatch(content)):
                continue
            if not _claude_native_command_record(raw, content, sequence_context):
                continue
            message = raw.get("message")
            role = message.get("role") if isinstance(message, Mapping) else raw.get("type")
            classify_provider_interaction(
                provider,
                role=str(role or ""),
                content_text=content,
                raw_json=raw,
                sequence_context=sequence_context,
            )
            classified_indexes.add(index)
            changed = True


def seed_persisted_provider_interaction_context(
    provider: str | None,
    events: list[Any] | tuple[Any, ...],
    sequence_context: MutableMapping[str, Any],
) -> None:
    """Seed parser-owned context for rows whose raw companion is absent.

    This is compatibility evidence, not a replacement for native raw replay.
    It lets a complete legacy window use persisted sequence keys after raw
    bytes have moved to archive-primary. Only keys from rows already persisted
    as controls establish sequence evidence; an ordinary command-shaped row
    must not be able to establish its own control context.
    """

    if str(provider or "").strip().lower() != "claude":
        return
    control_kinds = {
        INTERACTION_LOCAL_CONTROL,
        INTERACTION_LOCAL_CONTROL_OUTPUT,
        INTERACTION_CONVERSATION_BOUNDARY,
    }

    def field(event: Any, name: str, default: Any = None) -> Any:
        if isinstance(event, Mapping):
            return event.get(name, default)
        return getattr(event, name, default)

    def raw_value(event: Any) -> Any:
        if isinstance(event, Mapping):
            raw_json = event.get("raw_json")
            if raw_json is None and event.get("raw_json_codec") == 1:
                blob = event.get("raw_json_z")
                if blob is not None:
                    from zerg.services.raw_json_compression import decompress_raw_json

                    return decompress_raw_json(blob)
            return raw_json
        return decode_raw_json(event)

    def set_field(event: Any, name: str, value: Any) -> None:
        if isinstance(event, MutableMapping):
            event[name] = value
        else:
            setattr(event, name, value)

    # First pass: only an already-classified control may establish the
    # prompt/UUID context used by a later semantic repair.
    for event in events:
        if _raw_mapping(raw_value(event)) is not None:
            continue
        if field(event, "interaction_kind") not in control_kinds:
            continue
        for context_key in interaction_context_key_parts(str(field(event, "interaction_context_key") or "").strip()):
            if context_key.startswith("uuid:"):
                sequence_context.setdefault("claude_local_command_uuids", set()).add(context_key.removeprefix("uuid:"))
            else:
                sequence_context.setdefault("claude_local_command_prompt_ids", set()).add(context_key)

    # Second pass: raw bytes may have moved to archive-primary, but the
    # normalized row still carries its exact content, interaction key, and
    # Claude UUID lineage. Reclassify only when that persisted lineage matches
    # a control context. Mutating these sidecars is intentional; raw history is
    # never rewritten.
    for event in events:
        raw_json = raw_value(event)
        if _raw_mapping(raw_json) is not None:
            continue
        if str(field(event, "role") or "user").strip().lower() != "user":
            continue
        content_text = field(event, "content_text")
        if not isinstance(content_text, str) or not content_text:
            continue
        surrogate: dict[str, Any] = {
            "type": "user",
            "message": {"role": "user", "content": content_text},
        }
        context_parts = interaction_context_key_parts(str(field(event, "interaction_context_key") or "").strip())
        prompt_id = next((part for part in context_parts if not part.startswith("uuid:")), None)
        if prompt_id is not None:
            surrogate["promptId"] = prompt_id
        event_uuid = field(event, "event_uuid")
        parent_event_uuid = field(event, "parent_event_uuid")
        if isinstance(event_uuid, str) and event_uuid:
            surrogate["uuid"] = event_uuid
        if isinstance(parent_event_uuid, str) and parent_event_uuid:
            surrogate["parentUuid"] = parent_event_uuid
        classification = classify_provider_interaction(
            provider,
            role="user",
            content_text=content_text,
            raw_json=surrogate,
            sequence_context=sequence_context,
        )
        if classification["interaction_kind"] not in control_kinds:
            continue
        set_field(event, "interaction_kind", classification["interaction_kind"])
        set_field(event, "title_eligible", 0)


def classify_provider_interaction(
    provider: str | None,
    *,
    role: str | None,
    content_text: str | None,
    raw_json: Any = None,
    source_surface: str | None = None,
    interaction_kind: str | None = None,
    changes_provider_state: bool | None = None,
    sequence_context: MutableMapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return mechanical semantic facts for one normalized provider event.

    The classifier only makes a positive control classification from exact
    provider evidence. In particular, a text value beginning with ``/`` is not
    enough: custom slash commands may be sent to the model as ordinary prompts.
    """

    normalized_provider = str(provider or "").strip().lower()
    normalized_role = str(role or "").strip().lower()
    parser_changes_provider_state = changes_provider_state
    raw = _raw_mapping(raw_json)
    raw_text = _raw_content(raw)
    text = str(content_text or "")
    combined_values = [value for value in (text, raw_text) if value]
    combined = combined_values[0] if len(set(combined_values)) == 1 else "\n".join(combined_values)
    _advance_claude_sequence_context(raw, raw_text, sequence_context)

    kind = INTERACTION_DURABLE_USER_MESSAGE if normalized_role == "user" else INTERACTION_PROVIDER_SYSTEM
    changes_provider_state: bool | None = None
    starts_model_turn: bool | None = None

    claude_native_command = any(
        _claude_native_command_record(raw, value, sequence_context) for value in (text, raw_text, combined) if value
    )
    if normalized_provider == "claude" and normalized_role == "user" and claude_native_command:
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
    # ``interaction_kind`` is parser-owned normalized data. Never accept a
    # Longhouse semantic override from provider raw JSON: raw provider text is
    # evidence to classify, not an authority that can demote itself.
    explicit_kind = interaction_kind
    if explicit_kind in _INTERACTION_KINDS:
        kind = str(explicit_kind)
        if kind in {INTERACTION_LOCAL_CONTROL, INTERACTION_CONVERSATION_BOUNDARY}:
            changes_provider_state = True
            starts_model_turn = False
        elif kind == INTERACTION_LOCAL_CONTROL_OUTPUT:
            changes_provider_state = False
            starts_model_turn = False
        elif kind == INTERACTION_PROVIDER_SYSTEM:
            changes_provider_state = False
            starts_model_turn = False

    if parser_changes_provider_state is not None:
        changes_provider_state = bool(parser_changes_provider_state)

    _remember_claude_local_context(raw if normalized_provider == "claude" else None, kind, sequence_context)

    title_eligible = normalized_role == "user" and kind in _TITLE_ELIGIBLE_KINDS
    counts_as_user_message = title_eligible
    return {
        "interaction_kind": kind,
        "interaction_context_key": _interaction_context_key(normalized_provider, raw),
        "counts_as_user_message": counts_as_user_message,
        "title_eligible": title_eligible,
        "starts_model_turn": starts_model_turn,
        "changes_provider_state": changes_provider_state,
        "source_surface": source_surface or "unknown",
    }


def semantic_projection_facts(
    provider: str | None,
    *,
    role: str | None,
    content_text: str | None,
    raw_json: Any = None,
    interaction_kind: str | None = None,
    title_eligible: bool | int | None = None,
    sequence_context: MutableMapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify one event while preserving parser-owned persisted facts.

    Legacy rows may have neither semantic column populated. Every semantic
    projection must still replay those rows in provider order so Claude's
    caveat can establish context for a later command/output sibling.

    Claude's native JSON envelope is authoritative whenever it is available.
    Persisted semantic columns remain the compatibility authority for rows
    whose raw envelope is absent, but they cannot override a later raw replay.
    This lets a late caveat repair an already-ingested command consistently
    across every semantic consumer.
    """

    raw_is_authoritative = str(provider or "").strip().lower() == "claude" and _raw_mapping(raw_json) is not None

    classification = classify_provider_interaction(
        provider,
        role=role,
        content_text=content_text,
        raw_json=raw_json,
        interaction_kind=None if raw_is_authoritative else interaction_kind,
        sequence_context=sequence_context,
    )
    normalized_role = str(role or "").strip().lower()
    effective_kind = (
        interaction_kind if not raw_is_authoritative and interaction_kind in VALID_INTERACTION_KINDS else classification["interaction_kind"]
    )
    effective_title_eligible = (
        bool(title_eligible)
        if not raw_is_authoritative and normalized_role == "user" and title_eligible is not None
        else bool(classification["title_eligible"])
    )
    return {
        **classification,
        "interaction_kind": effective_kind,
        "title_eligible": effective_title_eligible,
        "counts_as_user_message": normalized_role == "user" and effective_title_eligible,
    }


def title_eligible_provider_event(
    provider: str | None,
    *,
    role: str | None,
    content_text: str | None,
    raw_json: Any = None,
    interaction_kind: str | None = None,
    title_eligible: bool | int | None = None,
    sequence_context: MutableMapping[str, Any] | None = None,
) -> bool:
    """Whether an event can be the first conversational title candidate."""

    raw_is_authoritative = str(provider or "").strip().lower() == "claude" and _raw_mapping(raw_json) is not None
    if not raw_is_authoritative and str(role or "").strip().lower() == "user" and title_eligible is not None:
        return bool(title_eligible)

    return bool(
        classify_provider_interaction(
            provider,
            role=role,
            content_text=content_text,
            raw_json=raw_json,
            interaction_kind=None if raw_is_authoritative else interaction_kind,
            sequence_context=sequence_context,
        )["title_eligible"]
    )


def semantic_event_included(
    provider: str | None,
    *,
    role: str | None,
    content_text: str | None,
    raw_json: Any = None,
    interaction_kind: str | None = None,
    title_eligible: bool | int | None = None,
    sequence_context: MutableMapping[str, Any] | None = None,
) -> bool:
    """Whether a normalized event belongs in semantic projections.

    Non-user records remain part of semantic context. Provider-local records
    that are normalized as ``role=user`` are the only rows this boundary can
    exclude, and they are excluded only when the provider contract supplies
    positive evidence that they are local control rather than a prompt.
    """

    if str(role or "").strip().lower() != "user":
        return True
    return title_eligible_provider_event(
        provider,
        role=role,
        content_text=content_text,
        raw_json=raw_json,
        interaction_kind=interaction_kind,
        title_eligible=title_eligible,
        sequence_context=sequence_context,
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
    "interaction_context_key_parts",
    "interaction_contract_snapshot",
    "semantic_projection_facts",
    "seed_persisted_provider_interaction_context",
    "seed_provider_interaction_sequence_context",
    "semantic_event_included",
    "title_eligible_provider_event",
]
