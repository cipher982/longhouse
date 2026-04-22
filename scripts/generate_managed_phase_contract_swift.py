#!/usr/bin/env python3

from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_ROOT = REPO_ROOT / "server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from zerg.managed_phase_contract import render_swift_source


def main() -> int:
    output_path = (
        REPO_ROOT
        / "desktop"
        / "LonghouseMenuBarHarness"
        / "Sources"
        / "LonghouseMenuBarCore"
        / "ManagedPhaseContract.generated.swift"
    )
    output_path.write_text(render_swift_source())
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
