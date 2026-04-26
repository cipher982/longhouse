"""Unit tests for the sla_watch histogram parser + percentile calc.

The scraper reads raw /metrics text and computes percentiles. If the
bucket format changes or the filter logic regresses, we want a fast
failure rather than silent "no breach" outputs.
"""

import importlib.util
from pathlib import Path


def _load_sla_watch():
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "scripts" / "canary" / "sla_watch.py"
    spec = importlib.util.spec_from_file_location("sla_watch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SAMPLE_METRICS = """
# HELP canary_latency_seconds Synthetic canary hop latency (seconds).
# TYPE canary_latency_seconds histogram
canary_latency_seconds_bucket{hop="sse",surface="observer",le="0.01"} 0.0
canary_latency_seconds_bucket{hop="sse",surface="observer",le="0.05"} 0.0
canary_latency_seconds_bucket{hop="sse",surface="observer",le="0.1"} 5.0
canary_latency_seconds_bucket{hop="sse",surface="observer",le="0.25"} 18.0
canary_latency_seconds_bucket{hop="sse",surface="observer",le="0.5"} 20.0
canary_latency_seconds_bucket{hop="sse",surface="observer",le="1.0"} 20.0
canary_latency_seconds_bucket{hop="sse",surface="observer",le="+Inf"} 20.0
canary_latency_seconds_count{hop="sse",surface="observer"} 20.0
canary_latency_seconds_sum{hop="sse",surface="observer"} 3.2
canary_latency_seconds_bucket{hop="ingest",surface="producer",le="0.1"} 10.0
canary_latency_seconds_bucket{hop="ingest",surface="producer",le="+Inf"} 10.0
canary_latency_seconds_count{hop="ingest",surface="producer"} 10.0
"""


def test_parse_histogram_filters_by_label():
    sla = _load_sla_watch()
    buckets = sla.parse_histogram_buckets(SAMPLE_METRICS, "canary_latency_seconds", {"hop": "sse"})
    # 7 buckets (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, +Inf)
    assert len(buckets) == 7
    # Sorted ascending by le
    les = [b[0] for b in buckets]
    assert les == sorted(les)


def test_percentile_from_histogram_p50():
    sla = _load_sla_watch()
    buckets = sla.parse_histogram_buckets(SAMPLE_METRICS, "canary_latency_seconds", {"hop": "sse"})
    # total=20; p50 target=10; first bucket with count>=10 is 0.25
    p50 = sla.percentile_from_histogram(buckets, 0.5)
    assert p50 == 0.25


def test_percentile_from_histogram_p95():
    sla = _load_sla_watch()
    buckets = sla.parse_histogram_buckets(SAMPLE_METRICS, "canary_latency_seconds", {"hop": "sse"})
    # p95 target = 19; first bucket with count>=19 is 0.5
    p95 = sla.percentile_from_histogram(buckets, 0.95)
    assert p95 == 0.5


def test_percentile_from_histogram_empty():
    sla = _load_sla_watch()
    assert sla.percentile_from_histogram([], 0.5) is None
    assert sla.percentile_from_histogram([(0.1, 0.0), (float("inf"), 0.0)], 0.5) is None


def test_parse_histogram_different_filter_returns_different_buckets():
    sla = _load_sla_watch()
    ingest = sla.parse_histogram_buckets(SAMPLE_METRICS, "canary_latency_seconds", {"hop": "ingest"})
    sse = sla.parse_histogram_buckets(SAMPLE_METRICS, "canary_latency_seconds", {"hop": "sse"})
    # Ingest has 2 buckets in fixture, sse has 7.
    assert len(ingest) == 2
    assert len(sse) == 7
    # Counts must not cross-contaminate: ingest's +Inf count is 10, sse's is 20.
    assert ingest[-1][1] == 10.0
    assert sse[-1][1] == 20.0
