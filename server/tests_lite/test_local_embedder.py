"""The embedder must refuse to produce a vector it cannot vouch for.

Why this exists: every failure mode here produces *numbers*. A zero vector
scores an identical similarity against the whole corpus, a wrong output head
returns plausible floats from the wrong tensor, and a short vector truncates to
something that still ranks. None of them raise on their own, and none are
distinguishable downstream from a correct embedding — they would surface as a
ranking opinion rather than a fault.

These tests use a stub session so they assert the contract rather than the
model: the real weights are a downloaded artifact, and a test that needs them
would be skipped exactly when it mattered.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.embedding_space import DOCUMENT_PREFIX
from zerg.embedding_space import QUERY_PREFIX
from zerg.services.local_embedder import LocalEmbedder
from zerg.services.local_embedder import LocalEmbedderUnavailable

DIMS = 4


class _StubTokenizer:
    def __init__(self):
        self.seen: list[str] = []

    def encode_batch(self, texts):
        self.seen.extend(texts)
        width = max(len(t.split()) for t in texts)
        return [
            type("E", (), {"ids": [1] * width, "attention_mask": [1] * width})()
            for _ in texts
        ]


class _StubSession:
    def __init__(self, vectors, *, output_name="sentence_embedding"):
        self._vectors = vectors
        self._output_name = output_name

    def get_outputs(self):
        return [type("O", (), {"name": self._output_name})()]

    def run(self, _outputs, feed):
        rows = feed["input_ids"].shape[0]
        return [np.asarray(self._vectors, dtype="float32")[:rows]]


def _embedder(vectors, *, output_name="sentence_embedding"):
    embedder = LocalEmbedder("/nonexistent", dims=DIMS)
    embedder._session = _StubSession(vectors, output_name=output_name)
    embedder._tokenizer = _StubTokenizer()
    embedder._embedding_output = 0
    return embedder


def test_unloaded_embedder_raises_rather_than_returning_nothing():
    embedder = LocalEmbedder("/nonexistent", dims=DIMS)
    assert embedder.ready is False
    with pytest.raises(LocalEmbedderUnavailable):
        embedder.embed_queries(["anything"])


def test_output_is_normalized_and_truncated_to_the_active_dimension():
    embedder = _embedder([[3.0, 4.0, 0.0, 0.0, 99.0]])
    vector = embedder.embed_queries(["hello"])[0]
    assert vector.shape == (DIMS,)
    assert np.isclose(np.linalg.norm(vector), 1.0)
    # Truncation happens before normalization, so the discarded 5th component
    # must not have influenced the result.
    assert np.allclose(vector, [0.6, 0.8, 0.0, 0.0])


def test_zero_vector_is_rejected_not_normalized():
    """A zero vector would tie against everything instead of failing."""
    embedder = _embedder([[0.0, 0.0, 0.0, 0.0]])
    with pytest.raises(LocalEmbedderUnavailable, match="zero or non-finite"):
        embedder.embed_queries(["hello"])


def test_non_finite_vector_is_rejected():
    embedder = _embedder([[float("nan"), 1.0, 0.0, 0.0]])
    with pytest.raises(LocalEmbedderUnavailable, match="zero or non-finite"):
        embedder.embed_queries(["hello"])


def test_too_few_dimensions_is_rejected():
    embedder = _embedder([[1.0, 0.0]])
    with pytest.raises(LocalEmbedderUnavailable, match="dims"):
        embedder.embed_queries(["hello"])


def test_queries_and_documents_use_their_prescribed_prefixes():
    """The prefixes are part of the space; measured worth ~10 points of recall."""
    embedder = _embedder([[1.0, 0.0, 0.0, 0.0]])
    embedder.embed_queries(["find the thing"])
    embedder.embed_documents(["the thing itself"])
    seen = embedder._tokenizer.seen
    assert seen[0].startswith(QUERY_PREFIX)
    assert seen[1].startswith(DOCUMENT_PREFIX)


def test_document_batches_are_length_sorted_but_returned_in_caller_order():
    """Sorting is a throughput detail; the caller must not have to know about it."""
    # Distinct unit vectors so each input's output is identifiable.
    embedder = _embedder([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]])
    texts = ["short", "a much much much longer document here", "medium length one"]
    vectors = embedder.embed_documents(texts)

    ordered_by_length = sorted(texts, key=len, reverse=True)
    assert [t[len(DOCUMENT_PREFIX) :] for t in embedder._tokenizer.seen] == ordered_by_length

    # The longest text was encoded first, so it received the first stub vector;
    # after restoration it must sit back at its original index 1.
    assert np.allclose(vectors[1], [1, 0, 0, 0])
    assert vectors.shape == (3, DIMS)


def test_empty_document_batch_is_not_an_error():
    embedder = _embedder([[1.0, 0.0, 0.0, 0.0]])
    assert embedder.embed_documents([]).shape == (0, DIMS)
