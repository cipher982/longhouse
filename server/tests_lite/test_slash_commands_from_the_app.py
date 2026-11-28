"""Slash commands sent from the app: the server's half (engine/src/slash_input.rs has the rest)."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.provider_contract.managed_provider_contract_manifest import _validate_slash_commands  # noqa: E402
from zerg.routers.session_chat import SessionInputRequest  # noqa: E402
from zerg.services.session_chat_impl import dispatch_command_turn  # noqa: E402
from zerg.services.session_chat_impl import dispatch_started_no_turn  # noqa: E402
from zerg.services.slash_input import is_slash_command  # noqa: E402

CONTRACTS = Path(__file__).resolve().parents[1] / "zerg" / "config" / "managed_provider_contracts.json"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/goal ship it", True),
        ("  /model", True),
        ("/skill:review x", True),
        ("//goal is text", False),
        ("/tmp/x is broken", False),
        ("/", False),
        ("hello /goal", False),
    ],
)
def test_is_slash_command_reads_text_as_the_engine_does(text: str, expected: bool) -> None:
    assert is_slash_command(text) is expected


def test_a_command_sent_as_a_steer_waits_for_idle_and_text_does_not() -> None:
    assert SessionInputRequest(text="/goal x", intent="steer", client_request_id="c1").intent == "queue"
    assert SessionInputRequest(text="change course", intent="steer", client_request_id="c1").intent == "steer"
    assert SessionInputRequest(text="//goal x", intent="steer", client_request_id="c1").intent == "steer"


def test_send_results_say_whether_a_command_opened_a_turn() -> None:
    assert dispatch_started_no_turn({"command": "rename", "turn_started": False})
    assert not dispatch_started_no_turn({"command": "compact", "turn_started": True})
    assert not dispatch_started_no_turn({"exit_code": 0})
    assert dispatch_command_turn({"command": "rename"}) is None  # typed into a terminal: unknown
    assert dispatch_command_turn({"command": "x", "turn_started": True}) is True
    assert dispatch_command_turn({"turn_started": False}) is None  # not a command


def _contract(provider: str) -> dict:
    payload = json.loads(CONTRACTS.read_text(encoding="utf-8"))
    return next(item for item in payload["providers"] if item["provider"] == provider)


@pytest.mark.parametrize("provider", ["claude", "codex", "opencode", "antigravity", "cursor", "pi", "omp"])
def test_every_provider_declares_slash_commands(provider: str) -> None:
    item = _contract(provider)
    _validate_slash_commands(item)
    assert item["slash_commands"]["cases"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda s: s.update(console="refuse", refuse_reason=""), "refuse_reason"),
        (lambda s: s.update(helm="maybe"), "helm must be one of"),
        (lambda s: s.update(pickers=["/model"]), "without the slash"),
        (lambda s: s.update(cases=[]), "non-empty"),
        (lambda s: s.update(cases=[{"command": "x", "expect": "worked"}]), "expect must be one of"),
    ],
)
def test_the_contract_validator_rejects_bad_sections(change, message: str) -> None:
    item = copy.deepcopy(_contract("claude"))
    change(item["slash_commands"])
    with pytest.raises(ValueError, match=message):
        _validate_slash_commands(item)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda s: s.update(reads_leading_slash=["shadow"]), "reads_leading_slash"),
        (lambda s: s.update(cases=[{"command": "x", "expect": "text"}]), "literal"),
        (lambda s: s.update(cases=[{"command": "x", "literal": True, "expect": "ran"}]), "literal never runs"),
    ],
)
def test_the_validator_checks_literal_cases_and_slash_reading_modes(change, message: str) -> None:
    item = copy.deepcopy(_contract("cursor"))
    change(item["slash_commands"])
    with pytest.raises(ValueError, match=message):
        _validate_slash_commands(item)


@pytest.mark.parametrize("provider", ["claude", "codex", "opencode", "antigravity", "cursor", "pi", "omp"])
def test_every_provider_proves_a_double_slash_message(provider: str) -> None:
    """The shared cases send ``//...`` on every provider: one slash dropped, or refused where the terminal would run it."""

    literal = [case for case in _contract(provider)["slash_commands"]["cases"] if case.get("literal")]
    assert any(case["expect"] == "text" for case in literal)
    reads = _contract(provider)["slash_commands"].get("reads_leading_slash", [])
    if "helm" in reads:
        assert any(case["expect"] == "refused" for case in literal)


def test_the_slash_command_scenario_sends_a_literal_and_wants_one_slash_dropped() -> None:
    from zerg.qa.terminal_truth_command import CommandPlan
    from zerg.qa.terminal_truth_command import judge_case
    from zerg.qa.terminal_truth_command import plan_command_cell
    from zerg.qa.terminal_truth_verdicts import CellSpec

    plan = CommandPlan(CellSpec("codex", "helm", "slash_command"), "abc123")
    case = {"command": "ttlit", "args": "TTLIT", "literal": True, "expect": "text"}
    assert plan.text_for(case) == "//ttlit TTLITabc123"
    assert plan.model_text_for(case) == "/ttlit TTLITabc123"
    sent = {"text": plan.text_for(case), "status": "delivered"}
    assert judge_case(plan, case, sent, None, ["/ttlit TTLITabc123"])[0]
    assert not judge_case(plan, case, sent, None, ["//ttlit TTLITabc123"])[0], "both slashes reached the model"
    assert not judge_case(plan, case, sent, None, [])[0], "nothing reached the model"

    assert not plan_command_cell(CellSpec("codex", "console", "slash_command")).not_measured
    assert plan_command_cell(CellSpec("pi", "console", "slash_command")).not_measured
