"""Tests for live worker output buffer."""

from zerg.services.worker_output_buffer import WorkerOutputBuffer


def test_worker_output_buffer_tail_truncates():
    buffer = WorkerOutputBuffer(max_bytes=10, ttl_seconds=60)

    buffer.append_output(worker_id="worker-1", stream="stdout", data="12345")
    buffer.append_output(worker_id="worker-1", stream="stdout", data="67890")

    assert buffer.get_tail("worker-1") == "1234567890"

    buffer.append_output(worker_id="worker-1", stream="stdout", data="abc")

    # Tail should keep last 10 chars
    assert buffer.get_tail("worker-1") == "4567890abc"


def test_worker_output_buffer_stderr_prefix():
    buffer = WorkerOutputBuffer(max_bytes=50, ttl_seconds=60)

    buffer.append_output(worker_id="worker-2", stream="stderr", data="boom")
    output = buffer.get_tail("worker-2")

    assert "[stderr]" in output
    assert "boom" in output
