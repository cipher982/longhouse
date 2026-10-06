#!/usr/bin/env python3
"""Regression coverage for the Runtime Host restart probe summary."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ops" / "deploy_window_probe.py"
SPEC = importlib.util.spec_from_file_location("deploy_window_probe", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)

BASE_TIME = datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc)


def observation(channel: str, monotonic: float, **fields: object) -> dict[str, object]:
    wall_time = (BASE_TIME + timedelta(seconds=monotonic)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {"wall_time": wall_time, "monotonic": monotonic, "channel": channel, **fields}


def restart_records() -> list[dict[str, object]]:
    lifecycle_updating = {
        "type": "host.lifecycle",
        "state": "updating",
        "runtime_epoch": "epoch-1",
        "attempt_id": "attempt-7",
        "phase": "stop",
        "expected_back_by": None,
        "deadline": None,
        "cutoff": None,
    }
    lifecycle_serving = {
        "type": "host.lifecycle",
        "state": "serving",
        "runtime_epoch": "epoch-2",
        "attempt_id": "attempt-7",
        "phase": None,
        "expected_back_by": None,
        "deadline": None,
        "cutoff": None,
    }
    return [
        observation("health", 9.0, event="response", status=200, latency_s=0.01, runtime=None, build_commit="old-build"),
        observation("health", 9.5, event="response", status=200, latency_s=0.02, runtime={"epoch": "epoch-1", "admission": "open"}, build_commit="old-build"),
        observation("control", 10.0, event="connect_attempt", attempt=1, outcome="connected", latency_s=0.2),
        observation("sse", 10.1, event="connect_attempt", attempt=1, outcome="connected", status=200, latency_s=0.1),
        observation("write", 10.8, event="response", status=200, latency_s=0.08, is_json=True, code=None, is_html=False, transport_error_class=None),
        observation("control", 11.0, event="frame", type="host.lifecycle", frame_kind="text", host_lifecycle=lifecycle_updating),
        observation("sse", 11.1, event="message", name="connected", data={"runtime_epoch": "epoch-1", "admission": "draining"}),
        observation("sse", 11.2, event="message", name="host_lifecycle", data=lifecycle_updating),
        observation("write", 11.5, event="response", status=503, latency_s=0.04, content_type="application/json", is_json=True, code="runtime_restarting", retryable=True, retry_after="2", is_html=False, transport_error_class=None),
        observation("write", 12.0, event="response", status=503, latency_s=0.04, content_type="application/json", is_json=True, code="runtime_restarting", retryable=True, retry_after="2", is_html=False, transport_error_class=None),
        observation("control", 12.5, event="disconnect", close_code=1012, close_reason="host.lifecycle"),
        observation("sse", 12.6, event="disconnect", reason="transport_error"),
        observation("write", 13.0, event="response", status=502, latency_s=0.03, content_type="text/html", is_json=False, code=None, retryable=None, retry_after=None, is_html=True, transport_error_class=None),
        observation("health", 13.2, event="response", status=200, latency_s=0.02, runtime={"epoch": "epoch-2", "admission": "pending"}, build_commit="new-build"),
        observation("sse", 13.4, event="message", name="connected", data={"runtime_epoch": "epoch-2", "admission": "open"}),
        observation("health", 13.5, event="response", status=200, latency_s=0.02, runtime={"epoch": "epoch-2", "admission": "open"}, build_commit="new-build"),
        observation("write", 13.75, event="response", status=503, latency_s=0.03, content_type="application/json", is_json=True, code=None, retryable=None, retry_after="1", is_html=False, transport_error_class=None),
        observation("write", 14.0, event="response", status=None, latency_s=0.02, content_type=None, is_json=False, code=None, retryable=None, retry_after=None, is_html=False, transport_error_class="connect"),
        observation("sse", 14.1, event="connect_attempt", attempt=2, outcome="connected", status=200, latency_s=0.15),
        observation("sse", 14.15, event="message", name="host_lifecycle", data=lifecycle_serving),
        observation("control", 14.25, event="connect_attempt", attempt=2, outcome="connected", latency_s=0.25),
        observation("control", 14.3, event="frame", type="host.lifecycle", frame_kind="text", host_lifecycle=lifecycle_serving),
        observation("write", 14.5, event="response", status=None, latency_s=6.0, content_type=None, is_json=False, code=None, retryable=None, retry_after=None, is_html=False, transport_error_class="read_timeout"),
        observation("write", 16.0, event="response", status=200, latency_s=0.07, content_type="application/json", is_json=True, code=None, retryable=None, retry_after=None, is_html=False, transport_error_class=None),
    ]


class AnalyzeRestartTests(unittest.TestCase):
    def test_full_restart_summary_reports_every_window_and_reconnect_measurement(self) -> None:
        records = restart_records()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "restart.jsonl"
            path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            summary = PROBE.analyze(path)

        expected = {
            "observation_count": 24,
            "writes": {
                "first_failure_monotonic": 11.5,
                "first_failure_wall_time": "2026-10-06T18:00:11.500Z",
                "first_success_after_failure_monotonic": 16.0,
                "first_success_after_failure_wall_time": "2026-10-06T18:00:16.000Z",
                "closed_writes_s": 4.5,
                "failed_by_class": {
                    "typed": 2,
                    "untyped_json": 1,
                    "html": 1,
                    "transport": 1,
                    "timeout": 1,
                    "other": 0,
                },
            },
            "health": {
                "epoch_change": {
                    "at_monotonic": 13.2,
                    "wall_time": "2026-10-06T18:00:13.200Z",
                    "from_epoch": "epoch-1",
                    "to_epoch": "epoch-2",
                },
                "first_open_after_change": {
                    "at_monotonic": 13.5,
                    "wall_time": "2026-10-06T18:00:13.500Z",
                    "after_epoch_change_s": 0.3,
                },
                "first_serving_evidence": {
                    "source": "sse_connected_admission_open",
                    "at_monotonic": 13.4,
                    "wall_time": "2026-10-06T18:00:13.400Z",
                    "after_restart_start_s": 1.9,
                },
            },
            "control": {
                "disconnect_monotonic": 12.5,
                "close_code": 1012,
                "close_reason": "host.lifecycle",
                "reconnect_monotonic": 14.25,
                "reconnect_after_disconnect_s": 1.75,
                "reconnect_after_open_s": 0.85,
                "reconnect_reference": "sse_connected_admission_open",
                "host_lifecycle_states": ["updating", "serving"],
            },
            "sse": {
                "disconnect_monotonic": 12.6,
                "close_code": None,
                "close_reason": None,
                "reconnect_monotonic": 14.1,
                "reconnect_after_disconnect_s": 1.5,
                "reconnect_after_open_s": 0.7,
                "reconnect_reference": "sse_connected_admission_open",
                "host_lifecycle_states": ["updating", "serving"],
            },
            "verdict": {
                "no_untyped_5xx": False,
                "closed_writes_s": 4.5,
                "control_reconnect_after_open_within_1s": True,
            },
        }
        self.assertEqual(summary, expected)


    def test_legacy_health_reports_write_and_reconnect_durations(self) -> None:
        records = [
            observation("health", 1.0, event="response", status=200, latency_s=0.01, runtime=None, build_commit="legacy-build"),
            observation("control", 1.1, event="connect_attempt", attempt=1, outcome="connected", latency_s=0.1),
            observation("sse", 1.2, event="connect_attempt", attempt=1, outcome="connected", status=200, latency_s=0.1),
            observation("write", 1.5, event="response", status=200, is_json=True, code=None, is_html=False, transport_error_class=None),
            observation("write", 2.0, event="response", status=503, is_json=True, code="runtime_restarting", retryable=True, is_html=False, transport_error_class=None),
            observation("control", 2.1, event="disconnect", close_code=1012, close_reason="host.lifecycle"),
            observation("sse", 2.2, event="disconnect", reason="transport_error"),
            observation("control", 3.2, event="connect_attempt", attempt=2, outcome="connected", latency_s=0.2),
            observation("sse", 3.3, event="connect_attempt", attempt=2, outcome="connected", status=200, latency_s=0.2),
            observation("write", 4.0, event="response", status=200, is_json=True, code=None, is_html=False, transport_error_class=None),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.jsonl"
            path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            summary = PROBE.analyze(path)

        self.assertEqual(summary["observation_count"], 10)
        self.assertIsNone(summary["health"]["epoch_change"])
        self.assertIsNone(summary["health"]["first_open_after_change"])
        self.assertEqual(
            summary["health"]["first_serving_evidence"],
            {
                "source": "accepted_write",
                "at_monotonic": 4.0,
                "wall_time": "2026-10-06T18:00:04.000Z",
                "after_restart_start_s": 2.0,
            },
        )
        self.assertEqual(summary["writes"]["closed_writes_s"], 2.0)
        self.assertEqual(
            summary["writes"]["failed_by_class"],
            {"typed": 1, "untyped_json": 0, "html": 0, "transport": 0, "timeout": 0, "other": 0},
        )
        self.assertEqual(summary["control"]["reconnect_after_disconnect_s"], 1.1)
        self.assertIsNone(summary["control"]["reconnect_after_open_s"])
        self.assertEqual(summary["sse"]["reconnect_after_disconnect_s"], 1.1)
        self.assertIsNone(summary["sse"]["reconnect_after_open_s"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
