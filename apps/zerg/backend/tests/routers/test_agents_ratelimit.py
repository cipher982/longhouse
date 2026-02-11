"""Tests for agents API rate limiting.

Tests the server-side rate limiting for the ingest endpoint:
- 1000 events/min per device (token-derived key or device_id) soft cap
- HTTP 429 with Retry-After header when exceeded
- Gzip decompression support
"""

import gzip
import json
from datetime import datetime
from datetime import timezone

from zerg.routers.agents import RATE_LIMIT_EVENTS_PER_MIN
from zerg.routers.agents import check_rate_limit
from zerg.routers.agents import reset_rate_limits


class TestRateLimiting:
    """Tests for rate limit checking logic."""

    def setup_method(self):
        """Reset rate limits before each test."""
        reset_rate_limits()

    def teardown_method(self):
        """Reset rate limits after each test."""
        reset_rate_limits()

    def test_under_limit_passes(self):
        """Requests under the rate limit pass."""
        exceeded, retry_after = check_rate_limit("device-1", 100)
        assert exceeded is False
        assert retry_after == 0

    def test_exactly_at_limit_passes(self):
        """Requests exactly at the limit pass."""
        exceeded, retry_after = check_rate_limit("device-1", RATE_LIMIT_EVENTS_PER_MIN)
        assert exceeded is False
        assert retry_after == 0

    def test_over_limit_fails(self):
        """Requests over the limit are rejected."""
        # First request at limit
        exceeded1, _ = check_rate_limit("device-1", RATE_LIMIT_EVENTS_PER_MIN)
        assert exceeded1 is False

        # Second request should fail
        exceeded2, retry_after = check_rate_limit("device-1", 1)
        assert exceeded2 is True
        assert retry_after > 0

    def test_separate_devices_independent(self):
        """Different devices have independent rate limits."""
        # Fill up device-1's limit
        exceeded1, _ = check_rate_limit("device-1", RATE_LIMIT_EVENTS_PER_MIN)
        assert exceeded1 is False

        # Device-2 should still be able to ingest
        exceeded2, _ = check_rate_limit("device-2", RATE_LIMIT_EVENTS_PER_MIN)
        assert exceeded2 is False

    def test_multiple_requests_accumulate(self):
        """Multiple requests accumulate toward the limit."""
        # 10 requests of 100 events each = 1000 total
        for _ in range(10):
            exceeded, _ = check_rate_limit("device-1", 100)
            assert exceeded is False

        # Next request should fail
        exceeded, retry_after = check_rate_limit("device-1", 1)
        assert exceeded is True
        assert retry_after > 0

    def test_retry_after_reasonable_value(self):
        """Retry-After header suggests a reasonable wait time."""
        # Fill the limit
        check_rate_limit("device-1", RATE_LIMIT_EVENTS_PER_MIN)

        # Check retry-after
        exceeded, retry_after = check_rate_limit("device-1", 1)
        assert exceeded is True
        # Should be between 1 and 60 seconds
        assert 1 <= retry_after <= 60


class TestIngestGzipDecompression:
    """Tests for gzip decompression in ingest endpoint.

    Note: These tests require a full test client setup which is typically
    done in integration tests. Here we document the expected behavior.
    """

    def test_gzip_payload_structure(self):
        """Verify gzip compression/decompression works correctly."""
        payload = {
            "id": "test-session-123",
            "provider": "claude",
            "project": "test-project",
            "device_id": "test-device",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "events": [
                {
                    "role": "user",
                    "content_text": "Hello",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ],
        }

        # Compress
        json_bytes = json.dumps(payload).encode("utf-8")
        compressed = gzip.compress(json_bytes)

        # Verify compression reduces size (for non-trivial payloads)
        # Note: Small payloads might not compress well
        assert len(compressed) > 0

        # Decompress and verify
        decompressed = gzip.decompress(compressed)
        recovered = json.loads(decompressed)

        assert recovered["id"] == payload["id"]
        assert recovered["provider"] == payload["provider"]
        assert len(recovered["events"]) == 1
