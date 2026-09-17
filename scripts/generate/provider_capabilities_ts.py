#!/usr/bin/env python3
"""Generate the web's provider capability claims from the provider contract.

`web/src/lib/providers.ts` held a hand-written `LAUNCH_PROVIDER_SUPPORT` table
whose header simultaneously called itself "single source of truth for provider
capability claims" and said it "mirrors managed_provider_contracts.json". Both
cannot be true, and the mirror has drifted twice: `4402f99ea` fixed a matrix
that understated Cursor and Antigravity, and `6432e21fa` had to fix Antigravity
again the same day. Its guard test checked five of eight fields.

These are the fields the contract can answer. The rest -- marketing name,
archive visibility, hooks support, telemetry quality -- have no contract
counterpart and stay hand-maintained in providers.ts, now visibly separated
from the derived ones instead of interleaved with them.

Sources:
  - server/zerg/config/managed_provider_contracts.json (capability flags, proof edges)
  - config/native_device_entrypoints.json (which `longhouse <provider>` exists)
Output:
  - web/src/generated/provider-capabilities.ts
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONTRACTS = REPO / "server" / "zerg" / "config" / "managed_provider_contracts.json"
ENTRYPOINTS = REPO / "config" / "native_device_entrypoints.json"
TS_OUT = REPO / "web" / "src" / "generated" / "provider-capabilities.ts"

# The chip -> proof edge definition is shared with the Runtime Host's
# certification rollup, so the covered and certified layers read one graph.
sys.path.insert(0, str(REPO / "server"))
from zerg.services.provider_chip_edges import CHIP_CAPABILITIES  # noqa: E402,F401
from zerg.services.provider_chip_edges import CHIP_OPERATIONS  # noqa: E402,F401
from zerg.services.provider_chip_edges import capability_proven  # noqa: E402,F401
from zerg.services.provider_chip_edges import chip_states  # noqa: E402
from zerg.services.provider_chip_edges import operation_proven  # noqa: E402,F401


def _native_launch_commands() -> dict[str, str]:
    """`longhouse <provider>` per provider, for entrypoints that actually ship.

    Deliberately not derived from `launch_local`: a provider can support
    launching while its device entrypoint stays `excluded`, which is exactly
    Antigravity's state. Telling a user to run a command that does not exist is
    worse than saying nothing.
    """

    payload = json.loads(ENTRYPOINTS.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for command in payload.get("commands") or []:
        if not isinstance(command, dict) or command.get("status") != "available":
            continue
        providers = command.get("providers")
        target = str(command.get("native_target_command") or "").strip()
        if isinstance(providers, list) and len(providers) == 1 and target:
            out[str(providers[0])] = target
    return out


def _rows() -> list[dict[str, object]]:
    payload = json.loads(CONTRACTS.read_text(encoding="utf-8"))
    launch_commands = _native_launch_commands()
    rows: list[dict[str, object]] = []
    for provider in payload["providers"]:
        name = str(provider["provider"])
        capabilities = provider.get("capabilities") or {}
        console_turn_admitted = isinstance(capabilities, dict) and "session.turn.start" in capabilities
        supports = provider.get("machine_control_supports") or []
        turn_interrupt = f"{name}.turn_interrupt" in supports
        rows.append(
            {
                "id": name,
                # Runtime capability, for reference docs. Both folds admit the
                # Console lane, not just the live one: Pi is the case that forces
                # it -- `longhouse pi` ships and its Console turns start and
                # interrupt. These never light a landing chip; `proven` does.
                "launchAndSend": bool(provider["launch_local"]) and (bool(provider["send_input"]) or bool(provider["turn_start"])),
                "interrupt": (bool(provider["interrupt"]) and bool(provider["terminate"])) or turn_interrupt,
                "steerMidTurn": bool(provider["steer_active_turn"]),
                "resume": bool(provider["can_resume"]),
                "proven": chip_states(provider),
                # The low-level turn_start flag describes adapter inventory.
                # User-facing Console admission requires an implemented
                # session.turn.start capability; Antigravity deliberately has
                # the former but not the latter.
                "cloudSessionStart": "live" if console_turn_admitted else "none",
                "nativeLaunchCommand": launch_commands.get(name),
            }
        )
    return sorted(rows, key=lambda row: str(row["id"]))


def render_ts() -> str:
    rows = _rows()
    ids = " | ".join(f'"{row["id"]}"' for row in rows)
    lines = [
        "// GENERATED FILE - DO NOT EDIT.",
        "// Source: server/zerg/config/managed_provider_contracts.json",
        "//         config/native_device_entrypoints.json",
        "// Regenerate: make generate-provider-capabilities",
        "//",
        "// Only fields the provider contract can answer live here. Marketing name,",
        "// archive visibility, hooks support and telemetry quality have no contract",
        "// counterpart and remain hand-maintained in ../lib/providers.ts.",
        "",
        f"export type GeneratedProviderId = {ids};",
        "",
        "// Landing chips: lit only by live-token factory assertions behind every",
        "// backing operation. The runtime capability fields never light a chip.",
        "export type ProvenChips = {",
        "  readonly search: boolean;",
        "  readonly launchAndSend: boolean;",
        "  readonly interrupt: boolean;",
        "  readonly steerMidTurn: boolean;",
        "  readonly resume: boolean;",
        "};",
        "",
        "export type GeneratedProviderCapabilities = {",
        "  readonly id: GeneratedProviderId;",
        "  readonly launchAndSend: boolean;",
        "  readonly interrupt: boolean;",
        "  readonly steerMidTurn: boolean;",
        "  readonly resume: boolean;",
        "  readonly proven: ProvenChips;",
        '  readonly cloudSessionStart: "live" | "none";',
        "  readonly nativeLaunchCommand: string | null;",
        "};",
        "",
        "export const GENERATED_PROVIDER_CAPABILITIES: Record<GeneratedProviderId, GeneratedProviderCapabilities> = {",
    ]
    for row in rows:
        command = row["nativeLaunchCommand"]
        command_literal = "null" if command is None else f'"{command}"'
        lines.extend(
            [
                f"  {row['id']}: {{",
                f'    id: "{row["id"]}",',
                f"    launchAndSend: {str(row['launchAndSend']).lower()},",
                f"    interrupt: {str(row['interrupt']).lower()},",
                f"    steerMidTurn: {str(row['steerMidTurn']).lower()},",
                f"    resume: {str(row['resume']).lower()},",
                "    proven: {",
                *(f"      {chip}: {str(value).lower()}," for chip, value in row["proven"].items()),
                "    },",
                f'    cloudSessionStart: "{row["cloudSessionStart"]}",',
                f"    nativeLaunchCommand: {command_literal},",
                "  },",
            ]
        )
    lines.extend(["};", ""])
    return "\n".join(lines)


def main() -> int:
    check = "--check" in sys.argv
    rendered = render_ts()
    if check:
        current = TS_OUT.read_text(encoding="utf-8") if TS_OUT.exists() else ""
        if current != rendered:
            print(f"{TS_OUT} is out of date; run scripts/generate/provider_capabilities_ts.py", file=sys.stderr)
            return 1
        return 0
    TS_OUT.parent.mkdir(parents=True, exist_ok=True)
    TS_OUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {TS_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
