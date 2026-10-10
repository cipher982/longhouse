#!/usr/bin/env python3
"""Render schemas/coordination_contract.yml for every coordination surface.

The contract is the one definition of the coordination tools (names,
descriptions, JSON schemas, instructions, the session-start awareness note,
peers line vectors, pending and delivery wording). This writes:

- engine/src/coordination_contract.generated.json   (engine MCP server, include_str!)
- server/zerg/config/coordination_contract.json      (delivery facts)
- the generated block in engine/assets/longhouse-omp-helm.ts (the extension ships
  as one file, so it cannot import JSON)
- the generated blocks in server/zerg/mcp_server/server.py and
  server/zerg/services/shipper/hooks.py. The provider factory's verifier imports
  both, and its pinned bundle may never gain a file (SUBJECT_PINNED only
  shrinks), so they carry the contract inline instead of reading the JSON.

`--check` exits non-zero when any rendered copy differs from the schema.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schemas" / "coordination_contract.yml"
ENGINE_JSON = ROOT / "engine" / "src" / "coordination_contract.generated.json"
SERVER_JSON = ROOT / "server" / "zerg" / "config" / "coordination_contract.json"
OMP_EXTENSION = ROOT / "engine" / "assets" / "longhouse-omp-helm.ts"
OMP_BEGIN = "// BEGIN GENERATED COORDINATION CONTRACT (scripts/generate/generate_coordination_contract.py)"
OMP_END = "// END GENERATED COORDINATION CONTRACT"
MCP_SERVER = ROOT / "server" / "zerg" / "mcp_server" / "server.py"
SHIPPER_HOOKS = ROOT / "server" / "zerg" / "services" / "shipper" / "hooks.py"
PY_BEGIN = "# BEGIN GENERATED COORDINATION CONTRACT (scripts/generate/generate_coordination_contract.py)"
PY_END = "# END GENERATED COORDINATION CONTRACT"

TOOL_NAMES = ("search_sessions", "recall", "recall_context", "peers", "tail", "send", "inbox", "reply")
DELIVERY_STATES = ("stored", "queued", "delivering", "delivered", "steered", "expired", "failed", "cancelled", "unknown")
PENDING_STATES = ("retrying", "stopped", "unknown")


def _fail(message: str) -> None:
    raise SystemExit(f"{SCHEMA_PATH.relative_to(ROOT)}: {message}")


def load_contract() -> dict:
    payload = yaml.safe_load(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        _fail("must contain a YAML object")
    tools = payload.get("tools")
    if not isinstance(tools, list) or tuple(tool.get("name") for tool in tools) != TOOL_NAMES:
        _fail(f"tools must be exactly {', '.join(TOOL_NAMES)} in that order")
    rendered_tools = []
    for tool in tools:
        properties = tool.get("properties") or {}
        required = list(tool.get("required") or [])
        missing = [name for name in required if name not in properties]
        if missing:
            _fail(f"{tool['name']} requires undeclared properties {missing}")
        if not str(tool.get("description") or "").strip():
            _fail(f"{tool['name']} has no description")
        schema: dict = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        rendered_tools.append({"name": tool["name"], "description": tool["description"].strip(), "inputSchema": schema})
    for key, states in (("delivery", DELIVERY_STATES), ("registration_pending", PENDING_STATES)):
        section = payload.get(key) or {}
        if set(section) != set(states):
            _fail(f"{key} must define exactly {', '.join(states)}")
    if not str(payload.get("session_start") or "").strip():
        _fail("session_start must be set")
    vectors = (payload.get("peers_line") or {}).get("vectors") or []
    if not vectors:
        _fail("peers_line needs vectors")
    return {
        "version": payload["version"],
        "instructions": str(payload["instructions"]).strip(),
        "session_start": str(payload["session_start"]).strip(),
        "tools": rendered_tools,
        "peers_line": payload["peers_line"],
        "registration_pending": payload["registration_pending"],
        "delivery": payload["delivery"],
    }


def render_json(contract: dict) -> str:
    return json.dumps(contract, indent=2, ensure_ascii=False) + "\n"


def render_omp(contract: dict, current: str) -> str:
    begin = current.find(OMP_BEGIN)
    end = current.find(OMP_END)
    if begin < 0 or end < begin:
        raise SystemExit(f"{OMP_EXTENSION.relative_to(ROOT)} lacks the generated coordination contract markers")
    block = (
        f"{OMP_BEGIN}\n"
        "// Do not edit: run the generator. Source: schemas/coordination_contract.yml\n"
        f"const COORDINATION_CONTRACT = {json.dumps(contract, indent=2, ensure_ascii=False)} as const;\n"
    )
    return current[:begin] + block + current[end:]


def _python_literal(value: object) -> str:
    """A raw triple-quoted JSON literal the module parses at import, no file read."""

    text = json.dumps(value, indent=2, ensure_ascii=False)
    if '"""' in text or text.endswith("\\"):
        raise SystemExit("coordination contract text cannot be embedded in a raw triple-quoted string")
    return f'json.loads(\n    r"""\n{text}\n"""\n)'


def render_python_block(path: Path, current: str, assignment: str) -> str:
    begin = current.find(PY_BEGIN)
    end = current.find(PY_END)
    if begin < 0 or end < begin:
        raise SystemExit(f"{path.relative_to(ROOT)} lacks the generated coordination contract markers")
    block = f"{PY_BEGIN}\n# Do not edit: run the generator. Source: schemas/coordination_contract.yml\n{assignment}\n"
    return current[:begin] + block + current[end:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()

    contract = load_contract()
    rendered = render_json(contract)
    omp_current = OMP_EXTENSION.read_text(encoding="utf-8")
    targets = {
        ENGINE_JSON: rendered,
        SERVER_JSON: rendered,
        OMP_EXTENSION: render_omp(contract, omp_current),
        MCP_SERVER: render_python_block(
            MCP_SERVER,
            MCP_SERVER.read_text(encoding="utf-8"),
            f"_COORDINATION_CONTRACT = {_python_literal(contract)}",
        ),
        SHIPPER_HOOKS: render_python_block(
            SHIPPER_HOOKS,
            SHIPPER_HOOKS.read_text(encoding="utf-8"),
            f"COORDINATION_BOOTSTRAP: str = {_python_literal(contract['session_start'])}",
        ),
    }
    drifted = [path for path, text in targets.items() if not path.exists() or path.read_text(encoding="utf-8") != text]
    if args.check:
        for path in drifted:
            print(f"coordination contract drift: {path.relative_to(ROOT)} (run {Path(__file__).relative_to(ROOT)} --write)")
        return 1 if drifted else 0
    for path in drifted:
        path.write_text(targets[path], encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
