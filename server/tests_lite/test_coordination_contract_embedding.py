"""The provider factory's verifier imports these modules from a pinned bundle that may never gain
a file (control-plane ``verifier.SUBJECT_PINNED`` only shrinks). When they read
``config/coordination_contract.json`` at import, every factory image past the contract refactor
failed to build (Factory Image run 38020297427). They carry the contract inline instead."""

from __future__ import annotations

from pathlib import Path

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
    import importlib.util

    from zerg.mcp_server import server
    from zerg.services.shipper import hooks

    spec = importlib.util.spec_from_file_location(
        "generate_coordination_contract", ROOT / "scripts" / "generate" / "generate_coordination_contract.py"
    )
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    contract = generator.load_contract()
    assert server._COORDINATION_CONTRACT == {"instructions": contract["instructions"], "tools": contract["tools"]}
    assert hooks.COORDINATION_BOOTSTRAP == contract["session_start"]
