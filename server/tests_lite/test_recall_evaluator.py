from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[2] / "eval" / "recall" / "run_eval.py"
    spec = importlib.util.spec_from_file_location("recall_eval", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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
            )
        ],
    )

    assert report.gate_failures(max_false_negative_rate=0.671, min_recall_at_5=0.329) == []
    assert report.embedding_metadata() == {
        "model": "google/embeddinggemma-300m",
        "dims": 256,
        "revision": "a" * 40,
    }


def test_category_regression_fails_the_release_gate_at_25():
    evaluator = _module()
    query = evaluator.Query("answer", "causal", "why", ["gold"])
    report = evaluator.Report(
        strategy="semantic",
        results=[evaluator.Result(query, [*[f"other-{i}" for i in range(24)], "gold-session"], 0.02)],
    )

    assert report.by_category(5) == {"causal": (0, 1)}
    assert report.by_category(25) == {"causal": (1, 1)}
    assert report.gate_failures(
        max_false_negative_rate=1.0,
        min_recall_at_5=0.0,
        min_category_hits_at_25={"causal": 2},
    ) == ["causal_hits_at_25 1/1 is below 2"]
