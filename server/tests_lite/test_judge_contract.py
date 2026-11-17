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
)


def _judging_producers() -> list[tuple[str, Producer]]:
    found = []
    for name in JUDGING_MODULES:
        producer = importlib.import_module(f"{QA_PACKAGE}.{name}").PRODUCER
        assert isinstance(producer, Producer) and producer.judge is not None, name
        found.append((name, producer))
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
