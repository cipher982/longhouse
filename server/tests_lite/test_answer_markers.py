"""A marker counts as the model's answer only when it stands alone on a line.

The fixtures are the steered turns from the factory's claude_helm_steer_active
runs on 2026-10-10 (claude-haiku-5-5): two that obeyed the steer but quoted the
forbidden marker while explaining, and one that answered with the marker alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zerg.qa.answer_markers import marker_answered
from zerg.qa.claude_helm_lifecycle import steer_landed_in_turn
from zerg.qa.pi_family_turn_oracle import abort_then_send_verdict

FIXTURES = Path(__file__).parent / "fixtures" / "claude_helm_steer"
MARKER = "LONGHOUSE_CLAUDE_UNSTEERED_abc123"


@pytest.mark.parametrize(
    "text",
    [
        MARKER,
        f"{MARKER}\n",
        f"`{MARKER}`",
        f"**{MARKER}**",
        f"{MARKER}.",
        f"- {MARKER}",
        f"> {MARKER}",
        f"All three checks ran.\n\n{MARKER}",
    ],
)
def test_a_marker_alone_on_a_line_is_the_answer(text: str) -> None:
    assert marker_answered(text, MARKER)


@pytest.mark.parametrize(
    "text",
    [
        f"Steps 2 and 3 were not run, so I did not include `{MARKER}`.",
        f"so I did not include the {MARKER} marker.",
        f"I won't reply with {MARKER} because the steer said to stop.",
        f"{MARKER}_extra",
        "",
    ],
)
def test_a_marker_mentioned_in_prose_is_not_the_answer(text: str) -> None:
    assert not marker_answered(text, MARKER)


def _fixture(name: str) -> tuple[list[dict], str]:
    document = json.loads((FIXTURES / f"{name}.json").read_text())
    return document["rows"], document["token"]


def _steer_verdict(rows: list[dict], token: str) -> dict:
    step = f"lh_claude_step_{token}"
    steered = f"LONGHOUSE_CLAUDE_STEERED_{token}"
    return steer_landed_in_turn(
        rows,
        prompt_marker=f"{step}_1",
        steer_marker=steered,
        steered_marker=steered,
        done_marker=f"LONGHOUSE_CLAUDE_UNSTEERED_{token}",
        later_step_command=f"{step}_3",
    )


@pytest.mark.parametrize("name", ["quoted_in_backticks", "quoted_in_prose", "marker_only"])
def test_real_obeyed_steers_pass_even_when_the_model_explains(name: str) -> None:
    rows, token = _fixture(name)
    verdict = _steer_verdict(rows, token)
    assert verdict["passed"], verdict
    assert verdict["original_task_finished"] is False


def test_a_model_that_really_finished_the_original_task_still_fails() -> None:
    rows, token = _fixture("marker_only")
    done = f"LONGHOUSE_CLAUDE_UNSTEERED_{token}"
    finished = [
        row
        if row.get("type") != "assistant" or "STEERED" not in json.dumps(row)
        else {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": f"All three checks passed.\n\n{done}"}]},
        }
        for row in rows
    ]
    verdict = _steer_verdict(finished, token)
    assert not verdict["passed"]
    assert verdict["original_task_finished"] is True
    assert verdict["failure_code"] == "steer_did_not_change_course"


def test_pi_family_abort_ignores_a_quoted_done_marker_but_not_an_answered_one() -> None:
    def entries(done_text: str) -> list[dict]:
        return [
            {"id": "u1", "parentId": None, "type": "message", "message": {"role": "user", "content": "run task TASK_MARKER"}},
            {
                "id": "a1",
                "parentId": "u1",
                "type": "message",
                "message": {"role": "assistant", "stopReason": "aborted", "content": [{"type": "text", "text": done_text}]},
            },
            {"id": "u2", "parentId": "a1", "type": "message", "message": {"role": "user", "content": "AFTER_MARKER"}},
            {
                "id": "a2",
                "parentId": "u2",
                "type": "message",
                "message": {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "AFTER_MARKER"}]},
            },
        ]

    quoted = abort_then_send_verdict(
        entries("Stopped; I never reached DONE_MARKER."),
        task_marker="TASK_MARKER",
        task_done_marker="DONE_MARKER",
        after_marker="AFTER_MARKER",
    )
    answered = abort_then_send_verdict(
        entries("DONE_MARKER"), task_marker="TASK_MARKER", task_done_marker="DONE_MARKER", after_marker="AFTER_MARKER"
    )
    assert quoted["code"] != "abort_did_not_stop_active_turn", quoted
    assert answered["code"] == "abort_did_not_stop_active_turn", answered
