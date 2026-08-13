from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from zerg.qa import cursor_turn_boundary_producer
from zerg.qa.cursor_turn_boundary_producer import REGISTRATION
from zerg.qa.cursor_turn_boundary_producer import main
from zerg.qa.cursor_turn_boundary_producer import run_turn_boundary


class _FakeShipper:
    def __init__(self) -> None:
        self.receipt = {"status": "started", "provider": "cursor"}
        self.stop_calls = 0

    def stop(self) -> dict[str, Any]:
        self.stop_calls += 1
        return {"status": "stopped", "stop_calls": self.stop_calls}


def _base_args(tmp_path: Path, *, evidence_root: Path) -> argparse.Namespace:
    return argparse.Namespace(
        variant="cell:cursor:activity_returns_to_quiescent_at_turn_boundary:cursor_turn_boundary_quiescent",
        evidence_root=evidence_root,
        repo_root=tmp_path / "repo",
        engine=tmp_path / "longhouse-engine",
        longhouse_cli=tmp_path / "longhouse",
        provider_bin=tmp_path / "cursor-agent",
        model=None,
        timeout_secs=5.0,
        max_archive_lag_secs=5.0,
        api_url="https://runtime.example",
        agents_token="super-secret-token",
    )


def test_registration_shape() -> None:
    assert REGISTRATION.providers == ("cursor",)
    assert REGISTRATION.modes == ("helm",)
    assert REGISTRATION.evidence_classes == ("live_token",)
    assert REGISTRATION.scenario_id == "cursor_turn_boundary_quiescent"
    # No `variant:` is authored for this assertion in
    # schemas/managed_providers.yml, so the cell's variant must be None (not
    # an empty string) to match how required_assertions gets parsed.
    assert REGISTRATION.assertion_cells == (("activity_returns_to_quiescent_at_turn_boundary", None),)
    assert REGISTRATION.executable is True
    assert REGISTRATION.executable_module == "zerg.qa.cursor_turn_boundary_producer"
    assert REGISTRATION.oracle_source == "server/zerg/qa/cursor_helm_product_e2e.py"


def test_registration_cli_flag_prints_registration(capsys) -> None:
    assert main(["--registration"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["producer_id"] == REGISTRATION.producer_id
    assert payload["assertion_cells"] == [{"assertion_id": "activity_returns_to_quiescent_at_turn_boundary", "variant": None}]


def test_run_turn_boundary_passes_when_product_e2e_reports_passed(tmp_path: Path, monkeypatch) -> None:
    args = _base_args(tmp_path, evidence_root=tmp_path / "evidence")
    fake_shipper = _FakeShipper()

    monkeypatch.setattr(cursor_turn_boundary_producer, "_isolated_provider_home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(
        cursor_turn_boundary_producer,
        "_start_transcript_shipper",
        lambda *a, **k: fake_shipper,
    )
    monkeypatch.setattr(cursor_turn_boundary_producer, "_sha256", lambda _path: "sha256:fake")

    def fake_run_product_e2e(e2e_args: argparse.Namespace) -> dict[str, Any]:
        assert e2e_args.skip_machine_agent_restart is True
        assert e2e_args.longhouse_bin == str(args.longhouse_cli)
        assert e2e_args.engine_bin == str(args.engine)
        return {
            "status": "passed",
            "session_id": "session-123",
            "run_lifecycle_after_teardown": "ended",
            "activity_after_teardown": "quiescent",
            "run_id_stable_across_session": True,
            "process_alive_after_cancel": True,
            "archive_lag_seconds": {"first": 0.5, "second": 0.4, "recovery": 0.3, "machine_agent_restart": None},
        }

    monkeypatch.setattr(cursor_turn_boundary_producer.cursor_helm_product_e2e, "run_product_e2e", fake_run_product_e2e)
    monkeypatch.setattr(
        cursor_turn_boundary_producer.subprocess,
        "run",
        lambda *a, **k: type("_R", (), {"stdout": "cursor-agent 1.0.0"})(),
    )

    result = run_turn_boundary(args)

    assert result["status"] == "pass"
    assert result["provider"] == "cursor"
    # Must be None -- the compiled command's raw authored variant -- not the
    # opaque execution_variant scheduling key received via --variant.
    assert result["variant"] is None
    assert result["scenario_id"] == "cursor_turn_boundary_quiescent"
    assert result["scenario_revision"] == REGISTRATION.scenario_revision
    assert result["evidence_class"] == "live_token"
    assert result["assertions"] == {"activity_returns_to_quiescent_at_turn_boundary": True}
    assert result["producer"]["producer_id"] == REGISTRATION.producer_id
    assert result["session_id"] == "session-123"
    assert fake_shipper.stop_calls >= 1

    written = json.loads((args.evidence_root / "result.json").read_text())
    assert written == result
    assert "super-secret-token" not in json.dumps(written)


def test_run_turn_boundary_fails_when_product_e2e_raises(tmp_path: Path, monkeypatch) -> None:
    args = _base_args(tmp_path, evidence_root=tmp_path / "evidence-fail")
    fake_shipper = _FakeShipper()

    monkeypatch.setattr(cursor_turn_boundary_producer, "_isolated_provider_home", lambda: tmp_path / "home2")
    (tmp_path / "home2").mkdir()
    monkeypatch.setattr(
        cursor_turn_boundary_producer,
        "_start_transcript_shipper",
        lambda *a, **k: fake_shipper,
    )
    monkeypatch.setattr(cursor_turn_boundary_producer, "_sha256", lambda _path: "sha256:fake")

    def fake_run_product_e2e_raises(_e2e_args: argparse.Namespace) -> dict[str, Any]:
        raise RuntimeError("timed out waiting for served Cursor activity settling to quiescent after the turn")

    monkeypatch.setattr(cursor_turn_boundary_producer.cursor_helm_product_e2e, "run_product_e2e", fake_run_product_e2e_raises)
    monkeypatch.setattr(
        cursor_turn_boundary_producer.subprocess,
        "run",
        lambda *a, **k: type("_R", (), {"stdout": "cursor-agent 1.0.0"})(),
    )

    result = run_turn_boundary(args)

    assert result["status"] == "fail"
    assert result["variant"] is None
    assert result["scenario_id"] == "cursor_turn_boundary_quiescent"
    assert "quiescent" in result["error"]
    assert fake_shipper.stop_calls >= 1
    written = json.loads((args.evidence_root / "result.json").read_text())
    assert written == result


def test_parser_accepts_the_real_execute_retained_plan_argv_shape(tmp_path: Path) -> None:
    """provider_factory/assurance.py's execute_retained_plan only passes
    --codex-bin for provider == "codex"; every other provider (cursor
    included) gets --longhouse-cli and --provider-bin. This producer's parser
    must accept exactly that shape, not a --cursor-bin flag.
    """

    engine = tmp_path / "longhouse-engine"
    engine.write_text("#!/bin/sh\n")
    engine.chmod(0o755)
    cli = tmp_path / "longhouse"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    provider_bin = tmp_path / "cursor-agent"
    provider_bin.write_text("#!/bin/sh\n")
    provider_bin.chmod(0o755)

    parsed = cursor_turn_boundary_producer._parser().parse_args(
        [
            "--variant",
            "cell:cursor:activity_returns_to_quiescent_at_turn_boundary:cursor_turn_boundary_quiescent",
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--repo-root",
            str(tmp_path),
            "--engine",
            str(engine),
            "--longhouse-cli",
            str(cli),
            "--provider-bin",
            str(provider_bin),
        ]
    )
    assert parsed.longhouse_cli == cli
    assert parsed.provider_bin == provider_bin
