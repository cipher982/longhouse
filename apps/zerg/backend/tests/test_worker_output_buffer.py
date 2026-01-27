"""Tests for live commis output buffer."""

from zerg.services.commis_output_buffer import CommisOutputBuffer


def test_commis_output_buffer_tail_truncates():
    buffer = CommisOutputBuffer(max_bytes=10, ttl_seconds=60)

    buffer.append_output(commis_id="commis-1", stream="stdout", data="12345")
    buffer.append_output(commis_id="commis-1", stream="stdout", data="67890")

    assert buffer.get_tail("commis-1") == "1234567890"

    buffer.append_output(commis_id="commis-1", stream="stdout", data="abc")

    # Tail should keep last 10 chars
    assert buffer.get_tail("commis-1") == "4567890abc"


def test_commis_output_buffer_stderr_prefix():
    buffer = CommisOutputBuffer(max_bytes=50, ttl_seconds=60)

    buffer.append_output(commis_id="commis-2", stream="stderr", data="boom")
    output = buffer.get_tail("commis-2")

    assert "[stderr]" in output
    assert "boom" in output
