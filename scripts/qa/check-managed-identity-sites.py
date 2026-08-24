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


def main() -> int:
    providers = _launchable()
    sources = _sources()
    findings: list[str] = []

    for provider in providers:
        pattern = re.compile(
            r"ManagedIdentity::new\(\s*ManagedProvider::%s\b" % re.escape(_variant(provider))
        )
        sites = [path.relative_to(ROOT) for path, text in sources.items() if pattern.search(text)]
        if not sites:
            findings.append(
                f"{provider}: schema says launch_local, but nothing constructs "
                f"ManagedIdentity::new(ManagedProvider::{_variant(provider)}, ..). "
                f"A launch that claims no identity is the defect this check exists for."
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
        f"({', '.join(providers)}) each claim identity through the overlay."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
