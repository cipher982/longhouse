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
