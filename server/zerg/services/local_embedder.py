"""In-process embedding, so recall never waits on a third party.

Recall used to embed the query by calling a hosted API inside the request. That
provider's p50 was fine (~0.4s) and its tail was not: 13.4s and 27.0s measured
against a 5s route budget. The lane could therefore never finish, so it returned
nothing while consuming the whole budget, and the same tail killed the corpus
projector. No timeout tuning fixes a dependency whose p99 exceeds the request.

A local forward pass is not merely faster, it is *bounded*: measured on the
hosted 8-vCPU EPYC at four threads, p50 21.4ms and p99 24.5ms — a 3ms spread.
It also makes recall work without internet, which the product claims and did
not deliver.

Two things here are load-bearing and easy to get wrong:

- **fp16, not int8.** int8 measured 6x slower on that host (151ms p50) because
  Zen 2 has no AVX-512 VNNI, so int8 is emulated rather than accelerated. The
  conventional quantization choice is the wrong one on this silicon.
- **Task prefixes are part of the embedding space.** EmbeddingGemma is trained
  with an asymmetric query/document framing. Query and corpus text must be
  wrapped identically on both sides of any migration or the vectors do not
  compare.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from zerg.embedding_space import DOCUMENT_PREFIX
from zerg.embedding_space import EMBEDDING_OUTPUT_NAME
from zerg.embedding_space import QUERY_PREFIX

if TYPE_CHECKING:  # pragma: no cover - typing only
    from zerg.models_config import EmbeddingSpaceConfig

logger = logging.getLogger(__name__)

# Measured on the hosted box: 4 threads beat 8 (p99 24.5ms vs 90.5ms).
# Oversubscribing a host that is already serving costs more than the
# parallelism returns, and a bounded count keeps the embedder from starving
# the rest of the process.
EMBED_INTRA_OP_THREADS = int(os.getenv("LONGHOUSE_EMBED_THREADS", "4"))
EMBED_MAX_TOKENS = int(os.getenv("LONGHOUSE_EMBED_MAX_TOKENS", "1024"))
EMBED_DOCUMENT_MICROBATCH = int(os.getenv("LONGHOUSE_EMBED_DOCUMENT_MICROBATCH", "8"))


class LocalEmbedderUnavailable(RuntimeError):
    """The model is not loaded. Callers must fail loudly, never return nothing.

    A dense lane that answers with an empty list when its model is missing is
    indistinguishable from one that legitimately found nothing, which is exactly
    how the previous remote lane stayed dead without anyone noticing.
    """


class LocalEmbedder:
    """One process-wide ONNX session shared by the query path and the projector.

    Serialized behind a lock: concurrent sessions would each spawn their own
    intra-op pool and oversubscribe the host, which is the failure the thread
    measurement above already rules out.
    """

    def __init__(self, model_dir: str | Path, *, dims: int) -> None:
        self._model_dir = Path(model_dir)
        self._dims = dims
        self._condition = threading.Condition()
        self._active = False
        self._waiting_queries = 0
        self._session = None
        self._tokenizer = None
        self._embedding_output = 0

    def load(self) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        options = ort.SessionOptions()
        options.intra_op_num_threads = EMBED_INTRA_OP_THREADS
        options.inter_op_num_threads = 1
        model_path = self._model_dir / "model_fp16.onnx"
        tokenizer_path = self._model_dir / "tokenizer.json"
        if not model_path.is_file() or model_path.is_symlink():
            raise LocalEmbedderUnavailable(f"embedding model missing at {model_path}")
        if not tokenizer_path.is_file() or tokenizer_path.is_symlink():
            raise LocalEmbedderUnavailable(f"embedding tokenizer missing at {tokenizer_path}")
        session = ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])
        tokenizer = Tokenizer.from_file(str(tokenizer_path))
        tokenizer.enable_padding()
        tokenizer.enable_truncation(max_length=EMBED_MAX_TOKENS)
        names = [o.name for o in session.get_outputs()]
        if EMBEDDING_OUTPUT_NAME not in names:
            # Falling back to output 0 would silently embed with whatever tensor
            # happened to come first — plausible-looking numbers from the wrong
            # head, which no test downstream could distinguish from correct ones.
            raise LocalEmbedderUnavailable(f"model exposes no {EMBEDDING_OUTPUT_NAME} output, found {names}")
        self._embedding_output = names.index(EMBEDDING_OUTPUT_NAME)
        self._session = session
        self._tokenizer = tokenizer
        logger.info("Local embedder loaded model=%s threads=%d", model_path, EMBED_INTRA_OP_THREADS)

    @property
    def ready(self) -> bool:
        return self._session is not None

    def _acquire_query(self) -> None:
        with self._condition:
            self._waiting_queries += 1
            try:
                while self._active:
                    self._condition.wait()
                self._active = True
            finally:
                self._waiting_queries -= 1

    def _acquire_document_batch(self) -> None:
        with self._condition:
            while self._active or self._waiting_queries:
                self._condition.wait()
            self._active = True

    def _release(self) -> None:
        with self._condition:
            self._active = False
            self._condition.notify_all()

    def _encode(self, texts: list[str]) -> np.ndarray:
        if self._session is None or self._tokenizer is None:
            raise LocalEmbedderUnavailable("embedding model is not loaded")
        encoded = self._tokenizer.encode_batch(texts)
        ids = np.array([e.ids for e in encoded], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        out = self._session.run(None, {"input_ids": ids, "attention_mask": mask})[self._embedding_output]
        vectors = np.asarray(out, dtype="float32")
        if vectors.ndim != 2 or vectors.shape[0] != len(texts):
            raise LocalEmbedderUnavailable(f"model returned invalid embedding shape {vectors.shape}")
        # Truncate then renormalize: Matryoshka dimensions are only valid as a
        # prefix of the full vector, and cosine over an unnormalized prefix is
        # not the similarity the model was trained for.
        if vectors.shape[1] < self._dims:
            raise LocalEmbedderUnavailable(f"model returned {vectors.shape[1]} dims, need at least {self._dims}")
        vectors = vectors[:, : self._dims]
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        # A zero or non-finite vector normalizes to something that scores an
        # equal, meaningless similarity against everything — it would rank
        # rather than fail, which is the worst available outcome.
        if not np.isfinite(vectors).all() or float(norms.min()) <= 1e-6:
            raise LocalEmbedderUnavailable("model produced a zero or non-finite embedding")
        return vectors / norms

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        self._acquire_query()
        try:
            return self._encode([QUERY_PREFIX + t for t in texts])
        finally:
            self._release()

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        """Embed corpus text, returning vectors in the caller's order.

        Batches pad to their longest member, so inputs are length-sorted into
        small microbatches and then restored to caller order. The lock is
        released between microbatches so interactive queries can interleave
        with corpus projection.
        """

        if not texts:
            return np.zeros((0, self._dims), dtype="float32")
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        restored = np.empty((len(texts), self._dims), dtype="float32")
        microbatch = max(1, EMBED_DOCUMENT_MICROBATCH)
        for start in range(0, len(order), microbatch):
            indices = order[start : start + microbatch]
            # Release between microbatches so an interactive query never waits
            # behind the projector's entire maximum batch.
            self._acquire_document_batch()
            try:
                packed = self._encode([DOCUMENT_PREFIX + texts[index] for index in indices])
            finally:
                self._release()
            restored[indices] = packed
        return restored


_embedder: LocalEmbedder | None = None


def get_local_embedder() -> LocalEmbedder:
    if _embedder is None or not _embedder.ready:
        raise LocalEmbedderUnavailable("local embedder has not been initialized")
    return _embedder


def initialize_local_embedder(config: "EmbeddingSpaceConfig", model_dir: str | Path) -> LocalEmbedder:
    global _embedder
    embedder = LocalEmbedder(model_dir, dims=config.dims)
    embedder.load()
    _embedder = embedder
    return embedder


async def embed_query(text: str) -> np.ndarray:
    """Embed one query off the event loop.

    The forward pass is CPU-bound; running it inline would block every other
    request on this worker for the duration.
    """

    embedder = get_local_embedder()
    return (await asyncio.to_thread(embedder.embed_queries, [text]))[0]
