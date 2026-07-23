#!/usr/bin/env python3
"""Regression test for the Runtime Host command boundary."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("boundary", ROOT / "scripts/qa/check-no-python-device-path.py")
assert SPEC and SPEC.loader
boundary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(boundary)


def test_current_runtime_host_boundary() -> None:
    assert boundary.check(ROOT) == []


if __name__ == "__main__":
    test_current_runtime_host_boundary()
    print("PASS test_current_runtime_host_boundary")
