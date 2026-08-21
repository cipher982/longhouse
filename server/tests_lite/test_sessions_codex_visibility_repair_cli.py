from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from zerg.cli import sessions


def test_local_codex_visibility_repair_cli_round_trips_dry_run_fingerprint(monkeypatch):
    calls: list[tuple[Path, str, dict[str, object]]] = []
    fingerprint = "a" * 64

    class FakeCatalogClient:
        def __init__(self, socket_path: Path) -> None:
            self.socket_path = socket_path

        async def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            calls.append((self.socket_path, method, params))
            return {
                "eligible": True,
                "applied": params["dry_run"] is False,
                "dry_run": params["dry_run"],
                "expected_fingerprint": fingerprint,
            }

        async def close(self) -> None:
            return None

    socket_path = Path("/runtime/.catalogd/catalogd.sock")
    monkeypatch.setattr(sessions, "CatalogClient", FakeCatalogClient)
    monkeypatch.setattr(sessions, "catalogd_paths", lambda: (Path("/runtime/live.db"), socket_path))
    session_id = str(uuid4())
    runner = CliRunner()

    dry_run = runner.invoke(sessions.app, ["repair-codex-launch-visibility", session_id])
    assert dry_run.exit_code == 0, dry_run.output
    assert json.loads(dry_run.output)["expected_fingerprint"] == fingerprint
    assert calls[-1] == (
        socket_path,
        "session.repair.codex_launch_visibility.v2",
        {"session_id": session_id, "dry_run": True, "expected_fingerprint": None},
    )

    apply = runner.invoke(
        sessions.app,
        [
            "repair-codex-launch-visibility",
            session_id,
            "--apply",
            "--expected-fingerprint",
            fingerprint,
        ],
    )
    assert apply.exit_code == 0, apply.output
    assert json.loads(apply.output)["applied"] is True
    assert calls[-1][2] == {
        "session_id": session_id,
        "dry_run": False,
        "expected_fingerprint": fingerprint,
    }


def test_local_codex_visibility_repair_cli_requires_dry_run_receipt():
    result = CliRunner().invoke(
        sessions.app,
        ["repair-codex-launch-visibility", str(uuid4()), "--apply"],
    )
    assert result.exit_code == 2
    assert "requires --expected-fingerprint from dry-run" in result.output
