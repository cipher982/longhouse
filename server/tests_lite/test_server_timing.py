from __future__ import annotations

import pytest
from fastapi import Response

from zerg.metrics import product_read_stage_seconds
from zerg.utils.server_timing import ServerTimingRecorder


def _histogram_sample_value(metric, suffix: str) -> float:
    return next(sample.value for family in metric.collect() for sample in family.samples if sample.name.endswith(suffix))


def test_server_timing_preserves_header_and_retains_stage_metric():
    metric = product_read_stage_seconds.labels("session_detail", "render_object_read")
    before_count = _histogram_sample_value(metric, "_count")
    before_sum = _histogram_sample_value(metric, "_sum")
    response = Response()
    timing = ServerTimingRecorder(surface="session_detail")

    timing.record("render object/read", 12.5)
    timing.apply(response)

    assert response.headers["Server-Timing"] == "render_object_read;dur=12.5"
    assert _histogram_sample_value(metric, "_count") == before_count + 1
    assert _histogram_sample_value(metric, "_sum") == pytest.approx(before_sum + 0.0125)


def test_server_timing_does_not_floor_retained_fast_stage():
    metric = product_read_stage_seconds.labels("timeline", "catalog_list")
    before_sum = _histogram_sample_value(metric, "_sum")
    response = Response()
    timing = ServerTimingRecorder(surface="timeline")

    timing.record("catalog_list", 0.01)
    timing.apply(response)

    assert response.headers["Server-Timing"] == "catalog_list;dur=0.1"
    assert _histogram_sample_value(metric, "_sum") == pytest.approx(before_sum + 0.00001)


def test_server_timing_rejects_unbounded_surface_labels():
    with pytest.raises(ValueError, match="Unsupported product read surface"):
        ServerTimingRecorder(surface="private-session-id")
