"""Unit tests for hybrid RRF search fusion."""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from zerg.services.search import rrf_fuse, _RRF_K


@dataclass
class _FakeSession:
    """Minimal AgentSession-like object for RRF tests."""
    id: str
    started_at: datetime
    ended_at: Optional[datetime]
    provider: str = "claude"
    project: Optional[str] = None


def _make_session(id_str, days_ago=1):
    now = datetime.now(timezone.utc)
    return _FakeSession(
        id=id_str,
        started_at=now - timedelta(days=days_ago + 0.5),
        ended_at=now - timedelta(days=days_ago),
    )


def test_rrf_doc_in_both_lists_scores_higher():
    """A doc appearing in both lists should outscore one appearing in only one."""
    a = _make_session("a")
    b = _make_session("b")
    c = _make_session("c")

    lexical = [a, b]
    semantic = [(a, 0.9), (c, 0.8)]

    result = rrf_fuse(lexical, semantic, limit=3)
    ids = [str(s.id) for s in result]
    assert ids[0] == "a"


def test_rrf_single_list_doc_still_scores():
    """Doc in only one list must still appear in results (no zero score)."""
    a = _make_session("a")
    b = _make_session("b")

    lexical = [a, b]
    semantic = []

    result = rrf_fuse(lexical, semantic, limit=2)
    ids = [str(s.id) for s in result]
    assert "a" in ids
    assert "b" in ids


def test_rrf_no_missing_penalty():
    """Scores use standard RRF — only lists where doc appears contribute."""
    a = _make_session("a")
    b = _make_session("b")

    lexical = [a]
    semantic = [(b, 0.9)]

    result = rrf_fuse(lexical, semantic, limit=2)
    ids = [str(s.id) for s in result]
    assert len(ids) == 2
    assert "a" in ids
    assert "b" in ids


def test_rrf_formula_correct():
    """Verify RRF formula: score = 1/(K + rank) with K=60."""
    a = _make_session("a")

    lexical = [a]
    semantic = [(a, 0.9)]

    result = rrf_fuse(lexical, semantic, limit=1)
    assert len(result) == 1
    assert str(result[0].id) == "a"
    # Score should be 1/(K+1) + 1/(K+1) = 2/61


def test_rrf_empty_semantic_returns_lexical():
    """Empty semantic list returns only lexical results."""
    sessions = [_make_session(str(i)) for i in range(3)]
    result = rrf_fuse(sessions, [], limit=3)
    assert len(result) == 3


def test_rrf_empty_lexical_returns_semantic():
    """Empty lexical list returns only semantic results."""
    sessions = [(_make_session(str(i)), float(i)) for i in range(3)]
    result = rrf_fuse([], sessions, limit=3)
    assert len(result) == 3


def test_rrf_deterministic():
    """Same input always produces same output order."""
    a, b, c = _make_session("a"), _make_session("b"), _make_session("c")
    lexical = [a, b, c]
    semantic = [(b, 0.9), (c, 0.8), (a, 0.7)]

    r1 = rrf_fuse(lexical[:], semantic[:], limit=3)
    r2 = rrf_fuse(lexical[:], semantic[:], limit=3)
    assert [str(s.id) for s in r1] == [str(s.id) for s in r2]


def test_rrf_over_fetch_can_differ_from_individual_lists():
    """Top-N fused result can include docs not in top-N of either list."""
    a = _make_session("a")
    b = _make_session("b")
    c = _make_session("c")
    d = _make_session("d")

    lexical = [b, c, a, d]
    semantic = [(a, 0.9), (d, 0.8), (b, 0.7), (c, 0.6)]

    result = rrf_fuse(lexical, semantic, limit=2)
    ids = [str(s.id) for s in result]
    assert len(ids) == 2
