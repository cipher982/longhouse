"""Pure machine-evidence identity, authority, and hashing contract."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any

MAX_REDUCER_EVIDENCE_FACTS = 256
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_MIN_CANONICAL_INTEGER = -(2**63)
_MAX_CANONICAL_INTEGER = 2**64 - 1
_CANONICAL_TIMESTAMP_FIELDS = frozenset({"observed_at", "valid_until", "source_mtime", "hook_observed_at", "claimed_at", "response_at"})
_SUBJECT_PREFIX = {
    "process": "process:",
    "activity": "run:",
    "control": "connection:",
    "transcript": "thread:",
    "readiness": "readiness:",
}
_AUTHORITY_CLASS = {
    "process": "exact_process_identity",
    "activity": "provider_runtime",
    "control": "provider_control",
    "transcript": "source_cursor",
    "readiness": "operation_proof",
}
_GRANTED_OPERATIONS = frozenset({"send_input", "interrupt", "terminate", "tail_output", "resume"})


@dataclass(frozen=True, slots=True)
class ValidatedEvidenceIdentity:
    family: str
    fact_index: int
    subject_key: str
    source: str
    source_epoch: str
    source_seq: int | None
    dedupe_key: str
    evidence_hash: str
    value: dict[str, Any]


def canonical_value_json(value: dict[str, Any]) -> str:
    return json.dumps(_canonical_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_evidence_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_value_json(value).encode()).hexdigest()


def _canonical_value(value: Any, field: str | None = None) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical_value(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical_value(item, field) for item in value]
    if isinstance(value, float):
        raise ValueError("floating-point machine evidence is not canonical")
    if isinstance(value, int) and not isinstance(value, bool):
        if not _MIN_CANONICAL_INTEGER <= value <= _MAX_CANONICAL_INTEGER:
            raise ValueError("machine evidence integer is outside the serde_json range")
    if isinstance(value, str) and field in _CANONICAL_TIMESTAMP_FIELDS and _RFC3339_RE.fullmatch(value):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        except (ValueError, OverflowError):
            return value
    return value


def validate_machine_evidence_identities(evidence: object) -> list[ValidatedEvidenceIdentity]:
    """Validate every reducer identity against its typed fact and content."""

    if not isinstance(evidence, dict) or evidence.get("schema_version") not in {2, 3}:
        return []
    schema_version = evidence["schema_version"]
    identities = evidence.get("identities")
    if not isinstance(identities, list) or len(identities) > MAX_REDUCER_EVIDENCE_FACTS:
        raise ValueError(f"machine evidence identities must contain at most {MAX_REDUCER_EVIDENCE_FACTS} rows")
    families = {family: evidence.get(family) for family in _SUBJECT_PREFIX}
    if schema_version == 3:
        for family, rows in families.items():
            if not isinstance(rows, list):
                continue
            for value in rows:
                if not isinstance(value, dict):
                    raise ValueError("machine evidence fact must be an object")
                _validate_v3_authority(family, value)
    validated: list[ValidatedEvidenceIdentity] = []
    seen: set[tuple[str, int]] = set()
    for identity in identities:
        if not isinstance(identity, dict):
            raise ValueError("machine evidence identity must be an object")
        family = identity.get("fact_family")
        fact_index = identity.get("fact_index")
        if family not in families or type(fact_index) is not int or fact_index < 0:
            raise ValueError("machine evidence identity has an invalid fact reference")
        key = (family, fact_index)
        if key in seen:
            raise ValueError("machine evidence identity is duplicated")
        seen.add(key)
        family_rows = families[family]
        if not isinstance(family_rows, list) or fact_index >= len(family_rows):
            raise ValueError("machine evidence identity references a missing fact")
        value = family_rows[fact_index]
        if not isinstance(value, dict):
            raise ValueError("machine evidence fact must be an object")
        source = identity.get("source")
        if not isinstance(source, str) or source != value.get("source"):
            raise ValueError("machine evidence identity source does not match its fact")
        subject_key = identity.get("subject_key")
        if not isinstance(subject_key, str) or not subject_key.startswith(_SUBJECT_PREFIX[family]):
            raise ValueError(f"machine evidence {family} subject is not reducer-grade")
        _validate_subject_key(family, subject_key, value)
        source_epoch = identity.get("source_epoch")
        if source_epoch is not None and (not isinstance(source_epoch, str) or len(source_epoch) > 255):
            raise ValueError("machine evidence source_epoch must be a bounded string or null")
        source_seq = identity.get("source_seq")
        if source_seq is not None and (type(source_seq) is not int or source_seq < 0):
            raise ValueError("machine evidence source_seq must be non-negative or null")
        if identity.get("sequenced") is not (source_seq is not None):
            raise ValueError("machine evidence sequence declaration is inconsistent")
        dedupe_key = identity.get("dedupe_key")
        evidence_hash = identity.get("evidence_hash")
        if not isinstance(dedupe_key, str) or not _SHA256_RE.fullmatch(dedupe_key):
            raise ValueError("machine evidence dedupe_key must be lowercase sha256")
        if not isinstance(evidence_hash, str) or not _SHA256_RE.fullmatch(evidence_hash):
            raise ValueError("machine evidence evidence_hash must be lowercase sha256")
        if canonical_evidence_hash(value) != evidence_hash:
            raise ValueError("machine evidence evidence_hash does not match canonical fact content")
        validated.append(
            ValidatedEvidenceIdentity(
                family=family,
                fact_index=fact_index,
                subject_key=subject_key,
                source=source,
                source_epoch=source_epoch or "",
                source_seq=source_seq,
                dedupe_key=dedupe_key,
                evidence_hash=evidence_hash,
                value=value,
            )
        )
    return validated


def _validate_v3_authority(family: str, value: dict[str, Any]) -> None:
    expected = _AUTHORITY_CLASS[family]
    if value.get("authority_class") != expected:
        raise ValueError(f"machine evidence {family} authority_class must be {expected}")
    if family != "control":
        return
    _required_component(value, "run_id")
    operations = value.get("granted_operations")
    if not isinstance(operations, list) or any(not isinstance(operation, str) for operation in operations):
        raise ValueError("machine evidence control granted_operations must be a string list")
    if operations != sorted(set(operations)) or any(operation not in _GRANTED_OPERATIONS for operation in operations):
        raise ValueError("machine evidence control granted_operations must be sorted, unique, and supported")


def _validate_subject_key(family: str, subject_key: str, value: dict[str, Any]) -> None:
    if family == "process":
        provider = _required_component(value, "provider")
        boot_id = _required_component(value, "boot_id", allow_colon=True)
        pid = value.get("pid")
        process_start = _required_component(value, "process_start_time", allow_colon=True)
        if type(pid) is not int or pid <= 0:
            raise ValueError("process reducer identity requires a positive pid")
        generation = _stable_component(f"{provider}:{pid}:{process_start}")
        parts = subject_key.split(":", 5)
        if (
            len(parts) != 6
            or parts[2] != provider
            or parts[3] != _stable_component(boot_id)
            or parts[4] != str(pid)
            or parts[5] != generation
            or not _SHA256_RE.fullmatch(parts[1])
        ):
            raise ValueError("process reducer identity does not match its fact")
        return
    if family == "activity":
        expected = f"run:{_required_component(value, 'run_id')}"
    elif family == "control":
        expected = f"connection:{_required_component(value, 'connection_id')}:{_required_component(value, 'lease_generation')}"
    elif family == "transcript":
        provider = _required_component(value, "provider")
        provider_session_id = _required_component(value, "provider_session_id", allow_colon=True)
        expected = f"thread:{provider}:{_stable_component(provider_session_id)}"
    else:
        session_id = _required_component(value, "session_id")
        operation = _required_component(value, "operation")
        expected = f"readiness:{session_id}:{operation}"
    if subject_key != expected:
        raise ValueError(f"machine evidence {family} identity does not match its fact")


def _required_component(value: dict[str, Any], field: str, *, allow_colon: bool = False) -> str:
    component = value.get(field)
    if not isinstance(component, str) or not component.strip():
        raise ValueError(f"machine evidence reducer identity requires {field}")
    if not allow_colon and ":" in component:
        raise ValueError(f"machine evidence reducer identity {field} cannot contain ':'")
    return component


def _stable_component(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
