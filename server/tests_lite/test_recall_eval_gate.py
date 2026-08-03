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


def _coverage(*, commit_seq: str = "10", sessions: int = 5_901, episodes: int = 82_958) -> dict[str, object]:
    return {
        "ready": True,
        "projector": "embeddings-5090578d9565-256d-p2",
        "catalog_lag_count": 0,
        "catalog_indexed_through": commit_seq,
        "catalog_commit_seq": commit_seq,
        "catalog_observed_at": f"2026-08-02T00:00:{int(commit_seq) % 60:02d}+00:00",
        "expected_sessions": sessions,
        "published_sessions": sessions,
        "expected_episodes": episodes,
        "current_episodes": episodes,
        "invalid_vectors": 0,
        "unnormalized_vectors": 0,
        "unlocatable_episodes": 0,
        "episode_count_mismatches": 0,
        "missing_session_ids": [],
    }


def _payload(*, coverage: dict[str, object] | None) -> dict[str, object]:
    return {
        "matches": [],
        "total": 0,
        "lanes": ["dense"],
        "embedding_model": "google/embeddinggemma-300m",
        "embedding_dims": 256,
        "embedding_revision": "a" * 40,
        "coverage": coverage,
        "server_commit": "c" * 40,
    }


def test_live_eval_request_requires_complete_coverage_and_real_result_depth(monkeypatch):
    seen = {}

    def urlopen(request, *, timeout):
        seen["params"] = parse_qs(urlparse(request.full_url).query)
        seen["timeout"] = timeout
        return io.BytesIO(json.dumps(_payload(coverage=_coverage())).encode())

    monkeypatch.setattr(recall_eval.urllib.request, "urlopen", urlopen)

    payload = recall_eval.search_recall(
        "why did we change it?",
        base_url="https://example.test",
        token="device-token",
        limit=25,
        days=365,
        mode="semantic",
        expected_sha="c" * 40,
    )

    assert payload["coverage"]["ready"] is True
    assert seen["params"]["max_results"] == ["25"]
    assert seen["params"]["mode"] == ["semantic"]
    assert seen["timeout"] == 30

    malformed = _coverage()
    malformed["current_episodes"] = 82_957
    monkeypatch.setattr(
        recall_eval.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(json.dumps(_payload(coverage=malformed)).encode()),
    )
    with pytest.raises(ValueError, match="coverage is incomplete"):
        recall_eval.search_recall(
            "why did we change it?",
            base_url="https://example.test",
            token="device-token",
            limit=25,
            days=365,
            mode="semantic",
            expected_sha="c" * 40,
        )

    with pytest.raises(ValueError, match="serving commit mismatch"):
        recall_eval.search_recall(
            "why did we change it?",
            base_url="https://example.test",
            token="device-token",
            limit=25,
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
                coverage=_coverage(commit_seq="10"),
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
                coverage=_coverage(commit_seq="12", episodes=82_960),
                server_commit="c" * 40,
            ),
        ],
    )

    metadata = report.corpus_coverage_metadata()
    assert metadata["status"] == "complete"
    assert metadata["catalog_commit_seq"] == {"min": 10, "max": 12}
    assert metadata["expected_episodes"] == {"min": 82_958, "max": 82_960}
    assert report.false_negative_rate() == pytest.approx(1 / 3)

    failures = report.gate_failures(
        max_false_negative_rate=1.0,
        min_recall_at_5=0.0,
        min_category_hits_at_25={},
    )
    assert "1 query(s) errored" in failures
    assert "evaluation observed 2 embedding spaces instead of one" in failures
