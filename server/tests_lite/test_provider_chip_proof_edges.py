"""Landing chips follow factory proof, and unproven claims stay visible.

A contract boolean such as ``send_input: true`` is runtime authority: it lets
the Machine Agent dispatch the operation. The landing chip is a separate claim
that a live-token factory assertion exercises that operation against the real
provider binary. This test makes the gap between the two an explicit, shrinking
list instead of an invisible drift. See
control-plane ``docs/specs/provider-chip-proof-graph.md``.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONTRACTS = REPO / "server" / "zerg" / "config" / "managed_provider_contracts.json"
GENERATOR = REPO / "scripts" / "generate" / "provider_capabilities_ts.py"

CHIP_OPERATIONS = ("launch_local", "send_input", "interrupt", "terminate", "steer_active_turn")

# Operations the runtime supports but no live-token factory assertion proves
# yet. Each entry keeps its landing chip gray. Remove an entry in the same
# commit that adds its required_assertions; the test fails if one lingers.
UNPROVEN_RUNTIME_CLAIMS = frozenset(
    {
        ("antigravity", "launch_local"),
        ("antigravity", "send_input"),
        ("claude", "launch_local"),
        ("claude", "send_input"),
        ("claude", "interrupt"),
        ("claude", "terminate"),
        ("claude", "steer_active_turn"),
    }
)


def _generator():
    spec = importlib.util.spec_from_file_location("provider_capabilities_ts", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _providers() -> list[dict]:
    return json.loads(CONTRACTS.read_text(encoding="utf-8"))["providers"]


def test_every_runtime_claim_is_proven_or_listed_as_unproven() -> None:
    generator = _generator()
    unproven = set()
    for provider in _providers():
        for operation in CHIP_OPERATIONS:
            if provider[operation] and not generator.operation_proven(provider, operation):
                unproven.add((provider["provider"], operation))
        if provider["can_resume"] and not generator.capability_proven(provider, "session.resume.helm"):
            unproven.add((provider["provider"], "can_resume"))

    assert unproven - UNPROVEN_RUNTIME_CLAIMS == set(), "runtime claims without factory proof must be listed"
    assert UNPROVEN_RUNTIME_CLAIMS - unproven == set(), "proven claims must leave UNPROVEN_RUNTIME_CLAIMS"


def test_chip_is_never_lit_without_its_runtime_capability() -> None:
    generator = _generator()
    for provider in _providers():
        chips = generator.chip_states(provider)
        for chip, operations in generator.CHIP_OPERATIONS.items():
            if chips[chip]:
                assert all(provider[operation] for operation in operations), (provider["provider"], chip)
        if chips["resume"]:
            assert provider["can_resume"], provider["provider"]


def test_live_no_token_and_hermetic_cells_never_light_a_chip() -> None:
    generator = _generator()
    provider = {
        "launch_local": True,
        "send_input": True,
        "operation_evidence": {
            "launch_local": {
                "disposition": "implemented",
                "required_assertions": [{"id": "surface", "acceptable_evidence": ["live_no_token"]}],
            },
            "send_input": {
                "disposition": "implemented",
                "required_assertions": [{"id": "send", "acceptable_evidence": ["hermetic", "live_token"]}],
            },
        },
    }
    assert not generator.operation_proven(provider, "launch_local")
    assert not generator.operation_proven(provider, "send_input")
    provider["operation_evidence"]["launch_local"]["required_assertions"][0]["acceptable_evidence"] = ["live_token"]
    provider["operation_evidence"]["send_input"]["required_assertions"][0]["acceptable_evidence"] = ["live_token"]
    assert generator.chip_states(provider)["launchAndSend"]


def test_generated_landing_capabilities_are_current() -> None:
    generator = _generator()
    assert generator.TS_OUT.read_text(encoding="utf-8") == generator.render_ts()
