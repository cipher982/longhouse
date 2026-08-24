"""Ingest must reject an uninterpretable phase_signal and never a terminal."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from zerg.managed_phase_contract import raw_phases
from zerg.services.session_runtime import RuntimeEventIngest


def _event(kind: str, phase: str | None) -> dict:
    return {
        "runtime_key": "codex:sess",
        "session_id": str(uuid4()),
        "run_id": str(uuid4()),
        "provider": "codex",
        "device_id": "cinder",
        "source": "codex_exec",
        "kind": kind,
        "phase": phase,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "dedupe_key": f"{kind}:{uuid4()}",
        "payload": {},
    }


def test_phase_signal_outside_the_contract_is_rejected():
    # `tool` is the phase codex_exec actually shipped; it carried no freshness
    # window and left the Console activity axis dark for every run.
    with pytest.raises(ValidationError, match="outside the managed phase contract"):
        RuntimeEventIngest.model_validate(_event("phase_signal", "tool"))


@pytest.mark.parametrize("phase", raw_phases())
def test_every_contract_phase_is_accepted_on_a_phase_signal(phase: str):
    assert RuntimeEventIngest.model_validate(_event("phase_signal", phase)).phase == phase


def test_terminal_signal_is_never_rejected_on_vocabulary():
    # A terminal carries its meaning in the payload and legitimately ships
    # `finished`, which the contract marks local-health-only. Rejecting one
    # would dead-letter it under the engine's poison isolation -- recreating
    # the exact loss this work exists to prevent.
    assert RuntimeEventIngest.model_validate(_event("terminal_signal", "finished")).phase == "finished"
    assert RuntimeEventIngest.model_validate(_event("terminal_signal", "tool")).phase == "tool"


def test_binding_signal_is_never_rejected_on_vocabulary():
    assert RuntimeEventIngest.model_validate(_event("binding_signal", "tool")).phase == "tool"


def test_a_phase_signal_without_a_phase_is_still_accepted():
    assert RuntimeEventIngest.model_validate(_event("phase_signal", None)).phase is None


def test_known_phases_is_derived_from_the_contract_not_restated():
    from zerg.services.session_runtime import KNOWN_PHASES

    assert KNOWN_PHASES == frozenset(raw_phases())


def test_python_and_engine_freshness_windows_agree():
    """The two sides read one contract; prove the numbers actually match.

    These windows used to live in session_runtime, in local health, and in the
    engine, aligned by a doc comment that named a module which had already been
    refactored away.
    """
    import re
    from pathlib import Path

    from zerg.managed_phase_contract import managed_phase_definitions
    from zerg.services.session_runtime import PHASE_FRESHNESS

    generated = Path(__file__).resolve().parents[2] / "engine" / "src" / "managed_phase_contract.rs"
    table = re.search(r"PHASE_FRESHNESS_SECONDS: &\[\(&str, i64\)\] = &\[(.*?)\];", generated.read_text(), re.S)
    assert table is not None, "the engine must expose a generated freshness table"
    engine_windows = {name.lower(): int(seconds) for name, seconds in re.findall(r"PHASE_([A-Z_]+),\s*(\d+)", table.group(1))}

    contract_windows = {item.normalized_raw_phase: item.freshness_seconds for item in managed_phase_definitions()}
    assert engine_windows == contract_windows

    for phase, window in PHASE_FRESHNESS.items():
        assert int(window.total_seconds()) == contract_windows[phase]
