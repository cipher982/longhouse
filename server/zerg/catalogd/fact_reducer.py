"""Bounded shadow reducer for independent machine-evidence fact families."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy import Connection
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy import tuple_
from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from zerg.catalogd.models import FactConflict
from zerg.catalogd.models import FactHead
from zerg.catalogd.models import FactReceipt
from zerg.catalogd.schema import catalog_meta
from zerg.machine_evidence import canonical_evidence_hash
from zerg.machine_evidence import canonical_value_json
from zerg.machine_evidence import validate_machine_evidence_identities

MAX_REDUCER_FACTS = 256
MAX_VALUE_JSON_BYTES = 4 * 1024
MAX_RAW_LOCATOR_BYTES = 1024
MAX_RECEIPTS_PER_CANDIDATE = 16
MAX_CONFLICTS_PER_CANDIDATE = 8
MAX_HEADS_PER_FAMILY = 2_048
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ReducerFact:
    family: str
    subject_key: str
    source: str
    source_epoch: str
    source_seq: int | None
    dedupe_key: str
    evidence_hash: str
    value: dict[str, Any]
    observed_at: datetime | None
    session_id: str | None = None
    valid_until: datetime | None = None
    raw_locator: str | None = None


@dataclass(frozen=True, slots=True)
class ReducerResult:
    commit_seq: int
    changed_heads: int
    duplicates: int
    stale: int
    conflicts: int


def reducer_facts_from_machine_evidence(evidence: object) -> list[ReducerFact]:
    """Validate and extract the bounded reducer-eligible subset of schema v3."""

    if not isinstance(evidence, dict) or evidence.get("schema_version") != 3:
        return []

    facts: list[ReducerFact] = []
    for identity in validate_machine_evidence_identities(evidence):
        value = identity.value
        observed_at = _parse_wire_datetime(value.get("observed_at"), "observed_at")
        valid_until = _parse_wire_datetime(value.get("valid_until"), "valid_until", nullable=True)
        raw_locator = value.get("raw_locator")
        facts.append(
            ReducerFact(
                family=identity.family,
                subject_key=identity.subject_key,
                source=identity.source,
                source_epoch=identity.source_epoch,
                source_seq=identity.source_seq,
                dedupe_key=identity.dedupe_key,
                evidence_hash=identity.evidence_hash,
                value=value,
                observed_at=observed_at,
                session_id=value.get("session_id") if isinstance(value.get("session_id"), str) else None,
                valid_until=valid_until,
                raw_locator=raw_locator if isinstance(raw_locator, str) else None,
            )
        )
    return facts


def reduce_fact_batch(
    connection: Connection,
    facts: list[ReducerFact],
    *,
    received_at: datetime,
    commit_seq_override: int | None = None,
) -> ReducerResult:
    """Reduce one validated batch inside the caller's catalog transaction."""

    if len(facts) > MAX_REDUCER_FACTS:
        raise ValueError(f"reducer batch exceeds {MAX_REDUCER_FACTS} facts")
    received_at = _aware(received_at, "received_at")
    prepared, batch_duplicates, batch_conflicts = _prepare_batch(facts)
    current_commit = _current_commit_seq(connection)
    if commit_seq_override is not None and commit_seq_override != current_commit:
        raise ValueError("reducer commit_seq_override must equal the current catalog commit")
    allocated_commit: int | None = None
    changed_heads = stale = conflicts = 0
    duplicates = batch_duplicates
    touched_candidates: dict[tuple[str, str, str, str], ReducerFact] = {}

    def commit_seq() -> int:
        nonlocal allocated_commit
        if allocated_commit is None:
            allocated_commit = commit_seq_override if commit_seq_override is not None else _advance_commit_seq(connection, received_at)
        return allocated_commit

    for fact, value_json in prepared:
        receipts = FactReceipt.__table__
        heads = FactHead.__table__
        receipt_candidate = _candidate_predicates(fact, receipts)
        head_candidate = _candidate_predicates(fact, heads)
        position_key = _position_key(fact)
        ordering_mode = _ordering_mode(fact)

        dedupe_receipt = connection.execute(
            select(receipts.c.evidence_hash).where(*receipt_candidate, receipts.c.dedupe_key == fact.dedupe_key)
        ).scalar_one_or_none()
        if dedupe_receipt is not None:
            if str(dedupe_receipt) == fact.evidence_hash:
                duplicates += 1
            else:
                conflicts += _record_conflict(
                    connection,
                    fact=fact,
                    existing_hash=str(dedupe_receipt),
                    position_key=position_key,
                    conflict_kind="dedupe_key_reuse",
                    at=received_at,
                    commit_seq=commit_seq,
                )
                touched_candidates[_candidate_key(fact)] = fact
            continue

        position_hash = connection.execute(
            select(receipts.c.evidence_hash).where(*receipt_candidate, receipts.c.position_key == position_key)
        ).scalar_one_or_none()
        if position_hash is not None:
            if str(position_hash) == fact.evidence_hash:
                duplicates += 1
            else:
                conflicts += _record_conflict(
                    connection,
                    fact=fact,
                    existing_hash=str(position_hash),
                    position_key=position_key,
                    conflict_kind="source_position_reuse",
                    at=received_at,
                    commit_seq=commit_seq,
                )
                touched_candidates[_candidate_key(fact)] = fact
            continue

        head = connection.execute(select(heads).where(*head_candidate)).mappings().first()
        if head is not None:
            if str(head["ordering_mode"]) != ordering_mode:
                conflicts += _record_conflict(
                    connection,
                    fact=fact,
                    existing_hash=str(head["evidence_hash"]),
                    position_key=position_key,
                    conflict_kind="ordering_mode_change",
                    at=received_at,
                    commit_seq=commit_seq,
                )
                touched_candidates[_candidate_key(fact)] = fact
                continue
            if ordering_mode == "sequenced":
                head_seq = int(head["source_seq"])
                assert fact.source_seq is not None
                if int(fact.source_seq) < int(head_seq):
                    stale += 1
                    continue
                if int(fact.source_seq) == int(head_seq):
                    if str(head["evidence_hash"]) == fact.evidence_hash:
                        duplicates += 1
                    else:
                        conflicts += _record_conflict(
                            connection,
                            fact=fact,
                            existing_hash=str(head["evidence_hash"]),
                            position_key=position_key,
                            conflict_kind="head_position_reuse",
                            at=received_at,
                            commit_seq=commit_seq,
                        )
                        touched_candidates[_candidate_key(fact)] = fact
                    continue
            else:
                head_observed = _sqlite_datetime(head["observed_at"])
                assert fact.observed_at is not None
                if head_observed is not None and fact.observed_at < head_observed:
                    stale += 1
                    continue
                if head_observed is not None and fact.observed_at == head_observed:
                    if str(head["evidence_hash"]) == fact.evidence_hash:
                        duplicates += 1
                    else:
                        conflicts += _record_conflict(
                            connection,
                            fact=fact,
                            existing_hash=str(head["evidence_hash"]),
                            position_key=position_key,
                            conflict_kind="head_position_reuse",
                            at=received_at,
                            commit_seq=commit_seq,
                        )
                        touched_candidates[_candidate_key(fact)] = fact
                    continue

        seq = commit_seq()
        values = {
            "session_id": fact.session_id,
            "ordering_mode": ordering_mode,
            "source_seq": fact.source_seq,
            "evidence_hash": fact.evidence_hash,
            "observed_at": fact.observed_at,
            "valid_until": fact.valid_until,
            "value_json": value_json,
            "raw_locator": fact.raw_locator,
            "updated_commit_seq": seq,
            "received_at": received_at,
        }
        connection.execute(
            sqlite_insert(heads)
            .values(
                family=fact.family,
                subject_key=fact.subject_key,
                source=fact.source,
                source_epoch=fact.source_epoch,
                **values,
            )
            .on_conflict_do_update(
                index_elements=[heads.c.family, heads.c.subject_key, heads.c.source, heads.c.source_epoch],
                set_=values,
            )
        )
        connection.execute(
            receipts.insert().values(
                family=fact.family,
                subject_key=fact.subject_key,
                source=fact.source,
                source_epoch=fact.source_epoch,
                position_key=position_key,
                source_seq=fact.source_seq,
                dedupe_key=fact.dedupe_key,
                evidence_hash=fact.evidence_hash,
                received_at=received_at,
                commit_seq=seq,
            )
        )
        touched_candidates[_candidate_key(fact)] = fact
        changed_heads += 1

    for winner, incoming in batch_conflicts:
        conflicts += _record_conflict(
            connection,
            fact=incoming,
            existing_hash=winner.evidence_hash,
            position_key=_position_key(incoming),
            conflict_kind="same_batch_position_reuse",
            at=received_at,
            commit_seq=commit_seq,
        )
        touched_candidates[_candidate_key(incoming)] = incoming

    for fact in touched_candidates.values():
        _prune_candidate_rows(
            connection,
            FactReceipt.__table__,
            _candidate_predicates(fact, FactReceipt.__table__),
            MAX_RECEIPTS_PER_CANDIDATE,
        )
        _prune_candidate_rows(
            connection,
            FactConflict.__table__,
            _candidate_predicates(fact, FactConflict.__table__),
            MAX_CONFLICTS_PER_CANDIDATE,
        )
    for family in sorted({fact.family for fact in touched_candidates.values()}):
        _prune_fact_family(connection, family)

    return ReducerResult(
        commit_seq=allocated_commit if allocated_commit is not None else current_commit,
        changed_heads=changed_heads,
        duplicates=duplicates,
        stale=stale,
        conflicts=conflicts,
    )


def _prepare_batch(
    facts: list[ReducerFact],
) -> tuple[list[tuple[ReducerFact, str]], int, list[tuple[ReducerFact, ReducerFact]]]:
    """Canonicalize batch order and retain typed same-position conflicts."""

    grouped: dict[tuple[str, str, str, str, str], dict[str, ReducerFact]] = {}
    duplicates = 0
    for raw_fact in facts:
        fact = _validate_fact(raw_fact)
        key = (*_candidate_key(fact), _position_key(fact))
        values = grouped.setdefault(key, {})
        if fact.evidence_hash in values:
            duplicates += 1
        else:
            values[fact.evidence_hash] = fact
    prepared = []
    conflicts = []
    for key in sorted(grouped):
        ordered = [fact for _hash, fact in sorted(grouped[key].items())]
        winner = ordered[0]
        prepared.append((winner, canonical_value_json(winner.value)))
        conflicts.extend((winner, incoming) for incoming in ordered[1:])
    return prepared, duplicates, conflicts


def read_fact_heads(connection: Connection, *, family: str, subject_key: str) -> tuple[int, list[dict[str, Any]]]:
    """Read one coherent candidate set at the transaction's catalog snapshot."""

    commit_seq = _current_commit_seq(connection)
    rows = connection.execute(
        select(FactHead.__table__)
        .where(FactHead.family == family, FactHead.subject_key == subject_key)
        .order_by(FactHead.source, FactHead.source_epoch)
    ).mappings()
    return commit_seq, [dict(row) for row in rows]


def _validate_fact(fact: ReducerFact) -> ReducerFact:
    if not fact.family or len(fact.family) > 32:
        raise ValueError("fact family is missing or too long")
    if not fact.subject_key or len(fact.subject_key.encode()) > 1024:
        raise ValueError("fact subject_key is missing or too long")
    if not fact.source or len(fact.source) > 64:
        raise ValueError("fact source is missing or too long")
    if fact.session_id is not None and (not fact.session_id.strip() or len(fact.session_id) > 255):
        raise ValueError("fact session_id is empty or too long")
    if len(fact.source_epoch) > 255:
        raise ValueError("fact source_epoch is too long")
    if fact.source_seq is not None and fact.source_seq < 0:
        raise ValueError("fact source_seq must be non-negative")
    if not _SHA256_RE.fullmatch(fact.dedupe_key):
        raise ValueError("fact dedupe_key must be lowercase sha256")
    if not _SHA256_RE.fullmatch(fact.evidence_hash):
        raise ValueError("fact evidence_hash must be lowercase sha256")
    if canonical_evidence_hash(fact.value) != fact.evidence_hash:
        raise ValueError("fact evidence_hash does not match canonical value")
    if len(canonical_value_json(fact.value).encode()) > MAX_VALUE_JSON_BYTES:
        raise ValueError("fact value exceeds reducer bound")
    if fact.raw_locator is not None and len(fact.raw_locator.encode()) > MAX_RAW_LOCATOR_BYTES:
        raise ValueError("fact raw_locator exceeds reducer bound")
    if fact.observed_at is not None:
        _aware(fact.observed_at, "observed_at")
    if fact.valid_until is not None:
        _aware(fact.valid_until, "valid_until")
    return fact


def _candidate_predicates(fact: ReducerFact, table) -> tuple[Any, ...]:
    return (
        table.c.family == fact.family,
        table.c.subject_key == fact.subject_key,
        table.c.source == fact.source,
        table.c.source_epoch == fact.source_epoch,
    )


def _candidate_key(fact: ReducerFact) -> tuple[str, str, str, str]:
    return fact.family, fact.subject_key, fact.source, fact.source_epoch


def _ordering_mode(fact: ReducerFact) -> str:
    return "sequenced" if fact.source_seq is not None else "observed_at"


def _position_key(fact: ReducerFact) -> str:
    if fact.source_seq is not None:
        return f"seq:{fact.source_seq:020d}"
    if fact.observed_at is None:
        raise ValueError("unsequenced fact requires observed_at")
    return f"time:{fact.observed_at.astimezone(UTC).isoformat(timespec='microseconds')}"


def _record_conflict(
    connection: Connection,
    *,
    fact: ReducerFact,
    existing_hash: str,
    position_key: str,
    conflict_kind: str,
    at: datetime,
    commit_seq,
) -> int:
    table = FactConflict.__table__
    exists = connection.execute(
        select(table.c.id).where(
            *_candidate_predicates(fact, table),
            table.c.position_key == position_key,
            table.c.incoming_hash == fact.evidence_hash,
        )
    ).scalar_one_or_none()
    if exists is not None:
        return 0
    connection.execute(
        table.insert().values(
            family=fact.family,
            subject_key=fact.subject_key,
            source=fact.source,
            source_epoch=fact.source_epoch,
            position_key=position_key,
            source_seq=fact.source_seq,
            conflict_kind=conflict_kind,
            existing_hash=existing_hash,
            incoming_hash=fact.evidence_hash,
            detected_at=at,
            commit_seq=commit_seq(),
        )
    )
    return 1


def _prune_candidate_rows(connection: Connection, table, candidate: tuple[Any, ...], keep: int) -> None:
    keep_ids = select(table.c.id).where(*candidate).order_by(table.c.id.desc()).limit(keep)
    connection.execute(delete(table).where(*candidate, table.c.id.not_in(keep_ids)))


def _prune_fact_family(connection: Connection, family: str) -> None:
    """Bound heads and all child history for one family in the same transaction."""

    heads = FactHead.__table__
    candidate_columns = (heads.c.family, heads.c.subject_key, heads.c.source, heads.c.source_epoch)
    keep_candidates = (
        select(*candidate_columns)
        .where(heads.c.family == family)
        .order_by(
            heads.c.updated_commit_seq.desc(),
            heads.c.subject_key,
            heads.c.source,
            heads.c.source_epoch,
        )
        .limit(MAX_HEADS_PER_FAMILY)
    )
    for table in (FactReceipt.__table__, FactConflict.__table__):
        child_candidate = tuple_(table.c.family, table.c.subject_key, table.c.source, table.c.source_epoch)
        connection.execute(delete(table).where(table.c.family == family, child_candidate.not_in(keep_candidates)))
    connection.execute(delete(heads).where(heads.c.family == family, tuple_(*candidate_columns).not_in(keep_candidates)))


def _current_commit_seq(connection: Connection) -> int:
    return int(connection.execute(select(catalog_meta.c.commit_seq).where(catalog_meta.c.singleton == 1)).scalar_one())


def _advance_commit_seq(connection: Connection, at: datetime) -> int:
    return int(
        connection.execute(
            update(catalog_meta)
            .where(catalog_meta.c.singleton == 1)
            .values(commit_seq=catalog_meta.c.commit_seq + 1, updated_at=at.isoformat())
            .returning(catalog_meta.c.commit_seq)
        ).scalar_one()
    )


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC)


def _sqlite_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise RuntimeError("catalog fact timestamp is invalid")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _parse_wire_datetime(value: object, field: str, *, nullable: bool = False) -> datetime | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError(f"machine evidence {field} must be an RFC3339 string")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as exc:
        raise ValueError(f"machine evidence {field} must be an RFC3339 string") from exc


__all__ = [
    "MAX_REDUCER_FACTS",
    "ReducerFact",
    "ReducerResult",
    "canonical_evidence_hash",
    "canonical_value_json",
    "read_fact_heads",
    "reducer_facts_from_machine_evidence",
    "reduce_fact_batch",
]
