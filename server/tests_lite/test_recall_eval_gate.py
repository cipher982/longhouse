from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlparse

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "eval" / "recall" / "run_eval.py"
_SPEC = importlib.util.spec_from_file_location("longhouse_recall_eval_for_tests", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
recall_eval = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = recall_eval
_SPEC.loader.exec_module(recall_eval)


def _coverage(*, lagging_sessions: int = 0, unpublished_sessions: int = 0) -> dict[str, object]:
    return {
        "complete": lagging_sessions == 0,
        "lagging_sessions": lagging_sessions,
        "unpublished_sessions": unpublished_sessions,
        "oldest_lag_seconds": 1.0 if lagging_sessions else None,
    }


def _payload(*, coverage: dict[str, object] | None) -> dict[str, object]:
    return {
        "results": [],
        "total": 0,
        "lanes": ["dense"],
        "degraded": [],
        "coverage": coverage,
    }


class _HTTPBody(io.BytesIO):
    headers = {
        "X-Longhouse-Commit": "c" * 40,
        "X-Recall-Embedding-Model": "google/embeddinggemma-300m",
        "X-Recall-Embedding-Dims": "256",
        "X-Recall-Embedding-Revision": "a" * 40,
        "X-Recall-Projector": "embeddings-a-256d-p3",
    }


def test_live_eval_request_requires_complete_coverage_and_real_result_depth(monkeypatch):
    seen = {}

    def urlopen(request, *, timeout):
        seen["params"] = parse_qs(urlparse(request.full_url).query)
        seen["timeout"] = timeout
        return _HTTPBody(json.dumps(_payload(coverage=_coverage())).encode())

    monkeypatch.setattr(recall_eval.urllib.request, "urlopen", urlopen)

    payload = recall_eval.search_recall(
        "why did we change it?",
        base_url="https://example.test",
        token="device-token",
        limit=10,
        days=365,
        mode="semantic",
        expected_sha="c" * 40,
    )

    assert payload["coverage"]["complete"] is True
    assert payload["_diagnostics"]["projector"] == "embeddings-a-256d-p3"
    assert seen["params"]["max_results"] == ["10"]
    assert "context_turns" not in seen["params"]
    assert seen["params"]["mode"] == ["semantic"]
    assert seen["timeout"] == 30

    malformed = _coverage(lagging_sessions=101)
    monkeypatch.setattr(
        recall_eval.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _HTTPBody(json.dumps(_payload(coverage=malformed)).encode()),
    )
    with pytest.raises(ValueError, match="coverage is incomplete"):
        recall_eval.search_recall(
            "why did we change it?",
            base_url="https://example.test",
            token="device-token",
            limit=10,
            days=365,
            mode="semantic",
            expected_sha="c" * 40,
        )

    with pytest.raises(ValueError, match="serving commit mismatch"):
        recall_eval.search_recall(
            "why did we change it?",
            base_url="https://example.test",
            token="device-token",
            limit=10,
            days=365,
            mode="semantic",
            expected_sha="d" * 40,
        )


def test_report_records_corpus_range_and_fails_errors_or_mixed_spaces():
    query = recall_eval.Query("q1", "paraphrase", "what happened?", ["gold"])
    report = recall_eval.Report(
        strategy="semantic",
        results=[
            recall_eval.Result(
                query=query,
                returned=["gold-session"],
                latency_s=0.1,
                lanes=("dense",),
                embedding_model="google/embeddinggemma-300m",
                embedding_dims=256,
                embedding_revision="a" * 40,
                projector="embeddings-a-256d-p3",
                coverage=_coverage(),
                server_commit="c" * 40,
            ),
            recall_eval.Result(
                query=query,
                returned=[],
                latency_s=0.2,
                error="typed 503",
            ),
            recall_eval.Result(
                query=query,
                returned=["gold-session"],
                latency_s=0.1,
                lanes=("dense",),
                embedding_model="different-space",
                embedding_dims=256,
                embedding_revision="b" * 40,
                projector="embeddings-b-256d-p3",
                coverage=_coverage(),
                server_commit="c" * 40,
            ),
        ],
    )

    metadata = report.corpus_coverage_metadata()
    assert metadata["status"] == "current"
    assert metadata["lagging_sessions"] == {"min": 0, "max": 0}
    assert metadata["unpublished_sessions"] == {"min": 0, "max": 0}
    assert report.false_negative_rate() == pytest.approx(1 / 3)

    failures = report.gate_failures(
        max_false_negative_rate=1.0,
        min_recall_at_5=0.0,
        min_category_hits_at_10={},
    )
    assert "1 query(s) errored" in failures
    assert "evaluation observed 2 embedding spaces instead of one" in failures
