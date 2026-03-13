from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent.parent / "scripts"))

from smoke_models import classify_smoke_exception  # noqa: E402


def test_classify_smoke_exception_skips_explicit_rate_limits():
    status, detail = classify_smoke_exception(
        RuntimeError("Error code: 429 - {'error': {'code': '1302', 'message': 'Rate limit reached for requests'}}")
    )

    assert status == "skipped"
    assert "rate limited" in detail


def test_classify_smoke_exception_keeps_real_failures_red():
    status, detail = classify_smoke_exception(RuntimeError("Connection reset by peer"))

    assert status == "fail"
    assert detail == "Connection reset by peer"
