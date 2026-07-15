"""Cursor must not retain retired legacy-ingest paths while v2 is the product truth."""

from __future__ import annotations

from pathlib import Path

from zerg.services.managed_provider_contracts import contract_for_provider


def test_cursor_console_is_receipt_backed_while_helm_binding_stays_explicitly_gated():
    contract = contract_for_provider("cursor")

    assert contract is not None
    assert contract.launch_local is True
    assert contract.send_input is True
    assert contract.interrupt is True
    assert contract.terminate is True
    assert contract.launch_remote is True
    assert contract.run_once is True
    # Console resume is adapter-owned turn execution, not Helm reattach.
    assert contract.can_resume is False
    assert contract.tail_output is True
    assert contract.runtime_phase is True
    assert contract.transcript_binding is True
    assert "cursor.turn_start" in contract.machine_control_supports
    assert "cursor.resume_run_once" not in contract.machine_control_supports


def test_cursor_cli_and_helm_do_not_produce_legacy_ingest_payloads():
    repo_root = Path(__file__).resolve().parents[2]
    for relative_path in (
        "server/zerg/cli/cursor.py",
        "server/zerg/cli/cursor_helm.py",
        "server/zerg/services/cursor_transcript.py",
    ):
        source = (repo_root / relative_path).read_text(encoding="utf-8")
        assert "/api/agents/ingest" not in source
        assert "AgentsStore" not in source
