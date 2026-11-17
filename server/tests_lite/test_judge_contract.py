"""The replay contract of a producer: ``Producer.judge`` names a pure ``judge(evidence)``, and result.json records it.

The differential replay guard (control-plane ``provider_factory/differential_replay.py``) reads ``judge.entry_point``
out of a stored result.json and calls it in an old and a new tree. Whatever a producer lists here must therefore
resolve, be pure, and decide the same assertions the live run reached.
"""

from __future__ import annotations

import builtins
import copy
import importlib
import json
import pathlib
import socket
import time
from collections.abc import Callable
from collections.abc import Mapping
from typing import Any

import pytest

from zerg.qa import omp_console_producer
from zerg.qa import pi_console_tool_producer
from zerg.qa import provider_console_lifecycle as lifecycle
from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run

QA_PACKAGE = "zerg.qa"


# Every producer module that declares a replay judge. A producer joins the guard's contract by being listed here.
JUDGING_MODULES = (
    "provider_console_lifecycle",
    "omp_console_producer",
    "pi_console_tool_producer",
    "pi_coordination_awareness",
    "omp_coordination_awareness",
    "claude_coordination_awareness_create",
    "claude_coordination_awareness_post_compaction",
    "claude_coordination_directed_input",
    "cursor_coordination_producer",
    "codex_coordination_native",
    "codex_native_resume",
    "claude_background_producer",
)


def _judging_producers() -> list[tuple[str, Producer]]:
    found = []
    for name in JUDGING_MODULES:
        producer = importlib.import_module(f"{QA_PACKAGE}.{name}").PRODUCER
        assert isinstance(producer, Producer) and producer.judge is not None, name
        found.append((name, producer))
    native = importlib.import_module(f"{QA_PACKAGE}.provider_native_resume")
    for provider in native.SPECS:
        found.append((f"provider_native_resume[{provider}]", native.producer_for(provider)))
    return found


def resolve(entry_point: str) -> Callable[[Mapping[str, Any]], dict[str, bool]]:
    module, _, function = entry_point.partition(":")
    assert module.startswith(QA_PACKAGE + "."), entry_point
    return getattr(importlib.import_module(module), function)


def recorded(
    producer: Producer, tmp_path: pathlib.Path, *, observation: dict, assertions: dict, provider: str | None = None, **fields: Any
) -> dict:
    """The result.json the shell writes for a run that reached ``assertions``, as the guard would read it."""

    run = Run(root=tmp_path, observation=observation, assertions=assertions, fields=dict(fields))
    return json.loads(json.dumps(producer.verdict(run, provider=provider)))


def evidence_of(result: Mapping[str, Any]) -> dict[str, Any]:
    """What the guard hands a judge: the recorded result without its assertions and its judge block."""

    return {key: value for key, value in result.items() if key not in ("assertions", "judge")}


@pytest.mark.parametrize(("name", "producer"), _judging_producers(), ids=lambda value: getattr(value, "judge", value))
def test_every_declared_judge_resolves_and_is_recorded(name: str, producer: Producer, tmp_path: pathlib.Path):
    assert callable(resolve(producer.judge))
    module = importlib.import_module(producer.judge.partition(":")[0])
    assert module.JUDGE_REVISION == producer.judge_revision
    assert module.JUDGE == producer.judge
    result = recorded(producer, tmp_path, observation={}, assertions={})
    assert result["judge"] == {"entry_point": producer.judge, "revision": producer.judge_revision}


class _World:
    """Wraps a judge so that the clock, the network and files refuse it while it runs."""

    def __init__(self, judge: Callable[[Mapping[str, Any]], dict[str, bool]]):
        self.judge = judge

    def __call__(self, evidence: Mapping[str, Any]) -> dict[str, bool]:
        def refuse(*_args: Any, **_kwargs: Any):
            raise AssertionError("a judge touched the world")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(builtins, "open", refuse)
            patch.setattr(time, "time", refuse)
            patch.setattr(time, "sleep", refuse)
            patch.setattr(socket, "socket", refuse)
            patch.setattr(pathlib.Path, "read_text", refuse)
            patch.setattr(pathlib.Path, "exists", refuse)
            return self.judge(evidence)


def pure(judge: Callable[[Mapping[str, Any]], dict[str, bool]]) -> _World:
    return _World(judge)


def _console_observation() -> dict[str, Any]:
    observation: dict[str, Any] = {fact: True for fact in lifecycle.OBSERVED_ACTIVITY}
    observation.update(pi_tool_enabled=True, continuation_context_recalled=True)
    return observation


def _steer(**overrides: Any) -> dict[str, Any]:
    receipt: dict[str, Any] = dict(
        steer_accepted=True, same_run_completed=True, steer_marker_answered=True, no_new_turn=True, tool_ran_to_completion=True
    )
    receipt.update(overrides)
    return receipt


CONSOLE = [
    ("provider.console_lifecycle", lifecycle.PRODUCER, {"provider": "codex"}),
    ("pi.console_tool", pi_console_tool_producer.PRODUCER, {"provider": "pi"}),
]


@pytest.mark.parametrize(("label", "producer", "identity"), CONSOLE, ids=[row[0] for row in CONSOLE])
def test_console_cell_is_routed_by_its_steer_receipt(label, producer, identity, tmp_path):
    judge = pure(resolve(producer.judge))
    steer_cell = recorded(producer, tmp_path / "s", observation=_console_observation(), assertions={}, steer=_steer(), **identity)
    assert judge(evidence_of(steer_cell)) == {lifecycle.STEER_ASSERTION_ID: True}
    dropped = copy.deepcopy(steer_cell)
    dropped["steer"]["steer_marker_answered"] = False
    assert judge(evidence_of(dropped)) == {lifecycle.STEER_ASSERTION_ID: False}
    plain = recorded(producer, tmp_path / "p", observation=_console_observation(), assertions={}, steer=None, **identity)
    plain_verdict = judge(evidence_of(plain))
    assert len(plain_verdict) == 1 and set(plain_verdict.values()) == {True} and lifecycle.STEER_ASSERTION_ID not in plain_verdict
    broken = copy.deepcopy(plain)
    broken["observation"]["pi_tool_enabled"] = False
    broken["observation"][lifecycle.OBSERVED_ACTIVITY[0]] = False
    assert set(judge(evidence_of(broken)).values()) == {False}


def test_pi_tool_fact_joins_the_generic_cell_only_for_pi(tmp_path):
    observation = _console_observation()
    observation["pi_tool_enabled"] = False
    assert pure(lifecycle.judge)({"provider": "pi", "observation": observation, "steer": None}) == {lifecycle.ASSERTION_ID: False}
    assert pure(lifecycle.judge)({"provider": "codex", "observation": observation, "steer": None}) == {lifecycle.ASSERTION_ID: True}


def test_omp_judge_routes_the_steer_cell_and_decides_the_plain_cell_by_its_own_rule():
    assert pure(omp_console_producer.judge)({"observation": {}, "steer": _steer()}) == {lifecycle.STEER_ASSERTION_ID: True}
    plain = pure(omp_console_producer.judge)({"observation": {}, "steer": None})
    assert plain == {omp_console_producer.ASSERTION_ID: False}


def test_the_live_run_and_the_replay_share_one_judge():
    """The run decides its assertions through the same function the guard calls, so they cannot drift apart."""

    import inspect

    assert "judge(" in inspect.getsource(lifecycle._run_live)
    assert "judge(" in inspect.getsource(omp_console_producer._reach)
    assert "judge(" in inspect.getsource(pi_console_tool_producer.run_pi_console_tool)


# --- coordination: the scenario a result records picks the oracle ---------------------------------------------------------

COORDINATION = (
    "pi_coordination_awareness",
    "omp_coordination_awareness",
    "claude_coordination_awareness_create",
    "claude_coordination_awareness_post_compaction",
    "claude_coordination_directed_input",
    "cursor_coordination_producer",
    "codex_coordination_native",
)

HELD = {
    "awareness_create": {"coordination_instructions_model_visible": True},
    "awareness_post_compaction": {
        "coordination_instructions_model_visible_after_compaction": True,
        "visible_bootstrap_count": 1,
    },
    "directed_input": {"input_persisted": True, "input_receipt_linked": True, "input_visible": True, "source_session_id": "s"},
}


def _scenario_ids(module_name: str) -> list[str]:
    """The scenario ids a coordination producer's cells record: its registration's, and any further one it dispatches."""

    module = importlib.import_module(f"{QA_PACKAGE}.{module_name}")
    ids = {module.REGISTRATION.scenario_id}
    for name in ("_DISPATCH",):
        for value in getattr(module, name, {}).values():
            ids.add(value[1])
    for name in dir(module):
        value = getattr(module, name)
        if name.startswith("_SCENARIO") and isinstance(value, str):
            ids.add(value)
    return sorted(ids)


@pytest.mark.parametrize("module_name", COORDINATION)
def test_every_coordination_scenario_routes_to_its_oracle_and_decides_like_the_run(module_name, tmp_path):
    from zerg.qa import provider_coordination_oracles as oracles

    judge = pure(oracles.judge)
    for scenario_id in _scenario_ids(module_name):
        kind = next(kind for kind in HELD if scenario_id.endswith("_" + kind))
        run_oracle = {
            "awareness_create": oracles.awareness_create_assertions,
            "awareness_post_compaction": oracles.awareness_post_compaction_assertions,
            "directed_input": oracles.directed_input_assertions,
        }[kind]
        held = {"scenario_id": scenario_id, "observation": dict(HELD[kind])}
        assert judge(held) == run_oracle(HELD[kind])
        assert all(judge(held).values())
        failed = {"scenario_id": scenario_id, "observation": {}}
        assert judge(failed) == run_oracle({}) and not any(judge(failed).values())


@pytest.mark.parametrize("module_name", COORDINATION)
def test_the_judge_names_every_assertion_the_producer_registers(module_name):
    from zerg.qa import provider_coordination_oracles as oracles

    module = importlib.import_module(f"{QA_PACKAGE}.{module_name}")
    registered = {assertion for assertion, _variant in module.REGISTRATION.assertion_cells}
    judged = set()
    for scenario_id in _scenario_ids(module_name):
        kind = next(kind for kind in HELD if scenario_id.endswith("_" + kind))
        judged |= set(oracles.judge({"scenario_id": scenario_id, "observation": HELD[kind]}))
    assert registered <= judged


def test_an_unknown_coordination_scenario_is_an_error_not_a_pass():
    from zerg.qa import provider_coordination_oracles as oracles

    for evidence in ({"scenario_id": "x_other", "observation": {}}, {"observation": {}}, {"scenario_id": "a_directed_input"}):
        with pytest.raises((KeyError, ValueError)):
            oracles.judge(evidence)


# --- native Resume: the cell's variant is the named extra input ---------------------------------------------------------


def _resume_observation(variant: str) -> dict[str, Any]:
    facts = {
        name: True
        for name in (
            "same_longhouse_session same_provider_thread new_run new_connection new_app_server_process provider_neutral_resume_intent "
            "native_resume_command bridge_subscribed post_resume_provider_activity post_resume_response_correlated stale_input_rejected "
            "concurrent_resume_refused artifact_secret_scan_passed final_cleanup_verified"
        ).split()
    }
    facts.update(stale_generation_dispatched=False, orphan_count=0)
    if variant == "clean_exit":
        facts["clean_stop_verified"] = True
    else:
        facts.update(old_bridge_process_dead=True, old_app_server_process_dead=True, replacement_started_after_process_loss=True)
    return facts


@pytest.mark.parametrize("variant", ["clean_exit", "process_loss"])
def test_native_resume_is_judged_by_its_recorded_variant(variant):
    from zerg.qa import provider_resume_oracles as oracles

    judge = pure(oracles.judge)
    other = "process_loss" if variant == "clean_exit" else "clean_exit"
    held = {"variant": variant, "observation": _resume_observation(variant)}
    assert judge(held) == {"native_provider_resume_proven": True}
    # The other variant's facts do not prove this one: the variant picks which facts are required.
    assert judge({"variant": other, "observation": _resume_observation(variant)}) == {"native_provider_resume_proven": False}
    broken = {"variant": variant, "observation": {**_resume_observation(variant), "stale_generation_dispatched": True}}
    assert judge(broken) == {"native_provider_resume_proven": False}


def test_native_resume_with_no_or_unknown_variant_is_an_error():
    from zerg.qa import provider_resume_oracles as oracles

    for evidence in ({"observation": {}}, {"variant": "other", "observation": {}}):
        with pytest.raises((KeyError, ValueError)):
            oracles.judge(evidence)


def test_a_written_result_carries_the_inputs_its_judge_reads(tmp_path):
    """Round trip through the shell: what the guard reads from result.json is enough to decide the assertions again."""

    from zerg.qa import codex_coordination_native
    from zerg.qa import codex_native_resume

    resume = codex_native_resume.PRODUCER.verdict(
        Run(root=tmp_path / "r", observation=_resume_observation("process_loss"), assertions={"native_provider_resume_proven": True}),
        variant="process_loss",
    )
    (tmp_path / "r").mkdir(exist_ok=True)
    stored = json.loads(json.dumps(resume))
    assert resume_judge(stored) == stored["assertions"]

    scenario = "codex_coordination_directed_input"
    run = Run(root=tmp_path / "c", observation=dict(HELD["directed_input"]), assertions={})
    coordination = codex_coordination_native.PRODUCER.verdict(run, scenario_id=scenario)
    stored = json.loads(json.dumps(coordination))
    assert stored["scenario_id"] == scenario
    verdict = resolve(stored["judge"]["entry_point"])(evidence_of(stored))
    assert verdict and all(verdict.values())


def resume_judge(stored):
    return resolve(stored["judge"]["entry_point"])(evidence_of(stored))


# --- Claude background: the stored scenario record is judged from its raw facts, not from a stored verdict -----------------


def _background_scenario() -> dict[str, Any]:
    return {
        "source": {
            "identity_ok": True,
            "digests_ok": True,
            "active_registry_observed": True,
            "active_registry_kinds": ["subagent", "shell"],
            "active_registry_at_parent_turn_end": True,
            "native_callbacks_matched": True,
            "explicit_empty_observed": True,
        },
        "parent_turn": {"assistant_marker_archived": True, "end_timestamp": 12.5},
        "canonical_parent_observed": True,
        "initial_served_active": False,
        "child_callback_replaced_parent_tool": False,
        "served_active": True,
        "served_empty": True,
        # A producer that wrote a verdict here must not be believed: the judge ignores it.
        "assertions": {},
    }


def _background(scenario: dict[str, Any]) -> dict[str, Any]:
    return {"observation": {"background": scenario}}


def test_claude_background_judges_the_stored_observation_and_ignores_a_stored_verdict(tmp_path):
    from zerg.qa import claude_background_producer as m

    judge = pure(m.judge)
    healthy = _background_scenario()
    healthy["assertions"] = {name: False for name in m.ASSERTIONS}  # a lying stored verdict
    assert judge(_background(healthy)) == {name: True for name in m.ASSERTIONS}
    stored = recorded(m.PRODUCER, tmp_path, observation={"background": _background_scenario()}, assertions={})
    assert stored["judge"]["entry_point"] == "zerg.qa.claude_background_producer:judge"
    assert all(judge(evidence_of(stored)).values())


@pytest.mark.parametrize(
    ("path", "value", "fails"),
    [
        (("source", "identity_ok"), False, {"claude_background_native_source", "claude_background_callbacks_scoped"}),
        (("source", "active_registry_kinds"), ["shell"], {"claude_background_native_source"}),
        (("source", "active_registry_observed"), False, {"claude_background_native_source", "claude_background_parent_turn_boundary"}),
        (("source", "active_registry_at_parent_turn_end"), False, {"claude_background_parent_turn_boundary"}),
        (("parent_turn", "assistant_marker_archived"), False, {"claude_background_parent_turn_boundary"}),
        (("parent_turn", "end_timestamp"), None, {"claude_background_parent_turn_boundary"}),
        (("served_active",), False, {"claude_background_registry_served"}),
        (("served_empty",), False, {"claude_background_registry_served"}),
        (("child_callback_replaced_parent_tool",), True, {"claude_background_callbacks_scoped"}),
        (("initial_served_active",), True, {"claude_background_callbacks_scoped"}),
        (("canonical_parent_observed",), False, {"claude_background_callbacks_scoped"}),
        (("source", "native_callbacks_matched"), False, {"claude_background_callbacks_scoped"}),
        (("source", "explicit_empty_observed"), False, {"claude_background_explicit_empty"}),
    ],
)
def test_claude_background_each_raw_fact_decides_its_assertions(path, value, fails):
    from zerg.qa import claude_background_producer as m

    scenario = _background_scenario()
    target = scenario
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    verdict = pure(m.judge)(_background(scenario))
    assert {name for name, held in verdict.items() if not held} == fails


def test_claude_background_a_scenario_that_errored_or_is_missing_judges_false():
    from zerg.qa import claude_background_producer as m

    errored = _background_scenario() | {"error": "TimeoutError: x"}
    assert not any(m.judge(_background(errored)).values())
    assert not any(m.judge({"observation": {}}).values()) and not any(m.judge({}).values())
