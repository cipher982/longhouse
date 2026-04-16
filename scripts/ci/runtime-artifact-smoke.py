#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from zerg.services.runtime_artifacts import RuntimeComponent
from zerg.services.runtime_artifacts import ensure_runtime_artifact

COMPONENT_CHOICES = [
    RuntimeComponent.ENGINE.value,
    RuntimeComponent.MANAGED_CODEX.value,
    RuntimeComponent.DESKTOP_APP.value,
    RuntimeComponent.DESKTOP_WINDOW.value,
    "local-health-app",
    "local-health-window",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Install or verify a Longhouse runtime artifact.")
    parser.add_argument("--component", required=True, choices=COMPONENT_CHOICES)
    parser.add_argument("--overwrite", action="store_true", help="Force a reinstall even if the artifact already exists.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args()

    artifact = ensure_runtime_artifact(RuntimeComponent(args.component), overwrite=args.overwrite)
    payload = asdict(artifact)
    payload["component"] = artifact.component.value
    payload["kind"] = artifact.kind.value

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"{artifact.component.value}: {artifact.path}")
        print(f"  launch: {artifact.launch_path}")
        print(f"  source: {artifact.source}")
        print(f"  installed_now: {'yes' if artifact.installed_now else 'no'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
