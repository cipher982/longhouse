"""The resident vector index: one matrix, owned by the daemon, read by every worker.

The old SQL query path rebuilt this on every request — SELECT every
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

Filters are applied **before** top-k, not after. The retired SQL query
scopes by owner, project, provider, environment and recency through
`session_index` (``store.py:919-939``); selecting a global top-k and filtering it
afterwards returns a different, silently smaller answer.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from dataclasses import replace

import numpy as np


@dataclass(frozen=True)
class _Snapshot:
    """One consistent view. Never mutated after publication."""

    vectors: np.ndarray  # (N, D) float32, L2-normalized
    session_ids: np.ndarray  # (N,) object
    episode_ordinals: np.ndarray  # (N,) int64
    generation_ids: np.ndarray  # (N,) object
    revisions: np.ndarray  # (N,) int64
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


@dataclass(frozen=True)
class EmbeddingCoverage:
    ready: bool
    expected_sessions: int
    published_sessions: int
    expected_episodes: int
    current_episodes: int
    invalid_vectors: int
    unnormalized_vectors: int
    unlocatable_episodes: int
    episode_count_mismatches: int
    missing_session_ids: tuple[str, ...]
    stale: bool = False

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ready": self.ready,
            "expected_sessions": self.expected_sessions,
            "published_sessions": self.published_sessions,
            "expected_episodes": self.expected_episodes,
            "current_episodes": self.current_episodes,
            "invalid_vectors": self.invalid_vectors,
            "unnormalized_vectors": self.unnormalized_vectors,
            "unlocatable_episodes": self.unlocatable_episodes,
            "episode_count_mismatches": self.episode_count_mismatches,
            "missing_session_ids": list(self.missing_session_ids),
        }
        # Successful dense responses keep their exact strict contract. This
        # marker appears only while a committed mutation has invalidated the
        # last fully validated snapshot, so progress counters cannot be
        # mistaken for current database truth during a deferred backfill.
        if self.stale:
            payload["stale"] = True
        return payload


_EMPTY = _Snapshot(
    vectors=np.zeros((0, 0), dtype="float32"),
    **{
        name: np.array([], dtype=dtype)
        for name, dtype in (
            ("session_ids", object),
            ("episode_ordinals", "int64"),
            ("generation_ids", object),
            ("revisions", "int64"),
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


class ResidentEpisodeIndex:
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
        self._coverage = EmbeddingCoverage(False, 0, 0, 0, 0, 0, 0, 0, 0, ())
        self._blocking_session_ids: frozenset[str] = frozenset()
        self._nonrelational_blocking_session_ids: frozenset[str] = frozenset()
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

    @property
    def coverage(self) -> EmbeddingCoverage:
        return self._coverage

    @property
    def blocking_session_ids(self) -> frozenset[str]:
        """Sessions that prevented the last full coverage validation."""

        return self._blocking_session_ids

    @property
    def nonrelational_blocking_session_ids(self) -> frozenset[str]:
        """Blockers the relational completeness precheck cannot detect.

        Blob shape, locators, counts, ordinals, and publications are visible to
        SQLite. Floating-point finiteness and normalization require decoding
        the matrix, so only those failures may safely suppress a rebuild after
        the relational precheck reports a complete candidate.
        """

        return self._nonrelational_blocking_session_ids

    @property
    def model(self) -> str:
        return self._model

    @property
    def dims(self) -> int:
        return self._dims

    def invalidate(self) -> None:
        """Close the coverage gate while a committed mutation awaits refresh.

        Keep the prior immutable matrix alive for readers that already hold a
        reference, but do not let a new semantic query serve it as current.
        The next successful ``load`` atomically replaces both the matrix and
        its coverage proof.
        """

        with self._write_lock:
            self._coverage = replace(self._coverage, ready=False, stale=True)

    def load(self, connection) -> None:
        """Build the snapshot from SQLite. Called on the writer thread."""

        publications = connection.execute(
            """
            SELECT s.session_id, p.expected_episode_count
            FROM session_index s
            LEFT JOIN embedding_publications p
              ON p.session_id = s.session_id
             AND p.model = ?
             AND p.dims = ?
             AND p.generation_id = s.generation_id
             AND p.revision = s.indexed_through
            ORDER BY s.session_id ASC
            """,
            (self._model, self._dims),
        ).fetchall()
        rows = connection.execute(
            """
            SELECT e.session_id, e.episode_ordinal, e.generation_id, e.revision, e.embedding,
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
             AND s.indexed_through = e.revision
            WHERE e.model = ? AND e.dims = ?
            ORDER BY e.session_id ASC, e.episode_ordinal ASC
            """,
            (self._model, self._dims),
        ).fetchall()
        valid_rows, coverage, blocking_session_ids, nonrelational_blocking_session_ids = self._validate_coverage(publications, rows)
        with self._write_lock:
            self._snapshot = self._build(valid_rows)
            self._coverage = coverage
            self._blocking_session_ids = blocking_session_ids
            self._nonrelational_blocking_session_ids = nonrelational_blocking_session_ids
            self._loaded = True

    def _validate_coverage(self, publications, rows) -> tuple[list, EmbeddingCoverage, frozenset[str], frozenset[str]]:
        expected_by_session = {
            str(row["session_id"]): int(row["expected_episode_count"]) for row in publications if row["expected_episode_count"] is not None
        }
        missing_sessions = tuple(str(row["session_id"]) for row in publications if row["expected_episode_count"] is None)
        ordinals_by_session: dict[str, set[int]] = {}
        valid_rows = []
        invalid_vectors = 0
        unnormalized_vectors = 0
        unlocatable_episodes = 0
        blocking_session_ids = set(missing_sessions)
        nonrelational_blocking_session_ids: set[str] = set()
        for row in rows:
            session_id = str(row["session_id"])
            ordinals_by_session.setdefault(session_id, set()).add(int(row["episode_ordinal"]))
            payload = row["embedding"]
            if not isinstance(payload, bytes) or len(payload) != self._dims * 4:
                invalid_vectors += 1
                blocking_session_ids.add(session_id)
                continue
            vector = np.frombuffer(payload, dtype="float32", count=self._dims)
            if not np.isfinite(vector).all() or float(np.linalg.norm(vector)) <= 1e-6:
                invalid_vectors += 1
                blocking_session_ids.add(session_id)
                nonrelational_blocking_session_ids.add(session_id)
                continue
            if not np.isclose(float(np.linalg.norm(vector)), 1.0, rtol=1e-4, atol=1e-4):
                unnormalized_vectors += 1
                blocking_session_ids.add(session_id)
                nonrelational_blocking_session_ids.add(session_id)
                continue
            if row["start_order_time_us"] is None:
                unlocatable_episodes += 1
                blocking_session_ids.add(session_id)
                continue
            valid_rows.append(row)

        count_mismatches = 0
        for session_id, expected_count in expected_by_session.items():
            if ordinals_by_session.get(session_id, set()) != set(range(expected_count)):
                count_mismatches += 1
                blocking_session_ids.add(session_id)
        expected_episodes = sum(expected_by_session.values())
        current_episodes = len(rows)
        ready = (
            len(publications) == len(expected_by_session)
            and current_episodes == expected_episodes
            and invalid_vectors == 0
            and unnormalized_vectors == 0
            and unlocatable_episodes == 0
            and count_mismatches == 0
        )
        return (
            valid_rows,
            EmbeddingCoverage(
                ready=ready,
                expected_sessions=len(publications),
                published_sessions=len(expected_by_session),
                expected_episodes=expected_episodes,
                current_episodes=current_episodes,
                invalid_vectors=invalid_vectors,
                unnormalized_vectors=unnormalized_vectors,
                unlocatable_episodes=unlocatable_episodes,
                episode_count_mismatches=count_mismatches,
                missing_session_ids=missing_sessions[:20],
            ),
            frozenset(blocking_session_ids),
            frozenset(nonrelational_blocking_session_ids),
        )

    def _build(self, rows) -> _Snapshot:
        count = len(rows)
        if count == 0:
            return _EMPTY
        vectors = np.empty((count, self._dims), dtype="float32")
        for index, row in enumerate(rows):
            vectors[index] = np.frombuffer(row["embedding"], dtype="float32", count=self._dims)
        # Coverage validation proved these are finite unit vectors. Do not
        # normalize malformed stored data here: repairing it during load would
        # hide a broken projector behind plausible scores.

        def column(key, dtype, missing=None):
            return np.array([(row[key] if row[key] is not None else missing) for row in rows], dtype=dtype)

        return _Snapshot(
            vectors=vectors,
            session_ids=column("session_id", object),
            episode_ordinals=column("episode_ordinal", "int64", -1),
            generation_ids=column("generation_id", object),
            revisions=column("revision", "int64", -1),
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
        environment: str | None = None,
        exclude_environments: list[str] | None = None,
        since_iso: str | None = None,
    ) -> list[dict[str, object]]:
        snapshot = self._snapshot  # one atomic read; immutable thereafter
        if not self._loaded:
            raise RuntimeError("resident episode index is not loaded")
        if snapshot.size == 0:
            return []

        keep = snapshot.owner_ids == owner_id
        if project:
            keep &= snapshot.projects == project
        if provider:
            keep &= snapshot.providers == provider
        if environment:
            keep &= snapshot.environments == environment
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

        vector = np.asarray(query, dtype="float32").reshape(-1)
        if vector.shape != (self._dims,) or not np.isfinite(vector).all():
            raise ValueError(f"query vector must contain exactly {self._dims} finite dimensions")
        norm = float(np.linalg.norm(vector))
        if norm <= 1e-6:
            raise ValueError("query vector must be nonzero")
        vector = vector / norm
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
