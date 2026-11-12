#!/usr/bin/env python3
"""Run the engine rustfmt check when a rustfmt is installed.

The same command as CI's "Engine rustfmt gate" step, run from `make validate`
so a formatting miss is caught before a push. The test image carries rustfmt;
a machine without one gets a one-line note and a pass.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def cargo_candidates() -> list[Path]:
    found: list[Path] = []
    on_path = shutil.which("cargo")
    if on_path:
        found.append(Path(on_path))
    home = Path.home()
    found.append(home / ".cargo" / "bin" / "cargo")
    found.extend(sorted((home / ".rustup" / "toolchains").glob("*/bin/cargo")))
    return [path for path in found if path.is_file()]


def run(cargo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ)
    env["PATH"] = f"{cargo.parent}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run([str(cargo), *args], cwd=REPO_ROOT, env=env, capture_output=True)


def main() -> int:
    for cargo in cargo_candidates():
        if run(cargo, "fmt", "--version").returncode != 0:
            continue
        check = run(cargo, "fmt", "--manifest-path", "engine/Cargo.toml", "--", "--check")
        if check.returncode != 0:
            sys.stdout.buffer.write(check.stdout)
            sys.stderr.buffer.write(check.stderr)
            print(
                "validate-engine-format: engine code is not rustfmt-clean. "
                "Fix with: cargo fmt --manifest-path engine/Cargo.toml",
                file=sys.stderr,
            )
        return check.returncode
    print("validate-engine-format: no rustfmt found, skipped (CI enforces it)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
