"""Focused safety tests for the installed managed-launch evidence harness."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "qa"
    / "installed-managed-launch-fault-matrix.py"
)
_SPEC = importlib.util.spec_from_file_location("installed_launch_fault_matrix", _SCRIPT)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


def _result(returncode: int, *, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_process_records_fails_closed_on_unexpected_ps_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _MODULE.subprocess,
        "run",
        lambda *args, **kwargs: _result(2, stderr="ps failed"),
    )

    with pytest.raises(_MODULE.ProcessScanFailure, match="returncode=2"):
        _MODULE.process_records({1234})


def test_process_group_pids_accepts_pgrep_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _MODULE.subprocess,
        "run",
        lambda *args, **kwargs: _result(1),
    )

    assert _MODULE.process_group_pids(1234) == set()


def test_process_group_pids_fails_closed_on_unexpected_pgrep_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _MODULE.subprocess,
        "run",
        lambda *args, **kwargs: _result(2, stderr="pgrep failed"),
    )

    with pytest.raises(_MODULE.ProcessScanFailure, match="returncode=2"):
        _MODULE.process_group_pids(1234)


def test_process_start_identity_fails_closed_on_unexpected_ps_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _MODULE.subprocess,
        "run",
        lambda *args, **kwargs: _result(2, stderr="ps failed"),
    )

    with pytest.raises(_MODULE.ProcessScanFailure, match="returncode=2"):
        _MODULE.process_start_identity(1234)
