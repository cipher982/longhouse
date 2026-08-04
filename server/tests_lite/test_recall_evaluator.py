from __future__ import annotations

import importlib.util
import io
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest


def _coverage() -> dict[str, object]:
    return {
        "projector": "embeddings-5090578d9565-256d-p3",
        "complete": True,
        "complete_through_commit_seq": "10",
        "unpublished_sessions": 0,
        "catalog_lag_count": 0,
        "catalog_indexed_through": "10",
        "catalog_oldest_lag_at": None,
        "catalog_oldest_lag_seconds": None,
        "catalog_commit_seq": "10",
        "catalog_observed_at": "2026-08-02T00:00:10+00:00",
        "resident_stale": False,
        "expected_sessions": 1,
        "published_sessions": 1,
        "expected_episodes": 1,
        "current_episodes": 1,
        "invalid_vectors": 0,
        "unnormalized_vectors": 0,
        "unlocatable_episodes": 0,
        "episode_count_mismatches": 0,
        "missing_session_ids": [],
    }


def _module():
    path = Path(__file__).resolve().parents[2] / "eval" / "recall" / "run_eval.py"
    spec = importlib.util.spec_from_file_location("recall_eval", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_default_false_negative_gate_matches_full_corpus_qwen_baseline():
    evaluator = _module()

    assert evaluator.DEFAULT_MAX_FALSE_NEGATIVE_RATE == 36 / 76
    assert evaluator.DEFAULT_MIN_RECALL_AT_5 == 25 / 76


def test_all_error_run_fails_every_quality_signal_and_the_gate():
    evaluator = _module()
    answerable = evaluator.Query("answer", "paraphrase", "where is it", ["gold"])
    absent = evaluator.Query("absent", "absent", "never happened", [])
    report = evaluator.Report(
        strategy="auto",
        results=[
            evaluator.Result(answerable, [], 0.1, error="503"),
            evaluator.Result(absent, [], 0.1, error="503"),
        ],
    )

    assert report.recall_at(5) == 0.0
    assert report.false_negative_rate() == 1.0
    assert report.correct_abstention_rate() == 0.0
    assert report.by_category() == {"absent": (0, 1), "paraphrase": (0, 1)}
    failures = report.gate_failures(max_false_negative_rate=0.671, min_recall_at_5=0.329)
    assert any("errored" in failure for failure in failures)


def test_healthy_run_passes_and_reports_one_embedding_space():
    evaluator = _module()
    query = evaluator.Query("answer", "paraphrase", "where is it", ["gold"])
    report = evaluator.Report(
        strategy="semantic",
        results=[
            evaluator.Result(
                query,
                ["gold-session"],
                0.02,
                lanes=("dense",),
                embedding_model="google/embeddinggemma-300m",
                embedding_dims=256,
                embedding_revision="a" * 40,
                coverage=_coverage(),
                server_commit="c" * 40,
            )
        ],
    )

    assert report.gate_failures(max_false_negative_rate=0.671, min_recall_at_5=0.329) == []
    assert report.embedding_metadata() == {
        "consistent": True,
        "model": "google/embeddinggemma-300m",
        "dims": 256,
        "revision": "a" * 40,
    }
    assert report.corpus_coverage_metadata()["resident_defects"] == {
        "invalid_vectors": {"min": 0, "max": 0},
        "unnormalized_vectors": {"min": 0, "max": 0},
        "unlocatable_episodes": {"min": 0, "max": 0},
        "episode_count_mismatches": {"min": 0, "max": 0},
        "missing_sessions": {"min": 0, "max": 0},
    }


def test_excluded_session_prefixes_remove_full_session_ids():
    evaluator = _module()

    assert evaluator._without_excluded_sessions(
        ["5595c356-f89d-48c1-bba5-9052eaf04d17", "kept-session"],
        {"5595c356"},
    ) == ["kept-session"]


def test_coverage_metadata_reports_observed_defects_instead_of_inventing_zeroes():
    evaluator = _module()
    coverage = _coverage()
    coverage["invalid_vectors"] = 2
    report = evaluator.Report(
        strategy="semantic",
        results=[evaluator.Result(evaluator.Query("q", "exact", "q", ["gold"]), [], 0.1, coverage=coverage)],
    )

    metadata = report.corpus_coverage_metadata()
    assert metadata["status"] == "incomplete"
    assert metadata["resident_defects"]["invalid_vectors"] == {"min": 2, "max": 2}


def test_coverage_metadata_distinguishes_bounded_live_head_from_current():
    evaluator = _module()
    coverage = _coverage()
    coverage.update(
        {
            "catalog_lag_count": 1,
            "catalog_indexed_through": "9",
            "catalog_oldest_lag_at": "2026-08-02T00:00:09+00:00",
            "catalog_oldest_lag_seconds": 1.0,
            "resident_stale": True,
        }
    )
    report = evaluator.Report(
        strategy="semantic",
        results=[evaluator.Result(evaluator.Query("q", "exact", "q", ["gold"]), ["gold"], 0.1, coverage=coverage)],
    )

    assert report.corpus_coverage_metadata()["status"] == "bounded_head"


def test_category_regression_fails_the_release_gate_at_25():
    evaluator = _module()
    query = evaluator.Query("answer", "causal", "why", ["gold"])
    report = evaluator.Report(
        strategy="lexical",
        results=[
            evaluator.Result(
                query,
                [*[f"other-{i}" for i in range(24)], "gold-session"],
                0.02,
                server_commit="c" * 40,
            )
        ],
    )

    assert report.by_category(5) == {"causal": (0, 1)}
    assert report.by_category(25) == {"causal": (1, 1)}
    assert report.gate_failures(
        max_false_negative_rate=1.0,
        min_recall_at_5=0.0,
        min_category_hits_at_25={"causal": 2},
    ) == ["causal_hits_at_25 1/1 is below 2"]


def test_frozen_qwen_counts_pass_their_exact_release_boundaries():
    module = _module()
    queries = [
        module.Query(id=str(index), category="exact", query=f"q{index}", gold_sessions=[f"gold-{index}"])
        for index in range(76)
    ]
    report = module.Report(
        strategy="lexical",
        results=[
            module.Result(
                query=query,
                returned=(
                    [query.gold_sessions[0]]
                    if index < 25
                    else ([f"miss-{index}-{rank}" for rank in range(24)] + [query.gold_sessions[0]] if index < 40 else [])
                ),
                latency_s=0.01,
                server_commit="a" * 40,
            )
            for index, query in enumerate(queries)
        ],
    )

    assert report.recall_at(5) == 25 / 76
    assert report.false_negative_rate() == 36 / 76
    assert report.gate_failures(
        max_false_negative_rate=module.DEFAULT_MAX_FALSE_NEGATIVE_RATE,
        min_recall_at_5=module.DEFAULT_MIN_RECALL_AT_5,
    ) == []


def test_http_error_preserves_typed_response_detail():
    evaluator = _module()
    error = urllib.error.HTTPError(
        "https://example.test/api/agents/recall",
        503,
        "Service Unavailable",
        hdrs=None,
        fp=io.BytesIO(b'{"detail":{"code":"catalog_unavailable"}}'),
    )

    with (
        patch.object(evaluator.urllib.request, "urlopen", side_effect=error),
        pytest.raises(ValueError, match="catalog_unavailable"),
    ):
        evaluator.search_recall(
            "query",
            base_url="https://example.test",
            token="test-token",
            limit=25,
            days=365,
            mode="lexical",
            expected_sha="a" * 40,
        )
