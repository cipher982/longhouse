"""The provider factory's verifier imports these modules from a pinned bundle that may never gain
a file (control-plane ``verifier.SUBJECT_PINNED`` only shrinks). When they read
``config/coordination_contract.json`` at import, every factory image past the contract refactor
failed to build (Factory Image run 38020297427). They carry the contract inline instead."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PINNED_READERS = (
    ROOT / "server" / "zerg" / "mcp_server" / "server.py",
    ROOT / "server" / "zerg" / "services" / "shipper" / "hooks.py",
)


def test_pinned_contract_readers_read_no_file_at_import() -> None:
    for path in PINNED_READERS:
        text = path.read_text(encoding="utf-8")
        assert "coordination_contract.json" not in text, path
        assert "# BEGIN GENERATED COORDINATION CONTRACT" in text, path


def test_the_inline_copies_match_the_schema() -> None:
    from zerg.mcp_server import server
    from zerg.services.shipper import hooks

    schema = yaml.safe_load((ROOT / "schemas" / "coordination_contract.yml").read_text(encoding="utf-8"))
    assert server.COORDINATION_INSTRUCTIONS == str(schema["instructions"]).strip()
    assert hooks.COORDINATION_BOOTSTRAP == str(schema["session_start"]).strip()
