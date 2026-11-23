"""The question scenarios cannot pass vacuously: the model's follow-up turn fires only on the chosen answer, and every oracle must hold."""

from __future__ import annotations

import pytest

from zerg.qa import scripted_model_server as server_module
from zerg.qa import terminal_truth_question as question
from zerg.qa.terminal_truth_verdicts import CellSpec

TOOLS = [{"name": "AskUserQuestion", "input_schema": {"type": "object"}}]


def _plan(scenario: str = "question") -> question.QuestionPlan:
    return question.plan_question_cell(CellSpec("claude", "helm", scenario))


def _request(plan: question.QuestionPlan, result: str) -> dict:
    """Claude's follow-up request: the prompt, its own AskUserQuestion call (both labels), then the tool_result."""

    call = plan.turns[1]["tool_call"]
    return {
        "tools": TOOLS,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": plan.prompt}]},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu_1", "name": "AskUserQuestion", "input": call["arguments"]}],
            },
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": result}]},
        ],
    }


def _rows(plan: question.QuestionPlan, result: str, *, done: bool = True) -> list[dict]:
    rows = [
        {"message": {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "AskUserQuestion", "input": {}}]}},
        {"message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": result}]}},
    ]
    if done:
        rows.append({"message": {"role": "assistant", "content": [{"type": "text", "text": f"{plan.done} got the answer"}]}})
    return rows


def _shown(plan: question.QuestionPlan) -> list[dict]:
    options = [{"label": label, "value": label} for label in plan.labels]
    return [{"id": "question_1", "question": plan.question, "options": options}]


def _good(plan: question.QuestionPlan) -> dict:
    return {
        "send_action": {"state": "available"},
        "pause_request": {"id": "p1", "kind": "structured_question", "can_respond": True, "questions": _shown(plan)},
        "answer_ok": True,
        "transcript_rows": _rows(plan, f'"{plan.question}"="{plan.chosen}"'),
        "followup_served": 1,
        "pause_request_after": {"id": "p1", "status": "resolved"},
    }


@pytest.mark.parametrize("scenario", question.SCENARIOS)
def test_the_prompt_gets_the_question_and_only_the_chosen_answer_gets_the_closing_reply(scenario: str) -> None:
    plan = _plan(scenario)
    first = server_module._scripted_plan({"tools": TOOLS, "messages": [{"role": "user", "content": plan.prompt}]}, plan.turns)
    assert first is not None and first.turn_id.endswith(":a") and first.tool_call[0] == "AskUserQuestion"
    options = first.tool_call[1]["questions"][0]["options"]
    assert [o["label"] for o in options] == list(plan.labels)

    followup = server_module._scripted_plan(_request(plan, f'"{plan.question}"="{plan.chosen}"'), plan.turns)
    assert followup is not None and followup.turn_id.endswith(":b") and plan.done in followup.text
    # Option 1, or a result that names neither, does not reach the closing reply (the labels in the call's own input do not count).
    other = server_module._scripted_plan(_request(plan, f'"{plan.question}"="{plan.labels[0]}"'), plan.turns)
    assert other is None or not other.turn_id.endswith(":b")


def test_only_claude_helm_is_measured() -> None:
    for provider, mode in (("claude", "console"), ("codex", "helm"), ("pi", "helm")):
        plan = question.plan_question_cell(CellSpec(provider, mode, "question"))
        assert plan.not_measured and not plan.turns


def test_every_oracle_must_hold() -> None:
    plan = _plan()
    assert all(ok for _, ok, _ in question.judge_question(plan, _good(plan)))
    one_label = [{**_shown(plan)[0], "options": [{"label": plan.labels[1]}]}]
    breaks = {
        "the host offers send_input (live-control lease)": {"send_action": {"state": "unavailable"}},
        "the app is shown the question and both options": {"pause_request": {"id": "p1", "can_respond": True, "questions": one_label}},
        "the host lists the question as answerable": {"pause_request": {"id": "p1", "can_respond": False, "questions": _shown(plan)}},
        "the answer was accepted": {"answer_ok": False},
        "Claude's transcript has the call's tool_result carrying the answer": {
            "transcript_rows": _rows(plan, f"answered {plan.labels[0]}")
        },
        "the scripted model got the follow-up request carrying the answer": {"followup_served": 0},
        "the turn ended (the closing reply is in the transcript)": {"transcript_rows": _rows(plan, plan.chosen, done=False)},
        "the pause request resolved": {"pause_request_after": {"id": "p1", "status": "failed"}},
    }
    for check, change in breaks.items():
        failed = [name for name, ok, _ in question.judge_question(plan, {**_good(plan), **change}) if not ok]
        assert failed == [check], (check, failed)


def test_the_steer_variant_needs_the_owners_own_text_in_the_tool_result() -> None:
    plan = _plan("question_steer")
    assert plan.chosen == plan.response and plan.response.startswith(f"TTQ{plan.nonce}R")
    obs = {**_good(plan), "transcript_rows": _rows(plan, f'"{plan.question}"="{plan.labels[1]}"')}
    failed = [name for name, ok, _ in question.judge_question(plan, obs) if not ok]
    assert failed == ["Claude's transcript has the call's tool_result carrying the answer"]


def test_an_error_result_or_one_naming_both_options_does_not_count() -> None:
    plan = _plan()
    rows = _rows(plan, f"{plan.labels[0]} or {plan.labels[1]}")
    checks = {name: ok for name, ok, _ in question.judge_question(plan, {**_good(plan), "transcript_rows": rows})}
    assert not checks["Claude's transcript has the call's tool_result carrying the answer"]
    errored = _rows(plan, plan.chosen)
    errored[1]["message"]["content"][0]["is_error"] = True
    assert question.tool_result_text(errored, "toolu_1") is None


def test_a_steer_that_also_became_a_user_turn_fails() -> None:
    plan = _plan("question_steer")
    rows = _rows(plan, plan.chosen) + [{"message": {"role": "user", "content": plan.chosen}}]
    failed = [name for name, ok, _ in question.judge_question(plan, {**_good(plan), "transcript_rows": rows}) if not ok]
    assert failed == ["Claude's transcript has the call's tool_result carrying the answer"]


def test_a_run_of_only_not_measured_cells_fails(monkeypatch, tmp_path) -> None:
    from zerg.qa import terminal_truth

    engine = tmp_path / "engine"
    engine.write_text("")
    monkeypatch.setattr(
        terminal_truth, "run_question_suite", lambda *a, **k: [{"cell": "codex/helm", "scenario": "question", "outcome": "not_measured"}]
    )
    assert terminal_truth.main(["run", "--scenario", "question", "--engine", str(engine), "--cell", "codex/helm"]) == 1
