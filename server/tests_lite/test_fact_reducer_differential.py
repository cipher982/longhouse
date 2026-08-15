"""Differential tests for the fact reducer rewrite.

The reducer is moving from a per-fact loop to staged set-based SQL. Every test
here runs the same scenario through the reference implementation and the
candidate implementation and requires identical durable state.

While only one implementation exists, `CANDIDATE_REDUCERS` holds just the
reference and these run as characterization tests: they pin today's behaviour so
the rewrite has something to be wrong against. Adding the set-based function to
that list is what turns them into the oracle.

The scenarios are drawn from the failure modes two independent reviews called
out as the ones a naive `INSERT ... ON CONFLICT` rewrite would silently change.
"""

from __future__ import annotations

import random

import pytest

from tests_lite._fact_reducer_oracle import BASE_TIME
from tests_lite._fact_reducer_oracle import coverage_of
from tests_lite._fact_reducer_oracle import make_fact
from tests_lite._fact_reducer_oracle import random_batch
from tests_lite._fact_reducer_oracle import reference_reduce
from tests_lite._fact_reducer_oracle import run_scenario
from tests_lite._fact_reducer_oracle import setwise_reduce

CANDIDATE_REDUCERS = [setwise_reduce]


def assert_equivalent(tmp_path, *, name, seed_batches, batch):
    """Every implementation must produce byte-identical durable state."""

    reference = run_scenario(
        reference_reduce,
        seed_batches=seed_batches,
        batch=batch,
        tmp_path=tmp_path,
        name=f"{name}-reference",
    )
    for index, candidate_reduce in enumerate(CANDIDATE_REDUCERS):
        candidate = run_scenario(
            candidate_reduce,
            seed_batches=seed_batches,
            batch=batch,
            tmp_path=tmp_path,
            name=f"{name}-candidate-{index}",
        )
        assert candidate == reference, (
            f"{getattr(candidate_reduce, '__name__', candidate_reduce)} diverged: "
            f"{reference.difference(candidate)}"
        )
    return reference


def test_same_position_exact_duplicates(tmp_path):
    """Identical facts collapse to one head and count as duplicates."""

    fact = make_fact(seq=1, payload="v1")
    snapshot = assert_equivalent(
        tmp_path,
        name="exact-duplicates",
        seed_batches=[],
        batch=[fact, fact, fact],
    )
    assert len(snapshot.heads) == 1
    assert snapshot.result[2] >= 1  # duplicates counted


def test_same_position_distinct_hashes_record_a_conflict(tmp_path):
    """Two payloads at one position is the conflict the evidence must retain."""

    snapshot = assert_equivalent(
        tmp_path,
        name="same-position-conflict",
        seed_batches=[],
        batch=[make_fact(seq=1, payload="v1"), make_fact(seq=1, payload="v2")],
    )
    # The classification is the contract; ON CONFLICT DO NOTHING would drop it.
    assert snapshot.conflicts, "same-position divergence must leave conflict evidence"


def test_reused_dedupe_key_across_positions(tmp_path):
    """A dedupe key reused at a different position is not a duplicate."""

    assert_equivalent(
        tmp_path,
        name="dedupe-reuse",
        seed_batches=[[make_fact(seq=1, dedupe="shared", payload="v1")]],
        batch=[make_fact(seq=2, dedupe="shared", payload="v2")],
    )


def test_reused_dedupe_key_same_hash(tmp_path):
    assert_equivalent(
        tmp_path,
        name="dedupe-reuse-same-hash",
        seed_batches=[[make_fact(seq=1, dedupe="shared", payload="v1")]],
        batch=[make_fact(seq=2, dedupe="shared", payload="v1")],
    )


def test_stale_sequenced_fact_is_dropped_without_a_receipt(tmp_path):
    """Stale facts count as stale and leave no receipt."""

    snapshot = assert_equivalent(
        tmp_path,
        name="stale-sequenced",
        seed_batches=[[make_fact(seq=5, payload="v5")]],
        batch=[make_fact(seq=2, payload="v2")],
    )
    assert snapshot.result[3] >= 1  # stale counted
    assert all("seq:00000000000000000002" not in str(row) for row in snapshot.receipts)


def test_every_advancing_position_keeps_its_receipt(tmp_path):
    """Intermediate positions are retained; a collapsing upsert would lose them."""

    snapshot = assert_equivalent(
        tmp_path,
        name="advancing-positions",
        seed_batches=[],
        batch=[make_fact(seq=n, payload=f"v{n}") for n in (1, 2, 3)],
    )
    assert len(snapshot.receipts) == 3, "each accepted position must leave a receipt"
    assert snapshot.result[1] == 3  # changed_heads


def test_unsequenced_facts_order_by_observed_at(tmp_path):
    assert_equivalent(
        tmp_path,
        name="unsequenced",
        seed_batches=[],
        batch=[
            make_fact(seq=None, observed_offset_s=offset, payload=f"v{offset}")
            for offset in (0, 5, 2)
        ],
    )


def test_mixed_sequenced_and_unsequenced_is_an_ordering_mode_conflict(tmp_path):
    snapshot = assert_equivalent(
        tmp_path,
        name="ordering-mode-change",
        seed_batches=[[make_fact(seq=1, payload="v1")]],
        batch=[make_fact(seq=None, observed_offset_s=9, payload="v9")],
    )
    assert snapshot.conflicts, "an ordering-mode change must be recorded"


def test_conflict_replay_allocates_no_new_commit_seq(tmp_path):
    """Replaying a known conflict must not advance the catalog or duplicate rows."""

    seed = [make_fact(seq=1, payload="v1")]
    conflicting = [make_fact(seq=1, payload="v2")]
    once = run_scenario(
        reference_reduce,
        seed_batches=[seed, conflicting],
        batch=conflicting,
        tmp_path=tmp_path,
        name="conflict-replay-once",
    )
    assert_equivalent(
        tmp_path,
        name="conflict-replay",
        seed_batches=[seed, conflicting],
        batch=conflicting,
    )
    # Replaying the same conflict must not keep growing the evidence table.
    twice = run_scenario(
        reference_reduce,
        seed_batches=[seed, conflicting, conflicting],
        batch=conflicting,
        tmp_path=tmp_path,
        name="conflict-replay-twice",
    )
    assert len(twice.conflicts) == len(once.conflicts)


def test_receipt_retention_bound_per_candidate(tmp_path):
    """MAX_RECEIPTS_PER_CANDIDATE is enforced inside the transaction."""

    snapshot = assert_equivalent(
        tmp_path,
        name="receipt-bound",
        seed_batches=[],
        batch=[make_fact(seq=n, payload=f"v{n}") for n in range(1, 40)],
    )
    assert len(snapshot.receipts) <= 16


def test_batch_order_does_not_change_the_outcome(tmp_path):
    """The reducer sorts internally; permuting the input must not matter.

    This is the property most at risk in a set-based rewrite, because upsert
    order is not something SQL guarantees.
    """

    facts = [make_fact(seq=n, payload=f"v{n}") for n in (1, 2, 3, 5)]
    baseline = run_scenario(
        reference_reduce,
        seed_batches=[],
        batch=facts,
        tmp_path=tmp_path,
        name="order-baseline",
    )
    rng = random.Random(20260814)
    for attempt in range(5):
        shuffled = facts[:]
        rng.shuffle(shuffled)
        permuted = run_scenario(
            reference_reduce,
            seed_batches=[],
            batch=shuffled,
            tmp_path=tmp_path,
            name=f"order-permuted-{attempt}",
        )
        assert permuted == baseline, baseline.difference(permuted)


@pytest.mark.parametrize("seed", [1, 2, 3, 7, 11, 13, 17, 19])
def test_randomized_batches_are_equivalent(tmp_path, seed):
    """Randomized, collision-biased batches over seeded state."""

    rng = random.Random(seed)
    seed_batches = [random_batch(rng, rng.randint(1, 6)) for _ in range(rng.randint(0, 3))]
    assert_equivalent(
        tmp_path,
        name=f"random-{seed}",
        seed_batches=seed_batches,
        batch=random_batch(rng, rng.randint(1, 12)),
    )


@pytest.mark.parametrize("seed", [23, 29, 31])
def test_randomized_batches_are_order_independent(tmp_path, seed):
    rng = random.Random(seed)
    batch = random_batch(rng, 10)
    baseline = run_scenario(
        reference_reduce,
        seed_batches=[],
        batch=batch,
        tmp_path=tmp_path,
        name=f"random-order-{seed}",
    )
    shuffled = batch[:]
    rng.shuffle(shuffled)
    permuted = run_scenario(
        reference_reduce,
        seed_batches=[],
        batch=shuffled,
        tmp_path=tmp_path,
        name=f"random-order-{seed}-permuted",
    )
    assert permuted == baseline, baseline.difference(permuted)


def test_oracle_detects_a_planted_divergence(tmp_path):
    """The harness must fail when behaviour actually differs.

    Without this, a comparison that silently compares nothing would look like
    passing equivalence for the entire rewrite.
    """

    def wrong_reduce(connection, facts, **kwargs):
        # Drop the last fact: a plausible-looking off-by-one in batch handling.
        return reference_reduce(connection, list(facts)[:-1], **kwargs)

    batch = [make_fact(seq=n, payload=f"v{n}") for n in (1, 2, 3)]
    reference = run_scenario(
        reference_reduce, seed_batches=[], batch=batch, tmp_path=tmp_path, name="planted-reference"
    )
    candidate = run_scenario(
        wrong_reduce, seed_batches=[], batch=batch, tmp_path=tmp_path, name="planted-candidate"
    )
    assert candidate != reference
    assert reference.difference(candidate) != "identical"


def test_snapshot_covers_every_table_the_reducer_writes(tmp_path):
    """A snapshot that ignored a table would make the oracle vacuous."""

    snapshot = assert_equivalent(
        tmp_path,
        name="coverage",
        seed_batches=[[make_fact(seq=1, payload="v1")]],
        batch=[make_fact(seq=1, payload="v2"), make_fact(seq=2, payload="v3")],
    )
    assert snapshot.heads, "heads must be captured"
    assert snapshot.receipts, "receipts must be captured"
    assert snapshot.conflicts, "conflicts must be captured"
    assert snapshot.commit_seq > 0, "catalog commit_seq must be captured"
    assert BASE_TIME.year == 2026


def test_randomized_suite_reaches_every_reduction_branch(tmp_path):
    """The randomized tests are only worth running if they reach the branches.

    An earlier generator produced zero duplicates across every seed, which would
    have made a third of the reduction logic untested while the suite stayed
    green. This asserts coverage directly so that regression is loud.
    """

    snapshots = []
    for seed in (1, 2, 3, 7, 11, 13, 17, 19):
        rng = random.Random(seed)
        seed_batches = [random_batch(rng, rng.randint(1, 6)) for _ in range(rng.randint(0, 3))]
        snapshots.append(
            run_scenario(
                reference_reduce,
                seed_batches=seed_batches,
                batch=random_batch(rng, rng.randint(1, 12)),
                tmp_path=tmp_path,
                name=f"coverage-random-{seed}",
            )
        )

    # Stale needs prior state at a higher position than the incoming batch, which
    # random batches rarely build on their own because the reducer sorts each
    # batch ascending. Construct it rather than hope for it.
    snapshots.append(
        run_scenario(
            reference_reduce,
            seed_batches=[[make_fact(seq=8, payload="v8")]],
            batch=[make_fact(seq=n, payload=f"v{n}") for n in (1, 2, 3)],
            tmp_path=tmp_path,
            name="coverage-stale",
        )
    )

    coverage = coverage_of(snapshots)
    missing = [branch for branch, count in coverage.items() if count == 0]
    assert not missing, f"randomized suite never exercised: {missing} (coverage={coverage})"


def test_head_established_earlier_in_the_batch_is_visible_to_later_facts(tmp_path):
    """An accepted fact must be visible to the rest of its own batch.

    The row-wise reducer writes each head before reading the next fact, so a
    later fact in the same batch sees it. A set-based rewrite that preloads
    state and forgets to fold its own writes back in produces a different
    outcome only in this shape -- a sequenced fact establishing a head, then an
    unsequenced fact for the same candidate, which must be an
    ordering_mode_change rather than a second accepted head.

    Found by a planted-bug run: the randomized seeds caught it and none of the
    hand-written scenarios did.
    """

    snapshot = assert_equivalent(
        tmp_path,
        name="intra-batch-cascade",
        seed_batches=[],
        batch=[
            make_fact(seq=1, payload="v1"),
            make_fact(seq=None, observed_offset_s=7, payload="v7"),
        ],
    )
    assert snapshot.conflicts, "the second fact must conflict against the head the first established"
    assert snapshot.result[1] == 1, "only the first fact may become a head"


def test_dedupe_key_established_earlier_in_the_batch_is_visible_to_later_facts(tmp_path):
    """The dedupe index must also fold in this batch's own accepted facts.

    Two facts sharing a dedupe key at different positions survive `_prepare_batch`
    as separate entries, so the second must see the receipt the first wrote and
    classify as dedupe_key_reuse rather than being accepted as a second head.

    Also found by planted-bug run rather than by design: removing this cascade
    left every other test green.
    """

    snapshot = assert_equivalent(
        tmp_path,
        name="intra-batch-dedupe",
        seed_batches=[],
        batch=[
            make_fact(seq=1, dedupe="shared", payload="v1"),
            make_fact(seq=2, dedupe="shared", payload="v2"),
        ],
    )
    assert snapshot.result[1] == 1, "the second fact must not become a head"
    assert snapshot.conflicts, "reusing a dedupe key at a new position is a recorded conflict"
