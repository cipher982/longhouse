"""The resident vector index: one matrix, owned by the daemon, read by every worker.

`query_episode_embeddings` used to rebuild this on every request — SELECT every
matching row, `np.frombuffer` each blob, `np.vstack` 85 MB — measured at
0.57-0.66s per query against a memory-bandwidth floor near 10ms. The vectors
change a few times a minute and were being reconstructed thousands of times.

Two things about searchd's shape decide this design:

- It runs **one writer store and a pool of independent read workers**, each with
  its own SQLite connection and its own ``SearchStore``
  (``searchd/server.py:87-97``, ``305-311``). So the index cannot hang off a
  store: it would be duplicated per reader and no reader would ever observe a
  write. It is owned by the daemon and shared.
- Writes are UPSERTs with ordinal pruning, and sessions get deleted outright.
  It is not append-only, so slots are reused and removals must actually remove.

Readers never take a lock. The writer publishes an immutable snapshot and swaps
one reference; a reader that grabbed the previous snapshot keeps scanning a
consistent matrix rather than one being mutated underneath it. Torn reads of a
single row would be silent and produce a plausible wrong score, which is the
failure mode this whole subsystem keeps having.

Filters are applied **before** top-k, not after. `query_episode_embeddings`
scopes by owner, project, provider, environment and recency through
`session_index` (``store.py:919-939``); selecting a global top-k and filtering it
afterwards returns a different, silently smaller answer.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class _Snapshot:
    """One consistent view. Never mutated after publication."""

    vectors: np.ndarray  # (N, D) float32, L2-normalized
    session_ids: np.ndarray  # (N,) object
    episode_ordinals: np.ndarray  # (N,) int64
    generation_ids: np.ndarray  # (N,) object
    start_order_times: np.ndarray  # (N,) int64, -1 when absent
    event_index_starts: np.ndarray  # (N,) int64, -1 when absent
    event_index_ends: np.ndarray  # (N,) int64, -1 when absent
    owner_ids: np.ndarray  # (N,) object
    projects: np.ndarray  # (N,) object
    providers: np.ndarray  # (N,) object
    environments: np.ndarray  # (N,) object
    started_ats: np.ndarray  # (N,) object

    @property
    def size(self) -> int:
        return int(self.vectors.shape[0])


_EMPTY = _Snapshot(
    vectors=np.zeros((0, 0), dtype="float32"),
    **{
        name: np.array([], dtype=dtype)
        for name, dtype in (
            ("session_ids", object),
            ("episode_ordinals", "int64"),
            ("generation_ids", object),
            ("start_order_times", "int64"),
            ("event_index_starts", "int64"),
            ("event_index_ends", "int64"),
            ("owner_ids", object),
            ("projects", object),
            ("providers", object),
            ("environments", object),
            ("started_ats", object),
        )
    },
)


class DenseIndex:
    """Resident vectors for one embedding space, rebuilt from SQLite on demand.

    SQLite remains the store of record. This is a derived cache that can always
    be discarded and rebuilt, which is also the crash-recovery story: if the
    process dies between the SQLite commit and the in-memory update, the next
    load reads the committed truth.
    """

    def __init__(self, *, model: str, dims: int) -> None:
        self._model = model
        self._dims = dims
        self._snapshot = _EMPTY
        self._loaded = False
        self._write_lock = threading.Lock()

    @property
    def ready(self) -> bool:
        """False until the first load completes.

        searchd must not report itself ready while this is False, or a semantic
        query observes an empty index and returns an empty success — which reads
        exactly like an honest miss.
        """

        return self._loaded

    @property
    def size(self) -> int:
        return self._snapshot.size

    def load(self, connection) -> None:
        """Build the snapshot from SQLite. Called on the writer thread."""

        rows = connection.execute(
            """
            SELECT e.session_id, e.episode_ordinal, e.generation_id, e.embedding,
                   e.start_order_time_us, e.event_index_start, e.event_index_end,
                   e.owner_id, s.project, s.provider, s.environment, s.started_at
            FROM episode_embeddings e
            JOIN session_index s
              ON s.session_id = e.session_id
             -- Fenced on the published generation. Joining by session alone
             -- lets a vector from a superseded generation occupy top-k and then
             -- fail to hydrate, which hides the current generation's vector
             -- behind a hit that cannot produce evidence.
             AND s.generation_id = e.generation_id
            WHERE e.model = ? AND e.dims = ?
            ORDER BY e.session_id ASC, e.episode_ordinal ASC
            """,
            (self._model, self._dims),
        ).fetchall()
        with self._write_lock:
            self._snapshot = self._build(rows)
            self._loaded = True

    def _build(self, rows) -> _Snapshot:
        count = len(rows)
        if count == 0:
            return _EMPTY
        vectors = np.empty((count, self._dims), dtype="float32")
        for index, row in enumerate(rows):
            vectors[index] = np.frombuffer(row["embedding"], dtype="float32", count=self._dims)
        # Normalize once here rather than per query: cosine over unit vectors is
        # a plain inner product, which is the whole point of keeping this
        # resident.
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        np.divide(vectors, np.clip(norms, 1e-9, None), out=vectors)

        def column(key, dtype, missing=None):
            return np.array([(row[key] if row[key] is not None else missing) for row in rows], dtype=dtype)

        return _Snapshot(
            vectors=vectors,
            session_ids=column("session_id", object),
            episode_ordinals=column("episode_ordinal", "int64", -1),
            generation_ids=column("generation_id", object),
            start_order_times=column("start_order_time_us", "int64", -1),
            event_index_starts=column("event_index_start", "int64", -1),
            event_index_ends=column("event_index_end", "int64", -1),
            owner_ids=column("owner_id", object),
            projects=column("project", object, ""),
            providers=column("provider", object, ""),
            environments=column("environment", object, ""),
            started_ats=column("started_at", object, ""),
        )

    def search(
        self,
        query: np.ndarray,
        *,
        owner_id: str,
        limit: int,
        project: str | None = None,
        provider: str | None = None,
        exclude_environments: list[str] | None = None,
        since_iso: str | None = None,
    ) -> list[dict[str, object]]:
        snapshot = self._snapshot  # one atomic read; immutable thereafter
        if snapshot.size == 0:
            return []

        keep = snapshot.owner_ids == owner_id
        if project:
            keep &= snapshot.projects == project
        if provider:
            keep &= snapshot.providers == provider
        if exclude_environments:
            for environment in exclude_environments:
                keep &= snapshot.environments != environment
        if since_iso:
            # started_at is an ISO-8601 string in a fixed format, so lexical
            # comparison is chronological and avoids parsing 83k timestamps.
            keep &= snapshot.started_ats >= since_iso

        candidates = np.flatnonzero(keep)
        if candidates.size == 0:
            return []

        vector = np.asarray(query, dtype="float32").reshape(-1)[: self._dims]
        vector = vector / max(float(np.linalg.norm(vector)), 1e-9)
        scores = snapshot.vectors[candidates] @ vector

        take = min(limit, candidates.size)
        # argpartition is O(n); a full sort of 83k scores to take 30 is waste.
        top = np.argpartition(-scores, take - 1)[:take] if take < scores.size else np.arange(scores.size)
        top = top[np.argsort(-scores[top])]

        results = []
        for position in top:
            index = candidates[position]
            start = int(snapshot.start_order_times[index])
            results.append(
                {
                    "session_id": str(snapshot.session_ids[index]),
                    "episode_ordinal": int(snapshot.episode_ordinals[index]),
                    "score": float(scores[position]),
                    "event_index_start": int(snapshot.event_index_starts[index]),
                    "event_index_end": int(snapshot.event_index_ends[index]),
                    "generation_id": str(snapshot.generation_ids[index]),
                    "start_order_time_us": None if start < 0 else start,
                }
            )
        return results
