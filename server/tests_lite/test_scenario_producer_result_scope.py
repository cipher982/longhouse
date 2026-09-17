"""A scenario-scoped producer must say so in the result it writes.

The factory refuses a result from a producer registered with
``observation_scope="scenario"`` unless the result also carries
``"observation_scope": "scenario"``. Claude and Cursor Helm lifecycle
producers shipped without it and every factory cell they back failed with
"scenario-scoped producer returned a cell-specific result".
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

QA_ROOT = Path(__file__).resolve().parents[1] / "zerg" / "qa"
SCENARIO_PRODUCERS = (
    "claude_helm_lifecycle",
    "codex_helm_lifecycle",
    "cursor_helm_lifecycle",
    "opencode_helm_lifecycle",
    "pi_helm_lifecycle",
)


@pytest.mark.parametrize("module_name", SCENARIO_PRODUCERS)
def test_scenario_scoped_producer_result_declares_its_scope(module_name: str) -> None:
    module = importlib.import_module(f"zerg.qa.{module_name}")
    assert module.REGISTRATION.observation_scope == "scenario"
    source = (QA_ROOT / f"{module_name}.py").read_text(encoding="utf-8")
    assert '"observation_scope": "scenario"' in source
