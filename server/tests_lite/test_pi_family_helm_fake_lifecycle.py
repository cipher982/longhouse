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
from tests_lite._factory_envelope import assert_result_conforms
from zerg.qa.live_kit import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.live_kit import RUNTIME_API_URL_ENV
from zerg.qa.live_session_toolkit import QUALIFICATION_HOME_ENV
from zerg.qa.openrouter_routing import OPENROUTER_QUALIFICATION_ROUTING

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


def _arguments(provider: str, module, cli: Path, root: Path) -> list[str]:
    arguments = ["--variant", module.PRODUCER.variants[0], "--evidence-root", str(root), "--repo-root", str(_REPO)]
    arguments += ["--engine", str(cli), "--longhouse-cli", str(cli), "--provider-bin", str(cli), "--provider-version", "0.0.0"]
    arguments += ["--model", "openrouter/fake-model", "--provider", provider, "--platform", "linux", "--architecture", "x86_64"]
    return [*arguments, "--evidence-class", "live_token", "--credential-binding", "runtime_host_control"]


def _run(provider: str, cli: Path, tmp_path: Path, monkeypatch) -> tuple[int, dict, Path]:
    module = importlib.import_module(f"zerg.qa.{provider}_helm_lifecycle")
    monkeypatch.setenv(f"LONGHOUSE_{provider.upper()}_LIVE", "1")
    root = tmp_path / "evidence"
    code = module.main(_arguments(provider, module, cli, root))
    return code, json.loads((root / "result.json").read_text(encoding="utf-8")), root


@pytest.mark.parametrize("provider", ["omp", "pi"])
def test_a_whole_lifecycle_holds_every_cell(provider, longhouse, tmp_path, monkeypatch, capsys):
    cli, isolation = longhouse
    code, result, root = _run(provider, cli, tmp_path, monkeypatch)
    capsys.readouterr()

    not_held = [name for name, held in result.get("assertions", {}).items() if not held]
    assert not not_held and result.get("assertions"), {"not held": not_held, "error": result.get("error")}
    assert (code, result["status"]) == (0, "pass")
    _conforms(provider, result)
    assert set(result["assertions"]) == set(importlib.import_module(f"zerg.qa.{provider}_helm_lifecycle").ASSERTIONS)
    assert json.loads((root / "cleanup-receipt.json").read_text(encoding="utf-8"))["status"] == "pass"
    # The producer removed its own isolation; the fixture had nothing to remove.
    assert list(isolation.iterdir()) == []


def _conforms(provider: str, result: dict, cell: str = "launch_registration") -> None:
    """The factory would take ``result`` as the answer to its command for ``cell``."""

    module = importlib.import_module(f"zerg.qa.{provider}_helm_lifecycle")
    variant = module.PRODUCER.variants[module.ASSERTIONS.index(f"{provider}_helm_{cell}")]
    assert_result_conforms(module, result, variant=variant)


@pytest.mark.parametrize("provider", ["omp", "pi"])
def test_a_provider_that_dies_mid_run_is_a_typed_failure_with_what_it_reached_kept(provider, longhouse, tmp_path, monkeypatch, capsys):
    cli, _isolation = longhouse
    # The launcher and its provider die, with no terminal state written, once the idle send has been answered.
    monkeypatch.setenv(fake.FAULT_ENV, "exit_after:2")
    code, result, root = _run(provider, cli, tmp_path, monkeypatch)
    capsys.readouterr()

    _conforms(provider, result)
    assert (code, result["status"], result["failure_code"]) == (1, "fail", f"{provider}_helm_lifecycle_failed")
    assert "Runtime Host HTTP 502" in result["error"]
    # Every cell it reached had held, so the run reports no verdict: it is a failure of the run, not of a cell.
    assert "observation" not in result and "assertions" not in result
    cleanup = json.loads((root / "cleanup-receipt.json").read_text(encoding="utf-8"))
    assert cleanup["status"] == "pass"
    # What it had reached stays as evidence: the send that held, and nothing for the follow-up that never ran.
    if provider == "omp":
        reached = result["partial_observation"]
        assert reached["send_idle"] is True and "follow_up_evidence" not in reached
        assert reached["cleanup"] == cleanup
        assert (root / "partial-observation.json").is_file()
    else:
        reached = result["diagnostic_observation"]
        assert reached["send_idle"] is True and "follow_up_native" not in reached
        assert (root / "failure-envelope.json").is_file()
        routing = json.loads((root / "openrouter-routing-receipt.json").read_text(encoding="utf-8"))
        assert (routing["model_id"], routing["routing"]) == ("fake-model", OPENROUTER_QUALIFICATION_ROUTING)


@pytest.mark.parametrize("provider", ["omp", "pi"])
def test_a_failing_verdict_reached_before_a_late_failure_is_kept(provider, longhouse, tmp_path, monkeypatch, capsys):
    cli, _isolation = longhouse
    # The steer is held until its turn is over, which the steer cell must refuse; then the provider dies.
    monkeypatch.setenv(fake.FAULT_ENV, "steer_as_follow_up,exit_after:6")
    code, result, _root = _run(provider, cli, tmp_path, monkeypatch)
    capsys.readouterr()

    _conforms(provider, result, cell="steer_active")
    assert (code, result["status"], result["failure_code"]) == (1, "fail", f"{provider}_helm_lifecycle_failed")
    steer = f"{provider}_helm_steer_active"
    if provider == "omp":
        # The verdicts of the steps it reached, the failing one among them; none for a step it did not reach.
        assert result["assertions"] == {"omp_helm_send_idle": True, "omp_helm_follow_up_native": True, steer: False}
        assert result["observation"]["steer_turn_verdict"]["code"] == "steer_delivered_as_queued_follow_up"
    else:
        # Pi reports no verdict of a failed run: what it had decided is diagnostic only.
        assert "assertions" not in result
        assert result["diagnostic_assertions"][steer] is False
        assert result["diagnostic_observation"]["steer_turn_verdict"]["code"] == "steer_delivered_as_queued_follow_up"


def test_the_background_scope_ends_after_its_first_turn(longhouse, tmp_path, monkeypatch, capsys):
    """The OMP background scenario rides this lifecycle for one turn: none of the Helm controls run."""

    cli, _isolation = longhouse
    module = importlib.import_module("zerg.qa.omp_helm_lifecycle")
    monkeypatch.setenv("LONGHOUSE_OMP_LIVE", "1")
    args = module.PRODUCER.parser().parse_args(_arguments("omp", module, cli, tmp_path / "evidence"))
    args.background_prompt = "Start one background job and wait for it."
    result = module.run_omp_helm(args)
    capsys.readouterr()

    assert result["observation"]["scenario_scope"] == "background"
    assert result["observation"]["send_idle"] is False and "send_evidence" not in result["observation"]
    assert "error" not in result and result["observation_scope"] == "scenario"
    assert result["observation"]["cleanup"]["status"] == "pass"
