#!/usr/bin/env python3
"""scripts/ops/prove.py negative-control mode: the wrapper that hides the fault from the producer and the verdict rule."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("prove", ROOT / "scripts/ops/prove.py")
prove = importlib.util.module_from_spec(spec)
sys.modules["prove"] = prove
spec.loader.exec_module(prove)

TARGET = "pi_helm_send_idle"


def verdict(assertions: dict, receipts: int, observation: dict | None = None) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "r.jsonl"
        path.write_text('{"fault": "x"}\n' * receipts)
        result = {"status": "fail", "assertions": assertions, "observation": observation or {}}
        return prove.control_verdict(result, path, TARGET, "pi_send_noop", 1.0, "ev")


def test_caught_fault_passes() -> None:
    assert verdict({"launch": True, TARGET: False}, 1)["verdict"] == "PASS"


def test_blind_oracle_unfired_fault_and_broken_preconditions_fail() -> None:
    assert "ORACLE BLIND" in verdict({"launch": True, TARGET: True}, 1)["reason"]
    assert "did not fire" in verdict({"launch": True, TARGET: False}, 0)["reason"]
    assert "preconditions failed" in verdict({"launch": False, TARGET: False}, 1)["reason"]
    assert "target not reported" in verdict({"launch": True}, 1)["reason"]
    assert "model" in verdict({TARGET: False}, 1, {"model_noncompliance": {TARGET: {}}})["reason"]


def test_wrapper_hands_the_producer_the_same_argv_and_environment() -> None:
    argv = ["python", "-m", "producer", "--evidence-root", "/e"]
    env = {"PATH": "/bin", "HOME": "/h"}
    command = prove.fault_wrapper(argv, env, "pi_send_noop", Path("/tmp/r.jsonl"), 600)
    assert command[:2] == ["sudo", "-n"] and "unshare" in command
    tail = command[command.index("_") + 1 :]
    config = json.loads(tail[0])
    assert config == {"fault": "pi_send_noop", "receipt": "/run/longhouse-qa/receipts.jsonl"}
    inner = tail[2:]
    assert inner[inner.index("env") : inner.index("env") + 2] == ["env", "-i"]
    assert inner[-len(argv) :] == argv and "PATH=/bin" in inner and "HOME=/h" in inner
    assert not any("FAULT" in item or "negative" in item for item in inner)


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"prove-control tests passed ({len(tests)} scenarios)")
