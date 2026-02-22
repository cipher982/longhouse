"""Unit tests for search ranking modes."""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from zerg.services.search import apply_sort


@dataclass
class _FakeSession:
    """Minimal AgentSession-like object for sorting tests."""
    id: str
    started_at: datetime
    ended_at: Optional[datetime]
    provider: str = "claude"
    project: Optional[str] = None


def _make_session(id_str, days_ago, ended=True):
    now = datetime.now(timezone.utc)
    started = now - timedelta(days=days_ago + 0.5)
    ended_at = now - timedelta(days=days_ago) if ended else None
    return _FakeSession(id=id_str, started_at=started, ended_at=ended_at)


def test_sort_recency_orders_newest_first():
    sessions = [
        _make_session("old", days_ago=10),
        _make_session("mid", days_ago=5),
        _make_session("new", days_ago=1),
    ]
    result = apply_sort(sessions, "recency")
    assert [str(s.id) for s in result] == ["new", "mid", "old"]


def test_sort_relevance_uses_bm25_order():
    sessions = [
        _make_session("a", days_ago=1),
        _make_session("b", days_ago=5),
        _make_session("c", days_ago=10),
    ]
    # BM25 order: b is best match, then c, then a
    bm25_order = ["b", "c", "a"]
    result = apply_sort(sessions, "relevance", bm25_order=bm25_order)
    assert [str(s.id) for s in result] == ["b", "c", "a"]


def test_sort_balanced_blends_rank_and_recency():
    # old_relevant: days_ago=30, bm25_rank=0 → high relevance, low recency
    # new_irrelevant: days_ago=0, bm25_rank=2 → low relevance, high recency
    # mid: days_ago=5, bm25_rank=1 → middle on both
    #
    # Balanced formula: 0.5 * norm_rank + 0.5 * norm_recency
    # old_relevant: 0.5*1.0 + 0.5*exp(-1)   ≈ 0.684
    # mid:          0.5*0.667 + 0.5*exp(-1/6) ≈ 0.757   ← wins (balanced)
    # new_irrelevant: 0.5*0.333 + 0.5*1.0    = 0.667
    # Expected balanced order: mid, old_relevant, new_irrelevant
    sessions = [
        _make_session("old_relevant", days_ago=30),
        _make_session("new_irrelevant", days_ago=0),
        _make_session("mid", days_ago=5),
    ]
    # BM25: old_relevant first, then mid, then new_irrelevant
    bm25_order = ["old_relevant", "mid", "new_irrelevant"]
    result = apply_sort(sessions, "balanced", bm25_order=bm25_order)
    ids = [str(s.id) for s in result]
    assert len(ids) == 3
    # mid should be first: it has the best blend of relevance + recency
    assert ids[0] == "mid"
    # Balanced != pure relevance (pure relevance would put old_relevant first)
    assert ids[0] != "old_relevant"
    # Balanced != pure recency (pure recency would put new_irrelevant first)
    assert ids[0] != "new_irrelevant"


def test_sort_deterministic():
    """Same input always produces same output."""
    sessions = [_make_session(str(i), days_ago=i) for i in range(5)]
    bm25_order = [str(i) for i in range(5)]
    result1 = apply_sort(sessions[:], "balanced", bm25_order=bm25_order)
    result2 = apply_sort(sessions[:], "balanced", bm25_order=bm25_order)
    assert [str(s.id) for s in result1] == [str(s.id) for s in result2]


def test_sort_recency_handles_null_ended_at():
    """Sessions with null ended_at fall back to started_at."""
    sessions = [
        _make_session("finished", days_ago=1, ended=True),
        _make_session("ongoing", days_ago=0, ended=False),
    ]
    result = apply_sort(sessions, "recency")
    # ongoing has more recent started_at → should be first
    assert str(result[0].id) == "ongoing"
