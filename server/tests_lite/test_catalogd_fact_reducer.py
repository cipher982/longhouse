from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func
from sqlalchemy import select

import zerg.catalogd.fact_reducer as fact_reducer
from zerg.catalogd.fact_reducer import ReducerFact
from zerg.catalogd.fact_reducer import canonical_evidence_hash
from zerg.catalogd.fact_reducer import canonical_value_json
from zerg.catalogd.fact_reducer import read_fact_heads
from zerg.catalogd.fact_reducer import reduce_fact_batch
from zerg.catalogd.fact_reducer import reducer_facts_from_machine_evidence
from zerg.catalogd.models import FactConflict
from zerg.catalogd.models import FactReceipt
from zerg.catalogd.schema import catalog_meta
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
HASH_VECTOR = Path(__file__).parents[2] / "schemas" / "machine-evidence-hash-v1.json"


def _fact(
    *,
    subject: str = "run:run-1",
    run_id: str = "run-1",
    source_seq: int | None = 1,
    state: str = "thinking",
    observed_at: datetime = NOW,
) -> ReducerFact:
    value = {
        "kind": state,
        "observed_at": observed_at.isoformat(),
        "run_id": run_id,
        "session_id": "session-1",
    }
    position = str(source_seq) if source_seq is not None else canonical_evidence_hash(value)
    dedupe = hashlib.sha256(f"activity:{subject}:phase_ledger:epoch-1:{position}".encode()).hexdigest()
    return ReducerFact(
        family="activity",
        subject_key=subject,
        source="phase_ledger",
        source_epoch="epoch-1",
        source_seq=source_seq,
        dedupe_key=dedupe,
        evidence_hash=canonical_evidence_hash(value),
        value=value,
        observed_at=observed_at,
        valid_until=observed_at + timedelta(seconds=90),
    )


def _engine(tmp_path):
    engine = create_catalog_engine(tmp_path / "catalog.db")
    initialize_catalog_schema(engine)
    return engine


def test_canonical_hash_matches_shared_rust_vector():
    vector = json.loads(HASH_VECTOR.read_text())

    assert canonical_evidence_hash(vector["value"]) == vector["sha256"]
    assert canonical_value_json(vector["value"]) == vector["canonical_json"]


@pytest.mark.parametrize("value", [-(2**63), 2**64 - 1])
def test_canonical_hash_accepts_serde_json_integer_boundaries(value):
    assert len(canonical_evidence_hash({"integer": value})) == 64


@pytest.mark.parametrize("value", [-(2**63) - 1, 2**64])
def test_canonical_hash_rejects_integers_outside_serde_json_range(value):
    with pytest.raises(ValueError, match="serde_json range"):
        canonical_evidence_hash({"integer": value})


@pytest.mark.parametrize(
    "value",
    [
        "2026-99-99T99:99:99Z",
        "2016-12-31T23:59:60Z",
        "2026-05-08t12:00:00z",
        "2026-05-08 12:00:00Z",
        "0000-01-01T00:00:00Z",
        "0001-01-01T00:00:00+00:01",
        "9999-12-31T23:59:59-00:01",
    ],
)
def test_canonical_hash_preserves_unparseable_declared_timestamps(value):
    assert canonical_value_json({"observed_at": value}) == json.dumps(
        {"observed_at": value}, separators=(",", ":")
    )


def test_extraction_rejects_subject_that_does_not_match_explicit_run_id():
    value = {
        "provider": "codex",
        "session_id": "session-1",
        "run_id": "run-1",
        "source": "phase_ledger",
        "observed_at": NOW.isoformat(),
        "valid_until": (NOW + timedelta(seconds=90)).isoformat(),
    }
    evidence_hash = canonical_evidence_hash(value)
    envelope = {
        "schema_version": 2,
        "activity": [value],
        "identities": [
            {
                "fact_family": "activity",
                "fact_index": 0,
                "subject_key": "run:wrong-run",
                "source": "phase_ledger",
                "source_epoch": "epoch-1",
                "source_seq": 1,
                "sequenced": True,
                "dedupe_key": hashlib.sha256(b"dedupe").hexdigest(),
                "evidence_hash": evidence_hash,
            }
        ],
    }

    with pytest.raises(ValueError, match="does not match"):
        reducer_facts_from_machine_evidence(envelope)


def test_duplicate_is_zero_write_and_does_not_advance_commit(tmp_path):
    engine = _engine(tmp_path)
    fact = _fact()
    with engine.begin() as connection:
        first = reduce_fact_batch(connection, [fact], received_at=NOW)
    with engine.begin() as connection:
        duplicate = reduce_fact_batch(connection, [fact], received_at=NOW + timedelta(seconds=1))
        snapshot_seq, heads = read_fact_heads(connection, family="activity", subject_key=fact.subject_key)

    assert first.changed_heads == 1
    assert duplicate.changed_heads == 0
    assert duplicate.duplicates == 1
    assert duplicate.commit_seq == first.commit_seq == snapshot_seq
    assert len(heads) == 1


def test_same_batch_duplicate_is_collapsed_before_writing(tmp_path):
    engine = _engine(tmp_path)
    fact = _fact()

    with engine.begin() as connection:
        result = reduce_fact_batch(connection, [fact, fact], received_at=NOW)
        receipt_count = connection.execute(select(func.count()).select_from(FactReceipt)).scalar_one()

    assert result.changed_heads == 1
    assert result.duplicates == 1
    assert receipt_count == 1


def test_batch_permutation_has_same_heads_and_commit_boundary(tmp_path):
    facts = [
        _fact(subject="run:run-1", run_id="run-1", source_seq=4, state="thinking"),
        _fact(subject="run:run-2", run_id="run-2", source_seq=2, state="running"),
    ]
    snapshots = []
    for suffix, batch in (("forward", facts), ("reverse", list(reversed(facts)))):
        engine = create_catalog_engine(tmp_path / f"{suffix}.db")
        initialize_catalog_schema(engine)
        with engine.begin() as connection:
            result = reduce_fact_batch(connection, batch, received_at=NOW)
            rows = connection.execute(
                select(FactReceipt.subject_key, FactReceipt.evidence_hash).order_by(FactReceipt.subject_key)
            ).all()
        snapshots.append((result.commit_seq, result.changed_heads, rows))

    assert snapshots[0] == snapshots[1]


def test_sequenced_conflict_is_bounded_and_never_replaces_head(tmp_path):
    engine = _engine(tmp_path)
    original = _fact(source_seq=5, state="thinking")
    conflict = _fact(source_seq=5, state="idle", observed_at=NOW + timedelta(seconds=1))
    with engine.begin() as connection:
        initial = reduce_fact_batch(connection, [original], received_at=NOW)
        result = reduce_fact_batch(connection, [conflict], received_at=NOW + timedelta(seconds=1))
        repeated = reduce_fact_batch(connection, [conflict], received_at=NOW + timedelta(seconds=2))
        _, heads = read_fact_heads(connection, family="activity", subject_key=original.subject_key)
        conflict_count = connection.execute(select(func.count()).select_from(FactConflict)).scalar_one()

    assert result.conflicts == 1
    assert result.commit_seq == initial.commit_seq + 1
    assert repeated.commit_seq == result.commit_seq
    assert conflict_count == 1
    assert '"kind":"thinking"' in heads[0]["value_json"]


def test_stale_position_and_old_run_cannot_mutate_current_head(tmp_path):
    engine = _engine(tmp_path)
    run_one = _fact(subject="run:run-1", run_id="run-1", source_seq=10)
    run_two = _fact(subject="run:run-2", run_id="run-2", source_seq=1, state="running")
    stale_run_one = _fact(
        subject="run:run-1",
        run_id="run-1",
        source_seq=9,
        state="idle",
        observed_at=NOW + timedelta(seconds=2),
    )
    with engine.begin() as connection:
        first = reduce_fact_batch(connection, [run_one, run_two], received_at=NOW)
        stale = reduce_fact_batch(connection, [stale_run_one], received_at=NOW + timedelta(seconds=2))
        _, run_one_heads = read_fact_heads(connection, family="activity", subject_key=run_one.subject_key)
        _, run_two_heads = read_fact_heads(connection, family="activity", subject_key=run_two.subject_key)

    assert first.changed_heads == 2
    assert stale.stale == 1
    assert stale.commit_seq == first.commit_seq
    assert '"kind":"thinking"' in run_one_heads[0]["value_json"]
    assert '"kind":"running"' in run_two_heads[0]["value_json"]


def test_same_position_batch_conflict_is_recorded_independent_of_order(tmp_path):
    for source_seq in (4, None):
        thinking = _fact(source_seq=source_seq, state="thinking")
        idle = _fact(source_seq=source_seq, state="idle")
        snapshots = []
        for suffix, facts in (("forward", [thinking, idle]), ("reverse", [idle, thinking])):
            engine = create_catalog_engine(tmp_path / f"conflict-{source_seq}-{suffix}.db")
            initialize_catalog_schema(engine)
            with engine.begin() as connection:
                result = reduce_fact_batch(connection, facts, received_at=NOW)
                _, heads = read_fact_heads(connection, family="activity", subject_key=thinking.subject_key)
                rows = connection.execute(
                    select(FactConflict.existing_hash, FactConflict.incoming_hash, FactConflict.conflict_kind)
                ).all()
            snapshots.append((result, heads[0]["evidence_hash"], rows))

        assert snapshots[0] == snapshots[1]
        assert snapshots[0][0].changed_heads == 1
        assert snapshots[0][0].conflicts == 1
        assert snapshots[0][2][0].conflict_kind == "same_batch_position_reuse"


def test_ordering_mode_change_conflicts_without_replacing_head(tmp_path):
    engine = _engine(tmp_path)
    sequenced = _fact(source_seq=4, state="thinking")
    unsequenced = _fact(source_seq=None, state="idle", observed_at=NOW + timedelta(seconds=1))
    with engine.begin() as connection:
        first = reduce_fact_batch(connection, [sequenced], received_at=NOW)
        changed_mode = reduce_fact_batch(connection, [unsequenced], received_at=NOW + timedelta(seconds=1))
        _, heads = read_fact_heads(connection, family="activity", subject_key=sequenced.subject_key)
    assert changed_mode.conflicts == 1
    assert changed_mode.commit_seq == first.commit_seq + 1
    assert heads[0]["ordering_mode"] == "sequenced"
    assert heads[0]["source_seq"] == 4


def test_higher_sequence_advances_position_even_when_content_hash_is_unchanged(tmp_path):
    engine = _engine(tmp_path)
    first = _fact(source_seq=1)
    second = _fact(source_seq=2)
    assert first.evidence_hash == second.evidence_hash
    with engine.begin() as connection:
        reduce_fact_batch(connection, [first], received_at=NOW)
        result = reduce_fact_batch(connection, [second], received_at=NOW + timedelta(seconds=1))
        _, heads = read_fact_heads(connection, family="activity", subject_key=first.subject_key)
    assert result.changed_heads == 1
    assert heads[0]["source_seq"] == 2


def test_unsequenced_receipts_are_bounded_and_restart_replays_same_head(tmp_path):
    database = tmp_path / "catalog.db"
    engine = create_catalog_engine(database)
    initialize_catalog_schema(engine)
    last = None
    with engine.begin() as connection:
        for index in range(70):
            last = _fact(source_seq=None, state=f"raw-{index}", observed_at=NOW + timedelta(seconds=index))
            reduce_fact_batch(connection, [last], received_at=NOW + timedelta(seconds=index))
        receipt_count = connection.execute(select(func.count()).select_from(FactReceipt)).scalar_one()
        before_seq, before = read_fact_heads(connection, family="activity", subject_key=last.subject_key)
    engine.dispose()

    reopened = create_catalog_engine(database)
    initialize_catalog_schema(reopened)
    with reopened.begin() as connection:
        after_seq, after = read_fact_heads(connection, family="activity", subject_key=last.subject_key)

    assert receipt_count == 16
    assert before_seq == after_seq
    assert before[0]["evidence_hash"] == after[0]["evidence_hash"] == last.evidence_hash


def test_global_family_bound_evicts_heads_and_child_history_together(tmp_path, monkeypatch):
    monkeypatch.setattr(fact_reducer, "MAX_HEADS_PER_FAMILY", 3)
    engine = _engine(tmp_path)
    original = [_fact(subject=f"run:run-{index}", run_id=f"run-{index}", source_seq=1) for index in range(3)]
    conflicts = [replace(fact, value={**fact.value, "kind": "conflict"}) for fact in original]
    conflicts = [replace(fact, evidence_hash=canonical_evidence_hash(fact.value)) for fact in conflicts]
    newcomers = [_fact(subject=f"run:run-{index}", run_id=f"run-{index}", source_seq=1) for index in range(3, 5)]
    with engine.begin() as connection:
        reduce_fact_batch(connection, original, received_at=NOW)
        reduce_fact_batch(connection, conflicts, received_at=NOW + timedelta(seconds=1))
        reduce_fact_batch(connection, newcomers, received_at=NOW + timedelta(seconds=2))
        head_count = connection.execute(select(func.count()).select_from(fact_reducer.FactHead)).scalar_one()
        receipt_count = connection.execute(select(func.count()).select_from(FactReceipt)).scalar_one()
        conflict_count = connection.execute(select(func.count()).select_from(FactConflict)).scalar_one()

    assert head_count == 3
    assert receipt_count == 3
    assert conflict_count == 1


def test_failed_outer_transaction_rolls_back_head_and_commit_sequence(tmp_path):
    engine = _engine(tmp_path)
    with pytest.raises(RuntimeError, match="crash point"):
        with engine.begin() as connection:
            reduce_fact_batch(connection, [_fact()], received_at=NOW)
            raise RuntimeError("crash point")

    with engine.begin() as connection:
        commit_seq = connection.execute(select(catalog_meta.c.commit_seq)).scalar_one()
        _, heads = read_fact_heads(connection, family="activity", subject_key=_fact().subject_key)
    assert commit_seq == 0
    assert heads == []


def test_commit_sequence_override_must_match_current_catalog_commit(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as connection:
        with pytest.raises(ValueError, match="must equal the current catalog commit"):
            reduce_fact_batch(connection, [_fact()], received_at=NOW, commit_seq_override=1)


def test_hash_mismatch_is_rejected_before_any_write(tmp_path):
    engine = _engine(tmp_path)
    bad = _fact()
    bad = replace(bad, evidence_hash="0" * 64)
    with engine.begin() as connection, pytest.raises(ValueError, match="canonical value"):
        reduce_fact_batch(connection, [bad], received_at=NOW)
