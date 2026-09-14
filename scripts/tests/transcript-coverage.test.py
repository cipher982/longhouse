#!/usr/bin/env python3
"""Boundaries for the provider-fidelity coverage instrument."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "ops"))

from managed_profiler import transcript_coverage as coverage  # noqa: E402

OBSERVED_AT_MS = 1_789_308_600_000


def write_transcript(records: list[dict]) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    for record in records:
        handle.write(json.dumps(record) + "\n")
    handle.close()
    return Path(handle.name)


def assistant(
    record_id: str,
    *,
    timestamp: str = "2026-09-13T14:08:48.525Z",
    thinking: str | None = None,
    text: str | None = None,
    calls: list[str] | None = None,
) -> dict:
    parts: list[dict] = []
    if thinking is not None:
        parts.append({"type": "thinking", "thinking": thinking})
    if text is not None:
        parts.append({"type": "text", "text": text})
    for index, call_id in enumerate(calls or []):
        parts.append({"type": "toolCall", "id": call_id, "name": "bash", "arguments": {"i": f"call {index}"}})
    return {
        "type": "message",
        "id": record_id,
        "timestamp": timestamp,
        "message": {"role": "assistant", "content": parts},
    }


def tool_result(record_id: str, call_id: str, text: str, *, timestamp: str = "2026-09-13T14:08:49.000Z") -> dict:
    return {
        "type": "message",
        "id": record_id,
        "timestamp": timestamp,
        "message": {
            "role": "toolResult",
            "toolCallId": call_id,
            "toolName": "bash",
            "content": [{"type": "text", "text": text}],
        },
    }


def served_event(event_id: str, role: str, **fields: object) -> dict:
    event = {"id": event_id, "role": role, "content_text": None, "tool_name": None, "tool_call_id": None}
    event.update(fields)
    return event


class ExtractOmpTranscriptTests(unittest.TestCase):
    """The extractor must mirror the engine's own event identity scheme."""

    def test_keys_match_engine_part_suffixes(self) -> None:
        path = write_transcript(
            [
                assistant("rec-a", thinking="reasoning one", text="first text", calls=["call-1"]),
                tool_result("rec-b", "call-1", "output"),
            ]
        )
        try:
            events = coverage.extract_native_events("omp", path)
        finally:
            path.unlink()
        keys = {(event.event_class, event.key) for event in events}
        self.assertIn(("thinking", "rec-a"), keys)
        # Block index 1 is a text part, so it carries the explicit suffix.
        self.assertIn(("assistant_text", "rec-a-text-1"), keys)
        self.assertIn(("tool_call", "call-1"), keys)
        self.assertIn(("tool_result", "call-1"), keys)

    def test_first_text_block_uses_the_bare_record_id(self) -> None:
        path = write_transcript([assistant("rec-only-text", text="sole block")])
        try:
            events = coverage.extract_native_events("omp", path)
        finally:
            path.unlink()
        self.assertEqual([(event.event_class, event.key) for event in events], [("assistant_text", "rec-only-text")])

    def test_blank_and_malformed_records_are_ignored(self) -> None:
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        handle.write("\n")
        handle.write("not json\n")
        handle.write(json.dumps({"type": "title", "title": "ignored"}) + "\n")
        handle.write(json.dumps({"type": "message", "id": "rec-empty", "message": {"role": "assistant", "content": []}}) + "\n")
        handle.close()
        path = Path(handle.name)
        try:
            self.assertEqual(coverage.extract_native_events("omp", path), [])
        finally:
            path.unlink()

    def test_unknown_provider_is_rejected(self) -> None:
        path = write_transcript([])
        try:
            with self.assertRaises(SystemExit):
                coverage.extract_native_events("nonesuch", path)
        finally:
            path.unlink()


class CoverageReportTests(unittest.TestCase):
    def test_complete_parity_passes(self) -> None:
        native = [
            coverage.NativeEvent("thinking", "rec-a-thinking-0", OBSERVED_AT_MS - 5_000, 10),
            coverage.NativeEvent("tool_call", "call-1", OBSERVED_AT_MS - 5_000, 0),
            coverage.NativeEvent("tool_result", "call-1", OBSERVED_AT_MS - 4_000, 20),
        ]
        served = [
            served_event("rec-a-thinking-0", "system"),
            served_event("rec-a-tool-call-1", "assistant", tool_name="bash", tool_call_id="call-1"),
            served_event("rec-b", "tool", tool_name="bash", tool_call_id="call-1", tool_output_text="output"),
        ]
        report = coverage.coverage_report(
            native, served, provider="omp", session_id="s", transcript="/t", observed_at_ms=OBSERVED_AT_MS
        )
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["classes"]["thinking"]["coverage"], 1.0)
        self.assertEqual(report["classes"]["tool_call"]["coverage"], 1.0)

    def test_thinking_deleted_at_read_time_is_reported_as_a_class_gap(self) -> None:
        """The 2026-09-13 defect: reasoning is stored but never served."""

        native = [coverage.NativeEvent("thinking", f"rec-{i}", OBSERVED_AT_MS - 1_000, 100) for i in range(3)]
        native.append(coverage.NativeEvent("tool_call", "call-1", OBSERVED_AT_MS - 1_000, 0))
        served = [served_event("rec-0-tool-call-1", "assistant", tool_name="bash", tool_call_id="call-1")]
        report = coverage.coverage_report(
            native, served, provider="omp", session_id="s", transcript="/t", observed_at_ms=OBSERVED_AT_MS
        )
        self.assertEqual(report["verdict"], "partial")
        self.assertEqual(report["classes"]["thinking"]["provider"], 3)
        self.assertEqual(report["classes"]["thinking"]["served"], 0)
        self.assertEqual(report["classes"]["thinking"]["coverage"], 0.0)
        self.assertIn("thinking", report["totals"]["incomplete_classes"])
        self.assertEqual(report["classes"]["thinking"]["missing_chars"], 300)

    def test_tool_events_join_on_provider_call_id_not_event_id_shape(self) -> None:
        native = [coverage.NativeEvent("tool_result", "call_00_abc|fc_tmp_xyz", OBSERVED_AT_MS - 1_000, 5)]
        served = [served_event("deadbeef", "tool", tool_name="bash", tool_call_id="call_00_abc|fc_tmp_xyz")]
        report = coverage.coverage_report(
            native, served, provider="omp", session_id="s", transcript="/t", observed_at_ms=OBSERVED_AT_MS
        )
        self.assertEqual(report["classes"]["tool_result"]["coverage"], 1.0)

    def test_a_stale_backlog_is_a_gap_not_a_stall(self) -> None:
        """A record that never arrived is a gap however old it is; stalling is
        a property of the lane, which this snapshot cannot see."""
        native = [coverage.NativeEvent("tool_call", "call-1", OBSERVED_AT_MS - 500_000, 0)]
        report = coverage.coverage_report(
            native,
            [],
            provider="omp",
            session_id="s",
            transcript="/t",
            observed_at_ms=OBSERVED_AT_MS,
            stall_age_ms=60_000,
        )
        self.assertEqual(report["verdict"], "gap")
        self.assertEqual(report["oldest_unpropagated_age_ms"], 500_000)

    def test_recent_gap_is_partial_not_stalled(self) -> None:
        native = [coverage.NativeEvent("tool_call", "call-1", OBSERVED_AT_MS - 2_000, 0)]
        report = coverage.coverage_report(
            native,
            [],
            provider="omp",
            session_id="s",
            transcript="/t",
            observed_at_ms=OBSERVED_AT_MS,
            stall_age_ms=60_000,
        )
        self.assertEqual(report["verdict"], "missing")
        self.assertEqual(report["oldest_unpropagated_age_ms"], 2_000)

    def test_a_run_boundary_never_makes_an_otherwise_healthy_session_stall(self) -> None:
        """`ended_at` advances after every turn of a live interactive session."""

        native = [coverage.NativeEvent("tool_call", "call-1", OBSERVED_AT_MS - 1_000, 0)]
        served = [served_event("rec-tool-call-1", "assistant", tool_name="bash", tool_call_id="call-1")]
        report = coverage.coverage_report(
            native,
            served,
            provider="omp",
            session_id="s",
            transcript="/t",
            observed_at_ms=OBSERVED_AT_MS,
            provider_alive=True,
            served_ended_at="2026-09-13T14:27:06.632000Z",
        )
        self.assertEqual(report["liveness"]["verdict"], "served_marked_ended")
        self.assertEqual(report["verdict"], "pass")

    def test_an_in_flight_tail_is_not_a_partial_gap(self) -> None:
        native = [coverage.NativeEvent("tool_call", f"call-{i}", OBSERVED_AT_MS - 1_000, 0) for i in range(100)]
        served = [
            served_event(f"rec-tool-call-{i}", "assistant", tool_name="bash", tool_call_id=f"call-{i}")
            for i in range(99)
        ]
        report = coverage.coverage_report(
            native, served, provider="omp", session_id="s", transcript="/t", observed_at_ms=OBSERVED_AT_MS
        )
        self.assertEqual(report["classes"]["tool_call"]["coverage"], 0.99)
        self.assertEqual(report["verdict"], "pass")
        self.assertIn("tool_call", report["totals"]["incomplete_classes"])
        self.assertEqual(report["totals"]["lagging_classes"], [])

    def test_session_reported_ended_while_provider_alive_is_stalled(self) -> None:
        native = [coverage.NativeEvent("tool_call", "call-1", OBSERVED_AT_MS - 1_000, 0)]
        served = [served_event("rec-tool-call-1", "assistant", tool_name="bash", tool_call_id="call-1")]
        report = coverage.coverage_report(
            native,
            served,
            provider="omp",
            session_id="s",
            transcript="/t",
            observed_at_ms=OBSERVED_AT_MS,
            provider_alive=True,
            served_ended_at="2026-09-13T14:27:06.632000Z",
        )
        self.assertEqual(report["classes"]["tool_call"]["coverage"], 1.0)
        self.assertEqual(report["liveness"]["verdict"], "served_marked_ended")
        # The verdict comes from the gap, and this session has none.
        self.assertEqual(report["verdict"], "pass")

    def test_liveness_is_unknown_without_evidence(self) -> None:
        report = coverage.coverage_report(
            [coverage.NativeEvent("user", "rec-1", OBSERVED_AT_MS, 4)],
            [served_event("rec-1", "user", content_text="hi")],
            provider="omp",
            session_id="s",
            transcript="/t",
            observed_at_ms=OBSERVED_AT_MS,
        )
        self.assertEqual(report["liveness"]["verdict"], "unknown")
        self.assertEqual(report["verdict"], "pass")

    def test_empty_provider_transcript_is_empty_not_failed(self) -> None:
        report = coverage.coverage_report(
            [], [], provider="omp", session_id="s", transcript="/t", observed_at_ms=OBSERVED_AT_MS
        )
        self.assertEqual(report["verdict"], "empty")

    def test_unmapped_served_events_are_counted_not_silently_dropped(self) -> None:
        report = coverage.coverage_report(
            [coverage.NativeEvent("user", "rec-1", OBSERVED_AT_MS, 4)],
            [served_event("rec-1", "user", content_text="hi"), {"id": "x", "role": "mystery"}],
            provider="omp",
            session_id="s",
            transcript="/t",
            observed_at_ms=OBSERVED_AT_MS,
        )
        self.assertEqual(report["totals"]["served_unmapped_events"], 1)

    def test_text_report_names_every_class_and_the_missing_sample(self) -> None:
        native = [coverage.NativeEvent("thinking", "rec-1", OBSERVED_AT_MS - 90_000, 42)]
        report = coverage.coverage_report(
            native, [], provider="omp", session_id="s", transcript="/t", observed_at_ms=OBSERVED_AT_MS
        )
        text = coverage.render_text(report)
        for name in coverage.EVENT_CLASSES:
            self.assertIn(name, text)
        self.assertIn("rec-1", text)
        self.assertIn("GAP", text)


class ServedPayloadTests(unittest.TestCase):
    def test_payload_shapes(self) -> None:
        self.assertEqual(coverage.served_events_from_payload({"events": [{"id": "a"}]}), [{"id": "a"}])
        self.assertEqual(coverage.served_events_from_payload([{"id": "a"}]), [{"id": "a"}])
        self.assertEqual(coverage.served_events_from_payload({"events": "nope"}), [])
        self.assertEqual(coverage.served_events_from_payload(None), [])




class ServeLatencyTests(unittest.TestCase):
    """Latency is served_at - provider_recorded_at, and only when both exist."""

    def test_latency_is_measured_for_records_first_seen_inside_the_window(self) -> None:
        window_start = OBSERVED_AT_MS
        native = [
            coverage.NativeEvent("tool_call", "call-1", window_start + 1_000, 0),
            coverage.NativeEvent("tool_call", "call-2", window_start + 2_000, 0),
            coverage.NativeEvent("thinking", "rec-1", window_start + 1_500, 10),
        ]
        first_seen = {
            ("tool_call", "call-1"): window_start + 3_000,
            ("tool_call", "call-2"): window_start + 10_000,
            ("thinking", "rec-1"): window_start + 4_500,
        }

        report = coverage.summarise_serve_latency(native, first_seen, window_started_at_ms=window_start)

        calls = report["classes"]["tool_call"]
        self.assertEqual(calls["samples"], 2)
        self.assertEqual(calls["min_ms"], 2_000)
        self.assertEqual(calls["max_ms"], 8_000)
        self.assertEqual(calls["p50_ms"], 2_000)
        self.assertEqual(report["classes"]["thinking"]["p50_ms"], 3_000)

    def test_a_call_and_its_result_are_measured_apart(self) -> None:
        window_start = OBSERVED_AT_MS
        native = [
            coverage.NativeEvent("tool_call", "call-shared", window_start + 1_000, 0),
            coverage.NativeEvent("tool_result", "call-shared", window_start + 1_000, 20),
        ]
        first_seen = {
            ("tool_call", "call-shared"): window_start + 2_000,
            ("tool_result", "call-shared"): window_start + 5_000,
        }

        report = coverage.summarise_serve_latency(native, first_seen, window_started_at_ms=window_start)

        self.assertEqual(report["classes"]["tool_call"]["p50_ms"], 1_000)
        self.assertEqual(report["classes"]["tool_result"]["p50_ms"], 4_000)

    def test_records_already_present_are_not_counted_as_instant(self) -> None:
        window_start = OBSERVED_AT_MS
        native = [coverage.NativeEvent("tool_call", "call-old", window_start - 60_000, 0)]
        # The first poll stamps every served key with its own clock; only the
        # baseline set can tell that this key was already there.
        first_seen = {("tool_call", "call-old"): window_start + 5}

        report = coverage.summarise_serve_latency(
            native,
            first_seen,
            window_started_at_ms=window_start,
            baseline_keys={("tool_call", "call-old")},
        )

        entry = report["classes"]["tool_call"]
        self.assertEqual(entry["samples"], 0)
        self.assertEqual(entry["pre_existing"], 1)
        self.assertIsNone(entry["p50_ms"])

    def test_a_record_absent_from_the_first_poll_is_measured_even_if_old(self) -> None:
        window_start = OBSERVED_AT_MS
        # Written long before the window opened but not yet served: its arrival
        # is exactly what the measurement is for.
        native = [coverage.NativeEvent("tool_result", "call-late", window_start - 600_000, 10)]
        first_seen = {("tool_result", "call-late"): window_start + 4_000}

        report = coverage.summarise_serve_latency(native, first_seen, window_started_at_ms=window_start)

        entry = report["classes"]["tool_result"]
        self.assertEqual(entry["samples"], 1)
        self.assertEqual(entry["p50_ms"], 604_000)
        self.assertEqual(entry["pre_existing"], 0)

    def test_records_that_never_arrived_are_counted_not_dropped(self) -> None:
        window_start = OBSERVED_AT_MS
        native = [coverage.NativeEvent("thinking", "rec-never", window_start + 1_000, 10)]

        report = coverage.summarise_serve_latency(native, {}, window_started_at_ms=window_start)

        entry = report["classes"]["thinking"]
        self.assertEqual(entry["never_seen"], 1)
        self.assertEqual(entry["samples"], 0)

    def test_percentiles_use_nearest_rank(self) -> None:
        self.assertEqual(coverage._percentile([10, 20, 30, 40], 0.5), 20)
        self.assertEqual(coverage._percentile([10, 20, 30, 40], 0.95), 40)
        self.assertEqual(coverage._percentile([7], 0.95), 7)


class ManagedDiscoveryTests(unittest.TestCase):
    """The sweep finds launches from the Machine Agent's own state files."""

    def _state(self, root: Path, provider_dir: str, name: str, payload: dict) -> None:
        directory = root / provider_dir
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(json.dumps(payload), encoding="utf-8")

    def test_live_launches_are_discovered_and_stopped_ones_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            transcript.write_text("", encoding="utf-8")
            self._state(root, "omp-helm", "live.json", {
                "session_id": "s-live", "session_file": str(transcript), "status": "degraded",
            })
            self._state(root, "omp-helm", "stopped.json", {
                "session_id": "s-stopped", "session_file": str(transcript), "status": "stopped",
            })

            discovered = coverage.discover_managed_sessions(root)

            self.assertEqual([entry.session_id for entry in discovered], ["s-live"])
            self.assertEqual(discovered[0].provider, "omp")
            self.assertEqual(discovered[0].status, "degraded")

            with_stopped = coverage.discover_managed_sessions(root, include_stopped=True)
            self.assertEqual(sorted(entry.session_id for entry in with_stopped), ["s-live", "s-stopped"])

    def test_launches_without_a_readable_transcript_are_not_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._state(root, "omp-helm", "gone.json", {
                "session_id": "s-gone", "session_file": str(root / "missing.jsonl"), "status": "ready",
            })
            self._state(root, "omp-helm", "nameless.json", {"session_file": "/tmp/x.jsonl"})

            self.assertEqual(coverage.discover_managed_sessions(root), [])

    def test_malformed_state_files_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "omp-helm"
            directory.mkdir(parents=True)
            (directory / "broken.json").write_text("{not json", encoding="utf-8")

            self.assertEqual(coverage.discover_managed_sessions(root), [])

    def test_a_missing_state_root_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(coverage.discover_managed_sessions(Path(tmp) / "absent"), [])


class SweepVerdictTests(unittest.TestCase):
    def test_the_worst_verdict_wins_and_unknown_ranks_highest(self) -> None:
        self.assertEqual(coverage.worst_verdict([{"verdict": "pass"}, {"verdict": "partial"}]), "partial")
        self.assertEqual(coverage.worst_verdict([{"verdict": "partial"}, {"verdict": "stalled"}]), "stalled")
        self.assertEqual(coverage.worst_verdict([{"verdict": "pass"}, {"verdict": "empty"}]), "empty")
        self.assertEqual(coverage.worst_verdict([{"verdict": "pass"}, {"verdict": "surprising"}]), "surprising")


def main() -> int:
    result = unittest.main(module=__name__, exit=False, verbosity=2)
    return 0 if result.result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
