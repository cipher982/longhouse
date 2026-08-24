#!/usr/bin/env python3
"""Every launchable provider must claim its launch identity through the overlay.

The recurring defect is not a wrong value, it is an absent one: a launcher spawns
a provider process and simply never says which session it belongs to, or says it
in a name nothing reads. Five of thirteen launch sites had some form of this on
2026-08-24, and a person found every one.

A drift check cannot see it. There is no second artifact disagreeing -- the
provider was never in the set. So this asks the dual question: for every provider
the schema says Longhouse can launch, does something actually construct a
`ManagedIdentity` for it?

The registry is `schemas/managed_providers.yml`, which is load-bearing: the
factory, the census, the contract digest and the generated Rust enum all derive
from it, so a provider missing from it is broken long before this check runs.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "schemas" / "managed_providers.yml"
ENGINE_SRC = ROOT / "engine" / "src"
OVERLAY = "managed_identity.rs"
CONTRACT = "managed_identity_contract.rs"

# A launcher may only name these keys through the overlay. Hand-writing them is
# how each site drifted from the others in the first place.
GUARDED = ("LONGHOUSE_MANAGED_SESSION_ID", "LONGHOUSE_MANAGED_PROVIDER")


def _variant(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _launchable() -> list[str]:
    payload = yaml.safe_load(SCHEMA.read_text(encoding="utf-8"))
    return sorted(str(row["provider"]) for row in payload["providers"] if row.get("launch_local"))


def _sources() -> dict[Path, str]:
    return {
        path: path.read_text(encoding="utf-8", errors="ignore")
        for path in sorted(ENGINE_SRC.rglob("*.rs"))
        if path.name not in (OVERLAY, CONTRACT)
    }


def _console_adapters() -> dict[str, str]:
    """Each provider's Console adapter module, named by the schema itself.

    This is what makes the check per-lane rather than per-provider. Asking only
    "does this provider appear beside a constructor somewhere?" passes when one
    of a provider's launchers routes and the rest do not -- which is the exact
    shape of the bug, since Codex had four launch sites and one of them was
    wrong.
    """
    payload = yaml.safe_load(SCHEMA.read_text(encoding="utf-8"))
    return {
        str(row["provider"]): str(row["console_adapter"])
        for row in payload["providers"]
        if row.get("launch_local") and row.get("console_adapter")
    }


def main() -> int:
    providers = _launchable()
    sources = _sources()
    adapters = _console_adapters()
    findings: list[str] = []

    for provider in providers:
        variant = _variant(provider)
        pattern = re.compile(r"ManagedIdentity::new\(\s*ManagedProvider::%s\b" % re.escape(variant))
        sites = [path for path, text in sources.items() if pattern.search(text)]
        if not sites:
            findings.append(
                f"{provider}: schema says launch_local, but nothing constructs "
                f"ManagedIdentity::new(ManagedProvider::{variant}, ..). "
                f"A launch that claims no identity is the defect this check exists for."
            )
            continue

        # Per-lane: the Console adapter the schema names for this provider must
        # itself claim the identity. One routed launcher must not cover for the
        # rest.
        adapter = adapters.get(provider)
        if adapter:
            adapter_path = ENGINE_SRC / f"{adapter}.rs"
            if adapter_path.exists() and not pattern.search(sources.get(adapter_path, "")):
                findings.append(
                    f"{provider}: schema names {adapter} as its Console adapter, but "
                    f"{adapter}.rs does not claim identity for {provider}. Another "
                    f"launcher doing so does not cover this one."
                )

    # Constructing without applying claims nothing. Every construction must reach
    # an applier, or it is decoration that satisfies the check above and changes
    # no process environment.
    construction = re.compile(r"ManagedIdentity::new\([^;]*?\)(?P<tail>[^;]*);", re.S)
    for path, text in sources.items():
        for match in construction.finditer(text):
            if ".apply(" in match.group("tail") or ".apply_to_pairs(" in match.group("tail"):
                continue
            line = text[: match.start()].count("\n") + 1
            findings.append(
                f"{path.relative_to(ROOT)}:{line}: constructs a ManagedIdentity and "
                f"never applies it. A claim that reaches no process environment is "
                f"decoration."
            )

    for path, text in sources.items():
        for line_number, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("//"):
                continue
            for key in GUARDED:
                if f'"{key}"' in line and "env_remove" not in line and ".env(" in line:
                    findings.append(
                        f"{path.relative_to(ROOT)}:{line_number}: sets {key} by hand. "
                        f"Route it through ManagedIdentity so every launch site "
                        f"carries the same contract."
                    )

    if findings:
        print("Managed launch identity is not claimed everywhere the schema says it is:\n")
        for finding in findings:
            print(f"  - {finding}")
        print(
            f"\nChecked {len(providers)} launchable providers from "
            f"{SCHEMA.relative_to(ROOT)} against {len(sources)} engine sources."
        )
        return 1

    print(
        f"Managed launch identity: {len(providers)} launchable providers "
        f"({', '.join(providers)}) claim identity through the overlay, each in the "
        f"Console adapter the schema names for it, and every construction is applied."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
