"""A marker counts as the model's answer only when it stands alone on a line.

The fixtures are the steered turns from the factory's claude_helm_steer_active
runs on 2026-10-10 (claude-haiku-5-5): two that obeyed the steer but quoted the
forbidden marker while explaining, and one that answered with the marker alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zerg.qa.answer_markers import marker_answer_count
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
        f"Not reached:\n- {MARKER}",
        f"> {MARKER}",
        f"* {MARKER}",
        "",
    ],
)
def test_a_marker_mentioned_in_prose_is_not_the_answer(text: str) -> None:
    assert not marker_answered(text, MARKER)


# A turn that works through tools narrates before it answers. The narration
# names the marker inside a sentence ahead of a tool call; the final reply puts a
# line or two about the work first and the marker alone on its last line.
NARRATION = (
    "[INFERENCE] The shell job is terminal. The worker has not reported yet.\n\n"
    f"**Status: not complete.** Both jobs must finish before I may reply with `{MARKER}`.\n\n"
    "- shell job: done\n- worker: waiting for its terminal output"
)
FINAL_REPLY = f"Both terminal events observed:\n\n- worker: done\n- shell job: done\n\n{MARKER}"
CONTEXT = "LH_FIXTURE_CONTEXT_0123456789abcdef"


def test_narration_that_quotes_the_marker_is_a_mention_and_the_final_reply_is_the_answer() -> None:
    assert not marker_answered(NARRATION, MARKER)
    assert marker_answer_count(NARRATION, MARKER) == 0
    assert marker_answered(FINAL_REPLY, MARKER)
    assert marker_answer_count(FINAL_REPLY, MARKER) == 1


@pytest.mark.parametrize(
    "text",
    [
        f"{CONTEXT} {MARKER}",
        f"{CONTEXT}\n{MARKER}",
        # A model told to reply with "that value followed by exactly <MARKER>"
        # sometimes puts nothing between the two.
        f"{CONTEXT}{MARKER}",
        f"`{CONTEXT}`, `{MARKER}`.",
        f"The stored value is {CONTEXT}.\n\n{MARKER}",
    ],
)
def test_a_reply_asked_to_carry_two_markers_answers_with_each(text: str) -> None:
    assert marker_answered(text, MARKER, alongside=(CONTEXT,))
    assert marker_answer_count(text, MARKER, alongside=(CONTEXT,)) == 1


def test_a_second_marker_on_the_line_is_only_allowed_when_the_reply_was_asked_for_it() -> None:
    assert not marker_answered(f"{CONTEXT} {MARKER}", MARKER)
    assert not marker_answered(f"{CONTEXT}{MARKER}", MARKER)
    # Any other word on the line is still a mention, companions or not.
    assert not marker_answered(f"The value is {CONTEXT} and then {MARKER}", MARKER, alongside=(CONTEXT,))
    assert not marker_answered(f"I will not send {CONTEXT} {MARKER}", MARKER, alongside=(CONTEXT,))
    assert not marker_answered(f"- {CONTEXT} {MARKER}", MARKER, alongside=(CONTEXT,))
    # A longer token that contains the marker is not the marker.
    assert not marker_answered(f"{MARKER}_DONE", MARKER, alongside=(f"{MARKER}_DONE",))


@pytest.mark.parametrize(
    ("text", "count"),
    [
        (f"{MARKER}\n{MARKER}", 2),
        (f"{MARKER} {MARKER}", 2),
        (f"{MARKER}{MARKER}", 2),
        (f"Done.\n{MARKER}\n\nAgain: {MARKER}", 1),
        (f"I may not reply with {MARKER} yet.", 0),
        ("", 0),
    ],
)
def test_answers_are_counted_and_mentions_are_not(text: str, count: int) -> None:
    assert marker_answer_count(text, MARKER) == count


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


def test_every_prompt_that_elicits_a_judged_marker_asks_for_it_alone_on_a_line() -> None:
    from zerg.qa.claude_helm_lifecycle import abort_setup_prompt
    from zerg.qa.claude_helm_lifecycle import steer_setup_prompt
    from zerg.qa.cursor_helm_product_e2e import abort_task_prompt
    from zerg.qa.cursor_helm_product_e2e import steer_task_prompt as cursor_steer_prompt
    from zerg.qa.pi_family_turn_oracle import step_task_prompt

    for prompt, marker in (
        (steer_setup_prompt("lh_step", "DONE_X"), "DONE_X"),
        (abort_setup_prompt("lh_abort", "FORBIDDEN_X", 30), "FORBIDDEN_X"),
        (step_task_prompt("TASK_X", "DONE_X"), "DONE_X"),
        (cursor_steer_prompt("STEP_X", "DONE_X"), "DONE_X"),
        (abort_task_prompt("FORBIDDEN_X"), "FORBIDDEN_X"),
    ):
        assert f"{marker} alone on its own line" in prompt
