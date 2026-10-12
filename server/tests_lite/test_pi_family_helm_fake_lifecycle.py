"""The Pi-family Helm producers, end to end through their real ``main``, against a fake Longhouse.

The fake (``_pi_family_helm_fake``) stands at the producers' process boundary: the ``longhouse`` and
``longhouse-engine`` executables and the Runtime Host. Its docstring says what a green run does not cover.
"""

from __future__ import annotations

import importlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from tests_lite import _pi_family_helm_fake as fake
from zerg.qa.live_kit import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.live_kit import RUNTIME_API_URL_ENV
from zerg.qa.live_session_toolkit import QUALIFICATION_HOME_ENV

# A whole lifecycle of real waits and process exits; the suite's 60 s guard is for a hang, not for this.
pytestmark = pytest.mark.timeout(240)

_REPO = Path(__file__).resolve().parents[2]
_TOKEN = "fixture-agents-token"


@pytest.fixture
def longhouse(tmp_path, monkeypatch):
    """A fake Longhouse and the one directory a producer may isolate itself in.

    The directory is short and under /tmp because a launcher's socket path has to fit a unix socket address.
    It and every fake process are gone when the test ends, passed or not, and that is checked.
    """

    host = fake.FakeRuntimeHost(_TOKEN)
    cli = tmp_path / "longhouse"
    cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake.__file__}" "$@"\n', encoding="utf-8")
    cli.chmod(0o755)
    isolation = Path(tempfile.mkdtemp(prefix="lhq-", dir="/tmp"))
    monkeypatch.setenv(RUNTIME_API_URL_ENV, host.url)
    monkeypatch.setenv(RUNTIME_AGENTS_TOKEN_ENV, _TOKEN)
    monkeypatch.setenv(QUALIFICATION_HOME_ENV, str(isolation))
    monkeypatch.delenv(fake.FAULT_ENV, raising=False)
    try:
        yield cli, isolation
    finally:
        left_running = host.close()
        shutil.rmtree(isolation, ignore_errors=True)
    assert not left_running, f"fake processes outlived the run and were killed: {left_running}"
    assert not isolation.exists()


def _run(provider: str, cli: Path, tmp_path: Path, monkeypatch) -> tuple[int, dict, Path]:
    module = importlib.import_module(f"zerg.qa.{provider}_helm_lifecycle")
    monkeypatch.setenv(f"LONGHOUSE_{provider.upper()}_LIVE", "1")
    root = tmp_path / "evidence"
    arguments = ["--variant", module.PRODUCER.variants[0], "--evidence-root", str(root), "--repo-root", str(_REPO)]
    arguments += ["--engine", str(cli), "--longhouse-cli", str(cli), "--provider-bin", str(cli), "--provider-version", "0.0.0"]
    arguments += ["--model", "openrouter/fake-model", "--provider", provider, "--platform", "linux", "--architecture", "x86_64"]
    arguments += ["--evidence-class", "live_token", "--credential-binding", "runtime_host_control"]
    code = module.main(arguments)
    return code, json.loads((root / "result.json").read_text(encoding="utf-8")), root


@pytest.mark.parametrize("provider", ["omp", "pi"])
def test_a_whole_lifecycle_holds_every_cell(provider, longhouse, tmp_path, monkeypatch, capsys):
    cli, isolation = longhouse
    code, result, root = _run(provider, cli, tmp_path, monkeypatch)
    capsys.readouterr()

    not_held = [name for name, held in result.get("assertions", {}).items() if not held]
    assert not not_held and result.get("assertions"), {"not held": not_held, "error": result.get("error")}
    assert (code, result["status"]) == (0, "pass")
    assert set(result["assertions"]) == set(importlib.import_module(f"zerg.qa.{provider}_helm_lifecycle").ASSERTIONS)
    assert json.loads((root / "cleanup-receipt.json").read_text(encoding="utf-8"))["status"] == "pass"
    # The producer removed its own isolation; the fixture had nothing to remove.
    assert list(isolation.iterdir()) == []


@pytest.mark.parametrize(("provider", "what_it_had_seen"), [("omp", "partial-observation.json"), ("pi", "failure-envelope.json")])
def test_a_provider_that_dies_mid_run_is_a_typed_failure_with_its_cleanup_proven(
    provider, what_it_had_seen, longhouse, tmp_path, monkeypatch, capsys
):
    cli, _isolation = longhouse
    # The launcher and its provider die, with no terminal state written, once the idle send has been answered.
    monkeypatch.setenv(fake.FAULT_ENV, "exit_after:2")
    code, result, root = _run(provider, cli, tmp_path, monkeypatch)
    capsys.readouterr()

    assert (code, result["status"], result["failure_code"]) == (1, "fail", f"{provider}_helm_lifecycle_failed")
    assert "Runtime Host HTTP 502" in result["error"]
    # Every cell it reached had held, so the run reports no verdict: it is a failure of the run, not of a cell.
    assert not result.get("assertions")
    assert (root / what_it_had_seen).is_file()
    assert json.loads((root / "cleanup-receipt.json").read_text(encoding="utf-8"))["status"] == "pass"
