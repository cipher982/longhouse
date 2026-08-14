"""Differential oracle for the fact reducer.

The reducer is being converted from a per-fact loop to staged set-based SQL. Its
observable behaviour is not obvious from reading it: the head outcome depends on
batch order, the counters are served through a health RPC, conflict evidence
carries a classification that only the first writer sets, and receipts for
intermediate positions must survive. A rewrite that "looks equivalent" is exactly
the kind of change that passes review and silently changes results.

So the rewrite is validated against this oracle rather than against reasoning:
run both implementations over the same seeded state and batch, snapshot
*everything* durable, and require byte equality.

Nothing here asserts what the behaviour should be. It asserts that it does not
change.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import Callable

from sqlalchemy import select

from zerg.catalogd.fact_reducer import ReducerFact
from zerg.catalogd.fact_reducer import ReducerResult
from zerg.catalogd.fact_reducer import reduce_fact_batch
from zerg.catalogd.models import FactConflict
from zerg.catalogd.models import FactHead
from zerg.catalogd.models import FactReceipt
from zerg.catalogd.schema import catalog_meta
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.machine_evidence import canonical_evidence_hash

BASE_TIME = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)

ReduceFn = Callable[..., ReducerResult]


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Everything durable the reducer can influence, in a comparable form."""

    result: tuple[int, int, int, int, int]
    commit_seq: int
    heads: tuple[tuple[Any, ...], ...]
    receipts: tuple[tuple[Any, ...], ...]
    conflicts: tuple[tuple[Any, ...], ...]

    def difference(self, other: "Snapshot") -> str:
        """Human-readable first divergence, so a failure names itself."""

        if self.result != other.result:
            fields = ("commit_seq", "changed_heads", "duplicates", "stale", "conflicts")
            deltas = [
                f"{name}: {mine!r} != {theirs!r}"
                for name, mine, theirs in zip(fields, self.result, other.result, strict=True)
                if mine != theirs
            ]
            return "counters differ -> " + "; ".join(deltas)
        if self.commit_seq != other.commit_seq:
            return f"catalog commit_seq differs -> {self.commit_seq} != {other.commit_seq}"
        for name, mine, theirs in (
            ("heads", self.heads, other.heads),
            ("receipts", self.receipts, other.receipts),
            ("conflicts", self.conflicts, other.conflicts),
        ):
            if mine != theirs:
                if len(mine) != len(theirs):
                    return f"{name} row count differs -> {len(mine)} != {len(theirs)}"
                for left, right in zip(mine, theirs, strict=True):
                    if left != right:
                        return f"{name} row differs ->\n  reference: {left!r}\n  candidate: {right!r}"
        return "identical"


def _rows(connection, table, order_columns) -> tuple[tuple[Any, ...], ...]:
    # Sorted by a stable key rather than by insertion order: the set-based
    # implementation is free to write rows in a different physical order, and
    # that difference is not a behaviour change.
    statement = select(table).order_by(*order_columns)
    return tuple(tuple(row) for row in connection.execute(statement).all())


def snapshot(connection, result: ReducerResult) -> Snapshot:
    heads = FactHead.__table__
    receipts = FactReceipt.__table__
    conflicts = FactConflict.__table__
    commit_seq = connection.execute(select(catalog_meta.c.commit_seq)).scalar_one()
    return Snapshot(
        result=(result.commit_seq, result.changed_heads, result.duplicates, result.stale, result.conflicts),
        commit_seq=int(commit_seq),
        heads=_rows(
            connection,
            heads,
            (heads.c.family, heads.c.subject_key, heads.c.source, heads.c.source_epoch),
        ),
        receipts=_rows(
            connection,
            receipts,
            (
                receipts.c.family,
                receipts.c.subject_key,
                receipts.c.source,
                receipts.c.source_epoch,
                receipts.c.position_key,
                receipts.c.evidence_hash,
            ),
        ),
        conflicts=_rows(
            connection,
            conflicts,
            (
                conflicts.c.family,
                conflicts.c.subject_key,
                conflicts.c.source,
                conflicts.c.source_epoch,
                conflicts.c.position_key,
                conflicts.c.conflict_kind,
                conflicts.c.existing_hash,
                conflicts.c.incoming_hash,
            ),
        ),
    )


def make_fact(
    *,
    family: str = "activity",
    subject: str = "s1",
    source: str = "machine-a",
    epoch: str = "epoch-1",
    seq: int | None = 1,
    dedupe: str | None = None,
    payload: Any = "v1",
    observed_offset_s: int = 0,
    session_id: str | None = None,
) -> ReducerFact:
    """One fact, with the identity fields that drive reduction made explicit.

    `payload` exists purely to vary `evidence_hash`: two facts with the same
    identity but different payloads are the conflict case the reducer classifies.
    """

    value: dict[str, Any] = {"payload": payload}
    if session_id is not None:
        value["session_id"] = session_id
    observed_at = BASE_TIME + timedelta(seconds=observed_offset_s)
    value["observed_at"] = observed_at.isoformat()
    evidence_hash = canonical_evidence_hash(value)
    if dedupe is None:
        dedupe = f"{source}:{epoch}:{seq if seq is not None else observed_offset_s}"
    # dedupe_key is validated as a lowercase sha256, so callers pass a readable
    # label and the harness hashes it. Keeping the label in the test keeps the
    # scenarios legible; hashing here keeps them valid.
    dedupe_hash = hashlib.sha256(dedupe.encode()).hexdigest()
    return ReducerFact(
        family=family,
        subject_key=subject,
        source=source,
        source_epoch=epoch,
        source_seq=seq,
        dedupe_key=dedupe_hash,
        evidence_hash=evidence_hash,
        value=value,
        observed_at=observed_at,
        session_id=session_id,
    )


def run_scenario(
    reduce: ReduceFn,
    *,
    seed_batches: list[list[ReducerFact]],
    batch: list[ReducerFact],
    tmp_path,
    name: str,
) -> Snapshot:
    """Apply seed batches, then the batch under test, and snapshot the result.

    Each scenario gets its own database file so ordering effects come from the
    batch rather than from residue of a previous scenario.
    """

    engine = create_catalog_engine(str(tmp_path / f"{name}.db"))
    initialize_catalog_schema(engine)
    try:
        with engine.begin() as connection:
            for index, seed in enumerate(seed_batches):
                reduce(connection, seed, received_at=BASE_TIME + timedelta(seconds=index))
            result = reduce(connection, batch, received_at=BASE_TIME + timedelta(minutes=1))
            return snapshot(connection, result)
    finally:
        engine.dispose()


def reference_reduce(connection, facts, **kwargs) -> ReducerResult:
    """The current row-wise implementation, pinned as the oracle."""

    return reduce_fact_batch(connection, facts, **kwargs)


def random_batch(rng: random.Random, size: int) -> list[ReducerFact]:
    """Generate a batch biased toward collision, not toward realism.

    Uniform random facts almost never collide, and collisions are the entire
    behaviour under test. An earlier version of this generator produced zero
    duplicates across eleven seeded scenarios, which would have left the whole
    duplicate branch unexercised by the randomized suite -- so repetition is
    injected explicitly rather than hoped for.
    """

    families = ("activity", "control", "run")
    subjects = ("s1", "s2")
    sources = ("machine-a", "machine-b")
    epochs = ("epoch-1", "epoch-2")
    facts: list[ReducerFact] = []
    for _ in range(size):
        # Re-emit an earlier fact verbatim: the exact-duplicate path.
        if facts and rng.random() < 0.25:
            facts.append(rng.choice(facts))
            continue
        sequenced = rng.random() < 0.7
        seq = rng.choice([1, 2, 3, 5, 8]) if sequenced else None
        facts.append(
            make_fact(
                family=rng.choice(families),
                subject=rng.choice(subjects),
                source=rng.choice(sources),
                epoch=rng.choice(epochs),
                seq=seq,
                dedupe="shared-dedupe" if rng.random() < 0.25 else None,
                payload=rng.choice(["v1", "v2", "v3"]),
                observed_offset_s=rng.choice([0, 1, 2, 5]),
            )
        )
    return facts


def coverage_of(snapshots) -> dict[str, int]:
    """Which reduction branches a set of scenarios actually reached.

    Used to keep the randomized suite honest: a generator that stops producing
    conflicts or duplicates silently turns those tests into no-ops.
    """

    totals = {"changed_heads": 0, "duplicates": 0, "stale": 0, "conflicts": 0}
    for snap in snapshots:
        _commit, changed, duplicates, stale, conflicts = snap.result
        totals["changed_heads"] += changed
        totals["duplicates"] += duplicates
        totals["stale"] += stale
        totals["conflicts"] += conflicts
    return totals
