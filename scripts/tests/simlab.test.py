#!/usr/bin/env python3
"""False-pass and failure-artifact boundaries for the simulator harness."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("simlab", ROOT / "scripts/qa/simlab.py")
assert spec is not None and spec.loader is not None
simlab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(simlab)

FINAL_TEXT = "The actual final provider reply."


def workspace(final_text: str = FINAL_TEXT, final_id: str = "final") -> dict:
    return {
        "session": {"hidden_from_default_timeline": True},
        "projection": {
            "items": [
                {"kind": "event", "event": {"id": f"user-{index}", "role": "user", "content_text": f"Prompt {index}"}}
                for index in range(3)
            ] + [{"kind": "event", "event": {"id": final_id, "role": "assistant", "content_text": final_text}}],
            "abandoned_events": 0,
        },
    }


def mark(stage: str, detail: str = "") -> str:
    return f"session open stage={stage} session=session-1 elapsed_ms=20 {detail}\n"


def render(latest: str = "prose:final", extra: str = "") -> str:
    return mark("webkit_rendered", f"rows=9 bytes=800 latest={latest} revision=-1 sequence=2 js_failures=0 {extra}")


def evaluate(log: str, **kwargs) -> dict:
    return simlab.evaluate_verdict(
        simlab.parse_app_marks(log, "session-1"), kwargs.pop("workspace", workspace()),
        expect_frames=False, expect_abandoned=None, expect_user_turns=3,
        expected_text=FINAL_TEXT, **kwargs,
    )


class VerdictBoundaryTests(unittest.TestCase):
    def test_duplicate_initial_renders_and_server_only_progress_cannot_pass(self):
        initial = mark("start") + mark("stream_connected") + render("prose:old") * 3
        result = evaluate(initial)
        self.assertEqual(result["status"], "fail")
        self.assertIn("client_latest_rendered", [item["id"] for item in result["checks"] if item["status"] == "fail"])
        self.assertEqual(evaluate(initial + render())["status"], "pass")

    def test_relaunch_needs_new_start_and_newest_render_from_that_open(self):
        initial = mark("start") + mark("stream_connected") + render()
        self.assertEqual(evaluate(initial, minimum_starts=2)["status"], "fail")
        reopened = initial + mark("start") + mark("stream_connected") + render("prose:old")
        self.assertEqual(evaluate(reopened, minimum_starts=2)["status"], "fail")
        self.assertEqual(evaluate(reopened + render(), minimum_starts=2)["status"], "pass")
        self.assertEqual(evaluate(reopened + render() + render("prose:old"), minimum_starts=2)["status"], "fail")

    def test_matching_partial_head_is_not_source_completeness(self):
        partial = workspace("Earlier reply; final assistant entry has not shipped.", "earlier")
        self.assertFalse(simlab.projection_complete(partial, 3, FINAL_TEXT))
        self.assertEqual(evaluate(mark("start") + mark("stream_connected") + render("prose:earlier"), workspace=partial)["status"], "fail")
        self.assertTrue(simlab.projection_complete(workspace(), 3, FINAL_TEXT))
        self.assertEqual(evaluate(mark("start") + mark("stream_connected") + render())["status"], "pass")

    def test_missing_client_contract_and_renderer_failure_fail_closed(self):
        opened = mark("start") + mark("stream_connected")
        self.assertEqual(evaluate(opened + mark("webkit_rendered", "rows=9 bytes=800"))["status"], "fail")
        self.assertEqual(evaluate(opened + render(extra="js_failures=1"))["status"], "fail")
        self.assertEqual(evaluate(opened + render() + mark("webkit_failed"))["status"], "fail")

    def test_network_fault_allows_transport_failure_but_not_decode_or_js_failure(self):
        recovered = mark("start") + mark("stream_connected") + mark("stream_stale") + mark("request_failed") + render()
        self.assertEqual(evaluate(recovered)["status"], "fail")
        self.assertEqual(evaluate(recovered, allow_transport_fault=True)["status"], "pass")
        self.assertEqual(evaluate(recovered + mark("stream_decode_failed"), allow_transport_fault=True)["status"], "fail")
        self.assertEqual(evaluate(recovered + mark("webkit_failed"), allow_transport_fault=True)["status"], "fail")

    def test_network_recovery_rejects_poll_only_catchup_or_unobserved_outage(self):
        initial = mark("start") + mark("stream_connected") + render("prose:old")
        offline = initial + mark("stream_disconnected")
        boundary = {"initial_marks": 3, "offline_marks": 4}
        options = {"allow_transport_fault": True, "network_boundary": boundary}
        self.assertEqual(evaluate(offline + render(), **options)["status"], "fail")
        reconnected = offline + mark("stream_connected") + render()
        self.assertEqual(evaluate(reconnected, **options)["status"], "pass")
        self.assertEqual(evaluate(reconnected + mark("start") + mark("stream_connected") + render(), **options)["status"], "fail")
        unobserved = initial + mark("request_failed") + mark("stream_connected") + render()
        self.assertEqual(evaluate(unobserved, **options)["status"], "fail")

    def test_visible_synthetic_session_cannot_pass(self):
        visible = workspace()
        visible["session"]["hidden_from_default_timeline"] = False
        self.assertEqual(evaluate(mark("start") + mark("stream_connected") + render(), workspace=visible)["status"], "fail")


class FailureArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run_dir = self.root / "current"
        self.state_path = self.run_dir / "simlab.json"
        self.patches = [
            patch.object(simlab, "RUN_DIR", self.run_dir),
            patch.object(simlab, "STATE_FILE", self.state_path),
            patch.object(simlab, "SCRATCH_ROOT", self.root / "scratch"),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self.temporary.cleanup)
        for item in self.patches:
            self.addCleanup(item.stop)

    def test_scenario_timeout_keeps_diagnostics_projection_screenshot_and_summary(self):
        scratch = self.root / "run-one"
        scratch.mkdir()
        state = {"scratch": str(scratch), "base_url": "http://127.0.0.1:1", "token": "scratch-only"}
        simlab.save_state(state)

        def failed_scenario(current, deploy):
            current.update({"session_id": "session-1", "phase": "await_final_render"})
            raise TimeoutError("final reply never rendered")

        def capture(current, scenario, label):
            path = simlab.scenario_artifact_dir(current, scenario) / f"{label}.png"
            path.write_bytes(b"test capture")
            return str(path)

        raw_log = mark("start") + mark("stream_connected") + render("prose:old")
        with patch.object(simlab, "SCENARIOS", {"timeout-case": failed_scenario}), \
             patch.object(simlab, "screenshot", side_effect=capture), \
             patch.object(simlab, "app_log_text", return_value=raw_log), \
             patch.object(simlab, "server_projection", return_value=workspace()):
            with self.assertRaises(SystemExit) as exit_context:
                simlab.cmd_run(argparse.Namespace(scenario=["timeout-case"], deploy=False))
        self.assertEqual(exit_context.exception.code, 2)
        summary = json.loads((scratch / "artifacts/summary.json").read_text())
        self.assertEqual(summary["status"], "fail")
        envelope = json.loads(Path(summary["scenarios"][0]["artifacts"]["verdict"]).read_text())
        self.assertEqual(envelope["evidence"]["phase"], "await_final_render")
        self.assertIn("final reply never rendered", envelope["evidence"]["error"])
        self.assertEqual(Path(envelope["artifacts"]["app_log"]).read_text(), raw_log)
        self.assertEqual(json.loads(Path(envelope["artifacts"]["workspace"]).read_text()), workspace())
        self.assertTrue(Path(envelope["artifacts"]["screenshots"]["final"]).is_file())
        self.assertEqual(json.loads(Path(envelope["artifacts"]["diagnostics"]).read_text())[-1]["fields"]["latest"], "prose:old")
        # A later run can replace the convenience files without losing proof.
        previous_verdict = Path(envelope["artifacts"]["verdict"])
        simlab.save_state({"scratch": str(self.root / "run-two")})
        self.assertEqual(json.loads(previous_verdict.read_text())["status"], "fail")

    def test_evidence_capture_failures_cannot_suppress_failure_verdict(self):
        scratch = self.root / "capture-errors"
        scratch.mkdir()
        state = {"scratch": str(scratch), "session_id": "session-1"}
        with patch.object(simlab, "screenshot", side_effect=RuntimeError("simulator unavailable")), \
             patch.object(simlab, "app_log_text", side_effect=RuntimeError("logs unavailable")), \
             patch.object(simlab, "server_projection", side_effect=RuntimeError("host unavailable")):
            envelope = simlab.failure_verdict(state, "failed", TimeoutError("catch-up timed out"))
        self.assertEqual(envelope["status"], "fail")
        self.assertEqual(len(envelope["artifacts"]["capture_errors"]), 3)
        self.assertIn("catch-up timed out", json.loads(Path(envelope["artifacts"]["verdict"]).read_text())["evidence"]["error"])

    def test_dead_follower_cannot_switch_to_a_sliding_log_window(self):
        follower = Mock()
        follower.process.poll.return_value = 1
        with patch.object(simlab, "_app_log_stream", follower), \
             patch.object(simlab.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "cannot switch log sources"):
                simlab.app_log_text({}, "10m")
            run.assert_not_called()

    def test_standalone_verdict_uses_preserved_log_not_a_sliding_window(self):
        (self.root / "app.log").write_text(mark("start") + render())
        with patch.object(simlab, "_app_log_stream", None), \
             patch.object(simlab.subprocess, "run") as run:
            self.assertEqual(simlab.app_log_text({"scratch": str(self.root)}, "1s"), mark("start") + render())
            run.assert_not_called()

    def test_down_does_not_signal_recycled_pid_but_stops_owned_process(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
        )
        try:
            identity = simlab.process_identity(child.pid)
            self.assertIsNotNone(identity)
            state = {"scratch": str(self.root), "server_pid": child.pid,
                     "process_identities": {"server_pid": "different process start and command"}}
            simlab.save_state(state)
            simlab.cmd_down(argparse.Namespace())
            self.assertIsNone(child.poll(), "down signalled a process that did not belong to this run")
            state["process_identities"]["server_pid"] = identity
            simlab.save_state(state)
            simlab.cmd_down(argparse.Namespace())
            self.assertIsNotNone(child.wait(timeout=3), "down left its owned process running")
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()

    def test_startup_failure_reaps_spawned_service_and_writes_summary(self):
        # Use an actual disposable process so cleanup is observed, not a mock
        # echo of killpg. No Runtime Host, engine, relay, or simulator starts.
        real_popen = subprocess.Popen
        children = []

        def spawn_service(command, **kwargs):
            self.assertEqual(command[:5], ["uv", "run", "python", "-m", "zerg.cli.main"])
            process = real_popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdout=kwargs["stdout"], stderr=kwargs["stderr"], start_new_session=True,
            )
            children.append(process)
            return process

        try:
            with patch.object(simlab, "Popen", side_effect=spawn_service), \
                 patch.object(simlab, "wait_for", side_effect=TimeoutError("runtime host health failed")):
                with self.assertRaises(TimeoutError):
                    simlab.cmd_up(argparse.Namespace(build=False, port=12345))
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll(), "startup left its service running")
            summary = json.loads((self.run_dir / "summary.json").read_text())
            self.assertEqual(summary["status"], "fail")
            envelope = json.loads(Path(summary["scenarios"][0]["artifacts"]["verdict"]).read_text())
            self.assertIn("runtime host health failed", envelope["evidence"]["error"])
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    unittest.main()
