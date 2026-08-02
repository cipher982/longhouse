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

import base64
import os
import threading
import time
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.embedding_space import DOCUMENT_PREFIX
from zerg.embedding_space import QUERY_PREFIX
from zerg.services.local_embedder import LocalEmbedder
from zerg.services.local_embedder import LocalEmbedderUnavailable

DIMS = 4
_GOLDEN_QUERY = "how do I repair a managed session?"
_GOLDEN_VECTOR_BASE64 = (
    "H/uCvhpb3DuXMk280CVxu/zizz1HvxA98XW/OkIhYrwlxC0981V5vcKicT3wZaG8PLRrPGZ/x7yRpXo8/9y3O7rjGj1TFio+AMDkvU4c1DyIiMo9sXKiuy+BAr3rcP48QKguPENvsj1imk89ewROvaihp71G0gq8aQz/PDqZIr35J7E92u+MvEsf5Dx6Abm9zCiQPIB/zD2xXqM9IUyuvMbeTbuzNZA905FTveb5Mz2UGbs9oVeMPcF4MD0W2zA9d1okPSmTfr38ode9qfTMPOw9Dj2Ymrq8Ein7vJ/xHT3eLjM9QtiivWLFJr0v/DI9xfYLvavX/DyCnKu9vQPTvLg0k7zZPxc9oxqsPSqiMbpwrd+77ipbPUl2pb28vf26obXWPBSfKb3dPDo+qaUqPQg5oLrVfLc85VGGPdU7Ab0Z02g730x1PGtff70rTqA9dZ9tPc3Dkj0/XaW9vb0hvcGdKD3MbwS9s7ScvWFaPb0zor69/yudvbJllD3326s9fuCUvSF/lb09qmi7blcAvdqJzj0K2+I7q3e0PTIVjz1S/by8vbhmPR48rr3qqRe9u/k9PS7OQj3HHOo8a4aFPGWIvL2XDt29ey6yvUHIFLyB5DE9j+mUPZ1nwj3OqWG9ITxOPQZlkbxmkGs9r92RvJ6M/b2WXpG8Zt1hvCMQDb2DSwu9l3AmvShzQzxDQFU9bWnBvOLwbjpdr1a9D2shvXQrTL1FFfs6pVX2u/BfE71qpuK9BagDPexCCz5I7se70ZDRvdc597zCNge9qYuoPB57Sjwq7Oy8WzeVvTai9bzKNyQ9q9SYvYB0hb2I9Q09FcJZu69WED2DeKK9z7BAvCpxF7yyYRo82ka2vayhMT540gw+3SA5Ppup2T0B7uA7fuSKva8TRb18w4W9Gp+GvYM3wTyVdRy8+DBpve4Ig71aEAW9gBYFPSlDuTzygCc9AL8PPhgFYTwmnsO9dZL0Pc8Zkz0On6o8rwGlPWZSbrx0Anq9qsI+PPY+qjs1e3+7gTfWPaICF72ZkLq8PFEhPRsLNT6UFTW9M10gPVUw2TxCZBo9pXvsPBItSL10nhy9P5OrvVp2hT0ViHq8ko4qPcGgI74JUp68q8jbvJRku7xkljO9oTSGPDMMfjt1Q7K5s8aWvIjqe70eEK080LyxvflXej3rfMI7yTu4PL1u/jy7krm9r4eivWeY+bz8FaC9lWyvvUm7NT2J1Cu8qwqnPFyUxz0LPia9zAI0vde7A71/62a9mu61PC6Ahj3AMi89x/aEvMtZVz3PkDC6a4qEPd/qpT0D5Gw9rIPcuxOdmLzb+gK8ZcrTvMwaMTvLtwO9hFxhPVD2Xj301+q9RwYPPA=="
)


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

    ordered_by_length = sorted(texts, key=len)
    assert [t[len(DOCUMENT_PREFIX) :] for t in embedder._tokenizer.seen] == ordered_by_length

    # The longest text was encoded third, then restored to its caller index 1.
    assert np.allclose(vectors[1], [0, 0, 1, 0])
    assert vectors.shape == (3, DIMS)


def test_empty_document_batch_is_not_an_error():
    embedder = _embedder([[1.0, 0.0, 0.0, 0.0]])
    assert embedder.embed_documents([]).shape == (0, DIMS)


def test_waiting_query_runs_between_document_microbatches(monkeypatch):
    import zerg.services.local_embedder as local_embedder_module

    monkeypatch.setattr(local_embedder_module, "EMBED_DOCUMENT_MICROBATCH", 1)
    embedder = LocalEmbedder("/nonexistent", dims=DIMS)
    embedder._session = object()
    embedder._tokenizer = object()
    first_document_started = threading.Event()
    release_first_document = threading.Event()
    sequence: list[str] = []

    def encode(texts):
        label = "query" if texts[0].startswith(QUERY_PREFIX) else "document"
        sequence.append(label)
        if label == "document" and len(sequence) == 1:
            first_document_started.set()
            assert release_first_document.wait(1)
        return np.tile(np.array([[1, 0, 0, 0]], dtype="float32"), (len(texts), 1))

    monkeypatch.setattr(embedder, "_encode", encode)
    document_thread = threading.Thread(target=lambda: embedder.embed_documents(["a", "bb", "ccc"]))
    query_thread = threading.Thread(target=lambda: embedder.embed_queries(["interactive"]))
    document_thread.start()
    assert first_document_started.wait(1)
    query_thread.start()
    deadline = time.monotonic() + 1
    while embedder._waiting_queries == 0 and time.monotonic() < deadline:
        time.sleep(0.001)
    release_first_document.set()
    document_thread.join(1)
    query_thread.join(1)

    assert not document_thread.is_alive() and not query_thread.is_alive()
    assert sequence == ["document", "query", "document", "document"]


def test_pinned_production_graph_matches_the_golden_vector():
    """Exercise the downloaded tokenizer, ONNX graph, prefix, truncation, and normalization together."""

    model_dir_value = os.getenv("LONGHOUSE_EMBED_MODEL_DIR")
    if not model_dir_value:
        pytest.skip("production embedding artifact is not provisioned for this test run")

    from zerg.embedding_space import ACTIVE_EMBEDDING_DIMS
    from zerg.services.embedding_artifact import validate_embedding_artifact

    model_dir = Path(model_dir_value)
    assert validate_embedding_artifact(model_dir)["ready"] is True
    embedder = LocalEmbedder(model_dir, dims=ACTIVE_EMBEDDING_DIMS)
    embedder.load()
    actual = embedder.embed_queries([_GOLDEN_QUERY])[0]
    expected = np.frombuffer(base64.b64decode(_GOLDEN_VECTOR_BASE64), dtype="<f4")

    assert actual.shape == expected.shape == (ACTIVE_EMBEDDING_DIMS,)
    np.testing.assert_allclose(actual, expected, rtol=2e-4, atol=2e-4)
    assert np.isclose(np.linalg.norm(actual), 1.0, atol=1e-5)
