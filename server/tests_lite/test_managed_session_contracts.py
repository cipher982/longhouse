from __future__ import annotations

from pathlib import Path

from zerg.services.managed_session_contracts import REASON_BRIDGE_STATE_PATH_MISSING
from zerg.services.managed_session_contracts import REASON_PROVIDER_SESSION_CWD_MISSING
from zerg.services.managed_session_contracts import REASON_PROVIDER_SESSION_CWD_REPLACED
from zerg.services.managed_session_contracts import build_managed_session_contract
from zerg.services.managed_session_contracts import collect_managed_session_contract_diagnostics
from zerg.services.managed_session_contracts import current_path_file_identity
from zerg.services.managed_session_contracts import list_managed_session_contracts
from zerg.services.managed_session_contracts import write_managed_session_contract


def test_managed_session_contract_round_trips_private_file(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    state_path = tmp_path / "bridge" / "sess-1.json"
    state_path.parent.mkdir()
    state_path.write_text("{}", encoding="utf-8")
    contract = build_managed_session_contract(
        session_id="sess-1",
        provider="codex",
        cwd=workspace,
        launch_mode="tui",
        provider_binary_path="/opt/homebrew/bin/codex",
        provider_binary_source="PATH",
        provider_version="codex 0.133.0",
        control_kind="codex_bridge",
        control_state_path=state_path,
    )

    path = write_managed_session_contract(contract, base_dir=tmp_path)

    assert path == tmp_path / "managed-local" / "contracts" / "codex" / "sess-1.json"
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = list_managed_session_contracts(tmp_path)
    assert loaded[0]["session_id"] == "sess-1"
    assert loaded[0]["workspace"]["file_identity"] == current_path_file_identity(workspace)


def test_contract_diagnostics_report_missing_workspace(tmp_path: Path):
    missing_workspace = tmp_path / "deleted"
    contract = build_managed_session_contract(
        session_id="sess-1",
        provider="claude",
        cwd=missing_workspace,
        control_kind="claude_channel",
    )
    write_managed_session_contract(contract, base_dir=tmp_path)

    diagnostics = collect_managed_session_contract_diagnostics(tmp_path)

    assert diagnostics["state"] == "degraded"
    assert diagnostics["issues"][0]["reason"] == REASON_PROVIDER_SESSION_CWD_MISSING
    assert diagnostics["issues"][0]["session_id"] == "sess-1"
    assert diagnostics["issues"][0]["detail"]["cwd"] == str(missing_workspace)


def test_contract_diagnostics_report_replaced_workspace(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    contract = build_managed_session_contract(
        session_id="sess-1",
        provider="opencode",
        cwd=workspace,
        control_kind="opencode_bridge",
    )
    write_managed_session_contract(contract, base_dir=tmp_path)
    workspace.rmdir()
    workspace.mkdir()

    diagnostics = collect_managed_session_contract_diagnostics(tmp_path)

    assert diagnostics["state"] == "degraded"
    assert diagnostics["issues"][0]["reason"] == REASON_PROVIDER_SESSION_CWD_REPLACED
    assert diagnostics["issues"][0]["detail"]["cwd"] == str(workspace)


def test_contract_diagnostics_report_missing_bridge_state(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    state_path = tmp_path / "bridge" / "missing.json"
    contract = build_managed_session_contract(
        session_id="sess-1",
        provider="codex",
        cwd=workspace,
        control_kind="codex_bridge",
        control_state_path=state_path,
    )
    write_managed_session_contract(contract, base_dir=tmp_path)

    diagnostics = collect_managed_session_contract_diagnostics(tmp_path)

    assert diagnostics["state"] == "degraded"
    assert diagnostics["issues"][0]["reason"] == REASON_BRIDGE_STATE_PATH_MISSING
    assert diagnostics["issues"][0]["detail"]["state_path"] == str(state_path)
