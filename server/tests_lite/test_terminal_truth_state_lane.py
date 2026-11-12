"""The facts lane (session state, as the web client reads it) cannot pass vacuously, and reads the shape the Runtime Host serves."""

from __future__ import annotations

import json

import pytest

from zerg.qa import terminal_truth_kinds as kinds
from zerg.qa.terminal_truth_lanes import state_snapshot
from zerg.qa.terminal_truth_verdicts import CellSpec

SID = "11111111-1111-1111-1111-111111111111"


def _workspace(activity: str, *, tool: str | None = None, label: str = "Idle", tasks: list[dict] | None = None) -> str:
    return json.dumps(
        {
            "session": {
                "session_state": {
                    "activity": {"state": activity, "tool": tool, "source": "hook", "observed_at": "2026-11-11T00:00:00Z"},
                    "presentation": {"primary": {"key": activity, "label": label, "tone": "running"}},
                    "delegation": {"state": "pending" if tasks else "none", "count": len(tasks or []), "items": tasks or []},
                    "commit_seq": 7,
                }
            }
        }
    )


def test_the_snapshot_reads_what_the_runtime_strip_draws() -> None:
    snap = state_snapshot(_workspace("executing", tool="Bash", label="Using Bash"), SID)
    assert snap and (snap["headline"], snap["activity_state"], snap["activity_tool"]) == ("Using Bash", "executing", "Bash")


def test_a_subagent_detail_matches_the_strip_formula() -> None:
    task = {"id": "t1", "kind": "subagent", "status": "running", "description": "Reviewer", "native_progress": {"current_tool": "Read"}}
    snap = state_snapshot(_workspace("quiescent", tasks=[task]), SID)
    assert snap and snap["detail"] == "Reviewer . Using Read" and snap["delegation_count"] == 1
    thinking = {**task, "native_progress": {"status": "thinking"}}
    snap = state_snapshot(_workspace("quiescent", tasks=[thinking]), SID)
    assert snap and snap["detail"] == "Reviewer . Thinking"


@pytest.mark.parametrize("body", ["not json", "{}", json.dumps({"session": {}}), json.dumps({"session": {"session_state": {}}})])
def test_a_response_without_the_facts_is_not_a_snapshot(body: str) -> None:
    assert state_snapshot(body, SID) is None


def _timeline(*rows: tuple[float, str, str | None]) -> list[tuple[float, dict]]:
    return [
        (
            t,
            state_snapshot(
                _workspace(act, tool=tool, label="Thinking" if act == "thinking" else "Using " + str(tool) if tool else "Idle"), SID
            )
            or {},
        )
        for t, act, tool in rows
    ]


def test_thinking_shown_for_the_whole_window_is_live() -> None:
    line = _timeline((0.0, "quiescent", None), (1.0, "thinking", None), (9.0, "quiescent", None))
    out = kinds.classify_state(line, kinds._thinking_state, (2.0, 8.0), 12.0)
    assert out["class"] == "live" and out["coverage"] == 1.0 and out["shown"] == ["Thinking"]


def test_thinking_that_appears_after_most_of_the_window_is_late() -> None:
    line = _timeline((0.0, "quiescent", None), (7.0, "thinking", None), (9.0, "quiescent", None))
    assert kinds.classify_state(line, kinds._thinking_state, (2.0, 8.0), 12.0)["class"] == "late"


def test_mutation_removing_the_thinking_samples_flips_live_to_absent() -> None:
    """The non-vacuity check: the same timeline with its thinking snapshots deleted must not still pass."""

    line = _timeline((0.0, "quiescent", None), (1.0, "thinking", None), (9.0, "quiescent", None))
    assert kinds.classify_state(line, kinds._thinking_state, (2.0, 8.0), 12.0)["class"] == "live"
    mutated = [(t, s) for t, s in line if s.get("activity_state") != "thinking"]
    assert kinds.classify_state(mutated, kinds._thinking_state, (2.0, 8.0), 12.0)["class"] == "absent"


def test_a_tool_state_outside_the_window_does_not_count() -> None:
    line = _timeline((0.0, "executing", "Bash"), (1.5, "quiescent", None))
    assert kinds.classify_state(line, kinds._tool_state, (3.0, 8.0), 12.0)["class"] == "absent"


def test_the_subagent_state_accepts_a_delegation_item_or_the_spawning_tool() -> None:
    pred = kinds._subagent_predicate("claude")
    task = {"id": "t1", "kind": "subagent", "status": "running", "description": "x"}
    assert pred(state_snapshot(_workspace("quiescent", tasks=[task]), SID) or {})
    assert pred(state_snapshot(_workspace("executing", tool="Agent", label="Using Agent"), SID) or {})
    assert not pred(state_snapshot(_workspace("executing", tool="Bash", label="Using Bash"), SID) or {})


def _obs(scenario: str, line: list[tuple[float, dict]], *, shaped: int | None = None) -> kinds.KindObservation:
    plan = kinds.plan_kind_cell(CellSpec("claude", "console", scenario))
    obs = kinds.KindObservation(plan)
    obs.state_timeline = line
    obs.state_reads = 50
    obs.state_shaped = len(line) if shaped is None else shaped
    obs.submitted = 1.0
    obs.model_sent = {s.marker: 2.0 + i for i, s in enumerate(plan.signals) if s.group in plan.model_groups}
    obs.side = {s.marker: 2.0 + i for i, s in enumerate(plan.signals) if s.group in plan.side_file_groups}
    return obs


def test_an_observer_that_never_saw_the_facts_reports_blind_not_absent() -> None:
    out = kinds.state_lane_result(_obs("thinking", [], shaped=0), 20.0)
    assert out["verdict"]["class"] == "observer_blind"


def test_a_dirty_baseline_is_reported_instead_of_credited_to_the_scenario() -> None:
    line = _timeline((0.5, "thinking", None), (3.0, "thinking", None))
    out = kinds.state_lane_result(_obs("thinking", line), 20.0)
    assert out["verdict"]["class"] == "observer_dirty"


def test_a_clean_run_with_thinking_in_the_window_is_live_end_to_end() -> None:
    line = _timeline((0.2, "quiescent", None), (1.5, "thinking", None), (12.0, "quiescent", None))
    out = kinds.state_lane_result(_obs("thinking", line), 20.0)
    assert out["baseline_idle"] and out["verdict"]["class"] == "live"
